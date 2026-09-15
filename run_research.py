#!/usr/bin/env python3
"""Rule-based strategy research across crypto, FX and gold.

    python run_research.py --universe fx --strategy tsmom --timeframe 1d
    python run_research.py --universe crypto --strategy tsmom --timeframe 1h
    python run_research.py --universe fx --strategy carry          # daily only
    python run_research.py --universe all --strategy tsmom --offline --detail

Each asset class is ONE trial, decided at the portfolio level. The per-
instrument rows are for understanding, not for picking winners: choosing the
best pair after seeing this table multiplies the trial count, and the
per-instrument deflated Sharpe is charged for that.

No accounts or keys needed: FX and gold come from Dukascopy, crypto from Binance.
"""

from __future__ import annotations

import argparse
import sys
import warnings

from harness.backtest import benchmark_suite, combine, simulate_positions
from harness.data import load_prices, load_short_rates
from harness.instruments import TIMEFRAMES, UNIVERSES, universe
from harness.pipeline import (STRATEGIES, check_supported, financing_for, strategy_params,
                              target_positions)
from harness.scoring import score_positions
from harness.strategies import VOL_DAYS
from harness.trials import TrialCounter, code_hash
from live.env import load_env


def run_instrument(inst, strategy: str, timeframe: str, rates: dict,
                   cache_dir: str, offline: bool):
    df = load_prices(inst, timeframe, cache_dir, offline)
    close = df["close"]
    ppy = inst.periods_per_year(timeframe)
    financing = financing_for(inst, close.index, rates)
    pos, start = target_positions(inst, close, strategy, timeframe, rates)
    sim = simulate_positions(pos, close, inst.costs, financing, inst.long_only)
    benches, primary = benchmark_suite(close, inst.costs, financing, sim, inst.asset_class,
                                       inst.long_only, ppy, VOL_DAYS[timeframe], start=start)
    sim = sim.slice(start)
    benches = {k: v.slice(start) for k, v in benches.items()}

    note = f"{df.index[0].date()} -> {df.index[-1].date()}, {len(df):,} bars"
    if "spread" in df and df["spread"].notna().any():
        observed = float(df["spread"].median())
        modeled = 2 * inst.costs.half_spread
        flag = "  <-- model is OPTIMISTIC" if observed > modeled else ""
        note += (f", median observed spread {observed * 1e4:.2f}bp "
                 f"vs modeled {modeled * 1e4:.2f}bp{flag}")
    return sim, benches, primary, ppy, note


def short_verdict(v: str) -> str:
    return v.split(".")[0]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--universe", choices=sorted(UNIVERSES), default="crypto")
    ap.add_argument("--strategy", choices=STRATEGIES, default="tsmom")
    ap.add_argument("--timeframe", choices=TIMEFRAMES, default="1d")
    ap.add_argument("--cache-dir", default="data")
    ap.add_argument("--offline", action="store_true", help="use cached data only")
    ap.add_argument("--detail", action="store_true", help="full scorecard per instrument")
    ap.add_argument("--trials-file", default="trials.json")
    ap.add_argument("--env-file", default=".env",
                    help="reads FX_DATA_SOURCE / MT5_HISTORY_YEARS (FX and gold from MT5 or Dukascopy)")
    a = ap.parse_args()
    load_env(a.env_file)
    warnings.filterwarnings("ignore", category=RuntimeWarning)

    insts = universe(a.universe)
    if a.strategy == "carry":
        insts = [i for i in insts if i.asset_class == "fx"]
    try:
        for cls in {i.asset_class for i in insts} or {"none"}:
            check_supported(a.strategy, cls, a.timeframe)
    except ValueError as e:
        sys.exit(str(e))

    try:
        rate_ccys = [c for i in insts if i.uses_rates for c in (i.base, i.quote)]
        rates = load_short_rates(rate_ccys, a.cache_dir, a.offline) if rate_ccys else {}

        counter = TrialCounter(a.trials_file)
        classes = list(dict.fromkeys(i.asset_class for i in insts))
        for cls in classes:
            group = [i for i in insts if i.asset_class == cls]
            print(f"\n{'#' * 74}\n# {a.strategy.upper()} | {cls} | {a.timeframe} | "
                  f"{len(group)} instruments\n{'#' * 74}")
            runs = []
            for inst in group:
                run = run_instrument(inst, a.strategy, a.timeframe, rates,
                                     a.cache_dir, a.offline)
                print(f"  {inst.symbol:<9} {run[4]}")
                runs.append((inst, run))

            # A trial counts once results exist, not when a download fails.
            params = strategy_params(a.strategy, a.timeframe)
            n_trials = counter.record(
                {"strategy": a.strategy, "asset_class": cls, "timeframe": a.timeframe,
                 "instruments": [i.symbol for i in group], "params": params,
                 "code": code_hash()},
                note=f"{a.strategy}-{cls}-{a.timeframe}")
            print(f"  {counter.summary()}")

            sims, bench_sets, rows = [], [], []
            for inst, (sim, benches, primary, ppy, _) in runs:
                rep = score_positions(sim, benches, primary, n_trials * len(group), ppy,
                                      name=f"{inst.symbol} {a.strategy} {a.timeframe}")
                if a.detail:
                    print(rep.to_text())
                sims.append(sim)
                bench_sets.append(benches)
                rows.append((inst.symbol, rep))

            print(f"\n  {'instrument':<10}{'return':>8}{'vol':>7}{'sharpe':>8}{'maxdd':>8}"
                  f"{'turn/yr':>9}{'cost/yr':>9}{'fin/yr':>8}  verdict")
            for sym, r in rows:
                print(f"  {sym:<10}{r.ann_return:+8.1%}{r.ann_vol:7.1%}{r.sharpe:8.2f}"
                      f"{r.max_drawdown:8.1%}{r.turnover_per_year:9.1f}{r.cost_drag:9.2%}"
                      f"{r.financing_drag:+8.2%}  {short_verdict(r.verdict)}")

            portfolio = combine(sims)
            benches = {k: combine([b[k] for b in bench_sets]) for k in bench_sets[0]}
            rep = score_positions(portfolio, benches, primary, n_trials, ppy,
                                  name=f"{cls.upper()} PORTFOLIO  {a.strategy} {a.timeframe}  "
                                       f"(the decision)")
            print("\n" + rep.to_text())
    except (RuntimeError, FileNotFoundError) as e:
        sys.exit(str(e))


if __name__ == "__main__":
    main()
