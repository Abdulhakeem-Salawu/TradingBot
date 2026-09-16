"""MetaTrader 5 broker adapter.

Backends (MT5_BACKEND):
  native    the official `MetaTrader5` package. Windows only, with the MT5
            terminal installed; it attaches to the running terminal, or starts
            and logs it in when MT5_LOGIN / MT5_PASSWORD / MT5_SERVER are set.
  wine      Linux: the terminal and a Windows Python run under Wine, and
            live/mt5_bridge.py exposes the MetaTrader5 functions on
            127.0.0.1 (MT5_RPYC_PORT, default 8001). Anything that reaches the
            port can trade the account, so only localhost is accepted.

Everything the bot sees is in canonical symbols (EUR_USD) and base units
(euros, ounces). This adapter resolves broker spellings (EURUSD, EURUSDm,
EURUSD.a), converts units to lots with the symbol's contract size and lot
step, and only counts or modifies positions carrying the bot's magic number,
so manual trades in the same account are left alone.

Netting vs hedging accounts matter when reducing a position. On a netting
account an opposite deal reduces it. On a hedging account an opposite deal
would open a SECOND position (a hedge that costs double swap and margin), so
the adapter closes the bot's own positions by ticket first and only then opens
in the new direction.
"""

from __future__ import annotations

import math
import os
import socket

from harness.instruments import INSTRUMENTS
from live.broker import Broker, BrokerError, Fill, Quote

DEFAULT_MAGIC = 26091301
DEVIATION_POINTS = 20
RETCODE_DONE, RETCODE_PLACED, RETCODE_PARTIAL = 10009, 10008, 10010
RETCODE_MARKET_CLOSED, RETCODE_INVALID_FILL = 10018, 10030
CONTRACT_TOLERANCE = 0.02
ACCOUNT_TRADE_MODE_DEMO, ACCOUNT_TRADE_MODE_CONTEST, ACCOUNT_TRADE_MODE_REAL = 0, 1, 2
MARGIN_MODE_RETAIL_HEDGING = 2
SYMBOL_TRADE_MODE_FULL = 4
SYMBOL_FILLING_FOK, SYMBOL_FILLING_IOC = 1, 2
ORDER_FILLING_FOK, ORDER_FILLING_IOC, ORDER_FILLING_RETURN = 0, 1, 2
POSITION_TYPE_BUY = 0


def load_backend(name: str | None = None):
    """Return an object exposing the MetaTrader5 module API."""
    name = (name or os.environ.get("MT5_BACKEND") or "native").lower()
    if name == "native":
        try:
            import MetaTrader5 as mt5  # Windows-only wheel
        except ImportError as e:
            raise BrokerError("the MetaTrader5 package is not installed (it only exists for "
                              "Windows). On Linux use MT5_BACKEND=wine.") from e
        return mt5
    if name in ("wine", "mt5linux"):
        host = os.environ.get("MT5_RPYC_HOST", "127.0.0.1")
        port = int(os.environ.get("MT5_RPYC_PORT", "8001"))
        if host not in ("127.0.0.1", "localhost", "::1"):
            raise BrokerError(f"MT5_RPYC_HOST={host} refused: the bridge can trade the logged-in "
                              f"account for whoever reaches it. It must run on this machine.")
        try:
            from live.mt5_bridge import WineMT5
            return WineMT5(host, port)
        except ImportError as e:
            raise BrokerError("rpyc is not installed in the bot's Python (pip install rpyc)") from e
        except OSError as e:
            raise BrokerError(f"cannot reach the MT5 bridge on {host}:{port} ({e}) -- is "
                              f"mt5-bridge running under Wine? (deploy/LINUX_WINE_MT5.md)") from e
    raise BrokerError(f"unknown MT5_BACKEND {name!r} (native or wine)")


def _const(mt5, name: str, default):
    return getattr(mt5, name, default)


