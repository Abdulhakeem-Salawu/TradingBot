"""Prediction scoring.

Reports the accuracy you asked for -- AND the numbers that make accuracy mean
something. Accuracy in isolation is close to useless: in a rising market,
"always predict up" scores 53-58%, so a model reporting 60% may be adding
almost nothing, and after costs may be strictly worse than doing nothing.

Every run therefore scores four baselines alongside the model. If the model
does not beat the best baseline on NET EXPECTANCY, it has no edge, whatever
its accuracy says.
"""

from dataclasses import dataclass, field

import numpy as np
import pandas as pd
from scipy import stats

from .costs import CostModel

EULER_GAMMA = 0.5772156649015329


# --------------------------------------------------------------- deflated SR
def expected_max_sharpe(n_trials: int, sr_variance: float) -> float:
    """Expected maximum Sharpe under the null that no strategy has edge.

    This is the bar a reported Sharpe must clear. It rises with the number of
    strategies you tried, which is exactly why the trial counter exists.
    """
    if n_trials < 2:
        return 0.0
    a = stats.norm.ppf(1.0 - 1.0 / n_trials)
    b = stats.norm.ppf(1.0 - 1.0 / (n_trials * np.e))
    return float(np.sqrt(sr_variance) * ((1 - EULER_GAMMA) * a + EULER_GAMMA * b))


def deflated_sharpe(returns: np.ndarray, n_trials: int) -> dict:
    """Probabilistic Sharpe adjusted for multiple testing, skew and kurtosis.

    Returns a probability that the true Sharpe exceeds the selection-adjusted
    threshold. Below ~0.95, treat the result as indistinguishable from luck.
    """
    r = np.asarray(returns, dtype=float)
    r = r[np.isfinite(r)]
    n = len(r)
    if n < 30 or r.std(ddof=1) == 0:
        return {"deflated_sharpe": float("nan"), "sharpe": float("nan"),
                "sr_threshold": float("nan"), "n_obs": n,
                "verdict": "insufficient observations"}

    sr = r.mean() / r.std(ddof=1)
    skew = float(stats.skew(r))
    kurt = float(stats.kurtosis(r, fisher=False))

    sr_var = (1.0 / (n - 1)) * (1 + 0.5 * sr**2 - skew * sr + (kurt - 3) / 4 * sr**2)
    sr_var = max(sr_var, 1e-12)
    sr0 = expected_max_sharpe(n_trials, sr_var)

    denom = np.sqrt(1 - skew * sr + (kurt - 1) / 4 * sr**2)
    if not np.isfinite(denom) or denom <= 0:
        return {"deflated_sharpe": float("nan"), "sharpe": float(sr),
                "sr_threshold": float(sr0), "n_obs": n,
                "verdict": "undefined (degenerate moments)"}

    dsr = float(stats.norm.cdf((sr - sr0) * np.sqrt(n - 1) / denom))
    if dsr >= 0.95:
        verdict = "survives multiple-testing adjustment"
    elif dsr >= 0.80:
        verdict = "marginal -- more out-of-sample data needed"
    else:
        verdict = "INDISTINGUISHABLE FROM LUCK at this trial count"

    return {"deflated_sharpe": dsr, "sharpe": float(sr), "sr_threshold": float(sr0),
            "skew": skew, "kurtosis": kurt, "n_obs": n, "n_trials": n_trials,
            "verdict": verdict}


# ------------------------------------------------------------------ baselines
def _strategy_returns(signal: np.ndarray, gross_fwd: np.ndarray,
                      costs: CostModel, horizon: int) -> np.ndarray:
    """Net return per TRADE, simulated as sequential non-overlapping positions.

    CRITICAL: gross_fwd[t] spans bars t..t+horizon. With horizon > 1 those
    windows overlap, so treating every bar as an independent trade would
    multiply-count the same price moves -- and compound them into fantasy
    numbers. Real capital cannot be in two overlapping positions at once.

    So: when the signal fires we take the trade, then skip `horizon` bars
    before we are eligible again. This is both the honest simulation and the
    one that matches what the bot would actually do.
    """
    signal = np.asarray(signal, dtype=float)
    gross = np.asarray(gross_fwd, dtype=float)
    n = len(signal)
    trades, t = [], 0
    while t < n:
        if signal[t] > 0 and np.isfinite(gross[t]):
            trades.append(gross[t] - costs.round_trip)
            t += max(1, horizon)
        else:
            t += 1
    return np.asarray(trades, dtype=float)


