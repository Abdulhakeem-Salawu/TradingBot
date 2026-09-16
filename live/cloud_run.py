"""Cloud Run job entrypoint: one scheduled run of the bot on a machine with no disk of its own.

    python -m live.cloud_run hourly   # the scheduled run (default)
    python -m live.cloud_run retrain  # weekly: retrain the models on the cached prices (no MT5)
    python -m live.cloud_run probe    # start MT5 and report what the bot can see; saves nothing

hourly:
  1. take the state lease and restore data/, state/, models/ from STATE_URI
     (live/cloud_state.py);
  2. start MetaTrader 5 under Wine: virtual screen, wineserver, terminal (logged
     in from MT5_LOGIN / MT5_PASSWORD / MT5_SERVER), and the Python bridge
     (live/mt5_bridge.py), then wait until the bridge can initialize();
  3. run every paper job (live/run_jobs.py). If MT5 did not come up, jobs that
     need it are skipped rather than each waiting for an IPC timeout;
  4. at COMPARE_HOUR_UTC, send the daily comparison;
  5. record model predictions if models/ has any (live/ml_job.py, paper only);
  6. with EXECUTOR_ENABLED=true only, sync the demo/live slots to MT5;
  7. save the state (refused if another run changed it), keep a daily backup,
     release the lease, stop Wine.

retrain takes the same lease (waiting up to RETRAIN_WAIT_SECONDS, default 900,
for an hourly run to finish), trains on the price caches in the state and saves.

Every phase prints its duration: an hourly run must stay short to fit Cloud
Run's free tier (see deploy/CLOUD_RUN.md).

Settings come from the job's environment -- there is no .env file here:
  STATE_URI          gs://bucket/prefix (required for hourly)
  JOBS               strategy:universe:timeframe list (default: live.run_jobs)
  FX_DATA_SOURCE     mt5 (default here) or dukascopy
  MT5_LOGIN / MT5_SERVER, and MT5_PASSWORD from Secret Manager
  MT5_READY_SECONDS  how long to wait for the terminal (default 240)
  COMPARE_HOUR_UTC   hour whose run also sends the comparison (default 23)
  EXECUTOR_ENABLED   true to let this run trade demo/live slots (default false)
  MT5_MAX_BARS       bars per chart the terminal keeps: auto (default) is 10000,
                     or 100000 for a run that must download a whole history
  MEMORY_REPORT_AT   memory fractions at which to log the biggest processes and
                     files (default 0.7,0.9)
"""

from __future__ import annotations

import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

WINEPREFIX = os.environ.get("WINEPREFIX", "/opt/mt5")
# Broker-branded terminals install under their own name and ship only that
# broker's servers, so the name must match the image built by install_mt5.sh.
TERMINAL_NAME = os.environ.get("MT5_TERMINAL_NAME", "MetaTrader 5 EXNESS")
TERMINAL_WIN = rf"C:\Program Files\{TERMINAL_NAME}\terminal64.exe"
TERMINAL_DIR = Path(WINEPREFIX) / "drive_c" / "Program Files" / TERMINAL_NAME
WIN_PYTHON = r"C:\Python311\python.exe"
BRIDGE_WIN = r"Z:\app\live\mt5_bridge.py"
DISPLAY = ":99"
STARTUP_INI = Path("/tmp/mt5/startup.ini")
MT5_UNIVERSES = ("fx", "metals", "all")
LEASE_TTL = timedelta(minutes=40)       # longer than the job's task timeout
TAIL_BARS, FULL_BARS = 10_000, 100_000   # MT5 "Max bars in chart": recent bars only / whole history


def _load_secrets() -> None:
    """SECRETS_URI/bot.env into the environment, before anything reads a
    credential. Settings live in a private bucket object rather than Secret
    Manager, which bills per active version past the first six. Values already
    set on the job win, as with live/env.py's loader.
    """
    uri = os.environ.get("SECRETS_URI")
    if not uri:
        return
    from live.cloud_state import open_store
    from live.env import parse_line

    try:
        data, _ = open_store(uri).read("bot.env")
    except Exception as e:                      # never block a run on the store
        print(f"could not read {uri}/bot.env: {type(e).__name__}: {e}")
        return
    if data is None:
        print(f"no bot.env at {uri} -- MT5 and Telegram settings must come from the job")
        return
    names = []
    for line in data.decode("utf-8", errors="replace").splitlines():
        kv = parse_line(line)
        if kv:
            os.environ.setdefault(*kv)
            names.append(kv[0])
    print(f"settings from {uri}/bot.env: {', '.join(names)}")


