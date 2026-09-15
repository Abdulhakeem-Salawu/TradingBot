"""Run on the machine with MetaTrader 5: trade the VM's demo/live targets.

    python -m live.mt5_executor                 # one sync -- schedule every 10 minutes
    python -m live.mt5_executor --dry-run       # show what would be traded; no orders, no report
    python -m live.mt5_executor --status        # account, the bot's positions, current targets
    python -m live.mt5_executor --check-costs   # broker spreads and swaps vs the cost model
    python -m live.mt5_executor --smoke-test    # DEMO only: open/reduce/flip/close one min lot
    python -m live.mt5_executor --env-file .env.live   # a second executor for a live terminal

One executor serves ONE kind of account (EXECUTOR_MODE=demo or live, default
demo): it trades only the slots and flatten requests of that mode, and refuses
to run if its terminal is logged into the other kind. Demo and live can run
side by side with two MT5 terminals, each with its own .env file
(MT5_TERMINAL_PATH, MT5_LOGIN) and scheduled task. MT5_LOGIN pins the account
number: a terminal logged into any other account is refused.

Where targets come from (--targets-source):
  ssh     (default) the Windows PC asks the VM over ssh. Set VM_SSH_TARGET to
          the host alias written by `gcloud compute config-ssh`
          (e.g. signal-bot.us-central1-a.my-project) and VM_BOT_DIR if the
          project is not in ~/signal-monitoring on the VM.
  ledger  the paper jobs run on this machine too (Windows test phase, or Linux
          + Wine): read and write state/ledger.db directly.

A sync: fetch targets -> connect to MT5 -> run queued flattens -> for each
demo/live universe run every safety check and trade to target -> report the
result back to the ledger, which stores it and sends Telegram messages. If the
targets cannot be read, nothing is traded. A report that cannot be delivered
(VM unreachable, ledger busy) is kept in state/unreported/ and re-sent at the
next sync, so no fill goes unrecorded. Nothing here stores your MT5 password
anywhere except the optional MT5_PASSWORD you put in this machine's .env.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from harness.instruments import universe as universe_instruments
from live.broker import BrokerError
from live.env import load_env
from live.executor import (DEFAULT_KILL_FILE, EXECUTABLE_UNIVERSES, Limits, Outcome, TargetSet,
                           _blocked, execute_targets, export_targets, flatten_universe)

STATE_FILE = "state/mt5_executor_state.json"   # one file per executor mode
OUTBOX_DIR = "state/unreported"                  # reports that could not be delivered yet
REPEAT_ALERT_AFTER = timedelta(hours=6)
EXECUTOR_MODES = ("demo", "live")


def executor_mode(value: str | None = None) -> str:
    mode = (value or os.environ.get("EXECUTOR_MODE") or "demo").strip().lower()
    if mode not in EXECUTOR_MODES:
        raise ValueError(f"EXECUTOR_MODE={mode!r}: use demo or live")
    return mode


def _state_file(mode: str) -> str:
    return STATE_FILE.replace(".json", f"_{mode}.json")


# ------------------------------------------------------------------ transport
def _ssh_base() -> list[str]:
    target = os.environ.get("VM_SSH_TARGET")
    if not target:
        raise RuntimeError("VM_SSH_TARGET is not set in .env (run `gcloud compute config-ssh` "
                           "and use the host alias it prints, e.g. "
                           "signal-bot.us-central1-a.my-project)")
    ssh = shutil.which("ssh")
    if not ssh:
        raise RuntimeError("ssh not found. On Windows: Settings > Apps > Optional features > "
                           "add 'OpenSSH Client'.")
    return [ssh, "-o", "BatchMode=yes", "-o", "ConnectTimeout=20", target]


def _remote(args: str) -> str:
    bot_dir = os.environ.get("VM_BOT_DIR", "~/signal-monitoring")
    return f"cd {bot_dir} && .venv/bin/python -m live.control {args}"


def _run_ssh(args: str, runner=subprocess.run, attempts: int = 3) -> str:
    cmd = _ssh_base() + [_remote(args)]
    last = ""
    for _ in range(attempts):
        try:
            res = runner(cmd, capture_output=True, text=True, timeout=120)
        except subprocess.TimeoutExpired:
            last = "ssh timed out"
            continue
        if res.returncode == 0:
            return res.stdout
        last = (res.stderr or res.stdout or "").strip()[-400:]
    raise RuntimeError(f"ssh to the VM failed: {last}")


def fetch_targets(source: str, db: str, kill_file: str, runner=subprocess.run) -> dict:
    if source == "ledger":
        from live.ledger import Ledger
        return export_targets(Ledger(db), kill_file)
    out = _run_ssh("targets --json", runner)
    line = next((ln for ln in reversed(out.splitlines()) if ln.strip().startswith("{")), None)
    if line is None:
        raise RuntimeError(f"the VM returned no targets JSON: {out.strip()[-300:]}")
    return json.loads(line)


def _outbox(mode: str) -> Path:
    return Path(OUTBOX_DIR) / mode


def save_unreported(report: dict, mode: str) -> Path:
    """Keep a report whose delivery failed; fills must reach the ledger eventually."""
    folder = _outbox(mode)
    folder.mkdir(parents=True, exist_ok=True)
    stamp = report.get("ts", datetime.now(timezone.utc).isoformat()).replace(":", "")
    path = folder / f"{stamp}-{hashlib.sha1(json.dumps(report).encode()).hexdigest()[:8]}.json"
    path.write_text(json.dumps(report))
    return path


def flush_unreported(source: str, db: str, mode: str, runner=subprocess.run) -> list[str]:
    """Re-send saved reports, oldest first. Stops at the first failure."""
    lines = []
    for path in sorted(_outbox(mode).glob("*.json")):
        try:
            report_back(source, json.loads(path.read_text()), db, runner)
        except Exception as e:  # noqa: BLE001 -- try again at the next sync
            lines.append(f"still unreported: {path.name} ({e})")
            break
        path.unlink()
        lines.append(f"delivered an earlier report: {path.name}")
    return lines


def report_back(source: str, report: dict, db: str, runner=subprocess.run) -> None:
    if source == "ledger":
        from live.ledger import Ledger
        from live.notify import send
        ledger = Ledger(db)
        ledger.record_execution(report)
        ledger.commit()
        if report.get("notify") and report.get("summary"):
            send(report["summary"])
        return
    payload = base64.b64encode(json.dumps(report).encode("utf-8")).decode("ascii")
    _run_ssh(f"record-execution --payload {payload}", runner)


# ---------------------------------------------------------------- de-dupe state
def _load_state(mode: str) -> dict:
    try:
        return json.loads(Path(_state_file(mode)).read_text())
    except (OSError, ValueError):
        return {}


def _save_state(state: dict, mode: str) -> None:
    Path(_state_file(mode)).parent.mkdir(parents=True, exist_ok=True)
    Path(_state_file(mode)).write_text(json.dumps(state))


def _suppress_repeat(state: dict, signature: str, now: datetime) -> bool:
    """True if this exact blocked/error message was already reported recently."""
    last_sig, last_at = state.get("sig"), state.get("at")
    if last_sig == signature and last_at and now - datetime.fromisoformat(last_at) < REPEAT_ALERT_AFTER:
        return True
    state.update(sig=signature, at=now.isoformat())
    return False


# ------------------------------------------------------------------------ sync
def sync(args, broker_factory=None, runner=subprocess.run, now: datetime | None = None) -> int:
    now = now or datetime.now(timezone.utc)
    mode = executor_mode(getattr(args, "mode", None))
    print(f"== {now:%Y-%m-%d %H:%M:%S} UTC  {mode} executor{' (dry run)' if args.dry_run else ''}")
    state = _load_state(mode)
    try:
        data = fetch_targets(args.targets_source, args.db, args.kill_file, runner)
    except Exception as e:  # noqa: BLE001 -- no targets means no orders, never a crash loop
        print(f"cannot get targets: {e}")
        return 1
    if not args.dry_run:
        for line in flush_unreported(args.targets_source, args.db, mode, runner):
            print(line)

    slots = [TargetSet.from_dict(s) for s in data.get("slots", []) if s.get("mode") == mode]
    flattens = [r for r in data.get("flatten", []) if r.get("mode") == mode]
    if not slots and not flattens:
        print(f"no {mode} slots and nothing queued for {mode} -- nothing to do")
        return 0

    from live.mt5_broker import MT5Broker, host_label
    report = {"ts": now.isoformat(timespec="seconds"), "host": host_label(), "account": {},
              "executor_mode": mode, "orders": [], "completed_requests": {}, "summary": "",
              "notify": False}
    lines = []
    try:
        broker = broker_factory() if broker_factory else MT5Broker()
    except BrokerError as e:
        msg = f"MT5 {mode} executor on {report['host']}: cannot use MT5 -- {e}"
        print(msg)
        if args.dry_run:
            return 1
        repeat = _suppress_repeat(state, hashlib.sha1(msg.encode()).hexdigest(), now)
        _save_state(state, mode)
        report.update(summary=msg, notify=not repeat)
        if not repeat:
            report["orders"].append({"universe": None, "strategy": None, "timeframe": None,
                                     "mode": None, "symbol": None, "status": "blocked",
                                     "message": str(e)})
        try:
            report_back(args.targets_source, report, args.db, runner)
        except Exception as e2:  # noqa: BLE001
            print(f"could not report ({e2}); saved to {save_unreported(report, mode)} for the next sync")
        return 1

    try:
        try:
            equity, ccy = broker.account()
            report["account"] = {"mode": broker.account_mode(), "equity": equity, "currency": ccy,
                                 "label": getattr(broker, "account_label", lambda: "")()}
        except BrokerError as e:
            lines.append(f"account info unavailable: {e}")

        limits = Limits.from_env()
        blocked_lines = []
        want = "real" if mode == "live" else "demo"
        if report["account"].get("mode") not in (None, want):
            # The wrong terminal for this executor: refuse everything it would do.
            first = slots[0].universe if slots else flattens[0]["universe"]
            out = _blocked(Outcome(), f"MT5 {mode} executor", TargetSet(first, "executor", "", mode,
                                                                        None, "", {}),
                           [f"EXECUTOR_MODE={mode} but the terminal is logged into a "
                            f"{report['account']['mode'].upper()} account "
                            f"({report['account'].get('label')}); point MT5_TERMINAL_PATH / "
                            f"MT5_LOGIN at a {want} account"])
            lines += out.lines
            report["orders"] += out.orders
            blocked_lines += out.lines
            slots, flattens = [], []
        for req in flattens:
            try:
                out = flatten_universe(req["universe"], req["mode"], broker, dry_run=args.dry_run)
            except Exception as e:  # noqa: BLE001 -- report, keep going with other universes
                blocked_lines.append(f"FLATTEN {req['universe']}: unexpected error {type(e).__name__}: {e}")
                lines.append(blocked_lines[-1])
                continue
            lines += out.lines
            report["orders"] += out.orders
            if out.blocked:
                blocked_lines += out.lines
            elif not args.dry_run:
                report["completed_requests"][str(req["id"])] = "; ".join(out.lines)[:500]
        for ts in slots:
            if ts.universe not in EXECUTABLE_UNIVERSES:
                continue
            try:
                out = execute_targets(ts, broker, limits, args.kill_file, dry_run=args.dry_run,
                                      now=now)
            except Exception as e:  # noqa: BLE001
                blocked_lines.append(f"MT5 {ts.universe}: unexpected error {type(e).__name__}: {e}")
                lines.append(blocked_lines[-1])
                continue
            lines += out.lines
            report["orders"] += out.orders
            if out.blocked:
                blocked_lines += out.lines
        notes = getattr(broker, "foreign_positions", lambda: [])()
        lines += [f"  note: {n}" for n in notes]
    finally:
        getattr(broker, "shutdown", lambda: None)()

    placed = [o for o in report["orders"] if o.get("status") in ("filled", "rejected")]
    notify = bool(placed or report["completed_requests"])
    if blocked_lines:
        sig = hashlib.sha1("\n".join(blocked_lines).encode()).hexdigest()
        if _suppress_repeat(state, sig, now):
            report["orders"] = [o for o in report["orders"] if o.get("status") != "blocked"]
        else:
            notify = True
    else:
        state.pop("sig", None)
    summary = "\n".join(lines) or f"MT5 {mode} executor: account already at target"
    print(summary)
    if args.dry_run:
        print("(dry run: no orders sent, nothing reported to the VM)")
        return 0
    _save_state(state, mode)
    report.update(summary=f"[{report['host']}] {summary}", notify=notify)
    try:
        report_back(args.targets_source, report, args.db, runner)
    except Exception as e:  # noqa: BLE001 -- orders already went out; keep the record
        path = save_unreported(report, mode)
        print(f"WARNING: orders were processed but the report failed ({e}); saved to {path} "
              f"and re-sent at the next sync")
        return 1
    return 1 if blocked_lines else 0


def status(args, broker_factory=None, runner=subprocess.run) -> int:
    from live.mt5_broker import MT5Broker
    mode = executor_mode(getattr(args, "mode", None))
    print(f"Executor mode: {mode} (trades {mode} slots only); targets from {args.targets_source}; "
          f"live gate here {'open' if os.environ.get('ALLOW_LIVE_TRADING', '').lower() == 'true' else 'closed'}")
    rc = 0
    try:
        broker = broker_factory() if broker_factory else MT5Broker()
    except BrokerError as e:
        print(f"MT5: {e}")
        broker, rc = None, 1
    if broker is not None:
        try:
            equity, ccy = broker.account()
            kind = "hedging" if getattr(broker, "hedging", lambda: False)() else "netting"
            account_mode = broker.account_mode()
            want = "real" if mode == "live" else "demo"
            warn = "" if account_mode == want else f"  <-- WRONG ACCOUNT TYPE for a {mode} executor"
            print(f"MT5 account {getattr(broker, 'account_label', lambda: '?')()}: "
                  f"{account_mode}, {kind}, equity {equity:,.2f} {ccy}{warn}")
            if warn:
                rc = 1
            held = broker.positions()
            print("Bot positions: " + (", ".join(f"{s} {u:+,.2f}" for s, u in held.items()) or "none"))
            for n in getattr(broker, "foreign_positions", lambda: [])():
                print(f"  note: {n}")
        finally:
            getattr(broker, "shutdown", lambda: None)()
    try:
        data = fetch_targets(args.targets_source, args.db, args.kill_file, runner)
    except Exception as e:  # noqa: BLE001
        print(f"Targets: unavailable ({e})")
        return 1
    print(f"Targets (generated {data.get('generated_at')}, VM kill switch "
          f"{'ON' if data.get('kill') else 'off'}):")
    for s in data.get("slots", []):
        tgt = ", ".join(f"{k} {v['position']:+.2f}" for k, v in s["targets"].items())
        mine = "" if s["mode"] == mode else f"   (not this executor: needs a {s['mode']} executor)"
        print(f"  {s['universe']}: {s['strategy']} {s['timeframe']} [{s['mode']}] "
              f"capital {s['capital']}: {tgt}{mine}")
    for r in data.get("flatten", []):
        print(f"  queued: flatten {r['universe']} [{r['mode']}]")
    if not data.get("slots") and not data.get("flatten"):
        print("  none -- every universe is in signal mode or inactive")
    return rc


def check_costs(args, broker_factory=None) -> int:
    from live.mt5_broker import MT5Broker
    try:
        broker = broker_factory() if broker_factory else MT5Broker()
    except BrokerError as e:
        print(f"MT5: {e}")
        return 1
    try:
        symbols = [i.symbol for u in EXECUTABLE_UNIVERSES for i in universe_instruments(u)]
        print("\n".join(broker.cost_report(symbols)))
    finally:
        getattr(broker, "shutdown", lambda: None)()
    return 0


def smoke_test(args, broker_factory=None) -> int:
    """Exercise the real order path on a DEMO account with the smallest position allowed.

    Buys 2 minimum lots, sells 1 (partial close), sells 2 (flip to short),
    buys 1 (flat), checking the bot's position after every step, that a
    hedging account never holds the bot long and short at once, and that
    positions the bot did not open are untouched. Refuses real accounts,
    whatever ALLOW_LIVE_TRADING says.
    """
    from live.mt5_broker import MT5Broker
    import time

    sym = args.symbol
    try:
        broker = broker_factory() if broker_factory else MT5Broker()
    except BrokerError as e:
        print(f"MT5: {e}")
        return 1
    ok = True
    try:
        if broker.account_mode() != "demo":
            print("Refused: the smoke test only runs on a DEMO account.")
            return 2
        if Path(args.kill_file).exists():
            print(f"Refused: kill switch {args.kill_file} exists.")
            return 2
        name = broker.resolve(sym)
        quote = broker.quotes([sym]).get(sym)
        if quote is None or not quote.tradeable:
            print(f"{sym} ({name}) has no tradeable quote now -- market closed? Try again later.")
            return 1
        if broker.positions().get(sym):
            print(f"Refused: the bot already holds {sym}. Flatten it first.")
            return 2
        mt5 = broker.mt5

        def others():
            return sorted((int(p.ticket), float(p.volume)) for p in mt5.positions_get(symbol=name) or ()
                          if int(p.magic) != broker.magic)

        def bot_sides():
            return {int(p.type) for p in mt5.positions_get(symbol=name) or ()
                    if int(p.magic) == broker.magic}

        info = broker._info(sym)
        lot = broker.normalize_units(sym, float(info.volume_min) * broker.contract(sym) * 1.000001)
        foreign_before = others()
        print(f"Smoke test on {broker.account_label()} ({'hedging' if broker.hedging() else 'netting'}), "
              f"{sym} = {name}, 1 min lot = {lot:,.0f} units, quote {quote.bid} / {quote.ask}")
        steps = [("buy 2 lots", 2 * lot, 2 * lot), ("sell 1 (partial close)", -lot, lot),
                 ("sell 2 (flip short)", -2 * lot, -lot), ("buy 1 (flat)", lot, 0.0)]
        for label, order, expect in steps:
            t0 = time.time()
            fill = broker.market_order(sym, order)
            ms = (time.time() - t0) * 1000
            held = broker.positions().get(sym, 0.0)
            sides = bot_sides()
            good = fill.ok and abs(held - expect) < 1e-6 and len(sides) <= 1
            ok &= good
            price = f" @ {fill.price:,.5g}" if fill.price else ""
            print(f"  {'PASS' if good else 'FAIL'} {label:<24} {fill.message}{price} in {ms:,.0f} ms; "
                  f"bot holds {held:+,.0f} (expected {expect:+,.0f})"
                  f"{'; LONG AND SHORT AT ONCE' if len(sides) > 1 else ''}")
            if not good:
                break
        foreign_after = others()
        same = foreign_before == foreign_after
        ok &= same
        print(f"  {'PASS' if same else 'FAIL'} positions the bot did not open are untouched "
              f"({len(foreign_after)} of them)")
    finally:
        try:
            left = broker.positions().get(sym, 0.0)
            if left:
                fill = broker.market_order(sym, -left)
                print(f"  cleanup: closed {left:+,.0f} -> {fill.message}")
        except BrokerError as e:
            print(f"  cleanup failed, close the bot's {sym} position in MT5 by hand: {e}")
        getattr(broker, "shutdown", lambda: None)()
    print("SMOKE TEST " + ("PASSED" if ok else "FAILED"))
    return 0 if ok else 1


def main() -> int:
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--env-file", default=".env")
    known, _ = pre.parse_known_args()
    load_env(known.env_file)   # before the parser, so .env can set the defaults below

    ap = argparse.ArgumentParser(description="Follow the demo/live targets in MetaTrader 5")
    ap.add_argument("--targets-source", choices=["ssh", "ledger"],
                    default=os.environ.get("TARGETS_SOURCE", "ssh"))
    ap.add_argument("--mode", choices=EXECUTOR_MODES, default=None,
                    help="demo or live slots (default: EXECUTOR_MODE in .env, else demo)")
    ap.add_argument("--db", default="state/ledger.db", help="ledger path for --targets-source ledger")
    ap.add_argument("--env-file", default=".env")
    ap.add_argument("--kill-file", default=DEFAULT_KILL_FILE,
                    help="local kill switch: if this file exists, no orders are sent")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--check-costs", action="store_true")
    ap.add_argument("--smoke-test", action="store_true",
                    help="DEMO accounts only: trade one minimum lot through every order path")
    ap.add_argument("--symbol", default="EUR_USD", help="instrument for --smoke-test")
    args = ap.parse_args()
    try:
        executor_mode(args.mode)
    except ValueError as e:
        print(e)
        return 2
    if args.status:
        return status(args)
    if args.check_costs:
        return check_costs(args)
    if args.smoke_test:
        return smoke_test(args)
    return sync(args)


if __name__ == "__main__":
    sys.exit(main())
