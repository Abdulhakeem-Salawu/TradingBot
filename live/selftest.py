#!/usr/bin/env python3
"""Self-test for switching and execution. No network, no MetaTrader, no real money.

    python -m live.selftest

Run it before switching any universe to demo or live, and after every change
to live/. It drives the executor's sizing and safety checks, the comparison
report, the real MT5 adapter against a fake MetaTrader5 module (netting and
hedging accounts, symbol suffixes, lot rounding, order results), and a full
VM <-> MT5-machine round trip with a fake ssh.
"""

from __future__ import annotations

import fnmatch
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd

from harness.instruments import universe
from live.broker import BrokerError, Fill, Quote
from live.compare import compare_universe, followed_daily, switch_threshold
from live.executor import (Limits, TargetSet, execute, execute_targets, export_targets,
                           flatten_universe, plan_rebalance, targets_from_ledger)
from live.ledger import Ledger

FAILS: list[str] = []
ROOT = Path(__file__).resolve().parent.parent


def check(cond: bool, msg: str) -> None:
    print(("  PASS " if cond else "  FAIL ") + msg)
    if not cond:
        FAILS.append(msg)


PRICES = {"EUR_USD": 1.10, "USD_JPY": 150.0, "GBP_USD": 1.30, "USD_CHF": 0.90,
          "AUD_USD": 0.65, "USD_CAD": 1.35, "NZD_USD": 0.60, "XAU_USD": 3000.0,
          "BTCUSDT": 78_000.0, "ETHUSDT": 2_500.0}


class FakeBroker:
    """Unit-level broker: whole units, no lots."""

    def __init__(self, equity=100_000.0, ccy="USD", tradeable=True, held=None, mode="demo"):
        self.equity, self.ccy, self.tradeable, self.mode = equity, ccy, tradeable, mode
        self.held = dict(held or {})
        self.orders: list[tuple[str, float]] = []

    def account(self):
        return self.equity, self.ccy

    def account_mode(self):
        return self.mode

    def positions(self):
        return {k: v for k, v in self.held.items() if v}

    def quotes(self, symbols):
        return {s: Quote(PRICES[s] * 0.9999, PRICES[s] * 1.0001, self.tradeable) for s in symbols}

    def normalize_units(self, symbol, units):
        return float(round(units))

    def market_order(self, symbol, units):
        self.orders.append((symbol, units))
        self.held[symbol] = self.held.get(symbol, 0) + units
        return Fill(True, units, PRICES[symbol], str(len(self.orders)), "filled")


def seed_sleeve(ledger: Ledger, strategy: str, timeframe: str, uni: str, targets: dict,
                returns: np.ndarray | None = None, bar_age=timedelta(minutes=30)) -> None:
    """Write a paper sleeve as the signal job would: bars, trades, current position."""
    step = timedelta(days=1) if timeframe == "1d" else timedelta(hours=1)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    last_start = pd.Timestamp(now - bar_age - step)
    for inst in universe(uni):
        n = 0 if returns is None else len(returns)
        eq = 1.0
        for k in range(n):
            ts = last_start - step * (n - 1 - k)
            eq *= 1 + returns[k]
            ledger.add_bar(strategy, timeframe, inst.symbol, ts.isoformat(), returns[k], 0.0,
                           0.0, returns[k], eq)
        pos = targets.get(inst.symbol, 0.0)
        started = (last_start - step * max(n - 1, 0)).isoformat()
        ledger.conn.execute(
            "INSERT INTO trades (strategy, timeframe, symbol, bar_ts, from_pos, to_pos, price, "
            "cost, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (strategy, timeframe, inst.symbol, started, 0.0, pos, 1.0, 0.0,
             (now - timedelta(days=400)).isoformat()))
        ledger.set_position(strategy, timeframe, inst.symbol, pos, PRICES.get(inst.symbol, 100.0),
                            last_start.isoformat(), eq, started)
    ledger.commit()


def target_set(targets: dict, mode="demo", capital=70_000.0, uni="fx", age_min=1, bar_age_h=0.5,
               timeframe="1d", kill=False) -> TargetSet:
    now = datetime.now(timezone.utc)
    step = timedelta(days=1) if timeframe == "1d" else timedelta(hours=1)
    bar = (now - timedelta(hours=bar_age_h) - step).isoformat()
    return TargetSet(uni, "tsmom", timeframe, mode, capital,
                     (now - timedelta(minutes=age_min)).isoformat(),
                     {s: {"position": p, "price": PRICES[s], "bar_ts": bar} for s, p in targets.items()},
                     kill=kill)


# --------------------------------------------------------------- fake MetaTrader5
class FakeMT5:
    ACCOUNT_TRADE_MODE_DEMO, ACCOUNT_TRADE_MODE_REAL = 0, 2
    ACCOUNT_MARGIN_MODE_RETAIL_NETTING, ACCOUNT_MARGIN_MODE_RETAIL_HEDGING = 0, 2
    SYMBOL_TRADE_MODE_FULL = 4
    ORDER_FILLING_FOK, ORDER_FILLING_IOC, ORDER_FILLING_RETURN = 0, 1, 2
    TRADE_ACTION_DEAL, ORDER_TYPE_BUY, ORDER_TYPE_SELL, ORDER_TIME_GTC = 1, 0, 1, 0
    POSITION_TYPE_BUY, POSITION_TYPE_SELL = 0, 1
    TIMEFRAME_H1 = 16385

    def __init__(self, names=None, hedging=True, real=False, equity=100_000.0, currency="USD",
                 trade_allowed=True, init_ok=True, filling=1, login=123):
        self.hedging, self.real, self.equity, self.currency = hedging, real, equity, currency
        self.trade_allowed, self.init_ok, self.login = trade_allowed, init_ok, login
        self.rates = None            # structured array for copy_rates_range
        self.server_time = None      # newest tick time, server seconds
        self.fill_rejects: set[int] = set()   # filling modes the "server" refuses (10030)
        self.profit_scale = {}       # symbol -> factor applied by order_calc_profit
        names = names or {"EUR_USD": "EURUSDm", "USD_JPY": "USDJPYm", "GBP_USD": "GBPUSDm",
                          "USD_CHF": "USDCHFm", "AUD_USD": "AUDUSDm", "USD_CAD": "USDCADm",
                          "NZD_USD": "NZDUSDm", "XAU_USD": "XAUUSDm"}
        self.syms = {}
        for canon, name in names.items():
            gold = canon == "XAU_USD"
            base, quote = canon.split("_")
            self.syms[name] = SimpleNamespace(
                name=name, trade_contract_size=100.0 if gold else 100_000.0, volume_step=0.01,
                volume_min=0.01, trade_mode=4, filling_mode=filling, swap_long=-5.0,
                swap_short=1.2, swap_mode=1, price=PRICES[canon], point=0.00001,
                currency_base=base, currency_profit=quote)
        self.positions: list[SimpleNamespace] = []
        self.requests: list[dict] = []
        self.next_retcodes: list[int] = []
        self.ticket = 1000
        self.maxbars = 100_000
        self.loading_answers = 0       # copy_rates_range calls that return no bars first
        self.offline_polls = 0         # terminal_info calls that report "not connected" first

    def initialize(self, **kw):
        return self.init_ok

    def shutdown(self):
        pass

    def last_error(self):
        return (-1, "fake error")

    def terminal_info(self):
        connected = self.offline_polls <= 0
        self.offline_polls -= 1
        return SimpleNamespace(trade_allowed=self.trade_allowed, connected=connected, maxbars=self.maxbars)

    def account_info(self):
        return SimpleNamespace(equity=self.equity, currency=self.currency, login=self.login,
                               server="Fake-Demo", trade_mode=2 if self.real else 0,
                               margin_mode=2 if self.hedging else 0)

    def order_calc_profit(self, kind, name, lots, price_open, price_close):
        s = self.syms[name]
        profit_quote = lots * s.trade_contract_size * (price_close - price_open)
        profit = profit_quote if s.currency_profit == "USD" else profit_quote / s.price
        return profit * self.profit_scale.get(name, 1.0)

    def copy_rates_range(self, name, timeframe, start, end):
        self.rates_asked = (start, end)
        if self.loading_answers:        # a terminal still downloading the symbol's bars
            self.loading_answers -= 1
            return self.rates[:0]
        return self.rates

    def symbol_info(self, name):
        return self.syms.get(name)

    def symbols_get(self, pattern):
        return [s for n, s in self.syms.items() if fnmatch.fnmatch(n, pattern)]

    def symbol_select(self, name, enable):
        return name in self.syms

    def symbol_info_tick(self, name):
        p = self.syms[name].price
        return SimpleNamespace(bid=p * 0.9999, ask=p * 1.0001, time=self.server_time or 0)

    def positions_get(self, symbol=None):
        return [p for p in self.positions if symbol is None or p.symbol == symbol]

    def add_position(self, name, lots, buy, magic):
        self.ticket += 1
        self.positions.append(SimpleNamespace(ticket=self.ticket, symbol=name, volume=lots,
                                              type=0 if buy else 1, magic=magic))

    def order_send(self, req):
        self.requests.append(dict(req))
        if req.get("type_filling") in self.fill_rejects:
            return SimpleNamespace(retcode=10030, comment="Unsupported filling mode", order=0,
                                   volume=0, price=0)
        code = self.next_retcodes.pop(0) if self.next_retcodes else 10009
        if code not in (10009, 10008):
            return SimpleNamespace(retcode=code, comment="rejected by fake", order=0, volume=0, price=0)
        name, vol, buy = req["symbol"], req["volume"], req["type"] == 0
        if "position" in req:
            pos = next(p for p in self.positions if p.ticket == req["position"])
            pos.volume = round(pos.volume - vol, 8)
            if pos.volume <= 1e-9:
                self.positions.remove(pos)
        elif self.hedging:
            self.add_position(name, vol, buy, req["magic"])
        else:
            net = sum(p.volume * (1 if p.type == 0 else -1) for p in self.positions if p.symbol == name)
            net = round(net + (vol if buy else -vol), 8)
            self.positions = [p for p in self.positions if p.symbol != name]
            if net:
                self.add_position(name, abs(net), net > 0, req["magic"])
        self.ticket += 1
        return SimpleNamespace(retcode=code, comment="done", order=self.ticket, volume=vol,
                               price=req["price"])


