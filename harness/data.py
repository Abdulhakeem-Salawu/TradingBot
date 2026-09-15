"""Data loading.

Sources, all free:

  Binance   crypto OHLCV. Public market data, no API key. The default host is
            data-api.binance.vision, Binance's market-data-only endpoint,
            because api.binance.com refuses US IPs (HTTP 451) -- including
            every Google Cloud free-tier region -- and is unreachable from
            some networks. Override with BINANCE_BASE_URL.
  Dukascopy FX and gold bid/ask history back to 2005 from Dukascopy Bank's free
            public datafeed. No account, no key. Hourly bars are built from
            monthly, daily and tick files depending on how recent they are,
            from BID prices; daily bars are rolled up from the same hourly bars.
  MT5       alternative FX and gold source on a machine running a MetaTrader 5
            terminal: FX_DATA_SOURCE=mt5 (see live/mt5_data.py).
  FRED      short-term interest rates for FX financing and carry. No key.
            Monthly OECD 3-month rates, extended with month-averaged daily
            overnight rates where OECD stopped (see SHORT_RATE_EXTENSIONS).

Caches are Parquet/CSV under data/ and are UPDATED, not just reused: each call
appends bars that closed since the last run. Bars that have not closed yet are
never stored -- a half-formed candle cached as final silently corrupts every
later run.

The synthetic generators exist so you can validate the harness itself. You
cannot trust a scorecard produced by a harness you have never tested, and the
only way to test a harness is to feed it data whose ground truth you already
know.
"""

from __future__ import annotations

import io
import json
import lzma
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd

from .calendar import session_dates, session_start
from .instruments import SHORT_RATE_EXTENSIONS, SHORT_RATE_SERIES, Instrument

BINANCE_DEFAULT = "https://data-api.binance.vision"
DUKASCOPY = "https://datafeed.dukascopy.com/datafeed"
DUKASCOPY_START = "2005-01-01"
FRED_CSV = "https://fred.stlouisfed.org/graph/fredgraph.csv"
_COLS = ["open_time", "open", "high", "low", "close", "volume", "close_time",
         "qav", "trades", "tbbav", "tbqav", "ignore"]


def _get(url: str, retries: int = 5, timeout: float = 30, **kwargs):
    """GET with exponential backoff on network errors, 429 and 5xx.

    A bot that runs unattended every hour will meet DNS blips and rate limits;
    one failed request must not kill the run. 4xx other than 429 is returned
    immediately because retrying a bad token or symbol never helps.
    """
    import requests  # local import so the module loads without network deps

    for attempt in range(retries):
        try:
            r = requests.get(url, timeout=timeout, **kwargs)
            if r.status_code != 429 and r.status_code < 500:
                return r
            err = f"HTTP {r.status_code}"
        except (requests.ConnectionError, requests.Timeout) as e:
            err = type(e).__name__
        if attempt == retries - 1:
            raise RuntimeError(f"GET {url} failed after {retries} attempts ({err})")
        time.sleep(2 ** attempt)


# ------------------------------------------------------------------- binance
def _binance_frame(rows: list, now_ms: int) -> pd.DataFrame:
    df = pd.DataFrame(rows, columns=_COLS)
    df = df[df["close_time"].astype("int64") < now_ms]   # closed bars only
    df["ts"] = pd.to_datetime(df["open_time"].astype("int64"), unit="ms", utc=True)
    return df.set_index("ts")[["open", "high", "low", "close", "volume"]].astype(float)


def _merge(*frames: pd.DataFrame) -> pd.DataFrame:
    frames = [f for f in frames if f is not None and len(f)]
    if not frames:
        return pd.DataFrame()
    df = pd.concat(frames).sort_index()
    return df[~df.index.duplicated(keep="last")]


