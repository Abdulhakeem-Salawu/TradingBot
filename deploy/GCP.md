# Deploying the paper trader on Google Cloud's free tier

What runs: one **e2-micro** VM that wakes on a schedule, pulls the bars that
just closed, updates paper positions and messages you on Telegram. No orders
are ever placed. Research and backtests stay on your own machine.

Everything below is inside Google Cloud's **Always Free** tier
(<https://docs.cloud.google.com/free/docs/free-cloud-features>) if you follow
the guardrails. The steps that create accounts, enter billing details or
generate tokens are yours to do.

## Free-tier guardrails -- read before creating anything

| Rule | Why |
|---|---|
| Exactly **one** `e2-micro` VM | The free allowance is one e2-micro's worth of hours per month |
| Region `us-central1`, `us-east1` or `us-west1` | Other regions are billed |
| Boot disk type **Standard persistent disk** (`pd-standard`), 30 GB max | The console defaults to *balanced*, which is **not** free |
| No GPUs, no snapshots, no extra disks | Never covered by the free tier |
| Under 1 GB outbound data per month | This bot uses tens of MB; downloads into the VM are free |
| Set a **budget alert** | Alerts email you; they do not cap spending |

The VM needs an external IPv4 address for outbound internet (the alternative,
Cloud NAT, is billed). Google's free-tier page does not list external IPs
explicitly, so check the Billing report after 48 hours (step 7). If anything
is going to show a small charge, this is it.

## 1. Project, billing, budget

1. At <https://console.cloud.google.com>, create a project (e.g. `signal-bot`).
2. Link a billing account. The free tier requires one; staying inside the
   limits costs $0.
3. **Billing -> Budgets & alerts -> Create budget**: amount $1, alerts at
   50% / 90% / 100%.

## 2. Create the VM

In Cloud Shell (the `>_` icon in the console) or with the gcloud CLI:

```bash
gcloud config set project YOUR_PROJECT_ID
gcloud services enable compute.googleapis.com
gcloud compute instances create signal-bot \
  --zone=us-central1-a \
  --machine-type=e2-micro \
  --boot-disk-type=pd-standard \
  --boot-disk-size=30GB \
  --image-family=debian-12 \
  --image-project=debian-cloud
```

Double-check in the console that the disk type says **Standard persistent
disk**.

## 3. Copy the project up

From the project folder on your own machine, leaving out cached data, local
state and your `.env`.

Windows (PowerShell, gcloud CLI installed):

```powershell
tar -czf "$env:TEMP\signal-bot.tgz" --exclude=data --exclude=state --exclude=logs --exclude=.venv --exclude=__pycache__ --exclude=.env .
gcloud compute scp "$env:TEMP\signal-bot.tgz" signal-bot:~ --zone=us-central1-a
gcloud compute ssh signal-bot --zone=us-central1-a --command "mkdir -p signal-monitoring && tar xzf signal-bot.tgz -C signal-monitoring"
```

macOS / Linux:

```bash
tar czf /tmp/signal-bot.tgz --exclude=data --exclude=state --exclude=logs \
    --exclude=.venv --exclude=__pycache__ --exclude=.env .
gcloud compute scp /tmp/signal-bot.tgz signal-bot:~ --zone=us-central1-a
gcloud compute ssh signal-bot --zone=us-central1-a \
    --command "mkdir -p signal-monitoring && tar xzf signal-bot.tgz -C signal-monitoring"
```

## 4. Get your tokens

- **Market data needs no account.** FX and gold come from Dukascopy's free
  datafeed, crypto from Binance's public market-data host, and interest rates
  from FRED. The first Dukascopy download of 20 years of history is
  deliberately slow, because Dukascopy blocks bursts.
- **Telegram** (optional; without it messages only go to the logs): message
  `@BotFather`, send `/newbot`, keep the token. Send your new bot any message,
  then open `https://api.telegram.org/bot<TOKEN>/getUpdates` and copy
  `chat.id`.

## 5. Install and schedule

```bash
gcloud compute ssh signal-bot --zone=us-central1-a
cd ~/signal-monitoring
bash deploy/setup_vm.sh                # creates .env, then stops
nano .env                              # paste tokens, save
JOBS="tsmom:crypto:1d tsmom:crypto:1h tsmom:fx:1d tsmom:fx:1h carry:fx:1d tsmom:metals:1d tsmom:metals:1h" bash deploy/setup_vm.sh
```

`JOBS` lists every strategy to **paper trade**, as `strategy:universe:timeframe`.
Include every candidate you might want to switch to: the daily comparison
can only compare what is running.

| Piece | Choices |
|---|---|
| strategy | `tsmom` (any universe), `carry` (`fx` with `1d` only) |
| universe | `crypto`, `fx`, `metals`, `all` |
| timeframe | `1h`, `1d` |

The second run validates the harness and the execution code, downloads
history, dry-runs every job and installs cron:

| Job | When (UTC) | Messages |
|---|---|---|
| any `1h` | every hour, a few minutes past | position changes, stale data, errors, orders |
| crypto `1d` | about 00:10 daily | the same |
| fx / metals `1d` | 21:xx and 22:xx Mon-Fri | the same (the second run is a no-op that covers daylight saving) |
| comparison | 22:45 daily | the strategy comparison table |

## 6. Check it is working

```bash
tail -f ~/signal-monitoring/logs/*.log
cd ~/signal-monitoring
.venv/bin/python -m live.compare
.venv/bin/python -m live.control status
```

## 7. After 48 hours: confirm $0

**Billing -> Reports**, filtered to the project. Every line should be $0.00.
If anything is charged, compare against the guardrails table; the usual
causes are a balanced disk, a second VM, or a non-US region.

## Compare strategies and choose which one to follow

Every job paper trades its strategy. Separately, each universe (`crypto`,
`fx`, `metals`) can **follow** one of them for actual trading. The daily
Telegram comparison, also available as `python -m live.compare`, looks like:

```
== FX  common window 2026-06-19 .. 2026-09-11 (61 days) ==
  strategy      ret   shp    mdd cost/y  trd     t    dt
* carry 1d    -0.5%  -0.6  -2.2%   0.1%    1  -0.3     -
  tsmom 1d    +1.8%   2.4  -0.8%   0.5%   56  +1.2  +0.8
  tsmom 1h    -1.8%  -2.8  -1.7%  13.3% 1584  -1.4  -0.5
  active: carry 1d [signal]
  biggest gap: tsmom 1d dt +0.8, needs |dt| >= 2.2 -- not significant (~392 more days)
  followed: -0.03% since 2026-09-13, 2 switch(es), switch costs 0.03%
```

- **`*`** marks the strategy being followed.
- **`dt`** asks whether the gap to the followed strategy is real. The bar
  starts at 2.0 and rises with the number of alternatives, because the biggest
  of several random gaps is usually bigger than one.
- **`followed`** is what your actual choices earned, including the cost of
  every switch. That's the scorecard for your switching decisions.

### Switching

```bash
cd ~/signal-monitoring
.venv/bin/python -m live.control activate --universe fx --strategy tsmom --timeframe 1d --reason "tsmom 1d ahead over 90 days"
```

Only strategies already being paper traded can be activated, and every switch
is recorded with its reason.

| Mode | What happens | Needs |
|---|---|---|
| `signal` (default) | Telegram tells you what to hold; you place the orders | nothing extra; all universes |
| `demo` | the MT5 executor on your PC trades a MetaTrader 5 **demo** account to the target | `--capital`; the executor set up per [WINDOWS_MT5.md](WINDOWS_MT5.md); fx, metals only |
| `live` | the MT5 executor trades a **real-money** MT5 account | `--capital`, `ALLOW_LIVE_TRADING=true` on the VM **and** the PC, `--confirm-live "REAL MONEY"`, and `--accept-unproven` unless the paper record has t >= 2 over 90+ days |

This VM can't see your MT5 account. It publishes demo/live targets; the
executor on your PC fetches them over ssh every 10 minutes, checks the
account, places the orders and reports back. `live.control status` shows when
it last synced.

```bash
# demo: the executor trades at its next sync; preview on the PC with
#   .venv-windows\Scripts\python.exe -m live.mt5_executor --dry-run
.venv/bin/python -m live.control activate --universe fx --strategy tsmom --timeframe 1d --mode demo --capital 10000
```

### Safety

- **Kill switch:** `live.control kill` stops new demo/live orders from the
  executor's next sync and drops those universes back to signal mode. Add
  `--flatten` to also close the bot's positions at that sync. For an
  immediate stop, create `state\KILL` in the project folder on the PC. Undo
  with `live.control unkill`.
- **Fail closed:** any failed check blocks every order in that run. Checks:
  - the kill switch;
  - the live gate;
  - capital above the account's value, or a non-USD account;
  - stale data or a missing target;
  - total position value above `MAX_GROSS_LEVERAGE`;
  - a single order bigger than a full flip;
  - too many orders.

  You're told why on Telegram, and the order log keeps it
  (`live.control orders`).
- **Stays in its lane:** the bot trades to target and never touches instruments
  outside the universe it manages.
- **Changing account type:** switching from demo to live (or back) doesn't move
  positions between accounts. `control` refuses until you
  `deactivate --flatten` or pass `--leave-positions`.
- **Crypto** is signal-only: Binance refuses US servers and is blocked in Nigeria.
- **Test before trusting:** run `python -m live.selftest` before using demo or
  live, and after any code change. It tests all of this against a fake broker.

## What paper trading will and won't show you

Within a few days, paper trading proves the **plumbing**:
- **Data:** bars arrive on schedule.
- **Signals:** positions match what the backtest would have held.
- **Costs:** turnover and financing charges look sensible.

It cannot prove an **edge** quickly, at any bar frequency. A track record's
t-statistic is `Sharpe * sqrt(years)`. Hourly bars give more data points,
but each carries proportionally less signal, so they don't shorten the wait.
The comparison's `t` and `dt` columns show how far each record is from proof.

## Updating the code

Copy the new version up (step 3), then:

```bash
.venv/bin/python validate_harness.py --quick
.venv/bin/python -m live.selftest
```

Changing a strategy parameter creates a new, untested strategy. Run it through
`run_research.py` on your own machine first. The trial counter records the
change even if you don't.

## Not possible on this free VM

- **Live orders on Binance:** `api.binance.com` refuses US IP addresses
  (HTTP 451), and every free-tier region is in the US. Market data from
  `data-api.binance.vision` works, and that is what this bot uses.
- **MetaTrader 5:** its Python package is Windows-only, so orders are placed
  from your Windows PC ([WINDOWS_MT5.md](WINDOWS_MT5.md)). Running MT5 under
  Wine needs about 2 GB of RAM, more than this VM has
  ([LINUX_WINE_MT5.md](LINUX_WINE_MT5.md)).
- **Deep learning (LSTM / transformers):** no GPUs, and only 1 GB of RAM.
