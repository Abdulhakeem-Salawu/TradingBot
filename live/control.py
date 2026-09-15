"""Choose which paper strategy each universe follows for actual trading.

    python -m live.control status
    python -m live.control compare
    python -m live.control activate --universe fx --strategy tsmom --timeframe 1d
    python -m live.control activate --universe fx --strategy carry --timeframe 1d \\
        --mode demo --capital 10000 --reason "carry leads over 90 days"
    python -m live.control rebalance --universe fx [--resend]
    python -m live.control deactivate --universe fx [--flatten]
    python -m live.control kill [--flatten]      # stop all orders at once
    python -m live.control unkill
    python -m live.control orders

  Used by the MT5 executor over ssh (not usually typed by hand):
    python -m live.control targets --json
    python -m live.control record-execution --payload <base64 json>

A universe (crypto, fx, metals) follows at most one strategy x timeframe at a
time, in one mode:

  signal  DEFAULT. You get trade instructions on Telegram and place orders
          yourself. Works for every universe.
  demo    the MT5 executor trades a MetaTrader 5 DEMO account (fx, metals).
  live    the MT5 executor trades a REAL MetaTrader 5 account (fx, metals).
          Needs ALLOW_LIVE_TRADING=true in .env here AND on the MT5 machine,
          --confirm-live "REAL MONEY", and --accept-unproven unless the
          strategy's paper record is significant (t >= 2 over 90+ days).

This VM cannot see the MT5 account. Demo/live targets are published here and
the executor on the MT5 machine (live.mt5_executor) trades them, checks the
account type and balance, and reports back. Flattening is queued the same way.

Only strategies that are already being paper traded can be activated -- the
comparison is the reason to switch, so there must be one. Every change is
recorded with its reason, and the comparison report charges each switch its
trading cost.
"""

from __future__ import annotations

import argparse
import base64
import json
import math
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from harness.instruments import TIMEFRAMES
from harness.instruments import universe as universe_instruments
from harness.pipeline import STRATEGIES, check_supported
from live.compare import portfolio_daily, report, stats
from live.env import load_env
from live.executor import (DEFAULT_KILL_FILE, EXECUTABLE_UNIVERSES, SLOT_UNIVERSES, execute,
                           export_targets)
from live.ledger import MODES, Ledger
from live.notify import send

CONFIRM_PHRASE = "REAL MONEY"
PROVEN_T, PROVEN_DAYS = 2.0, 90
SYNC_WARN_HOURS = 2


def _paper_record(ledger: Ledger, strategy: str, timeframe: str, universe: str) -> dict | None:
    daily = portfolio_daily(ledger, strategy, timeframe, universe)
    if daily is None or not len(daily):
        return None
    return stats(daily["net"], universe_instruments(universe)[0].days_per_year)


def _record_line(rec: dict | None) -> str:
    if rec is None:
        return "no paper record"
    t = f"{rec['t']:+.1f}" if math.isfinite(rec["t"]) else "-"
    return f"paper {rec['total']:+.2%} over {rec['days']} days, t {t}"


def _live_gate_open() -> bool:
    return os.environ.get("ALLOW_LIVE_TRADING", "").lower() == "true"


def _queue_flatten(ledger: Ledger, universe: str, mode: str) -> str:
    ledger.add_request(universe, "flatten", mode)
    return (f"  flatten queued: the MT5 executor closes the bot's {universe} positions on the "
            f"{mode} account at its next sync")


