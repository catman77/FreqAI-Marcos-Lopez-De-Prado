
# from freqtrade.persistence import Trade
from freqtrade.strategy import DecimalParameter, IntParameter, IStrategy, merge_informative_pair
from functools import reduce
from pandas import DataFrame
from scipy.signal import argrelextrema
from technical import qtpylib

import logging
import numpy as np
import pandas as pd
import talib.abstract as ta

logger = logging.getLogger(__name__)


class LitmusMinMaxClassificationStrategy(IStrategy):
    """
    Example strategy showing how the user connects their own
    IFreqaiModel to the strategy. Namely, the user uses:
    self.freqai.start(dataframe, metadata)
    to make predictions on their data. populate_any_indicators() automatically
    generates the variety of features indicated by the user in the
    canonical freqtrade configuration file under config['freqai'].
    """

    minimal_roi = {"0": 0.1, "600": -1}

    plot_config = {
        "main_plot": {},
        "subplots": {
            "do_predict": {
                "do_predict": {"color": "brown"},
                "DI_values": {"color": "grey"},
            },
            "Maxima": {
                "is_maxima": {"color": "CornflowerBlue"},
                "long_exit_target": {"color": "FireBrick"},
                "short_entry_target": {"color": "DarkOliveGreen"},
                "missed_maxima": {"color": "LightPink"}
            },
            "Minima": {
                "is_minima": {"color": "CornflowerBlue"},
                "long_entry_target": {"color": "DarkOliveGreen"},
                "short_exit_target": {"color": "FireBrick"},
                "missed_minima": {"color": "LightPink"}
            },
            "Not M/M": {
                "not_minmax": {"color": "Moccasin"}
            },
            "Real": {
                "real-minima": {"color": "blue"},
                "real-maxima": {"color": "yellow"}
            },
            "ROC": {
                "roc_auc_is_maxima": {"color": "#B2D732"},
                "roc_auc_is_minima": {"color": "#FC600A"},
                "roc_auc_missed_maxima": {"color": "#F0F7D4"},
                "roc_auc_missed_minima": {"color": "#F7E0D4"}
            },
            "Prec": {
                "avg_precision_is_maxima": {"color": "#B2D732"},
                "avg_precision_is_minima": {"color": "#FC600A"},
                "avg_precision_missed_maxima": {"color": "#F0F7D4"},
                "avg_precision_missed_minima": {"color": "#F7E0D4"}
            },
            "Time": {
                "time_to_train": {"color": "Salmon"}
            },
        },
    }

    process_only_new_candles = True
    stoploss = -0.05
    use_exit_signal = True
    startup_candle_count: int = 300
    can_short = True

    linear_roi_offset = DecimalParameter(
        0.00, 0.02, default=0.005, space="sell", optimize=False, load=True
    )
    max_roi_time_long = IntParameter(0, 800, default=400, space="sell", optimize=False, load=True)

    def informative_pairs(self):
        whitelist_pairs = self.dp.current_whitelist()
        corr_pairs = self.config["freqai"]["feature_parameters"]["include_corr_pairlist"]
        informative_pairs = []
        for tf in self.config["freqai"]["feature_parameters"]["include_timeframes"]:
            for pair in whitelist_pairs:
                informative_pairs.append((pair, tf))
            for pair in corr_pairs:
                if pair in whitelist_pairs:
                    continue  # avoid duplication
                informative_pairs.append((pair, tf))
        return informative_pairs

    def populate_any_indicators(
        self, pair, df, tf, informative=None, set_generalized_indicators=False
    ):
        """
        Function designed to automatically generate, name and merge features
        from user indicated timeframes in the configuration file. User controls the indicators
        passed to the training/prediction by prepending indicators with `'%-' + coin `
        (see convention below). I.e. user should not prepend any supporting metrics
        (e.g. bb_lowerband below) with % unless they explicitly want to pass that metric to the
        model.
        :param pair: pair to be used as informative
        :param df: strategy dataframe which will receive merges from informatives
        :param tf: timeframe of the dataframe which will modify the feature names
        :param informative: the dataframe associated with the informative pair
        """

        coin = pair.split('/')[0]

        with self.freqai.lock:
            if informative is None:
                informative = self.dp.get_pair_dataframe(pair, tf)

            # first loop is automatically duplicating indicators for time periods
            for t in self.freqai_info["feature_parameters"]["indicator_periods_candles"]:

                t = int(t)
                informative[f"%-{coin}rsi-period_{t}"] = ta.RSI(informative, timeperiod=t)
                informative[f"%-{coin}mfi-period_{t}"] = ta.MFI(informative, timeperiod=t)
                informative[f"%-{coin}adx-period_{t}"] = ta.ADX(informative, window=t)
                informative[f"{coin}sma-period_{t}"] = ta.SMA(informative, timeperiod=t)
                informative[f"{coin}ema-period_{t}"] = ta.EMA(informative, timeperiod=t)
                informative[f"%-{coin}close_over_sma-period_{t}"] = (
                    informative["close"] / informative[f"{coin}sma-period_{t}"]
                )

                informative[f"%-{coin}mfi-period_{t}"] = ta.MFI(informative, timeperiod=t)

                bollinger = qtpylib.bollinger_bands(
                    qtpylib.typical_price(informative), window=t, stds=2.2
                )
                informative[f"{coin}bb_lowerband-period_{t}"] = bollinger["lower"]
                informative[f"{coin}bb_middleband-period_{t}"] = bollinger["mid"]
                informative[f"{coin}bb_upperband-period_{t}"] = bollinger["upper"]

                informative[f"%-{coin}bb_width-period_{t}"] = (
                    informative[f"{coin}bb_upperband-period_{t}"]
                    - informative[f"{coin}bb_lowerband-period_{t}"]
                ) / informative[f"{coin}bb_middleband-period_{t}"]
                informative[f"%-{coin}close-bb_lower-period_{t}"] = (
                    informative["close"] / informative[f"{coin}bb_lowerband-period_{t}"]
                )

                informative[f"%-{coin}roc-period_{t}"] = ta.ROC(informative, timeperiod=t)

                informative[f"%-{coin}relative_volume-period_{t}"] = (
                    informative["volume"] / informative["volume"].rolling(t).mean()
                )

            informative[f"%-{coin}pct-change"] = informative["close"].pct_change()
            informative[f"%-{coin}raw_volume"] = informative["volume"]
            informative[f"%-{coin}raw_price"] = informative["close"]

            indicators = [col for col in informative if col.startswith("%")]
            # This loop duplicates and shifts all indicators to add a sense of recency to data
            for n in range(self.freqai_info["feature_parameters"]["include_shifted_candles"] + 1):
                if n == 0:
                    continue
                informative_shift = informative[indicators].shift(n)
                informative_shift = informative_shift.add_suffix("_shift-" + str(n))
                informative = pd.concat((informative, informative_shift), axis=1)

            df = merge_informative_pair(df, informative, self.config["timeframe"], tf, ffill=True)
            skip_columns = [
                (s + "_" + tf) for s in ["date", "open", "high", "low", "close", "volume"]
            ]
            df = df.drop(columns=skip_columns)

            # Add generalized indicators here (because in live, it will call this
            # function to populate indicators during training). Notice how we ensure not to
            # add them multiple times
            if set_generalized_indicators:
                df["%-day_of_week"] = df["date"].dt.dayofweek
                df["%-hour_of_day"] = df["date"].dt.hour

                # Define Min & Max binary indicators
                min_peaks, _ = argrelextrema(df["close"].values, np.less, order=20)
                max_peaks, _ = argrelextrema(df["close"].values, np.greater, order=20)

                df["&target"] = 'not_minmax'
                df["real-minima"] = 0
                df["real-maxima"] = 0

                # Minima
                for mp in min_peaks:
                    df.at[mp, "real-minima"] = 1
                    df.at[mp, "&target"] = 'is_minima'
                    df.at[mp + 1, "&target"] = 'missed_minima'
                    df.at[mp + 2, "&target"] = 'missed_minima'

                # Maxima
                for mp in max_peaks:
                    df.at[mp, "real-maxima"] = 1
                    df.at[mp, "&target"] = 'is_maxima'
                    df.at[mp + 1, "&target"] = 'missed_maxima'
                    df.at[mp + 2, "&target"] = 'missed_maxima'

        return df

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:

        self.freqai_info = self.config["freqai"]

        # All indicators must be populated by populate_any_indicators() for live functionality
        # to work correctly.

        # the model will return all labels created by user in `populate_any_indicators`
        # (& appended targets), an indication of whether or not the prediction should be accepted,
        # the target mean/std values for each of the labels created by user in
        # `populate_any_indicators()` for each training period.

        dataframe = self.freqai.start(dataframe, metadata, self)

        # Standard deviation entry / exists

        # Short
        dataframe["short_entry_target"] = 0.2
        dataframe["short_exit_target"] = 0.15

        # Long
        dataframe["long_entry_target"] = 0.2
        dataframe["long_exit_target"] = 0.15

        return dataframe

    def populate_entry_trend(self, df: DataFrame, metadata: dict) -> DataFrame:

        enter_long_conditions = [df["do_predict"] == 1, df["is_minima"] > df["long_entry_target"]]

        if enter_long_conditions:
            df.loc[
                reduce(lambda x, y: x & y, enter_long_conditions), ["enter_long", "enter_tag"]
            ] = (1, "long")

        enter_short_conditions = [df["do_predict"] == 1, df["is_maxima"] > df["short_entry_target"]]

        if enter_short_conditions:
            df.loc[
                reduce(lambda x, y: x & y, enter_short_conditions), ["enter_short", "enter_tag"]
            ] = (1, "short")

        return df

    def populate_exit_trend(self, df: DataFrame, metadata: dict) -> DataFrame:

        exit_long_conditions = [1 == 1, df["is_maxima"] > df["long_exit_target"]]

        if exit_long_conditions:
            df.loc[reduce(lambda x, y: x & y, exit_long_conditions), "exit_long"] = 1

        exit_short_conditions = [1 == 1, df["is_minima"] > df["short_exit_target"]]

        if exit_short_conditions:
            df.loc[reduce(lambda x, y: x & y, exit_short_conditions), "exit_short"] = 1

        return df

    def get_ticker_indicator(self):
        return int(self.config["timeframe"][:-1])

    """def custom_exit(
        self, pair: str, trade: Trade, current_time, current_rate, current_profit, **kwargs
    ):
        dataframe, _ = self.dp.get_analyzed_dataframe(pair=pair, timeframe=self.timeframe)
        trade_date = timeframe_to_prev_date(self.config["timeframe"], trade.open_date_utc)
        trade_candle = dataframe.loc[(dataframe["date"] == trade_date)]
        if trade_candle.empty:
            return None
        trade_candle = trade_candle.squeeze()
        follow_mode = self.config.get("freqai", {}).get("follow_mode", False)
        if not follow_mode:
            pair_dict = self.freqai.dd.pair_dict
        else:
            pair_dict = self.freqai.dd.follower_dict
        entry_tag = trade.enter_tag
        if (
            "prediction" + entry_tag not in pair_dict[pair]
            or pair_dict[pair]["prediction" + entry_tag] > 0
        ):
            with self.freqai.lock:
                pair_dict[pair]["prediction" + entry_tag] = abs(trade_candle["&-s_close"])
                if not follow_mode:
                    self.freqai.dd.save_drawer_to_disk()
                else:
                    self.freqai.dd.save_follower_dict_to_disk()
        roi_price = pair_dict[pair]["prediction" + entry_tag]
        roi_time = self.max_roi_time_long.value
        roi_decay = roi_price * (
            1 - ((current_time - trade.open_date_utc).seconds) / (roi_time * 60)
        )
        if roi_decay < 0:
            roi_decay = self.linear_roi_offset.value
        else:
            roi_decay += self.linear_roi_offset.value
        if current_profit > roi_decay:
            return "roi_custom_win"
        if current_profit < -roi_decay:
            return "roi_custom_loss"
            """

    """def confirm_trade_exit(
        self,
        pair: str,
        trade: Trade,
        order_type: str,
        amount: float,
        rate: float,
        time_in_force: str,
        exit_reason: str,
        current_time,
        **kwargs,
    ) -> bool:

        entry_tag = trade.enter_tag
        follow_mode = self.config.get("freqai", {}).get("follow_mode", False)
        if not follow_mode:
            pair_dict = self.freqai.dd.pair_dict
        else:
            pair_dict = self.freqai.dd.follower_dict

        with self.freqai.lock:
            pair_dict[pair]["prediction" + entry_tag] = 0
            if not follow_mode:
                self.freqai.dd.save_drawer_to_disk()
            else:
                self.freqai.dd.save_follower_dict_to_disk()

        return True"""

    def confirm_trade_entry(
        self,
        pair: str,
        order_type: str,
        amount: float,
        rate: float,
        time_in_force: str,
        current_time,
        entry_tag,
        side: str,
        **kwargs,
    ) -> bool:

        df, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
        last_candle = df.iloc[-1].squeeze()

        if side == "long":
            if rate > (last_candle["close"] * (1 + 0.0025)):
                return False
        else:
            if rate < (last_candle["close"] * (1 - 0.0025)):
                return False

        return True
