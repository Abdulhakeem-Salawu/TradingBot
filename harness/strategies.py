"""Rule-based strategies and the benchmark positions they are judged against.

Every function maps prices known at the close of bar t to the position held
from that close onward. None of them may read a price after t; the lookahead
check in validate_harness.py enforces this by perturbing the future and
confirming nothing up to t changes.

Spans are in CALENDAR DAYS and converted to bars with the instrument's
periods_per_year, so "30 days" means the same stretch of time for 24/7 crypto,
24/5 FX and 23/5 gold, at daily or hourly resolution.

PARAMETERS ARE PRE-REGISTERED. The sets below were fixed before any real data
was scored. Changing one after seeing results is a new trial, and the trial
counter hashes this file so it notices even if you do not.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from .costs import align_asof

# One parameter set per timeframe, chosen in advance.
#   1d: the Hurst/Ooi/Pedersen 1-, 3- and 12-month trend blend.
#   1h: the same construction compressed to 1 day, 1 week and 1 month. There
#       is far less published evidence for trend at these horizons, and
#       costs are a much larger share of each bar's move; treat it as a
#       hypothesis, not as the daily rule run faster.
TSMOM_PARAMS = {
    "1d": {"lookback_days": (30, 91, 365), "vol_days": 91},
    "1h": {"lookback_days": (1, 7, 30), "vol_days": 7},
}
VOL_DAYS = {"1d": 91, "1h": 7}   # vol window for the vol-targeted benchmarks


def bars(days: float, periods_per_year: int) -> int:
    """Convert a calendar span to a bar count for this instrument and timeframe."""
    return max(1, int(round(days * periods_per_year / 365)))


def warmup_bars(periods_per_year: int, days: float) -> int:
    """Bars before a lookback of `days` is fully formed."""
    return bars(days, periods_per_year) + 1


def realised_vol(close: pd.Series, window: int, periods_per_year: int) -> pd.Series:
    """Annualised close-to-close volatility, backward-looking."""
    return close.pct_change().rolling(window).std() * np.sqrt(periods_per_year)


def apply_band(raw: pd.Series, band: float) -> pd.Series:
    """Hold the current position until the target moves far enough to pay for a trade.

    This is the cost-aware execution filter in position space: small drifts in
    the volatility scaling are ignored, while entries, exits and sign flips
    always go through. NaN targets (warm-up) mean flat.
    """
    out = np.zeros(len(raw))
    cur = 0.0
    for i, x in enumerate(raw.to_numpy(dtype=float)):
        if not np.isfinite(x):
            x = 0.0
        if np.sign(x) != np.sign(cur) or abs(x - cur) >= band:
            cur = x
        out[i] = cur
    return pd.Series(out, index=raw.index)


# ------------------------------------------------------------------ strategies
def tsmom_vol_managed(close: pd.Series, periods_per_year: int,
                      lookback_days=(30, 91, 365), vol_days: float = 91,
                      target_vol: float = 0.10, max_position: float = 1.0,
                      long_only: bool = False, band: float = 0.25) -> pd.Series:
    """S1: volatility-managed time-series momentum.

    Direction is the average SIGN of the returns over each lookback (the
    Hurst/Ooi/Pedersen construction), so exposure steps through -1, -1/3,
    +1/3, +1 as the horizons agree or disagree. Size is scaled so the position
    targets `target_vol` annualised, capped at `max_position` (no leverage).
    """
    signs = [np.sign(close.pct_change(bars(d, periods_per_year))) for d in lookback_days]
    direction = sum(signs) / len(signs)
    vol = realised_vol(close, bars(vol_days, periods_per_year), periods_per_year)
    raw = (direction * target_vol / vol).clip(-max_position, max_position)
    if long_only:
        raw = raw.clip(lower=0.0)
    return apply_band(raw, band)


def fx_carry(close: pd.Series, base_rate: pd.Series, quote_rate: pd.Series,
             periods_per_year: int, vol_days: float = 91, target_vol: float = 0.10,
             max_position: float = 1.0, band: float = 0.25,
             publication_lag_months: int = 3) -> pd.Series:
    """S2: time-series carry. Long the higher-yielding currency, vol-scaled.

    Rates are monthly averages published about two months later, so a value
    dated month M only becomes usable `publication_lag_months` later. Using it
    from the 1st of month M would leak the rest of that month's rates.
    Rebalances on the first bar of each month only.
    """
    def available(rate: pd.Series) -> pd.Series:
        s = rate.dropna().copy()
        s.index = s.index + pd.DateOffset(months=publication_lag_months)
        return align_asof(s, close.index)

    direction = np.sign(available(base_rate) - available(quote_rate))
    vol = realised_vol(close, bars(vol_days, periods_per_year), periods_per_year)
    raw = (direction * target_vol / vol).clip(-max_position, max_position)

    naive = close.index.tz_convert(None) if close.index.tz is not None else close.index
    month = naive.to_period("M")
    first_bar = np.r_[True, month[1:] != month[:-1]]
    raw = raw.where(first_bar).ffill()
    return apply_band(raw, band)


# ------------------------------------------------------------------ benchmarks
def buy_and_hold(close: pd.Series) -> pd.Series:
    return pd.Series(1.0, index=close.index)


def vol_target_hold(close: pd.Series, periods_per_year: int, direction: float = 1.0,
                    vol_days: float = 91, target_vol: float = 0.10,
                    max_position: float = 1.0, band: float = 0.25) -> pd.Series:
    """Always in the market in one direction, sized to the same vol target.

    This is the benchmark that matters for a long-only trend rule: it takes
    the same risk budget without any timing, so beating it means the timing
    added something rather than the asset simply going up.
    """
    vol = realised_vol(close, bars(vol_days, periods_per_year), periods_per_year)
    raw = (direction * target_vol / vol).clip(-max_position, max_position)
    return apply_band(raw, band)
