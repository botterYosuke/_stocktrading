"""Intraday technical features (jpx_mlbot_15m, #8 Phase 0b / C5).

Faithful port of the crypto tutorial's ``calc_features`` (``example/tutorial.ipynb``)
to JP-equity 15-minute bars. Same TA-Lib indicator families, same three
normalization regimes — the tutorial's whole point is breadth + leak-free
scaling, not the specific indicators:

  (A) price-level indicators  -> ``(indicator - hilo) / close``   (hilo=(hi+lo)/2)
      LINEARREG / LINEARREG_INTERCEPT use ``(indicator - close) / close``.
  (B) flow / dispersion        -> ``indicator / close``
  (C) bounded oscillators      -> raw (already scale-free: RSI, STOCH, ADX, ...)

Input is a SINGLE instrument's bar frame with columns
``open, high, low, close, volume`` (the project's ``daily_bars_to_frame`` shape;
``panel_builder`` (C6) feeds one ``MinuteBar`` series at a time and stacks the
results cross-sectionally). Output is the same frame with feature columns added;
warmup rows are left as NaN for the caller to drop (matches the tutorial, which
``dropna()`` before training). All indicators are causal (left-to-right), so a
feature at time *t* never depends on bars after *t* — verified by a causality
test in ``tests/test_features_intraday.py``.

TA-Lib (not the ``ta`` package) is used to keep the port verbatim; it installs
as a self-contained wheel (no system C lib needed).
"""
from __future__ import annotations

import pandas as pd
import talib

# The curated feature set the tutorial actually trains on (its `features` list),
# kept verbatim so downstream LightGBM (C8) consumes an identical column set.
FEATURES: list[str] = sorted(
    [
        "ADX", "ADXR", "APO", "AROON_aroondown", "AROON_aroonup", "AROONOSC",
        "CCI", "DX", "MACD_macd", "MACD_macdsignal", "MACD_macdhist", "MFI",
        "MOM", "RSI", "STOCH_slowk", "STOCH_slowd", "STOCHF_fastk", "ULTOSC",
        "WILLR", "HT_DCPERIOD", "HT_DCPHASE", "HT_PHASOR_inphase",
        "HT_PHASOR_quadrature", "HT_TRENDMODE", "BETA", "LINEARREG",
        "LINEARREG_ANGLE", "LINEARREG_INTERCEPT", "LINEARREG_SLOPE", "STDDEV",
        "BBANDS_upperband", "BBANDS_middleband", "BBANDS_lowerband", "DEMA",
        "EMA", "HT_TRENDLINE", "KAMA", "MA", "MIDPOINT", "T3", "TEMA", "TRIMA",
        "WMA",
    ]
)


