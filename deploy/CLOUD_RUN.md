# The bot on Cloud Run (no VM, no PC)

One **Cloud Run job** wakes at minute 2 of every hour, does one run of the bot
and exits. It keeps nothing between runs except one archive in **Cloud
Storage**, so there is no machine to keep on and no external IP address to pay
for.

```
Cloud Scheduler (hourly, :02 UTC)
  -> Cloud Run job "signal-bot" (1 vCPU, 2 GiB, gen2)
       1. lease + restore  gs://PROJECT-signal-bot/PREFIX/state.tar.gz
       2. Xvfb -> Wine -> MT5 terminal (logs in) + live/mt5_bridge.py
       3. live.run_jobs: every paper job; comparison in the 23:02 run
       4. live.ml_job predict: one prediction per model per new bar (if models exist)
       5. live.mt5_executor (only with EXECUTOR_ENABLED=true)
       6. save state (refused if it changed meanwhile), daily backup, release lease

Cloud Scheduler (Sundays 00:30 UTC)
  -> Cloud Run job "signal-bot-retrain" (1 vCPU, 2 GiB, no MT5)
       lease + restore, live.ml_job train --offline on the price caches, save, release
```

Entry point: `live/cloud_run.py` (tasks `hourly`, `retrain`, `probe`). State
handling: `live/cloud_state.py`. Models: `live/ml_job.py`.
Images: `deploy/cloudrun/`. Setup: `deploy/cloudrun/setup.sh`.

## Free tier budget

Allowances are per billing account and shared with the other services in the
project. Measured 2026-09-14 (last 30 days of the existing services), plus this bot:

| | Free per month | Existing services | This bot |
|---|---|---|---|
| Cloud Run vCPU-seconds (jobs / instance-based) | 240,000 | ~41,000 | 730 runs x run length at 1 vCPU |
| Cloud Run GiB-seconds | 450,000 | ~112,000 | 730 runs x run length x 2 GiB |
| Cloud Storage, us-central1 regional | 5 GB | 3.2 GB | ~30 MB state + 7 daily backups |
| Cloud Storage operations (A / B) | 5,000 / 50,000 | a few hundred | ~2,200 / ~1,500 |
| Cloud Scheduler jobs | 3 | 0 | 2 (hourly, weekly retraining) |

Retraining adds about 4 runs a month; measured locally, 7 models train in
~30 s, so it is a small share of the budget.

**Each hourly run must stay under about 2.5 minutes** (2 GiB) to leave the
existing services room. Measured 2026-09-15 on a copy of the PC's state: 140 s
(restore 1 s, MT5 start 45-60 s, the 7 paper jobs ~75 s, save 4 s), memory peak
1.8 of 2 GiB. That is ~102,000 vCPU-s and ~204,000 GiB-s a month.

Every run prints `-- phase: Ns (total Ns), memory N MB, peak N of 2048 MB`
lines; check them in the job's logs after changes. 2 GiB is the smallest size
that fits: do not lower the memory.

## Setup (once)

From the project root, with gcloud logged in to the project:

```bash
bash deploy/cloudrun/setup.sh resources   # repo, bucket (soft delete off), service account
bash deploy/cloudrun/setup.sh images      # ~30 min; the MT5 base image is rarely rebuilt
bash deploy/cloudrun/setup.sh job         # both jobs; new jobs use staging and do not trade
```

The images are built in four steps, because MetaTrader 5 will not install in
Cloud Build. Under Wine on Linux kernels older than 5.11, its copy protection
reports "a debugger has been found", and Cloud Build workers run 5.10. Cloud Run
runs 6.x:

1. `wine-base` (Cloud Build): Debian + WineHQ staging + virtual screen.
2. `signal-bot-bake` (Cloud Run job, 4 vCPU / 16 GiB, once): installs MT5 and
   Windows Python into the Wine prefix and uploads it to `gs://PROJECT-signal-bot/build/`.
3. `mt5-base` (Cloud Build): wine-base + that prefix. The archive is deleted
   afterwards; a lifecycle rule removes anything left in `build/` after a day.
4. `bot` (Cloud Build): Linux Python, requirements and the code.

`gcloud builds submit` uploads only what `.gcloudignore` allows: `deploy/cloudrun/`,
`harness/`, `live/` and `requirements.txt`. Your `.env`, `data/`, `state/` and
logs never leave the PC.

### Log the terminal in

The terminal needs your MT5 account. Enter these yourself; they are not
stored in the code or the images.

