# Signal Harness

A research harness and paper trader for crypto, FX majors and gold. It answers
"would this have made money, after costs, beyond luck?" without lying to you,
then runs the survivors on paper with the exact same code.

## Start here

```bash
pip install -r requirements.txt
python validate_harness.py          # ~1 min. Run this FIRST, and after every change.
```

`validate_harness.py` tests the harness against data whose answer is known.
Until it passes, no scorecard from this tool means anything.

## Research (on your own machine)

```bash
python run_research.py --universe crypto --strategy tsmom --timeframe 1d
python run_research.py --universe crypto --strategy tsmom --timeframe 1h
python run_research.py --universe fx     --strategy tsmom --timeframe 1d
python run_research.py --universe metals --strategy tsmom --timeframe 1h
python run_research.py --universe fx     --strategy carry --timeframe 1d
```

| Universe | Instruments | Data |
|---|---|---|
| `crypto` | BTC, ETH, BNB, XRP, ADA, SOL (USDT, spot, long-only) | Binance public market data, no key |
| `fx` | EUR/USD, USD/JPY, GBP/USD, USD/CHF, AUD/USD, USD/CAD, NZD/USD | Dukascopy free datafeed, no account: hourly BID prices back to 2005, spreads for the last year. Or, on a machine running MetaTrader 5, the terminal's own history (`FX_DATA_SOURCE=mt5`, see `live/mt5_data.py`) |
| `metals` | XAU/USD | same as fx |

FX and gold financing and carry use short rates from FRED (no key). No
accounts or keys are needed for research or paper trading. Dukascopy blocks
bursts, so the first 20-year download is deliberately slow (`DUKASCOPY_RPS`,
default 0.5 requests/second); after that each run fetches only new hours.
From slow or distant networks it can fail outright; on the Windows PC use
the MT5 terminal's history instead (10 years loads in minutes).

### Strategies (parameters fixed in advance, see `harness/strategies.py`)

- **`tsmom`** is volatility-managed trend following: the average sign of three
  lookbacks, sized to 10% annual volatility, with a rebalance band so small
  changes don't pay costs.
  - `1d`: 1, 3 and 12 months (the best-evidenced version).
  - `1h`: 1 day, 1 week and 1 month. A hypothesis with much thinner evidence,
    where costs take a far larger bite.
- **`carry`** holds the higher-yielding currency, rebalanced monthly. FX only,
  daily only. Its return comes mostly through the financing line, so the
  broker's markup decides whether it earns anything.

`run_backtest.py` is the ML classifier path (walk-forward, purged). It trains on
any registry instrument's full history with that instrument's costs, e.g.
`python run_backtest.py --symbol XAU_USD --interval 1h`.

## Paper trading, comparing, switching

```bash
python -m live.signal_job --universe crypto --timeframe 1h --dry-run   # one paper strategy
python -m live.compare                                                 # all of them, side by side
python -m live.control status                                          # what each universe follows
python -m live.control activate --universe fx --strategy tsmom --timeframe 1d
```

- **Paper trading.** Each job updates prices to the last closed bar, computes
  positions with the same `harness.pipeline` code the backtest uses, and marks
  a SQLite paper ledger to market. Run one job per candidate strategy; they
  share the ledger.
- **Comparing.** `live.compare` scores every paper strategy per universe on
  the same dates. Hourly results are rolled into trading days so they compare
  fairly with daily ones. It reports whether each gap to the strategy you
  follow is statistically real (the bar rises with the number of
  alternatives), and what following your choices earned after switch costs.
  Cron sends it to Telegram daily.
- **Switching.** Each universe follows one strategy, in one of the modes below.

| Mode | Behaviour |
|---|---|
| `signal` (default) | Telegram instructions; you place orders yourself |
| `demo` | the MT5 executor trades a MetaTrader 5 demo account |
| `live` | the MT5 executor trades a real MetaTrader 5 account. Gated by `ALLOW_LIVE_TRADING=true` on both machines, a typed confirmation, and an override if the paper record isn't significant |

