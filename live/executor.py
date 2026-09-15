"""Turn the active strategy's paper positions into instructions or broker orders.

  signal  (default) tell you what to trade. Sent when the followed target for
          an instrument differs from the last instruction, so a switch or a
          position change produces exactly one message per instrument.
  demo    MetaTrader 5 orders on a demo account.
  live    MetaTrader 5 orders on a real-money account.

The work is split between two machines:

  VM  (this module's execute / export_targets, run by the signal job and
      live.control): paper sleeves decide the targets; demo/live slots are
      published as a TargetSet JSON, together with the kill switch and any
      pending flatten requests.
  MT5 host (execute_targets / flatten_universe, run by live.mt5_executor on
      the Windows PC, or on Linux through Wine): fetches the TargetSets and
      trades the MT5 account TO TARGET, then reports back.

Trading to target means the executor reads the account's actual units and
sends only the difference: re-running is harmless, a missed or partial run is
repaired by the next one, and instruments outside the universe (or positions
the bot did not open) are never touched.

Safety checks for demo and live. Any failed check blocks EVERY order in that
run (fail closed) -- clamping would hide the bug that produced the bad target:
  - kill switch absent, on the VM (state/KILL, exported) AND on the MT5 host
  - live only: ALLOW_LIVE_TRADING=true in the MT5 host's environment
  - the MT5 terminal is logged into the right kind of account (demo vs real)
  - capital set, not above the account's equity; account currency USD
  - targets fresh: published within MAX_TARGET_AGE_MIN, bars not stale
  - every instrument has a target in [-1, 1]
  - gross target notional <= capital x MAX_GROSS_LEVERAGE (default 1.0)
  - no single order above 2x an instrument's allocation (a full flip)
  - at most MAX_ORDERS_PER_RUN orders (default 2 per instrument)
Orders smaller than MIN_ORDER_FRACTION (default 2%) of an instrument's
allocation, or below the broker's minimum lot, are skipped; closed markets are
retried on the next run.
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from harness.instruments import INSTRUMENTS
from harness.instruments import universe as universe_instruments
from live.broker import Broker, BrokerError, Fill, Quote
from live.ledger import Ledger
from live.timing import is_stale

EXECUTABLE_UNIVERSES = ("fx", "metals")
SLOT_UNIVERSES = ("crypto", "fx", "metals")
DEFAULT_KILL_FILE = "state/KILL"
DEFAULT_MAX_TARGET_AGE_MIN = 90


@dataclass
class Limits:
    max_gross_leverage: float = 1.0
    max_orders_per_run: int | None = None
    min_order_fraction: float = 0.02
    max_target_age_min: float = DEFAULT_MAX_TARGET_AGE_MIN

    @classmethod
    def from_env(cls) -> "Limits":
        mo = os.environ.get("MAX_ORDERS_PER_RUN")
        return cls(max_gross_leverage=float(os.environ.get("MAX_GROSS_LEVERAGE") or 1.0),
                   max_orders_per_run=int(mo) if mo else None,
                   min_order_fraction=float(os.environ.get("MIN_ORDER_FRACTION") or 0.02),
                   max_target_age_min=float(os.environ.get("MAX_TARGET_AGE_MIN")
                                            or DEFAULT_MAX_TARGET_AGE_MIN))


@dataclass
class OrderPlan:
    symbol: str
    target_pos: float
    price: float
    current_units: float
    target_units: float
    order_units: float
    order_notional: float
    skip: str | None = None


@dataclass
class Outcome:
    lines: list[str] = field(default_factory=list)
    blocked: bool = False
    orders_sent: int = 0
    notify: bool = False   # something the user should see
    orders: list[dict] = field(default_factory=list)   # records for the VM ledger


@dataclass
class TargetSet:
    """What one universe's MT5 account should hold, as published by the VM."""
    universe: str
    strategy: str
    timeframe: str
    mode: str
    capital: float | None
    generated_at: str
    targets: dict[str, dict]   # symbol -> {"position", "price", "bar_ts"}
    kill: bool = False

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "TargetSet":
        return cls(**{k: d[k] for k in cls.__dataclass_fields__ if k in d})


