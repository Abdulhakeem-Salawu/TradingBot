"""Model retraining and hourly predictions: recorded on paper, never traded.

    python -m live.ml_job train      # weekly: score out-of-sample, then fit on all history -> models/
    python -m live.ml_job predict    # after the paper jobs: a probability for each last closed bar
    python -m live.ml_job report     # how the recorded predictions have done since

The model is run_backtest.py's classifier with the same features
(harness/features.py), cost-aware labels (harness/labels.py: would a long held
`horizon` bars beat the round-trip cost?) and hyperparameters. Training first
scores it with purged walk-forward splits and stores that verdict next to the
model, so every prediction carries its model's evidence. The research so far
found NO EDGE in these models: predictions are recorded to measure that on
live data, and no order is ever placed from them.

Settings (environment or .env):
  ML_TARGETS   universe:timeframe list (default "fx:1h metals:1h crypto:1h")
  ML_HORIZON   bars per prediction (default 6)

Models are pickles in models/. Only ever load models this bot trained itself.
"""

from __future__ import annotations

import argparse
import json
import os
import pickle
import sys
import warnings
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import sklearn
from sklearn.ensemble import HistGradientBoostingClassifier

from harness.data import load_prices
from harness.features import FEATURE_NAMES, build_features
from harness.instruments import TIMEFRAMES, UNIVERSES, universe
from harness.labels import LabelSpec, make_labels
from harness.scoring import score_predictions
from harness.splits import PurgedWalkForward
from live.env import load_env
from live.ledger import Ledger

DEFAULT_TARGETS = "fx:1h metals:1h crypto:1h"
MODEL_DIR = "models"


def new_model() -> HistGradientBoostingClassifier:
    return HistGradientBoostingClassifier(max_iter=200, max_depth=3, learning_rate=0.05,
                                          min_samples_leaf=50, l2_regularization=1.0,
                                          random_state=42)


def parse_targets(text: str):
    """[(instrument, timeframe)] from 'universe:timeframe ...', each pair once."""
    out, seen = [], set()
    for item in text.split():
        uni, _, tf = item.partition(":")
        if uni not in UNIVERSES or tf not in TIMEFRAMES:
            raise ValueError(f"ML_TARGETS item {item!r}: use universe:timeframe, e.g. fx:1h")
        for inst in universe(uni):
            if (inst.symbol, tf) not in seen:
                seen.add((inst.symbol, tf))
                out.append((inst, tf))
    return out


def model_paths(model_dir: str, symbol: str, timeframe: str, horizon: int) -> tuple[Path, Path]:
    base = Path(model_dir) / f"{symbol}_{timeframe}_h{horizon}"
    return base.with_suffix(".pkl"), base.with_suffix(".json")


def training_frame(df: pd.DataFrame, costs, horizon: int) -> pd.DataFrame:
    frame = build_features(df)
    frame["_y"], frame["_fwd"] = make_labels(df["close"], LabelSpec(horizon=horizon, costs=costs))
    return frame.dropna()