def fetch_binance_ohlcv(symbol: str = "BTCUSDT", interval: str = "1h",
                        limit_total: int | None = 20000, cache_dir: str = "data",
                        base_url: str | None = None, offline: bool = False) -> pd.DataFrame:
    """Download OHLCV history, caching to Parquet and updating on every call.

    interval: 15m, 1h, 4h, 1d -- match this to your intended decision cadence.
    limit_total: minimum bars of history to hold; None means everything since
    the pair listed. Statistical power is the binding constraint on everything
    downstream, so get as much as you can.
    """
    Path(cache_dir).mkdir(exist_ok=True)
    cache = Path(cache_dir) / f"{symbol}_{interval}.parquet"
    df = pd.read_parquet(cache) if cache.exists() else pd.DataFrame()
    if offline:
        if not len(df):
            raise FileNotFoundError(f"--offline but no cache at {cache}")
        return df

    url = (base_url or os.environ.get("BINANCE_BASE_URL") or BINANCE_DEFAULT).rstrip("/")
    url += "/api/v3/klines"

    def page(**params) -> list:
        r = _get(url, params={"symbol": symbol, "interval": interval, **params})
        r.raise_for_status()
        time.sleep(0.25)  # stay well inside the weight limit
        return r.json()

    now_ms = int(time.time() * 1000)
    before = len(df)

    # Backfill older history until limit_total is met or the listing date is reached.
    while limit_total is None or len(df) < limit_total:
        want = 1000 if limit_total is None else min(1000, limit_total - len(df))
        params = {"limit": want}
        if len(df):
            params["endTime"] = int(df.index[0].timestamp() * 1000) - 1
        rows = page(**params)
        df = _merge(_binance_frame(rows, now_ms), df)
        print(f"  {symbol} {interval}: {len(df):,} bars", end="\r")
        if len(rows) < want:
            break

    # Bring the cache forward to the last closed bar.
    while len(df):
        rows = page(startTime=int(df.index[-1].timestamp() * 1000) + 1, limit=1000)
        new = _binance_frame(rows, now_ms)
        df = _merge(df, new)
        if len(rows) < 1000:
            break

    if len(df) != before:
        df.to_parquet(cache)
        print(f"  {symbol} {interval}: {len(df):,} bars saved to {cache}")
    return df


# ----------------------------------------------------------------- dukascopy
_CANDLE = np.dtype([("t", ">u4"), ("o", ">u4"), ("c", ">u4"), ("l", ">u4"), ("h", ">u4"),
                    ("v", ">f4")])
_TICK = np.dtype([("ms", ">u4"), ("ask", ">u4"), ("bid", ">u4"), ("askv", ">f4"),
                  ("bidv", ">f4")])
_OHLCV = ["open", "high", "low", "close", "volume"]


def _duka_point(symbol: str) -> float:
    """Integer price scale in the datafeed: 3 decimals for JPY pairs and gold, else 5."""
    return 1e3 if ("JPY" in symbol or symbol.startswith("XAU")) else 1e5


class _Throttle:
    """Space requests evenly across threads (Dukascopy blocks bursts with HTTP 429)."""

    def __init__(self, per_second: float):
        self.gap = 1.0 / per_second
        self.lock = threading.Lock()
        self.next_at = 0.0

    def wait(self) -> None:
        with self.lock:
            now = time.monotonic()
            at = max(now, self.next_at)
            self.next_at = at + self.gap
        time.sleep(max(0.0, at - now))


# Dukascopy returned 429 after ~25 requests at 1/second, so stay well below that.
_DUKA_THROTTLE = _Throttle(float(os.environ.get("DUKASCOPY_RPS", "0.5")))
_DUKA_BATCH = 24   # save progress after this many periods (two years of history)