def calc_features(df: pd.DataFrame) -> pd.DataFrame:
    """Add TA-Lib technical features to a single instrument's OHLCV frame.

    ``df`` columns: ``open, high, low, close, volume`` (float-like). The frame
    is modified in place and also returned (tutorial parity). Warmup rows are
    NaN; the caller drops them.
    """
    open = df["open"].astype("float64")
    high = df["high"].astype("float64")
    low = df["low"].astype("float64")
    close = df["close"].astype("float64")
    volume = df["volume"].astype("float64")

    hilo = (high + low) / 2

    # (A) price-level: subtract hilo (or close) then divide by close ----------
    df["BBANDS_upperband"], df["BBANDS_middleband"], df["BBANDS_lowerband"] = talib.BBANDS(
        close, timeperiod=5, nbdevup=2, nbdevdn=2, matype=0
    )
    df["BBANDS_upperband"] = (df["BBANDS_upperband"] - hilo) / close
    df["BBANDS_middleband"] = (df["BBANDS_middleband"] - hilo) / close
    df["BBANDS_lowerband"] = (df["BBANDS_lowerband"] - hilo) / close
    df["DEMA"] = (talib.DEMA(close, timeperiod=30) - hilo) / close
    df["EMA"] = (talib.EMA(close, timeperiod=30) - hilo) / close
    df["HT_TRENDLINE"] = (talib.HT_TRENDLINE(close) - hilo) / close
    df["KAMA"] = (talib.KAMA(close, timeperiod=30) - hilo) / close
    df["MA"] = (talib.MA(close, timeperiod=30, matype=0) - hilo) / close
    df["MIDPOINT"] = (talib.MIDPOINT(close, timeperiod=14) - hilo) / close
    df["T3"] = (talib.T3(close, timeperiod=5, vfactor=0) - hilo) / close
    df["TEMA"] = (talib.TEMA(close, timeperiod=30) - hilo) / close
    df["TRIMA"] = (talib.TRIMA(close, timeperiod=30) - hilo) / close
    df["WMA"] = (talib.WMA(close, timeperiod=30) - hilo) / close
    df["LINEARREG"] = (talib.LINEARREG(close, timeperiod=14) - close) / close
    df["LINEARREG_INTERCEPT"] = (talib.LINEARREG_INTERCEPT(close, timeperiod=14) - close) / close

    # (B) flow / dispersion: divide by close ---------------------------------
    df["APO"] = talib.APO(close, fastperiod=12, slowperiod=26, matype=0) / close
    df["HT_PHASOR_inphase"], df["HT_PHASOR_quadrature"] = talib.HT_PHASOR(close)
    df["HT_PHASOR_inphase"] /= close
    df["HT_PHASOR_quadrature"] /= close
    df["LINEARREG_SLOPE"] = talib.LINEARREG_SLOPE(close, timeperiod=14) / close
    df["MACD_macd"], df["MACD_macdsignal"], df["MACD_macdhist"] = talib.MACD(
        close, fastperiod=12, slowperiod=26, signalperiod=9
    )
    df["MACD_macd"] /= close
    df["MACD_macdsignal"] /= close
    df["MACD_macdhist"] /= close
    df["MOM"] = talib.MOM(close, timeperiod=10) / close
    df["STDDEV"] = talib.STDDEV(close, timeperiod=5, nbdev=1) / close

    # (C) bounded oscillators: raw -------------------------------------------
    df["ADX"] = talib.ADX(high, low, close, timeperiod=14)
    df["ADXR"] = talib.ADXR(high, low, close, timeperiod=14)
    df["AROON_aroondown"], df["AROON_aroonup"] = talib.AROON(high, low, timeperiod=14)
    df["AROONOSC"] = talib.AROONOSC(high, low, timeperiod=14)
    df["CCI"] = talib.CCI(high, low, close, timeperiod=14)
    df["DX"] = talib.DX(high, low, close, timeperiod=14)
    df["MFI"] = talib.MFI(high, low, close, volume, timeperiod=14)
    df["RSI"] = talib.RSI(close, timeperiod=14)
    df["STOCH_slowk"], df["STOCH_slowd"] = talib.STOCH(
        high, low, close, fastk_period=5, slowk_period=3, slowk_matype=0,
        slowd_period=3, slowd_matype=0,
    )
    df["STOCHF_fastk"], _ = talib.STOCHF(
        high, low, close, fastk_period=5, fastd_period=3, fastd_matype=0
    )
    df["ULTOSC"] = talib.ULTOSC(high, low, close, timeperiod1=7, timeperiod2=14, timeperiod3=28)
    df["WILLR"] = talib.WILLR(high, low, close, timeperiod=14)

    df["HT_DCPERIOD"] = talib.HT_DCPERIOD(close)
    df["HT_DCPHASE"] = talib.HT_DCPHASE(close)
    df["HT_TRENDMODE"] = talib.HT_TRENDMODE(close)
    df["BETA"] = talib.BETA(high, low, timeperiod=5)
    df["LINEARREG_ANGLE"] = talib.LINEARREG_ANGLE(close, timeperiod=14)
    return df
