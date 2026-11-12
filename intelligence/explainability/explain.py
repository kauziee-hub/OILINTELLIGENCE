"""Evidence-level explanation of attribution results.

For a hypothesis H with fitted signature S_h theta_hat, list the evidence rows
that carry most of the support: observed residual, the residual predicted by H,
its uncertainty and the z-score. Every number traces to a balance row.
"""
from __future__ import annotations

from typing import Any

import numpy as np

from intelligence.fault_attribution.bayesian import AttributionResult


def explain_hypothesis(result: AttributionResult, hid: str, top: int = 6) -> list[dict[str, Any]]:
    s = next((x for x in result.scores if x.hypothesis.hid == hid), None)
    if s is None:
        return []
    fitted = s.fitted if s.fitted is not None else np.zeros(len(result.residuals))
    weight = np.abs(fitted) / np.clip(result.sigma, 1e-9, None) + np.abs(result.residuals) / np.clip(
        result.sigma, 1e-9, None) * (np.abs(fitted) > 1e-9)
    order = np.argsort(-weight)
    out = []
    for i in order[:top]:
        if weight[i] <= 0:
            continue
        out.append({"row": result.rows[i], "observed": round(float(result.residuals[i]), 4),
                    "predicted_by_hypothesis": round(float(fitted[i]), 4), "sigma": round(float(result.sigma[i]), 4),
                    "z": round(float(result.residuals[i] / max(result.sigma[i], 1e-9)), 2)})
    return out


def most_abnormal_rows(result: AttributionResult, top: int = 8) -> list[dict[str, Any]]:
    z = result.residuals / np.clip(result.sigma, 1e-9, None)
    order = np.argsort(-np.abs(z))
    return [{"row": result.rows[i], "residual": round(float(result.residuals[i]), 4),
             "sigma": round(float(result.sigma[i]), 4), "z": round(float(z[i]), 2)} for i in order[:top]]
