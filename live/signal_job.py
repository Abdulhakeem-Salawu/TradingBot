"""Paper-trading signal job. Run once per bar from cron.

    python -m live.signal_job --universe crypto --timeframe 1h
    python -m live.signal_job --universe fx --timeframe 1d --notify always
    python -m live.signal_job --universe metals --timeframe 1h --dry-run
    python -m live.signal_job --universe crypto --timeframe 1h --report --notify always

Each run:
  1. updates the price cache to the last CLOSED bar,
  2. computes target positions with harness.pipeline -- the exact code the
     backtest used,
  3. marks every paper sleeve to market bar by bar since its last run,
     charging the same turnover and financing costs as the backtest,
  4. notifies on position changes, stale data or errors.

Re-running for a bar that was already processed does nothing, so an extra
cron invocation or a manual run is harmless. No real orders are ever placed.

WHAT PAPER TRADING CAN AND CANNOT TELL YOU. Within days it verifies the
plumbing: data arrives, signals match the backtest, costs look right. It
cannot confirm an edge quickly at ANY bar frequency: the t-statistic of a
track record is Sharpe * sqrt(years), so hourly bars do not shorten the wait.
--report prints that t-statistic so the wait is explicit rather than guessed.
"""

from __future__ import annotations

import argparse
import os
import sys
import warnings
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from harness.data import load_prices, load_short_rates, price_source
from harness.instruments import TIMEFRAMES, UNIVERSES, universe
from harness.pipeline import STRATEGIES, check_supported, financing_for, target_positions
from harness.scoring import years_to_confirm
from live.env import load_env
from live.executor import SLOT_UNIVERSES, execute
from live.ledger import Ledger
from live.notify import send
from live.timing import is_stale


NOTIFY_LEVELS = ("changes", "problems", "always", "never")


def should_notify(level: str, changed: bool, problems: bool, action_needed: bool) -> bool:
    """changes: any position change, problem or action; problems: only problems and
    actions (errors, stale data, orders a signal slot wants placed by hand)."""
    if level == "always":
        return True
    if level == "never":
        return False
    return problems or action_needed or (level == "changes" and changed)


def _fmt_ts(ts) -> str:
    return pd.Timestamp(ts).strftime("%Y-%m-%d %H:%M UTC")


def process(inst, strategy: str, timeframe: str, rates: dict, ledger: Ledger,
            cache_dir: str, offline: bool = False, df=None) -> dict:
    if df is None:
        df = load_prices(inst, timeframe, cache_dir, offline)
    close = df["close"]
    target, start = target_positions(inst, close, strategy, timeframe, rates)
    fin = financing_for(inst, close.index, rates)
    ts = close.index
    per_side = inst.costs.per_side
    out = {"symbol": inst.symbol, "changes": [], "new_bars": 0,
           "stale": is_stale(inst, timeframe, ts[-1], datetime.now(timezone.utc))}

    if len(close) <= start:
        out["status"] = f"warming up: {len(close)} of {start} bars"
        return out

    row = ledger.position(strategy, timeframe, inst.symbol)
    if row is None:
        # First run for this sleeve: start at the latest closed bar. No backfill --
        # a paper record that includes the past is just a backtest.
        k = len(close) - 1
        pos, price = float(target.iloc[k]), float(close.iloc[k])
        cost = abs(pos) * per_side
        held, equity, bar = pos, 1.0 - cost, ts[k]
        ledger.add_bar(strategy, timeframe, inst.symbol, bar.isoformat(), 0.0, cost, 0.0,
                       -cost, equity)
        if pos != 0:
            ledger.add_trade(strategy, timeframe, inst.symbol, bar.isoformat(), 0.0, pos,
                             price, cost)
            out["changes"].append((0.0, pos, price, bar))
        ledger.set_position(strategy, timeframe, inst.symbol, held, price, bar.isoformat(),
                            equity, bar.isoformat())
        out.update(position=held, price=price, bar=bar, equity=equity, new_bars=1,
                   started=bar, status="started paper trading")
        return out

    held, equity = float(row["position"]), float(row["equity"])
    prev_price, prev_ts = float(row["price"]), pd.Timestamp(row["bar_ts"])
    for k in np.flatnonzero(ts > prev_ts):
        price, bar = float(close.iloc[k]), ts[k]
        days = (bar - prev_ts).total_seconds() / 86400.0
        if fin is None:
            financing = float(inst.costs.financing_cost(held, days))
        else:
            rates_then = fin.iloc[max(k - 1, 0)]
            rate = rates_then["long"] if held > 0 else rates_then["short"]
            financing = abs(held) * rate * days / 365.0
        gross = held * (price / prev_price - 1.0)
        tgt = float(target.iloc[k])
        cost = abs(tgt - held) * per_side
        net = gross - cost - financing
        equity *= 1.0 + net
        ledger.add_bar(strategy, timeframe, inst.symbol, bar.isoformat(), gross, cost,
                       financing, net, equity)
        if tgt != held:
            ledger.add_trade(strategy, timeframe, inst.symbol, bar.isoformat(), held, tgt,
                             price, cost)
            out["changes"].append((held, tgt, price, bar))
        held, prev_price, prev_ts = tgt, price, bar
        out["new_bars"] += 1

    ledger.set_position(strategy, timeframe, inst.symbol, held, prev_price,
                        prev_ts.isoformat(), equity, row["started_ts"])
    out.update(position=held, price=prev_price, bar=prev_ts, equity=equity,
               started=pd.Timestamp(row["started_ts"]),
               status=f"{out['new_bars']} new bar(s)" if out["new_bars"] else "no new bar")
    return out