def _defaults() -> None:
    _load_secrets()
    os.environ.setdefault("MT5_BACKEND", "wine")
    os.environ.setdefault("FX_DATA_SOURCE", "mt5")
    os.environ.setdefault("MT5_TERMINAL_PATH", TERMINAL_WIN)
    os.environ.setdefault("TARGETS_SOURCE", "ledger")
    os.environ.setdefault("WINEDEBUG", "-all")
    # No WebView2 (the terminal's built-in browser: Market, Signals, news). Its
    # processes took ~1.7 GB, more than everything else in the run together.
    os.environ.setdefault("WEBVIEW2_BROWSER_EXECUTABLE_FOLDER", r"C:\missing-webview2")
    os.environ["WINEPREFIX"] = WINEPREFIX
    os.environ["DISPLAY"] = DISPLAY


class Clock:
    def __init__(self):
        self.t0 = self.last = time.monotonic()
        self.memory = MemoryWatch()

    def lap(self, what: str) -> None:
        now = time.monotonic()
        print(f"-- {what}: {now - self.last:.0f}s (total {now - self.t0:.0f}s){self.memory.note()}", flush=True)
        self.last = now


def meminfo() -> tuple[float, float, float] | None:
    """(used, limit, cache) MB for this container, in-memory files included.

    Cloud Run gen2 enforces the job's memory limit through a cgroup v1 memory
    controller; /proc/meminfo describes the whole sandbox, which is larger.
    """
    cgroup = Path("/sys/fs/cgroup/memory")
    try:
        limit = int((cgroup / "memory.limit_in_bytes").read_text()) / 2**20
        used = int((cgroup / "memory.usage_in_bytes").read_text()) / 2**20
        stat = dict(line.split()[:2] for line in (cgroup / "memory.stat").read_text().splitlines()
                    if len(line.split()) >= 2) if (cgroup / "memory.stat").exists() else {}
        if limit < 2**30:          # an unlimited cgroup reports a huge number
            return used, limit, int(stat.get("total_cache", stat.get("cache", 0))) / 2**20
    except (OSError, ValueError):
        pass
    try:
        info = {k: int(v.split()[0]) for k, v in
                (line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines())}
        return ((info["MemTotal"] - info["MemAvailable"]) / 1024, info["MemTotal"] / 1024,
                info.get("Shmem", 0) / 1024)
    except (OSError, KeyError, ValueError):
        return None


