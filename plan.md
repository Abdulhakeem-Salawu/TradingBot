# Trading bot plan

Last updated 2026-09-15, 22:50 UTC.

## What you asked for

1. **A trading bot.**
   - Rule strategies (time-series momentum `tsmom`, `carry`) on crypto, 7 FX majors and gold, on 1h and 1d bars.
   - Many strategies paper trade side by side and are compared daily. You switch the one that trades.
2. **MetaTrader 5 execution.**
   - MetaQuotes demo now, Exness live later.
   - Modes are signal (default), demo and live. Real-money orders only when you turn live on.
3. **Test on your Windows PC first.**
   - MT5 prices with 10 years of history, Task Scheduler jobs.
4. **Then run it in Google Cloud, within the free tier,** so it runs without the PC.
   - Share the free allowances with your existing services, and never touch those services.
5. **This session: stop the VM fixes and port the bot to Cloud Run as planned.**
   - A Cloud Run job wakes every hour (signals, predictions, MT5 sync).
   - A retraining job runs weekly.
   - State is kept in Cloud Storage.
6. **Work within these rules:**
   - Don't touch your existing Cloud Run resources.
   - Stay within the free quota.
   - You enter credentials yourself.
   - Nothing is scheduled without asking you.

## What is done

### Bot (before this session)
- **Research harness:** backtests with guards, walk-forward scoring and an ML research classifier.
- **Paper trading:** ledger, daily comparison report (`live.compare`) and switching (`live.control`).
- **MT5 layer:**
  - Executor with sizing and safety checks.
  - Price loader from the MT5 terminal (`live/mt5_data.py`) with server-clock detection.
  - Selftest (`python -m live.selftest`) that drives it all against a fake MT5.
- **PC test phase:** paper jobs and executor via Task Scheduler (`deploy/windows/`), 10-year MT5 price caches.

### Cloud Run port (this session)
- **Google Cloud resources, all named `signal-bot*`:**
  - Artifact Registry repo, bucket `gs://pioneering-axe-233302-signal-bot` and service account.
  - Jobs `signal-bot` (hourly) and `signal-bot-retrain`.
- **State in Cloud Storage** (`live/cloud_state.py`):
  - One bundle per run.
  - A lease so two runs never overlap.
  - A daily backup.
  - A cancelled run still saves its work and releases the lease.
- **MT5 under Wine in a container:**
  - **Install:** MT5's copy protection stops the installer on Cloud Build's Linux kernel (5.10). The install runs in a Cloud Run job instead (kernel 6.9), and the result is baked into the image: wine-base → bake job → mt5-base → bot image.
  - **IPC fixed:** the Python connection to the terminal only works when the terminal is logged in. The run writes a start-up config from `MT5_LOGIN`, `MT5_SERVER` and the `MT5_PASSWORD` secret (which you set), then deletes it once the terminal is up.
- **Hourly run** (`live/cloud_run.py`):
  - **Sequence:** restore state, start MT5, run every paper job, predictions, save.
  - **Data:** the price fetch is incremental. Once the cache holds the history, only the last week is re-read.
- **Memory, the last blocker:**
  - **Cause found:** runs ran out of memory at 2 GiB. A built-in memory watch showed MT5's embedded browser (WebView2) was taking ~1.7 GB. It is now switched off.
  - **Other changes:** no preloaded charts, a small bar limit when caches are complete, and a retry while the terminal downloads a symbol's bars.
