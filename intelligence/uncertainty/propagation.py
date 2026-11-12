"""Measurement uncertainty propagation (section 4.7).

    sigma_R^2 = J Sigma_e J^T

J is assembled from the per-row sensitivities produced by the balance engine;
error sources with the same key (systematic calibration errors, shared reading
time points) become one column, so the full residual covariance includes the
cross-row and cross-window correlations. Z = R / sigma_R and the whitened
chi-square statistic r^T Sigma^-1 r are provided for alarm logic.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy import stats

from intelligence.mass_balance.engine import Evidence, Row


@dataclass
class Assembled:
    rows: list[Row]
    r: np.ndarray
    J: np.ndarray
    sources: list[str]
    sigma_e: np.ndarray
    cov: np.ndarray

    @property
    def sigma_r(self) -> np.ndarray:
        return np.sqrt(np.clip(np.diag(self.cov), 1e-18, None))

    @property
    def z(self) -> np.ndarray:
        return self.r / self.sigma_r

    def chi2(self) -> tuple[float, int, float]:
        n = len(self.r)
        if n == 0:
            return 0.0, 0, 1.0
        L = np.linalg.cholesky(self.cov + 1e-12 * np.eye(n))
        wv = np.linalg.solve(L, self.r)
        c2 = float(wv @ wv)
        return c2, n, float(stats.chi2.sf(c2, n))

    def contributions(self, row_index: int) -> list[tuple[str, float]]:
        """Variance contribution of each error source to a row (for explanation)."""
        v = (self.J[row_index] * self.sigma_e) ** 2
        order = np.argsort(-v)
        tot = v.sum() or 1.0
        return [(self.sources[i], float(v[i] / tot)) for i in order[:6] if v[i] > 0]


def assemble(evidence: Evidence, rows: list[Row] | None = None, kinds: set[str] | None = None) -> Assembled:
    rows = rows if rows is not None else evidence.rows
    if kinds is not None:
        rows = [r for r in rows if r.kind in kinds]
    # rows depending on unavailable data (e.g. a dropped-out gauge) are excluded, never imputed
    rows = [r for r in rows if np.isfinite(r.value) and all(np.isfinite(v) for v in r.sens.values())]
    sources: dict[str, int] = {}
    for r in rows:
        for k in r.sens:
            if k not in sources:
                sources[k] = len(sources)
    J = np.zeros((len(rows), len(sources)))
    for i, r in enumerate(rows):
        for k, v in r.sens.items():
            J[i, sources[k]] += v
    names = list(sources)
    sig = np.array([evidence.sigma.get(k, 0.0) for k in names])
    sig = np.where(np.isfinite(sig), sig, 0.1)
    cov = (J * sig ** 2) @ J.T
    ks = getattr(evidence, "kind_scale", None)
    if ks:      # adaptive self-calibration: inflate / deflate whole row kinds (bounded upstream)
        s = np.array([ks.get(r.kind, 1.0) for r in rows])
        cov = cov * np.outer(s, s)
    cov = cov + np.diag(np.full(len(rows), 1e-10))
    return Assembled(rows=rows, r=np.array([r.value for r in rows]), J=J, sources=names, sigma_e=sig, cov=cov)


def window_statistics(evidence: Evidence) -> list[dict]:
    """Per-window balance statistics for monitoring / features."""
    out = []
    for w in evidence.windows:
        a = assemble(evidence, rows=w.rows)
        c2, n, p = a.chi2()
        zs = {r.row_id.split("|", 1)[1]: float(z) for r, z in zip(a.rows, a.z)}
        cv_rows = [r for r in w.rows if r.kind == "CV"]
        fac = sum(r.value for r in cv_rows)
        out.append({"window": w.index, "t0": w.t0, "t1": w.t1, "chi2": c2, "dof": n, "p_value": p,
                    "max_abs_z": float(np.max(np.abs(a.z))) if n else 0.0, "z": zs,
                    "facility_residual_t": fac,
                    "residuals": {r.row_id.split("|", 1)[1]: r.value for r in w.rows},
                    "sigma": {r.row_id.split("|", 1)[1]: float(s) for r, s in zip(a.rows, a.sigma_r)}})
    return out