def usd_per_unit(inst, mid: float) -> float:
    """USD value of one unit of the instrument's base (USDT counted as USD)."""
    if inst.base in ("USD", "USDT"):
        return 1.0
    if inst.quote in ("USD", "USDT"):
        return mid
    raise ValueError(f"{inst.symbol}: only instruments quoted against USD are supported")


def _fmt_units(inst, units: float) -> str:
    """FX trades in whole units, gold in ounces, crypto in fractions of a coin."""
    if inst.asset_class == "crypto":
        return f"{units:+,.6f} {inst.base}"
    if inst.asset_class == "metal":
        return f"{units:+,.2f} oz"
    return f"{units:+,.0f} units"


def _qty(units: float) -> str:
    return f"{units:+,.0f}" if abs(units - round(units)) < 1e-9 else f"{units:+,.2f}"


def plan_rebalance(insts, targets: dict[str, float], capital: float,
                   quotes: dict[str, Quote], current: dict[str, float],
                   limits: Limits, normalize=None) -> tuple[list[OrderPlan], list[str]]:
    """Pure sizing and limit checks. Returns (plans, violations).

    normalize(symbol, units) snaps a target to what the broker can hold (lot
    step, minimum lot) BEFORE diffing, so rounding never produces an endless
    stream of tiny corrective orders.
    """
    normalize = normalize or (lambda _sym, u: float(round(u)))
    violations, plans = [], []
    n = len(insts)
    per_capital = capital / n
    gross = 0.0
    for inst in insts:
        sym = inst.symbol
        if sym not in targets:
            violations.append(f"{sym}: no paper target for the active strategy")
            continue
        pos = float(targets[sym])
        if not -1.0 <= pos <= 1.0:
            violations.append(f"{sym}: target {pos:+.3f} outside [-1, 1]")
            continue
        q = quotes.get(sym)
        if q is None:
            violations.append(f"{sym}: no quote from broker")
            continue
        try:
            upu = usd_per_unit(inst, q.mid)
        except ValueError as e:
            violations.append(str(e))
            continue
        raw_units = pos * per_capital / upu
        target_units = normalize(sym, raw_units)
        cur = float(current.get(sym, 0.0))
        order_units = target_units - cur
        notional = abs(order_units) * upu
        gross += abs(target_units) * upu
        plan = OrderPlan(sym, pos, q.mid, cur, target_units, order_units, notional)
        if abs(order_units) < 1e-9:
            plan.skip = ("below broker minimum lot" if target_units == 0 and raw_units != 0
                         and cur == 0 else "at target")
        elif notional < limits.min_order_fraction * per_capital:
            plan.skip = "below minimum order size"
        elif not q.tradeable:
            plan.skip = "market closed -- retry next run"
        elif notional > 2.0 * per_capital * 1.02:
            violations.append(f"{sym}: order ${notional:,.0f} exceeds a full flip "
                              f"(${2 * per_capital:,.0f}) -- unexpected position in the account?")
        plans.append(plan)

    if gross > capital * limits.max_gross_leverage * 1.02:
        violations.append(f"gross target ${gross:,.0f} exceeds capital x leverage "
                          f"${capital * limits.max_gross_leverage:,.0f}")
    max_orders = limits.max_orders_per_run or 2 * n
    sending = sum(1 for p in plans if p.skip is None)
    if sending > max_orders:
        violations.append(f"{sending} orders exceeds MAX_ORDERS_PER_RUN={max_orders}")
    return plans, violations


# ======================================================================= VM side
def _targets(ledger: Ledger, strategy: str, timeframe: str, insts) -> tuple[dict, dict, list]:
    rows = {r["symbol"]: r for r in ledger.sleeves(strategy, timeframe)}
    now = datetime.now(timezone.utc)
    targets, prices, stale = {}, {}, []
    for inst in insts:
        r = rows.get(inst.symbol)
        if r is None:
            continue
        targets[inst.symbol] = float(r["position"])
        prices[inst.symbol] = float(r["price"])
        if is_stale(inst, timeframe, pd.Timestamp(r["bar_ts"]), now):
            stale.append(inst.symbol)
    return targets, prices, stale