- **First complete runs on Cloud Run, on staging state** (a copy of the PC's data):

  | Run | Setup | Result | Time | Peak memory |
  |---|---|---|---|---|
  | 17:02 UTC | 4 GiB, WebView2 on | 7/7 jobs ok | 184 s | full (4 GiB) |
  | 17:09 UTC | 4 GiB, WebView2 off | 7/7 jobs ok | 131 s | ~1.95 GB |
  | 17:49 UTC | **1 vCPU / 2 GiB, final setup** | **7/7 jobs ok** | **140 s** | **1,819 of 2,048 MB** |

- **Retraining job on staging:** 14 models trained in 83 s, peak memory 0.5 GB, state saved.
  - Every model's out-of-sample verdict is currently "NO EDGE".
  - Predictions stay paper only either way.

### Cutover: the bot now runs in the cloud (2026-09-15, 20:45 UTC)
- **PC tasks disabled:** "SignalBot Paper Jobs", "SignalBot MT5 Executor (demo)" and "SignalBot Daily Compare" are off; nothing on the PC trades any more.
  - The PC's MT5 terminal can stay open.
  - To undo, re-enable the tasks (after unscheduling the cloud, see `deploy/CLOUD_RUN.md`).
- **Production state:** the PC's `data/` and `state/` were uploaded as `gs://pioneering-axe-233302-signal-bot/prod/state.tar.gz` (SQLite backup copy; the PC files were not changed).
  - Both jobs use `prod`.
  - A later `setup.sh job` keeps `prod` and the executor switch (fixed so a redeploy can't fall back to staging or stop trading).
- **Demo execution in the cloud:** `EXECUTOR_ENABLED=true`, `EXECUTOR_MODE=demo`, live gate closed.
  - A dry run first matched the PC's last sync: bot positions at target, your 2 manual EURUSD trades ignored.
  - **Bug found and fixed:** the first real sync (21:02) had its orders rejected (nothing filled). The Wine bridge passed an empty keyword dict, which MetaTrader5's `order_send` refuses. Confirmed with `order_check` on the PC, fixed in `live/mt5_bridge.py`.
  - **Rerun at 21:15:** AUD_USD +4,000 and NZD_USD +4,000 units filled.
- **Scheduled with Cloud Scheduler:**
  - `signal-bot-trigger`: every hour at :02 UTC. The first scheduled run was at 21:02.
  - `signal-bot-retrain-trigger`: Sundays at 00:30 UTC.
- **Models trained on prod** (14, all "NO EDGE" for now), so hourly paper predictions start.
- **Cleanup:**
  - Trial VM `signal-bot` **stopped** (its external IP is released). Deleting it is yours to do (below).
  - `signal-bot-bake` redeployed without its temporary fix (rebuilt `wine-base`).
  - Test job `signal-bot-diag` deleted.
- **Prod runs so far:**

  | Run (UTC) | What | Result | Time | Peak memory |
  |---|---|---|---|---|
  | 20:48 | dry run | 7/7 jobs ok, nothing to trade | 118 s | 1,882 MB |
  | 21:02 (scheduled) | first cloud sync | 7/7 ok, 2 orders rejected (bridge bug) | 144 s | 2,012 MB |
  | 21:15 | sync after the fix | 7/7 ok, 2 orders filled | 112 s | 1,844 MB |

  Memory is counted with reclaimable file cache (about 1.2 GB of it), so the real margin is larger than these peaks suggest. See "Next improvements".

### Telegram reports, and getting off paid secrets (2026-09-15, 22:45 UTC)
- **Your bot:** @FxCurrencyBot. The token you added works.
- **What the bot sends** (paper position changes stay off; `NOTIFY_PAPER=changes` turns them on):
  - the daily strategy comparison, after the 23:02 UTC run;
  - demo orders filled, rejected or blocked by a safety check;
  - errors and stale data in a paper job;
  - a short alert when a run has problems: MT5 did not start, a job failed, the state was not saved, or the run was skipped. A run killed outright (out of memory) cannot send one; that shows in the logs.
- **Secret Manager costs money here** ($0.06 per active version per month beyond 6 free, and the billing account has 20). So the bot no longer uses it:
  - Settings now live in one private file, `gs://pioneering-axe-233302-signal-bot/secrets/bot.env`, read at the start of every run (`SECRETS_URI`).
  - The values were copied there **inside Cloud Run**, from the secrets themselves, so neither the password nor the token passed through this chat.
  - Both jobs no longer reference Secret Manager, so its two bot secrets can be destroyed (below). `setup.sh` no longer attaches them either: `bash deploy/cloudrun/setup.sh settings` writes one value (password, token, chat id) into the file, asking for it without showing it.
  - **Working end to end:** a probe logged into MT5 with the file alone, and the 23:02 run sent the comparison and an order notice to Telegram.
  - **Security note:** this is no weaker *in this project*. The default compute service account (which `apk-clone-factory`, `appcloner-server` and `scraper-camoufox` run as) already holds project-wide `editor`, `secretmanager.secretAccessor` and `storage.objectAdmin`, so it could already read every secret and every bucket. Worth revisiting before any live-money password.
- **Still to do here:** press Start in @FxCurrencyBot so Telegram reveals a chat to send to; then the chat id is set on both jobs and a test message goes out.

## What I'm working on now

Nothing is running by hand. The bot runs by itself every hour. What's left needs you, or can wait.

## Left for you

0. **Build 20 years of Exness history on the PC (in progress, 2026-09-16).** Cloud Run can't download it: one 10-year attempt hit the 30-minute limit, because a fresh container downloads everything again and keeps it in memory. A desktop terminal does it in minutes, and the cloud then fetches only the last days.
   - **You:** install Exness's MT5 on the PC and log in with the **investor (read-only) password**, so this PC can't trade.
   - **You:** Tools > Options > Charts > Max. bars in chart → Unlimited, then restart MT5.
   - **Me:** `python -m live.mt5_data --years 20`, which only downloads prices: no ledger, no orders. It reports each symbol's first bar and whether the history is complete.
   - **Me:** `python -m live.cloud_state add --uri gs://pioneering-axe-233302-signal-bot/prod data/mt5_Exness-MT5Real10_*`. This takes the same lease as a run, so it never clashes with the schedule.
   - **Me:** set `MT5_HISTORY_YEARS=20` on the job, run it once, and check that the run stays around 2–3 minutes and within 2 GiB.

1. **Press Start in @FxCurrencyBot** (Telegram). A bot cannot write to you until you write to it. Tell me when done and I finish the wiring: chat id on both jobs, then a test message.
2. **Change the MT5 demo password.** It was pasted in chat.
   - Change it in MT5 (or on the MetaQuotes account page).
   - Then save the new one yourself; it asks for the value and hides it as you type:
     ```bash
     bash deploy/cloudrun/setup.sh settings     # key: MT5_PASSWORD
     ```
   - The next hourly run uses it. Never paste it in chat.
3. **Delete the trial VM** (it is stopped). This deletes its disk for good:
   `gcloud compute instances delete signal-bot --zone=us-central1-a --project=pioneering-axe-233302`
4. **Get Secret Manager back to zero cost.** 20 active versions, 6 free, so about $0.84/month at list price. Destroying a version is permanent, so these are yours to run. Every app reads `:latest`, so no app loses the version it uses.
   - **The bot's two, now unused** (nothing references them any more):
     `gcloud secrets delete signal-bot-mt5-password --project=pioneering-axe-233302`
     `gcloud secrets delete signal-bot-telegram-token --project=pioneering-axe-233302`
   - **Older versions no app reads** (each app reads `:latest`): `API_KEY` v1, `KEYSTORE_PASSWORD` v1, `DB_PASSWORD` v1 (already disabled, still billed).
   - **Secrets nothing in Cloud Run references at all:** `APK_KEYSTORE` (v1, v2), `DB_CONNECTION`, `DB_DATABASE`, `DB_HOST`, `DB_PORT`, `DB_USERNAME`. Check first whether something outside Cloud Run uses them (a build, a local script). `APK_KEYSTORE` looks like an older copy of `apk-keystore`; **download any keystore before destroying it** — losing an Android signing key means you can no longer update that app.
   - That leaves 8: `APP_KEY`, `CLOUD_TASKS_SECRET`, `DB_PASSWORD`, `MAIL_PASSWORD` (the fiverr-keywords services), `API_KEY`, `KEYSTORE_PASSWORD` (appcloner-server), `apk-keystore`, `keystore-password` (apk-clone-factory).
   - **To reach 6 (free), one of those apps has to go.** You said some are unused: retiring `appcloner-server` or `apk-clone-factory` frees exactly 2. Tell me which, and I can list what else it uses before you delete anything.
5. **Optional: delete the staging test state** (~30 MB, a copy of the PC data plus test runs):
   `gcloud storage rm -r gs://pioneering-axe-233302-signal-bot/staging/`

## Next improvements (no rush)

- **Memory headroom:** the terminal still downloads full history for EURUSD, GBPUSD, USDJPY and USDCHF (~100 MB each, in memory). It looks like it opens its default charts even with the chart files removed. Stopping that frees ~400 MB and ~10–20 s per run.
- **Run length:** MT5 start (45–60 s) and one Python start per paper job (~70 s for 7 jobs) are the biggest costs.

## The long run

1. **Paper and demo phase in the cloud.**
   - Let the strategies build a live track record; read the daily comparison (in the 23:02 UTC run's log).
   - Retrain weekly and score the paper predictions.
   - Check demo fills, sizing and the safety limits against the paper ledger. The bot leaves your manual trades alone.
2. **Exness.**
   - Exness demo first (recommended), same checks, as a repeat of the cutover steps in `deploy/CLOUD_RUN.md`.
   - Then Exness live with `ALLOW_LIVE_TRADING`, small size, only when you decide.
   - Re-check the broker's server clock (Exness uses UTC).
   - Keep one price source per ledger.
3. **Keep it inside the free tier.** Watch the phase timings and memory lines in each run's log.
4. **Watch Nigeria's SEC rules for online FX/CFD brokers.** They may change which offshore brokers can serve you.

## Free-tier budget (per billing account, shared with your existing services)

| | Free per month | Existing services | This bot (hourly, measured) |
|---|---|---|---|
| Cloud Run vCPU-seconds | 240,000 | ~41,000 | 730 x 140 s x 1 vCPU ≈ 102,000 |
| Cloud Run GiB-seconds | 450,000 | ~112,000 | 730 x 140 s x 2 GiB ≈ 204,000 |
| Cloud Storage (us-central1) | 5 GB | 3.2 GB | ~30 MB state + 7 daily backups |
| Cloud Scheduler jobs | 3 | 0 | 2 (scheduled) |
| Secret Manager active versions | 6 | 20 (your other apps) | 0 (settings live in the bucket) |

- **Total with the bot:** ~143,000 vCPU-s (60%) and ~316,000 GiB-s (70%) a month. That leaves headroom, but runs should not grow much past ~150 s.
- **Today's testing:** the one-off bake, diagnostic and test runs plus image builds are small against the monthly allowance.
- **Artifact Registry:** storage is above its 0.5 GB free size (this was already true before the bot). Your invoices have shown $0.00 so far; check Billing → Reports before relying on that.
