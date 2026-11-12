"""Model 2 - instrument fault classification (per instrument, per window).

Classes: NORMAL, BIAS_POS, BIAS_NEG, DRIFT, STUCK, DROPOUT
Features combine signal statistics (flat runs, gaps, noise ratio, steps) with
the uncertainty-normalised residuals of the balances the instrument enters.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd
from xgboost import XGBClassifier

CLASSES = ["NORMAL", "BIAS_POS", "BIAS_NEG", "DRIFT", "STUCK", "DROPOUT"]

FAULT_TO_CLASS = {
    "METER_BIAS": "BIAS", "LEVEL_BIAS": "BIAS", "TEMPERATURE_BIAS": "BIAS", "DENSITY_BIAS": "BIAS",
    "METER_DRIFT": "DRIFT", "LEVEL_DRIFT": "DRIFT", "SENSOR_STUCK": "STUCK", "SENSOR_DROPOUT": "DROPOUT",
}


def sensor_feature_columns(df: pd.DataFrame) -> list[str]:
    return [c for c in df.columns if c.startswith(("type_", "q_", "z_", "sig_", "active"))]


@dataclass
class SensorFaultClassifier:
    model: Any = None
    columns: list[str] = field(default_factory=list)
    classes: list[str] = field(default_factory=lambda: list(CLASSES))
    # P(fault) = 1 - P(NORMAL) above which a window is called faulty; chosen on validation NORMAL windows for
    # a false-flag budget (calibrate_threshold). None = most-probable class, which with ~1 faulty window in
    # 100 almost always says NORMAL.
    fault_threshold: float | None = None

    def fit(self, df: pd.DataFrame, y: list[str], seed: int = 0) -> "SensorFaultClassifier":
        self.columns = sensor_feature_columns(df)
        yi = np.array([self.classes.index(v) for v in y])
        counts = np.bincount(yi, minlength=len(self.classes)).astype(float)
        w = (len(yi) / (len(self.classes) * np.clip(counts, 1, None)))[yi]
        w = np.sqrt(w)  # soften class re-weighting
        self.model = XGBClassifier(n_estimators=350, max_depth=5, learning_rate=0.08, subsample=0.9,
                                   colsample_bytree=0.8, objective="multi:softprob",
                                   num_class=len(self.classes), random_state=seed, n_jobs=4,
                                   eval_metric="mlogloss")
        self.model.fit(df[self.columns].to_numpy(), yi, sample_weight=w)
        return self

    def predict_proba(self, df: pd.DataFrame) -> np.ndarray:
        return self.model.predict_proba(df[self.columns].to_numpy())

    def calibrate_threshold(self, normal_val: pd.DataFrame, fp_budget: float = 0.01) -> float:
        """Set the fault threshold so that `fp_budget` of NORMAL validation windows would be flagged."""
        pf = 1.0 - self.predict_proba(normal_val)[:, 0]
        self.fault_threshold = float(np.quantile(pf, 1.0 - fp_budget))
        return self.fault_threshold

    def decide(self, P: np.ndarray) -> list[str]:
        """Faulty or not by the calibrated threshold, then the most probable fault class."""
        thr = getattr(self, "fault_threshold", None)     # models saved before the threshold existed
        if thr is None:
            return [self.classes[i] for i in P.argmax(1)]
        faulty = (1.0 - P[:, 0]) >= thr
        kind = 1 + P[:, 1:].argmax(1)
        return [self.classes[k] if f else self.classes[0] for f, k in zip(faulty, kind)]

    def predict(self, df: pd.DataFrame) -> list[str]:
        return self.decide(self.predict_proba(df))
