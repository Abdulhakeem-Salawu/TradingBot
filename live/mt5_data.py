"""FX and gold hourly bars from a MetaTrader 5 terminal (FX_DATA_SOURCE=mt5).

The alternative to Dukascopy on a machine that runs MT5 anyway. The terminal
already holds the broker's history and streams its prices, so there is no
rate limit and the bars are the prices the account actually trades at.
Only for machines with a terminal (Windows, or Linux through Wine); the free
VM keeps using Dukascopy.

    FX_DATA_SOURCE=mt5         use this loader for every FX and gold instrument
    MT5_HISTORY_YEARS=3        how far back to load (strategies need ~1.5 years)
    MT5_SERVER_TZ=auto         broker clock: auto, ny+7, utc, utc+3, utc-1 ...

Keep one source per ledger: paper records built from Dukascopy and from a
broker's feed differ slightly, so do not switch a running ledger between them.

SERVER TIME. MT5 stamps bars in the broker's server time, not UTC. Two
conventions cover almost every broker:
  ny+7   New York time + 7 hours, so the FX day (17:00 New York) starts at
         00:00 server time all year: UTC+3 in US summer, UTC+2 in winter.
         MetaQuotes-Demo and most brokers.
  utc+N  a fixed offset. Exness runs its servers on UTC (utc).
`auto` measures the current offset from the newest tick and picks ny+7 when
it matches New York + 7 today, otherwise a fixed offset. A server that keeps
EU instead of US daylight saving is off by one hour for about three weeks a
year under ny+7, which moves one hourly bar across a daily boundary; set
MT5_SERVER_TZ explicitly if your broker documents something else.

CLOSED BARS ONLY. A bar is kept only if its hour has ended in UTC and the
terminal is connected, so a forming bar is never stored as final.
"""

from __future__ import annotations

import atexit
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from harness.instruments import Instrument
from live.broker import BrokerError

DEFAULT_YEARS = 3
FRESH_TICK_SECONDS = 600    # newer than this: prices are flowing, the current hour is the edge
REFRESH_OVERLAP = pd.Timedelta(days=7)   # cached bars re-read from MT5 on every fetch
HISTORY_TRIES, HISTORY_RETRY_SECONDS = 8, 2.5   # while the terminal downloads a symbol's bars
RECONNECT_WAIT, RECONNECT_POLL = 60.0, 2.0       # a terminal switching access points drops briefly
CLOCK_TICK_SECONDS = 120    # newer than this: good enough to read the server's UTC offset
_CLOCK_PROBES = ("EURUSD", "XAUUSD")
_WEEKEND_PROBES = ("BTCUSD",)   # still ticks when FX and gold are closed
_session = {"mt5": None}