def cmd_status(ledger: Ledger, a) -> int:
    kill = Path(a.kill_file).exists()
    print(f"Kill switch: {'ON -- no demo/live orders' if kill else 'off'}")
    print(f"Live trading gate (ALLOW_LIVE_TRADING on this machine): "
          f"{'open' if _live_gate_open() else 'closed'}")
    active_modes = {s["mode"] for s in ledger.slots() if s["mode"] in ("demo", "live")}
    for mode in ("demo", "live"):
        sync = ledger.last_sync(mode)
        waiting = mode in active_modes
        if sync is None:
            if waiting:
                print(f"MT5 {mode} executor: never synced -- {mode} slots are waiting "
                      f"(is an executor with EXECUTOR_MODE={mode} running?)")
            continue
        age = datetime.now(timezone.utc) - pd.Timestamp(sync["ts"]).to_pydatetime()
        hours = age.total_seconds() / 3600
        warn = ("  <-- NOT SYNCING (PC off? terminal closed? ssh failing?)"
                if waiting and hours > SYNC_WARN_HOURS else "")
        equity = f", equity {sync['equity']:,.2f} {sync['currency']}" if sync["equity"] is not None else ""
        print(f"MT5 {mode} executor: last sync {sync['ts'][:16]} ({hours:.1f}h ago) from "
              f"{sync['host'] or '?'}, {sync['account_mode'] or '?'} account{equity}{warn}")
    pending = ledger.pending_requests()
    if pending:
        print("Queued for the MT5 executor: " +
              ", ".join(f"{r['action']} {r['universe']} [{r['mode']}]" for r in pending))

    print("\nActive strategies:")
    for u in SLOT_UNIVERSES:
        slot = ledger.slot(u)
        if slot is None:
            print(f"  {u:<7} (none)")
            continue
        rec = _paper_record(ledger, slot["strategy"], slot["timeframe"], u)
        cap = f"${slot['capital']:,.0f}" if slot["capital"] else "no capital set"
        print(f"  {u:<7} {slot['strategy']} {slot['timeframe']} [{slot['mode']}] {cap}, "
              f"since {slot['updated_at'][:16]} -- {_record_line(rec)}")
        for r in ledger.sleeves(slot["strategy"], slot["timeframe"]):
            if r["symbol"] in {i.symbol for i in universe_instruments(u)}:
                print(f"      {r['symbol']:<9} target {r['position']:+.2f}  (bar {r['bar_ts'][:16]})")
    keys = ", ".join(f"{s} {t}" for s, t in ledger.sleeve_keys()) or "none yet"
    print(f"\nPaper-traded strategies: {keys}")
    return 0


def cmd_activate(ledger: Ledger, a) -> int:
    u, mode = a.universe, a.mode
    asset_class = universe_instruments(u)[0].asset_class
    try:
        check_supported(a.strategy, asset_class, a.timeframe)
    except ValueError as e:
        print(e)
        return 2

    syms = {i.symbol for i in universe_instruments(u)}
    have = {r["symbol"] for r in ledger.sleeves(a.strategy, a.timeframe)}
    missing = sorted(syms - have)
    if missing:
        print(f"{a.strategy} {a.timeframe} is not being paper traded for: {', '.join(missing)}.\n"
              f"Add '{a.strategy}:{u}:{a.timeframe}' to JOBS (deploy/setup_vm.sh) and let it run "
              f"first -- you need a paper record to compare before following it.")
        return 2

    rec = _paper_record(ledger, a.strategy, a.timeframe, u)
    old = ledger.slot(u)

    if mode in ("demo", "live"):
        if u not in EXECUTABLE_UNIVERSES:
            print(f"{u} can only run in signal mode: there is no crypto order route (Binance is "
                  f"blocked in Nigeria and refuses US servers).")
            return 2
        if not a.capital or a.capital <= 0:
            print(f"--capital (USD) is required for {mode} mode.")
            return 2
    if mode == "live":
        if not _live_gate_open():
            print("Refused: set ALLOW_LIVE_TRADING=true in .env first (a deliberate second key; "
                  "the MT5 machine needs it too).")
            return 2
        if a.confirm_live != CONFIRM_PHRASE:
            print(f'Refused: add --confirm-live "{CONFIRM_PHRASE}" to confirm real-money orders.')
            return 2
        proven = rec is not None and rec["days"] >= PROVEN_DAYS and rec["t"] >= PROVEN_T
        if not proven and not a.accept_unproven:
            print(f"Refused: {a.strategy} {a.timeframe} has {_record_line(rec)}. A record that "
                  f"could be luck needs t >= {PROVEN_T} over {PROVEN_DAYS}+ days. If you accept "
                  f"trading real money on it anyway, add --accept-unproven.")
            return 2
    if old and old["mode"] in ("demo", "live") and old["mode"] != mode and not a.leave_positions:
        print(f"The {old['mode']} account may still hold {u} positions from the current slot, and "
              f"a {mode} slot will not manage them. Run 'deactivate --universe {u} --flatten' "
              f"first (and let the MT5 executor sync), or pass --leave-positions to keep them.")
        return 2

    ledger.set_slot(u, a.strategy, a.timeframe, mode, a.capital, a.reason or "")
    frm = f"{old['strategy']} {old['timeframe']} [{old['mode']}]" if old else "none"
    msg = [f"SWITCH {u}: {frm} -> {a.strategy} {a.timeframe} [{mode}]"
           + (f", capital ${a.capital:,.0f}" if a.capital else ""),
           f"  reason: {a.reason or '(none given)'}",
           f"  {_record_line(rec)}"]
    outcome = execute(ledger, u)
    msg += outcome.lines
    if mode != "signal":
        msg.append("  The MT5 executor checks the account and places these at its next sync "
                   "(preview there with: python -m live.mt5_executor --dry-run).")
    ledger.commit()
    text = "\n".join(msg)
    print(text)
    send(text)
    return 0


