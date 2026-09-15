#!/usr/bin/env python3
"""Walk-forward prediction harness.

    python run_backtest.py --symbol BTCUSDT --interval 1h --horizon 6
    python run_backtest.py --symbol XAU_USD --interval 1h     # 20 years of Dukascopy history
    python run_backtest.py --symbol EUR_USD --interval 1d --horizon 5
    python run_backtest.py --synthetic edge      # positive control
    python run_backtest.py --synthetic noise     # negative control

Every prediction is made by a model that has never seen the bar it is
predicting, nor any bar whose outcome overlaps it. That is the whole game.

Registry instruments (crypto, FX, gold) load their full history through the
same cached loaders as run_research.py and are scored with their own cost
preset unless --fee / --slippage override it.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import replace

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier

from harness.costs import CostModel
from harness.data import (fetch_binance_ohlcv, load_prices, synthetic_random_walk,
                          synthetic_with_edge)
from harness.instruments import INSTRUMENTS, TIMEFRAMES
from harness.features import build_features, FEATURE_NAMES
from harness.labels import LabelSpec, make_labels, label_balance
from harness.scoring import score_predictions
from harness.splits import PurgedWalkForward
from harness.trials import SYNTHETIC_TRIALS_FILE, TrialCounter, code_hash
from live.env import load_env


def run(df: pd.DataFrame, horizon: int, costs: CostModel, n_splits: int,
        threshold: float, counter: TrialCounter, tag: str) -> None:
    print(f"\nbars: {len(df):,}   span: {df.index[0].date()} -> {df.index[-1].date()}")
    print(f"costs: {costs.describe()}")

    X = build_features(df)
    spec = LabelSpec(horizon=horizon, costs=costs)
    y, fwd = make_labels(df["close"], spec)

    frame = X.copy()
    frame["_y"] = y
    frame["_fwd"] = fwd
    frame = frame.dropna()
    if len(frame) < 1000:
        sys.exit(f"Only {len(frame)} usable rows after warmup. Need more history.")

    Xv = frame[FEATURE_NAMES].to_numpy(dtype=float)
    yv = frame["_y"].to_numpy(dtype=float)
    fv = frame["_fwd"].to_numpy(dtype=float)

    bal = label_balance(frame["_y"])
    print(f"\nlabel base rate: {bal['positive_rate']:.2%} of bars clear the "
          f"{costs.round_trip:.2%} cost hurdle over {horizon} bars")
    print(f"  -> {bal['note']}")

    cv = PurgedWalkForward(n_splits=n_splits, label_horizon=horizon,
                           embargo_frac=0.01, min_train=max(750, len(Xv) // 6))
    print("\n" + cv.describe(len(Xv)))

    oos_true, oos_prob, oos_fwd = [], [], []
    for k, (tr, te) in enumerate(cv.split(len(Xv)), 1):
        model = HistGradientBoostingClassifier(
            max_iter=200, max_depth=3, learning_rate=0.05,
            min_samples_leaf=50, l2_regularization=1.0, random_state=42,
        )
        model.fit(Xv[tr], yv[tr])
        p = model.predict_proba(Xv[te])[:, 1]
        oos_true.append(yv[te]); oos_prob.append(p); oos_fwd.append(fv[te])
        print(f"  fold {k}: trained on {len(tr):,}, predicted {len(te):,}")

    y_true = np.concatenate(oos_true)
    y_prob = np.concatenate(oos_prob)
    g_fwd = np.concatenate(oos_fwd)

    n_trials = counter.record(
        {"tag": tag, "features": FEATURE_NAMES, "horizon": horizon,
         "threshold": threshold, "n_splits": n_splits,
         "costs": costs.round_trip, "model": "HistGB(d3,lr0.05,200)",
         "code": code_hash()},
        note=tag,
    )
    print(f"\n{counter.summary()}")

    report = score_predictions(y_true, y_prob, g_fwd, costs,
                               threshold=threshold, n_trials=n_trials,
                               horizon=horizon)
    print("\n" + report.to_text())


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="BTCUSDT",
                    help="registry name (EUR_USD, XAU_USD, BTCUSDT, ...) or any Binance pair")
    ap.add_argument("--interval", default="1h")
    ap.add_argument("--bars", type=int, default=None,
                    help="use only the most recent N bars (default: all history)")
    ap.add_argument("--horizon", type=int, default=6,
                    help="bars held per trade; also the purge width")
    ap.add_argument("--splits", type=int, default=5)
    ap.add_argument("--threshold", type=float, default=0.5)
    ap.add_argument("--fee", type=float, default=None,
                    help="taker fee per side (default: the instrument's cost preset)")
    ap.add_argument("--slippage", type=float, default=None,
                    help="per side (default: the instrument's cost preset)")
    ap.add_argument("--synthetic", choices=["edge", "noise"], default=None)
    ap.add_argument("--cache-dir", default="data")
    ap.add_argument("--offline", action="store_true", help="use cached data only")
    ap.add_argument("--trials-file", default="trials.json")
    ap.add_argument("--env-file", default=".env",
                    help="reads FX_DATA_SOURCE / MT5_HISTORY_YEARS (FX and gold from MT5 or Dukascopy)")
    a = ap.parse_args()
    load_env(a.env_file)

    counter = TrialCounter(SYNTHETIC_TRIALS_FILE if a.synthetic else a.trials_file)
    inst = INSTRUMENTS.get(a.symbol)
    costs = inst.costs if inst else CostModel()
    if a.fee is not None or a.slippage is not None:
        costs = replace(costs, taker_fee=costs.taker_fee if a.fee is None else a.fee,
                        slippage_per_side=(costs.slippage_per_side if a.slippage is None
                                           else a.slippage))

    if a.synthetic == "noise":
        print("SYNTHETIC: pure random walk (negative control -- no edge exists)")
        df, tag = synthetic_random_walk(12000), "synthetic-noise"
    elif a.synthetic == "edge":
        print("SYNTHETIC: planted momentum effect (positive control)")
        df, tag = synthetic_with_edge(12000), "synthetic-edge"
    elif inst is not None:
        if a.interval not in TIMEFRAMES:
            sys.exit(f"--interval must be one of {TIMEFRAMES} for registry instruments")
        try:
            df = load_prices(inst, a.interval, a.cache_dir, a.offline)
        except (RuntimeError, FileNotFoundError) as e:
            sys.exit(str(e))
        tag = f"{a.symbol}-{a.interval}"
    else:
        df = fetch_binance_ohlcv(a.symbol, a.interval, a.bars or 20000, cache_dir=a.cache_dir,
                                 offline=a.offline)
        tag = f"{a.symbol}-{a.interval}"
    if a.bars:
        df = df.iloc[-a.bars:]

    run(df, a.horizon, costs, a.splits, a.threshold, counter, tag)


if __name__ == "__main__":
    main()
