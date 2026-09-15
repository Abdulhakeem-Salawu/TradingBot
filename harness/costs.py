"""Transaction cost and financing model.

Every prediction is scored against this, not against raw price movement.
A +0.05% move that costs 0.20% to capture is a LOSS, and any harness that
scores it as a correct prediction is lying to you.

Two kinds of cost matter:

  TURNOVER   paid when the position CHANGES: fees, half the bid-ask spread,
             slippage. Holding a position costs nothing here.
  FINANCING  paid for every day a position is HELD. Zero for unlevered crypto
             spot; first-order for FX and gold, where the overnight swap is
             the interest-rate differential plus a broker markup. For FX carry
             strategies the financing line IS the return.
"""

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class CostModel:
    """Costs as fractions of notional.

    Defaults are Binance spot VIP 0 taker with a conservative slippage
    assumption, kept for the hourly classifier path. Check your own venue's
    actual schedule before trusting any preset -- fee tiers, spreads and
    financing markups change without much notice.
    """

    taker_fee: float = 0.0010        # 0.10% per side
    maker_fee: float = 0.0010        # set lower if you reliably post
    slippage_per_side: float = 0.0010  # 0.10% per side; raise for illiquid pairs
    use_maker: bool = False
    half_spread: float = 0.0         # half the bid-ask spread, paid on every side
    financing_long_annual: float = 0.0   # annual rate paid to hold +1; negative = earned
    financing_short_annual: float = 0.0  # annual rate paid to hold -1; negative = earned

    @property
    def per_side(self) -> float:
        fee = self.maker_fee if self.use_maker else self.taker_fee
        return fee + self.half_spread + self.slippage_per_side

    @property
    def round_trip(self) -> float:
        """Total cost of a full in-and-out cycle."""
        return 2.0 * self.per_side

    def breakeven_move(self) -> float:
        """Minimum favourable move required for a trade to net zero."""
        return self.round_trip

    def net_return(self, gross_return: float) -> float:
        """Apply round-trip cost to a gross return."""
        return gross_return - self.round_trip

    def turnover_cost(self, abs_dpos):
        """Cost of changing the position by |dpos| units of notional.

        Going 0 -> 1 is one side; 1 -> -1 is two sides.
        """
        return self.per_side * abs_dpos

    def financing_cost(self, pos, days):
        """Cost of holding `pos` for `days` calendar days at the constant rates."""
        pos = np.asarray(pos, dtype=float)
        rate = np.where(pos > 0, self.financing_long_annual, self.financing_short_annual)
        return np.abs(pos) * rate * np.asarray(days, dtype=float) / 365.0

    def describe(self) -> str:
        s = (f"per-side {self.per_side * 100:.3f}%  |  "
             f"round-trip {self.round_trip * 100:.3f}%  |  "
             f"breakeven move {self.breakeven_move() * 100:.3f}%")
        if self.financing_long_annual or self.financing_short_annual:
            s += (f"  |  financing long {self.financing_long_annual:+.2%}/yr "
                  f"short {self.financing_short_annual:+.2%}/yr")
        return s


def align_asof(series: pd.Series, index: pd.DatetimeIndex) -> pd.Series:
    """Last known value of `series` at each timestamp in `index`.

    FRED dates are timezone-naive and price bars are UTC. Mixing the two in a
    union silently breaks the alignment -- an earlier version forward-filled
    the final rate across the whole history -- so the timezone is matched
    explicitly first.
    """
    s = series.dropna().sort_index()
    if index.tz is not None and s.index.tz is None:
        s.index = s.index.tz_localize(index.tz)
    elif index.tz is None and s.index.tz is not None:
        s.index = s.index.tz_convert(None)
    return s.reindex(s.index.union(index)).ffill().reindex(index)


def financing_from_rates(index: pd.DatetimeIndex, base_rate: pd.Series | None,
                         quote_rate: pd.Series | None, markup: float) -> pd.DataFrame:
    """Annual financing rates for a BASE/QUOTE position, from short-rate series.

    Holding +1 of BASE/QUOTE means lending BASE and borrowing QUOTE, so the
    long pays (quote - base) and the short pays (base - quote). The broker
    adds `markup` to both sides, which is why round-tripping carry is never
    free. Rates are decimals (0.05 = 5%). A missing leg (gold has no deposit
    rate worth modelling) counts as zero.

    These are ACCOUNTING rates for P&L, so they are used contemporaneously.
    Strategy SIGNALS that read rates must lag them for publication delay.
    """
    def _on(series):
        if series is None:
            return pd.Series(0.0, index=index)
        return align_asof(series, index).fillna(0.0)

    diff = _on(quote_rate) - _on(base_rate)
    return pd.DataFrame({"long": diff + markup, "short": -diff + markup}, index=index)


# Preset for a venue where you post limit orders and get filled. Binance spot
# VIP 0 charges 0.10% maker too; 0.02% is only reachable on futures or at
# high VIP tiers, so this preset is optimistic for a retail spot account.
MAKER_MODEL = CostModel(maker_fee=0.0002, slippage_per_side=0.0003, use_maker=True)

# Preset for aggressive market orders on a thin altcoin
ILLIQUID_MODEL = CostModel(taker_fee=0.0010, slippage_per_side=0.0035)

# Daily rebalancing of liquid USDT pairs with market orders. Spread on BTCUSDT
# is a fraction of a basis point, so slippage covers it.
BINANCE_SPOT = CostModel(taker_fee=0.0010, slippage_per_side=0.0005)

# Retail MT5 broker, standard (spread-only) account. Typical EUR/USD spreads
# are ~1-1.5 pips (~1-1.4bp); the AUD/NZD/CAD/CHF majors run wider, so 3bp
# full spread is a conservative figure for the whole group. Dukascopy's ECN
# spreads (printed by run_research.py) are tighter than this on purpose.
# Financing comes from rates; the swap markup is an ASSUMPTION --
# `python -m live.mt5_executor --check-costs` shows your broker's actual swaps.
RETAIL_FX_MAJOR = CostModel(taker_fee=0.0, half_spread=0.00015, slippage_per_side=0.00005)
RETAIL_FX_FINANCING_MARKUP = 0.02

# XAU/USD: spreads of roughly $0.30-0.50/oz at retail. Longs always pay
# financing (borrowing USD to hold a non-yielding asset).
RETAIL_XAU = CostModel(taker_fee=0.0, half_spread=0.0002, slippage_per_side=0.0001)
RETAIL_XAU_FINANCING_MARKUP = 0.02