def biggest_processes(n: int = 8) -> list[str]:
    rows = []
    for status in Path("/proc").glob("[0-9]*/status"):
        try:
            rss = next((int(line.split()[1]) for line in status.read_text().splitlines()
                        if line.startswith("VmRSS:")), 0)
            cmd = (status.parent / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
        except (OSError, ValueError):
            continue
        rows.append((rss, cmd.strip()[:100] or status.parent.name))
    return [f"{rss / 1024:6.0f} MB  {cmd}" for rss, cmd in sorted(rows, reverse=True)[:n]]


def written_since(since: float, roots, depth: int = 9, n: int = 8) -> list[str]:
    """Files created or changed since `since` (epoch), summed per directory `depth` deep."""
    sizes: dict[str, int] = {}
    for root in roots:
        for dirpath, _, files in os.walk(root):
            for name in files:
                try:
                    st = os.lstat(os.path.join(dirpath, name))
                except OSError:
                    continue
                if max(st.st_mtime, st.st_ctime) >= since:
                    key = "/".join(Path(dirpath).parts[:depth + 1]).replace("//", "/")
                    sizes[key] = sizes.get(key, 0) + st.st_size
    top = sorted(sizes.items(), key=lambda kv: -kv[1])[:n]
    total = sum(sizes.values())
    return [f"{total / 1e6:6.0f} MB  in total"] + [f"{size / 1e6:6.0f} MB  {path}" for path, size in top]


class MemoryWatch:
    """Samples memory every few seconds. The first time use passes each of
    MEMORY_REPORT_AT (70% and 90%), prints the biggest processes and the files
    written since the start, so a run killed for running out of memory leaves
    the cause in its log."""

    ROOTS = (WINEPREFIX, "/work", "/tmp")

    def __init__(self, every: float = 3.0, thresholds=None):
        self.started, self.every = time.time(), every
        if thresholds is None:
            thresholds = [float(x) for x in os.environ.get("MEMORY_REPORT_AT", "0.7,0.9").split(",")]
        self.thresholds = sorted(thresholds)
        self.peak = 0.0
        if meminfo() is not None:
            threading.Thread(target=self._watch, daemon=True).start()

    def _watch(self) -> None:
        while True:
            reading = meminfo()
            if reading is None:
                return
            used, total, cache = reading
            self.peak = max(self.peak, used)
            if self.thresholds and used >= self.thresholds[0] * total:
                while self.thresholds and used >= self.thresholds[0] * total:
                    self.thresholds.pop(0)
                lines = [f"MEMORY {used:.0f} of {total:.0f} MB in use (cache and files {cache:.0f} MB). "
                         f"Biggest processes:", *biggest_processes(),
                         "Written since the run started:",
                         *written_since(self.started, [r for r in self.ROOTS if Path(r).is_dir()])]
                print("\n  ".join(lines), flush=True)
            time.sleep(self.every)

    def note(self) -> str:
        reading = meminfo()
        if reading is None:
            return ""
        used, total, cache = reading
        peak, self.peak = max(self.peak, used), used
        return f", memory {used:.0f} MB, peak {peak:.0f} of {total:.0f} MB (cache and files {cache:.0f} MB)"


# ------------------------------------------------------------------- MT5 stack
def startup_ini(env=None, trading: bool = False, max_bars: int | None = None) -> str | None:
    """Terminal start-up config that logs in without any dialog, or None without credentials.

    A terminal that starts with no account opens the "open an account" wizard,
    which nobody can click on a virtual screen.
    """
    env = os.environ if env is None else env
    if not (env.get("MT5_LOGIN") and env.get("MT5_PASSWORD") and env.get("MT5_SERVER")):
        return None
    lines = ["[Common]", f"Login={int(env['MT5_LOGIN'])}", f"Password={env['MT5_PASSWORD']}",
             f"Server={env['MT5_SERVER']}", "NewsEnable=0", "ProxyEnable=0",
             "[Experts]", f"Enabled={int(trading)}", f"AllowLiveTrading={int(trading)}",
             "AllowDllImport=0", "Account=0", "Profile=0"]
    if max_bars:
        lines += ["[Charts]", f"MaxBars={int(max_bars)}", "PreloadCharts=0"]
    return "\r\n".join(lines) + "\r\n"


def sandbox_info() -> str:
    """The kernel matters: under Wine on Linux older than 5.11, MetaQuotes' copy
    protection stops MT5 with a "debugger has been found" dialog."""
    return f"sandbox: kernel {os.uname().release if hasattr(os, 'uname') else '?'}"


class MT5Stack:
    """Xvfb + wineserver + MT5 terminal + bridge, as child processes of this run."""

    def __init__(self, port: int = 8001):
        self.port = port
        self.procs: list[tuple[str, subprocess.Popen]] = []

    def _spawn(self, name: str, args: list[str]) -> None:
        log = open(f"/tmp/{name}.log", "ab")
        self.procs.append((name, subprocess.Popen(args, stdout=log, stderr=subprocess.STDOUT)))

    def start(self, trading: bool = False, max_bars: int | None = None) -> None:
        # No charts: each would load its symbol's whole history into the in-memory disk.
        for chart in (TERMINAL_DIR / "MQL5" / "Profiles" / "Charts" / "Default").glob("*.chr"):
            chart.unlink()
        self._spawn("xvfb", ["Xvfb", DISPLAY, "-screen", "0", "1024x768x24", "-nolisten", "tcp"])
        _wait_for(lambda: Path(f"/tmp/.X11-unix/X{DISPLAY[1:]}").exists(), 30, "virtual screen")
        self._spawn("wineserver", ["wineserver", "--foreground", "--persistent"])
        args = ["wine", TERMINAL_WIN]
        ini = startup_ini(trading=trading, max_bars=max_bars)
        if ini is not None:
            STARTUP_INI.parent.mkdir(parents=True, exist_ok=True)
            STARTUP_INI.write_bytes(b"\xff\xfe" + ini.encode("utf-16-le"))
            STARTUP_INI.chmod(0o600)
            args.append(r"/config:Z:\tmp\mt5\startup.ini")
        else:
            print("MT5_LOGIN / MT5_PASSWORD / MT5_SERVER not all set: terminal starts logged out")
        self._spawn("terminal", args)
        self._spawn("bridge", ["wine", WIN_PYTHON, "-u", BRIDGE_WIN, "--port", str(self.port)])
        _wait_for(self._port_open, 180, f"MT5 bridge on 127.0.0.1:{self.port}")

    def _port_open(self) -> bool:
        with socket.socket() as s:
            s.settimeout(1)
            return s.connect_ex(("127.0.0.1", self.port)) == 0

    def wait_ready(self, seconds: float) -> tuple[bool, str]:
        """initialize() through the bridge until it works or `seconds` pass."""
        from live.mt5_bridge import WineMT5
        from live.mt5_broker import initialize_kwargs

        deadline = time.monotonic() + seconds
        mt5, last = WineMT5(port=self.port), "not tried"
        try:
            while time.monotonic() < deadline:
                left_ms = int(max(10, min(60, deadline - time.monotonic())) * 1000)
                if mt5.initialize(timeout=left_ms, **initialize_kwargs()):
                    info, acct = mt5.terminal_info(), mt5.account_info()
                    summary = (f"terminal build {info.build}, connected={info.connected}, "
                               f"account {getattr(acct, 'login', None)} on "
                               f"{getattr(acct, 'server', None)}")
                    mt5.shutdown()
                    if info.connected and acct is not None:
                        return True, summary
                    last = summary
                else:
                    last = str(mt5.last_error())
                    self.dismiss_dialog()
                time.sleep(5)
            return False, last
        finally:
            STARTUP_INI.unlink(missing_ok=True)       # the password is only needed at start-up
            mt5.close()

    def dismiss_dialog(self) -> None:
        """Press Escape in the terminal window. A modal dialog -- the account
        wizard of a terminal with no account, a failed login -- keeps
        initialize() timing out, and nobody can click it on a virtual screen."""
        if not shutil.which("xdotool"):
            return
        try:
            subprocess.run(["xdotool", "search", "--name", "^MetaTrader 5", "windowfocus", "key", "Escape"],
                           capture_output=True, timeout=15, env={**os.environ, "DISPLAY": DISPLAY})
        except subprocess.SubprocessError:
            pass

    def diagnostics(self, lines: int = 25) -> str:
        out = []
        log = TERMINAL_DIR / "logs" / f"{datetime.now(timezone.utc):%Y%m%d}.log"
        if log.exists():
            text = log.read_bytes().decode("utf-16-le", errors="replace").lstrip("\ufeff")
            out.append(f"terminal log ({log.name}):")
            out += [f"  {line}" for line in text.splitlines()[-lines:]]
        for name, proc in self.procs:
            state = "running" if proc.poll() is None else f"exited {proc.returncode}"
            out.append(f"{name}: {state}")
        out.append(sandbox_info())
        try:
            tree = subprocess.run(["xwininfo", "-root", "-tree"], capture_output=True, text=True,
                                  timeout=10, env={**os.environ, "DISPLAY": DISPLAY}).stdout
            titles = sorted(set(re.findall(r'"([^"]+)"', tree)))
            out.append(f"windows: {titles}")
        except (OSError, subprocess.SubprocessError):
            pass
        return "\n".join(out)

    def screenshot(self) -> bytes | None:
        try:
            import io

            from PIL import ImageGrab

            buf = io.BytesIO()
            ImageGrab.grab(xdisplay=DISPLAY).save(buf, "PNG")
            return buf.getvalue()
        except Exception as e:  # noqa: BLE001 -- diagnostics only
            print(f"(no screenshot: {e})")
            return None

    def stop(self) -> None:
        STARTUP_INI.unlink(missing_ok=True)
        subprocess.run(["wineserver", "-k"], capture_output=True, timeout=60)
        for _, proc in reversed(self.procs):
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    proc.kill()


def _wait_for(check, seconds: float, what: str) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(1)
    raise RuntimeError(f"{what} did not come up within {seconds:.0f}s")


# ------------------------------------------------------------------------ runs
def split_jobs(jobs, mt5_ready: bool):
    """(runnable, skipped): without MT5, FX and gold jobs would only wait for IPC timeouts."""
    if mt5_ready or os.environ.get("FX_DATA_SOURCE", "mt5").lower() != "mt5":
        return list(jobs), []
    keep = [j for j in jobs if j[1] not in MT5_UNIVERSES]
    return keep, [j for j in jobs if j[1] in MT5_UNIVERSES]


def terminal_max_bars(jobs, data_dir: Path) -> int:
    """TAIL_BARS when every MT5 price cache holds its history (a fetch reads only
    the last days); FULL_BARS when one must be downloaded whole, which uses more
    memory and time."""
    setting = os.environ.get("MT5_MAX_BARS", "auto").strip().lower()
    if setting != "auto":
        return int(setting)
    import pandas as pd

    from harness.instruments import universe
    from live.mt5_data import history_gaps

    symbols = sorted({i.symbol for _, u, _ in jobs if u in MT5_UNIVERSES
                      for i in universe(u) if i.asset_class in ("fx", "metal")})
    # 10,000 hourly bars reach about 18 months back; allow for weekends and holidays
    gaps = history_gaps(symbols, str(data_dir), max_tail=pd.Timedelta(days=365))
    if gaps:
        print(f"MT5 history to download: {', '.join(gaps)} (terminal keeps {FULL_BARS:,} bars)")
        return FULL_BARS
    return TAIL_BARS


def _print_logs(logs: Path) -> None:
    for log in sorted(logs.glob("*.log")):
        print(f"\n----- {log.name}")
        print(log.read_text(encoding="utf-8", errors="replace").rstrip())


def with_state(label: str, work, wait_seconds: float = 0) -> int:
    """Lease + restore the state, run work(root, now, clock), save + release. Returns work's code."""
    from live import cloud_state

    uri = os.environ.get("STATE_URI")
    if not uri:
        print("STATE_URI is not set (gs://bucket/prefix)")
        return 2
    clock, now = Clock(), datetime.now(timezone.utc)
    root = Path(os.environ.get("WORK_DIR", "/work"))
    root.mkdir(parents=True, exist_ok=True)
    os.chdir(root)
    owner = f"{label} {os.environ.get('CLOUD_RUN_EXECUTION', socket.gethostname())}"
    print(f"== {now:%Y-%m-%d %H:%M:%S} UTC {owner}")

    store = cloud_state.open_store(uri)
    deadline = time.monotonic() + wait_seconds
    while True:
        try:
            session = cloud_state.acquire(store, root, owner, LEASE_TTL)
            break
        except cloud_state.StateConflict as e:
            if time.monotonic() >= deadline:
                print(f"not running: {e}")
                return 0
            time.sleep(30)
    clock.lap("restore state")

    rc = 1
    try:
        rc = work(root, now, clock)
    except Exception as e:  # noqa: BLE001 -- still save what was done
        print(f"run failed: {type(e).__name__}: {e}")
    except Terminated as e:
        print(f"run stopped ({e}): saving what was done")
        rc = 143
    finally:
        try:
            data = cloud_state.save(session)
            if label == "hourly" and now.hour == int(os.environ.get("COMPARE_HOUR_UTC", "23")):
                cloud_state.backup(store, data, f"{now:%Y-%m-%d}")
        except cloud_state.StateConflict as e:
            print(f"STATE NOT SAVED: {e}")
            rc = 1
        finally:
            cloud_state.release(session)
        clock.lap("save state")
    return rc


def hourly_work(root: Path, now: datetime, clock: Clock) -> int:
    from live.run_jobs import DEFAULT_JOBS, parse_jobs, run

    rc, stack, ready = 0, None, False
    jobs = parse_jobs(os.environ.get("JOBS") or DEFAULT_JOBS)
    trading = os.environ.get("EXECUTOR_ENABLED", "").lower() == "true"
    try:
        if os.environ.get("FX_DATA_SOURCE", "mt5").lower() == "mt5" or trading:
            stack = MT5Stack()
            try:
                stack.start(trading=trading, max_bars=terminal_max_bars(jobs, root / "data"))
                ready, detail = stack.wait_ready(float(os.environ.get("MT5_READY_SECONDS", "240")))
            except Exception as e:  # noqa: BLE001 -- crypto jobs still run
                detail = str(e)
            print(f"MT5 {'ready' if ready else 'NOT READY'}: {detail}")
            if not ready:
                print(stack.diagnostics())
                rc = 1
            clock.lap("start MT5")

        runnable, skipped = split_jobs(jobs, ready)
        for s, u, t in skipped:
            print(f"{s}-{u}-{t:<14} skipped: MT5 not ready")
        compare = now.hour == int(os.environ.get("COMPARE_HOUR_UTC", "23"))
        logs = root / "logs"
        shutil.rmtree(logs, ignore_errors=True)
        rc |= run(runnable, env_file=str(root / ".env"), compare=compare)
        _print_logs(logs)
        clock.lap("paper jobs" + (" + comparison" if compare else ""))

        if any((root / "models").glob("*.pkl")):
            rc |= subprocess.run([sys.executable, "-m", "live.ml_job", "predict"]).returncode
            clock.lap("predictions")

        if trading and ready:
            rc |= subprocess.run([sys.executable, "-m", "live.mt5_executor",
                                  "--targets-source", "ledger"]).returncode
            clock.lap("executor sync")
    finally:
        if stack is not None:
            stack.stop()
    return rc


def retrain_work(root: Path, now: datetime, clock: Clock) -> int:
    rc = subprocess.run([sys.executable, "-m", "live.ml_job", "train", "--offline"]).returncode
    clock.lap("train models")
    return rc


def hourly() -> int:
    return with_state("hourly", hourly_work)


def retrain() -> int:
    return with_state("retrain", retrain_work, float(os.environ.get("RETRAIN_WAIT_SECONDS", "900")))


def probe() -> int:
    """Start MT5, report what the bridge sees, then stop. Writes a screenshot next to the state."""
    clock = Clock()
    stack = MT5Stack()
    ok = False
    try:
        stack.start()
        clock.lap("start processes")
        ok, detail = stack.wait_ready(float(os.environ.get("MT5_READY_SECONDS", "240")))
        print(f"MT5 {'ready' if ok else 'NOT READY'}: {detail}")
        clock.lap("initialize")
        if ok:
            from live.mt5_bridge import WineMT5
            from live.mt5_broker import initialize_kwargs, resolve_symbol

            mt5 = WineMT5(port=stack.port)
            mt5.initialize(**initialize_kwargs())
            for canonical in ("EUR_USD", "XAU_USD"):
                try:
                    name = resolve_symbol(mt5, canonical, {})
                    print(f"{canonical} -> {name}: tick {mt5.symbol_info_tick(name)}")
                except Exception as e:  # noqa: BLE001
                    print(f"{canonical}: {e}")
            mt5.shutdown()
            mt5.close()
        print(stack.diagnostics())
        png = stack.screenshot()
        uri = os.environ.get("STATE_URI")
        if png and uri:
            from live import cloud_state

            name = f"debug/probe-{datetime.now(timezone.utc):%Y%m%dT%H%M%S}.png"
            cloud_state.open_store(uri).write(name, png, 0)
            print(f"screenshot saved as {uri.rstrip('/')}/{name}")
    except Exception as e:  # noqa: BLE001
        print(f"probe failed: {type(e).__name__}: {e}")
        print(stack.diagnostics())
    finally:
        stack.stop()
        clock.lap("stop")
    return 0 if ok else 1


class Terminated(BaseException):
    """SIGTERM: Cloud Run cancelled the execution or hit the task timeout. It
    allows 10 seconds before killing the container -- enough to save the state
    and release the lease, which would otherwise block runs for LEASE_TTL."""


def _on_sigterm(signum, frame) -> None:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)     # a second signal must not break the save
    raise Terminated(f"signal {signum}")


def main(argv=None) -> int:
    _defaults()
    signal.signal(signal.SIGTERM, _on_sigterm)
    task = (argv if argv is not None else sys.argv[1:] or ["hourly"])[0]
    tasks = {"hourly": hourly, "retrain": retrain, "probe": probe}
    if task not in tasks:
        print(f"unknown task {task!r}: choose from {', '.join(tasks)}")
        return 2
    return tasks[task]()


if __name__ == "__main__":
    sys.exit(main())