def _duka_file(path: str, attempts: int = 6) -> bytes | None:
    """One datafeed file, decompressed. None = not published (404); b"" = published, empty.

    Dukascopy rate-limits hard and a burst earns a temporary block, so every
    request passes the shared throttle, and a 429 waits minutes, not seconds.
    Raises RuntimeError when retries are exhausted; the caller keeps progress.
    """
    import requests

    url = f"{DUKASCOPY}/{path}"
    err = ""
    for attempt in range(attempts):
        _DUKA_THROTTLE.wait()
        try:
            r = requests.get(url, timeout=20)
        except (requests.ConnectionError, requests.Timeout) as e:
            err = type(e).__name__
            time.sleep(2 ** attempt)
            continue
        if r.status_code == 404:
            return None
        if r.status_code == 200:
            return lzma.decompress(r.content) if r.content else b""
        err = f"HTTP {r.status_code}"
        if r.status_code == 429:
            wait = min(300, 60 * (attempt + 1))
            print(f"\n  Dukascopy rate limit (429): waiting {wait}s before retrying")
            time.sleep(wait)
        else:
            time.sleep(2 ** attempt)
    raise RuntimeError(f"GET {url} failed after {attempts} attempts ({err})")


def _duka_candles(raw: bytes, base: pd.Timestamp, point: float) -> pd.DataFrame:
    a = np.frombuffer(raw, dtype=_CANDLE)
    idx = base + pd.to_timedelta(a["t"].astype("int64"), unit="s")
    df = pd.DataFrame({"open": a["o"] / point, "high": a["h"] / point, "low": a["l"] / point,
                       "close": a["c"] / point, "volume": a["v"].astype(float)}, index=idx)
    return df[df["volume"] > 0]   # weekend and holiday filler rows have zero volume