Demo and live cover FX and gold only; crypto is signal-only (Binance refuses
US servers and is blocked in Nigeria). Orders are placed by
`live.mt5_executor` on the machine running MetaTrader 5. It fetches the VM's
targets over ssh, runs every safety check, trades to target and reports back.
Every check fails closed. There's a kill switch (`live.control kill
[--flatten]`), and `python -m live.selftest` tests all of it, including the
MT5 adapter against a fake MetaTrader5 module.

- **Test phase, everything on your Windows PC:** `deploy\windows\setup_local.ps1`, see [deploy/WINDOWS_MT5.md](deploy/WINDOWS_MT5.md).
- **Target: hourly Cloud Run job + Cloud Storage, MT5 under Wine inside the job:** see [deploy/CLOUD_RUN.md](deploy/CLOUD_RUN.md).
- **Earlier option, always-on e2-micro VM:** see [deploy/GCP.md](deploy/GCP.md).
- **MT5 on your Windows PC, demo and/or live (Exness):** see [deploy/WINDOWS_MT5.md](deploy/WINDOWS_MT5.md).
- **Later, 24/7 MT5 on Linux through Wine:** see [deploy/LINUX_WINE_MT5.md](deploy/LINUX_WINE_MT5.md).

---

## Reading a research scorecard

Stop at the first failure:

1. **Under 2 years of history?** No conclusion. That's no information, not a failure.
2. **Does any benchmark match it?** Then there's no edge. Benchmarks:
   - crypto and gold: buy-and-hold and a vol-targeted hold;
   - FX: flat and a vol-targeted long;
   - all: the strategy's own positions shifted to random dates, which keeps
     exposure and turnover but destroys timing.
3. **Deflated Sharpe of excess returns under 0.95?** Unproven. Excess returns
   are measured against the primary benchmark, scaled to the same volatility.
4. **All clear?** Paper trade. That's the next test, not a formality.

The decision is made at the **portfolio** level for each asset class. Picking
the best single pair from the table afterwards is extra trials, and the
per-instrument deflated Sharpe is charged for it.

**"PROMISING BUT UNPROVEN" is not a reason to deploy.** In the self-test, pure
noise gets that verdict in about a quarter of runs.

## Hourly vs daily: what each actually buys you

Hourly signals feel faster to evaluate, but a track record's significance is
`Sharpe * sqrt(years)` at any bar frequency. More bars don't shorten the wait;
only a higher Sharpe does. Every scorecard prints **"Live time to confirm this
Sharpe"** so the wait is a number, not a guess. What hourly paper trading
*does* give quickly is a check that data, signals and costs behave.

Costs are also a far larger share of an hourly move than a daily one. On pure
noise, the pre-registered `tsmom` rules pay these costs per year:

| | cost drag / year | turnover / year | gross Sharpe needed to break even |
|---|---|---|---|
| crypto `1d` | 0.2% | 1x | ~0.1 |
| FX `1d` | 0.6% | 30x | ~0.1 |
| crypto `1h` | 4.6% | 31x | ~1.4 |
| FX `1h` | 14% | ~700x | ~2 |

So an hourly rule has to be very good before it is even break-even.

## What the research says (September 2026)

- **Crypto.** The best net-of-cost results come from simple rules plus a
  cost-aware trading filter, not from better forecasts. The best-known
  ML-with-filter result (arXiv 2606.00060) did not significantly beat
  buy-and-hold.
- **FX majors.** Close to a random walk at short horizons. Simple technical
  rules stopped working in the early 1990s. Carry and momentum are contested:
  one study finds them still profitable across many currencies, another finds
  they decayed after publication. The seven majors all share the USD leg, so
  they diversify far less than seven independent markets.
- **Gold.** The link to real yields broke after 2022, so avoid "fair value"
  models. Trend following has long-run support as part of diversified
  strategies. Long positions always pay financing.

## What each guard does

- **Costs.** Turnover (fees, half-spread, slippage) is paid when the position
  changes. Financing (the swap, from FRED rates plus a broker markup) is paid
  while it's held. `harness/costs.py`
- **Position-based simulation.** A held position is charged once, not every
  bar. The lag between decision and return is applied inside the simulator.
  `harness/backtest.py`
- **Profitable-after-costs labels** for the classifier path. `harness/labels.py`
- **Purged, embargoed walk-forward** for the classifier path. `harness/splits.py`
- **Trial counting and deflated Sharpe.** `trials.json` hashes the harness
  source, so editing a strategy default counts as a new trial. Synthetic
  control runs go to `trials-synthetic.json`. `harness/trials.py`,
  `harness/scoring.py`
- **Self-test.** Leakage, cost accounting, lookahead (prices and rates),
  zero-cost and with-cost negative controls at both timeframes, and a planted
  trend positive control with a power curve. `validate_harness.py`

## Honest limits

- **Passing does not mean profitable.** It means not yet disproven.
- **Cost presets are assumptions.** `run_research.py` prints the median spread
  in Dukascopy's data (an ECN feed, tighter than retail) next to the modeled
  one. `python -m live.mt5_executor --check-costs` shows your broker's actual
  spreads and swaps. The swap markup can erase carry entirely.
- **Data source differs from execution.** With Dukascopy prices, your MT5
  broker's prices differ slightly. That's fine for daily and hourly trend
  rules, but fills won't match the paper ledger to the pip. With
  `FX_DATA_SOURCE=mt5` the paper ledger uses the broker's own bars. Don't
  switch a running ledger between sources.
- **Broker availability.** Nigeria's SEC published draft rules (September 2026)
  that would bring offshore FX/CFD brokers serving Nigerians under licensing.
  The bot is broker-agnostic, but check your broker still serves you.
- **Rate data lags.** The OECD rate series behind financing are monthly and
  arrive two to four months late; later months are forward-filled, and the
  loader warns once a series is over 180 days old. OECD's EUR and GBP
  3-month series stopped at 2026-01, so from 2026-02 they continue with
  monthly averages of the daily euro short-term rate and SONIA
  (`SHORT_RATE_EXTENSIONS` in `harness/instruments.py`). Those are overnight
  rates: within about 0.25 points of the 3-month rate over 2024-2026, but
  up to about 1 point apart while central banks are moving quickly.
- **Regimes.** Out-of-sample in time is not out-of-sample in regime.

## Files

```
harness/costs.py        turnover + financing cost model, rate alignment
harness/instruments.py  instrument registry, universes, bars per year per timeframe
harness/data.py         Binance / Dukascopy / FRED loaders (cached, incremental) + synthetic data
live/mt5_data.py        FX/gold hourly bars from a MetaTrader 5 terminal (server clock -> UTC)
harness/calendar.py     trading-day boundaries (17:00 New York for FX and gold)
harness/strategies.py   tsmom, carry, benchmark positions (pre-registered parameters)
harness/backtest.py     position simulator, portfolio combiner, benchmark suite
harness/pipeline.py     instrument -> positions, shared by research and paper trading
harness/scoring.py      classifier scorecard + position scorecard, deflated Sharpe
harness/features.py     classifier features
harness/labels.py       classifier labels
harness/splits.py       purged walk-forward
harness/trials.py       persistent trial counter with code hash
run_research.py         rule research entry point
run_backtest.py         classifier entry point
validate_harness.py     self-test -- run before trusting anything
live/signal_job.py      paper-trading job (cron); acts on the active slot after each run
live/run_jobs.py        every paper job once, for Task Scheduler on the Windows PC
live/compare.py         side-by-side comparison of every paper strategy, switch accounting
live/control.py         activate / switch / deactivate / kill / rebalance / order log
live/executor.py        signal instructions, target export, trade-to-target with safety checks
live/broker.py          broker interface
live/mt5_broker.py      MetaTrader 5 adapter (native Windows, or MT5_BACKEND=wine on Linux)
live/mt5_bridge.py      MetaTrader5 package under Wine <-> the bot's Linux Python (127.0.0.1 only)
live/mt5_data.py        FX/gold hourly bars from an MT5 terminal (FX_DATA_SOURCE=mt5)
live/cloud_run.py       Cloud Run job entry point: restore state, start MT5, run jobs, save
live/cloud_state.py     state archive in Cloud Storage with a lease and generation checks
live/mt5_executor.py    runs next to MT5: fetch targets, trade, report back; --smoke-test on demo
live/selftest.py        execution and switching self-test (fake broker, fake MT5, fake ssh)
live/ledger.py          SQLite: paper sleeves, slots, switches, orders, requests, syncs
live/timing.py          market calendar: stale data, trading-day alignment
live/env.py             .env loader
live/notify.py          Telegram
deploy/setup_vm.sh      one-time VM setup + cron
deploy/GCP.md           free-tier deployment guide
deploy/WINDOWS_MT5.md   MT5 executor on your Windows PC
deploy/windows/         setup_local.ps1 (all on the PC), setup_executor.ps1, task wrappers, .env template
deploy/LINUX_WINE_MT5.md  later option: MT5 on Linux through Wine
deploy/CLOUD_RUN.md     hourly Cloud Run job: budget, setup, cutover from the PC
deploy/cloudrun/        images (Wine + MT5 base, bot), Cloud Build configs, setup.sh
deploy/linux/           systemd units and installer for MT5 under Wine on a VM
```
