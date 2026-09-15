"""Label construction.

THE MOST IMPORTANT FILE IN THE PACKAGE.

Almost every retail backtest labels bars as "price went up" vs "price went
down". That label is not tradeable. If price rises 0.05% and a round trip costs
you 0.20%, the prediction was CORRECT and the trade LOST MONEY.

So the label here is not "did price go up". It is:

    Would a long position opened at this bar, held for `horizon` bars, and
    closed at market, have finished ABOVE the round-trip cost?

A model trained on this label is answering the question you actually care
about. A model trained on raw direction is answering a question whose right
answer still loses money.
"""

from dataclasses import dataclass

import numpy as np
import pandas as pd

from .costs import CostModel


@dataclass
class LabelSpec:
    horizon: int = 6          # bars held
    costs: CostModel = None   # set in __post_init__ if omitted

    def __post_init__(self):
        if self.costs is None:
            self.costs = CostModel()


def make_labels(
    close: pd.Series, spec: LabelSpec
) -> tuple[pd.Series, pd.Series]:
    """Return (label, gross_forward_return).

    label = 1 when the forward return over `horizon` bars exceeds the
    round-trip cost; 0 otherwise. The final `horizon` bars get NaN because
    their outcome has not happened yet -- dropping them is mandatory, and
    forgetting to is a classic way to leak the end of your sample.
    """
    close = close.astype(float)
    fwd = close.shift(-spec.horizon) / close - 1.0
    fwd.iloc[-spec.horizon:] = np.nan

    threshold = spec.costs.round_trip
    label = (fwd > threshold).astype("float64")
    label[fwd.isna()] = np.nan

    return label, fwd


def label_balance(label: pd.Series) -> dict:
    """Base rate of the positive class. Your model must beat THIS, not 50%."""
    clean = label.dropna()
    if len(clean) == 0:
        return {"n": 0, "positive_rate": float("nan")}
    return {
        "n": int(len(clean)),
        "positive_rate": float(clean.mean()),
        "note": (
            "A model predicting 'always trade' scores exactly this accuracy. "
            "Any model scoring near it has learned nothing."
        ),
    }
