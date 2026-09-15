# MetaTrader 5 on your Windows PC: demo and live (Exness)

There are two ways to run the bot with MT5. Both use the same code.

```
A. Test phase: everything on the PC          B. Later: free Google VM + PC executor
   Task Scheduler on the PC                      Google VM (24/7)            Windows PC (when on)
     paper jobs (hourly), comparison               paper jobs, comparison      MT5 terminal
     MT5 executor (every 10 min)                   live.control targets  <--   live.mt5_executor
     all share state\ledger.db                     record-execution      <--   (over ssh)
     prices from the MT5 terminal                  prices from Dukascopy
```

Start with **A**. Move to **B** (or MT5 on Linux, [LINUX_WINE_MT5.md](LINUX_WINE_MT5.md))
once the demo runs cleanly. **Signal mode needs no MT5 at all.**

## What to know first

- **One executor serves one kind of account.** `EXECUTOR_MODE=demo` trades
  only demo slots, `live` only live slots, and each refuses to run if its
  terminal is logged into the other kind. Demo and live side by side need
  two terminals (see [Demo and live together](#demo-and-live-together)).
- **The PC must be on, logged in, with MT5 open** for anything to run.
  Nothing is lost when it's off: paper jobs catch up on missed bars, and the
  executor trades the account straight to the current target.
- **Only the bot's own positions are touched.** Orders carry a magic number
  (`MT5_MAGIC`). On hedging accounts (Exness and MetaQuotes demo accounts
  are hedging) positions you open by hand are ignored. On netting accounts
  MT5 merges everything per symbol, so don't trade the bot's symbols by hand.
- **Your MT5 password stays in the terminal.** The executor attaches to the
  terminal you're logged into. `MT5_PASSWORD` is optional and only needed if
  you want the executor to log the terminal in itself.
- **Lot sizes are checked.** Before sizing any order the adapter compares
  the broker's contract size with MT5's own profit calculation and refuses
  the symbol if they disagree.

## 1. Terminal settings

1. Log in to your account (demo first).
2. **Tools > Options > Expert Advisors**: tick **Allow algorithmic trading**.
3. Click **Algo Trading** in the toolbar so it's green.
4. Account currency must be **USD**.

## 2. Test phase: everything on this PC (A)

```powershell
cd C:\Users\USER\Downloads\signal-monitoring
powershell -ExecutionPolicy Bypass -File deploy\windows\setup_local.ps1 -NoSchedule
```

This creates `.venv-windows` and `.env` (from `deploy\windows\env.windows.example`:
`TARGETS_SOURCE=ledger`, `FX_DATA_SOURCE=mt5`, `MT5_HISTORY_YEARS=10`,
`EXECUTOR_MODE=demo`). It then validates the harness, runs every paper job
once (downloading history from MT5 and Binance), runs the executor self-test,
and shows `--status` plus a dry run. Drop `-NoSchedule` to also register
three scheduled tasks. They run hidden, while you're logged in:

| Task | When | What |
|---|---|---|
| SignalBot Paper Jobs | hourly at :02 | `live.run_jobs`: every paper job in `JOBS` |
| SignalBot Daily Compare | 22:45 UTC | comparison report (Telegram if configured) |
| SignalBot MT5 Executor (demo) | every 10 min | trade demo slots to target |

**Prices from MT5.** `FX_DATA_SOURCE=mt5` reads the terminal's hourly history
instead of Dukascopy, which is slow and unreliable from many networks. Ten
years of the 8 FX/gold instruments uses about 1.6 GB of terminal disk and
loads in a few minutes the first time. Bars are converted from the broker's
server clock to UTC (`MT5_SERVER_TZ=auto`), and only closed hours are stored.
Keep one price source per ledger.

## 3. Check the broker

```powershell
.venv-windows\Scripts\python.exe -m live.mt5_executor --status
.venv-windows\Scripts\python.exe -m live.mt5_executor --check-costs
.venv-windows\Scripts\python.exe -m live.mt5_executor --smoke-test --symbol EUR_USD
```

- `--status`: account, type (demo/real), hedging or netting, the bot's
  positions, positions it ignores, and current targets.
- `--check-costs`: each symbol's live spread next to the backtest's
  assumption, swap rates, and **the dollar value of the smallest position
  the broker allows** (`min lot $`).
- `--smoke-test` (**demo accounts only**): buys 2 minimum lots, closes 1,
  flips short, closes. It checks the bot's position after each step, that
  it's never long and short at once, and that your own positions weren't
  touched. Run it once per new broker or account type, while the market is
  open.

**Capital and minimum lots.** A universe's capital is split equally across
its instruments, and targets are usually 0.3–1.0 of each share. With the
usual 0.01-lot minimum:

| Universe | Min lot | Capital for every instrument to trade at a typical target |
|---|---|---|
| fx (7 pairs) | ~1,000 units, ≈ $580–1,350 | ≈ $15,000–25,000 |
| metals (gold) | 1 oz, ≈ $4,300 | ≈ $15,000 |

With less, small targets are skipped ("below broker minimum lot") and the
account follows the paper strategy only roughly. On a demo account, use the
capital you'd actually trade with, so the demo is honest.

## 4. Follow a strategy on the demo account

```powershell
.venv-windows\Scripts\python.exe -m live.control compare
.venv-windows\Scripts\python.exe -m live.control activate --universe fx --strategy tsmom --timeframe 1h --mode demo --capital 25000 --reason "demo test"
.venv-windows\Scripts\python.exe -m live.mt5_executor --dry-run
.venv-windows\Scripts\python.exe -m live.mt5_executor
.venv-windows\Scripts\python.exe -m live.control status
.venv-windows\Scripts\python.exe -m live.control orders
```

(On the VM setup, `live.control` runs on the VM instead; see [GCP.md](GCP.md).)

## Exness

Exness runs MT5 normally; nothing in the bot is Exness-specific except the
settings below. Download their MT5 terminal from the Exness Personal Area.
It installs separately from the MetaQuotes one.

- **Symbol names carry the account type:** `EURUSDm` / `XAUUSDm` on
  Standard, `EURUSDz` on Zero, plain `EURUSD` on Pro and Raw Spread. The
  executor finds these by itself; set `MT5_SYMBOLS` only if it reports
  several matches.
- **Standard Cent is not supported.** It is MT4-only and denominated in US
  cents; the executor refuses non-USD accounts and mis-scaled lots.
- **Server time is UTC.** `MT5_SERVER_TZ=auto` detects it; set
  `MT5_SERVER_TZ=utc` to be explicit. This matters only when the Exness
  terminal is the price source.
- **Swaps.** Many Exness accounts are swap-free. `--check-costs` shows
  zero swaps if yours is. The backtests charge financing anyway, so a
  swap-free account makes live results slightly better than paper, not worse.
- **Recommended order:** MetaQuotes demo (done) → **Exness demo**, where you
  run `--check-costs` and `--smoke-test` against Exness's real symbols,
  spreads and filling rules → Exness live.

## Demo and live together

One terminal holds one account, so a live account needs its own terminal:

1. Install the Exness MT5 terminal (it has its own folder, e.g.
   `C:\Program Files\MetaTrader 5 EXNESS\terminal64.exe`). Log in to the
   real account with "save password" ticked. Enable Algo Trading.
2. Create `.env.live` next to `.env`:

   ```ini
   TARGETS_SOURCE=ledger
   FX_DATA_SOURCE=mt5
   EXECUTOR_MODE=live
   MT5_TERMINAL_PATH=C:\Program Files\MetaTrader 5 EXNESS\terminal64.exe
   MT5_LOGIN=<your Exness real account number>
   ALLOW_LIVE_TRADING=true
   MAX_GROSS_LEVERAGE=1.0
   ```

   `MT5_LOGIN` pins the account: if that terminal is ever logged into
   another account, the executor refuses to trade. For the demo executor in
   `.env`, set `MT5_TERMINAL_PATH` to the MetaQuotes terminal too, so each
   executor attaches to the right one.
3. Check, then schedule the live executor:

   ```powershell
   .venv-windows\Scripts\python.exe -m live.mt5_executor --env-file .env.live --status
   .venv-windows\Scripts\python.exe -m live.mt5_executor --env-file .env.live --check-costs
   powershell -ExecutionPolicy Bypass -File deploy\windows\setup_executor.ps1 -EnvFile .env.live
   ```

4. Activate live on the ledger side. This needs `ALLOW_LIVE_TRADING=true` in
   `.env` there too, the typed confirmation, and `--accept-unproven` while
   the paper record isn't significant:

   ```powershell
   .venv-windows\Scripts\python.exe -m live.control activate --universe metals --strategy tsmom --timeframe 1d --mode live --capital 5000 --confirm-live "REAL MONEY" --reason "first live"
   ```

The paper jobs keep a single price source. With both terminals open,
`MT5_TERMINAL_PATH` in `.env` decides which terminal supplies prices. Use
the same broker's terminal consistently.

## Stopping

| Goal | Command |
|---|---|
| Stop new orders now (both executors) | create the file `state\KILL` in the project folder |
| Stop and close the bot's positions | `python -m live.control kill --flatten` (done at the next sync) |
| Close one universe | `python -m live.control deactivate --universe fx --flatten` |
| Pause everything | `Get-ScheduledTask 'SignalBot*' \| Disable-ScheduledTask` |
| Resume | delete `state\KILL`; `Get-ScheduledTask 'SignalBot*' \| Enable-ScheduledTask` |
| Remove the tasks | `Get-ScheduledTask 'SignalBot*' \| Unregister-ScheduledTask -Confirm:$false` |

## Real money

Live mode needs **both** keys (`ALLOW_LIVE_TRADING=true` where `live.control`
runs, and in the live executor's env file), a terminal logged into a real
account, `--confirm-live "REAL MONEY"`, and `--accept-unproven` until the
paper record is significant. Use the capital you can afford to lose. For
24/7 operation without your PC, see [LINUX_WINE_MT5.md](LINUX_WINE_MT5.md)
or [GCP.md](GCP.md).