def baseline_signals(n: int, gross_fwd: np.ndarray, rng: np.random.Generator,
                     signal_rate: float, horizon: int) -> dict[str, np.ndarray]:
    """The comparison set every model must beat.

    naive_persistence must look back a FULL horizon, not one bar. Lagging by
    one bar leaks: the return over t-1..t-1+h shares h-1 bars with the return
    over t..t+h, so a one-bar lag is close to knowing the answer.
    """
    gross = np.asarray(gross_fwd, dtype=float)
    persistence = np.zeros(n)
    if n > horizon:
        persistence[horizon:] = (gross[:-horizon] > 0).astype(float)
    return {
        "always_trade": np.ones(n),
        "never_trade": np.zeros(n),
        "random_same_rate": (rng.random(n) < signal_rate).astype(float),
        "naive_persistence": persistence,
    }


# -------------------------------------------------------------------- report
@dataclass
class ScoreReport:
    accuracy: float
    precision_on_trades: float
    n_predictions: int
    n_trades: int
    trade_rate: float
    base_rate: float
    accuracy_lift: float
    net_expectancy: float
    net_total_return: float
    buy_and_hold: float
    max_drawdown: float
    baselines: dict = field(default_factory=dict)
    dsr: dict = field(default_factory=dict)
    calibration: pd.DataFrame = None
    verdict: str = ""

    def to_text(self) -> str:
        L = []
        w = 74
        L.append("=" * w)
        L.append("PREDICTION SCORECARD")
        L.append("=" * w)
        L.append("")
        L.append(f"  Predictions scored (out-of-sample) : {self.n_predictions:,}")
        L.append(f"  Non-overlapping trades taken       : {self.n_trades:,} "
                 f"({self.trade_rate:.1%} of available slots)")
        L.append("")
        L.append("  ACCURACY")
        L.append(f"    Model accuracy                   : {self.accuracy:.2%}")
        L.append(f"    Base rate (always-trade)         : {self.base_rate:.2%}")
        L.append(f"    Lift over base rate              : {self.accuracy_lift:+.2%}  "
                 f"(information only -- selective models barely move accuracy)")
        L.append(f"    Precision when it says trade     : {self.precision_on_trades:.2%}")
        L.append("")
        L.append("  PROFITABILITY (net of costs)")
        L.append(f"    Expectancy per trade             : {self.net_expectancy:+.4%}")
        L.append(f"    Total net return                 : {self.net_total_return:+.2%}")
        L.append(f"    Buy and hold over same period    : {self.buy_and_hold:+.2%}")
        L.append(f"    Max drawdown                     : {self.max_drawdown:.2%}")
        L.append("")
        L.append("  BASELINE COMPARISON (total net return)")
        for name, val in self.baselines.items():
            flag = "  <-- model loses to this" if val >= self.net_total_return else ""
            L.append(f"    {name:<33}: {val:+.2%}{flag}")
        L.append("")
        if self.dsr:
            L.append("  MULTIPLE-TESTING ADJUSTMENT")
            L.append(f"    Sharpe (raw)                     : {self.dsr.get('sharpe', float('nan')):.3f}")
            L.append(f"    Threshold at {self.dsr.get('n_trials', 0)} trials"
                     f"{' ' * max(0, 20 - len(str(self.dsr.get('n_trials', 0))))}: "
                     f"{self.dsr.get('sr_threshold', float('nan')):.3f}")
            L.append(f"    Deflated Sharpe probability      : "
                     f"{self.dsr.get('deflated_sharpe', float('nan')):.3f}")
            L.append(f"    -> {self.dsr.get('verdict', '')}")
            L.append("")
        if self.calibration is not None and len(self.calibration):
            L.append("  CALIBRATION (are the probabilities honest?)")
            L.append("    confidence      n      predicted   actual")
            for _, row in self.calibration.iterrows():
                L.append(f"    {row['bucket']:<14}{int(row['n']):>6}   "
                         f"{row['predicted']:>9.2%}  {row['actual']:>8.2%}")
            L.append("")
        L.append("-" * w)
        L.append("  VERDICT: " + self.verdict)
        L.append("=" * w)
        return "\n".join(L)


