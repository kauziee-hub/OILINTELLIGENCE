"""Model 1 - anomaly detection and its baselines.

* ConventionalThreshold : facility mass-balance discrepancy above a fixed
                          percentage of throughput (industry practice baseline)
* StatisticalDetector   : uncertainty-aware chi-square / max|z| test
* IsolationForestDetector: unsupervised, trained on normal windows only
* XGBoostDetector       : supervised, trained on labelled simulated windows
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd
from sklearn.ensemble import IsolationForest
from xgboost import XGBClassifier

PHYS_PREFIX = ("phys_", "ctx_", "qual_")
RAW_PREFIX = ("raw_", "ctx_", "qual_")


def feature_columns(df: pd.DataFrame, kind: str = "all") -> list[str]:
    if kind == "raw":
        pref = RAW_PREFIX
    elif kind == "phys":
        pref = PHYS_PREFIX
    else:
        pref = PHYS_PREFIX + ("raw_",)
    return [c for c in df.columns if c.startswith(pref)]


@dataclass
class ConventionalThreshold:
    rel_threshold: float = 0.005      # 0.5 % of throughput
    abs_threshold_t: float = 2.0      # when idle / low throughput
    name: str = "conventional_threshold"

    def score(self, df: pd.DataFrame) -> np.ndarray:
        fac = df["phys_facility_residual_t"].abs().to_numpy()
        thr = np.maximum(self.rel_threshold * df["ctx_throughput_t"].to_numpy(), self.abs_threshold_t)
        return fac / thr

    threshold: float = 1.0


@dataclass
class StatisticalDetector:
    """Alarm when the whitened residual vector is improbable under the calibrated uncertainty model."""
    name: str = "statistical_chi2"
    threshold: float = 3.0            # -log10 p

    def score(self, df: pd.DataFrame) -> np.ndarray:
        return np.maximum(df["phys_log_p"].to_numpy(), (df["phys_max_abs_z_cv"].to_numpy() / 4.0) * 3.0)


@dataclass
class IsolationForestDetector:
    name: str = "isolation_forest"
    kind: str = "all"
    contamination: float = 0.02
    model: Any = None
    columns: list[str] = field(default_factory=list)
    threshold: float = 0.0

    def fit(self, normal: pd.DataFrame, seed: int = 0) -> "IsolationForestDetector":
        self.columns = feature_columns(normal, self.kind)
        self.model = IsolationForest(n_estimators=300, contamination=self.contamination, random_state=seed)
        self.model.fit(normal[self.columns].to_numpy())
        s = self.score(normal)
        self.threshold = float(np.quantile(s, 1 - self.contamination))
        return self

    def score(self, df: pd.DataFrame) -> np.ndarray:
        return -self.model.score_samples(df[self.columns].to_numpy())


@dataclass
class XGBoostDetector:
    name: str = "xgboost"
    kind: str = "all"
    model: Any = None
    columns: list[str] = field(default_factory=list)
    threshold: float = 0.5

    def fit(self, df: pd.DataFrame, y: np.ndarray, seed: int = 0) -> "XGBoostDetector":
        self.columns = feature_columns(df, self.kind)
        pos = max(int(y.sum()), 1)
        neg = max(int(len(y) - y.sum()), 1)
        self.model = XGBClassifier(n_estimators=300, max_depth=4, learning_rate=0.08, subsample=0.9,
                                   colsample_bytree=0.8, scale_pos_weight=neg / pos, random_state=seed,
                                   eval_metric="logloss", n_jobs=4)
        self.model.fit(df[self.columns].to_numpy(), y)
        return self

    def score(self, df: pd.DataFrame) -> np.ndarray:
        return self.model.predict_proba(df[self.columns].to_numpy())[:, 1]

    def feature_importance(self, top: int = 15) -> list[tuple[str, float]]:
        imp = self.model.feature_importances_
        order = np.argsort(-imp)[:top]
        return [(self.columns[i], float(imp[i])) for i in order]


def tune_threshold_for_false_alarms(scores_normal: np.ndarray, max_fa_per_100: float = 5.0) -> float:
    """Threshold giving <= max false alarms per 100 normal windows (validation data)."""
    q = 1.0 - max_fa_per_100 / 100.0
    return float(np.quantile(scores_normal, q)) if len(scores_normal) else math.inf
