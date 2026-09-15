"""Compare every paper-traded strategy, per universe.

    python -m live.compare                 # all universes, printed
    python -m live.compare --universe fx
    python -m live.compare --send          # also to Telegram (cron sends this daily)

For each universe, every strategy x timeframe that has been paper traded is
scored on the SAME dates (the common window), with hourly sleeves rolled up
into trading days so they sit next to daily ones fairly.

Columns
  ret     total net return over the common window
  shp     annualised Sharpe (needs 20+ days)
  mdd     maximum drawdown
  cost/y  trading costs per year
  trd     position changes (all instruments)
  t       t-statistic of the mean return: significant only beyond ~2
  dt      t-statistic of the DIFFERENCE from the active strategy, paired day
          by day. This is the column that says whether switching is justified.

"followed" is what actually following the active designation earned: the
active strategy's returns segment by segment, minus the cost of moving the
book at every switch. It is the honest scorecard for your switching decisions.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone

import numpy as np
import pandas as pd
from scipy import stats as sps

from harness.instruments import universe as universe_instruments
from live.env import load_env
from live.executor import SLOT_UNIVERSES
from live.ledger import Ledger
from live.notify import send
from live.timing import STEP, session_dates

MIN_DAYS_FOR_SHARPE = 20
SIGNIFICANT_T = 2.0      # single comparison, two-sided ~95%
MIN_DAYS_TO_SWITCH = 60


def switch_threshold(n_challengers: int) -> float:
    """|dt| needed when the biggest of several gaps is picked (Bonferroni, 95%).

    With five alternatives, the largest of five noise t-statistics clears 2.0
    far more often than 5% of the time, so the bar rises with the count.
    """
    return float(max(SIGNIFICANT_T, sps.norm.ppf(1 - 0.025 / max(n_challengers, 1))))


def _session_date_of(ts: pd.Timestamp, asset_class: str):
    ts = pd.Timestamp(ts)
    if asset_class in ("fx", "metal"):
        return (ts.tz_convert("America/New_York") + pd.Timedelta(hours=7)).date()
    return ts.tz_convert("UTC").date()


def portfolio_daily(ledger: Ledger, strategy: str, timeframe: str,
                    universe: str) -> pd.DataFrame | None:
    """Equal-weight paper portfolio for one strategy, as daily net / cost / financing."""
    insts = universe_instruments(universe)
    syms = [i.symbol for i in insts]
    net = ledger.net_returns(strategy, timeframe, syms, "net")
    if net.empty:
        return None
    cost = ledger.net_returns(strategy, timeframe, syms, "cost").mean(axis=1)
    fin = ledger.net_returns(strategy, timeframe, syms, "financing").mean(axis=1)
    bar = net.mean(axis=1)
    dates = session_dates(bar.index, timeframe, insts[0].asset_class)
    return pd.DataFrame({
        "net": (1 + bar).groupby(dates).prod() - 1,
        "cost": cost.groupby(dates).sum(),
        "financing": fin.groupby(dates).sum(),
    })


def stats(net: pd.Series, days_per_year: int) -> dict:
    r = net.to_numpy(dtype=float)
    n = len(r)
    equity = np.cumprod(1 + r) if n else np.array([1.0])
    peak = np.maximum.accumulate(equity)
    sd = r.std(ddof=1) if n > 1 else 0.0
    return {
        "days": n,
        "total": float(equity[-1] - 1),
        "sharpe": float(r.mean() / sd * np.sqrt(days_per_year))
        if n >= MIN_DAYS_FOR_SHARPE and sd > 0 else float("nan"),
        "t": float(r.mean() / sd * np.sqrt(n)) if n > 1 and sd > 0 else float("nan"),
        "max_dd": float((equity / peak - 1).min()) if n else 0.0,
    }


def paired_t(a: pd.Series, b: pd.Series) -> tuple[float, float]:
    """(t of mean daily difference a - b, days needed for |t| = 2 if the gap persists)."""
    d = (a - b).dropna()
    n = len(d)
    sd = d.std(ddof=1) if n > 1 else 0.0
    if n < 2 or sd == 0:
        return float("nan"), float("nan")
    t = d.mean() / sd * np.sqrt(n)
    need = (SIGNIFICANT_T * sd / abs(d.mean())) ** 2 if d.mean() != 0 else float("inf")
    return float(t), float(need)


def followed_daily(ledger: Ledger, universe: str) -> tuple[pd.Series, int, float] | None:
    """Daily returns from following the slot's designations, including switch costs."""
    switches = ledger.switches(universe)
    if not switches:
        return None
    insts = universe_instruments(universe)
    syms = [i.symbol for i in insts]
    per_side = {i.symbol: i.costs.per_side for i in insts}
    asset_class = insts[0].asset_class
    pieces, n_switches, switch_cost_total = [], 0, 0.0

    for k, sw in enumerate(switches):
        start = pd.Timestamp(sw["created_at"])
        end = pd.Timestamp(switches[k + 1]["created_at"]) if k + 1 < len(switches) else None

        # Cost of moving the book from the old designation to the new one.
        if (sw["from_strategy"], sw["from_timeframe"]) != (sw["to_strategy"], sw["to_timeframe"]):
            cost = 0.0
            for sym in syms:
                old = (ledger.position_at(sw["from_strategy"], sw["from_timeframe"], sym,
                                          sw["created_at"]) if sw["from_strategy"] else 0.0)
                new = (ledger.position_at(sw["to_strategy"], sw["to_timeframe"], sym,
                                          sw["created_at"]) if sw["to_strategy"] else 0.0)
                cost += abs(new - old) * per_side[sym]
            cost /= len(syms)
            if cost:
                pieces.append(pd.Series([-cost], index=[_session_date_of(start, asset_class)]))
                switch_cost_total += cost
            n_switches += 1

        if sw["to_strategy"] is None:
            continue
        net = ledger.net_returns(sw["to_strategy"], sw["to_timeframe"], syms)
        if net.empty:
            continue
        bar = net.mean(axis=1)
        bar_end = bar.index + STEP[sw["to_timeframe"]]
        keep = bar_end > start
        if end is not None:
            keep &= bar_end <= end
        seg = bar[keep]
        if len(seg):
            dates = session_dates(seg.index, sw["to_timeframe"], asset_class)
            pieces.append((1 + seg).groupby(dates).prod() - 1)

    if not pieces:
        return pd.Series(dtype=float), n_switches, switch_cost_total
    allp = pd.concat(pieces)
    daily = (1 + allp).groupby(level=0).prod() - 1
    return daily.sort_index(), n_switches, switch_cost_total