def initialize_kwargs(env=None) -> dict:
    """Arguments for mt5.initialize() from the environment.

    MT5_TERMINAL_PATH picks one terminal when several are installed (e.g. a
    demo and a live one). Login details are only passed when MT5_PASSWORD is
    set: otherwise the executor attaches to whatever account the terminal is
    logged into and never switches it.
    """
    env = os.environ if env is None else env
    kwargs = {}
    if env.get("MT5_TERMINAL_PATH"):
        kwargs["path"] = env["MT5_TERMINAL_PATH"]
    if env.get("MT5_LOGIN") and env.get("MT5_PASSWORD"):
        kwargs.update(login=int(env["MT5_LOGIN"]), password=env["MT5_PASSWORD"])
        if env.get("MT5_SERVER"):
            kwargs["server"] = env["MT5_SERVER"]
    return kwargs


def resolve_symbol(mt5, canonical: str, symbol_map: dict) -> str:
    """Broker symbol for a canonical name: MT5_SYMBOLS override, exact name, or one suffix match.

    Handles broker suffixes such as Exness's EURUSDm (Standard) and EURUSDz
    (Zero). Several matches are refused rather than guessed.
    """
    inst = INSTRUMENTS.get(canonical)
    want = symbol_map.get(canonical) or (inst.data_symbol if inst else canonical)
    name = None
    if mt5.symbol_info(want) is not None:
        name = want
    else:
        found = mt5.symbols_get(f"*{want}*") or ()
        names = sorted({s.name for s in found if s.name.upper().startswith(want.upper())})
        if len(names) == 1:
            name = names[0]
        elif len(names) > 1:
            raise BrokerError(f"{canonical}: several broker symbols match {want} "
                              f"({', '.join(names)}). Set MT5_SYMBOLS={canonical}:<name>.")
    if name is None:
        raise BrokerError(f"{canonical}: no broker symbol like {want}. Set "
                          f"MT5_SYMBOLS={canonical}:<name> if your broker spells it differently.")
    if not mt5.symbol_select(name, True):
        raise BrokerError(f"{canonical}: could not add {name} to Market Watch")
    return name


