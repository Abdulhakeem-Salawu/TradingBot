"""SQLite ledger: paper sleeves, the active-strategy control state, and orders.

  positions / trades / equity   one paper sleeve per strategy x timeframe x
                                instrument, marked to market every bar
  slots                         which strategy each universe FOLLOWS for
                                actual trading, and in which mode
  switches                      every change to a slot, with a reason -- the
                                audit trail, and what the "followed" record
                                is reconstructed from
  orders                        every instruction, published target and
                                broker order, including ones refused by a
                                safety check
  requests                      actions queued for the MT5 executor (flatten)
  syncs                         each MT5 executor run: when, which account
                                type, equity -- so the VM can tell whether the
                                executor is alive
  nav                           broker account value snapshots (demo / live)

SQLite because it is a single file, needs no server, and survives the VM
rebooting -- which is all a bot on a free-tier e2-micro needs.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS positions (
    strategy TEXT, timeframe TEXT, symbol TEXT,
    position REAL NOT NULL, price REAL NOT NULL, bar_ts TEXT NOT NULL,
    equity REAL NOT NULL, started_ts TEXT NOT NULL,
    PRIMARY KEY (strategy, timeframe, symbol)
);
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy TEXT, timeframe TEXT, symbol TEXT, bar_ts TEXT,
    from_pos REAL, to_pos REAL, price REAL, cost REAL, created_at TEXT
);
CREATE TABLE IF NOT EXISTS equity (
    strategy TEXT, timeframe TEXT, symbol TEXT, bar_ts TEXT,
    gross REAL, cost REAL, financing REAL, net REAL, equity REAL,
    PRIMARY KEY (strategy, timeframe, symbol, bar_ts)
);
CREATE TABLE IF NOT EXISTS slots (
    universe TEXT PRIMARY KEY, strategy TEXT NOT NULL, timeframe TEXT NOT NULL,
    mode TEXT NOT NULL, capital REAL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS switches (
    id INTEGER PRIMARY KEY AUTOINCREMENT, universe TEXT NOT NULL,
    from_strategy TEXT, from_timeframe TEXT, from_mode TEXT,
    to_strategy TEXT, to_timeframe TEXT, to_mode TEXT,
    capital REAL, reason TEXT, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT, universe TEXT, strategy TEXT, timeframe TEXT,
    mode TEXT, symbol TEXT, target_pos REAL, current_units REAL, target_units REAL,
    order_units REAL, price REAL, status TEXT, broker_ref TEXT, message TEXT,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS nav (
    mode TEXT, ts TEXT, nav REAL, currency TEXT, PRIMARY KEY (mode, ts)
);
CREATE TABLE IF NOT EXISTS requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT, universe TEXT NOT NULL, action TEXT NOT NULL,
    mode TEXT NOT NULL, created_at TEXT NOT NULL, done_at TEXT, result TEXT
);
CREATE TABLE IF NOT EXISTS syncs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, host TEXT, account_mode TEXT,
    equity REAL, currency TEXT, summary TEXT, executor_mode TEXT
);
CREATE TABLE IF NOT EXISTS predictions (
    feed TEXT NOT NULL, symbol TEXT, timeframe TEXT, horizon INTEGER, bar_ts TEXT, prob REAL NOT NULL,
    model_until TEXT, model_verdict TEXT, outcome REAL, fwd_return REAL, created_at TEXT,
    PRIMARY KEY (feed, symbol, timeframe, horizon, bar_ts)
);
"""
# Predictions made before they carried their price feed: kept, tagged, never scored again.
LEGACY_FEED = "legacy"
PREDICTIONS_WITH_FEED = f"""CREATE TABLE predictions_new (
    feed TEXT NOT NULL, symbol TEXT, timeframe TEXT, horizon INTEGER, bar_ts TEXT, prob REAL NOT NULL,
    model_until TEXT, model_verdict TEXT, outcome REAL, fwd_return REAL, created_at TEXT,
    PRIMARY KEY (feed, symbol, timeframe, horizon, bar_ts)
);
INSERT INTO predictions_new (feed, symbol, timeframe, horizon, bar_ts, prob, model_until,
    model_verdict, outcome, fwd_return, created_at)
    SELECT '{LEGACY_FEED}', symbol, timeframe, horizon, bar_ts, prob, model_until,
    model_verdict, outcome, fwd_return, created_at FROM predictions;
DROP TABLE predictions;
ALTER TABLE predictions_new RENAME TO predictions;
"""
MIGRATIONS = (
    "ALTER TABLE syncs ADD COLUMN executor_mode TEXT",
)

