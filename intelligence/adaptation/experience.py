"""Layer 8 - engineering experience memory.

Engineer-confirmed investigations (diagnostic cases) are the plant's own labelled history. They are
used in two ways that need no retraining:

1. Learned priors. The prior over fault families is a Dirichlet posterior:

       p_f = (A0 * p0_f + n_f) / (A0 + N)

   p0 = engineering default prior, n_f = confirmed cases of family f, A0 = prior strength (pseudo-
   counts). The ratio p_f / p0_f is bounded so a handful of cases nudges - never dominates - the
   physics evidence. The prior enters Bayes' rule directly (posterior ~ likelihood x prior).

2. Similar cases. Each case keeps the hypothesis distribution the model produced; a new
   investigation retrieves the most similar verified cases (cosine similarity of the distributions,
   adjusted for residual size) and shows what they turned out to be.
"""
from __future__ import annotations

import math
from collections import Counter
from typing import Any

from intelligence.fault_attribution.hypotheses import DEFAULT_FAMILY_PRIOR

PRIOR_STRENGTH = 40.0          # A0: pseudo-cases behind the engineering prior
MAX_SHIFT = 3.0                # learned prior within [p0 / MAX_SHIFT, p0 * MAX_SHIFT]


def _family(cause: str | None) -> str | None:
    if not cause:
        return None
    fam = cause.split(":", 1)[0]
    return fam if fam in DEFAULT_FAMILY_PRIOR else None


def _dist(case: dict[str, Any]) -> dict[str, float]:
    out: dict[str, float] = {}
    for h in case.get("candidate_faults") or []:
        if isinstance(h, dict) and h.get("hypothesis_id"):
            out[h["hypothesis_id"]] = float(h.get("calibrated_probability") or h.get("probability") or 0.0)
    return out


def _cosine(a: dict[str, float], b: dict[str, float]) -> float:
    keys = set(a) | set(b)
    num = sum(a.get(k, 0.0) * b.get(k, 0.0) for k in keys)
    da = math.sqrt(sum(v * v for v in a.values()))
    db = math.sqrt(sum(v * v for v in b.values()))
    return num / (da * db) if da > 0 and db > 0 else 0.0


class ExperienceMemory:
    def __init__(self, base_prior: dict[str, float] | None = None, strength: float = PRIOR_STRENGTH):
        self.base = dict(base_prior or DEFAULT_FAMILY_PRIOR)
        z = sum(self.base.values())
        self.base = {k: v / z for k, v in self.base.items()}
        self.strength = strength
        self.cases: list[dict[str, Any]] = []
        self.counts: Counter[str] = Counter()

    def update(self, cases: list[dict[str, Any]]) -> None:
        self.cases = [c for c in cases if c.get("verified_cause")]
        self.counts = Counter(f for f in (_family(c.get("verified_cause")) for c in self.cases) if f)

    @property
    def n_cases(self) -> int:
        return sum(self.counts.values())

    def priors(self) -> dict[str, float]:
        N = self.n_cases
        p = {f: (self.strength * p0 + self.counts.get(f, 0)) / (self.strength + N) for f, p0 in self.base.items()}
        # clip to [p0 / MAX_SHIFT, p0 * MAX_SHIFT] and renormalise until both hold (renormalising alone
        # would push a clipped family back above its bound)
        for _ in range(50):
            p = {f: min(max(v, self.base[f] / MAX_SHIFT), self.base[f] * MAX_SHIFT) for f, v in p.items()}
            z = sum(p.values())
            p = {f: v / z for f, v in p.items()}
            if all(self.base[f] / MAX_SHIFT - 1e-9 <= v <= self.base[f] * MAX_SHIFT + 1e-9 for f, v in p.items()):
                break
        return p

    def summary(self) -> dict[str, Any]:
        pr = self.priors()
        return {"verified_cases": self.n_cases, "prior_strength": self.strength,
                "by_family": dict(self.counts),
                "families": [{"family": f, "default_prior": round(self.base[f], 4), "learned_prior": round(pr[f], 4),
                              "shift": round(pr[f] / self.base[f], 3), "confirmed_cases": self.counts.get(f, 0)}
                             for f in sorted(self.base, key=lambda f: -pr[f])]}

    def similar(self, hypothesis_distribution: dict[str, float], residual_t: float | None = None, k: int = 3,
                exclude: str | None = None) -> list[dict[str, Any]]:
        out = []
        for c in self.cases:
            if exclude and c.get("case_id") == exclude:
                continue
            d = _dist(c)
            if not d:
                continue
            sim = _cosine(hypothesis_distribution, d)
            r0, r1 = residual_t, c.get("residual")
            if r0 is not None and r1 is not None and max(abs(r0), abs(r1)) > 0:
                sim *= 0.75 + 0.25 * min(abs(r0), abs(r1)) / max(abs(r0), abs(r1))
            if sim > 0.2:
                out.append({"case_id": c.get("case_id"), "similarity": round(sim, 3),
                            "verified_cause": c.get("verified_cause"),
                            "verification_method": c.get("verification_method", ""),
                            "residual_t": r1})
        out.sort(key=lambda x: -x["similarity"])
        return out[:k]