def _last_target(ledger: Ledger, universe: str, symbol: str, statuses: tuple) -> float | None:
    marks = ",".join("?" * len(statuses))
    row = ledger.conn.execute(
        f"SELECT target_pos FROM orders WHERE universe=? AND symbol=? AND status IN ({marks}) "
        f"ORDER BY id DESC LIMIT 1", (universe, symbol, *statuses)).fetchone()
    return None if row is None else float(row[0])


def execute(ledger: Ledger, universe: str, dry_run: bool = False,
            force_instructions: bool = False) -> Outcome:
    """VM side: act on the slot for `universe`. Writes to the ledger; the caller commits.

    signal mode    -> trade instructions for you, on every target change
    demo/live mode -> record newly published targets; the MT5 executor trades them
    """
    out = Outcome()
    slot = ledger.slot(universe)
    if slot is None:
        return out
    strategy, timeframe, mode, capital = (slot["strategy"], slot["timeframe"], slot["mode"],
                                          slot["capital"])
    insts = universe_instruments(universe)
    tag = f"ACTIVE {universe}: {strategy} {timeframe} [{mode}]"
    targets, prices, stale = _targets(ledger, strategy, timeframe, insts)
    signal = mode == "signal"
    statuses = ("signal", "filled") if signal else ("published",)

    changes = []
    for inst in insts:
        sym = inst.symbol
        if sym not in targets:
            continue
        last = _last_target(ledger, universe, sym, statuses)
        if not force_instructions:
            held = 0.0 if last is None else last
            if abs(held - targets[sym]) < 1e-9:
                continue   # nothing to trade: unchanged, or flat and never instructed
        units = None
        if capital:
            units = targets[sym] * capital / len(insts) / usd_per_unit(inst, prices[sym])
            if inst.asset_class == "fx":
                units = round(units)
        changes.append((inst, last, targets[sym], units, prices[sym]))
        if not dry_run:
            ledger.add_order(universe, strategy, timeframe, mode, sym, targets[sym], None,
                             units, None, prices[sym], "signal" if signal else "published")
    if changes:
        out.notify = signal
        out.lines.append(f"{tag} -- trade these yourself:" if signal else
                         f"{tag} -- new targets; the MT5 executor trades them at its next sync:")
        for inst, last, tgt, units, price in changes:
            size = f"  hold ~{_fmt_units(inst, units)}" if units is not None else ""
            frm = "new" if last is None else f"{last:+.2f}"
            out.lines.append(f"  {inst.symbol:<9} {frm} -> {tgt:+.2f} of allocation{size} "
                             f"(ref price {price:,.5g})")
    if stale:
        out.lines.append(f"  warning: stale data for {', '.join(stale)}")
    return out


def targets_from_ledger(ledger: Ledger, universe: str,
                        kill_file: str = DEFAULT_KILL_FILE) -> TargetSet | None:
    """The TargetSet for a demo/live slot, or None for signal-only or inactive universes."""
    slot = ledger.slot(universe)
    if slot is None or slot["mode"] not in ("demo", "live"):
        return None
    rows = {r["symbol"]: r for r in ledger.sleeves(slot["strategy"], slot["timeframe"])}
    targets = {i.symbol: {"position": float(rows[i.symbol]["position"]),
                          "price": float(rows[i.symbol]["price"]),
                          "bar_ts": rows[i.symbol]["bar_ts"]}
               for i in universe_instruments(universe) if i.symbol in rows}
    return TargetSet(universe, slot["strategy"], slot["timeframe"], slot["mode"],
                     slot["capital"], datetime.now(timezone.utc).isoformat(timespec="seconds"),
                     targets, kill=Path(kill_file).exists())


