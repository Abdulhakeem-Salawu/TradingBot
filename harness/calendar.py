"""Trading-day calendar shared by data loading, paper trading and comparison."""

from __future__ import annotations

from datetime import timedelta

import pandas as pd

STEP = {"1d": timedelta(days=1), "1h": timedelta(hours=1)}


def session_dates(bar_starts: pd.DatetimeIndex, timeframe: str, asset_class: str) -> pd.Index:
    """Trading date each bar belongs to, so hourly and daily bars line up.

    A bar belongs to the day in which it CLOSES. FX and gold days end at 17:00
    New York (the conventional FX day boundary); crypto days end at 00:00 UTC.
    """
    end = bar_starts + STEP[timeframe] - pd.Timedelta(microseconds=1)
    if asset_class in ("fx", "metal"):
        end = end.tz_convert("America/New_York") + pd.Timedelta(hours=7)
    else:
        end = end.tz_convert("UTC")
    return pd.Index(end.date, name="date")


def session_start(date, asset_class: str) -> pd.Timestamp:
    """UTC start of the trading day `date` (the previous day's 17:00 New York for FX/gold)."""
    if asset_class in ("fx", "metal"):
        local = pd.Timestamp(date) - pd.Timedelta(hours=7)
        return local.tz_localize("America/New_York").tz_convert("UTC")
    return pd.Timestamp(date).tz_localize("UTC")