def portfolio_report(ledger: Ledger, strategy: str, timeframe: str, insts) -> list[str]:
    """Equal-weight paper portfolio per asset class: return, live Sharpe, distance from proof.

    Classes are reported separately because their calendars differ (24/7 vs
    24/5), so one blended bar count would misstate the years elapsed.
    """
    lines = []
    for cls in dict.fromkeys(i.asset_class for i in insts):
        group = [i for i in insts if i.asset_class == cls]
        lines += _class_report(ledger, strategy, timeframe, group, cls)
    return lines


def _class_report(ledger: Ledger, strategy: str, timeframe: str, insts, cls: str) -> list[str]:
    wide = ledger.net_returns(strategy, timeframe, [i.symbol for i in insts])
    if wide.empty:
        return [f"{cls} portfolio: no paper history yet."]
    net = wide.mean(axis=1)
    ppy = insts[0].periods_per_year(timeframe)
    years = len(net) / ppy
    total = float(np.prod(1 + net.to_numpy()) - 1)
    lines = [f"{cls} portfolio (equal weight): {total:+.2%} over {len(net)} bars "
             f"({years * insts[0].days_per_year:.0f} trading days)"]
    sd = net.std()
    if len(net) >= 30 and sd > 0:
        sharpe = float(net.mean() / sd * np.sqrt(ppy))
        t = sharpe * np.sqrt(years)
        ytc = years_to_confirm(sharpe)
        lines.append(f"Live Sharpe {sharpe:.2f}, t-stat {t:.2f} "
                     f"({'significant' if t >= 1.645 else 'not yet significant'} at 95%; "
                     f"needs 1.65)")
        if np.isfinite(ytc):
            lines.append(f"At this Sharpe a significant record takes {ytc:.1f} years in total, "
                         f"hourly or daily alike.")
    else:
        lines.append("Too few bars for a live Sharpe yet.")
    return lines


