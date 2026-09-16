"""Run every paper-trading job once: the scheduler-friendly equivalent of the VM's cron lines.

    python -m live.run_jobs                      # every job in JOBS, one after another
    python -m live.run_jobs --jobs "tsmom:fx:1h tsmom:metals:1h"
    python -m live.run_jobs --compare            # also send the daily comparison
    python -m live.run_jobs --compare-only       # only the daily comparison

Used on the Windows PC in the test phase (deploy/windows/setup_local.ps1 runs
it hourly with Task Scheduler). Every job runs every hour, daily ones
included: a job with no new closed bar does nothing, and a PC that was off at
the daily close catches up at the next run instead of waiting a day.

Each job runs in its own process with its own log in logs/, so one failing
instrument or a memory spike cannot take the others down. An OS file lock
stops a slow run from overlapping the next scheduled one; the OS releases it
if the run is killed or the PC shuts down mid-run.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from live.env import load_env

DEFAULT_JOBS = ("tsmom:fx:1h tsmom:fx:1d carry:fx:1d tsmom:metals:1h tsmom:metals:1d "
                "tsmom:crypto:1h tsmom:crypto:1d")
LOCK = Path("state/run_jobs.lock")
JOB_TIMEOUT_SECONDS = 45 * 60


def parse_jobs(text: str) -> list[tuple[str, str, str]]:
    jobs = []
    for item in text.split():
        parts = item.split(":")
        if len(parts) != 3:
            raise ValueError(f"job {item!r}: use strategy:universe:timeframe, e.g. tsmom:fx:1h")
        jobs.append((parts[0], parts[1], parts[2]))
    return jobs


def _acquire_lock():
    """An open, OS-locked handle, or None if another run holds the lock."""
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    handle = open(LOCK, "a+")
    try:
        if os.name == "nt":
            import msvcrt
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


def _release_lock(handle) -> None:
    try:
        if os.name == "nt":
            import msvcrt
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    except OSError:
        pass
    handle.close()


NOTIFY_MODES = ("changes", "problems", "always", "never")   # as live.signal_job


def notify_mode() -> str:
    """NOTIFY_PAPER for the paper jobs: always = a report every run, changes =
    only when a position moves or something is wrong, problems = only errors,
    stale data and orders to place by hand. An unknown value falls back
    rather than failing every job on a typo."""
    mode = os.environ.get("NOTIFY_PAPER", "changes").strip().lower()
    if mode not in NOTIFY_MODES:
        print(f"NOTIFY_PAPER={mode!r} is not one of {NOTIFY_MODES}; using changes")
        return "changes"
    return mode


def run(jobs, env_file: str, compare: bool, python: str = sys.executable,
        failed: list | None = None) -> int:
    """Run each job in turn; names of jobs that did not finish cleanly go to `failed`."""
    Path("logs").mkdir(exist_ok=True)
    print(f"== {datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S} UTC")
    failures = 0
    for strategy, uni, timeframe in jobs:
        name = f"{strategy}-{uni}-{timeframe}"
        started = datetime.now(timezone.utc)
        with open(Path("logs") / f"{name}.log", "a", encoding="utf-8") as log:
            log.write(f"\n===== {started:%Y-%m-%d %H:%M:%S} UTC =====\n")
            log.flush()
            try:
                rc = subprocess.run([python, "-m", "live.signal_job", "--strategy", strategy,
                                     "--universe", uni, "--timeframe", timeframe,
                                     "--notify", notify_mode(),
                                     "--env-file", env_file],
                                    stdout=log, stderr=subprocess.STDOUT,
                                    timeout=JOB_TIMEOUT_SECONDS).returncode
            except subprocess.TimeoutExpired:
                log.write(f"timed out after {JOB_TIMEOUT_SECONDS // 60} min\n")
                rc = -1
        secs = (datetime.now(timezone.utc) - started).total_seconds()
        print(f"{name:<22} {'ok' if rc == 0 else f'exit {rc}'}  ({secs:.0f}s, logs/{name}.log)")
        failures += rc != 0
        if rc != 0 and failed is not None:
            failed.append(f"{name} ({'timed out' if rc == -1 else f'exit {rc}'})")
    if compare:
        with open(Path("logs") / "compare.log", "a", encoding="utf-8") as log:
            log.write(f"\n===== {datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S} UTC =====\n")
            log.flush()
            rc = subprocess.run([python, "-m", "live.compare", "--send", "--env-file", env_file],
                                stdout=log,
                                stderr=subprocess.STDOUT).returncode
        print(f"{'compare':<22} {'ok' if rc == 0 else f'exit {rc}'}  (logs/compare.log)")
        if rc != 0 and failed is not None:
            failed.append(f"daily comparison (exit {rc})")
    return 1 if failures else 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Run every paper-trading job once")
    ap.add_argument("--jobs", default=None, help=f'space-separated strategy:universe:timeframe '
                                                 f'(default: JOBS in .env, else "{DEFAULT_JOBS}")')
    ap.add_argument("--env-file", default=".env")
    ap.add_argument("--compare", action="store_true", help="send the comparison report afterwards")
    ap.add_argument("--compare-only", action="store_true", help="only send the comparison report")
    a = ap.parse_args()
    load_env(a.env_file)
    if a.compare_only:
        return run([], a.env_file, compare=True)
    try:
        jobs = parse_jobs(a.jobs or os.environ.get("JOBS") or DEFAULT_JOBS)
    except ValueError as e:
        print(e)
        return 2
    lock = _acquire_lock()
    if lock is None:
        print(f"another run is still going ({LOCK} is locked) -- skipping")
        return 0
    try:
        return run(jobs, a.env_file, a.compare)
    finally:
        _release_lock(lock)


if __name__ == "__main__":
    sys.exit(main())
