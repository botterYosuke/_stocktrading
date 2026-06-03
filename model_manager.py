import datetime

import numpy as np
import pandas as pd
from sklearn.preprocessing import StandardScaler

from data_source import (
    load_daily_bars,
    newest_close_as_of,
    parse_date,
    select_pit_bars,
)
from misc import Misc
from signals_writer import write_daily_signals, write_manifest

# TensorFlow/Keras are imported lazily inside compile_model()/fit() so this
# module can be imported (and the light-weight paths tested) without TF (B2-1).


def daily_bars_to_frame(bars):
    """Adapter: list[DailyBar] -> DataFrame for add_technical_indicators (B2-2).

    Produces the same column shape the legacy SQLite path fed in
    (date/open/high/low/close/volume), so the existing feature pipeline
    consumes tier-2 CSV.gz bars (data_source.load_daily_bars) unchanged.
    """
    return pd.DataFrame(
        {
            "date": [b.date for b in bars],
            "open": [b.open for b in bars],
            "high": [b.high for b in bars],
            "low": [b.low for b in bars],
            "close": [b.close for b in bars],
            "volume": [b.volume for b in bars],
        }
    )


class ModelManager:
    def __init__(self, cache_dir=None):
        # Brain is self-contained on tier-2 CSV.gz; no SQLite/DataManager (B2-3).
        self.cache_dir = cache_dir
        self.window = 30
        self.train_window_business_days = 80
        self.codes = []

    def add_technical_indicators(self, df):
        # 日付をインデックスにする
        df.set_index("date", inplace=True)

        # 移動平均線を追加する
        df["MA5"] = df["close"].rolling(window=5).mean()
        df["MA25"] = df["close"].rolling(window=25).mean()

        # MACDを追加する
        df["MACD"] = df["close"].ewm(span=12).mean() - df["close"].ewm(span=26).mean()
        df["SIGNAL"] = df["MACD"].ewm(span=9).mean()
        df["HISTOGRAM"] = df["MACD"] - df["SIGNAL"]

        # ボリンジャーバンドを追加する
        sma20 = df["close"].rolling(window=20).mean()
        std20 = df["close"].rolling(window=20).std()
        df["Upper"] = sma20 + (std20 * 2)
        df["Lower"] = sma20 - (std20 * 2)

        # RSIを追加する
        delta = df["close"].diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
        rs = gain / loss
        df["RSI"] = 100 - (100 / (1 + rs))

        # 終値の前日比を追加する
        df_shift = df.shift(1)
        df["close_rate"] = (df["close"] - df_shift["close"]) / df_shift["close"]

        # 始値と終値の差を追加する
        df["trunk"] = df["open"] - df["close"]

        # 移動平均線乖離率を追加する
        df["MA5_rate"] = (df["close"] - df["MA5"]) / df["MA5"]
        df["MA25_rate"] = (df["close"] - df["MA25"]) / df["MA25"]

        # MACDの乖離率を追加する
        df["MACD_rate"] = (df["MACD"] - df["SIGNAL"]) / df["SIGNAL"]

        # RSIの乖離率を追加する
        df["RSI_rate"] = (df["RSI"] - 50) / 50

        # ボリンジャーバンドの乖離率を追加する
        df["Upper_rate"] = (df["close"] - df["Upper"]) / df["Upper"]

        # 移動平均の差を追加する
        df["MA_diff"] = df["MA5"] - df["MA25"]

        # nan を削除
        df = df.dropna()

        return df

    def compile_model(self, shape1, shape2, rnn_layer):
        from tensorflow.keras import metrics
        from tensorflow.keras.layers import (
            Bidirectional,
            Dense,
            Dropout,
            InputLayer,
        )
        from tensorflow.keras.models import Sequential
        from tensorflow.keras.optimizers import Adam

        model = Sequential()
        model.add(InputLayer(shape=(shape1, shape2)))
        model.add(Bidirectional(rnn_layer))
        model.add(Dropout(0.3))
        model.add(Dense(256, activation="relu"))
        model.add(Dropout(0.3))
        model.add(Dense(1, activation="sigmoid"))

        model.compile(
            optimizer=Adam(learning_rate=0.001),
            loss="binary_crossentropy",
            metrics=["accuracy", metrics.Precision(), metrics.Recall()],
        )

        return model

    def prepare_data(self, as_of):
        """Point-in-time data prep: only bars dated <= as_of are read (B2-3)."""
        scaler = StandardScaler()
        dict_df = {}
        dict_close = {}

        as_of_date = parse_date(as_of)
        start = as_of_date - datetime.timedelta(days=180)
        bars_by_code = load_daily_bars(
            cache_dir=self.cache_dir, start=start, end=as_of_date
        )
        self.bars_by_code = bars_by_code
        pit = select_pit_bars(
            bars_by_code, as_of_date, train_window=self.train_window_business_days
        )

        for code, bars in pit.items():
            df = self.add_technical_indicators(daily_bars_to_frame(bars))
            if len(df) <= self.window:
                continue
            dict_df[code] = pd.DataFrame(scaler.fit_transform(df))
            dict_close[code] = df["close"]

        self.codes = list(dict_df.keys())
        return dict_df, dict_close

    def fit(self, dict_df, dict_close, per, opt_model):
        from tensorflow.keras.callbacks import EarlyStopping
        from tensorflow.keras.layers import LSTM, SimpleRNN

        list_X, list_y = [], []
        window = self.window

        for code in self.codes:
            df = dict_df[code]
            cl = dict_close[code]

            for i in range(len(df) - window):
                list_X.append(df.iloc[i : i + window])

                current_close = cl.iloc[i : i + window].tail(1).item()
                future_close = cl.iloc[i + window : i + window + 1].item()

                if per > 1:
                    flag = future_close >= current_close * per
                elif per <= 1:
                    flag = future_close <= current_close * per
                list_y.append(1 if flag else 0)

        array_X = np.array(list_X)
        array_y = np.array(list_y)

        # モデルの学習
        layer = LSTM(200) if opt_model == "lstm" else SimpleRNN(200)
        model = self.compile_model(array_X.shape[1], array_X.shape[2], layer)
        model.fit(
            array_X,
            array_y,
            batch_size=128,
            epochs=30,
            validation_split=0.2,
            callbacks=[EarlyStopping(patience=3)],
            verbose=0,
        )

        return model

    def predict(self, model, dict_df, as_of):
        list_result = []
        window = self.window

        for code in self.codes:
            array_X = np.array(dict_df[code].tail(window))
            y_pred = model.predict(np.array([array_X]), verbose=0)
            list_result.append([code, y_pred[0][0]])

        df_result = pd.DataFrame(list_result, columns=["code", "pred"])
        df_extract = df_result[df_result["pred"] >= 0.7].copy()

        nbd = Misc.get_next_business_day(parse_date(as_of)).strftime("%Y-%m-%d")
        df_extract.loc[:, "date"] = nbd
        df_extract = df_extract[["date", "code", "pred"]]

        return df_extract


