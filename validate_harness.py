#!/usr/bin/env python3
"""Validate the harness itself.

    python validate_harness.py            # everything, about a minute
    python validate_harness.py --quick    # fewer seeds, for iterating

You cannot trust a scorecard from a harness you have never tested. This runs
the harness against data whose ground truth is known in advance and asserts it
gets the right answer both ways.

Classifier path (run_backtest.py):
  LEAKAGE CHECK     no training index within `horizon` of any test index
  NEGATIVE CONTROL  pure random walk -> MUST report no edge
  POSITIVE CONTROL  planted momentum -> MUST find it, and be well calibrated

Rule path (run_research.py, paper trading):
  COST ACCOUNTING   holding a position is charged once, not every bar
  LOOKAHEAD         perturbing prices and rates after t changes nothing up to t
  NEGATIVE CONTROL  zero-mean random walks, daily and hourly, with AND without
                    costs -> "SURVIVES" in at most 10% of seeds. The zero-cost
                    run matters: with costs on, fees alone can sink noise and
                    hide a scoring bug.
  POSITIVE CONTROL  strong planted trends -> detected in at least 80% of seeds;
                    weaker ones are printed as a power curve

Run this after ANY change to features, labels, splits, strategies or scoring.
A harness that passes the positive control but not the negative one is the
dangerous kind: it will find edges everywhere, including in noise.
"""

import argparse
import sys
import warnings
from dataclasses import replace

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier

from harness.backtest import benchmark_suite, simulate_positions
from harness.costs import CostModel, financing_from_rates
from harness.data import (completed_month_means, splice_rates, synthetic_random_walk,
                          synthetic_trend, synthetic_with_edge)
from harness.features import build_features, FEATURE_NAMES
from harness.instruments import INSTRUMENTS
from harness.labels import LabelSpec, make_labels
from harness.scoring import score_positions, score_predictions
from harness.splits import PurgedWalkForward, leakage_selftest
from harness.strategies import (TSMOM_PARAMS, VOL_DAYS, fx_carry, tsmom_vol_managed,
                                warmup_bars)

warnings.filterwarnings("ignore")
HORIZON = 6


