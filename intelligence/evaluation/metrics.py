"""Evaluation metrics (section 7.6)."""
from __future__ import annotations

import math
from typing import Any, Sequence

import numpy as np
from sklearn import metrics as skm


def binary_metrics(y_true: Sequence[int], score: Sequence[float], threshold: float) -> dict[str, Any]:
    y = np.asarray(y_true).astype(int)
    s = np.asarray(score, dtype=float)
    pred = (s >= threshold).astype(int)
    tp = int(((pred == 1) & (y == 1)).sum())
    fp = int(((pred == 1) & (y == 0)).sum())
    fn = int(((pred == 0) & (y == 1)).sum())
    tn = int(((pred == 0) & (y == 0)).sum())
    prec = tp / (tp + fp) if tp + fp else 0.0
    rec = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * prec * rec / (prec + rec) if prec + rec else 0.0
    out = {"precision": prec, "recall": rec, "f1": f1, "tp": tp, "fp": fp, "fn": fn, "tn": tn,
           "false_alarms_per_100_normal": 100.0 * fp / max(fp + tn, 1), "threshold": threshold}
    if len(np.unique(y)) > 1:
        out["pr_auc"] = float(skm.average_precision_score(y, s))
        out["roc_auc"] = float(skm.roc_auc_score(y, s))
    return out


def multiclass_metrics(y_true: Sequence[str], y_pred: Sequence[str], labels: list[str]) -> dict[str, Any]:
    cm = skm.confusion_matrix(y_true, y_pred, labels=labels)
    rep = skm.classification_report(y_true, y_pred, labels=labels, output_dict=True, zero_division=0)
    return {
        "macro_f1": float(skm.f1_score(y_true, y_pred, labels=labels, average="macro", zero_division=0)),
        "accuracy": float(skm.accuracy_score(y_true, y_pred)),
        "per_class_recall": {k: float(rep[k]["recall"]) for k in labels if k in rep},
        "per_class_f1": {k: float(rep[k]["f1-score"]) for k in labels if k in rep},
        "confusion_matrix": {"labels": labels, "matrix": cm.tolist()},
    }


def topk_accuracy(truths: list[list[str]], rankings: list[list[str]], k: int) -> float:
    """Fraction of cases where (any) true cause appears in the top-k ranking."""
    hits = [bool(set(t) & set(r[:k])) for t, r in zip(truths, rankings) if t]
    return float(np.mean(hits)) if hits else float("nan")


def log_loss_brier(prob_rows: list[dict[str, float]], truths: list[str], classes: list[str]) -> dict[str, float]:
    P = np.array([[max(p.get(c, 0.0), 0.0) for c in classes] for p in prob_rows])
    P = P / np.clip(P.sum(1, keepdims=True), 1e-12, None)
    Y = np.array([[1.0 if c == t else 0.0 for c in classes] for t in truths])
    eps = 1e-12
    ll = float(-np.mean(np.log(np.clip((P * Y).sum(1), eps, 1.0))))
    brier = float(np.mean(((P - Y) ** 2).sum(1)))
    conf = P.max(1)
    correct = (P.argmax(1) == Y.argmax(1)).astype(float)
    bins = np.linspace(0, 1, 11)
    ece = 0.0
    for a, b in zip(bins[:-1], bins[1:]):
        m = (conf > a) & (conf <= b)
        if m.any():
            ece += m.mean() * abs(conf[m].mean() - correct[m].mean())
    return {"log_loss": ll, "brier": brier, "ece": float(ece), "n": len(truths)}


def regression_metrics(true: Sequence[float], est: Sequence[float], lo: Sequence[float] | None = None,
                       hi: Sequence[float] | None = None) -> dict[str, float]:
    t = np.asarray(true, dtype=float)
    e = np.asarray(est, dtype=float)
    out = {"mae": float(np.mean(np.abs(e - t))), "rmse": float(math.sqrt(np.mean((e - t) ** 2))),
           "bias": float(np.mean(e - t)), "n": int(len(t)),
           "mape_pct": float(np.mean(np.abs(e - t) / np.clip(np.abs(t), 1e-6, None)) * 100) if len(t) else float("nan")}
    if lo is not None and hi is not None:
        lo_, hi_ = np.asarray(lo, dtype=float), np.asarray(hi, dtype=float)
        out["interval_coverage"] = float(np.mean((t >= lo_) & (t <= hi_)))
    return out