if __name__ == "__main__":
    # # 土日祝日は実行しない
    # if Misc.check_day_type(datetime.date.today()):
    #     exit()

    mm = ModelManager()
    as_of = datetime.date.today()

    # データを準備する（point-in-time: as_of 以前のみ）
    dict_df, dict_close = mm.prepare_data(as_of)

    # ショートモデルを学習する
    model = mm.fit(dict_df, dict_close, per=0.995, opt_model="lstm")
    df_short = mm.predict(model, dict_df, as_of)
    df_short.loc[:, "side"] = 1

    # ロングモデルを学習する
    model = mm.fit(dict_df, dict_close, per=1.005, opt_model="lstm")
    df_long = mm.predict(model, dict_df, as_of)
    df_long.loc[:, "side"] = 2

    # 予測結果を統合する
    df = pd.concat([df_long, df_short])
    df = df.sort_values("pred", ascending=False).drop_duplicates(
        subset=["code"], keep="first"
    )

    selected_indices = []

    # 不適切な銘柄は除外する
    for index, row in df.iterrows():
        close_price = newest_close_as_of(mm.bars_by_code, row["code"], as_of)
        if 700 < close_price < 6000:
            selected_indices.append(index)
    df = df.loc[selected_indices, :]

    # 予測値に応じて確率的に銘柄を50個サンプリング
    weights = df["pred"].to_numpy()
    probabilities = weights / np.sum(weights)
    sampled_indices = np.random.choice(
        a=df.index,
        size=50,
        replace=False,
        p=probabilities,
    )
    df = df.loc[sampled_indices, ["date", "code", "pred", "side"]]
    df = df.sort_values("pred", ascending=False).reset_index()
    df = df[["date", "code", "pred", "side"]]

    # signals JSON として出力する（SQLite Target の置き換え, B2-4）
    rows = [
        {"code": row["code"], "pred": row["pred"], "side": row["side"]}
        for _, row in df.iterrows()
    ]
    target_date = df["date"].iloc[0]

    signal_path = write_daily_signals(
        output_dir="signals",
        target_date=target_date,
        as_of=as_of,
        rows=rows,
    )
    write_manifest(
        output_dir="signals",
        start=target_date,
        end=target_date,
        signal_files=[signal_path],
    )
