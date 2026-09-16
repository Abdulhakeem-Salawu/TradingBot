#!/usr/bin/env bash
# Google Cloud setup for the Cloud Run bot (deploy/CLOUD_RUN.md). Every
# resource it creates is new and named signal-bot*; nothing else in the
# project is modified. Run from the project root (Git Bash or Cloud Shell):
#
#   bash deploy/cloudrun/setup.sh resources   # image repo, state bucket, service account
#   bash deploy/cloudrun/setup.sh images      # wine-base, MT5 bake (Cloud Run job), mt5-base, bot
#   bash deploy/cloudrun/setup.sh bot-image   # rebuild only the bot image after code changes
#   bash deploy/cloudrun/setup.sh job         # create/update the jobs: signal-bot (hourly), signal-bot-retrain
#   bash deploy/cloudrun/setup.sh settings    # set one value (MT5 password, Telegram token/chat) in the bucket
#   bash deploy/cloudrun/setup.sh schedule    # Cloud Scheduler: hourly run + weekly retraining
#   bash deploy/cloudrun/setup.sh unschedule  # pause: delete both triggers, keep everything else
#
# PREFIX picks the state folder in the bucket: staging while testing, prod for real.
# `job` keeps the folder and the executor switch the jobs already have unless
# PREFIX=... or EXECUTOR_ENABLED=... is given; new jobs start on staging, not trading.
set -euo pipefail
export MSYS2_ARG_CONV_EXCL="--args="   # Git Bash: keep --args=/opt/... a Linux path

PROJECT="${PROJECT:-$(gcloud config get-value project 2>/dev/null)}"
REGION="${REGION:-us-central1}"
BUCKET="${BUCKET:-$PROJECT-signal-bot}"
PREFIX="${PREFIX:-}"
JOB="${JOB:-signal-bot}"
SA="signal-bot-runner@$PROJECT.iam.gserviceaccount.com"
REPO="$REGION-docker.pkg.dev/$PROJECT/signal-bot"
SETTINGS="bot.env"       # MT5 password and Telegram settings, in gs://BUCKET/secrets/
HERE="$(cd "$(dirname "$0")" && pwd)"
g() { gcloud --project="$PROJECT" "$@"; }
job_env() {   # a variable's current value on the hourly job; empty if the job or the variable is missing
  local env="spec.template.spec.template.spec.containers[0].env"
  (g run jobs describe "$JOB" --region="$REGION" --flatten="$env" \
     --format="csv[no-heading]($env.name,$env.value)" 2>/dev/null || true) | sed -n "s/^$1,//p"
}

case "${1:-}" in
settings)
  # The MT5 password, the Telegram token and chat id live in one private file in
  # the bot's own bucket, read at the start of every run. Secret Manager charges
  # for every active version beyond six and this billing account is past that.
  # You type the value; it is never stored in this repo or passed on a command line.
  file="$(mktemp)"; new="$(mktemp)"
  trap 'rm -f "$file" "$new"' EXIT
  g storage cp "gs://$BUCKET/secrets/$SETTINGS" "$file" 2>/dev/null || true
  echo "keys in gs://$BUCKET/secrets/$SETTINGS: $(sed -n 's/^\([A-Z_][A-Z_0-9]*\)=.*/\1/p' "$file" | tr '\n' ' ')"
  read -rp "key to set (MT5_PASSWORD, MT5_LOGIN, MT5_SERVER, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID): " key
  read -rsp "value for $key (hidden): " value
  echo
  [ -n "$key" ] && [ -n "$value" ] || { echo "nothing entered"; exit 1; }
  { grep -v "^$key=" "$file" 2>/dev/null || true; printf '%s=%s\n' "$key" "$value"; } > "$new"
  g storage cp "$new" "gs://$BUCKET/secrets/$SETTINGS"
  echo "$key saved; the next run uses it."
  ;;
resources)
  g artifacts repositories describe signal-bot --location="$REGION" >/dev/null 2>&1 ||
    g artifacts repositories create signal-bot --repository-format=docker --location="$REGION" \
      --description="signal bot Cloud Run images"
  cat > /tmp/ar_cleanup.json <<'EOF'
[{"name": "keep-3-recent", "action": {"type": "Keep"}, "mostRecentVersions": {"keepCount": 3}},
 {"name": "delete-older", "action": {"type": "Delete"}, "condition": {"olderThan": "7d"}}]
EOF
  g artifacts repositories set-cleanup-policies signal-bot --location="$REGION" \
    --policy=/tmp/ar_cleanup.json --no-dry-run >/dev/null
  # Soft delete off: the state is overwritten every hour, and soft-deleted
  # copies would be kept (and billed) for a week.
  g storage buckets describe "gs://$BUCKET" >/dev/null 2>&1 ||
    g storage buckets create "gs://$BUCKET" --location="$REGION" --default-storage-class=STANDARD \
      --uniform-bucket-level-access --public-access-prevention --soft-delete-duration=0s
  cat > /tmp/bucket_lifecycle.json <<'EOF'
{"rule": [
  {"action": {"type": "Delete"}, "condition": {"age": 8, "matchesPrefix": ["staging/backups/", "prod/backups/"]}},
  {"action": {"type": "Delete"}, "condition": {"age": 3, "matchesPrefix": ["staging/debug/", "prod/debug/"]}},
  {"action": {"type": "Delete"}, "condition": {"age": 1, "matchesPrefix": ["build/"]}}]}
