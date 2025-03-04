# mypy: ignore-errors
import logging
import numpy as np
import numpy.typing as npt
import pandas as pd

from BorutaShap import BorutaShap
from catboost import CatBoostClassifier, Pool, EFstrType, EFeaturesSelectionAlgorithm, EShapCalcType
from feature_engine.selection import (SmartCorrelatedSelection, RecursiveFeatureAddition,
                                      DropHighPSIFeatures, DropCorrelatedFeatures)
from freqtrade.freqai.data_kitchen import FreqaiDataKitchen
from freqtrade.freqai.freqai_interface import IFreqaiModel
from freqtrade.litmus.model_helpers import (MergedModel, exclude_weak_features,
                                            get_low_ranked_features_rfe,
                                            threshold_from_optimum_f1,
                                            threshold_from_desired_precision)
from freqtrade.litmus.db_helpers import save_df_to_db
from pandas import DataFrame
from pathlib import Path
from sklearn.feature_selection import RFECV
from sklearn.metrics import precision_recall_curve
from sklearn.model_selection import TimeSeriesSplit
from sklearn.ensemble import RandomForestClassifier
from time import time
from typing import Any, Dict, Tuple

logger = logging.getLogger(__name__)


class LitmusMultiTargetClassifier(IFreqaiModel):
    """
    Model that supports multiple classifiers trained separately with
    different features and model weights.
    """

    def train(
            self, unfiltered_dataframe: DataFrame, pair: str, dk: FreqaiDataKitchen, **kwargs
    ) -> Any:
        """
        Filter the training data and train a model to it. Train makes heavy use of the datakitchen
        for storing, saving, loading, and analyzing the data.
        :param unfiltered_dataframe: Full dataframe for the current training period
        :return:
        :model: Trained model which can be used to inference (self.predict)
        """

        logger.info("-------------------- Starting training " f"{pair} --------------------")

        # filter the features requested by user in the configuration file and elegantly handle NaNs
        features_filtered, labels_filtered = dk.filter_features(
            unfiltered_dataframe,
            dk.training_features_list,
            dk.label_list,
            training_filter=True,
        )

        start_date = unfiltered_dataframe["date"].iloc[0].strftime("%Y-%m-%d")
        end_date = unfiltered_dataframe["date"].iloc[-1].strftime("%Y-%m-%d")
        logger.info(f"-------------------- Training on data from {start_date} to "
                    f"{end_date}--------------------")
        # split data into train/test data.
        data_dictionary = dk.make_train_test_datasets(features_filtered, labels_filtered)
        if not self.freqai_info.get("fit_live_predictions", 0) or not self.live:
            dk.fit_labels()
        # normalize all data based on train_dataset only
        data_dictionary = dk.normalize_data(data_dictionary)

        # optional additional data cleaning/analysis
        self.data_cleaning_train(dk)

        logger.info(
            f'Training model on {len(dk.data_dictionary["train_features"].columns)}' " features"
        )
        logger.info(f'Training model on {len(data_dictionary["train_features"])} data points')

        model = self.fit(data_dictionary, dk)

        logger.info(f"-------------------- Done training {pair} --------------------")

        return model

    def predict(
            self, unfiltered_dataframe: DataFrame, dk: FreqaiDataKitchen, **kwargs
    ) -> Tuple[DataFrame, npt.NDArray[np.int_]]:
        """
        Filter the prediction features data and predict with it.
        :param: unfiltered_dataframe: Full dataframe for the current backtest period.
        :return:
        :pred_df: dataframe containing the predictions
        :do_predict: np.array of 1s and 0s to indicate places where freqai needed to remove
        data (NaNs) or felt uncertain about data (PCA and DI index)
        """

        dk.find_features(unfiltered_dataframe)
        filtered_dataframe, _ = dk.filter_features(
            unfiltered_dataframe, dk.training_features_list, training_filter=False
        )
        filtered_dataframe = dk.normalize_data_from_metadata(filtered_dataframe)
        dk.data_dictionary["prediction_features"] = filtered_dataframe

        self.data_cleaning_predict(dk)

        predictions = self.model.predict(dk.data_dictionary["prediction_features"])
        pred_df = DataFrame(predictions, columns=dk.label_list)

        predictions_prob = self.model.predict_proba(dk.data_dictionary["prediction_features"])
        pred_df_prob = DataFrame(predictions_prob, columns=self.model.classes_)

        pred_df = pd.concat([pred_df, pred_df_prob], axis=1)

        return (pred_df, dk.do_predict)

    def fit(self, data_dictionary: Dict, dk: FreqaiDataKitchen, **kwargs) -> Any:  # noqa: C901
        """
        User sets up the training and test data to fit their desired model here
        :params:
        :data_dictionary: the dictionary constructed by DataHandler to hold
        all the training and test data/labels.
        """

        start_time = time()

        models = []

        for t in data_dictionary["train_labels"].columns:
            logger.info(f"Start training for target {t}")

            # Swap train and test data
            X_train = data_dictionary["test_features"]
            y_train = data_dictionary["test_labels"][t]
            weight_train = data_dictionary["test_weights"]
            X_test = data_dictionary["train_features"]
            y_test = data_dictionary["train_labels"][t]
            weight_test = data_dictionary["train_weights"]

            # Drop low performing features based on feature importance
            if self.freqai_info["feature_selection"]["drop_weak_feature_importance"].get(
                    "enabled", False):
                weak_params = self.freqai_info["feature_selection"]["drop_weak_feature_importance"]

                drop_table = exclude_weak_features(
                    model=t, pair=dk.pair,
                    loss_ratio_threshold=weak_params["loss_ratio_threshold"],
                    chance_excluded=weak_params["chance_excluded"],
                    min_num_trials=weak_params["min_num_trials"])

                cols_to_drop = drop_table["feature_id"].to_numpy()

                num_cols_before = len(X_train.columns)
                X_train = X_train.drop(columns=cols_to_drop, errors="ignore")
                X_test = X_test.drop(columns=cols_to_drop, errors="ignore")
                num_cols_after = len(X_train.columns)

                logger.info(f"Dropping {num_cols_before-num_cols_after} weak features "
                            f"based on prior training")

            # Drop low performing features based on RFECV rank results
            if self.freqai_info["feature_selection"]["drop_weak_rfecv_rank"].get(
                    "enabled", False):

                rfecv_params = self.freqai_info["feature_selection"]["drop_weak_rfecv_rank"]

                drop_table = get_low_ranked_features_rfe(
                    model=t, pair=dk.pair, rank_threshold=rfecv_params["rank_threshold"],
                    loss_ratio_threshold=rfecv_params["loss_ratio_threshold"],
                    chance_excluded=rfecv_params["chance_excluded"],
                    min_num_trials=rfecv_params["min_num_trials"])

                cols_to_drop = drop_table["feature_id"].to_numpy()

                num_cols_before = len(X_train.columns)
                X_train = X_train.drop(columns=cols_to_drop, errors="ignore")
                X_test = X_test.drop(columns=cols_to_drop, errors="ignore")
                num_cols_after = len(X_train.columns)

                logger.info(f"Dropping {num_cols_before - num_cols_after} weak features "
                            f"based on prior RFECV training")

            # Drop High PSI Features
            if self.freqai_info["feature_selection"]["psi_elimination"].get("enabled", False):
                num_feat = len(X_train.columns)
                logger.info(f"Starting PSI Feature Elimination for {num_feat} features")

                # Get config params
                psi_elimination_params = self.freqai_info["feature_selection"]["psi_elimination"]

                psi_elimination = DropHighPSIFeatures(
                    split_col=None, split_frac=psi_elimination_params["split_frac"],
                    split_distinct=False, cut_off=None, switch=False,
                    threshold=psi_elimination_params["threshold"], bins=10,
                    strategy="equal_frequency", min_pct_empty_bins=0.0001, missing_values="ignore",
                    variables=None, confirm_variables=False)

                X_train = psi_elimination.fit_transform(X_train, y_train)
                X_test = psi_elimination.transform(X_test)

                num_remaining = len(X_train.columns)
                num_dropped = len(psi_elimination.features_to_drop_)
                dk.data["extra_returns_per_train"][f"num_dropped_psi_{t}"] = num_dropped
                logger.info(f"Dropping {num_dropped} high psi features. {num_remaining} remaining.")

            # Drop features using Greedy Correlated Selection
            if self.freqai_info["feature_selection"]["greedy_selection"].get("enabled", False):
                num_feat = len(X_train.columns)
                logger.info(f"Starting Greedy Feature Selection for {num_feat} features")

                # Get config params
                greedy_selection_params = self.freqai_info["feature_selection"]["greedy_selection"]

                greedy_selection = DropCorrelatedFeatures(
                    variables=None, method="pearson",
                    threshold=greedy_selection_params["threshold"], missing_values="ignore",
                    confirm_variables=False)

                X_train = greedy_selection.fit_transform(X_train, y_train)
                X_test = greedy_selection.transform(X_test)

                num_remaining = len(X_train.columns)
                num_dropped = len(greedy_selection.features_to_drop_)
                dk.data["extra_returns_per_train"][f"num_dropped_greedy_{t}"] = num_dropped
                logger.info(f"Dropping {num_dropped} correlated features using greedy. "
                            f"{num_remaining} remaining.")

            # Drop rows for meta-model to align with primary entries only
            if self.freqai_info["feature_selection"]["drop_rows_meta_model"].get("enabled", False):
                # Boolean mask for rows to keep (True)
                train_row_keep = np.where(y_train == "drop-row", False, True)
                test_row_keep = np.where(y_test == "drop-row", False, True)

                # Drop rows
                X_train = X_train[train_row_keep]
                y_train = y_train[train_row_keep]
                weight_train = weight_train[train_row_keep]
                X_test = X_test[test_row_keep]
                y_test = y_test[test_row_keep]
                weight_test = weight_test[test_row_keep]

                logger.info(f"Meta-model prep: Dropped {len(train_row_keep)} rows from train data. "
                            f"{len(y_train)} remaining.")
                logger.info(f"Meta-model prep: Dropped {len(test_row_keep)} rows from test data. "
                            f"{len(y_test)} remaining.")

            # Drop features using Smart Correlated Selection
            if self.freqai_info["feature_selection"]["smart_selection"].get("enabled", False):
                num_feat = len(X_train.columns)
                logger.info(f"Starting Smart Feature Selection for {num_feat} features")

                # Get config params
                smart_selection_params = self.freqai_info["feature_selection"]["smart_selection"]

                scs_estimator = RandomForestClassifier(
                    n_estimators=smart_selection_params["n_estimators"], n_jobs=4)

                smart_selection = SmartCorrelatedSelection(
                    variables=None, method="pearson",
                    threshold=smart_selection_params["threshold"], missing_values="ignore",
                    selection_method="model_performance", estimator=scs_estimator,
                    scoring=smart_selection_params["scoring"], cv=smart_selection_params["cv"],
                    confirm_variables=False)

                X_train = smart_selection.fit_transform(X_train, y_train)
                X_test = smart_selection.transform(X_test)

                num_remaining = len(X_train.columns)
                num_dropped = len(smart_selection.features_to_drop_)
                dk.data["extra_returns_per_train"][f"num_dropped_corr_{t}"] = num_dropped
                logger.info(f"Dropping {num_dropped} correlated features. "
                            f"{num_remaining} remaining.")

            # Boruta Feature Selection
            if self.freqai_info["feature_selection"]["boruta_selection"].get("enabled", False):
                logger.info(f"Starting boruta for {len(X_train.columns)} features")
                boruta_params = self.freqai_info["feature_selection"]["boruta_selection"]

                boruta_selector = BorutaShap(
                    importance_measure=boruta_params["importance_measure"],
                    percentile=boruta_params["percentile"],
                    classification=True)
                boruta_selector.fit(
                    X=X_train, y=y_train, n_trials=boruta_params["n_trials"],
                    sample=boruta_params["sample"],
                    train_or_test=boruta_params["train_or_test"],
                    normalize=boruta_params["normalize"], verbose=boruta_params["verbose"])

                # Incase there are undecided features
                if boruta_params.get("drop_tentative", False):
                    boruta_selector.TentativeRoughFix()

                X_train = boruta_selector.Subset()
                X_test = X_test.loc[:, X_train.columns]

                logger.info(f"Boruta selected {len(X_train.columns)} features")

            # RFECV: Recursive Feature Elimination with Cross Validation
            if self.freqai_info["feature_selection"]["rfecv"].get("enabled", False):
                num_feat = len(X_train.columns)
                logger.info(f"Starting RFECV process for {num_feat} features")

                # Get config params
                rfecv_params = self.freqai_info["feature_selection"]["rfecv"]

                min_features_to_select = rfecv_params.get("min_features_to_select", 1)
                cv_n_splits = rfecv_params.get("cv_n_splits", 5)
                cv_gap = rfecv_params.get("cv_gap", 100)
                step = rfecv_params.get("step", 10)
                scoring = rfecv_params.get("scoring", "f1_macro")
                n_jobs = rfecv_params.get("n_jobs", 1)
                verbose = rfecv_params.get("n_jobs", 0)

                rfe_estimator = RandomForestClassifier(
                    n_estimators=rfecv_params["n_estimators"])
                cv = TimeSeriesSplit(n_splits=cv_n_splits, gap=cv_gap)

                rfecv = RFECV(
                    estimator=rfe_estimator,
                    step=step,
                    cv=cv,
                    scoring=scoring,
                    verbose=verbose,
                    min_features_to_select=min_features_to_select,
                    n_jobs=n_jobs,
                )

                rfecv.fit(X_train, y_train)
                original_col_names = X_train.columns
                col_names = rfecv.get_feature_names_out()
                X_train = pd.DataFrame(rfecv.transform(X_train), columns=col_names)
                X_test = pd.DataFrame(rfecv.transform(X_test), columns=col_names)

                num_remaining = len(X_train.columns)
                num_dropped = num_feat - num_remaining
                logger.info(f"Dropping {num_dropped} features using RFECV. "
                            f"{num_remaining} remaining.")

                # Store feature ranks to database
                feat_rank = rfecv.ranking_
                feat_id = original_col_names
                cv_score_mean = rfecv.cv_results_["mean_test_score"].mean()
                cv_score_std = rfecv.cv_results_["std_test_score"].mean()
                feat_rank_df = pd.DataFrame(
                    np.column_stack([feat_id, feat_rank]), columns=["feature_id", "feature_rank"])
                feat_rank_df["train_time"] = time()
                feat_rank_df["pair"] = dk.pair
                feat_rank_df["model"] = t
                feat_rank_df["cv_score_mean"] = cv_score_mean
                feat_rank_df["cv_score_std"] = cv_score_std
                feat_rank_df.set_index(
                    keys=["model", "train_time", "pair", "feature_id"], inplace=True)
                save_df_to_db(df=feat_rank_df, table_name="feature_rfecv_rank")

                # Save metrics for plotting
                dk.data["extra_returns_per_train"][f"cv_score_mean_{t}"] = cv_score_mean
                dk.data["extra_returns_per_train"][f"cv_score_std_{t}"] = cv_score_std

            # Recursive feature addition
            if self.freqai_info["feature_selection"]["recursive_addition"].get(
                    "enabled", False):
                num_feat = len(X_train.columns)
                logger.info(f"Starting RFA Process for {num_feat} features")

                # Get config params
                elimination_params = self.freqai_info["feature_selection"]["recursive_addition"]

                sbs_estimator = RandomForestClassifier(
                    n_estimators=elimination_params["n_estimators"], n_jobs=4)

                feature_elimination = RecursiveFeatureAddition(
                    estimator=sbs_estimator, scoring=elimination_params["scoring"],
                    cv=elimination_params["cv"], threshold=elimination_params["threshold"],
                    variables=None, confirm_variables=False)

                X_train = feature_elimination.fit_transform(X_train, y_train)
                X_test = feature_elimination.transform(X_test)

                num_remaining = len(X_train.columns)
                num_dropped = len(feature_elimination.features_to_drop_)
                dk.data["extra_returns_per_train"][f"num_dropped_model_{t}"] = num_dropped
                logger.info(f"Dropping {num_dropped} features with poor performance. "
                            f"{num_remaining} remaining.")

            # Create Pool objs for catboost
            train_data = Pool(
                data=X_train,
                label=y_train,
                weight=weight_train
            )
            eval_data = Pool(
                data=X_test,
                label=y_test,
                weight=weight_test
            )

            model = CatBoostClassifier(
                **self.model_training_parameters,
                train_dir=Path(dk.data_path),
            )

            # Feature reduction using catboost routine
            if self.freqai_info["feature_selection"]["catboost_selection"].get("enabled", False):
                num_feat = len(X_train.columns)
                logger.info(f"Starting Catboost Feature Selection for {num_feat} features")

                # Get config params for feature selection
                selection_params = self.freqai_info["feature_selection"]["catboost_selection"]

                # Run feature selection
                all_feature_names = np.arange(len(X_train.columns))
                summary = model.select_features(
                    X=train_data, eval_set=eval_data,
                    num_features_to_select=selection_params["num_features_select"],
                    features_for_select=all_feature_names, steps=selection_params["steps"],
                    algorithm=EFeaturesSelectionAlgorithm.RecursiveByLossFunctionChange,
                    shap_calc_type=EShapCalcType.Approximate, train_final_model=False)

                selected_features_names = summary["selected_features_names"]

                train_data = Pool(
                    data=X_train.loc[:, selected_features_names],
                    label=y_train,
                    weight=weight_train
                )
                eval_data = Pool(
                    data=X_test.loc[:, selected_features_names],
                    label=y_test,
                    weight=weight_test
                )

            # Train final model
            init_model = self.get_init_model(dk.pair)
            model.fit(X=train_data, eval_set=eval_data, init_model=init_model)

            dk.data["extra_returns_per_train"][f"num_trees_{t}"] = model.tree_count_

            # Compute feature importance & save to db
            if self.freqai_info["feature_selection"]["save_feature_importance"].get("enabled", 0):
                feature_imp = model.get_feature_importance(
                    data=eval_data, type=EFstrType.LossFunctionChange, prettified=True)
                feature_imp.rename(
                    columns={"Feature Id": "feature_id", "Importances": "importance"}, inplace=True)
                feature_imp["pair"] = dk.pair
                feature_imp["train_time"] = time()
                feature_imp["model"] = t
                feature_imp["rank"] = feature_imp["importance"].rank(
                    method="first", ascending=False)
                feature_imp.set_index(
                    keys=["model", "train_time", "pair", "feature_id"], inplace=True)
                save_df_to_db(df=feature_imp, table_name="feature_importance_history")

            end_time = time()
            total_time = end_time - start_time
            dk.data["extra_returns_per_train"][f"total_time_{t}"] = total_time

            # Model performance metrics
            y_pred_proba = model.predict_proba(X_test)[:, 0]
            logger.info(f"Model performance metrics for {t} and {dk.pair}")

            # Precision and recall stats
            precision, recall, thresholds = precision_recall_curve(
                y_test, y_pred_proba, pos_label=model.classes_[0])

            # Print summary of precision and recall table
            pr_summary = np.column_stack([precision, recall, np.append(thresholds, [1])])
            pr_summary_df = pd.DataFrame(pr_summary, columns=["precision", "recall", "thresholds"])
            bins = np.linspace(start=0, stop=1, num=11)
            pr_agg_df = pr_summary_df.groupby(pd.cut(pr_summary_df["thresholds"], bins=bins)).mean()
            print(pr_agg_df)

            # Get threshold for max F1 score
            fbeta_entry = self.freqai_info["trigger_parameters"].get("fbeta_entry", 1)
            max_fbeta_entry, fbeta_entry_thresh = threshold_from_optimum_f1(
                precision, recall, thresholds, beta=fbeta_entry)

            dk.data["extra_returns_per_train"][f"max_fbeta_entry_{t}"] = max_fbeta_entry
            dk.data["extra_returns_per_train"][f"fbeta_entry_thresh_{t}"] = fbeta_entry_thresh

            logger.info(f"{t} - Max FBeta entry score: {max_fbeta_entry}")
            logger.info(f"{t} - Optimum threshold FBeta entry score: {fbeta_entry_thresh}")

            # Get threshold for desired precision
            desired_precision = self.freqai_info["trigger_parameters"].get("desired_precision", 1)
            resulting_recall, desired_precision_threshold = threshold_from_desired_precision(
                precision, recall, thresholds, desired_precision=desired_precision)

            dk.data["extra_returns_per_train"][
                f"resulting_recall_{t}"] = resulting_recall
            dk.data["extra_returns_per_train"][
                f"desired_precision_threshold_{t}"] = desired_precision_threshold

            logger.info(f"{t} - Resulting recall from desired precision: {resulting_recall}")
            logger.info(f"{t} - Desired precision threshold: {desired_precision_threshold}")

            models.append(model)

        model = MergedModel(models)

        return model