def cmd_deactivate(ledger: Ledger, a) -> int:
    old = ledger.slot(a.universe)
    if old is None:
        print(f"{a.universe} has no active strategy.")
        return 0
    lines = []
    if old["mode"] in ("demo", "live"):
        lines.append(_queue_flatten(ledger, a.universe, old["mode"]) if a.flatten else
                     "  positions in the MT5 account were left open (use --flatten to close them).")
    ledger.set_slot(a.universe, None, None, None, None, a.reason or "deactivated")
    ledger.commit()
    text = "\n".join([f"DEACTIVATED {a.universe}: was {old['strategy']} {old['timeframe']} "
                      f"[{old['mode']}]"] + lines)
    print(text)
    send(text)
    return 0


def cmd_rebalance(ledger: Ledger, a) -> int:
    slot = ledger.slot(a.universe)
    outcome = execute(ledger, a.universe, dry_run=a.dry_run, force_instructions=a.resend)
    if a.dry_run:
        ledger.rollback()
    else:
        ledger.commit()
    text = "\n".join(outcome.lines) or f"{a.universe}: no target changes"
    if slot is not None and slot["mode"] in ("demo", "live"):
        text += "\n  (orders are placed by the MT5 executor at its next sync)"
    print(text)
    if outcome.notify and not a.dry_run:
        send(text)
    return 0


def cmd_kill(ledger: Ledger, a) -> int:
    Path(a.kill_file).parent.mkdir(parents=True, exist_ok=True)
    Path(a.kill_file).write_text("orders halted by live.control kill\n")
    lines = [f"KILL SWITCH ON ({a.kill_file}): the MT5 executor refuses new orders from its "
             f"next sync."]
    for slot in ledger.slots():
        if slot["mode"] in ("demo", "live"):
            if a.flatten:
                lines.append(_queue_flatten(ledger, slot["universe"], slot["mode"]))
            ledger.set_slot(slot["universe"], slot["strategy"], slot["timeframe"], "signal",
                            slot["capital"], "kill switch")
            lines.append(f"  {slot['universe']}: {slot['mode']} -> signal")
    lines.append("  For an immediate stop, also create state/KILL on the MT5 machine or close "
                 "positions in MT5 directly.")
    ledger.commit()
    text = "\n".join(lines)
    print(text)
    send(text)
    return 0