def export_targets(ledger: Ledger, kill_file: str = DEFAULT_KILL_FILE) -> dict:
    """Everything the MT5 executor needs from the VM, as one JSON-able dict."""
    sets = [targets_from_ledger(ledger, u, kill_file) for u in EXECUTABLE_UNIVERSES]
    return {"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "kill": Path(kill_file).exists(),
            "slots": [s.to_dict() for s in sets if s is not None],
            "flatten": [dict(r) for r in ledger.pending_requests("flatten")]}


# ================================================================== MT5 host side
def _record(ts: TargetSet, symbol, target_pos, current, target_units, order_units, price,
            status, ref=None, message=None, strategy=None) -> dict:
    return {"universe": ts.universe, "strategy": strategy or ts.strategy,
            "timeframe": ts.timeframe, "mode": ts.mode, "symbol": symbol,
            "target_pos": target_pos, "current_units": current, "target_units": target_units,
            "order_units": order_units, "price": price, "status": status, "broker_ref": ref,
            "message": message}


def _live_gate_open() -> bool:
    return os.environ.get("ALLOW_LIVE_TRADING", "").lower() == "true"


def execute_targets(ts: TargetSet, broker: Broker, limits: Limits | None = None,
                    local_kill_file: str = DEFAULT_KILL_FILE, dry_run: bool = False,
                    now: datetime | None = None) -> Outcome:
    """MT5 host side: trade the connected account to one TargetSet. Never touches a ledger."""
    out = Outcome()
    limits = limits or Limits.from_env()
    now = now or datetime.now(timezone.utc)
    tag = f"MT5 {ts.universe}: {ts.strategy} {ts.timeframe} [{ts.mode}]"
    insts = universe_instruments(ts.universe) if ts.universe in SLOT_UNIVERSES else []

    violations = []
    if ts.universe not in EXECUTABLE_UNIVERSES:
        violations.append(f"{ts.universe} cannot be auto-traded (signal-only universe)")
    if ts.kill:
        violations.append("kill switch is on at the VM -- no orders")
    if Path(local_kill_file).exists():
        violations.append(f"kill switch is on at this machine ({local_kill_file}) -- no orders")
    if ts.mode not in ("demo", "live"):
        violations.append(f"mode {ts.mode!r} is not tradeable")
    if ts.mode == "live" and not _live_gate_open():
        violations.append("ALLOW_LIVE_TRADING is not 'true' in this machine's .env "
                          "-- live orders refused")
    if not ts.capital or ts.capital <= 0:
        violations.append("no capital set for this slot")
    age_min = (now - pd.Timestamp(ts.generated_at).to_pydatetime()).total_seconds() / 60
    if age_min > limits.max_target_age_min:
        violations.append(f"targets are {age_min:.0f} min old (limit {limits.max_target_age_min:.0f})")
    stale = [s for s, t in ts.targets.items()
             if s in INSTRUMENTS and is_stale(INSTRUMENTS[s], ts.timeframe,
                                              pd.Timestamp(t["bar_ts"]), now)]
    if stale:
        violations.append(f"stale paper data for {', '.join(stale)} -- targets may be out of date")
    if violations:
        return _blocked(out, tag, ts, violations)

    try:
        account_mode = broker.account_mode()
        want = "real" if ts.mode == "live" else "demo"
        if account_mode != want:
            violations.append(f"MT5 is logged into a {account_mode.upper()} account but this "
                              f"slot is {ts.mode} -- log the terminal into a {want} account")
        equity, ccy = broker.account()
        if ccy != "USD":
            violations.append(f"account currency is {ccy}; only USD accounts are supported")
        if ts.capital > equity:
            violations.append(f"slot capital ${ts.capital:,.0f} exceeds account equity ${equity:,.0f}")
        if violations:
            return _blocked(out, tag, ts, violations)
        quotes = broker.quotes([i.symbol for i in insts])
        current = broker.positions()
    except BrokerError as e:
        return _blocked(out, tag, ts, violations + [str(e)])

    targets = {s: t["position"] for s, t in ts.targets.items()}
    try:
        plans, plan_violations = plan_rebalance(insts, targets, ts.capital, quotes, current,
                                                limits, normalize=broker.normalize_units)
    except BrokerError as e:
        return _blocked(out, tag, ts, [str(e)])
    if plan_violations:
        return _blocked(out, tag, ts, plan_violations)

    for p in plans:
        if p.skip:
            if p.skip != "at target":
                out.lines.append(f"  {p.symbol}: {p.skip}")
            continue
        if dry_run:
            out.lines.append(f"  would trade {p.symbol} {_qty(p.order_units)} units "
                             f"(~${p.order_notional:,.0f}) -> hold {_qty(p.target_units)}")
            continue
        try:
            fill = broker.market_order(p.symbol, p.order_units)
        except BrokerError as e:   # record it and let the next sync repair the position
            fill = Fill(False, 0.0, None, None, f"broker error: {e}")
        status = "filled" if fill.ok else "rejected"
        out.orders.append(_record(ts, p.symbol, p.target_pos, p.current_units, p.target_units,
                                  p.order_units, fill.price or p.price, status, fill.ref,
                                  fill.message))
        out.orders_sent += 1
        out.notify = True
        out.lines.append(f"  {p.symbol:<9} {_qty(p.order_units)} units -> {status}"
                         f"{'' if fill.ok else ': ' + fill.message}"
                         f"{f' @ {fill.price:,.5g}' if fill.ok and fill.price else ''}")
    if out.lines:
        out.lines.insert(0, f"{tag} (equity ${equity:,.2f}, {account_mode} account):")
    return out