def main() -> int:
    tmp = Path(tempfile.mkdtemp())
    kill = str(tmp / "KILL")
    saved_env = {k: os.environ.get(k) for k in ("ALLOW_LIVE_TRADING", "MT5_SYMBOLS", "MT5_LOGIN",
                                                "MT5_PASSWORD", "MT5_SERVER", "MT5_TERMINAL_PATH",
                                                "MT5_BACKEND", "MT5_RPYC_HOST", "VM_SSH_TARGET",
                                                "EXECUTOR_MODE", "MT5_SERVER_TZ", "FX_DATA_SOURCE",
                                                "MT5_HISTORY_YEARS", "TARGETS_SOURCE")}
    for k in saved_env:
        os.environ.pop(k, None)
    limits = Limits()
    try:
        from live.mt5_broker import MT5Broker, load_backend

        fx = universe("fx")
        fx_targets = {"EUR_USD": 0.5, "USD_JPY": -1.0, "GBP_USD": 0.0, "USD_CHF": 0.33,
                      "AUD_USD": -0.33, "USD_CAD": 1.0, "NZD_USD": 0.0}

        print("[1] sizing and limits (pure)")
        b = FakeBroker()
        q = b.quotes([i.symbol for i in fx])
        plans, v = plan_rebalance(fx, fx_targets, 70_000, q, {}, limits)
        by = {p.symbol: p for p in plans}
        check(not v, f"no violations for a normal book ({v})")
        check(by["EUR_USD"].target_units == round(0.5 * 10_000 / 1.10), "EUR_USD units = pos x allocation / price")
        check(by["USD_JPY"].target_units == -10_000, "USD_JPY (USD base) units = pos x allocation")
        check(by["GBP_USD"].skip == "at target", "flat target with no position sends nothing")
        _, v = plan_rebalance(fx, {**fx_targets, "EUR_USD": 1.5}, 70_000, q, {}, limits)
        check(any("outside" in x for x in v), "target outside [-1, 1] is a violation")
        _, v = plan_rebalance(fx, fx_targets, 70_000, q, {"USD_JPY": 50_000}, limits)
        check(any("full flip" in x for x in v), "an unexpected large position blocks orders")
        _, v = plan_rebalance(fx, {k: x for k, x in fx_targets.items() if k != "NZD_USD"}, 70_000, q, {}, limits)
        check(any("no paper target" in x for x in v), "missing sleeve is a violation")
        _, v = plan_rebalance(fx, fx_targets, 70_000, q, {}, Limits(max_orders_per_run=2))
        check(any("MAX_ORDERS_PER_RUN" in x for x in v), "order count limit")
        plans, _ = plan_rebalance(fx, fx_targets, 70_000, q, {}, limits,
                                  normalize=lambda s, u: float(int(u / 1000) * 1000))
        check({p.symbol: p for p in plans}["EUR_USD"].target_units == 4000,
              "broker normalization applied before diffing")

        print("\n[2] execute_targets trades to target and is idempotent")
        b = FakeBroker(held={"SPX500_USD": 3})
        ts = target_set(fx_targets)
        out = execute_targets(ts, b, limits, kill)
        check(not out.blocked and out.orders_sent == 5, f"5 orders sent (got {out.orders_sent})")
        check(b.held["EUR_USD"] == round(5_000 / 1.10), "EUR_USD reached target")
        check(len(out.orders) == 5 and all(o["status"] == "filled" for o in out.orders),
              "order records produced for the VM")
        out2 = execute_targets(ts, b, limits, kill)
        check(out2.orders_sent == 0, "second run sends nothing (trades to target, not deltas)")
        check(b.held["SPX500_USD"] == 3, "positions outside the universe are never touched")
        b.held["EUR_USD"] -= 1000
        out3 = execute_targets(ts, b, limits, kill)
        check(out3.orders_sent == 1 and b.held["EUR_USD"] == round(5_000 / 1.10),
              "a partial fill is repaired on the next run")

        print("\n[3] safety checks block every order")
        def blocked(ts_, broker, why):
            out = execute_targets(ts_, broker, limits, kill)
            ok = out.blocked and not broker.orders and any(why in x for x in out.lines)
            check(ok, f"{why}" + ("" if ok else f" -- got {out.lines}"))
        blocked(target_set(fx_targets, kill=True), FakeBroker(), "kill switch is on at the VM")
        Path(kill).write_text("x")
        blocked(target_set(fx_targets), FakeBroker(), "kill switch is on at this machine")
        Path(kill).unlink()
        blocked(target_set(fx_targets, mode="live"), FakeBroker(mode="real"), "ALLOW_LIVE_TRADING")
        os.environ["ALLOW_LIVE_TRADING"] = "true"
        b = FakeBroker(mode="real")
        out = execute_targets(target_set(fx_targets, mode="live"), b, limits, kill)
        check(not out.blocked and b.orders, "live proceeds once the gate is open on a real account")
        blocked(target_set(fx_targets, mode="live"), FakeBroker(mode="demo"), "logged into a DEMO account")
        os.environ.pop("ALLOW_LIVE_TRADING")
        blocked(target_set(fx_targets, mode="demo"), FakeBroker(mode="real"), "logged into a REAL account")
        blocked(target_set(fx_targets), FakeBroker(equity=5_000), "exceeds account equity")
        blocked(target_set(fx_targets), FakeBroker(ccy="EUR"), "only USD accounts")
        blocked(target_set(fx_targets, age_min=200), FakeBroker(), "min old")
        blocked(target_set(fx_targets, bar_age_h=24 * 6), FakeBroker(), "stale paper data")
        blocked(target_set({"BTCUSDT": 0.1}, uni="crypto"), FakeBroker(), "cannot be auto-traded")
        b = FakeBroker(tradeable=False)
        out = execute_targets(target_set(fx_targets), b, limits, kill)
        check(not out.blocked and not b.orders and any("market closed" in x for x in out.lines),
              "closed market skips without blocking")

        print("\n[4] VM: signal instructions and published targets, once per change")
        led = Ledger(str(tmp / "sig.db"))
        seed_sleeve(led, "tsmom", "1d", "crypto", {"BTCUSDT": 0.08, "ETHUSDT": 0.0})
        led.set_slot("crypto", "tsmom", "1d", "signal", 6_000, "test")
        out = execute(led, "crypto")
        led.commit()
        check(out.notify and any("BTCUSDT" in x and "BTC" in x for x in out.lines),
              "signal mode instructs, with fractional coin size")
        check(not execute(led, "crypto").lines, "unchanged targets send nothing")
        led.conn.execute("UPDATE positions SET position=0.1 WHERE symbol='BTCUSDT'")
        out = execute(led, "crypto")
        check(sum("BTCUSDT" in x for x in out.lines) == 1 and not any("ETHUSDT" in x for x in out.lines),
              "a target change instructs only that instrument")
        seed_sleeve(led, "tsmom", "1d", "fx", fx_targets)
        led.set_slot("fx", "tsmom", "1d", "demo", 70_000, "test")
        out = execute(led, "fx")
        led.commit()
        check(any("MT5 executor" in x for x in out.lines) and not out.notify,
              "demo slot publishes targets for the executor (no Telegram spam)")
        ts = targets_from_ledger(led, "fx", kill)
        check(ts is not None and ts.targets["USD_JPY"]["position"] == -1.0 and ts.capital == 70_000,
              "TargetSet built from the ledger")
        check(targets_from_ledger(led, "crypto", kill) is None, "signal-mode slots are not exported")
        led.add_request("fx", "flatten", "demo")
        led.commit()
        data = export_targets(led, kill)
        check(len(data["slots"]) == 1 and len(data["flatten"]) == 1 and data["kill"] is False,
              "export carries slots, queued flattens and the kill switch")
        check(TargetSet.from_dict(data["slots"][0]).to_dict() == data["slots"][0], "TargetSet JSON round trip")

        print("\n[5] flatten closes only the bot's positions in the universe")
        b = FakeBroker(held={"EUR_USD": 4000, "USD_JPY": -2000, "SPX500_USD": 3})
        out = flatten_universe("fx", "demo", b)
        check(b.held["EUR_USD"] == 0 and b.held["USD_JPY"] == 0 and b.held["SPX500_USD"] == 3,
              "universe closed, other positions untouched")
        out = flatten_universe("fx", "live", FakeBroker(mode="real", held={"EUR_USD": 1}))
        check(out.blocked, "flattening live needs the live gate")
        out = flatten_universe("fx", "demo", FakeBroker(mode="real", held={"EUR_USD": 1}))
        check(out.blocked, "flattening demo refuses a real account")

        print("\n[6] comparison report and switch accounting")
        led4 = Ledger(str(tmp / "cmp.db"))
        rng = np.random.default_rng(0)
        seed_sleeve(led4, "tsmom", "1d", "fx", {"EUR_USD": 1.0}, rng.normal(0.001, 0.004, 120))
        seed_sleeve(led4, "carry", "1d", "fx", {"EUR_USD": -1.0}, rng.normal(0.0, 0.004, 90))
        seed_sleeve(led4, "tsmom", "1h", "fx", {"EUR_USD": 0.5}, rng.normal(0.00002, 0.001, 24 * 60))
        led4.set_slot("fx", "tsmom", "1d", "signal", None, "start")
        led4.set_slot("fx", "carry", "1d", "signal", None, "switch test")
        led4.commit()
        lines = compare_universe(led4, "fx")
        text = "\n".join(lines)
        print("\n".join("      " + x for x in lines))
        check("common window" in text and "tsmom 1h" in text and "carry 1d" in text,
              "all three strategies compared on a common window")
        check(any(x.startswith("* carry 1d") for x in lines), "active strategy marked")
        check(switch_threshold(1) == 2.0 and 2.5 < switch_threshold(5) < 2.7,
              f"switch bar rises with alternatives (1: {switch_threshold(1):.2f}, 5: {switch_threshold(5):.2f})")
        daily, n_sw, cost = followed_daily(led4, "fx")
        per_side = universe("fx")[0].costs.per_side
        check(n_sw == 2 and np.isclose(cost, per_side / 7 + 2 * per_side / 7),
              f"switch costs charged: entry + tsmom->carry flip ({cost:.5%})")

        print("\n[7] MT5 adapter against a fake MetaTrader5 module")
        m = FakeMT5(hedging=True)
        br = MT5Broker(mt5=m, magic=777, symbol_map={})
        check(br.resolve("EUR_USD") == "EURUSDm", "broker suffix resolved (EURUSD -> EURUSDm)")
        br2 = MT5Broker(mt5=FakeMT5(names={"EUR_USD": "EURUSD.a", "USD_JPY": "USDJPY"}), magic=1,
                        symbol_map={"EUR_USD": "EURUSD.a"})
        check(br2.resolve("EUR_USD") == "EURUSD.a" and br2.resolve("USD_JPY") == "USDJPY",
              "MT5_SYMBOLS override and exact names")
        amb = FakeMT5(names={"EUR_USD": "EURUSDm"})
        amb.syms["EURUSD.pro"] = SimpleNamespace(**{**vars(amb.syms["EURUSDm"]), "name": "EURUSD.pro"})
        try:
            MT5Broker(mt5=amb, magic=1, symbol_map={}).resolve("EUR_USD")
            check(False, "ambiguous symbols must be refused")
        except BrokerError as e:
            check("several broker symbols" in str(e), "ambiguous symbols refused with a fix hint")
        check(br.normalize_units("EUR_USD", 4545.45) == 4000.0, "units floored to the 0.01-lot step (1,000 EUR)")
        check(br.normalize_units("EUR_USD", -999) == 0.0, "below the minimum lot -> 0")
        check(br.normalize_units("XAU_USD", 3.7) == 3.0, "gold: 100 oz contracts, 0.01 lot = 1 oz")
        check(br.account_mode() == "demo" and MT5Broker(mt5=FakeMT5(real=True), magic=1,
                                                         symbol_map={}).account_mode() == "real",
              "account type detected")
        m.add_position("EURUSDm", 0.05, True, magic=999)       # a manual trade
        f = br.market_order("EUR_USD", 3000)
        check(f.ok and br.positions().get("EUR_USD") == 3000.0, "buy opens a bot position (0.03 lots)")
        check(m.requests[-1]["type_filling"] == 0 and m.requests[-1]["magic"] == 777,
              "FOK filling and the bot's magic number on orders")
        f = br.market_order("EUR_USD", -5000)
        reqs = m.requests[-2:]
        check(f.ok and "position" in reqs[0] and reqs[0]["volume"] == 0.03 and "position" not in reqs[1]
              and reqs[1]["volume"] == 0.02,
              "hedging flip: closes the bot's ticket, then opens the remainder")
        check(br.positions().get("EUR_USD") == -2000.0 and len([p for p in m.positions if p.magic == 777]) == 1,
              "no hedge left behind; manual position ignored")
        check(any("not the bot's" in n for n in br.foreign_positions()), "manual position reported")
        n = FakeMT5(hedging=False)
        brn = MT5Broker(mt5=n, magic=5, symbol_map={})
        brn.market_order("USD_JPY", 20_000)
        brn.market_order("USD_JPY", -30_000)
        check(brn.positions().get("USD_JPY") == -10_000.0 and "position" not in n.requests[-1],
              "netting account: one opposite deal nets the position")
        m.next_retcodes = [10018]
        f = br.market_order("GBP_USD", 1000)
        check(not f.ok and "market closed" in f.message, "market-closed retcode mapped")
        m.next_retcodes = [10019]
        f = br.market_order("GBP_USD", 1000)
        check(not f.ok and "10019" in f.message, "other rejections reported with their retcode")
        check(MT5Broker(mt5=FakeMT5(filling=2), magic=1, symbol_map={})._filling(
            FakeMT5(filling=2).syms["EURUSDm"]) == 1, "IOC used when FOK is not allowed")
        for bad, why in ((FakeMT5(trade_allowed=False), "Algo Trading"), (FakeMT5(init_ok=False), "initialize")):
            try:
                MT5Broker(mt5=bad, magic=1, symbol_map={})
                check(False, why)
            except BrokerError as e:
                check(why in str(e), f"refuses to run when {why} fails/disabled")
        os.environ.update(MT5_BACKEND="wine", MT5_RPYC_HOST="0.0.0.0")
        try:
            load_backend()
            check(False, "RPyC on a public interface must be refused")
        except BrokerError as e:
            check("refused" in str(e), "wine bridge backend refuses a non-localhost host")
        os.environ.pop("MT5_BACKEND")
        os.environ.pop("MT5_RPYC_HOST")
        f7 = FakeMT5(filling=3)
        f7.fill_rejects = {0}
        b7 = MT5Broker(mt5=f7, magic=1, symbol_map={})
        f = b7.market_order("EUR_USD", 1000)
        check(f.ok and [r["type_filling"] for r in f7.requests] == [0, 1],
              "retcode 10030 (unsupported filling) retries with the next filling mode")
        b7.market_order("EUR_USD", 1000)
        check(f7.requests[-1]["type_filling"] == 1, "the filling mode that worked is remembered")
        cent = FakeMT5()
        cent.profit_scale["EURUSDm"] = 0.01       # P&L says 1,000 units per lot, spec says 100,000
        try:
            MT5Broker(mt5=cent, magic=1, symbol_map={}).normalize_units("EUR_USD", 50_000)
            check(False, "a contract size the profit calculation contradicts must be refused")
        except BrokerError as e:
            check("profit calculation implies" in str(e), "contract size cross-checked with order_calc_profit")
        check(MT5Broker(mt5=FakeMT5(), magic=1, symbol_map={}).normalize_units("USD_JPY", 25_000) == 25_000,
              "USD-base contract check passes (profit converted from JPY)")
        from live.mt5_broker import initialize_kwargs
        check(initialize_kwargs({"MT5_LOGIN": "5", "MT5_TERMINAL_PATH": "C:/mt5/terminal64.exe"})
              == {"path": "C:/mt5/terminal64.exe"},
              "MT5_LOGIN without a password never logs the terminal into another account")
        check(initialize_kwargs({"MT5_LOGIN": "5", "MT5_PASSWORD": "x", "MT5_SERVER": "Exness-MT5Real"})
              == {"login": 5, "password": "x", "server": "Exness-MT5Real"},
              "explicit login when a password is given")
        os.environ["MT5_LOGIN"] = "999"
        try:
            MT5Broker(mt5=FakeMT5(login=123), magic=1, symbol_map={})
            check(False, "MT5_LOGIN pin must refuse another account")
        except BrokerError as e:
            check("wrong account" in str(e), "MT5_LOGIN pins the account number")
        os.environ["MT5_LOGIN"] = "123"
        check(MT5Broker(mt5=FakeMT5(login=123), magic=1, symbol_map={}).account_label() == "123@Fake-Demo",
              "the pinned account is accepted")
        os.environ.pop("MT5_LOGIN")
        rep = br.cost_report(["EUR_USD", "XAU_USD"])
        check(len(rep) == 5 and "1,100" in rep[1] and "3,000" in rep[2],
              "cost report shows min-lot dollars (EUR/USD $1,100, gold $3,000)")

        print("\n[8] round trip: VM targets -> MT5 executor (fake ssh) -> VM ledger")
        import live.mt5_executor as mx
        vm_db, vm_kill = tmp / "vm.db", tmp / "VMKILL"
        vm = Ledger(str(vm_db))
        seed_sleeve(vm, "tsmom", "1d", "fx", fx_targets)
        seed_sleeve(vm, "tsmom", "1d", "metals", {"XAU_USD": 0.5})
        vm.set_slot("fx", "tsmom", "1d", "demo", 70_000, "test")
        vm.add_request("metals", "flatten", "demo")
        vm.commit()
        calls = []

        def fake_ssh(cmd, capture_output, text, timeout):
            calls.append(cmd)
            remote = cmd[-1].split("-m live.control ", 1)[1].split()
            res = subprocess.run([sys.executable, "-m", "live.control", "--db", str(vm_db),
                                  "--kill-file", str(vm_kill), "--env-file", str(tmp / "none.env"),
                                  *remote], capture_output=True, text=True, cwd=ROOT)
            return res

        mx.shutil.which = lambda name: "ssh"
        mx.STATE_FILE = str(tmp / "exec_state.json")
        os.environ["VM_SSH_TARGET"] = "signal-bot.test"
        fake = FakeMT5(hedging=True)
        fake.add_position("XAUUSDm", 0.02, True, magic=26091301)   # bot gold position to flatten
        args = SimpleNamespace(targets_source="ssh", db="unused", kill_file=str(tmp / "LOCALKILL"),
                               dry_run=False)
        rc = mx.sync(args, broker_factory=lambda: MT5Broker(mt5=fake, magic=26091301, symbol_map={}),
                     runner=fake_ssh)
        vm2 = Ledger(str(vm_db))
        filled = vm2.conn.execute("SELECT COUNT(*) FROM orders WHERE status='filled'").fetchone()[0]
        check(rc == 0 and len(calls) == 2, f"one targets fetch + one report over ssh (rc {rc}, {len(calls)} calls)")
        check(calls[0][:3] == ["ssh", "-o", "BatchMode=yes"] and "signal-bot.test" in calls[0],
              "ssh runs non-interactively against VM_SSH_TARGET")
        bot_units = MT5Broker(mt5=fake, magic=26091301, symbol_map={}).positions()
        check(bot_units.get("USD_JPY") == -10_000.0 and "XAU_USD" not in bot_units,
              "MT5 account traded to the fx targets and gold flattened")
        check(filled == 6 and vm2.last_sync() is not None and vm2.last_sync()["account_mode"] == "demo",
              f"VM ledger recorded {filled} fills and the sync")
        check(not vm2.pending_requests(), "flatten request marked done")
        calls.clear()
        rc = mx.sync(args, broker_factory=lambda: MT5Broker(mt5=fake, magic=26091301, symbol_map={}),
                     runner=fake_ssh)
        check(rc == 0 and len(fake.requests) == 6, "second sync: account already at target, no orders")
        vm_kill.write_text("x")
        for _ in range(2):
            mx.sync(args, broker_factory=lambda: MT5Broker(mt5=fake, magic=26091301, symbol_map={}),
                    runner=fake_ssh)
        blocked_rows = Ledger(str(vm_db)).conn.execute(
            "SELECT COUNT(*) FROM orders WHERE status='blocked'").fetchone()[0]
        check(blocked_rows == 1, f"repeated identical block is recorded once, not every run ({blocked_rows})")
        vm_kill.unlink()

        def broken_ssh(cmd, capture_output, text, timeout):
            return subprocess.CompletedProcess(cmd, 255, "", "Connection timed out")
        before = len(fake.requests)
        rc = mx.sync(args, broker_factory=lambda: MT5Broker(mt5=fake, magic=26091301, symbol_map={}),
                     runner=broken_ssh)
        check(rc == 1 and len(fake.requests) == before, "VM unreachable -> no orders at all")

        mx.OUTBOX_DIR = str(tmp / "unreported")
        vm_o = Ledger(str(vm_db))
        vm_o.conn.execute("UPDATE positions SET position=-0.5 WHERE strategy='tsmom' AND "
                          "timeframe='1d' AND symbol='USD_JPY'")
        vm_o.commit()

        def report_fails(cmd, capture_output, text, timeout):
            if "record-execution" in cmd[-1]:
                return subprocess.CompletedProcess(cmd, 1, "", "database is locked")
            return fake_ssh(cmd, capture_output, text, timeout)
        filled_before = Ledger(str(vm_db)).conn.execute(
            "SELECT COUNT(*) FROM orders WHERE status='filled'").fetchone()[0]
        rc = mx.sync(args, broker_factory=lambda: MT5Broker(mt5=fake, magic=26091301, symbol_map={}),
                     runner=report_fails)
        saved = list((tmp / "unreported" / "demo").glob("*.json"))
        check(rc == 1 and len(saved) == 1, "an undeliverable report (fills already sent) is kept on disk")
        rc = mx.sync(args, broker_factory=lambda: MT5Broker(mt5=fake, magic=26091301, symbol_map={}),
                     runner=fake_ssh)
        filled_after = Ledger(str(vm_db)).conn.execute(
            "SELECT COUNT(*) FROM orders WHERE status='filled'").fetchone()[0]
        check(rc == 0 and not list((tmp / "unreported" / "demo").glob("*.json"))
              and filled_after == filled_before + 1,
              f"...and delivered at the next sync ({filled_after - filled_before} fill recorded once)")

        vm3 = Ledger(str(vm_db))
        vm3.set_slot("metals", "tsmom", "1d", "live", 20_000, "test")
        vm3.commit()
        before = len(fake.requests)
        rc = mx.sync(args, broker_factory=lambda: MT5Broker(mt5=fake, magic=26091301, symbol_map={}),
                     runner=fake_ssh)
        vm3 = Ledger(str(vm_db))
        live_rows = vm3.conn.execute("SELECT COUNT(*) FROM orders WHERE mode='live' AND "
                                     "status IN ('blocked','filled','rejected')").fetchone()[0]
        check(rc == 0 and len(fake.requests) == before and live_rows == 0,
              "a demo executor ignores live slots (no orders, no alerts)")
        check(vm3.last_sync("demo") is not None and vm3.last_sync("live") is None,
              "syncs are recorded per executor mode")
        os.environ["EXECUTOR_MODE"] = "live"
        os.environ["ALLOW_LIVE_TRADING"] = "true"
        rc = mx.sync(args, broker_factory=lambda: MT5Broker(mt5=fake, magic=26091301, symbol_map={}),
                     runner=fake_ssh)
        vm3 = Ledger(str(vm_db))
        msg = vm3.conn.execute("SELECT message FROM orders WHERE status='blocked' AND mode='live' "
                               "ORDER BY id DESC LIMIT 1").fetchone()
        check(rc == 1 and len(fake.requests) == before and msg is not None
              and "EXECUTOR_MODE=live" in msg[0],
              "a live executor attached to a DEMO terminal refuses everything")
        check(vm3.last_sync("live") is not None, "the live executor's sync is visible on the VM")
        os.environ.pop("EXECUTOR_MODE")
        os.environ.pop("ALLOW_LIVE_TRADING")

        print("\n[9] MT5 price data: server clock and closed bars only")
        from live.mt5_data import ServerClock, detect_clock, fetch_mt5_hourly, history_gaps
        ny7 = ServerClock("ny+7")
        summer = int(pd.Timestamp("2026-07-06 00:00").timestamp())
        winter = int(pd.Timestamp("2026-01-05 00:00").timestamp())
        check([str(t) for t in ny7.to_utc([summer, winter])] ==
              ["2026-07-05 21:00:00+00:00", "2026-01-04 22:00:00+00:00"],
              "ny+7 server time: Monday 00:00 = Sunday 21:00 UTC in summer, 22:00 in winter")
        check(str(ServerClock("utc").to_utc([summer])[0]) == "2026-07-06 00:00:00+00:00",
              "utc server time is unchanged (Exness)")
        july = datetime(2026, 7, 8, 10, 20, tzinfo=timezone.utc)
        check(detect_clock(int(july.timestamp()) + 3 * 3600 - 30, july).scheme == "ny+7",
              "auto: +3h in US summer detected as ny+7")
        check(detect_clock(int(july.timestamp()) - 45, july).scheme == "utc+0",
              "auto: a UTC server detected as utc+0")
        try:
            detect_clock(int(july.timestamp()) - 2 * 86400, july)
            check(False, "no fresh tick and no cache must not guess the clock")
        except BrokerError:
            check(True, "no fresh tick and no saved clock: refuses to guess")
        check(detect_clock(None, july, cached="utc+2").scheme == "utc+2", "falls back to the saved clock")

        dm = FakeMT5(names={"EUR_USD": "EURUSD"})
        dtype = [("time", "<i8"), ("open", "<f8"), ("high", "<f8"), ("low", "<f8"), ("close", "<f8"),
                 ("tick_volume", "<u8"), ("spread", "<i4"), ("real_volume", "<u8")]
        server_hours = [int((july + timedelta(hours=3 - k)).replace(minute=0).timestamp())
                        for k in (4, 3, 2, 1, 0)]          # the last one is the forming 10:00 UTC bar
        dm.rates = np.array([(t, 1.1, 1.2, 1.0, 1.15, 100, 12, 0) for t in server_hours], dtype=dtype)
        dm.server_time = int(july.timestamp()) + 3 * 3600 - 5
        os.environ.pop("MT5_SERVER_TZ", None)
        bars, complete = fetch_mt5_hourly(universe("fx")[0], str(tmp / "mt5data"), mt5=dm, now=july)
        check(dm.rates_asked[0].year == 2023, "empty cache: asks MT5 for the whole history (3 years)")
        check(str(complete) == "2026-07-08 10:00:00+00:00" and len(bars) == 4
              and str(bars.index[-1]) == "2026-07-08 09:00:00+00:00",
              "forming bar dropped; bars indexed by UTC hour start")
        check(abs(bars["spread"].iloc[-1] - 12 * 0.00001 / 1.15) < 1e-12, "spread converted from points")
        cached, c2 = fetch_mt5_hourly(universe("fx")[0], str(tmp / "mt5data"), offline=True)
        check(len(cached) == 4 and c2 == complete, "offline reads the MT5 cache")
        import json
        other = tmp / "mt5data" / "mt5_Other-Real_EUR_USD_H1.parquet"
        bars.iloc[:2].to_parquet(other)                  # written last: the newest file
        other.with_suffix(".json").write_text(json.dumps({"complete_until": "2026-07-08T08:00:00+00:00"}))
        try:
            fetch_mt5_hourly(universe("fx")[0], str(tmp / "mt5data"), offline=True)
            check(False, "prices from two servers and no MT5_SERVER must not be guessed")
        except BrokerError:
            check(True, "offline, two servers' prices, no MT5_SERVER: refuses to guess")
        os.environ["MT5_SERVER"] = "Fake-Demo"
        try:
            chosen, _ = fetch_mt5_hourly(universe("fx")[0], str(tmp / "mt5data"), offline=True)
        finally:
            os.environ.pop("MT5_SERVER")
            other.unlink()
            other.with_suffix(".json").unlink()
        check(len(chosen) == 4, "offline reads MT5_SERVER's prices even when another server's are newer")
        dm.server_time = int(july.timestamp()) + 3 * 3600 - 2 * 3600 - 300   # market quiet for 2h
        _, complete = fetch_mt5_hourly(universe("fx")[0], str(tmp / "mt5data"), mt5=dm, now=july)
        check(str(complete) == "2026-07-08 09:00:00+00:00",
              "market closed: complete up to the end of the last ticking hour")
        check(dm.rates_asked[0] == datetime(2026, 7, 1, 9, tzinfo=timezone.utc),
              "cache holds the history: asks only for its last bar minus 7 days")
        fetch_mt5_hourly(universe("fx")[0], str(tmp / "mt5data"), mt5=dm, now=july, years=5)
        check(dm.rates_asked[0].year == 2021,
              "more years wanted than cached: asks for the whole history again")
        mt5dir = str(tmp / "mt5data")
        check(history_gaps(["EUR_USD"], mt5dir, server="Fake-Demo", years=5, now=july) == []
              and history_gaps(["EUR_USD", "GBP_USD"], mt5dir, server="Fake-Demo", years=8, now=july)
              == ["EUR_USD", "GBP_USD"],
              "history gaps: none once fetched in full; more years or no cache need a full download")
        check(history_gaps(["EUR_USD"], mt5dir, server="Fake-Demo", years=5, now=july + timedelta(days=400),
                           max_tail=pd.Timedelta(days=365)) == ["EUR_USD"],
              "history gaps: a cache older than the terminal's short bar limit reaches")
        dm.maxbars = 4
        try:
            fetch_mt5_hourly(universe("fx")[0], mt5dir, mt5=dm, now=july, years=5)
            check(False, "a bar limit that cannot reach the cache must not leave a gap")
        except BrokerError:
            check(True, "bar limit too short to reach the cached bars: refuses rather than leave a gap")
        dm.maxbars = 100_000
        import live.mt5_data as mt5_data
        os.environ["MT5_SERVER_TZ"] = "ny+7"
        cli_mt5 = FakeMT5()                 # every FX symbol, with the broker's "m" suffix
        cli_mt5.rates, cli_mt5.server_time = dm.rates, dm.server_time
        mt5_data._session["mt5"] = cli_mt5
        try:
            cli_rc = mt5_data.main(["fx", "--years", "3", "--cache-dir", str(tmp / "mt5cli"),
                                    "--passes", "1", "--env-file", str(tmp / "no.env")])
        finally:
            mt5_data._session["mt5"] = None
            os.environ.pop("MT5_SERVER_TZ")
        check(cli_rc == 0 and len(list((tmp / "mt5cli").glob("mt5_Fake-Demo_*_H1.parquet"))) == 7,
              "PC history download: caches for every FX symbol, reported complete")
        poll, mt5_data.RECONNECT_POLL = mt5_data.RECONNECT_POLL, 0
        try:
            dm.offline_polls = 3
            fetch_mt5_hourly(universe("fx")[0], mt5dir, mt5=dm, now=july, years=5)
            check(dm.offline_polls < 0, "terminal briefly disconnected: waits for it instead of failing")
            dm.offline_polls = 10_000
            try:
                fetch_mt5_hourly(universe("fx")[0], mt5dir, mt5=dm, now=july, years=5)
                check(False, "a terminal that stays disconnected must fail the fetch")
            except BrokerError as e:
                check("not connected" in str(e), "terminal stays disconnected: gives up after the wait")
        finally:
            mt5_data.RECONNECT_POLL = poll
            dm.offline_polls = 0
        retry_wait, mt5_data.HISTORY_RETRY_SECONDS = mt5_data.HISTORY_RETRY_SECONDS, 0
        try:
            dm.loading_answers = 2
            bars, _ = fetch_mt5_hourly(universe("fx")[0], mt5dir, mt5=dm, now=july, years=5)
            check(len(bars) == 3 and dm.loading_answers == 0,
                  "terminal still downloading a symbol: asks again instead of failing")
            dm.loading_answers = 99
            try:
                fetch_mt5_hourly(universe("fx")[0], mt5dir, mt5=dm, now=july, years=5)
                check(False, "a symbol that never gets bars must fail")
            except BrokerError:
                check(dm.loading_answers == 99 - mt5_data.HISTORY_TRIES, "gives up after a few tries")
        finally:
            mt5_data.HISTORY_RETRY_SECONDS = retry_wait
            dm.loading_answers = 0

        print("\n[10] Cloud Run: state in object storage, bridge data, start-up")
        import io
        import tarfile
        from collections import namedtuple

        from live import cloud_state
        from live.cloud_run import split_jobs, startup_ini
        from live.mt5_bridge import from_plain, to_plain

        store = cloud_state.FileStore(str(tmp / "bucket"))
        work = tmp / "work1"
        t0 = datetime(2026, 9, 14, 12, 2, tzinfo=timezone.utc)
        s1 = cloud_state.acquire(store, work, "run-1", timedelta(minutes=40), t0)
        (work / "state").mkdir(parents=True)
        (work / "data").mkdir()
        Ledger(str(work / "state" / "ledger.db"))
        (work / "data" / "prices.parquet").write_bytes(b"bars")
        (work / "state" / "run_jobs.lock").write_text("")
        cloud_state.save(s1)
        try:
            cloud_state.acquire(store, tmp / "work2", "run-2", timedelta(minutes=40), t0)
            check(False, "a second run is refused while the lease is held")
        except cloud_state.StateConflict:
            check(True, "a second run is refused while the lease is held")
        cloud_state.release(s1)
        s2 = cloud_state.acquire(store, tmp / "work2", "run-2", timedelta(minutes=40), t0)
        check((tmp / "work2" / "data" / "prices.parquet").read_bytes() == b"bars"
              and (tmp / "work2" / "state" / "ledger.db").exists()
              and not (tmp / "work2" / "state" / "run_jobs.lock").exists(),
              "state restored by the next run (ledger and caches, no lock files)")
        store.write(cloud_state.BUNDLE, cloud_state.pack(tmp / "work2"), s2.state_generation)
        try:
            cloud_state.save(s2)
            check(False, "an upload based on an older copy is refused")
        except cloud_state.StateConflict:
            check(True, "an upload based on an older copy is refused")
        s3 = cloud_state.acquire(store, tmp / "work3", "run-3", timedelta(minutes=40),
                                 t0 + timedelta(hours=1))
        check(s3.owner == "run-3", "an expired lease (crashed run) is taken over")
        check(cloud_state.backup(store, b"x", "2026-09-14") and not cloud_state.backup(store, b"y", "2026-09-14"),
              "one backup per day, never overwritten")
        evil = io.BytesIO()
        with tarfile.open(fileobj=evil, mode="w:gz") as tar:
            info = tarfile.TarInfo("../outside.txt")
            info.size = 1
            tar.addfile(info, io.BytesIO(b"x"))
        try:
            cloud_state.unpack(evil.getvalue(), tmp / "work4")
            check(False, "an archive entry outside data/, state/, models/ is refused")
        except ValueError:
            check(True, "an archive entry outside data/, state/, models/ is refused")

        class FakeHTTP:
            def __init__(self, status):
                self.status, self.calls = status, []

            def _resp(self, **kw):
                self.calls.append(kw)
                return SimpleNamespace(status_code=self.status, headers={}, content=b"",
                                       json=lambda: {"generation": "7"},
                                       raise_for_status=lambda: None)

            def post(self, url, **kw):
                return self._resp(url=url, **kw)

            def get(self, url, **kw):
                return self._resp(url=url, **kw)

        gcs = cloud_state.GcsStore("bkt", "staging", session=FakeHTTP(412),
                                   token_getter=lambda: ("tok", 1e12))
        try:
            gcs.write(cloud_state.BUNDLE, b"data", 5)
            check(False, "GCS precondition failure (412) becomes StateConflict")
        except cloud_state.StateConflict:
            call = gcs.http.calls[-1]
            check(call["params"]["ifGenerationMatch"] == "5" and call["params"]["name"] == "staging/state.tar.gz"
                  and call["headers"]["Authorization"] == "Bearer tok",
                  "GCS precondition failure (412) becomes StateConflict")
        missing = cloud_state.GcsStore("bkt", "", session=FakeHTTP(404), token_getter=lambda: ("t", 1e12))
        check(missing.read(cloud_state.LEASE) == (None, 0), "missing object reads as (None, 0)")

        Tick = namedtuple("Tick", "time bid ask")
        arr = np.array([(1, 1.1), (2, 1.2)], dtype=[("time", "<i8"), ("close", "<f8")])
        back = from_plain(to_plain({"tick": Tick(5, np.float64(1.5), 1.6), "rates": arr,
                                    "names": [Tick(1, 2, 3)]}))
        check(back["tick"].bid == 1.5 and back["rates"].dtype == arr.dtype
              and back["rates"]["close"].tolist() == [1.1, 1.2] and back["names"][0].ask == 3,
              "bridge results survive the trip as plain data (structs, arrays, lists)")

        check(startup_ini({"MT5_LOGIN": "1", "MT5_SERVER": "X"}) is None,
              "no start-up login without a password")
        ini = startup_ini({"MT5_LOGIN": "123", "MT5_PASSWORD": "pw", "MT5_SERVER": "Broker-Demo"})
        check("Login=123" in ini and "Server=Broker-Demo" in ini and "Enabled=0" in ini
              and "AllowLiveTrading=0" in ini and "MaxBars" not in ini,
              "start-up login config, algo trading off unless trading")
        limited = startup_ini({"MT5_LOGIN": "123", "MT5_PASSWORD": "pw", "MT5_SERVER": "Broker-Demo"},
                              max_bars=10_000)
        check("[Charts]\r\nMaxBars=10000\r\n" in limited, "start-up config limits the terminal's bars per chart")
        jobs = [("tsmom", "fx", "1h"), ("tsmom", "crypto", "1h"), ("tsmom", "metals", "1d")]
        os.environ["FX_DATA_SOURCE"] = "mt5"
        keep, skipped = split_jobs(jobs, mt5_ready=False)
        check(keep == [("tsmom", "crypto", "1h")] and len(skipped) == 2,
              "MT5 down: FX and gold jobs skipped, crypto still runs")
        check(split_jobs(jobs, mt5_ready=True) == (jobs, []), "MT5 up: every job runs")

        from live.cloud_run import Terminated, with_state
        bucket = tmp / "stopped-bucket"

        def stopped(root, now, clock):
            (root / "state").mkdir(exist_ok=True)
            (root / "state" / "done.txt").write_text("first job finished")
            raise Terminated("signal 15")

        cwd = os.getcwd()
        os.environ.update(STATE_URI=f"file://{bucket.as_posix()}", WORK_DIR=str(tmp / "stopped-work"))
        try:
            rc = with_state("hourly", stopped)
        finally:
            os.chdir(cwd)
            os.environ.pop("STATE_URI")
            os.environ.pop("WORK_DIR")
        stopped_store = cloud_state.FileStore(str(bucket))
        saved_blob, _ = stopped_store.read(cloud_state.BUNDLE)
        saved_names = tarfile.open(fileobj=io.BytesIO(saved_blob or b""), mode="r:gz").getnames() \
            if saved_blob else []
        check(rc == 143 and stopped_store.read(cloud_state.LEASE)[0] is None
              and "state/done.txt" in saved_names,
              "cancelled run (SIGTERM): saves what was done and releases the lease")

        from live.cloud_run import reset_feed
        feed_bucket = tmp / "feed-bucket"
        feed_store = cloud_state.FileStore(str(feed_bucket))
        seed = tmp / "feed-seed"
        for folder in ("data", "state", "models/binance", "models/Old-Demo"):
            (seed / folder).mkdir(parents=True, exist_ok=True)
        for name in ("mt5_Old-Demo_EUR_USD_H1.parquet", "mt5_Old-Demo_EUR_USD_H1.json",
                     "mt5_New-Real_EUR_USD_H1.parquet", "mt5_New-Real_EUR_USD_H1.json",
                     "BTCUSDT_1h.parquet"):
            (seed / "data" / name).write_text("x")
        (seed / "models" / "EUR_USD_1h_h6.pkl").write_text("trained before feeds")
        (seed / "models" / "Old-Demo" / "EUR_USD_1h_h6.pkl").write_text("demo")
        (seed / "models" / "binance" / "BTCUSDT_1h_h6.pkl").write_text("crypto")
        seed_ledger = Ledger(str(seed / "state" / "ledger.db"))
        for sym in ("EUR_USD", "XAU_USD", "BTCUSDT"):
            seed_ledger.set_position("tsmom", "1h", sym, 1.0, 1.0, "2026-09-15T00:00:00+00:00", 1.0,
                                     "2026-09-14T00:00:00+00:00")
            seed_ledger.add_bar("tsmom", "1h", sym, "2026-09-15T00:00:00+00:00", 0.0, 0.0, 0.0, 0.0, 1.0)
        seed_ledger.commit()
        seed_ledger.conn.close()
        feed_store.write(cloud_state.BUNDLE, cloud_state.pack(seed), 0)
        os.environ.update(STATE_URI=f"file://{feed_bucket.as_posix()}", WORK_DIR=str(tmp / "feed-work"),
                          MT5_SERVER="New-Real")
        try:
            reset_rc = reset_feed()
        finally:
            os.chdir(cwd)
            for key in ("STATE_URI", "WORK_DIR", "MT5_SERVER"):
                os.environ.pop(key, None)
        after_dir = tmp / "feed-after"
        cloud_state.unpack(feed_store.read(cloud_state.BUNDLE)[0], after_dir)
        names = {q.relative_to(after_dir).as_posix() for q in after_dir.rglob("*") if q.is_file()}
        after_ledger = Ledger(str(after_dir / "state" / "ledger.db"))
        sleeves = sorted(r["symbol"] for r in after_ledger.conn.execute("SELECT symbol FROM positions"))
        records = sorted(r["symbol"] for r in after_ledger.conn.execute("SELECT symbol FROM equity"))
        after_ledger.conn.close()
        archives = list((feed_bucket / "archive").glob("state-before-reset-*.tar.gz"))
        check(reset_rc == 0 and len(archives) == 1
              and {"data/mt5_New-Real_EUR_USD_H1.parquet", "data/BTCUSDT_1h.parquet",
                   "models/binance/BTCUSDT_1h_h6.pkl"} <= names
              and not any("Old-Demo" in n for n in names) and "models/EUR_USD_1h_h6.pkl" not in names
              and sleeves == ["BTCUSDT"] and records == ["BTCUSDT"]
              and feed_store.read(cloud_state.LEASE)[0] is None,
              "reset-feed: archives first, keeps one server's prices and models, restarts FX/gold only")

        add_store = cloud_state.FileStore(str(tmp / "add-bucket"))
        add_root = tmp / "add-pc"
        (add_root / "data").mkdir(parents=True)
        (add_root / "data" / "mt5_X_EUR_USD_H1.parquet").write_bytes(b"20 years")
        try:
            cloud_state.add_files(add_store, [add_root / "data" / "mt5_X_EUR_USD_H1.parquet"], add_root,
                                  "pc", wait_seconds=0)
            check(False, "adding to a location with no saved state must be refused")
        except cloud_state.StateConflict:
            pass
        first = cloud_state.acquire(add_store, tmp / "add-run", "run-1", timedelta(minutes=40))
        (tmp / "add-run" / "state").mkdir(parents=True, exist_ok=True)
        (tmp / "add-run" / "state" / "keep.txt").write_text("ledger")
        cloud_state.save(first)
        try:
            cloud_state.add_files(add_store, [add_root / "data" / "mt5_X_EUR_USD_H1.parquet"], add_root,
                                  "pc", wait_seconds=0)
            check(False, "a held lease must stop the upload")
        except cloud_state.StateConflict:
            pass
        cloud_state.release(first)
        added = cloud_state.add_files(add_store, [add_root / "data" / "mt5_X_EUR_USD_H1.parquet"],
                                      add_root, "pc", wait_seconds=0)
        add_blob, _ = add_store.read(cloud_state.BUNDLE)
        add_names = tarfile.open(fileobj=io.BytesIO(add_blob), mode="r:gz").getnames()
        check(added == ["data/mt5_X_EUR_USD_H1.parquet"] and "data/mt5_X_EUR_USD_H1.parquet" in add_names
              and "state/keep.txt" in add_names and add_store.read(cloud_state.LEASE)[0] is None,
              "PC upload: adds files to the saved state under the lease, keeps the rest")

        from live.cloud_run import SECRETS_OBJECT, load_secrets, parse_secrets, save_secrets
        check(parse_secrets('# comment\nMT5_LOGIN=4242\nTELEGRAM_TOKEN="tok:en"\nOTHER=x\nbad line\n')
              == {"MT5_LOGIN": "4242", "TELEGRAM_TOKEN": "tok:en"},
              "settings file: reads the bot's own keys, ignores comments and anything else")
        secrets_dir = tmp / "secrets-bucket"
        os.environ.update(SECRETS_URI=f"file://{secrets_dir.as_posix()}",
                          MT5_LOGIN="4242", TELEGRAM_TOKEN="tok")
        try:
            saved_rc = save_secrets()
            written = (secrets_dir / SECRETS_OBJECT).read_text()
            os.environ.pop("MT5_LOGIN")
            os.environ.pop("TELEGRAM_TOKEN")
            os.environ["MT5_SERVER"] = "Set-On-The-Job"   # Secret Manager / the job wins
            (secrets_dir / SECRETS_OBJECT).write_text(written + "MT5_SERVER=In-The-File\n")
            load_secrets()
            from_file = (os.environ.get("MT5_LOGIN"), os.environ.get("TELEGRAM_TOKEN"),
                         os.environ.get("MT5_SERVER"))
        finally:
            for key in ("SECRETS_URI", "MT5_LOGIN", "TELEGRAM_TOKEN", "MT5_SERVER"):
                os.environ.pop(key, None)
        check(saved_rc == 0 and "MT5_LOGIN=4242" in written and "TELEGRAM_TOKEN=tok" in written
              and from_file == ("4242", "tok", "Set-On-The-Job"),
              "settings file: saved, read back into the environment, job settings still win")

        import live.notify as notify_module
        from live.cloud_run import chats_from_updates, problem
        from live.signal_job import should_notify
        sent: list[str] = []
        real_send, notify_module.send = notify_module.send, lambda text, pre=False: sent.append(text) or True

        def troubled(root, now, clock):
            problem("MT5 not ready, FX and gold jobs skipped: IPC timeout", echo=False)
            return 1

        os.environ.update(STATE_URI=f"file://{(tmp / 'alert-bucket').as_posix()}",
                          WORK_DIR=str(tmp / "alert-work"))
        try:
            rc_bad = with_state("hourly", troubled)
            after_bad = list(sent)
            rc_good = with_state("hourly", lambda root, now, clock: 0)
        finally:
            os.chdir(cwd)
            notify_module.send = real_send
            os.environ.pop("STATE_URI")
            os.environ.pop("WORK_DIR")
        check(rc_bad == 1 and len(after_bad) == 1 and "MT5 not ready" in after_bad[0]
              and rc_good == 0 and len(sent) == 1,
              "a run with problems sends one Telegram alert; a clean run sends none")
        check(chats_from_updates([
            {"message": {"chat": {"id": 7, "type": "private", "first_name": "Ab", "username": "ab"}}},
            {"message": {"chat": {"id": -5, "type": "group", "title": "Desk"}}},
            {"my_chat_member": {"chat": {"id": 7, "type": "private", "first_name": "Ab"}}}]) ==
              [(-5, "group", "Desk"), (7, "private", "Ab (@ab)")],
              "telegram: chats that wrote to the bot, for TELEGRAM_CHAT_ID")
        check([should_notify(level, True, False, False) for level in ("changes", "problems", "always", "never")]
              == [True, False, True, False]
              and should_notify("problems", False, True, False) and should_notify("problems", False, False, True)
              and not should_notify("never", True, True, True),
              "paper messages: 'problems' skips plain position changes, keeps errors and hand orders")

        import time
        from live.cloud_run import written_since
        since = time.time() - 1
        (tmp / "written" / "Bases" / "history").mkdir(parents=True)
        (tmp / "written" / "Bases" / "history" / "2026.hcc").write_bytes(b"\0" * 3_000_000)
        report = written_since(since, [str(tmp / "written")], depth=len((tmp / "written").parts) + 1)
        check(report[0].split()[0] == "3" and "Bases" in report[1] and report[1].split()[0] == "3",
              "memory watch: files written since the start, summed per folder")

        print("\n[11] model retraining and paper predictions")
        from harness.data import synthetic_with_edge
        import json
        import sqlite3

        from live.ml_job import feed_for, model_paths, predict_one, train_one

        btc = universe("crypto")[0]
        syn = synthetic_with_edge(3000, seed=1)
        models = str(tmp / "models")
        line = train_one(btc, "1h", 6, syn, models, n_models=1, feed="binance")
        pkl, meta = model_paths(models, "binance", btc.symbol, "1h", 6)
        check(pkl.exists() and meta.exists() and "out-of-sample" in line
              and json.loads(meta.read_text())["feed"] == "binance",
              "train scores walk-forward, then saves the model, its verdict and its price feed")
        mled = Ledger(str(tmp / "ml.db"))
        first = predict_one(mled, btc, "1h", 6, syn.iloc[:-10], models, "binance")
        later = predict_one(mled, btc, "1h", 6, syn, models, "binance")
        again = predict_one(mled, btc, "1h", 6, syn, models, "binance")
        resolved = mled.resolved_predictions("binance", btc.symbol, "1h")
        check(first and later and "already recorded" in again and len(resolved) == 1
              and resolved[0]["outcome"] in (0.0, 1.0) and resolved[0]["bar_ts"] == str(syn.index[-11]),
              "one prediction per bar; scored once its horizon has passed")
        check(predict_one(mled, universe("crypto")[1], "1h", 6, syn, models, "binance") is None,
              "no model, no prediction")

        eur = universe("fx")[0]
        train_one(eur, "1h", 6, syn, models, n_models=1, feed="Broker-Demo")
        on_real = predict_one(mled, eur, "1h", 6, syn, models, "Broker-Real")
        on_demo = predict_one(mled, eur, "1h", 6, syn, models, "Broker-Demo")
        check(on_real is None and on_demo is not None,
              "a model trained on a demo feed is never applied to a real feed's prices")
        check(mled.add_prediction("Broker-Real", eur.symbol, "1h", 6, str(syn.index[-1]), 0.5, "", "x")
              and len(mled.open_predictions("Broker-Real", eur.symbol, "1h")) == 1
              and all(r["feed"] == "Broker-Demo" for r in mled.open_predictions("Broker-Demo", eur.symbol, "1h")),
              "the same bar predicted for two feeds: kept apart")
        os.environ["FX_DATA_SOURCE"] = "mt5"
        os.environ["MT5_SERVER"] = "Broker Real #1"
        check(feed_for(eur) == "Broker_Real_1" and feed_for(btc) == "binance",
              "feed names: the MT5 server for FX and gold, binance for crypto")
        os.environ.pop("MT5_SERVER")
        try:
            feed_for(eur)
            check(False, "an FX model without MT5_SERVER must not guess its feed")
        except ValueError:
            check(True, "FX model without MT5_SERVER: refuses to guess the feed")

        # --- the prediction scoreboard ---
        from live.ml_job import report_lines

        os.environ["MT5_SERVER"] = "Broker Real #1"
        empty = report_lines([(btc, "1h")], str(tmp / "empty-report.db"))
        check(any("Nothing has resolved yet" in ln for ln in empty)
              and any("no resolved predictions yet" in ln for ln in empty),
              "report: says plainly that nothing has resolved rather than showing blank columns")

        sdb = tmp / "scoreboard.db"
        sled = Ledger(str(sdb))
        # 6 LONG calls, 4 right; 4 flat calls, 3 right -> 7/10 accuracy, majority is 5/10
        rows = [(0.8, 1.0)] * 4 + [(0.8, 0.0)] * 2 + [(0.2, 0.0)] * 3 + [(0.2, 1.0)] * 1
        for i, (prob, outcome) in enumerate(rows):
            ts = f"2026-01-0{i + 1} 00:00:00+00:00"
            sled.add_prediction("binance", btc.symbol, "1h", 6, ts, prob, "2025-12-31", "NO EDGE")
            sled.resolve_prediction("binance", btc.symbol, "1h", 6, ts, outcome, 0.001)
        sled.commit()
        line = [ln for ln in report_lines([(btc, "1h")], str(sdb)) if ln.startswith(btc.symbol)][0]
        check("70.0%" in line and "50.0%" in line and "+20.0%" in line,
              "report: accuracy counts flat calls too, and lift is measured against the majority")
        check("66.7%" in line,
              "report: hit rate covers the LONG calls alone (4 of 6)")

        # --- the reserved holdout ---
        from live.ml_job import holdout_one, holdout_start, split_at_holdout, training_frame

        hsyn = synthetic_with_edge(6000, seed=7)
        hframe = training_frame(hsyn, btc.costs, 6)
        boundary = hframe.index[int(len(hframe) * 0.8)]
        os.environ["ML_HOLDOUT_FROM"] = str(boundary.date())
        bound = holdout_start()

        n_train = split_at_holdout(hframe.index, bound, 6)
        before = int((hframe.index < bound).sum())
        check(n_train == before - 6,
              "holdout: the label window before the boundary is purged from training")

        hmodels = str(tmp / "holdout-models")
        line = train_one(btc, "1h", 6, hsyn, hmodels, n_models=1, feed="binance")
        _, hmeta = model_paths(hmodels, "binance", btc.symbol, "1h", 6)
        info = json.loads(hmeta.read_text())
        check(info["holdout_from"] == str(bound) and "held back" in line,
              "holdout: the boundary is recorded next to the model")
        check(pd.Timestamp(info["trained_until"]) < bound and info["rows"] == n_train,
              "holdout: no training row reaches the boundary")

        scored = holdout_one(btc, "1h", 6, hsyn, hmodels, "binance", n_models=1)
        check("holdout bars" in scored and str(bound.date()) in scored,
              "holdout: the reserved slice scores against the shipped model")

        os.environ["ML_HOLDOUT_FROM"] = str((boundary + pd.Timedelta(days=1)).date())
        moved = holdout_one(btc, "1h", 6, hsyn, hmodels, "binance", n_models=1)
        check("retrain before scoring" in moved,
              "holdout: a model reserved for another boundary is refused, not silently scored")
        os.environ.pop("ML_HOLDOUT_FROM")
        check(split_at_holdout(hframe.index, holdout_start(), 6) == len(hframe),
              "holdout: unset reserves nothing, so existing behaviour is unchanged")

        old_db = tmp / "old-ledger.db"
        con = sqlite3.connect(old_db)
        con.executescript(
            "CREATE TABLE predictions (symbol TEXT, timeframe TEXT, horizon INTEGER, bar_ts TEXT, "
            "prob REAL NOT NULL, model_until TEXT, model_verdict TEXT, outcome REAL, fwd_return REAL, "
            "created_at TEXT, PRIMARY KEY (symbol, timeframe, horizon, bar_ts));"
            "INSERT INTO predictions VALUES ('EUR_USD', '1h', 6, '2026-09-15T00:00:00+00:00', 0.4, "
            "'', 'NO EDGE', NULL, NULL, '2026-09-15');")
        con.commit()
        con.close()
        migrated = Ledger(str(old_db))
        by_feed = [tuple(r) for r in migrated.conn.execute(
            "SELECT feed, COUNT(*) FROM predictions GROUP BY feed")]
        unscored = migrated.open_predictions("Broker-Real", "EUR_USD", "1h")
        migrated.conn.close()
        check(by_feed == [("legacy", 1)] and not unscored,
              "older predictions kept as 'legacy' and never scored against a new feed")
    finally:
        for k, val in saved_env.items():
            if val is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = val
        shutil.rmtree(tmp, ignore_errors=True)

    print("\nRESULT:", "ALL PASSED" if not FAILS else f"{len(FAILS)} FAILED")
    for f in FAILS:
        print("  -", f)
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
