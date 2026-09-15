"""Market-calendar helpers shared by the signal job, executor and comparison report."""

from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd

from harness.calendar import STEP, session_dates  # noqa: F401  (re-exported)


def is_stale(inst, timeframe: str, last_bar_start: pd.Timestamp, now: datetime) -> bool:
    """True when the newest closed bar is older than the market calendar explains.

    FX and gold close from Friday ~21:00 to Sunday ~21:00 UTC, so a Sunday
    without new bars is normal; a Tuesday without them is not.
    """
    age = now - (pd.Timestamp(last_bar_start).to_pydatetime() + STEP[timeframe])
    if inst.asset_class == "crypto":
        return age > 3 * STEP[timeframe]
    if timeframe == "1d":
        return age > timedelta(days=4)
    weekend = (now.weekday() == 4 and now.hour >= 20) or now.weekday() == 5 or \
              (now.weekday() == 6 and now.hour < 23)
    return age > (timedelta(hours=60) if weekend else timedelta(hours=3))