def _max_drawdown(equity: np.ndarray) -> float:
    peak = np.maximum.accumulate(equity)
    dd = (equity - peak) / peak
    return float(dd.min()) if len(dd) else 0.0


def score_predictions(
    y_true: np.ndarray,
    y_prob: np.ndarray,
    gross_fwd: np.ndarray,
    costs: CostModel,
    threshold: float = 0.5,
    n_trials: int = 1,
    horizon: int = 1,
    seed: int = 7,
) -> ScoreReport:
    """Score a set of out-of-sample predictions against baselines."""
    y_true = np.asarray(y_true, dtype=float)
    y_prob = np.asarray(y_prob, dtype=float)
    gross = np.asarray(gross_fwd, dtype=float)

    ok = np.isfinite(y_true) & np.isfinite(y_prob) & np.isfinite(gross)
    y_true, y_prob, gross = y_true[ok], y_prob[ok], gross[ok]
    n = len(y_true)
    if n == 0:
        raise ValueError("No finite predictions to score.")

    signal = (y_prob >= threshold).astype(float)
    accuracy = float((signal == y_true).mean())
    n_signals = int(signal.sum())
    precision = float(y_true[signal > 0].mean()) if n_signals else float("nan")

    base_rate = float(max(y_true.mean(), 1 - y_true.mean()))
    lift = accuracy - base_rate

    model_r = _strategy_returns(signal, gross, costs, horizon)
    n_trades = len(model_r)
    signal_rate = n_signals / n          # per-bar firing rate: what the random baseline must match
    trade_rate = n_trades / max(1, n // max(1, horizon))  # share of non-overlapping slots used
    equity = np.cumprod(1 + model_r) if n_trades else np.array([1.0])
    net_total = float(equity[-1] - 1)
    expectancy = float(model_r.mean()) if n_trades else 0.0

    # Buy-and-hold: gross_fwd values OVERLAP (each spans `horizon` bars), so
    # compounding them all would multiply-count the same price moves. Step
    # through non-overlapping slices instead.
    step = max(1, horizon)
    bh = float(np.prod(1 + gross[::step]) - 1)

    rng = np.random.default_rng(seed)
    baselines = {}
    for name, sig in baseline_signals(n, gross, rng, signal_rate, horizon).items():
        r = _strategy_returns(sig, gross, costs, horizon)
        baselines[name] = float(np.prod(1 + r) - 1) if len(r) else 0.0
    baselines["buy_and_hold"] = bh

    dsr = deflated_sharpe(model_r, n_trials) if n_trades >= 30 else {
        "verdict": f"only {n_trades} non-overlapping trades -- too few to assess",
        "n_trials": n_trials}

    # calibration
    cal_rows = []
    edges = [0.0, 0.4, 0.5, 0.6, 0.7, 1.01]
    names = ["<0.40", "0.40-0.50", "0.50-0.60", "0.60-0.70", ">0.70"]
    for lo, hi, nm in zip(edges[:-1], edges[1:], names):
        m = (y_prob >= lo) & (y_prob < hi)
        if m.sum() >= 10:
            cal_rows.append({"bucket": nm, "n": int(m.sum()),
                             "predicted": float(y_prob[m].mean()),
                             "actual": float(y_true[m].mean())})
    calibration = pd.DataFrame(cal_rows)

    best_baseline = max(baselines.values())
    if n_trades < 30:
        verdict = ("TOO FEW TRADES TO CONCLUDE ANYTHING. Not a failure -- "
                   "just no information yet.")
    elif net_total <= best_baseline:
        verdict = ("NO EDGE. A baseline matches or beats the model net of costs. "
                   "Do not deploy.")
    elif not dsr.get("deflated_sharpe", 0) >= 0.95:  # NaN must not read as a pass
        verdict = ("PROMISING BUT UNPROVEN. Beats baselines, but does not survive "
                   "the multiple-testing adjustment. More out-of-sample data needed.")
    else:
        verdict = ("SURVIVES ALL CHECKS. Proceed to paper trading -- which is the "
                   "next test, not a formality.")

    return ScoreReport(
        accuracy=accuracy, precision_on_trades=precision, n_predictions=n,
        n_trades=n_trades, trade_rate=trade_rate, base_rate=base_rate,
        accuracy_lift=lift, net_expectancy=expectancy, net_total_return=net_total,
        buy_and_hold=bh, max_drawdown=_max_drawdown(equity), baselines=baselines,
        dsr=dsr, calibration=calibration, verdict=verdict,
    )


# ============================================================ position scoring
@dataclass
class PositionReport:
    name: str
    periods_per_year: int
    years: float
    ann_return: float
    ann_vol: float
    sharpe: float
    max_drawdown: float
    turnover_per_year: float
    cost_drag: float        # annualised, positive = paid
    financing_drag: float   # annualised, positive = paid, negative = earned
    avg_abs_position: float
    years_to_confirm: float  # live years for this Sharpe to reach one-sided 95%
    benchmarks: dict = field(default_factory=dict)
    primary: str = ""
    dsr: dict = field(default_factory=dict)
    verdict: str = ""

    def to_text(self) -> str:
        w = 74
        L = ["=" * w, f"SCORECARD  {self.name}", "=" * w, ""]
        L.append(f"  History scored                     : {self.years:.1f} years")
        L.append(f"  Annual return (net)                : {self.ann_return:+.2%}")
        L.append(f"  Annual volatility                  : {self.ann_vol:.2%}")
        L.append(f"  Sharpe (net)                       : {self.sharpe:.2f}")
        L.append(f"  Max drawdown                       : {self.max_drawdown:.2%}")
        L.append(f"  Average |position|                 : {self.avg_abs_position:.2f}")
        L.append(f"  Turnover                           : {self.turnover_per_year:.1f}x capital / year")
        L.append(f"  Trading cost drag                  : {self.cost_drag:.2%} / year")
        L.append(f"  Financing drag                     : {self.financing_drag:+.2%} / year"
                 f"{'  (earned)' if self.financing_drag < 0 else ''}")
        ytc = (f"{self.years_to_confirm:.1f} years" if np.isfinite(self.years_to_confirm)
               else "never (Sharpe <= 0)")
        L.append(f"  Live time to confirm this Sharpe   : {ytc}  (same at any bar frequency)")
        L.append("")
        L.append("  BENCHMARKS                           return    sharpe   max dd")
        for name, b in self.benchmarks.items():
            beaten = name in self.verdict
            tag = "  <-- matches or beats strategy" if beaten else ""
            primary = " *" if name == self.primary else ""
            L.append(f"    {name + primary:<33}{b['ann_return']:+8.2%}  {_fmt_sr(b['sharpe'])}  "
                     f"{b['max_drawdown']:7.2%}{tag}")
        L.append(f"    (* = excess returns measured against this, scaled to the strategy's vol)")
        L.append("")
        if self.dsr:
            L.append("  MULTIPLE-TESTING ADJUSTMENT (on excess returns)")
            sr = self.dsr.get("sharpe", float("nan")) * np.sqrt(self.periods_per_year)
            thr = self.dsr.get("sr_threshold", float("nan")) * np.sqrt(self.periods_per_year)
            L.append(f"    Excess Sharpe (annualised)       : {sr:.2f}")
            L.append(f"    Threshold at {self.dsr.get('n_trials', 0):<4} trials         : {thr:.2f}")
            L.append(f"    Deflated Sharpe probability      : "
                     f"{self.dsr.get('deflated_sharpe', float('nan')):.3f}")
            L.append(f"    -> {self.dsr.get('verdict', '')}")
            L.append("")
        L.append("-" * w)
        L.append("  VERDICT: " + self.verdict)
        L.append("=" * w)
        return "\n".join(L)


def _fmt_sr(sr: float) -> str:
    return f"{sr:7.2f}" if np.isfinite(sr) else f"{'-':>7}"


def _ann_stats(net: pd.Series, periods_per_year: int) -> dict:
    r = net.to_numpy(dtype=float)
    n = len(r)
    equity = np.cumprod(1 + r) if n else np.array([1.0])
    years = n / periods_per_year
    sd = r.std(ddof=1) if n > 1 else 0.0
    return {
        "ann_return": float(equity[-1] ** (1 / years) - 1) if years > 0 and equity[-1] > 0 else -1.0,
        "ann_vol": float(sd * np.sqrt(periods_per_year)),
        "sharpe": float(r.mean() / sd * np.sqrt(periods_per_year)) if sd > 0 else float("nan"),
        "max_drawdown": _max_drawdown(equity),
    }


def years_to_confirm(annual_sharpe: float, z: float = 1.645) -> float:
    """Live years before a strategy with this true Sharpe shows a significant track record.

    The t-statistic of a mean return is Sharpe_annual * sqrt(years), whether
    returns are sampled hourly or daily. Sampling more often shrinks the noise
    per bar and the signal per bar by the same factor, so hourly paper trading
    does NOT confirm an edge faster -- only a higher Sharpe does.
    """
    if not np.isfinite(annual_sharpe) or annual_sharpe <= 0:
        return float("inf")
    return float((z / annual_sharpe) ** 2)


def score_positions(strategy, benchmarks: dict, primary: str, n_trials: int,
                    periods_per_year: int, name: str,
                    min_years: float = 2.0) -> PositionReport:
    """Score a position-based strategy (a backtest.SimResult) against benchmarks.

    Verdict ladder, stop at the first failure:
      1. under `min_years` of history          -> no conclusion
      2. any benchmark matches it              -> no edge
         (flat: annual return <= 0; others: Sharpe <= benchmark Sharpe)
      3. deflated Sharpe of returns in excess of the primary benchmark < 0.95
                                               -> unproven
    Excess returns matter because a long-only rule on a rising asset has a
    "significant" Sharpe without any timing skill; the zero-cost negative
    control showed exactly that failure on pure noise. The benchmark is
    vol-matched first so the test compares risk-adjusted performance.
    """
    ppy = periods_per_year
    s = _ann_stats(strategy.net, ppy)
    years = len(strategy.net) / ppy
    bench = {k: _ann_stats(v.net, ppy) for k, v in benchmarks.items()}

    # Scale the benchmark to the strategy's volatility before differencing.
    # Otherwise a strategy that is simply less exposed than its benchmark
    # shows negative excess returns in a rising market, and the test measures
    # exposure instead of timing.
    ref = benchmarks[primary].net
    if ref.std() > 0:
        ref = ref * (strategy.net.std() / ref.std())
    excess = (strategy.net - ref).dropna()
    dsr = deflated_sharpe(excess.to_numpy(), n_trials)

    losers = [k for k, b in bench.items()
              if (k == "flat" and not s["ann_return"] > 0)
              or (k != "flat" and not s["sharpe"] > b["sharpe"])]
    if years < min_years:
        verdict = (f"TOO LITTLE HISTORY ({years:.1f} years). Not a failure -- "
                   "no information yet.")
    elif losers:
        verdict = f"NO EDGE. Matched or beaten by: {', '.join(losers)}. Do not deploy."
    elif not dsr.get("deflated_sharpe", 0) >= 0.95:
        verdict = ("PROMISING BUT UNPROVEN. Beats every benchmark, but the excess return "
                   "does not survive the multiple-testing adjustment. Pure noise reaches "
                   "this verdict about a quarter of the time -- it is not a reason to deploy.")
    else:
        verdict = ("SURVIVES ALL CHECKS. Proceed to paper trading -- which is the "
                   "next test, not a formality.")

    return PositionReport(
        name=name, periods_per_year=ppy, years=years,
        ann_return=s["ann_return"], ann_vol=s["ann_vol"], sharpe=s["sharpe"],
        max_drawdown=s["max_drawdown"],
        turnover_per_year=float(strategy.turnover.sum() / max(years, 1e-9)),
        cost_drag=float(strategy.cost.sum() / max(years, 1e-9)),
        financing_drag=float(strategy.financing.sum() / max(years, 1e-9)),
        avg_abs_position=float(strategy.position.abs().mean()),
        years_to_confirm=years_to_confirm(s["sharpe"]),
        benchmarks=bench, primary=primary, dsr=dsr, verdict=verdict,
    )