MODES = ("signal", "demo", "live")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Ledger:
    def __init__(self, path: str = "state/ledger.db"):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.conn = sqlite3.connect(path, timeout=60)
        self.conn.row_factory = sqlite3.Row
        # WAL: readers never block the writer and the writer never blocks readers,
        # so `live.control status` works while a paper job is writing.
        try:
            self.conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.OperationalError:
            pass
        self.conn.executescript(SCHEMA)
        for sql in MIGRATIONS:   # columns added after a ledger was first created
            try:
                self.conn.execute(sql)
            except sqlite3.OperationalError:
                pass
        columns = {r["name"] for r in self.conn.execute("PRAGMA table_info(predictions)")}
        if "feed" not in columns:   # the feed joins the primary key: rebuild the table once
            self.conn.executescript("BEGIN;" + PREDICTIONS_WITH_FEED + "COMMIT;")

    # ----------------------------------------------------------- paper sleeves
    def position(self, strategy: str, timeframe: str, symbol: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM positions WHERE strategy=? AND timeframe=? AND symbol=?",
            (strategy, timeframe, symbol)).fetchone()

    def set_position(self, strategy, timeframe, symbol, position, price, bar_ts,
                     equity, started_ts) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO positions VALUES (?,?,?,?,?,?,?,?)",
            (strategy, timeframe, symbol, position, price, bar_ts, equity, started_ts))

    def add_trade(self, strategy, timeframe, symbol, bar_ts, from_pos, to_pos,
                  price, cost) -> None:
        self.conn.execute(
            "INSERT INTO trades (strategy, timeframe, symbol, bar_ts, from_pos, to_pos, "
            "price, cost, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (strategy, timeframe, symbol, bar_ts, from_pos, to_pos, price, cost, now_iso()))

    def add_bar(self, strategy, timeframe, symbol, bar_ts, gross, cost, financing,
                net, equity) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO equity VALUES (?,?,?,?,?,?,?,?,?)",
            (strategy, timeframe, symbol, bar_ts, gross, cost, financing, net, equity))

    def sleeves(self, strategy: str, timeframe: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM positions WHERE strategy=? AND timeframe=? ORDER BY symbol",
            (strategy, timeframe)).fetchall()

    def sleeve_keys(self) -> list[tuple[str, str]]:
        """Every (strategy, timeframe) that has been paper traded."""
        return [(r[0], r[1]) for r in self.conn.execute(
            "SELECT DISTINCT strategy, timeframe FROM positions ORDER BY strategy, timeframe")]

    def net_returns(self, strategy: str, timeframe: str, symbols: list[str],
                    column: str = "net"):
        """Per-bar values of one equity column for these sleeves, as a wide DataFrame."""
        import pandas as pd

        if column not in ("net", "gross", "cost", "financing"):
            raise ValueError(column)
        marks = ",".join("?" * len(symbols))
        rows = self.conn.execute(
            f"SELECT symbol, bar_ts, {column} AS v FROM equity WHERE strategy=? AND timeframe=? "
            f"AND symbol IN ({marks})", (strategy, timeframe, *symbols)).fetchall()
        if not rows:
            return pd.DataFrame()
        df = pd.DataFrame([dict(r) for r in rows])
        wide = df.pivot(index="bar_ts", columns="symbol", values="v").sort_index()
        wide.index = pd.to_datetime(wide.index)
        return wide

    def trade_count(self, strategy: str, timeframe: str, symbols: list[str],
                    since: str | None = None) -> int:
        marks = ",".join("?" * len(symbols))
        q = (f"SELECT COUNT(*) FROM trades WHERE strategy=? AND timeframe=? "
             f"AND symbol IN ({marks})")
        args = [strategy, timeframe, *symbols]
        if since:
            q += " AND bar_ts > ?"
            args.append(since)
        return int(self.conn.execute(q, args).fetchone()[0])

    def position_at(self, strategy: str, timeframe: str, symbol: str, ts: str) -> float:
        """Paper position held by a sleeve at wall-clock time `ts` (0 before it started)."""
        row = self.conn.execute(
            "SELECT to_pos FROM trades WHERE strategy=? AND timeframe=? AND symbol=? "
            "AND created_at <= ? ORDER BY id DESC LIMIT 1",
            (strategy, timeframe, symbol, ts)).fetchone()
        return float(row[0]) if row else 0.0

    # --------------------------------------------------------- control state
    def slot(self, universe: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM slots WHERE universe=?", (universe,)).fetchone()

    def slots(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM slots ORDER BY universe").fetchall()

    def set_slot(self, universe: str, strategy: str | None, timeframe: str | None,
                 mode: str | None, capital: float | None, reason: str) -> None:
        """Change what a universe follows. strategy=None deactivates it. Always audited."""
        if mode is not None and mode not in MODES:
            raise ValueError(f"mode must be one of {MODES}")
        old = self.slot(universe)
        self.conn.execute(
            "INSERT INTO switches (universe, from_strategy, from_timeframe, from_mode, "
            "to_strategy, to_timeframe, to_mode, capital, reason, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (universe, old["strategy"] if old else None, old["timeframe"] if old else None,
             old["mode"] if old else None, strategy, timeframe, mode, capital, reason, now_iso()))
        if strategy is None:
            self.conn.execute("DELETE FROM slots WHERE universe=?", (universe,))
        else:
            self.conn.execute("INSERT OR REPLACE INTO slots VALUES (?,?,?,?,?,?)",
                              (universe, strategy, timeframe, mode, capital, now_iso()))

    def switches(self, universe: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM switches WHERE universe=? ORDER BY id", (universe,)).fetchall()

    # ---------------------------------------------------------------- orders
    def add_order(self, universe, strategy, timeframe, mode, symbol, target_pos,
                  current_units, target_units, order_units, price, status,
                  broker_ref=None, message=None) -> None:
        self.conn.execute(
            "INSERT INTO orders (universe, strategy, timeframe, mode, symbol, target_pos, "
            "current_units, target_units, order_units, price, status, broker_ref, message, "
            "created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (universe, strategy, timeframe, mode, symbol, target_pos, current_units,
             target_units, order_units, price, status, broker_ref, message, now_iso()))

    def recent_orders(self, limit: int = 20) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM orders ORDER BY id DESC LIMIT ?", (limit,)).fetchall()

    # -------------------------------------------------- MT5 executor exchange
    def add_request(self, universe: str, action: str, mode: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO requests (universe, action, mode, created_at) VALUES (?,?,?,?)",
            (universe, action, mode, now_iso()))
        return int(cur.lastrowid)

    def pending_requests(self, action: str | None = None) -> list[sqlite3.Row]:
        q = "SELECT * FROM requests WHERE done_at IS NULL"
        args: tuple = ()
        if action:
            q += " AND action=?"
            args = (action,)
        return self.conn.execute(q + " ORDER BY id", args).fetchall()

    def complete_request(self, request_id: int, result: str) -> None:
        self.conn.execute("UPDATE requests SET done_at=?, result=? WHERE id=? AND done_at IS NULL",
                          (now_iso(), result, request_id))

    def record_execution(self, report: dict) -> int:
        """Store what the MT5 executor did. Returns the number of order records stored."""
        for o in report.get("orders", []):
            self.add_order(o.get("universe"), o.get("strategy"), o.get("timeframe"), o.get("mode"),
                           o.get("symbol"), o.get("target_pos"), o.get("current_units"),
                           o.get("target_units"), o.get("order_units"), o.get("price"),
                           o.get("status"), o.get("broker_ref"), o.get("message"))
        for rid, result in report.get("completed_requests", {}).items():
            self.complete_request(int(rid), result)
        acct = report.get("account") or {}
        if acct.get("equity") is not None and acct.get("mode"):
            self.add_nav("live" if acct["mode"] == "real" else "demo", acct["equity"],
                         acct.get("currency", ""))
        executor_mode = report.get("executor_mode") or (
            {"real": "live", "demo": "demo"}.get(acct.get("mode")))
        self.conn.execute(
            "INSERT INTO syncs (ts, host, account_mode, equity, currency, summary, executor_mode) "
            "VALUES (?,?,?,?,?,?,?)",
            (report.get("ts") or now_iso(), report.get("host"), acct.get("mode"),
             acct.get("equity"), acct.get("currency"), report.get("summary", "")[:2000],
             executor_mode))
        return len(report.get("orders", []))

    def last_sync(self, executor_mode: str | None = None) -> sqlite3.Row | None:
        """The newest executor report, optionally from the demo or the live executor only."""
        if executor_mode is None:
            return self.conn.execute("SELECT * FROM syncs ORDER BY id DESC LIMIT 1").fetchone()
        return self.conn.execute("SELECT * FROM syncs WHERE executor_mode=? ORDER BY id DESC LIMIT 1",
                                 (executor_mode,)).fetchone()

    def add_nav(self, mode: str, nav: float, currency: str) -> None:
        self.conn.execute("INSERT OR REPLACE INTO nav VALUES (?,?,?,?)",
                          (mode, now_iso(), nav, currency))

    # ------------------------------------------------------------- predictions
    def add_prediction(self, feed, symbol, timeframe, horizon, bar_ts, prob, model_until,
                       model_verdict) -> bool:
        """Record a model's probability for a bar of one price feed once; False if already recorded."""
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO predictions (feed, symbol, timeframe, horizon, bar_ts, prob, "
            "model_until, model_verdict, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (feed, symbol, timeframe, horizon, bar_ts, prob, model_until, model_verdict, now_iso()))
        return cur.rowcount == 1

    def open_predictions(self, feed: str, symbol: str, timeframe: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM predictions WHERE feed=? AND symbol=? AND timeframe=? AND outcome IS NULL "
            "ORDER BY bar_ts", (feed, symbol, timeframe)).fetchall()

    def resolve_prediction(self, feed, symbol, timeframe, horizon, bar_ts, outcome, fwd_return) -> None:
        self.conn.execute(
            "UPDATE predictions SET outcome=?, fwd_return=? WHERE feed=? AND symbol=? AND timeframe=? "
            "AND horizon=? AND bar_ts=?", (outcome, fwd_return, feed, symbol, timeframe, horizon, bar_ts))

    def resolved_predictions(self, feed: str, symbol: str, timeframe: str,
                             limit: int = 500) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM predictions WHERE feed=? AND symbol=? AND timeframe=? AND outcome IS NOT NULL "
            "ORDER BY bar_ts DESC LIMIT ?", (feed, symbol, timeframe, limit)).fetchall()

    def commit(self) -> None:
        self.conn.commit()

    def rollback(self) -> None:
        self.conn.rollback()
