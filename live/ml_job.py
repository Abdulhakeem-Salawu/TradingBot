"""Model retraining and hourly predictions: recorded on paper, never traded.

    python -m live.ml_job train      # weekly: score out-of-sample, then fit -> models/
    python -m live.ml_job predict    # after the paper jobs: a probability for each last closed bar
    python -m live.ml_job report     # how the recorded predictions have done since
    python -m live.ml_job holdout    # once: score the reserved data no model trained on

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
  MT5_SERVER   the broker server whose prices FX and gold models use
  ML_HOLDOUT_FROM  ISO date reserved from training, e.g. 2024-04-01 (unset: none)

THE HOLDOUT. Walk-forward already scores a model on data it did not train on,
five times over, so it answers "is there an edge". What it cannot do is survive
tuning: change a hyperparameter, re-run, keep the best, and the folds slowly
become in-sample. ML_HOLDOUT_FROM reserves a slice that no training run reads
at all, to be scored once when tuning is finished. It is a fixed date rather
than a fraction on purpose -- a fraction slides forward every retrain, and last
week's training rows become this week's test rows.

ONE FEED PER MODEL. A model is trained, stored and used for one price feed:
the MT5 server for FX and gold (a demo and a real server are different feeds),
binance for crypto. Models live in models/<feed>/, predictions carry their
feed, and a model is never applied to another feed's prices -- so demo prices
can never train or score a live model.

Models are pickles in models/<feed>/. Only ever load models this bot trained itself.
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

from harness.data import load_prices, price_source
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


def feed_for(inst) -> str:
    """The price feed an instrument's model is trained on and applied to."""
    source = price_source(inst)
    if source != "mt5":
        return source
    server = os.environ.get("MT5_SERVER", "")
    if not server:
        raise ValueError(f"{inst.symbol}: MT5 prices need MT5_SERVER to tell which feed a model is for")
    from live.mt5_data import feed_name

    return feed_name(server)


def model_paths(model_dir: str, feed: str, symbol: str, timeframe: str,
                horizon: int) -> tuple[Path, Path]:
    base = Path(model_dir) / feed / f"{symbol}_{timeframe}_h{horizon}"
    return base.with_suffix(".pkl"), base.with_suffix(".json")


def holdout_start() -> pd.Timestamp | None:
    """ML_HOLDOUT_FROM: the date no model may train on or past, or None for no holdout.

    A fixed date, deliberately not a fraction of the data. A fraction would slide
    forward on every retrain, so rows that trained last week's model become this
    week's test rows and the holdout quietly stops being one. Set it once and
    leave it alone; changing it invalidates every verdict measured against it.
    """
    raw = os.environ.get("ML_HOLDOUT_FROM", "").strip()
    return pd.Timestamp(raw, tz="UTC") if raw else None


def split_at_holdout(index: pd.Index, boundary: pd.Timestamp | None, horizon: int) -> int:
    """How many leading rows may be trained on.

    A label at time t reads prices up to t+horizon, so the last `horizon` rows
    before the boundary already know part of the holdout. They are purged.
    """
    if boundary is None:
        return len(index)
    return max(0, int((index < boundary).sum()) - horizon)


def training_frame(df: pd.DataFrame, costs, horizon: int) -> pd.DataFrame:
    frame = build_features(df)
    frame["_y"], frame["_fwd"] = make_labels(df["close"], LabelSpec(horizon=horizon, costs=costs))
    return frame.dropna()


