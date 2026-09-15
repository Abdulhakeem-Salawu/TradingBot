"""Instrument -> positions, shared by research and the live paper trader.

Keeping this in one place is the guarantee that paper trading runs the rule
that was backtested. If the live runner computed its own signals, the two
would drift apart one "small fix" at a time, and the paper track record would
stop saying anything about the backtest.
"""

from __future__ import annotations

import pandas as pd

from .costs import financing_from_rates
from .instruments import SHORT_RATE_SERIES
from .strategies import TSMOM_PARAMS, VOL_DAYS, fx_carry, tsmom_vol_managed, warmup_bars

STRATEGIES = ("tsmom", "carry")
CARRY_LAG_MONTHS = 3


def strategy_params(strategy: str, timeframe: str) -> dict:
    """The pre-registered parameters, for recording in the trial counter."""
    if strategy == "tsmom":
        return dict(TSMOM_PARAMS[timeframe])
    return {"publication_lag_months": CARRY_LAG_MONTHS, "vol_days": VOL_DAYS[timeframe]}


def check_supported(strategy: str, asset_class: str, timeframe: str) -> None:
    if strategy not in STRATEGIES:
        raise ValueError(f"Unknown strategy {strategy!r}. Choose from {STRATEGIES}.")
    if strategy == "carry" and (asset_class != "fx" or timeframe != "1d"):
        raise ValueError("carry is an FX strategy rebalanced monthly: "
                         "use it with FX instruments and --timeframe 1d")


def target_positions(inst, close: pd.Series, strategy: str, timeframe: str,
                     rates: dict) -> tuple[pd.Series, int]:
    """Positions for one instrument plus the number of warm-up bars to discard."""
    check_supported(strategy, inst.asset_class, timeframe)
    ppy = inst.periods_per_year(timeframe)
    if strategy == "tsmom":
        p = TSMOM_PARAMS[timeframe]
        pos = tsmom_vol_managed(close, ppy, long_only=inst.long_only, **p)
        return pos, warmup_bars(ppy, max(p["lookback_days"]))

    base, quote = rates[inst.base], rates[inst.quote]
    pos = fx_carry(close, base, quote, ppy, vol_days=VOL_DAYS[timeframe],
                   publication_lag_months=CARRY_LAG_MONTHS)
    first_rate = max(base.index[0], quote.index[0]) + pd.DateOffset(months=CARRY_LAG_MONTHS)
    if close.index.tz is not None:
        first_rate = first_rate.tz_localize(close.index.tz)
    start = max(warmup_bars(ppy, VOL_DAYS[timeframe]),
                int(close.index.searchsorted(first_rate)))
    return pos, start


def financing_for(inst, index: pd.DatetimeIndex, rates: dict) -> pd.DataFrame | None:
    """Annual financing rates for holding this instrument, or None if it has none.

    A currency with a rate series that failed to load raises instead of
    silently financing at zero -- a quiet zero would make every FX sleeve look
    cheaper to hold than it is.
    """
    if not inst.uses_rates:
        return None
    missing = [c for c in (inst.base, inst.quote) if c in SHORT_RATE_SERIES and c not in rates]
    if missing:
        raise RuntimeError(f"{inst.symbol}: short rates for {', '.join(missing)} are not loaded")
    return financing_from_rates(index, rates.get(inst.base), rates.get(inst.quote),
                                inst.financing_markup)