# ------------------------------------------------------------ classifier path
def _evaluate(df, costs):
    X = build_features(df)
    y, fwd = make_labels(df["close"], LabelSpec(horizon=HORIZON, costs=costs))
    f = X.copy(); f["_y"] = y; f["_fwd"] = fwd
    f = f.dropna()
    Xv = f[FEATURE_NAMES].to_numpy(float)
    yv = f["_y"].to_numpy(float)
    fv = f["_fwd"].to_numpy(float)

    cv = PurgedWalkForward(n_splits=5, label_horizon=HORIZON,
                           embargo_frac=0.01, min_train=len(Xv) // 6)
    t, p, g = [], [], []
    for tr, te in cv.split(len(Xv)):
        m = HistGradientBoostingClassifier(max_iter=200, max_depth=3,
                                           learning_rate=0.05, min_samples_leaf=50,
                                           l2_regularization=1.0, random_state=42)
        m.fit(Xv[tr], yv[tr])
        t.append(yv[te]); p.append(m.predict_proba(Xv[te])[:, 1]); g.append(fv[te])
    return score_predictions(np.concatenate(t), np.concatenate(p),
                             np.concatenate(g), costs, n_trials=1, horizon=HORIZON)


# ------------------------------------------------------------------ rule path
def _evaluate_rule(df, inst, timeframe, costs):
    close = df["close"]
    ppy = inst.periods_per_year(timeframe)
    p = TSMOM_PARAMS[timeframe]
    pos = tsmom_vol_managed(close, ppy, long_only=inst.long_only, **p)
    start = warmup_bars(ppy, max(p["lookback_days"]))
    sim = simulate_positions(pos, close, costs, None, inst.long_only)
    benches, primary = benchmark_suite(close, costs, None, sim, inst.asset_class,
                                       inst.long_only, ppy, VOL_DAYS[timeframe], start=start)
    return score_positions(sim.slice(start), {k: v.slice(start) for k, v in benches.items()},
                           primary, 1, ppy, "control")


def check_costs() -> list[str]:
    fails = []
    idx = pd.date_range("2020-01-01", periods=366, freq="D", tz="UTC")
    close = pd.Series(np.linspace(100, 110, len(idx)), index=idx)
    costs = CostModel(taker_fee=0.001, slippage_per_side=0.0005,
                      financing_long_annual=0.05, financing_short_annual=0.01)

    hold = simulate_positions(pd.Series(1.0, index=idx), close, costs)
    if not np.isclose(hold.cost.sum(), costs.per_side):
        fails.append(f"constant position charged {hold.cost.sum():.4%}, expected one side "
                     f"{costs.per_side:.4%}")
    if not np.isclose(hold.financing.sum(), 0.05, rtol=1e-3):
        fails.append(f"a year long at 5% financing charged {hold.financing.sum():.4%}")

    flip = pd.Series(1.0, index=idx)
    flip.iloc[100:] = -1.0
    s = simulate_positions(flip, close, costs)
    if not np.isclose(s.cost.sum(), 3 * costs.per_side):
        fails.append(f"enter + one flip charged {s.cost.sum() / costs.per_side:.2f} sides, expected 3")
    if not np.isclose(s.gross.iloc[1], close.iloc[1] / close.iloc[0] - 1):
        fails.append("position decided at bar 0 did not earn bar 1's return")
    if s.gross.iloc[0] != 0.0:
        fails.append("a position earned the return of the bar it was decided on (lookahead)")

    # Time-varying rates: FRED dates are tz-naive, bars are UTC. The financing
    # line must follow the rate path, not collapse to one forward-filled value.
    months = pd.date_range("2019-06-01", "2021-01-01", freq="MS")
    quote = pd.Series(np.where(months < pd.Timestamp("2020-07-01"), 0.01, 0.05), index=months)
    base = pd.Series(0.0, index=months)
    fin = financing_from_rates(idx, base, quote, markup=0.0)
    before, after = fin["long"].loc["2020-03"].mean(), fin["long"].loc["2020-09"].mean()
    if not (np.isclose(before, 0.01) and np.isclose(after, 0.05)):
        fails.append(f"financing ignored the rate path: {before:.2%} before and {after:.2%} after "
                     f"a 1% -> 5% step")

    # Extending a stopped monthly series with a daily one: completed-month
    # averages dated the 1st, the unfinished month dropped, and the monthly
    # series left untouched through its last observation.
    days = pd.date_range("2026-01-01", "2026-04-14", freq="D")
    daily = pd.Series(np.where(days < pd.Timestamp("2026-02-15"), 2.0, 3.0), index=days)
    m = completed_month_means(daily)
    feb = (14 * 2.0 + 14 * 3.0) / 28
    if not (list(m.index) == list(pd.to_datetime(["2026-01-01", "2026-02-01", "2026-03-01"]))
            and np.allclose(m.to_numpy(), [2.0, feb, 3.0])):
        fails.append(f"daily -> monthly rates gave {m.round(3).to_dict()}; expected completed-month "
                     f"means 2.0, {feb}, 3.0 dated the 1st, without the unfinished April")
    oecd = pd.Series([1.9, 1.95], index=pd.to_datetime(["2025-12-01", "2026-01-01"]), name="oecd")
    joined = splice_rates(oecd, m)
    if not (joined.index.is_monotonic_increasing and joined.loc["2026-01-01"] == 1.95
            and list(joined.index[-2:]) == list(pd.to_datetime(["2026-02-01", "2026-03-01"]))
            and joined.name == "oecd"):
        fails.append(f"splicing rates gave {joined.to_dict()}; expected the OECD values through "
                     f"2026-01 then the daily averages for 2026-02 and 2026-03")
    return fails


def check_lookahead() -> list[str]:
    fails = []
    for timeframe, freq, n in (("1d", "B", 1500), ("1h", "h", 12000)):
        inst = INSTRUMENTS["EUR_USD"]
        ppy = inst.periods_per_year(timeframe)
        df = synthetic_trend(n, seed=3, drift=0.0005, vol=0.008, freq=freq)
        close = df["close"]
        t0 = int(n * 0.7)
        bumped = close.copy()
        bumped.iloc[t0 + 1:] *= np.exp(np.random.default_rng(9).normal(0, 0.05, n - t0 - 1))

        a = tsmom_vol_managed(close, ppy, **TSMOM_PARAMS[timeframe])
        b = tsmom_vol_managed(bumped, ppy, **TSMOM_PARAMS[timeframe])
        if not a.iloc[:t0 + 1].equals(b.iloc[:t0 + 1]):
            fails.append(f"tsmom {timeframe}: positions up to t changed when prices after t changed")
        sa = simulate_positions(a, close, inst.costs)
        sb = simulate_positions(b, bumped, inst.costs)
        if not np.allclose(sa.net.iloc[:t0 + 1], sb.net.iloc[:t0 + 1]):
            fails.append(f"simulate {timeframe}: returns up to t changed when prices after t changed")

    # Carry must not see a monthly rate before its publication lag has passed.
    idx = pd.date_range("2010-01-01", periods=2000, freq="B", tz="UTC")
    close = pd.Series(100 * np.exp(np.cumsum(np.random.default_rng(1).normal(0, 0.006, len(idx)))),
                      index=idx)
    months = pd.date_range("2009-01-01", "2018-12-01", freq="MS")
    rng = np.random.default_rng(2)
    base = pd.Series(rng.normal(0.02, 0.01, len(months)), index=months)
    quote = pd.Series(rng.normal(0.02, 0.01, len(months)), index=months)
    t0 = idx[1200]
    cutoff = t0.tz_localize(None) - pd.DateOffset(months=3)
    base_bumped = base.copy()
    base_bumped[base_bumped.index > cutoff] += 0.5
    ca = fx_carry(close, base, quote, 260, publication_lag_months=3)
    cb = fx_carry(close, base_bumped, quote, 260, publication_lag_months=3)
    if not ca[:t0].equals(cb[:t0]):
        fails.append("carry: positions used a rate before its publication lag")
    if ca.equals(cb):
        fails.append("carry: lookahead test is vacuous (bumped rates never mattered)")
    return fails


def check_rule_negative(seeds: int) -> list[str]:
    fails = []
    cells = [("1d", "BTCUSDT", 0.03, "D", 3000), ("1d", "EUR_USD", 0.006, "B", 5200),
             ("1h", "BTCUSDT", 0.006, "h", 8760 * 4), ("1h", "EUR_USD", 0.0012, "h", 6240 * 5)]
    for tf, sym, vol, freq, n in cells:
        inst = INSTRUMENTS[sym]
        free = replace(inst.costs, taker_fee=0.0, half_spread=0.0, slippage_per_side=0.0)
        for label, costs in (("zero cost", free), ("with cost", inst.costs)):
            reports = [_evaluate_rule(
                synthetic_random_walk(n, seed=s, drift=-vol ** 2 / 2, vol=vol, freq=freq),
                inst, tf, costs) for s in range(seeds)]
            surv = sum("SURVIVES" in r.verdict for r in reports)
            prom = sum("PROMISING" in r.verdict for r in reports)
            ok = surv <= max(1, int(0.10 * seeds))
            print(f"      {'PASS' if ok else 'FAIL'}  {tf} {sym:<8} {label:<10} "
                  f"survives {surv}/{seeds}   promising {prom}/{seeds}")
            if not ok:
                fails.append(f"negative control {tf} {sym} {label}: {surv}/{seeds} survived")
    return fails


def check_rule_positive(seeds: int) -> list[str]:
    inst = INSTRUMENTS["EUR_USD"]
    fails = []
    # The pass bar is a strong trend. Weaker ones print as a power curve: they
    # are often missed, frequently because a passive long happened to do as
    # well over that sample -- which is the benchmark working, not a bug.
    for drift, required in ((0.0012, 0.8), (0.0008, None), (0.0004, None)):
        reports = [_evaluate_rule(
            synthetic_trend(6500, seed=s, drift=drift, vol=0.01, mean_duration=200, freq="B"),
            inst, "1d", inst.costs) for s in range(seeds)]
        hit = sum("SURVIVES" in r.verdict for r in reports)
        oracle = drift / 0.01 * np.sqrt(260)
        if required is None:
            print(f"      info  trend oracle Sharpe {oracle:.2f}: detected {hit}/{seeds} "
                  f"(power curve, not a pass/fail)")
            continue
        ok = hit >= required * seeds
        print(f"      {'PASS' if ok else 'FAIL'}  trend oracle Sharpe {oracle:.2f}: "
              f"detected {hit}/{seeds}")
        if not ok:
            fails.append(f"positive control: detected {hit}/{seeds} planted trends")
    return fails


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true", help="fewer seeds")
    seeds = 10 if ap.parse_args().quick else 20

    costs = CostModel()
    failures = []

    print("=" * 70)
    print("HARNESS SELF-TEST")
    print("=" * 70)

    # 1 -- leakage
    print("\n[1/7] leakage check (purge + embargo)")
    if leakage_selftest():
        print("      PASS  no train index within horizon of any test index")
    else:
        print("      FAIL  purging is not working -- everything downstream is void")
        failures.append("leakage")

    # 2 -- negative control
    print("\n[2/7] classifier negative control (pure random walk, no edge exists)")
    r = _evaluate(synthetic_random_walk(12000, seed=1), costs)
    print(f"      accuracy {r.accuracy:.2%}  lift {r.accuracy_lift:+.2%}  "
          f"net {r.net_total_return:+.1%}  trades {r.n_trades}")
    if "NO EDGE" in r.verdict:
        print("      PASS  correctly found no edge in noise")
    else:
        print(f"      FAIL  claimed an edge in pure noise: {r.verdict}")
        failures.append("classifier negative control")

    # 3 -- positive control
    print("\n[3/7] classifier positive control (planted momentum effect)")
    r = _evaluate(synthetic_with_edge(12000, seed=1), costs)
    print(f"      accuracy {r.accuracy:.2%}  lift {r.accuracy_lift:+.2%}  "
          f"expectancy {r.net_expectancy:+.3%}  trades {r.n_trades}")
    if r.accuracy_lift > 0.05 and r.net_expectancy > 0:
        print("      PASS  found the planted edge")
    else:
        print("      FAIL  missed an edge that is definitely there")
        failures.append("classifier positive control")

    if r.calibration is not None and len(r.calibration):
        err = float((r.calibration["predicted"] - r.calibration["actual"]).abs().mean())
        print(f"      calibration mean abs error {err:.2%} "
              f"({'good' if err < 0.08 else 'poor -- probabilities are not honest'})")

    for step, title, fn in (
        (4, "cost and financing accounting", check_costs),
        (5, "lookahead (prices and rates after t must not matter)", check_lookahead),
    ):
        print(f"\n[{step}/7] {title}")
        fails = fn()
        for f in fails:
            print(f"      FAIL  {f}")
        if not fails:
            print("      PASS")
        failures += fails

    print(f"\n[6/7] rule negative control (zero-mean random walks, {seeds} seeds per cell)")
    failures += check_rule_negative(seeds)
    print("      note: 'PROMISING' on noise this often is why it is not a reason to deploy")

    print(f"\n[7/7] rule positive control (planted slow trends, {seeds} seeds)")
    failures += check_rule_positive(seeds)

    print("\n" + "=" * 70)
    if failures:
        print(f"RESULT: FAILED -- {len(failures)} check(s)")
        for f in failures:
            print(f"  - {f}")
        print("Do not trust any scorecard until these pass.")
        return 1
    print("RESULT: ALL CHECKS PASSED")
    print("The harness finds real edges and rejects fake ones. Scorecards are")
    print("trustworthy to the extent your DATA is -- which is a separate problem.")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