# ----------------------------------------------------------------------- train
def train_one(inst, timeframe: str, horizon: int, df: pd.DataFrame, model_dir: str,
              n_models: int, feed: str, splits: int = 5) -> str:
    frame = training_frame(df, inst.costs, horizon)
    if len(frame) < 1000:
        return f"{inst.symbol:<8} {timeframe} [{feed}]: only {len(frame)} usable bars -- not trained"
    X = frame[FEATURE_NAMES].to_numpy(dtype=float)
    y = frame["_y"].to_numpy(dtype=float)
    fwd = frame["_fwd"].to_numpy(dtype=float)

    # Everything below happens on the training side of the holdout only: the
    # walk-forward folds, and the model that ships. The reserved tail is never
    # read here, so `ml_job holdout` scores it on data this model has not seen.
    boundary = holdout_start()
    n_train = split_at_holdout(frame.index, boundary, horizon)
    if n_train < 1000:
        return (f"{inst.symbol:<8} {timeframe} [{feed}]: holdout from {boundary:%Y-%m-%d} leaves "
                f"only {n_train} training bars -- not trained")
    X, y, fwd = X[:n_train], y[:n_train], fwd[:n_train]

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

    pkl, meta = model_paths(model_dir, feed, inst.symbol, timeframe, horizon)
    pkl.parent.mkdir(parents=True, exist_ok=True)
    pkl.write_bytes(pickle.dumps(new_model().fit(X, y)))
    verdict = rep.verdict.split(".")[0].strip()
    meta.write_text(json.dumps({
        "symbol": inst.symbol, "timeframe": timeframe, "horizon": horizon, "feed": feed,
        "trained_from": str(frame.index[0]),
        "trained_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "trained_until": str(frame.index[n_train - 1]), "rows": n_train,
        "holdout_from": str(boundary) if boundary is not None else None,
        "features": FEATURE_NAMES, "sklearn": sklearn.__version__,
        "oos": {"accuracy": rep.accuracy, "lift": rep.accuracy_lift,
                "expectancy": rep.net_expectancy, "trades": rep.n_trades, "verdict": verdict},
    }, indent=1))
    held = len(frame) - n_train
    return (f"{inst.symbol:<8} {timeframe} [{feed}]: {n_train:,} bars "
            f"{frame.index[0]:%Y-%m-%d} to {frame.index[n_train - 1]:%Y-%m-%d %H:%M}, "
            f"{held:,} held back, "
            f"out-of-sample lift {rep.accuracy_lift:+.1%}, expectancy {rep.net_expectancy:+.3%} "
            f"-> {verdict}")


def train(targets, horizon: int, cache_dir: str, offline: bool, model_dir: str) -> int:
    failures = 0
    for inst, tf in targets:
        try:
            feed = feed_for(inst)
            df = load_prices(inst, tf, cache_dir, offline)
            print(train_one(inst, tf, horizon, df, model_dir, n_models=len(targets), feed=feed),
                  flush=True)
        except Exception as e:  # noqa: BLE001 -- one instrument must not stop the others
            print(f"{inst.symbol:<8} {tf}: FAILED {type(e).__name__}: {e}", flush=True)
            failures += 1
    return 1 if failures else 0


# --------------------------------------------------------------------- predict
def predict_one(ledger: Ledger, inst, timeframe: str, horizon: int, df: pd.DataFrame,
                model_dir: str, feed: str) -> str | None:
    pkl, meta = model_paths(model_dir, feed, inst.symbol, timeframe, horizon)
    if not (pkl.exists() and meta.exists()):
        return None
    info = json.loads(meta.read_text())
    if info.get("feed") != feed:
        return f"{inst.symbol:<8} {timeframe}: model was trained on {info.get('feed')!r}, not {feed} -- run train"
    if info.get("sklearn") != sklearn.__version__ or info.get("features") != FEATURE_NAMES:
        return f"{inst.symbol:<8} {timeframe}: model is from another version -- run train"

    features = build_features(df)
    if features.empty or features.iloc[-1].isna().any():
        return f"{inst.symbol:<8} {timeframe}: not enough history for features"
    bar_ts = df.index[-1]
    model = pickle.loads(pkl.read_bytes())
    prob = float(model.predict_proba(features[FEATURE_NAMES].iloc[[-1]].to_numpy(dtype=float))[0, 1])
    new = ledger.add_prediction(feed, inst.symbol, timeframe, horizon, str(bar_ts), prob,
                                info["trained_until"], info["oos"]["verdict"])

    labels, fwd = make_labels(df["close"], LabelSpec(horizon=horizon, costs=inst.costs))
    for row in ledger.open_predictions(feed, inst.symbol, timeframe):
        ts = pd.Timestamp(row["bar_ts"])
        if ts in labels.index and pd.notna(labels.loc[ts]):
            ledger.resolve_prediction(feed, inst.symbol, timeframe, row["horizon"], row["bar_ts"],
                                      float(labels.loc[ts]), float(fwd.loc[ts]))
    return (f"{inst.symbol:<8} {timeframe} bar {bar_ts:%Y-%m-%d %H:%M}: p={prob:.2f} "
            f"{'LONG' if prob >= 0.5 else 'flat'}{'' if new else ' (already recorded)'}"
            f"  [model: {info['oos']['verdict']}]")


