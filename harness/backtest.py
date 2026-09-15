"""Position-based simulation.

The fixed-horizon simulator in scoring.py treats every signal as a separate
bet and charges a full round trip for each. That is right for "enter, hold h
bars, exit" classifiers and badly wrong for anything that HOLDS: a trend rule
that stays long for a month would be charged ~30 round trips for one trade.

Here the strategy is a position series instead:

    net[t] = pos[t-1] * r[t]  -  turnover_cost(|pos[t] - pos[t-1]|)
                              -  financing(pos[t-1], days held)

pos[t] is decided at the close of bar t and earns bar t+1's return. The lag is
applied inside simulate_positions, so a caller cannot accidentally trade on a
close it has not seen yet.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from .costs import CostModel
from .strategies import buy_and_hold, vol_target_hold


@dataclass
class SimResult:
    net: pd.Series        # net return per bar
    gross: pd.Series      # price P&L only
    cost: pd.Series       # turnover costs (positive = paid)
    financing: pd.Series  # financing (positive = paid, negative = earned)
    turnover: pd.Series   # |change in position| at each close
    position: pd.Series   # position decided at each close

    def slice(self, start: int) -> "SimResult":
        """Drop the first `start` bars (strategy warm-up)."""
        return SimResult(*(getattr(self, f).iloc[start:] for f in
                           ("net", "gross", "cost", "financing", "turnover", "position")))


def simulate_positions(target: pd.Series, close: pd.Series, costs: CostModel,
                       financing: pd.DataFrame | None = None,
                       long_only: bool = False) -> SimResult:
    """Simulate holding `target` positions (fractions of capital, -1..1).

    financing: optional frame of annual rates with columns "long" and "short"
    (positive = paid). When omitted the CostModel's constant rates apply.
    """
    close = close.astype(float)
    idx = close.index
    pos = target.reindex(idx).astype(float).fillna(0.0)
    pos = pos.clip(0.0 if long_only else -1.0, 1.0)

    ret = close.pct_change().fillna(0.0)
    held = pos.shift(1).fillna(0.0)
    turnover = pos.diff().abs()
    turnover.iloc[0] = abs(pos.iloc[0])
    days = pd.Series(idx, index=idx).diff().dt.total_seconds().fillna(0.0) / 86400.0

    gross = held * ret
    cost = turnover * costs.per_side
    if financing is None:
        fin = pd.Series(costs.financing_cost(held.to_numpy(), days.to_numpy()), index=idx)
    else:
        rates = financing.reindex(idx).ffill().fillna(0.0).shift(1).fillna(0.0)
        rate = np.where(held > 0, rates["long"], rates["short"])
        fin = held.abs() * rate * days / 365.0

    net = gross - cost - fin
    return SimResult(net=net, gross=gross, cost=cost, financing=fin,
                     turnover=turnover, position=pos)


def combine(sims: list[SimResult]) -> SimResult:
    """Equal-weight portfolio of already vol-targeted sleeves.

    Each sleeve targets the same volatility, so equal capital weights are
    roughly equal risk. On dates where a sleeve has no data yet (a coin not
    yet listed) the remaining sleeves share the weight.
    """
    def avg(field: str) -> pd.Series:
        return pd.concat([getattr(s, field) for s in sims], axis=1).mean(axis=1)

    return SimResult(net=avg("net"), gross=avg("gross"), cost=avg("cost"),
                     financing=avg("financing"), turnover=avg("turnover"),
                     position=avg("position"))


def shuffled_timing(position: pd.Series, close: pd.Series, costs: CostModel,
                    financing: pd.DataFrame | None, long_only: bool,
                    min_shift: int, n_shuffles: int = 20, seed: int = 0) -> SimResult:
    """The strategy's own positions, circularly shifted to a random point in time.

    Same exposure, same turnover, same long/short mix -- only the timing is
    destroyed. If this matches the strategy, the result came from holding the
    asset, not from knowing when to hold it. Returns the median-Sharpe shuffle.
    `min_shift` should be long enough to break any trend, e.g. a quarter year.
    """
    n = len(position)
    if n < 2 * min_shift + 1:
        min_shift = max(1, n // 4)
    rng = np.random.default_rng(seed)
    runs = []
    for _ in range(n_shuffles):
        shift = int(rng.integers(min_shift, n - min_shift))
        rolled = pd.Series(np.roll(position.to_numpy(), shift), index=position.index)
        sim = simulate_positions(rolled, close, costs, financing, long_only)
        sd = sim.net.std()
        runs.append((sim.net.mean() / sd if sd > 0 else -np.inf, sim))
    runs.sort(key=lambda t: t[0])
    return runs[len(runs) // 2][1]


def benchmark_suite(close: pd.Series, costs: CostModel, financing: pd.DataFrame | None,
                    strategy: SimResult, asset_class: str, long_only: bool,
                    periods_per_year: int, vol_days: float, start: int = 0,
                    seed: int = 0) -> tuple[dict[str, SimResult], str]:
    """Benchmarks for one instrument, and which one excess returns are measured against.

    crypto / metal: buy-and-hold and vol-targeted hold. The asset has a
        long-run premium, so the question is whether timing beats simply
        holding the same risk. Primary = vol_target_hold.
    fx: no natural direction, so flat is primary; a vol-targeted long is kept
        to catch a rule that is just long a pair that happened to trend.
    all: shuffled timing, so exposure alone cannot pass for skill.
    """
    def masked(pos: pd.Series) -> pd.Series:
        pos = pos.copy()
        pos.iloc[:start] = 0.0
        return pos

    sim = lambda pos: simulate_positions(masked(pos), close, costs, financing, long_only)
    out = {"flat": sim(pd.Series(0.0, index=close.index))}
    if asset_class == "fx":
        out["vol_target_long"] = sim(vol_target_hold(close, periods_per_year, vol_days=vol_days))
        primary = "flat"
    else:
        out["buy_and_hold"] = sim(buy_and_hold(close))
        out["vol_target_hold"] = sim(vol_target_hold(close, periods_per_year, vol_days=vol_days))
        primary = "vol_target_hold"
    out["shuffled_timing"] = shuffled_timing(strategy.position, close, costs, financing,
                                             long_only, min_shift=periods_per_year // 4,
                                             seed=seed)
    return out, primary
