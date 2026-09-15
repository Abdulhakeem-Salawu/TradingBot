"""Persistent trial counter.

Every distinct thing you evaluate -- a parameter set, a feature set, a model
type, a threshold -- is one trial. The count feeds the deflated Sharpe
adjustment, and if you do not track it honestly the adjustment is meaningless.

This file exists because the count is trivially easy to lose track of. You will
try forty variants over three weekends and remember it as "a few". The counter
does not forget, which is the entire point.

Command-line arguments are not the only degrees of freedom: editing a feature
window, a strategy default or the label definition is a new trial too. Record
code_hash() in every config so those edits register instead of hashing to a
config that was already counted. Synthetic control runs belong in a separate
file so they never inflate the count applied to real data.
"""

import json
import hashlib
from datetime import datetime, timezone
from pathlib import Path


SYNTHETIC_TRIALS_FILE = "trials-synthetic.json"


def code_hash() -> str:
    """Fingerprint of every module in the harness package."""
    h = hashlib.sha256()
    for f in sorted(Path(__file__).parent.glob("*.py")):
        h.update(f.name.encode())
        h.update(f.read_bytes())
    return h.hexdigest()[:12]


class TrialCounter:
    def __init__(self, path: str = "trials.json"):
        self.path = Path(path)
        if self.path.exists():
            self._data = json.loads(self.path.read_text())
        else:
            self._data = {"trials": [], "count": 0}

    def record(self, config: dict, note: str = "") -> int:
        """Record one evaluated configuration. Returns cumulative trial count.

        Identical configs are de-duplicated by hash, so re-running the same
        thing does not inflate the count.
        """
        blob = json.dumps(config, sort_keys=True, default=str)
        h = hashlib.sha256(blob.encode()).hexdigest()[:16]
        if any(t["hash"] == h for t in self._data["trials"]):
            return self._data["count"]

        self._data["trials"].append({
            "hash": h,
            "config": config,
            "note": note,
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        })
        self._data["count"] = len(self._data["trials"])
        self.path.write_text(json.dumps(self._data, indent=2, default=str))
        return self._data["count"]

    @property
    def count(self) -> int:
        return max(1, self._data["count"])

    def summary(self) -> str:
        n = self._data["count"]
        if n == 0:
            return "No trials recorded yet."
        first = self._data["trials"][0]["ts"]
        return (f"{n} distinct configurations evaluated since {first}. "
                f"Deflated Sharpe will be computed against n_trials={self.count}.")