EOF
  g storage buckets update "gs://$BUCKET" --lifecycle-file=/tmp/bucket_lifecycle.json
  g iam service-accounts describe "$SA" >/dev/null 2>&1 ||
    g iam service-accounts create signal-bot-runner --display-name="signal bot Cloud Run job"
  g storage buckets add-iam-policy-binding "gs://$BUCKET" --member="serviceAccount:$SA" \
    --role=roles/storage.objectUser >/dev/null
  echo "resources ready: $REPO, gs://$BUCKET, $SA"
  ;;
images)
  g builds submit --region="$REGION" --config="$HERE/cloudbuild-wine.yaml" .
  # MT5 installs only on Cloud Run (see Dockerfile.wine). The bake writes
  # ~3 GB to the in-memory disk, hence 16 GiB; it runs for about 15 minutes, once.
  g run jobs deploy "$JOB-bake" --region="$REGION" --image="$REPO/wine-base:latest" \
    --execution-environment=gen2 --cpu=4 --memory=16Gi --task-timeout=60m --max-retries=0 \
    --tasks=1 --parallelism=1 --service-account="$SA" --command=bash --args=/opt/bake_mt5.sh \
    --set-env-vars="BAKE_URI=gs://$BUCKET/build/mt5-prefix.tar.gz"
  g run jobs execute "$JOB-bake" --region="$REGION" --wait
  g builds submit --region="$REGION" --config="$HERE/cloudbuild-mt5.yaml" .
  g builds submit --region="$REGION" --config="$HERE/cloudbuild.yaml" .
  ;;
bot-image)
  g builds submit --region="$REGION" --config="$HERE/cloudbuild.yaml" .
  ;;
job)
  # A redeploy must not move a production job back to staging or stop its trading.
  STATE_URI="${PREFIX:+gs://$BUCKET/$PREFIX}"
  STATE_URI="${STATE_URI:-$(job_env STATE_URI)}"
  STATE_URI="${STATE_URI:-gs://$BUCKET/staging}"
  EXECUTOR_ENABLED="${EXECUTOR_ENABLED:-$(job_env EXECUTOR_ENABLED)}"
  EXECUTOR_ENABLED="${EXECUTOR_ENABLED:-false}"
  NOTIFY_PAPER="${NOTIFY_PAPER:-$(job_env NOTIFY_PAPER)}"
  NOTIFY_PAPER="${NOTIFY_PAPER:-problems}"   # paper jobs message only on errors and hand orders
  echo "state: $STATE_URI, executor enabled: $EXECUTOR_ENABLED, paper messages: $NOTIFY_PAPER"
  # --update-env-vars keeps MT5_LOGIN / MT5_SERVER that you set yourself.
  g run jobs deploy "$JOB" --region="$REGION" --image="$REPO/bot:latest" \
    --execution-environment=gen2 --cpu=1 --memory=2Gi --task-timeout=30m --max-retries=0 \
    --tasks=1 --parallelism=1 --service-account="$SA" --args=hourly \
    --update-env-vars="STATE_URI=$STATE_URI,SECRETS_URI=gs://$BUCKET/secrets,FX_DATA_SOURCE=mt5,MT5_HISTORY_YEARS=10,MT5_SERVER_TZ=auto,EXECUTOR_MODE=demo,EXECUTOR_ENABLED=$EXECUTOR_ENABLED,ALLOW_LIVE_TRADING=false,COMPARE_HOUR_UTC=23,NOTIFY_PAPER=$NOTIFY_PAPER"
  # Retraining needs no MT5: it trains on the price caches the hourly runs keep.
  g run jobs deploy "$JOB-retrain" --region="$REGION" --image="$REPO/bot:latest" \
    --execution-environment=gen2 --cpu=1 --memory=2Gi --task-timeout=45m --max-retries=0 \
    --tasks=1 --parallelism=1 --service-account="$SA" --args=retrain \
    --update-env-vars="^|^STATE_URI=$STATE_URI|SECRETS_URI=gs://$BUCKET/secrets|FX_DATA_SOURCE=mt5|ML_TARGETS=fx:1h metals:1h crypto:1h|ML_HORIZON=6"
  ;;
schedule)
  g services enable cloudscheduler.googleapis.com
  schedule_job() {   # job name, cron (UTC)
    g run jobs add-iam-policy-binding "$1" --region="$REGION" --member="serviceAccount:$SA" \
      --role=roles/run.invoker >/dev/null
    local uri="https://run.googleapis.com/v2/projects/$PROJECT/locations/$REGION/jobs/$1:run"
    local verb=create
    g scheduler jobs describe "$1-trigger" --location="$REGION" >/dev/null 2>&1 && verb=update
    g scheduler jobs "$verb" http "$1-trigger" --location="$REGION" --schedule="$2" --time-zone=Etc/UTC \
      --uri="$uri" --http-method=POST --oauth-service-account-email="$SA" \
      --oauth-token-scope=https://www.googleapis.com/auth/cloud-platform
    echo "scheduled $1: $2 (UTC)"
  }
  schedule_job "$JOB" "2 * * * *"                 # every hour at :02
  schedule_job "$JOB-retrain" "30 0 * * 0"        # Sundays 00:30, between hourly runs
  ;;
unschedule)
  for s in "$JOB-trigger" "$JOB-retrain-trigger"; do
    g scheduler jobs delete "$s" --location="$REGION" --quiet || true
  done
  ;;
*)
  sed -n '2,15p' "$0"
  exit 2
  ;;
esac
