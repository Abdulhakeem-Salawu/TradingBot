#!/usr/bin/env bash
# One-time setup for the paper-trading bot on a Debian 12 e2-micro VM.
#
# Run from anywhere inside the project, twice:
#   bash deploy/setup_vm.sh      # 1st: installs, creates .env, stops so you can fill it in
#   bash deploy/setup_vm.sh      # 2nd: validates, downloads history, installs cron
#
# Choose what is paper traded with JOBS, a space-separated list of
# strategy:universe:timeframe. Paper trade every candidate you might switch to --
# the daily comparison can only compare what is running:
#   JOBS="tsmom:crypto:1d tsmom:crypto:1h tsmom:fx:1d tsmom:fx:1h carry:fx:1d tsmom:metals:1d tsmom:metals:1h" \
#     bash deploy/setup_vm.sh
#
# Which strategy each universe FOLLOWS (and in which mode) is chosen afterwards
# with python -m live.control -- see deploy/GCP.md.
#
# Re-running is safe: the cron block is replaced, not duplicated.
set -euo pipefail

JOBS="${JOBS:-tsmom:crypto:1d tsmom:crypto:1h}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
PY="$ROOT/.venv/bin/python"

echo "== project: $ROOT"
echo "== jobs:    $JOBS"

# ---------------------------------------------------------------- packages
if [ ! -x "$PY" ]; then
  export DEBIAN_FRONTEND=noninteractive
  sudo -E apt-get update -y
  sudo -E apt-get install -y python3-venv python3-pip sqlite3 util-linux time
  python3 -m venv .venv
  .venv/bin/pip install --upgrade pip
fi

# 1 GB of RAM is tight for pip and for pandas on long hourly histories.
# A 1 GB swap file on the (free) 30 GB standard disk prevents OOM kills.
if ! sudo swapon --show | grep -q '/swapfile'; then   # swapon is in /sbin, not on a user's PATH
  echo "== adding 1 GB swap"
  sudo fallocate -l 1G /swapfile
  sudo chmod 600 /swapfile
  sudo mkswap /swapfile >/dev/null
  sudo swapon /swapfile
  grep -q '/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab >/dev/null
fi

.venv/bin/pip install -q -r requirements.txt
mkdir -p logs state data

# -------------------------------------------------------------------- .env
if [ ! -f .env ]; then
  cp .env.example .env
  chmod 600 .env
  echo
  echo "Created $ROOT/.env -- fill in the tokens (nano .env), then run this script again."
  exit 0
fi
chmod 600 .env
grep -qE '^TELEGRAM_TOKEN=.+' .env || echo "note: TELEGRAM_TOKEN empty -- messages will only go to logs/"

# ---------------------------------------------------------------- validate
echo "== validating the harness (a harness that fails its self-test must not trade, even on paper)"
"$PY" validate_harness.py --quick
echo "== validating switching and execution (fake broker, no network)"
"$PY" -m live.selftest

# ------------------------------------------------ download history, dry run
for job in $JOBS; do
  IFS=: read -r strategy universe timeframe <<<"$job"
  echo "== priming $strategy $universe $timeframe (first FX/gold download from Dukascopy takes a while; it is rate-limited)"
  "$PY" -m live.signal_job --strategy "$strategy" --universe "$universe" \
        --timeframe "$timeframe" --dry-run --notify never >/dev/null
done

# -------------------------------------------------------------------- cron
# Times are UTC (the GCE default). Jobs are idempotent, so an extra run is a
# no-op; that is used to cover the FX daily close moving between 21:00 and
# 22:00 UTC with US daylight saving. Minutes are staggered so jobs never
# compete for the 1 GB of RAM, and flock stops a slow run overlapping itself.
cron_lines() {
  local minute=5
  for job in $JOBS; do
    IFS=: read -r strategy universe timeframe <<<"$job"
    local name="$strategy-$universe-$timeframe"
    local cmd="cd $ROOT && flock -n state/$name.lock $PY -m live.signal_job --strategy $strategy --universe $universe --timeframe $timeframe"
    # Jobs message only on position changes, stale data, errors or orders; the
    # daily comparison below is the heartbeat, so nothing is sent twice.
    if [ "$timeframe" = "1h" ]; then
      echo "$minute * * * * $cmd >> logs/$name.log 2>&1"
    elif [ "$universe" = "crypto" ]; then
      echo "$((minute + 5)) 0 * * * $cmd >> logs/$name.log 2>&1"
    else
      echo "$((minute + 5)) 21,22 * * 1-5 $cmd >> logs/$name.log 2>&1"
    fi
    minute=$((minute + 4))
  done
  # One daily comparison of every paper strategy, after the FX/gold daily close.
  echo "45 22 * * * cd $ROOT && $PY -m live.compare --send >> logs/compare.log 2>&1"
  echo "0 3 * * 0 cd $ROOT && for f in logs/*.log; do tail -n 20000 \"\$f\" > \"\$f.tmp\" && mv \"\$f.tmp\" \"\$f\"; done"
}

BEGIN="# >>> signal-bot >>>"
END="# <<< signal-bot <<<"
{
  crontab -l 2>/dev/null | sed "/$BEGIN/,/$END/d" || true
  echo "$BEGIN"
  cron_lines
  echo "$END"
} | crontab -

echo "== installed cron:"
crontab -l | sed -n "/$BEGIN/,/$END/p"
echo
echo "Done. Paper trading starts at the next scheduled run. Check with:"
echo "  tail -f logs/*.log"
echo "  $PY -m live.compare"
echo "  $PY -m live.control status"