1. Put the password in the settings file (it asks for the value and hides it):
   ```bash
   bash deploy/cloudrun/setup.sh settings     # key: MT5_PASSWORD
   ```
   For price data only, the account's **investor (read-only) password** is enough;
   trading needs the master password.
2. The same command sets `MT5_LOGIN` and `MT5_SERVER`, or put them on the job:
   ```bash
   gcloud run jobs update signal-bot --region=us-central1 --update-env-vars=MT5_LOGIN=<number>,MT5_SERVER=<server>
   ```

The settings file is one private object, `gs://PROJECT-signal-bot/secrets/bot.env`,
that every run reads at start (`SECRETS_URI`). Secret Manager would do the same job
but charges for every active version beyond six, and this billing account is past
that; anything set on the job itself still wins over the file, so a secret can be
attached instead (`--update-secrets=MT5_PASSWORD=...`) where that is worth paying for.
Only the service account and project administrators can read the bucket.

Without a logged-in account the Python package cannot talk to the terminal
under Wine (`initialize` fails with `IPC timeout`), so FX and gold jobs are
skipped. The password is written to a start-up file only while the terminal
starts, and deleted as soon as it is up. Use the investor password while the
executor is off; the master password is needed only to trade.

### Test

```bash
gcloud run jobs execute signal-bot --region=us-central1 --args=probe --wait
```

The probe starts MT5, logs in, reads EURUSD and XAUUSD, prints the terminal
log, and saves a screenshot of the virtual screen under `PREFIX/debug/`
(deleted after 3 days). Then one real run:

```bash
gcloud run jobs execute signal-bot --region=us-central1 --wait
```

Read the output in Cloud Run > Jobs > signal-bot > Logs. Check the phase times
against the budget above. When memory passes 70% and 90% of the limit, the log
also lists the biggest processes and the files written since the start
(`MEMORY ...` lines), so a run killed for running out of memory shows why. For
other thresholds, per execution:

```bash
gcloud run jobs execute signal-bot --region=us-central1 "--update-env-vars=^@^MEMORY_REPORT_AT=0.5,0.75,0.9"
```

Train the models once, so the hourly runs start predicting:

```bash
gcloud run jobs execute signal-bot-retrain --region=us-central1 --wait
```

Each model is scored out of sample before it is fitted on all history; the
verdict is saved next to it (`models/*.json`). Predictions are paper only:
they go to the ledger's `predictions` table and are marked right or wrong once
their horizon has passed (`python -m live.ml_job report` on a restored state).

### Telegram reports

Telegram only delivers messages from a bot, so the bot needs one of its own (free):

1. In Telegram, open **@BotFather**, send `/newbot`, pick a name and a username
   ending in `bot`. It replies with the bot's token.
2. Open your new bot and press **Start** (a bot cannot write to you first).
3. Save the token (it asks for the value and hides it):
   ```bash
   bash deploy/cloudrun/setup.sh settings     # key: TELEGRAM_TOKEN
   ```
4. Find your chat id; it lists every chat that has written to the bot:
   ```bash
   gcloud run jobs execute signal-bot --region=us-central1 --args=telegram --wait
   ```
   Check your name next to `TELEGRAM_CHAT_ID=...` in the log, then save it and
   send a test message:
   ```bash
   bash deploy/cloudrun/setup.sh settings     # key: TELEGRAM_CHAT_ID
   gcloud run jobs execute signal-bot --region=us-central1 --args=telegram --wait
   ```

What arrives:

| Message | When |
|---|---|
| Daily comparison of the strategies | the 23:02 UTC run |
| Demo/live orders: filled, rejected, or blocked by a safety check | when the executor acts |
| Errors and stale data in a paper job | as they happen |
| "Cloud Run ... had problems" | a run where MT5 did not come up, a job failed, the state was not saved, or the run was skipped |
| Every paper position change | only with `NOTIFY_PAPER=changes` (about 3-5 messages an hour) |

`NOTIFY_PAPER` is `problems` on the job; change it with
`NOTIFY_PAPER=changes bash deploy/cloudrun/setup.sh job`. A run that is killed
outright (out of memory) cannot send its alert; that shows only in the job's logs.

### Start the schedule

```bash
bash deploy/cloudrun/setup.sh schedule    # hourly at :02, retraining Sundays 00:30 (UTC)
```

## Moving from the PC (cutover)

The PC and the cloud must not both run the same ledger or trade the same
account.

Done on 2026-09-15 at 20:45 UTC; the steps, for a repeat (for example to Exness):

1. Disable the PC's scheduled tasks ("SignalBot Paper Jobs", "SignalBot Daily Compare",
   "SignalBot MT5 Executor (demo)") and wait until no bot process runs.