class MT5Broker(Broker):
    def __init__(self, mt5=None, magic: int | None = None, symbol_map: dict | None = None,
                 connect: bool = True):
        self.mt5 = mt5 if mt5 is not None else load_backend()
        self.magic = int(magic or os.environ.get("MT5_MAGIC") or DEFAULT_MAGIC)
        self.symbol_map = dict(symbol_map if symbol_map is not None else
                               _parse_symbol_map(os.environ.get("MT5_SYMBOLS", "")))
        self._resolved: dict[str, str] = {}
        self._contracts: dict[str, float] = {}
        self._fill_mode: dict[str, int] = {}
        if connect:
            self.connect()

    # ---------------------------------------------------------------- session
    def connect(self) -> None:
        if not self.mt5.initialize(**initialize_kwargs()):
            raise BrokerError(f"MT5 initialize failed: {self.mt5.last_error()} -- is the terminal "
                              f"installed, running and logged in?")
        term = self.mt5.terminal_info()
        if term is not None and not getattr(term, "trade_allowed", True):
            raise BrokerError("Algo Trading is disabled in the MT5 terminal: click the 'Algo "
                              "Trading' button in the toolbar so it turns green")
        pinned = os.environ.get("MT5_LOGIN", "").strip()
        if pinned:
            info = self._account()
            if str(info.login) != pinned:
                raise BrokerError(f"MT5 is logged into account {info.login} ({info.server}), not "
                                  f"MT5_LOGIN={pinned} -- refusing to trade the wrong account")

    def shutdown(self) -> None:
        try:
            self.mt5.shutdown()
        except Exception:  # noqa: BLE001 -- best effort on exit
            pass

    def _account(self):
        info = self.mt5.account_info()
        if info is None:
            raise BrokerError(f"MT5 account_info failed: {self.mt5.last_error()}")
        return info

    def account(self) -> tuple[float, str]:
        info = self._account()
        return float(info.equity), str(info.currency)

    def account_mode(self) -> str:
        mode = int(self._account().trade_mode)
        return "real" if mode == _const(self.mt5, "ACCOUNT_TRADE_MODE_REAL",
                                         ACCOUNT_TRADE_MODE_REAL) else "demo"

    def account_label(self) -> str:
        info = self._account()
        return f"{info.login}@{info.server}"

    def hedging(self) -> bool:
        return int(self._account().margin_mode) == _const(
            self.mt5, "ACCOUNT_MARGIN_MODE_RETAIL_HEDGING", MARGIN_MODE_RETAIL_HEDGING)

    # ---------------------------------------------------------------- symbols
    def resolve(self, canonical: str) -> str:
        if canonical not in self._resolved:
            self._resolved[canonical] = resolve_symbol(self.mt5, canonical, self.symbol_map)
        return self._resolved[canonical]

    def _info(self, canonical: str):
        info = self.mt5.symbol_info(self.resolve(canonical))
        if info is None:
            raise BrokerError(f"{canonical}: symbol_info failed: {self.mt5.last_error()}")
        return info

    def contract(self, canonical: str) -> float:
        """Base units per lot, cross-checked against the terminal's own profit calculation.

        Every size the bot sends is lots x contract size, so a broker that
        reports a contract size its P&L does not use (a cent account, a
        mis-specified CFD) would mis-size every order by that factor. The
        check computes the profit of 1 lot on a 0.1% move with
        order_calc_profit and refuses the symbol if the implied units differ
        from the contract size by more than 2%. When it cannot run (no quote
        yet), it is retried on the next call.
        """
        if canonical in self._contracts:
            return self._contracts[canonical]
        info = self._info(canonical)
        contract = float(info.trade_contract_size)
        implied = self._implied_units_per_lot(canonical, info)
        if implied is not None:
            if abs(implied / contract - 1) > CONTRACT_TOLERANCE:
                raise BrokerError(f"{canonical}: the broker reports {contract:g} units per lot but "
                                  f"its profit calculation implies {implied:,.0f}. Refusing to "
                                  f"size orders (cent account or unusual contract?)")
            self._contracts[canonical] = contract
        return contract

    def _implied_units_per_lot(self, canonical: str, info) -> float | None:
        calc = getattr(self.mt5, "order_calc_profit", None)
        base, profit_ccy = getattr(info, "currency_base", None), getattr(info, "currency_profit", None)
        if calc is None or base is None or profit_ccy is None:
            return None
        tick = self.mt5.symbol_info_tick(self.resolve(canonical))
        if tick is None or not tick.bid or not tick.ask:
            return None
        account_ccy = str(self._account().currency)
        mid = (tick.bid + tick.ask) / 2
        move = mid * 0.001
        profit = calc(_const(self.mt5, "ORDER_TYPE_BUY", 0), self.resolve(canonical), 1.0, mid,
                      mid + move)
        if profit is None:
            return None
        if profit_ccy == account_ccy:          # EURUSD, XAUUSD: profit is units x move
            return float(profit) / move
        if base == account_ccy:                 # USDJPY: profit converted back at ~mid
            return float(profit) * mid / move
        return None

    def normalize_units(self, symbol: str, units: float) -> float:
        """Round TOWARD ZERO to the lot step, so rounding never exceeds a limit."""
        info = self._info(symbol)
        contract, step, vmin = (self.contract(symbol), float(info.volume_step),
                                float(info.volume_min))
        lots = abs(units) / contract
        lots = math.floor(lots / step + 1e-9) * step
        if lots < vmin - 1e-12:
            return 0.0
        return math.copysign(round(lots, 8) * contract, units) if lots else 0.0

    # ---------------------------------------------------------------- market
    def quotes(self, symbols: list[str]) -> dict[str, Quote]:
        out = {}
        full = _const(self.mt5, "SYMBOL_TRADE_MODE_FULL", SYMBOL_TRADE_MODE_FULL)
        for sym in symbols:
            info = self._info(sym)
            tick = self.mt5.symbol_info_tick(self.resolve(sym))
            if tick is None or not tick.bid or not tick.ask:
                continue
            out[sym] = Quote(float(tick.bid), float(tick.ask), int(info.trade_mode) == full)
        return out

    def _bot_positions(self, broker_symbol: str) -> list:
        rows = self.mt5.positions_get(symbol=broker_symbol) or ()
        if self.hedging():
            rows = [p for p in rows if int(p.magic) == self.magic]
        return list(rows)

    def positions(self) -> dict[str, float]:
        out = {}
        buy = _const(self.mt5, "POSITION_TYPE_BUY", POSITION_TYPE_BUY)
        for canonical in {c for c in INSTRUMENTS if INSTRUMENTS[c].asset_class != "crypto"}:
            try:
                name = self.resolve(canonical)
            except BrokerError:
                continue
            contract = self.contract(canonical)
            units = sum(float(p.volume) * contract * (1 if int(p.type) == buy else -1)
                        for p in self._bot_positions(name))
            if units:
                out[canonical] = units
        return out

    def foreign_positions(self) -> list[str]:
        """Positions in the bot's symbols that the bot did not open (hedging accounts)."""
        notes = []
        if not self.hedging():
            return notes
        for canonical in self._resolved:
            for p in self.mt5.positions_get(symbol=self._resolved[canonical]) or ():
                if int(p.magic) != self.magic:
                    notes.append(f"{canonical}: position #{p.ticket} ({p.volume} lots) is not the "
                                 f"bot's and is ignored")
        return notes

    def _fillings(self, info) -> list[int]:
        """Filling modes the symbol advertises, preferred first; RETURN as a last resort."""
        flags = int(getattr(info, "filling_mode", 0))
        modes = []
        if flags & SYMBOL_FILLING_FOK:
            modes.append(_const(self.mt5, "ORDER_FILLING_FOK", ORDER_FILLING_FOK))
        if flags & SYMBOL_FILLING_IOC:
            modes.append(_const(self.mt5, "ORDER_FILLING_IOC", ORDER_FILLING_IOC))
        modes.append(_const(self.mt5, "ORDER_FILLING_RETURN", ORDER_FILLING_RETURN))
        return modes

    def _filling(self, info) -> int:
        return self._fillings(info)[0]

    def _deal(self, canonical: str, lots: float, buy: bool, position_ticket: int | None = None):
        name = self.resolve(canonical)
        info = self._info(canonical)
        tick = self.mt5.symbol_info_tick(name)
        req = {
            "action": _const(self.mt5, "TRADE_ACTION_DEAL", 1),
            "symbol": name,
            "volume": round(lots, 8),
            "type": _const(self.mt5, "ORDER_TYPE_BUY", 0) if buy else _const(self.mt5, "ORDER_TYPE_SELL", 1),
            "price": float(tick.ask if buy else tick.bid) if tick is not None else 0.0,
            "deviation": DEVIATION_POINTS,
            "magic": self.magic,
            "comment": "signal-bot",
            "type_time": _const(self.mt5, "ORDER_TIME_GTC", 0),
        }
        if position_ticket is not None:
            req["position"] = int(position_ticket)
        # Some brokers advertise a filling mode their server then rejects
        # (retcode 10030); try the next one instead of failing the order.
        modes = self._fillings(info)
        if name in self._fill_mode:
            modes = [self._fill_mode[name]] + [m for m in modes if m != self._fill_mode[name]]
        for mode in modes:
            req["type_filling"] = mode
            res = self.mt5.order_send(req)
            if res is None or int(res.retcode) != RETCODE_INVALID_FILL:
                break
        if res is None:
            return False, None, None, f"order_send failed: {self.mt5.last_error()}", None
        code = int(res.retcode)
        if code != RETCODE_INVALID_FILL:
            self._fill_mode[name] = req["type_filling"]
        if code in (RETCODE_DONE, RETCODE_PLACED):
            return True, float(res.price or req["price"]), str(res.order), "filled", code
        if code == RETCODE_PARTIAL:
            return False, float(res.price or req["price"]), str(res.order), \
                f"partially filled ({res.volume} lots)", code
        return False, None, None, f"retcode {code}: {getattr(res, 'comment', '')}", code

    def market_order(self, symbol: str, units: float) -> Fill:
        info = self._info(symbol)
        contract, step = self.contract(symbol), float(info.volume_step)
        lots_left = round(abs(units) / contract / step) * step
        if lots_left <= 0:
            return Fill(True, 0.0, None, None, "nothing to do")
        buy = units > 0
        done_lots, prices, refs, notes = 0.0, [], [], []

        if self.hedging():
            # Close the bot's opposite positions by ticket before opening anything new.
            buy_type = _const(self.mt5, "POSITION_TYPE_BUY", POSITION_TYPE_BUY)
            opposite = [p for p in self._bot_positions(self.resolve(symbol))
                        if (int(p.type) == buy_type) != buy]
            for p in sorted(opposite, key=lambda p: int(p.ticket)):
                if lots_left <= 1e-12:
                    break
                vol = min(float(p.volume), lots_left)
                ok, price, ref, msg, code = self._deal(symbol, vol, buy, position_ticket=p.ticket)
                if not ok:
                    return self._fill(False, done_lots, contract, buy, prices, refs,
                                      notes + [msg], code)
                done_lots += vol
                lots_left = round(lots_left - vol, 8)
                prices.append(price)
                refs.append(ref)
        if lots_left > 1e-12:
            ok, price, ref, msg, code = self._deal(symbol, lots_left, buy)
            if not ok:
                return self._fill(False, done_lots, contract, buy, prices, refs, notes + [msg], code)
            done_lots += lots_left
            prices.append(price)
            refs.append(ref)
        return self._fill(True, done_lots, contract, buy, prices, refs, ["filled"], None)

    @staticmethod
    def _fill(ok, lots, contract, buy, prices, refs, notes, code) -> Fill:
        units = lots * contract * (1 if buy else -1)
        msg = "; ".join(notes)
        if code == RETCODE_MARKET_CLOSED:
            msg = "market closed -- retry next run"
        price = sum(prices) / len(prices) if prices else None
        return Fill(ok, units, price, ",".join(r for r in refs if r) or None, msg)

    def cost_report(self, symbols: list[str]) -> list[str]:
        """Broker spreads and swaps next to the backtest's cost assumptions."""
        lines = [f"{'symbol':<9}{'broker':<12}{'spread bp':>10}{'model bp':>10}"
                 f"{'min lot $':>11}{'swap long':>11}{'swap short':>11}  swap mode, contract, min lot"]
        for sym in symbols:
            try:
                info = self._info(sym)
                tick = self.mt5.symbol_info_tick(self.resolve(sym))
                self.contract(sym)
            except BrokerError as e:
                lines.append(f"{sym:<9}{e}")
                continue
            checked = "" if sym in self._contracts else "  (lot size not verified: no quote)"
            mid = (tick.bid + tick.ask) / 2 if tick is not None and tick.bid else float("nan")
            spread_bp = (tick.ask - tick.bid) / mid * 1e4 if tick is not None and tick.bid else float("nan")
            model_bp = 2 * INSTRUMENTS[sym].costs.half_spread * 1e4 if sym in INSTRUMENTS else float("nan")
            flag = "  <-- wider than modeled" if spread_bp > model_bp else ""
            inst = INSTRUMENTS.get(sym)
            usd_per_base = 1.0 if inst is None or inst.base == "USD" else mid
            min_lot_usd = float(info.volume_min) * float(info.trade_contract_size) * usd_per_base
            lines.append(f"{sym:<9}{self.resolve(sym):<12}{spread_bp:>10.2f}{model_bp:>10.2f}"
                         f"{min_lot_usd:>11,.0f}{float(info.swap_long):>11.2f}"
                         f"{float(info.swap_short):>11.2f}  "
                         f"mode {info.swap_mode}, {info.trade_contract_size:g}, "
                         f"{info.volume_min:g}{flag}{checked}")
        lines.append("min lot $ = the smallest position the broker allows. A universe's capital is "
                     "split equally across its instruments and targets are often 0.3-1.0 of that "
                     "share, so an instrument needs a share several times its min lot to trade "
                     "at all; smaller targets are skipped as 'below broker minimum lot'.")
        lines.append("Swap units depend on 'swap mode' (points, account currency or % per year); "
                     "compare their SIGN and rough size with the modeled financing markup.")
        return lines


def _parse_symbol_map(text: str) -> dict[str, str]:
    """"EUR_USD:EURUSDm,XAU_USD:GOLD" -> {"EUR_USD": "EURUSDm", "XAU_USD": "GOLD"}"""
    out = {}
    for item in filter(None, (t.strip() for t in text.split(","))):
        if ":" in item:
            k, v = item.split(":", 1)
            out[k.strip()] = v.strip()
    return out


def host_label() -> str:
    """Where a report came from. In Cloud Run the hostname is "localhost", so use
    the job's name instead; on the PC it is the computer's name."""
    return os.environ.get("CLOUD_RUN_JOB") or socket.gethostname()
