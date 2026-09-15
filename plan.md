# Trading bot plan

Last updated 2026-09-15 (UTC).

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

## What I'm working on now

The Cloud Run port is working on staging. Finishing touches, done 2026-09-15:

- [x] Retraining job tested.
- [x] `deploy/CLOUD_RUN.md` updated:
  - Measured budget, memory lines and `MEMORY_REPORT_AT`.
  - WebView2 off, `MT5_MAX_BARS`, the login requirement, a history-download row.
- [x] Throwaway test job `signal-bot-diag` deleted.
- [ ] `signal-bot-bake` still carries a temporary fix in its arguments. The next full `bash deploy/cloudrun/setup.sh images` (only needed to update MT5) rebuilds it cleanly.
- [ ] Waiting for your OK on the next step: scheduling (below).

## Still to do to finish the move (needs your OK where marked)

1. **Schedule the jobs** (your OK): `bash deploy/cloudrun/setup.sh schedule`.
   - Enables the Cloud Scheduler API.
   - Runs hourly at :02 and retraining on Sundays (2 of the 3 free scheduler jobs).
2. **Cutover from the PC** (your OK, and you run the PC steps):
   - Disable the PC's scheduled tasks.
   - Upload the PC state as `prod`.
   - Point the job at `prod`.
   - The PC and the cloud must never run the same ledger or trade the same account at the same time.
3. **Telegram alerts:** runs log "TELEGRAM_TOKEN / TELEGRAM_CHAT_ID not set". Add them as secrets if you want messages from the cloud. Each new secret version costs about $0.06/month, because the project is already past Secret Manager's 6 free versions.
4. **Delete the trial VM** `signal-bot` (your OK). It is no longer needed, and removing it also removes its external IP.
5. **Change the MT5 demo password.** It was pasted in chat, so change it, then add a new secret version yourself.

## The long run

1. **Paper phase in the cloud.**
   - Let the strategies build a live track record.
   - Read the daily comparison.
   - Retrain the models weekly and score their paper predictions.
2. **Demo execution.**
   - Turn on `EXECUTOR_ENABLED=true` for the demo slot, with the master password added by you.
   - Check fills, sizing and the safety limits against the paper ledger.
   - The bot must leave your manual trades alone.
3. **Exness.**
   - Exness demo first (recommended), same checks.
   - Then Exness live with `ALLOW_LIVE_TRADING`, small size, only when you decide.
   - Re-check the broker's server clock (Exness uses UTC).
   - Keep one price source per ledger.
4. **Keep it inside the free tier.**
   - Watch the phase timings and memory lines in each run's log.
   - If runs grow, first cut MT5 start-up (45–60 s) and the per-job Python start-up (paper jobs take ~70 s).
5. **Watch Nigeria's SEC rules for online FX/CFD brokers.** They may change which offshore brokers can serve you.

## Free-tier budget (per billing account, shared with your existing services)

| | Free per month | Existing services | This bot (hourly, measured) |
|---|---|---|---|
| Cloud Run vCPU-seconds | 240,000 | ~41,000 | 730 x 140 s x 1 vCPU ≈ 102,000 |
| Cloud Run GiB-seconds | 450,000 | ~112,000 | 730 x 140 s x 2 GiB ≈ 204,000 |
| Cloud Storage (us-central1) | 5 GB | 3.2 GB | ~30 MB state + 7 daily backups |
| Cloud Scheduler jobs | 3 | 0 | 2 |

- **Total with the bot:** ~143,000 vCPU-s (60%) and ~316,000 GiB-s (70%) a month. That leaves headroom, but runs should not grow much past ~150 s.
- **Today's testing:** the one-off bake, diagnostic and test runs plus image builds are small against the monthly allowance.
- **Artifact Registry:** storage is above its 0.5 GB free size (this was already true before the bot). Your invoices have shown $0.00 so far; check Billing → Reports before relying on that.