# ----------------------------------------------------------------------- train
def train_one(inst, timeframe: str, horizon: int, df: pd.DataFrame, model_dir: str,
              n_models: int, splits: int = 5) -> str:
    frame = training_frame(df, inst.costs, horizon)
    if len(frame) < 1000:
        return f"{inst.symbol:<8} {timeframe}: only {len(frame)} usable bars -- not trained"
    X = frame[FEATURE_NAMES].to_numpy(dtype=float)
    y = frame["_y"].to_numpy(dtype=float)
    fwd = frame["_fwd"].to_numpy(dtype=float)

    cv = PurgedWalkForward(n_splits=splits, label_horizon=horizon, embargo_frac=0.01,
                           min_train=max(750, len(X) // 6))
    truth, probs, fwds = [], [], []
    for tr, te in cv.split(len(X)):
        probs.append(new_model().fit(X[tr], y[tr]).predict_proba(X[te])[:, 1])
        truth.append(y[te])
        fwds.append(fwd[te])
    # Every model trained in the same run is another try: charge them all.
    rep = score_predictions(np.concatenate(truth), np.concatenate(probs), np.concatenate(fwds),
                            inst.costs, threshold=0.5, n_trials=n_models, horizon=horizon)

    pkl, meta = model_paths(model_dir, inst.symbol, timeframe, horizon)
    pkl.parent.mkdir(parents=True, exist_ok=True)
    pkl.write_bytes(pickle.dumps(new_model().fit(X, y)))
    verdict = rep.verdict.split(".")[0].strip()
    meta.write_text(json.dumps({
        "symbol": inst.symbol, "timeframe": timeframe, "horizon": horizon,
        "trained_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "trained_until": str(frame.index[-1]), "rows": len(frame),
        "features": FEATURE_NAMES, "sklearn": sklearn.__version__,
        "oos": {"accuracy": rep.accuracy, "lift": rep.accuracy_lift,
                "expectancy": rep.net_expectancy, "trades": rep.n_trades, "verdict": verdict},
    }, indent=1))
    return (f"{inst.symbol:<8} {timeframe}: {len(frame):,} bars to {frame.index[-1]:%Y-%m-%d %H:%M}, "
            f"out-of-sample lift {rep.accuracy_lift:+.1%}, expectancy {rep.net_expectancy:+.3%} "
            f"-> {verdict}")


def train(targets, horizon: int, cache_dir: str, offline: bool, model_dir: str) -> int:
    failures = 0
    for inst, tf in targets:
        try:
            df = load_prices(inst, tf, cache_dir, offline)
            print(train_one(inst, tf, horizon, df, model_dir, n_models=len(targets)), flush=True)
        except Exception as e:  # noqa: BLE001 -- one instrument must not stop the others
            print(f"{inst.symbol:<8} {tf}: FAILED {type(e).__name__}: {e}", flush=True)
            failures += 1
    return 1 if failures else 0


# --------------------------------------------------------------------- predict
def predict_one(ledger: Ledger, inst, timeframe: str, horizon: int, df: pd.DataFrame,
                model_dir: str) -> str | None:
    pkl, meta = model_paths(model_dir, inst.symbol, timeframe, horizon)
    if not (pkl.exists() and meta.exists()):
        return None
    info = json.loads(meta.read_text())
    if info.get("sklearn") != sklearn.__version__ or info.get("features") != FEATURE_NAMES:
        return f"{inst.symbol:<8} {timeframe}: model is from another version -- run train"

    features = build_features(df)
    if features.empty or features.iloc[-1].isna().any():
        return f"{inst.symbol:<8} {timeframe}: not enough history for features"
    bar_ts = df.index[-1]
    model = pickle.loads(pkl.read_bytes())
    prob = float(model.predict_proba(features[FEATURE_NAMES].iloc[[-1]].to_numpy(dtype=float))[0, 1])
    new = ledger.add_prediction(inst.symbol, timeframe, horizon, str(bar_ts), prob,
                                info["trained_until"], info["oos"]["verdict"])

    labels, fwd = make_labels(df["close"], LabelSpec(horizon=horizon, costs=inst.costs))
    for row in ledger.open_predictions(inst.symbol, timeframe):
        ts = pd.Timestamp(row["bar_ts"])
        if ts in labels.index and pd.notna(labels.loc[ts]):
            ledger.resolve_prediction(inst.symbol, timeframe, row["horizon"], row["bar_ts"],
                                      float(labels.loc[ts]), float(fwd.loc[ts]))
    return (f"{inst.symbol:<8} {timeframe} bar {bar_ts:%Y-%m-%d %H:%M}: p={prob:.2f} "
            f"{'LONG' if prob >= 0.5 else 'flat'}{'' if new else ' (already recorded)'}"
            f"  [model: {info['oos']['verdict']}]")


def predict(targets, horizon: int, cache_dir: str, model_dir: str, db: str) -> int:
    ledger = Ledger(db)
    failures = 0
    for inst, tf in targets:
        try:
            line = predict_one(ledger, inst, tf, horizon,
                               load_prices(inst, tf, cache_dir, offline=True), model_dir)
            if line:
                print(line, flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"{inst.symbol:<8} {tf}: FAILED {type(e).__name__}: {e}", flush=True)
            failures += 1
    ledger.commit()
    return 1 if failures else 0


def report(targets, db: str) -> int:
    ledger = Ledger(db)
    print(f"{'symbol':<8} {'tf':<3} {'resolved':>8} {'LONG calls':>10} {'hit rate':>8} "
          f"{'base rate':>9} {'avg fwd when LONG':>17}")
    for inst, tf in targets:
        rows = ledger.resolved_predictions(inst.symbol, tf, limit=5000)
        if not rows:
            continue
        outcome = np.array([r["outcome"] for r in rows])
        calls = np.array([r["prob"] >= 0.5 for r in rows])
        fwd = np.array([r["fwd_return"] for r in rows])
        hit = f"{outcome[calls].mean():.1%}" if calls.any() else "-"
        avg = f"{fwd[calls].mean():+.3%}" if calls.any() else "-"
        print(f"{inst.symbol:<8} {tf:<3} {len(rows):>8} {int(calls.sum()):>10} {hit:>8} "
              f"{outcome.mean():>9.1%} {avg:>17}")
    print("hit rate = share of LONG calls that beat the round-trip cost; compare with base rate.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Train models and record predictions (paper only)")
    ap.add_argument("task", choices=["train", "predict", "report"])
    ap.add_argument("--targets", default=None, help=f'default: ML_TARGETS, else "{DEFAULT_TARGETS}"')
    ap.add_argument("--horizon", type=int, default=None, help="default: ML_HORIZON, else 6")
    ap.add_argument("--cache-dir", default="data")
    ap.add_argument("--model-dir", default=MODEL_DIR)
    ap.add_argument("--db", default="state/ledger.db")
    ap.add_argument("--offline", action="store_true", help="train on cached prices only")
    ap.add_argument("--env-file", default=".env")
    a = ap.parse_args()
    warnings.filterwarnings("ignore")
    load_env(a.env_file)
    try:
        targets = parse_targets(a.targets or os.environ.get("ML_TARGETS") or DEFAULT_TARGETS)
    except ValueError as e:
        print(e)
        return 2
    horizon = a.horizon or int(os.environ.get("ML_HORIZON", "6"))
    if a.task == "train":
        return train(targets, horizon, a.cache_dir, a.offline, a.model_dir)
    if a.task == "predict":
        return predict(targets, horizon, a.cache_dir, a.model_dir, a.db)
    return report(targets, a.db)


if __name__ == "__main__":
    sys.exit(main())