def _blocked(out: Outcome, tag: str, ts: TargetSet, violations: list[str]) -> Outcome:
    out.blocked = True
    out.notify = True
    out.lines.append(f"{tag} -- NO ORDERS SENT, safety check failed:")
    out.lines += [f"  - {v}" for v in violations]
    out.orders.append(_record(ts, None, None, None, None, None, None, "blocked",
                              message="; ".join(violations)))
    return out


def flatten_universe(universe: str, mode: str, broker: Broker,
                     dry_run: bool = False) -> Outcome:
    """Close every position the bot holds in the universe's instruments.

    Ignores the kill switch -- closing positions is what the kill switch is
    for -- but still honours the live gate and the account type.
    """
    out = Outcome()
    ts = TargetSet(universe, "flatten", "", mode, None, "", {})
    if universe not in EXECUTABLE_UNIVERSES or mode not in ("demo", "live"):
        out.lines.append(f"{universe} [{mode}]: nothing to flatten")
        return out
    if mode == "live" and not _live_gate_open():
        return _blocked(out, f"FLATTEN {universe} [live]", ts,
                        ["ALLOW_LIVE_TRADING is not 'true' -- close live positions in MT5 by hand"])
    try:
        want = "real" if mode == "live" else "demo"
        if broker.account_mode() != want:
            return _blocked(out, f"FLATTEN {universe} [{mode}]", ts,
                            [f"MT5 is not logged into a {want} account"])
        held = broker.positions()
    except BrokerError as e:
        return _blocked(out, f"FLATTEN {universe} [{mode}]", ts, [str(e)])
    for inst in universe_instruments(universe):
        units = float(held.get(inst.symbol, 0.0))
        if not units:
            continue
        if dry_run:
            out.lines.append(f"  would close {inst.symbol} {_qty(-units)}")
            continue
        try:
            fill = broker.market_order(inst.symbol, -units)
        except BrokerError as e:
            fill = Fill(False, 0.0, None, None, f"broker error: {e}")
        out.orders.append(_record(ts, inst.symbol, 0.0, units, 0.0, -units, fill.price,
                                  "filled" if fill.ok else "rejected", fill.ref, fill.message,
                                  strategy="flatten"))
        out.notify = True
        out.lines.append(f"  close {inst.symbol} {_qty(-units)} -> "
                         f"{'filled' if fill.ok else fill.message}")
    if not out.lines:
        out.lines.append(f"{universe} [{mode}]: no open bot positions")
    else:
        out.lines.insert(0, f"FLATTEN {universe} [{mode}]:")
    return out