def cmd_unkill(ledger: Ledger, a) -> int:
    p = Path(a.kill_file)
    if p.exists():
        p.unlink()
    text = "Kill switch off. Slots stay in signal mode until you activate demo/live again."
    print(text)
    send(text)
    return 0


def cmd_orders(ledger: Ledger, a) -> int:
    rows = ledger.recent_orders(a.limit)
    if not rows:
        print("No orders or instructions yet.")
    for r in reversed(rows):
        units = f"{r['order_units']:+,.2f}" if r["order_units"] is not None else ""
        tgt = f"target {r['target_pos']:+.2f}" if r["target_pos"] is not None else ""
        print(f"{r['created_at'][:16]}  {(r['universe'] or ''):<7}{(r['mode'] or ''):<7}"
              f"{(r['status'] or ''):<10}{(r['symbol'] or ''):<9}{tgt:<14}{units:>12}  "
              f"{r['message'] or ''}")
    return 0


def cmd_targets(ledger: Ledger, a) -> int:
    print(json.dumps(export_targets(ledger, a.kill_file)))
    return 0


def cmd_record_execution(ledger: Ledger, a) -> int:
    try:
        report_ = json.loads(base64.b64decode(a.payload).decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as e:
        print(f"invalid payload: {e}")
        return 2
    stored = ledger.record_execution(report_)
    ledger.commit()
    summary = report_.get("summary", "")
    if report_.get("notify") and summary:
        send(summary)
    print(f"recorded {stored} order record(s)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Switch the strategy each universe follows")
    ap.add_argument("--db", default="state/ledger.db")
    ap.add_argument("--env-file", default=".env")
    ap.add_argument("--kill-file", default=DEFAULT_KILL_FILE)
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status")
    sub.add_parser("compare")

    p = sub.add_parser("activate")
    p.add_argument("--universe", choices=SLOT_UNIVERSES, required=True)
    p.add_argument("--strategy", choices=STRATEGIES, required=True)
    p.add_argument("--timeframe", choices=TIMEFRAMES, required=True)
    p.add_argument("--mode", choices=MODES, default="signal")
    p.add_argument("--capital", type=float, help="USD allocated to this universe")
    p.add_argument("--reason", help="why you are switching (kept in the audit trail)")
    p.add_argument("--confirm-live", default="", help=f'must be "{CONFIRM_PHRASE}" for live mode')
    p.add_argument("--accept-unproven", action="store_true")
    p.add_argument("--leave-positions", action="store_true")

    p = sub.add_parser("deactivate")
    p.add_argument("--universe", choices=SLOT_UNIVERSES, required=True)
    p.add_argument("--flatten", action="store_true",
                   help="queue closing the universe's MT5 positions")
    p.add_argument("--reason")

    p = sub.add_parser("rebalance")
    p.add_argument("--universe", choices=SLOT_UNIVERSES, required=True)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--resend", action="store_true", help="repeat all instructions/targets")

    p = sub.add_parser("kill")
    p.add_argument("--flatten", action="store_true", help="also queue closing demo/live positions")
    sub.add_parser("unkill")
    p = sub.add_parser("orders")
    p.add_argument("--limit", type=int, default=30)

    p = sub.add_parser("targets", help="JSON for the MT5 executor")
    p.add_argument("--json", action="store_true", help="(the only format)")
    p = sub.add_parser("record-execution", help="store an MT5 executor report")
    p.add_argument("--payload", required=True, help="base64-encoded JSON report")

    a = ap.parse_args()
    load_env(a.env_file)
    ledger = Ledger(a.db)
    if a.cmd == "compare":
        print(report(ledger, list(SLOT_UNIVERSES)))
        return 0
    return {"status": cmd_status, "activate": cmd_activate, "deactivate": cmd_deactivate,
            "rebalance": cmd_rebalance, "kill": cmd_kill, "unkill": cmd_unkill,
            "orders": cmd_orders, "targets": cmd_targets,
            "record-execution": cmd_record_execution}[a.cmd](ledger, a)


if __name__ == "__main__":
    sys.exit(main())