def predict(targets, horizon: int, cache_dir: str, model_dir: str, db: str) -> int:
    ledger = Ledger(db)
    failures = 0
    for inst, tf in targets:
        try:
            feed = feed_for(inst)
            line = predict_one(ledger, inst, tf, horizon,
                               load_prices(inst, tf, cache_dir, offline=True), model_dir, feed)
            if line:
                print(line, flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"{inst.symbol:<8} {tf}: FAILED {type(e).__name__}: {e}", flush=True)
            failures += 1
    ledger.commit()
    return 1 if failures else 0


def report_lines(targets, db: str) -> list[str]:
    """How the recorded predictions have actually done, per model.

    accuracy is the share of calls that matched -- LONG calls where a long did
    beat the round-trip cost, and flat calls where it did not. The baseline is
    always calling the majority class, so lift is what the model adds over
    guessing. hit rate looks at the LONG calls alone, which is the half that
    would have cost money.
    """
    ledger = Ledger(db)
    lines = [f"{'symbol':<8} {'tf':<3} {'resolved':>8} {'LONG':>5} {'accuracy':>9} "
             f"{'baseline':>9} {'lift':>7} {'hit rate':>9} {'base rate':>10}"]
    waiting, total = [], 0
    for inst, tf in targets:
        try:
            feed = feed_for(inst)
        except ValueError as e:
            lines.append(str(e))
            continue
        rows = ledger.resolved_predictions(feed, inst.symbol, tf, limit=5000)
        if not rows:
            waiting.append(inst.symbol)
            continue
        total += len(rows)
        outcome = np.array([r["outcome"] for r in rows], dtype=float)
        calls = np.array([r["prob"] >= 0.5 for r in rows])
        accuracy = float((calls == (outcome > 0.5)).mean())
        base = float(max(outcome.mean(), 1.0 - outcome.mean()))   # always guess the majority
        hit = f"{outcome[calls].mean():.1%}" if calls.any() else "-"
        lines.append(f"{inst.symbol:<8} {tf:<3} {len(rows):>8} {int(calls.sum()):>5} "
                     f"{accuracy:>9.1%} {base:>9.1%} {accuracy - base:>+7.1%} {hit:>9} "
                     f"{outcome.mean():>10.1%}")
    if waiting:
        lines.append(f"no resolved predictions yet: {', '.join(waiting)}")
    if not total:
        lines.append("Nothing has resolved yet. Each prediction needs its horizon to pass "
                     "(ML_HORIZON bars), so the first numbers appear a few hours after training.")
    lines.append("accuracy counts LONG and flat calls; lift is accuracy minus the baseline. "
                 "Predictions are paper only and never traded.")
    return lines


def report(targets, db: str, send_it: bool = False) -> int:
    text = "\n".join(["MODEL PREDICTIONS"] + report_lines(targets, db))
    print(text)
    if send_it:
        from live.notify import send

        send(text, pre=True)     # monospace, so the columns line up in Telegram
    return 0


