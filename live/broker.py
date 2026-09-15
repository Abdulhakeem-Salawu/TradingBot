"""Broker interface the executor depends on.

The only real implementation is live/mt5_broker.py (MetaTrader 5). Keeping the
interface separate lets the executor's sizing and safety logic be tested
against a fake broker on any machine.

Units are always units of the instrument's BASE (EUR for EUR_USD, ounces for
XAU_USD), keyed by the bot's canonical symbol names. A broker adapter converts
to lots and to its own symbol spelling internally.
"""

from __future__ import annotations

from dataclasses import dataclass


class BrokerError(RuntimeError):
    pass


@dataclass
class Quote:
    bid: float
    ask: float
    tradeable: bool

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0


@dataclass
class Fill:
    ok: bool
    units: float
    price: float | None
    ref: str | None
    message: str


class Broker:
    """What the executor needs from any broker."""

    def account(self) -> tuple[float, str]:
        """(equity, account currency)"""
        raise NotImplementedError

    def account_mode(self) -> str:
        """"demo" or "real" -- which kind of account the connection is logged into."""
        raise NotImplementedError

    def positions(self) -> dict[str, float]:
        """Net units per canonical symbol held BY THIS BOT (negative = short)."""
        raise NotImplementedError

    def quotes(self, symbols: list[str]) -> dict[str, Quote]:
        raise NotImplementedError

    def normalize_units(self, symbol: str, units: float) -> float:
        """Round a target to what the broker can actually hold (lot step, minimum lot)."""
        return float(round(units))

    def market_order(self, symbol: str, units: float) -> Fill:
        """Move the position by `units` (positive buys, negative sells)."""
        raise NotImplementedError
