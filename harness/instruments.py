"""Instrument registry.

One place that knows, for every tradeable thing, where its data comes from,
what it costs, whether it can be shorted and how many bars make a year at each
timeframe. Everything downstream reads these specs instead of assuming BTC.
"""

from __future__ import annotations

from dataclasses import dataclass

from .costs import (BINANCE_SPOT, RETAIL_FX_FINANCING_MARKUP, RETAIL_FX_MAJOR,
                    RETAIL_XAU, RETAIL_XAU_FINANCING_MARKUP, CostModel)

TIMEFRAMES = ("1d", "1h")

_GRANULARITY = {
    "binance": {"1d": "1d", "1h": "1h"},
    "dukascopy": {"1d": "D", "1h": "H1"},
}


@dataclass(frozen=True)
class Instrument:
    symbol: str             # canonical name used everywhere in the bot (EUR_USD, BTCUSDT)
    asset_class: str        # "crypto" | "fx" | "metal"
    source: str             # "binance" | "dukascopy"
    long_only: bool         # spot crypto cannot be shorted without margin
    days_per_year: int      # 365 for 24/7 markets, 260 for Sunday-Friday markets
    hours_per_day: int      # 24, or 23 for gold's daily maintenance break
    base: str
    quote: str
    costs: CostModel
    financing_markup: float = 0.0
    feed_symbol: str = ""   # the data source's spelling, if different (EURUSD)

    @property
    def data_symbol(self) -> str:
        return self.feed_symbol or self.symbol

    def periods_per_year(self, timeframe: str) -> int:
        _check(timeframe)
        return self.days_per_year * (1 if timeframe == "1d" else self.hours_per_day)

    def granularity(self, timeframe: str) -> str:
        _check(timeframe)
        return _GRANULARITY[self.source][timeframe]

    @property
    def uses_rates(self) -> bool:
        """True when overnight financing depends on interest rates."""
        return self.asset_class in ("fx", "metal")


def _check(timeframe: str) -> None:
    if timeframe not in TIMEFRAMES:
        raise ValueError(f"Unknown timeframe {timeframe!r}. Choose from {TIMEFRAMES}.")


def _fx(symbol: str) -> Instrument:
    base, quote = symbol.split("_")
    return Instrument(symbol, "fx", "dukascopy", False, 260, 24, base, quote,
                      RETAIL_FX_MAJOR, RETAIL_FX_FINANCING_MARKUP, feed_symbol=base + quote)


def _crypto(symbol: str) -> Instrument:
    return Instrument(symbol, "crypto", "binance", True, 365, 24,
                      symbol.removesuffix("USDT"), "USDT", BINANCE_SPOT)


INSTRUMENTS: dict[str, Instrument] = {i.symbol: i for i in [
    # The seven USD majors. All share the USD leg, so they are far less
    # diversified than seven independent markets.
    _fx("EUR_USD"), _fx("USD_JPY"), _fx("GBP_USD"), _fx("USD_CHF"),
    _fx("AUD_USD"), _fx("USD_CAD"), _fx("NZD_USD"),
    Instrument("XAU_USD", "metal", "dukascopy", False, 260, 23, "XAU", "USD",
               RETAIL_XAU, RETAIL_XAU_FINANCING_MARKUP, feed_symbol="XAUUSD"),
    # Liquid, long-listed USDT pairs. Adding coins after seeing which did well
    # is survivorship bias; this list is fixed before any results are viewed.
    _crypto("BTCUSDT"), _crypto("ETHUSDT"), _crypto("BNBUSDT"),
    _crypto("XRPUSDT"), _crypto("ADAUSDT"), _crypto("SOLUSDT"),
]}

UNIVERSES: dict[str, list[str]] = {
    "fx": [s for s, i in INSTRUMENTS.items() if i.asset_class == "fx"],
    "metals": [s for s, i in INSTRUMENTS.items() if i.asset_class == "metal"],
    "crypto": [s for s, i in INSTRUMENTS.items() if i.asset_class == "crypto"],
}
UNIVERSES["all"] = UNIVERSES["fx"] + UNIVERSES["metals"] + UNIVERSES["crypto"]

# OECD 3-month interbank rates on FRED, monthly averages dated the 1st of the
# month, in percent. One consistent family for every currency; published with
# a lag of roughly two to four months.
SHORT_RATE_SERIES: dict[str, str] = {
    "USD": "IR3TIB01USM156N",
    "EUR": "IR3TIB01EZM156N",
    "GBP": "IR3TIB01GBM156N",
    "JPY": "IR3TIB01JPM156N",
    "CHF": "IR3TIB01CHM156N",
    "AUD": "IR3TIB01AUM156N",
    "CAD": "IR3TIB01CAM156N",
    "NZD": "IR3TIB01NZM156N",
}

# Maintained daily overnight rates on FRED, in percent, appended after the
# OECD series above stops. OECD's EUR and GBP 3-month series stopped at
# 2026-01-01 (checked 2026-09-14), while the other six were still current.
# Loading averages each daily series over COMPLETED calendar months, dated the
# 1st, which is the OECD convention, and keeps only months after the OECD
# series' last observation. Downstream code sees one continuous monthly series.
#
#   EUR  ECBESTRVOLWGTTRMDMNRT  euro short-term rate (ECB), from 2019-10
#   GBP  IUDSOIA                SONIA (Bank of England), from 1997
#
# Overnight rates are not 3-month rates: the gap is the expected policy path
# over three months. Where the series overlap (EUR 2019-10..2026-01, GBP
# 1997..2026-01), 3-month minus the month-average overnight rate averaged:
#   EUR +0.02 (sd 0.10) over the last 12 months, -0.25..+0.77 over the last 60;
#   GBP -0.08 (sd 0.06) over the last 12 months, -0.20..+1.20 over the last 60.
# A fixed level adjustment would be a fitted number about as large as its own
# noise, so none is applied. OECD's own monthly overnight series
# (IRSTCI01xxM156N) match these averages to about 0.01. The daily series are
# the direct sources and were current to within days when checked.
SHORT_RATE_EXTENSIONS: dict[str, str] = {
    "EUR": "ECBESTRVOLWGTTRMDMNRT",
    "GBP": "IUDSOIA",
}


def universe(name: str) -> list[Instrument]:
    if name not in UNIVERSES:
        raise ValueError(f"Unknown universe {name!r}. Choose from {sorted(UNIVERSES)}.")
    return [INSTRUMENTS[s] for s in UNIVERSES[name]]