def main() -> int:
    ap = argparse.ArgumentParser(description="Paper-trading signal job")
    ap.add_argument("--universe", choices=sorted(UNIVERSES), required=True)
    ap.add_argument("--strategy", choices=STRATEGIES, default="tsmom")
    ap.add_argument("--timeframe", choices=TIMEFRAMES, required=True)
    ap.add_argument("--db", default="state/ledger.db")
    ap.add_argument("--cache-dir", default="data")
    ap.add_argument("--env-file", default=".env")
    ap.add_argument("--notify", choices=NOTIFY_LEVELS, default=None,
                    help="when to send Telegram: changes (default, or NOTIFY_PAPER), problems "
                         "(errors, warnings, orders to place by hand), always, never")
    ap.add_argument("--offline", action="store_true", help="use cached prices only")
    ap.add_argument("--dry-run", action="store_true",
                    help="compute and print, but write nothing and send nothing")
    ap.add_argument("--report", action="store_true",
                    help="summarise the ledger without processing new bars")
    a = ap.parse_args()
    warnings.filterwarnings("ignore", category=RuntimeWarning)
    load_env(a.env_file)
    a.notify = a.notify or os.environ.get("NOTIFY_PAPER") or "changes"
    if a.notify not in NOTIFY_LEVELS:
        print(f"NOTIFY_PAPER={a.notify!r}: use one of {', '.join(NOTIFY_LEVELS)}")
        return 2

    insts = universe(a.universe)
    if a.strategy == "carry":
        insts = [i for i in insts if i.asset_class == "fx"]
    try:
        for cls in {i.asset_class for i in insts} or {"none"}:
            check_supported(a.strategy, cls, a.timeframe)
    except ValueError as e:
        print(e)
        return 2

    ledger = Ledger(a.db)
    sources = ",".join(sorted({price_source(i) for i in insts})) or "-"
    header = f"{a.strategy} | {a.universe} | {a.timeframe} | paper | prices {sources}"
    lines, errors, warnings_, any_change = [], [], [], False

    if not a.report:
        try:
            ccys = [c for i in insts if i.uses_rates for c in (i.base, i.quote)]
            rates = load_short_rates(ccys, a.cache_dir, a.offline) if ccys else {}
        except Exception as e:  # noqa: BLE001 -- report and keep the other sleeves alive
            rates = {}
            errors.append(f"rates: {e}")

        # Download everything BEFORE touching the ledger: SQLite allows one writer
        # at a time, and a first download can take minutes. Writing while
        # downloading would lock out the MT5 executor's reports and live.control.
        prices = {}
        for inst in insts:
            try:
                prices[inst.symbol] = load_prices(inst, a.timeframe, a.cache_dir, a.offline)
            except Exception as e:  # noqa: BLE001
                errors.append(f"{inst.symbol}: {type(e).__name__}: {e}")
        results = []
        for inst in insts:
            if inst.symbol not in prices:
                continue
            try:
                results.append(process(inst, a.strategy, a.timeframe, rates, ledger,
                                       a.cache_dir, a.offline, df=prices[inst.symbol]))
            except Exception as e:  # noqa: BLE001
                errors.append(f"{inst.symbol}: {type(e).__name__}: {e}")

        bars = [r["bar"] for r in results if "bar" in r]
        if bars:
            header += f" | last bar {_fmt_ts(max(bars))}"
        changes = [(r["symbol"], c) for r in results for c in r["changes"]]
        any_change = bool(changes)
        if changes:
            lines.append("Position changes:")
            for sym, (frm, to, price, bar) in changes:
                lines.append(f"  {sym:<9} {frm:+.2f} -> {to:+.2f} @ {price:,.5g}  ({_fmt_ts(bar)})")
        for r in results:
            if r["stale"]:
                warnings_.append(f"{r['symbol']}: data stale (last bar {_fmt_ts(r.get('bar'))})"
                                 if r.get("bar") is not None else f"{r['symbol']}: data stale")
            if r.get("status", "").startswith("warming"):
                warnings_.append(f"{r['symbol']}: {r['status']}")

    lines.append("Positions:")
    for row in ledger.sleeves(a.strategy, a.timeframe):
        if row["symbol"] in {i.symbol for i in insts}:
            lines.append(f"  {row['symbol']:<9} {row['position']:+.2f}   "
                         f"paper {row['equity'] - 1:+.2%} since {row['started_ts'][:10]}")
    lines += portfolio_report(ledger, a.strategy, a.timeframe, insts)
    if warnings_:
        lines += ["Warnings:"] + [f"  {w}" for w in warnings_]
    if errors:
        lines += ["ERRORS:"] + [f"  {e}" for e in errors]

    message = "\n".join([header] + lines)
    print(message)

    if a.dry_run:
        exec_lines, _ = run_active_slots(ledger, a, insts, dry_run=True)
        if exec_lines:
            print("\n".join(exec_lines))
        ledger.rollback()
        print("(dry run: ledger not written, no orders, nothing sent)")
        return 1 if errors else 0
    ledger.commit()   # paper results are saved before any order is attempted

    exec_lines, exec_notify = ([], False) if a.report else run_active_slots(ledger, a, insts)
    ledger.commit()
    if exec_lines:
        print("\n".join(exec_lines))
        message += "\n" + "\n".join(exec_lines)

    if should_notify(a.notify, any_change, bool(warnings_ or errors), exec_notify):
        send(message)
    return 1 if errors else 0


def run_active_slots(ledger: Ledger, a, insts, dry_run: bool = False) -> tuple[list[str], bool]:
    """Act on every slot whose active strategy this job just updated."""
    symbols = {i.symbol for i in insts}
    lines, notify = [], False
    for slot_universe in SLOT_UNIVERSES:
        slot = ledger.slot(slot_universe)
        if slot is None or (slot["strategy"], slot["timeframe"]) != (a.strategy, a.timeframe):
            continue
        if not {i.symbol for i in universe(slot_universe)} <= symbols:
            continue
        try:
            outcome = execute(ledger, slot_universe, dry_run=dry_run)
        except Exception as e:  # noqa: BLE001 -- an executor bug must be loud, not fatal
            lines.append(f"ACTIVE {slot_universe}: executor error {type(e).__name__}: {e}")
            notify = True
            continue
        lines += outcome.lines
        notify |= outcome.notify
    return lines, notify


if __name__ == "__main__":
    sys.exit(main())