def _hourly(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame(columns=_OHLCV)
    agg = df.resample("1h").agg({"open": "first", "high": "max", "low": "min", "close": "last",
                                 "volume": "sum"})
    return agg.dropna(subset=["close"])


def _duka_ticks(raw: bytes, hour: pd.Timestamp, point: float) -> tuple[pd.DataFrame, pd.DataFrame]:
    a = np.frombuffer(raw, dtype=_TICK)
    if not len(a):
        empty = pd.DataFrame(columns=_OHLCV)
        return empty, empty
    idx = hour + pd.to_timedelta(a["ms"].astype("int64"), unit="ms")
    bid = _hourly(pd.DataFrame({**{k: a["bid"] / point for k in ("open", "high", "low", "close")},
                                "volume": a["bidv"].astype(float)}, index=idx))
    ask = _hourly(pd.DataFrame({**{k: a["ask"] / point for k in ("open", "high", "low", "close")},
                                "volume": a["askv"].astype(float)}, index=idx))
    return bid, ask


def _with_spread(bid: pd.DataFrame, ask: pd.DataFrame | None) -> pd.DataFrame:
    """BID bars plus the closing spread as a fraction of price (NaN where no ASK was fetched).

    Prices are BID throughout -- history, recent days and today alike -- so
    there is never a half-spread jump where one file type hands over to
    another. Trading costs are modeled explicitly in harness/costs.py; the
    spread column only lets research compare Dukascopy's spreads to the model.
    """
    out = bid[_OHLCV].astype(float).copy()
    if ask is not None and len(ask):
        out["spread"] = (ask["close"].reindex(out.index) - out["close"]) / out["close"]
    else:
        out["spread"] = np.nan
    return out


def _weekend_closed(kind: str, start: pd.Timestamp) -> bool:
    """True for periods when FX and gold cannot trade, so Dukascopy only serves empty files.

    The market shuts around 21:00-22:00 UTC on Friday and reopens around
    21:00-22:00 UTC on Sunday depending on daylight saving. Saturday and
    Sunday before 20:00 UTC are closed under either clock, so those requests
    are skipped -- every request counts against Dukascopy's rate limit.
    """
    wd = start.weekday()
    if kind == "day":
        return wd == 5
    if kind == "hour":
        return wd == 5 or (wd == 6 and start.hour < 20)
    return False


def _duka_period(symbol: str, kind: str, start: pd.Timestamp,
                 with_ask: bool = True) -> pd.DataFrame | None:
    """Hourly BID bars for one datafeed period, or None if it is not published yet.

    kind "month": hourly candle files (history)
         "day":   minute candle files, rolled up to hours (the current month)
         "hour":  one tick file, rolled up to an hour (today)
    Months are 0-based in datafeed paths; days are not. ASK files are only
    fetched when with_ask is set, halving the requests for old history.
    """
    if _weekend_closed(kind, start):
        return pd.DataFrame(columns=_OHLCV + ["spread"])   # nothing trades; skip the request
    point = _duka_point(symbol)
    y, m0 = start.year, start.month - 1
    if kind == "hour":
        raw = _duka_file(f"{symbol}/{y}/{m0:02d}/{start.day:02d}/{start.hour:02d}h_ticks.bi5")
        return None if raw is None else _with_spread(*_duka_ticks(raw, start, point))
    if kind == "month":
        folder, name = f"{symbol}/{y}/{m0:02d}", "candles_hour_1.bi5"
    else:
        folder, name = f"{symbol}/{y}/{m0:02d}/{start.day:02d}", "candles_min_1.bi5"
    raw_bid = _duka_file(f"{folder}/BID_{name}")
    if raw_bid is None:
        return None
    raw_ask = _duka_file(f"{folder}/ASK_{name}") if with_ask else None
    bid = _duka_candles(raw_bid, start, point)
    ask = _duka_candles(raw_ask, start, point) if raw_ask else None
    if kind == "day":
        bid = _hourly(bid)
        ask = _hourly(ask) if ask is not None else None
    return _with_spread(bid, ask)


def _duka_plan(cursor: pd.Timestamp, now: pd.Timestamp) -> list[tuple[str, pd.Timestamp, bool]]:
    """Datafeed periods covering [cursor, last completed hour), coarsest files first.

    Monthly files are only used for months that ended over 10 days ago and
    daily files for days that ended over 3 hours ago, because both are
    published with a lag; the rest comes hour by hour from tick files. Spreads
    (ASK files) are fetched for the last year only.
    """
    tasks = []
    t = cursor.floor("h")
    until = now.floor("h")
    recent = now - pd.Timedelta(days=365)
    while t < until:
        month = t.replace(day=1, hour=0)
        next_month = month + pd.offsets.MonthBegin(1)
        day = t.floor("D")
        if next_month <= now - pd.Timedelta(days=10):
            tasks.append(("month", month, month >= recent))
            t = next_month
        elif day + pd.Timedelta(days=1) <= now - pd.Timedelta(hours=3):
            tasks.append(("day", day, True))
            t = day + pd.Timedelta(days=1)
        else:
            tasks.append(("hour", t, True))
            t = t + pd.Timedelta(hours=1)
    return tasks


def fetch_dukascopy_hourly(symbol: str, cache_dir: str = "data", offline: bool = False,
                           start: str | None = None, workers: int = 2,
                           now: pd.Timestamp | None = None) -> tuple[pd.DataFrame, pd.Timestamp]:
    """Hourly BID bars from Dukascopy's free datafeed, with the closing spread where fetched.

    Returns (bars, complete_until): every hour before complete_until has been
    fetched, including empty weekend hours. The cache and complete_until are
    saved together after every batch, so an interrupted first download resumes
    where it stopped, a run on Sunday does not re-download Saturday, and a
    file that is not published yet stops the update instead of leaving a gap.
    """
    Path(cache_dir).mkdir(exist_ok=True)
    cache = Path(cache_dir) / f"dukascopy_{symbol}_H1.parquet"
    marker = cache.with_suffix(".json")
    df = pd.read_parquet(cache) if cache.exists() else pd.DataFrame()
    done = (pd.Timestamp(json.loads(marker.read_text())["complete_until"])
            if marker.exists() else None)
    if offline:
        if not len(df) or done is None:
            raise FileNotFoundError(f"--offline but no cache at {cache}")
        return df, done

    now = now or pd.Timestamp.now(tz="UTC")
    # First download starts at DUKASCOPY_START (e.g. 2016-09-01); later runs resume from the cache.
    start = start or os.environ.get("DUKASCOPY_START") or DUKASCOPY_START
    cursor = done or pd.Timestamp(start, tz="UTC")
    tasks = _duka_plan(cursor, now)
    if not tasks:
        return df, cursor

    def run(task):
        try:
            return _duka_period(symbol, *task)
        except RuntimeError as e:   # retries exhausted: keep progress, resume here next run
            return e

    reached, stopped = cursor, False
    recent = now - pd.Timedelta(days=7)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for i in range(0, len(tasks), _DUKA_BATCH):
            batch = tasks[i:i + _DUKA_BATCH]
            results = list(pool.map(run, batch))
            pieces, batch_start = [], reached
            for (kind, t0, _), res in zip(batch, results):
                if isinstance(res, Exception):
                    print(f"\n  {symbol}: {res} -- saving progress up to {reached:%Y-%m-%d %H:%M}")
                    stopped = True
                    break
                if res is None and kind == "day":
                    # Daily file not out yet: fall back to that day's hourly tick files.
                    hours = [("hour", t0 + pd.Timedelta(hours=h), True) for h in range(24)]
                    hours = [h for h in hours if h[1] + pd.Timedelta(hours=1) <= now]
                    sub = [run(h) for h in hours]
                    stop = next((j for j, r in enumerate(sub)
                                 if r is None or isinstance(r, Exception)), None)
                    pieces += [r for r in sub[:stop] if len(r)]
                    if stop is not None:
                        reached, stopped = hours[stop][1], True
                        break
                    reached = t0 + pd.Timedelta(days=1)
                    continue
                if res is None and t0 >= recent:
                    stopped = True   # not published yet; retry on the next run
                    break
                if res is not None and len(res):
                    pieces.append(res)
                reached = (t0 + pd.offsets.MonthBegin(1) if kind == "month"
                           else t0 + pd.Timedelta(days=1) if kind == "day"
                           else t0 + pd.Timedelta(hours=1))
            new = pd.concat(pieces) if pieces else pd.DataFrame()
            if len(new):
                new = new[(new.index >= batch_start) & (new.index < reached)]
            df = _merge(df, new)
            if reached > batch_start:
                df.index.name = "ts"
                df.to_parquet(cache)
                marker.write_text(json.dumps({"complete_until": reached.isoformat()}))
                print(f"  {symbol} H1: {len(df):,} bars, complete until "
                      f"{reached:%Y-%m-%d %H:%M} UTC", flush=True)
            if stopped:
                break
    return df, reached


def hourly_to_daily(hourly: pd.DataFrame, asset_class: str,
                    complete_until: pd.Timestamp) -> pd.DataFrame:
    """Roll hourly bars into trading days (17:00 New York for FX and gold).

    Built from the same hourly bars as the 1h timeframe, so daily and hourly
    sleeves never disagree about prices. Only days whose last hour is inside
    complete_until are returned. Index is the day's UTC start, like every
    other loader.
    """
    if hourly.empty:
        return hourly
    dates = session_dates(hourly.index, "1h", asset_class)
    g = hourly.groupby(dates)
    daily = pd.DataFrame({"open": g["open"].first(), "high": g["high"].max(),
                          "low": g["low"].min(), "close": g["close"].last(),
                          "spread": g["spread"].last(), "volume": g["volume"].sum()})
    starts = pd.DatetimeIndex([session_start(d, asset_class) for d in daily.index])
    ends = pd.DatetimeIndex([session_start(d + pd.Timedelta(days=1), asset_class)
                             for d in daily.index])
    daily.index = starts
    daily.index.name = "ts"
    return daily[ends <= complete_until]

# ---------------------------------------------------------------------- fred
def fetch_fred_csv(series_id: str, cache_dir: str = "data", offline: bool = False,
                   max_age_hours: float = 24) -> pd.Series:
    """One FRED series as a float Series (FRED's native units, e.g. percent)."""
    Path(cache_dir).mkdir(exist_ok=True)
    cache = Path(cache_dir) / f"fred_{series_id}.csv"
    fresh = cache.exists() and (time.time() - cache.stat().st_mtime) < max_age_hours * 3600
    if not (offline or fresh):
        r = _get(FRED_CSV, params={"id": series_id})
        r.raise_for_status()
        cache.write_text(r.text)
    if not cache.exists():
        raise FileNotFoundError(f"--offline but no cache at {cache}")
    raw = pd.read_csv(io.StringIO(cache.read_text()))
    s = pd.to_numeric(raw.iloc[:, 1], errors="coerce")   # FRED marks gaps with "."
    s.index = pd.to_datetime(raw.iloc[:, 0])
    return s.dropna().rename(series_id)


def completed_month_means(daily: pd.Series) -> pd.Series:
    """Daily rates -> calendar-month averages dated the 1st, as OECD publishes them.

    The month of the last observation is still in progress, so it is dropped:
    a partial average would be revised every day, and a value dated the 1st
    would carry rates from later in the month back to its start.
    """
    s = daily.dropna().sort_index()
    if s.empty:
        return s
    monthly = s.resample("MS").mean().dropna()
    return monthly[monthly.index < s.index[-1].to_period("M").to_timestamp()]


def splice_rates(primary: pd.Series, extension: pd.Series) -> pd.Series:
    """`primary` through its last observation, then `extension` for later months only."""
    primary = primary.dropna().sort_index()
    later = extension.dropna().sort_index()
    if not primary.empty:
        later = later[later.index > primary.index[-1]]
    return pd.concat([primary, later]).rename(primary.name)


def load_short_rates(currencies, cache_dir: str = "data",
                     offline: bool = False) -> dict[str, pd.Series]:
    """Short rates as DECIMALS (0.05 = 5%), keyed by currency code.

    Currencies without a series (XAU) are omitted and treated as zero.
    Currencies in SHORT_RATE_EXTENSIONS get the monthly OECD series followed
    by completed-month averages of a maintained daily series.
    Warns when a series has gone stale, because forward-filling a rate that
    stopped updating a year ago quietly fakes the financing line.
    """
    out = {}
    for ccy in sorted(set(currencies)):
        if ccy not in SHORT_RATE_SERIES:
            continue
        s = fetch_fred_csv(SHORT_RATE_SERIES[ccy], cache_dir, offline)
        if ccy in SHORT_RATE_EXTENSIONS:
            daily = fetch_fred_csv(SHORT_RATE_EXTENSIONS[ccy], cache_dir, offline)
            s = splice_rates(s, completed_month_means(daily))
        s = s / 100.0
        age = (pd.Timestamp.now() - s.index[-1]).days
        if age > 180:
            print(f"  warning: {ccy} rate series last updated {s.index[-1].date()} "
                  f"({age} days ago); financing after that is forward-filled")
        out[ccy] = s
    return out


# --------------------------------------------------------------- dispatcher
def price_source(inst: Instrument) -> str:
    """Where this instrument's prices come from: FX and gold follow FX_DATA_SOURCE."""
    if inst.source == "dukascopy":
        choice = os.environ.get("FX_DATA_SOURCE", "dukascopy").strip().lower() or "dukascopy"
        if choice not in ("dukascopy", "mt5"):
            raise ValueError(f"FX_DATA_SOURCE={choice!r}: use dukascopy or mt5")
        return choice
    return inst.source


def load_prices(inst: Instrument, timeframe: str, cache_dir: str = "data",
                offline: bool = False) -> pd.DataFrame:
    """Full available history for an instrument at a timeframe."""
    gran = inst.granularity(timeframe)
    source = price_source(inst)
    if source == "binance":
        return fetch_binance_ohlcv(inst.symbol, gran, limit_total=None,
                                   cache_dir=cache_dir, offline=offline)
    if source in ("dukascopy", "mt5"):
        if source == "mt5":   # a MetaTrader 5 terminal on this machine (live/mt5_data.py)
            from live.mt5_data import fetch_mt5_hourly
            hourly, complete = fetch_mt5_hourly(inst, cache_dir, offline)
        else:
            hourly, complete = fetch_dukascopy_hourly(inst.data_symbol, cache_dir, offline)
        return hourly if timeframe == "1h" else hourly_to_daily(hourly, inst.asset_class, complete)
    raise ValueError(f"Unknown source {inst.source!r}")


# ------------------------------------------------------- synthetic generators
def synthetic_random_walk(n: int = 12000, seed: int = 0, drift: float = 0.0,
                          vol: float = 0.006, freq: str = "h") -> pd.DataFrame:
    """Pure noise. NO predictable structure exists.

    Any harness that reports an edge on this data is broken. This is the
    negative control.

    `drift` is the LOG drift. With drift=0, simple returns still have mean
    vol**2/2 per bar, which is a genuine long premium (about 11% a year at
    crypto-like daily vol). Pass drift=-vol**2/2 for a series whose simple
    returns have zero mean, so buy-and-hold has nothing to earn either.
    """
    rng = np.random.default_rng(seed)
    r = rng.normal(drift, vol, n)
    close = 30000 * np.exp(np.cumsum(r))
    return _to_ohlcv(close, rng, n, freq)


def synthetic_with_edge(n: int = 12000, seed: int = 0, beta: float = 0.12,
                        lookback: int = 6, vol: float = 0.006,
                        freq: str = "h") -> pd.DataFrame:
    """Noise plus a deliberately planted, learnable momentum effect.

    Next-bar return is an AR function of the trailing `lookback` returns:

        r[t] = beta * sum(r[t-lookback:t]) + noise

    A working harness MUST find this. This is the positive control.

    STATIONARITY: the sum of AR coefficients is beta * lookback. If that
    reaches 1.0 the process explodes and prices overflow to infinity -- which
    is not a subtle failure, but it IS an easy one to write by accident, so it
    is checked rather than assumed.
    """
    ar_sum = beta * lookback
    if ar_sum >= 0.95:
        raise ValueError(
            f"beta*lookback = {ar_sum:.2f} -- non-stationary, prices will explode. "
            f"Keep beta below {0.95 / lookback:.3f} for lookback={lookback}."
        )
    rng = np.random.default_rng(seed)
    r = np.zeros(n)
    r[:lookback] = rng.normal(0, vol, lookback)
    for t in range(lookback, n):
        r[t] = beta * r[t - lookback:t].sum() + rng.normal(0, vol)
    close = 30000 * np.exp(np.cumsum(r))
    if not np.all(np.isfinite(close)):
        raise RuntimeError("Synthetic prices overflowed despite the guard.")
    return _to_ohlcv(close, rng, n, freq)


def synthetic_trend(n: int = 6500, seed: int = 0, drift: float = 0.0006,
                    vol: float = 0.01, mean_duration: float = 200,
                    freq: str = "B") -> pd.DataFrame:
    """Noise plus slow-switching trends: the structure trend-following exploits.

    Each bar's expected LOG return is +drift or -drift; the sign persists for
    geometrically distributed stretches averaging `mean_duration` bars. An
    oracle that knew the regime would earn a Sharpe of about
    drift/vol*sqrt(periods per year); a rule that has to infer it from past
    prices earns a fraction of that. Positive control for S1.
    """
    rng = np.random.default_rng(seed)
    flips = rng.random(n) < 1.0 / mean_duration
    regime = np.where(np.cumsum(flips) % 2 == 0, 1.0, -1.0) * (1 if rng.random() < 0.5 else -1)
    r = regime * drift - vol ** 2 / 2 + rng.normal(0, vol, n)
    close = 100 * np.exp(np.cumsum(r))
    return _to_ohlcv(close, rng, n, freq)


def _to_ohlcv(close: np.ndarray, rng, n: int, freq: str = "h") -> pd.DataFrame:
    noise = np.abs(rng.normal(0, 0.002, n))
    idx = pd.date_range("2005-01-03", periods=n, freq=freq, tz="UTC")
    return pd.DataFrame({
        "open": close * (1 + rng.normal(0, 0.0005, n)),
        "high": close * (1 + noise),
        "low": close * (1 - noise),
        "close": close,
        "volume": np.abs(rng.normal(1000, 300, n)),
    }, index=idx)