# --------------------------------------------------------------------- holdout
def holdout_one(inst, timeframe: str, horizon: int, df: pd.DataFrame, model_dir: str,
                feed: str, n_models: int) -> str:
    """Score the shipped model over the reserved tail it was never trained on."""
    boundary = holdout_start()
    if boundary is None:
        return "ML_HOLDOUT_FROM is not set: nothing is reserved, so there is nothing to score"
    pkl, meta = model_paths(model_dir, feed, inst.symbol, timeframe, horizon)
    if not (pkl.exists() and meta.exists()):
        return f"{inst.symbol:<8} {timeframe} [{feed}]: no model -- run train"
    info = json.loads(meta.read_text())
    if info.get("holdout_from") != str(boundary):
        return (f"{inst.symbol:<8} {timeframe}: model reserved {info.get('holdout_from')}, "
                f"not {boundary} -- retrain before scoring")
    if info.get("sklearn") != sklearn.__version__ or info.get("features") != FEATURE_NAMES:
        return f"{inst.symbol:<8} {timeframe}: model is from another version -- run train"

    frame = training_frame(df, inst.costs, horizon)
    test = frame[frame.index >= boundary]
    if len(test) < 100:
        return f"{inst.symbol:<8} {timeframe}: only {len(test)} holdout bars -- too few to score"
    model = pickle.loads(pkl.read_bytes())
    probs = model.predict_proba(test[FEATURE_NAMES].to_numpy(dtype=float))[:, 1]
    rep = score_predictions(test["_y"].to_numpy(dtype=float), probs,
                            test["_fwd"].to_numpy(dtype=float), inst.costs,
                            threshold=0.5, n_trials=n_models, horizon=horizon)
    return (f"{inst.symbol:<8} {timeframe} [{feed}]: {len(test):,} holdout bars "
            f"{test.index[0]:%Y-%m-%d} to {test.index[-1]:%Y-%m-%d}, "
            f"lift {rep.accuracy_lift:+.1%}, expectancy {rep.net_expectancy:+.3%} "
            f"-> {rep.verdict.split('.')[0].strip()}")


def holdout(targets, horizon: int, cache_dir: str, offline: bool, model_dir: str) -> int:
    """One scoring pass over the reserved data. Every look costs statistical
    power (harness/trials.py counts it), so run this once, when tuning is done."""
    failures = 0
    for inst, tf in targets:
        try:
            feed = feed_for(inst)
            df = load_prices(inst, tf, cache_dir, offline)
            print(holdout_one(inst, tf, horizon, df, model_dir, feed, n_models=len(targets)),
                  flush=True)
        except Exception as e:  # noqa: BLE001 -- one instrument must not stop the others
            print(f"{inst.symbol:<8} {tf}: FAILED {type(e).__name__}: {e}", flush=True)
            failures += 1
    return 1 if failures else 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Train models and record predictions (paper only)")
    ap.add_argument("task", choices=["train", "predict", "report", "holdout"])
    ap.add_argument("--targets", default=None, help=f'default: ML_TARGETS, else "{DEFAULT_TARGETS}"')
    ap.add_argument("--horizon", type=int, default=None, help="default: ML_HORIZON, else 6")
    ap.add_argument("--cache-dir", default="data")
    ap.add_argument("--model-dir", default=MODEL_DIR)
    ap.add_argument("--db", default="state/ledger.db")
    ap.add_argument("--offline", action="store_true", help="train on cached prices only")
    ap.add_argument("--send", action="store_true", help="report: also send it to Telegram")
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
    if a.task == "holdout":
        return holdout(targets, horizon, a.cache_dir, a.offline, a.model_dir)
    if a.task == "predict":
        return predict(targets, horizon, a.cache_dir, a.model_dir, a.db)
    return report(targets, a.db, a.send)


if __name__ == "__main__":
    sys.exit(main())