def compare_universe(ledger: Ledger, universe: str) -> list[str]:
    insts = universe_instruments(universe)
    dpy = insts[0].days_per_year
    syms = {i.symbol for i in insts}
    keys = [k for k in ledger.sleeve_keys()
            if {r["symbol"] for r in ledger.sleeves(*k)} & syms]
    series = {}
    for strategy, timeframe in keys:
        daily = portfolio_daily(ledger, strategy, timeframe, universe)
        if daily is not None and len(daily):
            series[(strategy, timeframe)] = daily
    if not series:
        return [f"== {universe.upper()} == no paper sleeves yet"]

    common_start = max(d.index[0] for d in series.values())
    slot = ledger.slot(universe)
    active = (slot["strategy"], slot["timeframe"]) if slot else None
    active_net = series[active]["net"].loc[common_start:] if active in series else None
    last = max(d.index[-1] for d in series.values())
    n_common = max(len(d.loc[common_start:]) for d in series.values())

    lines = [f"== {universe.upper()}  common window {common_start} .. {last} ({n_common} days) =="]
    lines.append(f"  {'strategy':<10}{'ret':>7}{'shp':>6}{'mdd':>7}{'cost/y':>7}{'trd':>5}"
                 f"{'t':>6}{'dt':>6}")
    challengers = []
    for key in sorted(series, key=lambda k: (k != active, k)):
        d = series[key].loc[common_start:]
        s = stats(d["net"], dpy)
        years = max(len(d) / dpy, 1 / dpy)
        trades = ledger.trade_count(key[0], key[1], sorted(syms),
                                    since=pd.Timestamp(common_start).isoformat())
        if key == active or active_net is None:
            dt = "-"
        else:
            t, need = paired_t(d["net"], active_net)
            dt = f"{t:+.1f}" if np.isfinite(t) else "-"
            if np.isfinite(t):
                challengers.append((abs(t), key, t, need, len(d)))
        mark = "*" if key == active else " "
        shp = f"{s['sharpe']:.1f}" if np.isfinite(s["sharpe"]) else "-"
        tt = f"{s['t']:+.1f}" if np.isfinite(s["t"]) else "-"
        lines.append(f"{mark} {key[0] + ' ' + key[1]:<10}{s['total']:>+7.1%}{shp:>6}"
                     f"{s['max_dd']:>7.1%}{d['cost'].sum() / years:>7.1%}{trades:>5}"
                     f"{tt:>6}{dt:>6}")

    if active is None:
        lines.append("  no active strategy -- python -m live.control activate ...")
    else:
        lines.append(f"  active: {active[0]} {active[1]} [{slot['mode']}]"
                     + (f", capital ${slot['capital']:,.0f}" if slot["capital"] else ""))
        if challengers:
            bar = switch_threshold(len(challengers))
            _, key, t, need, n = max(challengers)
            name = f"{key[0]} {key[1]}"
            if abs(t) >= bar and n >= MIN_DAYS_TO_SWITCH:
                side = "better" if t > 0 else "worse"
                lines.append(f"  {name} is significantly {side} (dt {t:+.1f} >= {bar:.1f} "
                             f"for {len(challengers)} alternative(s), {n} days)")
            else:
                if n < MIN_DAYS_TO_SWITCH:
                    why = f"only {n} of {MIN_DAYS_TO_SWITCH} days"
                elif np.isfinite(need):
                    need_bar = need * (bar / SIGNIFICANT_T) ** 2
                    why = f"~{max(need_bar - n, 0):,.0f} more days if the gap persists"
                else:
                    why = "no measurable gap"
                lines.append(f"  biggest gap: {name} dt {t:+.1f}, needs |dt| >= {bar:.1f} -- "
                             f"not significant ({why})")
    fol = followed_daily(ledger, universe)
    if fol is not None and len(fol[0]):
        daily, n_sw, sw_cost = fol
        s = stats(daily, dpy)
        lines.append(f"  followed: {s['total']:+.2%} since {daily.index[0]}, {n_sw} switch(es), "
                     f"switch costs {sw_cost:.2%}")
    return lines


def report(ledger: Ledger, universes) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    out = [f"PAPER STRATEGY COMPARISON  {stamp}",
           f"dt = difference vs active (*). Noise until |dt| clears the bar shown (2.0+, higher",
           f"with more alternatives) over {MIN_DAYS_TO_SWITCH}+ days; switching on less is chasing",
           "noise, and every switch costs.", ""]
    for u in universes:
        out += compare_universe(ledger, u) + [""]
    blocked = [r for r in ledger.recent_orders(50) if r["status"] == "blocked"]
    if blocked:
        out.append(f"recent blocked orders: {len(blocked)} -- see python -m live.control orders")
    return "\n".join(out).rstrip()


def main() -> int:
    ap = argparse.ArgumentParser(description="Compare paper-traded strategies")
    ap.add_argument("--universe", choices=SLOT_UNIVERSES)
    ap.add_argument("--db", default="state/ledger.db")
    ap.add_argument("--env-file", default=".env")
    ap.add_argument("--send", action="store_true", help="also send to Telegram")
    a = ap.parse_args()
    load_env(a.env_file)

    ledger = Ledger(a.db)
    text = report(ledger, [a.universe] if a.universe else list(SLOT_UNIVERSES))
    print(text)
    if a.send:
        send(text, pre=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