2. Pack the PC state and upload it as the production state (the upload refuses
   to overwrite an existing one):
   ```bash
   .venv-windows/Scripts/python.exe -m live.cloud_state pack --root . --out state.tar.gz
   gcloud storage cp state.tar.gz gs://PROJECT-signal-bot/prod/state.tar.gz --if-generation-match=0
   ```
   The MT5 price caches are named after the broker server, so 10 years of
   history from the PC carry over when the cloud uses the same server, and each
   run downloads only the last days. Without them the first run must download
   the whole history (see "History to download" below).
3. `PREFIX=prod bash deploy/cloudrun/setup.sh job`. Later `setup.sh job` runs keep
   `prod` and the executor switch; only `PREFIX=...` / `EXECUTOR_ENABLED=...` change them.
4. Check what the cloud would trade, without trading:
   ```bash
   gcloud run jobs execute signal-bot --region=us-central1 "--update-env-vars=^@^EXECUTOR_DRY_RUN=true"
   ```
   The executor lines should match the PC's last sync (usually nothing to do).
5. Let the cloud trade the demo/live slots, then schedule:
   ```bash
   EXECUTOR_ENABLED=true bash deploy/cloudrun/setup.sh job
   bash deploy/cloudrun/setup.sh schedule
   ```

The PC's MT5 terminal can stay open and logged in; it no longer trades.
To go back to the PC: `bash deploy/cloudrun/setup.sh unschedule`, download
`prod/state.tar.gz`, restore it with
`.venv-windows/Scripts/python.exe -m live.cloud_state unpack --archive state.tar.gz --root .`,
and re-enable the three tasks.

## Operating

| Want to | Do |
|---|---|
| Pause everything | `bash deploy/cloudrun/setup.sh unschedule` |
| Deploy code changes | `bash deploy/cloudrun/setup.sh bot-image`, then `setup.sh job` (a job pins the image it was deployed with) |
| See what the executor would trade | `gcloud run jobs execute signal-bot --region=us-central1 "--update-env-vars=^@^EXECUTOR_DRY_RUN=true"` |
| Stop trading, keep paper trading | `EXECUTOR_ENABLED=false bash deploy/cloudrun/setup.sh job` |
| Retrain now | `gcloud run jobs execute signal-bot-retrain --region=us-central1` (waits for a running hourly run to finish) |
| Change what is modelled | `gcloud run jobs update signal-bot-retrain --region=us-central1 --update-env-vars="^\|^ML_TARGETS=fx:1h metals:1h\|ML_HORIZON=6"`, then retrain |
| Update MT5 | `bash deploy/cloudrun/setup.sh images` (re-bakes the prefix), then `setup.sh job` |
| Try another Wine branch | edit `_WINE_BRANCH` in `cloudbuild-wine.yaml`, then `setup.sh images` (the prefix must be baked on the same Wine it runs on) |
| Roll back the state | copy `PREFIX/backups/state-YYYY-MM-DD.tar.gz` over `PREFIX/state.tar.gz` while unscheduled |
| A run is stuck on "state is leased" | wait: a lease expires 40 minutes after its run started. A cancelled run releases it; a run killed for memory does not |
| History to download (the log says `MT5 history to download: ...`) | a new instrument, more `MT5_HISTORY_YEARS`, or caches older than a year: that run lets the terminal keep 100,000 bars and needs more memory. Run it once with `gcloud run jobs update signal-bot --region=us-central1 --memory=4Gi`, execute, then set `--memory=2Gi` back |

## Caveats

- **MT5 updates itself.** Every run starts from the image, so a newer MT5 build
  is downloaded at start until the base image is rebuilt. Rebuild it when the
  logs show live updates.
- **MT5 under Wine is not officially supported for the Python package.** If the
  probe reports `IPC timeout`, first check the login (`MT5_LOGIN`, `MT5_SERVER`,
  the secret); a terminal without an account never answers. Only then try
  another Wine branch in the base image.
- **Needs a recent kernel.** MT5's copy protection fails under Wine on Linux
  older than 5.11 (a "debugger has been found" dialog nobody can click). Cloud
  Run gen2 is fine (6.9 measured); keep `--execution-environment=gen2`.
- **The terminal runs lean.** Its built-in browser (WebView2: Market, Signals,
  news panels) is switched off; it took ~1.7 GB, more than everything else in a run.
  It starts without charts, and keeps 10,000 bars per symbol when the price
  caches are complete (`MT5_MAX_BARS`, default `auto`). The bot needs none of those.
- **Nothing runs between hours.** Orders are placed at most once an hour, which
  suits the 1h and 1d strategies.