# ------------------------------------------------------------------ server clock
class ServerClock:
    """Converts MT5 server timestamps to UTC."""

    def __init__(self, scheme: str):
        scheme = scheme.lower().replace(" ", "")
        if scheme == "utc":
            scheme = "utc+0"
        if scheme != "ny+7" and not re.fullmatch(r"utc[+-]\d{1,2}", scheme):
            raise ValueError(f"MT5_SERVER_TZ={scheme!r}: use auto, ny+7, utc or utc+N / utc-N")
        self.scheme = scheme

    def __repr__(self) -> str:
        return f"ServerClock({self.scheme})"

    def offset_hours(self, utc: datetime) -> int:
        if self.scheme == "ny+7":
            ny = pd.Timestamp(utc).tz_convert("America/New_York")
            return int(ny.utcoffset().total_seconds() // 3600) + 7
        return int(self.scheme[3:])

    def to_utc(self, server_seconds) -> pd.DatetimeIndex:
        """Server wall-clock epoch seconds (as MT5 returns them) -> UTC timestamps."""
        naive = pd.to_datetime(pd.Series(server_seconds).astype("int64"), unit="s")
        if self.scheme == "ny+7":
            ny = (naive - pd.Timedelta(hours=7)).dt.tz_localize(
                "America/New_York", ambiguous=False, nonexistent="shift_forward")
            return pd.DatetimeIndex(ny.dt.tz_convert("UTC"))
        return pd.DatetimeIndex((naive - pd.Timedelta(hours=int(self.scheme[3:]))).dt.tz_localize("UTC"))


def fx_weekend(utc_now: datetime) -> bool:
    """Friday 20:00 to Sunday 22:00 UTC: FX and gold ticks are stale (both clocks covered)."""
    wd, hour = utc_now.weekday(), utc_now.hour
    return (wd == 4 and hour >= 20) or wd == 5 or (wd == 6 and hour < 22)


def detect_clock(server_now: int | None, utc_now: datetime, setting: str = "auto",
                 cached: str | None = None) -> ServerClock:
    """Pick the server clock from the newest tick (server seconds) and the real UTC time.

    A tick only reveals the offset if it is seconds old, and a stale tick can
    land close to a whole hour by chance. So a reading is used only when it
    is within CLOCK_TICK_SECONDS of a plausible offset, and it may override a
    saved clock only when it is very close (a changed broker clock), never
    when it is merely plausible.
    """
    if setting and setting.lower() != "auto":
        return ServerClock(setting)
    if server_now is not None:
        diff = server_now - utc_now.timestamp()
        hours = round(diff / 3600)
        residual = abs(diff - hours * 3600)
        if -12 <= hours <= 14 and residual <= CLOCK_TICK_SECONDS:
            ny7 = ServerClock("ny+7").offset_hours(utc_now)
            detected = ServerClock("ny+7" if hours == ny7 else f"utc{hours:+d}")
            if cached is None or detected.scheme == cached or residual <= 30:
                return detected
    if cached:
        return ServerClock(cached)
    raise BrokerError("cannot tell the MT5 server's clock: no fresh tick (market closed?). "
                      "Set MT5_SERVER_TZ (e.g. ny+7 or utc) in .env.")


# --------------------------------------------------------------------- terminal
def _terminal(mt5=None):
    """One attached terminal per process, shut down at exit."""
    if mt5 is not None:
        return mt5
    if _session["mt5"] is None:
        from live.mt5_broker import initialize_kwargs, load_backend

        term = load_backend()
        if not term.initialize(**initialize_kwargs()):
            raise BrokerError(f"MT5 initialize failed: {term.last_error()} -- is the terminal "
                              f"running and logged in?")
        _session["mt5"] = term
        atexit.register(lambda: term.shutdown())
    return _session["mt5"]


def _connected_terminal(mt5):
    """terminal_info(), waiting up to RECONNECT_WAIT for a dropped connection.

    Shortly after logging in, a terminal scans the broker's access points and
    may move to a faster one, which disconnects it for a moment; a fetch then
    should wait rather than fail the symbol.
    """
    waited = 0.0
    term = mt5.terminal_info()
    while term is not None and not getattr(term, "connected", True):
        if waited >= RECONNECT_WAIT:
            raise BrokerError(f"the MT5 terminal is not connected to its broker server "
                              f"(waited {waited:.0f}s)")
        time.sleep(RECONNECT_POLL)
        waited += max(RECONNECT_POLL, 0.5)
        term = mt5.terminal_info()
    return term


def _server_now(mt5, names) -> int | None:
    times = []
    for name in names:
        tick = mt5.symbol_info_tick(name)
        if tick is not None and tick.time:
            times.append(int(tick.time))
    return max(times) if times else None


def _probe_names(mt5, resolved: str, weekend: bool = False) -> list[str]:
    names = [] if weekend else [resolved]
    for probe in _WEEKEND_PROBES if weekend else _CLOCK_PROBES + _WEEKEND_PROBES:
        found = [s.name for s in (mt5.symbols_get(f"{probe}*") or ())]
        if found:
            mt5.symbol_select(found[0], True)
            names.append(found[0])
    return names


def feed_name(server: str) -> str:
    """The name one broker server's prices go by: in cache files, model folders and
    predictions. A demo and a real server are different feeds and never mix."""
    return re.sub(r"[^A-Za-z0-9._-]+", "_", server or "mt5")


def _cache_paths(cache_dir: str, server: str, symbol: str) -> tuple[Path, Path]:
    cache = Path(cache_dir) / f"mt5_{feed_name(server)}_{symbol}_H1.parquet"
    return cache, cache.with_suffix(".json")


def cached_servers(cache_dir: str, symbol: str) -> list[str]:
    """Feed names that have a price file for `symbol` in `cache_dir`."""
    suffix = f"_{symbol}_H1.parquet"
    return sorted(p.name[len("mt5_"):-len(suffix)] for p in Path(cache_dir).glob(f"mt5_*{suffix}"))


def _offline_cache(cache_dir: str, symbol: str) -> Path:
    """The price file of MT5_SERVER -- never another server's, however recent."""
    server = os.environ.get("MT5_SERVER", "")
    if server:
        cache = _cache_paths(cache_dir, server, symbol)[0]
        if not cache.exists():
            raise FileNotFoundError(f"offline, but no {feed_name(server)} prices for {symbol} ({cache})")
        return cache
    found = cached_servers(cache_dir, symbol)
    if len(found) == 1:
        return _cache_paths(cache_dir, found[0], symbol)[0]
    if not found:
        raise FileNotFoundError(f"offline, but no MT5 prices for {symbol} in {cache_dir}")
    raise BrokerError(f"{symbol}: prices from several MT5 servers ({', '.join(found)}); "
                      f"set MT5_SERVER to the one to use")


def _history_start(now: datetime, years: float | None) -> pd.Timestamp:
    years = float(years or os.environ.get("MT5_HISTORY_YEARS") or DEFAULT_YEARS)
    return pd.Timestamp(now) - pd.Timedelta(days=365.25 * years)


def _holds_history(first_bar, history_from, start: pd.Timestamp) -> bool:
    """The cache reaches back to `start`, or an earlier full fetch found no older bars."""
    return first_bar is not None and (pd.Timestamp(first_bar) <= start + REFRESH_OVERLAP or (
        history_from is not None and pd.Timestamp(history_from) <= start + REFRESH_OVERLAP))


def history_gaps(symbols, cache_dir: str = "data", server: str | None = None,
                 years: float | None = None, now: datetime | None = None,
                 max_tail: pd.Timedelta | None = None) -> list[str]:
    """Symbols whose next fetch needs deep history: no cache holds the whole
    history yet, or (with max_tail) the last cached bar is older than that.

    A terminal limited to a few thousand bars per chart cannot serve those, so
    Cloud Run raises the limit (and uses more memory) only for runs that need it.
    """
    server = server if server is not None else os.environ.get("MT5_SERVER", "")
    now = now or datetime.now(timezone.utc)
    start = _history_start(now, years)
    gaps = []
    for symbol in symbols:
        cache, marker = _cache_paths(cache_dir, server, symbol)
        try:
            index = pd.read_parquet(cache, columns=[]).index if cache.exists() else pd.DatetimeIndex([])
            saved = json.loads(marker.read_text()) if marker.exists() else {}
        except (OSError, ValueError):
            index, saved = pd.DatetimeIndex([]), {}
        first = index.min() if len(index) else None
        stale = max_tail is not None and len(index) and \
            pd.Timestamp(now) - index.max() + REFRESH_OVERLAP > max_tail
        if not _holds_history(first, saved.get("history_from"), start) or stale:
            gaps.append(symbol)
    return gaps


def fetch_mt5_hourly(inst: Instrument, cache_dir: str = "data", offline: bool = False,
                     years: float | None = None, mt5=None,
                     now: datetime | None = None) -> tuple[pd.DataFrame, pd.Timestamp]:
    """Hourly BID bars for one instrument, and the time up to which every hour is final.

    Same shape as harness.data.fetch_dukascopy_hourly: UTC hour-start index,
    open/high/low/close/volume (tick volume) and spread (fraction of price).
    """
    from live.mt5_broker import _parse_symbol_map, resolve_symbol

    Path(cache_dir).mkdir(exist_ok=True)
    server_hint = os.environ.get("MT5_SERVER", "")
    if offline:
        cache = _offline_cache(cache_dir, inst.symbol)
        marker = cache.with_suffix(".json")
        return pd.read_parquet(cache), pd.Timestamp(json.loads(marker.read_text())["complete_until"])

    mt5 = _terminal(mt5)
    term = _connected_terminal(mt5)
    account = mt5.account_info()
    server = str(getattr(account, "server", "") or server_hint)
    cache, marker = _cache_paths(cache_dir, server, inst.symbol)
    old = pd.read_parquet(cache) if cache.exists() else pd.DataFrame()
    saved = json.loads(marker.read_text()) if marker.exists() else {}

    name = resolve_symbol(mt5, inst.symbol, _parse_symbol_map(os.environ.get("MT5_SYMBOLS", "")))
    now = now or datetime.now(timezone.utc)
    clock = detect_clock(_server_now(mt5, _probe_names(mt5, name, weekend=fx_weekend(now))), now,
                         os.environ.get("MT5_SERVER_TZ", "auto"), saved.get("clock"))
    server_now = _server_now(mt5, [name])   # this instrument's own last tick decides "closed"

    start = _history_start(now, years)
    # Once the cache holds the whole history, ask MT5 only for its tail. A
    # terminal that starts empty -- every Cloud Run start -- would otherwise
    # download all the years of history again: minutes and hundreds of MB per symbol.
    history_from = saved.get("history_from")
    has_history = _holds_history(old.index[0] if len(old) else None, history_from, start)
    fetch_from = max(start, old.index[-1] - REFRESH_OVERLAP) if has_history else start
    # A terminal that has not loaded this symbol yet answers with no bars while
    # it downloads them (fresh Cloud Run terminals, symbols without a chart).
    for attempt in range(HISTORY_TRIES):
        rates = mt5.copy_rates_range(name, mt5.TIMEFRAME_H1,
                                     datetime.fromtimestamp(fetch_from.timestamp(), timezone.utc),
                                     datetime.fromtimestamp(now.timestamp() + 86400, timezone.utc))
        if (rates is not None and len(rates)) or attempt == HISTORY_TRIES - 1:
            break
        time.sleep(HISTORY_RETRY_SECONDS)
    if rates is None or not len(rates):
        raise BrokerError(f"{inst.symbol}: no H1 history from MT5 for {name}: {mt5.last_error()}")
    maxbars = int(getattr(term, "maxbars", 0) or 0)
    cut = bool(maxbars) and len(rates) >= maxbars - 1
    if cut and has_history:     # the bars between the cache and the terminal's oldest would be missing
        raise BrokerError(f"{inst.symbol}: the terminal's {maxbars:,}-bar limit does not reach back to "
                          f"the cached bars ({old.index[-1]:%Y-%m-%d}); raise MT5_MAX_BARS")
    if cut:
        print(f"  note: {inst.symbol} history was cut at the terminal's {maxbars:,}-bar limit; "
              f"raise Tools > Options > Charts > 'Max. bars in chart' (MT5_MAX_BARS on Cloud Run)")

    raw = pd.DataFrame(rates)
    point = float(getattr(mt5.symbol_info(name), "point", 0) or 0)
    idx = clock.to_utc(raw["time"])
    df = pd.DataFrame({"open": raw["open"].to_numpy(float), "high": raw["high"].to_numpy(float),
                       "low": raw["low"].to_numpy(float), "close": raw["close"].to_numpy(float),
                       "volume": raw["tick_volume"].to_numpy(float)}, index=idx)
    df["spread"] = (raw["spread"].to_numpy(float) * point / df["close"]) if point else float("nan")
    df.index.name = "ts"

    # Every hour before complete_until is final: the current hour when ticks are
    # flowing, or the end of the last ticking hour when the market is closed.
    now_hour = pd.Timestamp(now).floor("h")
    if server_now is not None:
        last_tick = pd.Timestamp(clock.to_utc([server_now])[0])
        fresh = (pd.Timestamp(now) - last_tick).total_seconds() <= FRESH_TICK_SECONDS
        complete = now_hour if fresh else min(now_hour, last_tick.floor("h") + pd.Timedelta(hours=1))
    else:
        complete = now_hour
    df = df[df.index + pd.Timedelta(hours=1) <= complete]
    if len(old):
        old = old[old.index < df.index[0]] if len(df) else old
    from harness.data import _merge

    df = _merge(old, df)
    df.index.name = "ts"
    df.to_parquet(cache)
    if not has_history and not cut:
        history_from = start.isoformat()     # the whole history was asked for, and all of it came
    marker.write_text(json.dumps({"complete_until": complete.isoformat(), "server": server,
                                  "symbol": name, "clock": clock.scheme,
                                  "history_from": history_from}))
    return df, complete


# ------------------------------------------------------------------ command line
BARS_PER_YEAR = 6_300      # FX and gold trade ~24h x 5 days: about 6,200 hourly bars a year


def main(argv=None) -> int:
    """Download MT5 hourly history into the price caches: prices only, no ledger, no orders.

    For a desktop terminal (the Windows PC), which downloads years of history in
    minutes; Cloud Run then only ever fetches the last days of these caches.

        python -m live.mt5_data --years 20            # FX and gold
        python -m live.mt5_data fx --years 20
    """
    import argparse

    from harness.instruments import universe
    from live.env import load_env

    ap = argparse.ArgumentParser(description="Download MT5 hourly history into the price caches "
                                             "(prices only: no ledger, no orders)")
    ap.add_argument("universes", nargs="*", default=["fx", "metals"])
    ap.add_argument("--years", type=float, default=None,
                    help="history to load (default: MT5_HISTORY_YEARS, else 3)")
    ap.add_argument("--cache-dir", default="data")
    ap.add_argument("--env-file", default=".env")
    ap.add_argument("--passes", type=int, default=4,
                    help="rounds over symbols that got no bars yet (the terminal downloads in the background)")
    a = ap.parse_args(argv)
    load_env(a.env_file)
    years = float(a.years or os.environ.get("MT5_HISTORY_YEARS") or DEFAULT_YEARS)
    insts = {i.symbol: i for u in a.universes for i in universe(u) if i.asset_class in ("fx", "metal")}
    if not insts:
        print("no FX or gold instruments in", a.universes)
        return 2

    mt5 = _terminal()
    term, account = mt5.terminal_info(), mt5.account_info()
    server = str(getattr(account, "server", "") or os.environ.get("MT5_SERVER", ""))
    maxbars = int(getattr(term, "maxbars", 0) or 0)
    kind = "demo" if getattr(account, "trade_mode", None) == 0 else "REAL"
    access = "can trade" if getattr(account, "trade_allowed", False) else "read-only login"
    print(f"terminal: {getattr(term, 'path', '?')}")
    print(f"account: {getattr(account, 'login', '?')} on {server} ({kind} account, {access}), "
          f"max bars in chart {maxbars:,}")
    need = int(years * BARS_PER_YEAR)
    if maxbars and maxbars < need:
        print(f"\n{years:g} years need about {need:,} hourly bars, but the terminal keeps {maxbars:,}.\n"
              f"In MT5: Tools > Options > Charts > 'Max. bars in chart' -> Unlimited (or a larger\n"
              f"number), OK, then restart the terminal and run this again.")
        return 2

    pending = list(insts)
    for attempt in range(1, a.passes + 1):
        for symbol in list(pending):
            try:
                bars, _ = fetch_mt5_hourly(insts[symbol], a.cache_dir, years=years, mt5=mt5)
                print(f"  {symbol:<8} {len(bars):>7,} bars  {bars.index[0]:%Y-%m-%d} .. {bars.index[-1]:%Y-%m-%d %H:%M}")
                pending.remove(symbol)
            except BrokerError as e:
                print(f"  {symbol:<8} not yet: {e}")
        if not pending or attempt == a.passes:
            break
        print(f"waiting 60 s for the terminal to download {', '.join(pending)} (pass {attempt} of {a.passes})")
        time.sleep(60)

    gaps = history_gaps(list(insts), a.cache_dir, server=server, years=years)
    print(f"\ncaches in {Path(a.cache_dir).resolve()} (mt5_{server}_*):")
    for symbol in insts:
        marker = _cache_paths(a.cache_dir, server, symbol)[1]
        if symbol in pending:
            state = "FAILED: no bars (open its chart in MT5, then run this again)"
        elif symbol in gaps:
            state = "INCOMPLETE: history was cut, raise 'Max. bars in chart'"
        else:
            since = json.loads(marker.read_text()).get("history_from", "")[:10]
            state = f"complete (asked from {since})"
        print(f"  {symbol:<8} {state}")
    ok = not pending and not gaps
    print("\nall complete: ready to upload" if ok else "\nnot complete yet")
    return 0 if ok else 1


if __name__ == "__main__":
    import sys

    sys.exit(main())
