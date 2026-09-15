"""Purged, embargoed walk-forward splitting.

WHY THIS EXISTS
---------------
If your label at time t is built from prices between t and t+h, then a training
sample at t-1 shares outcome information with a test sample at t. Ordinary
cross-validation -- and ordinary train/test splits -- leak that information
backwards, and the model scores brilliantly on data it has effectively already
seen.

This is the single most common reason a backtest shows 95%+ accuracy. The fix
is mechanical: drop (purge) every training sample whose label window overlaps
the test window, then drop a further buffer (embargo) after the test window to
account for serial correlation.
"""

from dataclasses import dataclass
from typing import Iterator

import numpy as np


@dataclass
class PurgedWalkForward:
    """Expanding-window walk-forward with purging and embargo.

    Parameters
    ----------
    n_splits:
        Number of sequential test folds.
    label_horizon:
        How many bars forward the label looks. Determines purge width.
    embargo_frac:
        Extra buffer after each test fold, as a fraction of total samples.
    min_train:
        Minimum training samples before the first fold is emitted.
    """

    n_splits: int = 5
    label_horizon: int = 1
    embargo_frac: float = 0.01
    min_train: int = 500

    def split(self, n_samples: int) -> Iterator[tuple[np.ndarray, np.ndarray]]:
        if n_samples <= self.min_train + self.n_splits:
            raise ValueError(
                f"Not enough samples ({n_samples}) for {self.n_splits} folds "
                f"with min_train={self.min_train}. Get more history or reduce folds."
            )

        embargo = int(n_samples * self.embargo_frac)
        usable = n_samples - self.min_train
        fold_size = usable // self.n_splits

        if fold_size <= self.label_horizon + embargo:
            raise ValueError(
                "Fold size is smaller than the purge+embargo width. "
                "Reduce n_splits or label_horizon, or get more data."
            )

        for i in range(self.n_splits):
            test_start = self.min_train + i * fold_size
            test_end = test_start + fold_size if i < self.n_splits - 1 else n_samples

            # Training set is everything before the test fold, minus the purge.
            # Purge width = label_horizon, because a sample at index j has a
            # label built from j..j+label_horizon.
            train_end = test_start - self.label_horizon - embargo
            if train_end <= 0:
                continue

            train_idx = np.arange(0, train_end)
            test_idx = np.arange(test_start, test_end)

            # Trim the tail of the test fold so its own labels stay in-fold.
            test_idx = test_idx[: max(0, len(test_idx) - self.label_horizon)]
            if len(test_idx) == 0:
                continue

            yield train_idx, test_idx

    def describe(self, n_samples: int) -> str:
        folds = list(self.split(n_samples))
        lines = [
            f"Purged walk-forward: {len(folds)} folds, "
            f"horizon={self.label_horizon}, embargo={self.embargo_frac:.1%}"
        ]
        for i, (tr, te) in enumerate(folds, 1):
            gap = te[0] - tr[-1] - 1
            lines.append(
                f"  fold {i}: train[0:{tr[-1] + 1}] ({len(tr)}) "
                f"-> gap {gap} -> test[{te[0]}:{te[-1] + 1}] ({len(te)})"
            )
        return "\n".join(lines)


def leakage_selftest() -> bool:
    """Confirm no train index ever comes within label_horizon of a test index."""
    cv = PurgedWalkForward(n_splits=4, label_horizon=12, embargo_frac=0.02, min_train=300)
    for train_idx, test_idx in cv.split(2000):
        if len(train_idx) == 0:
            continue
        gap = test_idx[0] - train_idx[-1] - 1
        if gap < cv.label_horizon:
            return False
    return True
