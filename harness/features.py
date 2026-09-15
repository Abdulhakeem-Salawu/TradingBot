"""Feature engineering.

DELIBERATELY SMALL. Eight features, not twenty-four.

Every feature you add is another dimension the model can overfit in, and it
inflates your effective trial count even if you never tune it directly. Start
here. Add a feature only when you can state in one sentence why it should carry
information the existing set does not, and re-run the full harness afterwards.

All features are computed using ONLY data available at or before the bar they
are attached to. Any feature that peeks forward invalidates everything
downstream, so each one below is built from backward-looking windows only.
"""

import numpy as np
import pandas as pd

FEATURE_NAMES = [
    "ret_1",
    "ret_6",
    "ret_24",
    "vol_24",
    "rsi_14",
    "ma_ratio",
    "range_pct",
    "vol_regime",
]


def _rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    return (100.0 - 100.0 / (1.0 + rs)).fillna(50.0)


def build_features(df: pd.DataFrame) -> pd.DataFrame:
    """Build the feature matrix from an OHLCV frame.

    Expects columns: open, high, low, close, volume. Index must be a sorted
    DatetimeIndex with no duplicates.
    """
    required = {"open", "high", "low", "close", "volume"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"OHLCV frame missing columns: {sorted(missing)}")
    if not df.index.is_monotonic_increasing:
        raise ValueError("Index must be sorted ascending. Unsorted data silently leaks.")
    if df.index.has_duplicates:
        raise ValueError("Index has duplicate timestamps. Deduplicate before proceeding.")

    close = df["close"].astype(float)
    out = pd.DataFrame(index=df.index)

    # Momentum over three horizons
    out["ret_1"] = close.pct_change(1)
    out["ret_6"] = close.pct_change(6)
    out["ret_24"] = close.pct_change(24)

    # Realised volatility, backward-looking
    out["vol_24"] = close.pct_change().rolling(24).std()

    # Mean-reversion / overbought signal
    out["rsi_14"] = _rsi(close, 14) / 100.0

    # Trend: price relative to its own moving average
    ma = close.rolling(48).mean()
    out["ma_ratio"] = (close / ma) - 1.0

    # Intrabar range as a share of close -- a cheap liquidity/stress proxy
    out["range_pct"] = (df["high"].astype(float) - df["low"].astype(float)) / close

    # Volatility regime: current vol vs its own longer-run level
    long_vol = close.pct_change().rolling(168).std()
    out["vol_regime"] = out["vol_24"] / long_vol

    out = out[FEATURE_NAMES]
    return out.replace([np.inf, -np.inf], np.nan)
