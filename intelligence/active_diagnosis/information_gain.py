"""Active diagnosis: next-best verification test (Model 4).

    IG(a) = H(H | E) - E_{y_a}[ H(H | E, y_a) ]

Actions are ranked by expected information gain, optionally divided by a cost
term that accounts for cost, duration and safety exposure, and filtered by
operational availability (e.g. isolation-requiring tests while a transfer is
running).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

from industrial.schemas.plant import PlantConfig
from intelligence.active_diagnosis.tests import VerificationAction, build_actions
from intelligence.fault_attribution.hypotheses import Hypothesis


def entropy(p: np.ndarray) -> float:
    p = p[p > 0]
    return float(-(p * np.log2(p)).sum())


@dataclass
class RankedAction:
    action: VerificationAction
    expected_information_gain: float
    p_positive: float
    score: float
    available: bool
    reason: str

    def to_dict(self) -> dict[str, Any]:
        d = self.action.to_dict()
        d.update({"expected_information_gain_bits": round(self.expected_information_gain, 4),
                  "p_positive": round(self.p_positive, 4), "score": round(self.score, 4),
                  "available": self.available, "rationale": self.reason})
        return d


class ActiveDiagnosis:
    def __init__(self, plant: PlantConfig, cost_weight: float = 0.5, safety_weight: float = 1.0):
        self.plant = plant
        self.actions = build_actions(plant)
        self.cost_weight = cost_weight
        self.safety_weight = safety_weight

    def _lik(self, hyps: list[Hypothesis]) -> np.ndarray:
        """L[a, h, y] = P(y | h) for every action."""
        return np.array([[a.likelihood(self.plant, h) for h in hyps] for a in self.actions])

    def rank(self, hyps: list[Hypothesis], p: np.ndarray, transfer_active: bool = False,
             performed: set[str] | None = None, top: int = 8) -> list[RankedAction]:
        p = np.asarray(p, dtype=float)
        p = p / p.sum()
        H0 = entropy(p)
        L = self._lik(hyps)
        performed = performed or set()
        out = []
        for ai, a in enumerate(self.actions):
            py = p @ L[ai]                         # predictive outcome probabilities
            post_h = 0.0
            for y in range(2):
                if py[y] <= 1e-12:
                    continue
                post = p * L[ai, :, y]
                post /= post.sum()
                post_h += py[y] * entropy(post)
            ig = max(H0 - post_h, 0.0)
            sp = a.spec
            cost_term = (1.0 + sp.cost) ** self.cost_weight * (1.0 + self.safety_weight * sp.safety_risk)
            available, reason = True, ""
            if a.action_id in performed:
                available, reason = False, "already performed in this investigation"
            elif transfer_active and sp.requires_isolation:
                available, reason = False, "requires isolation; not available while a transfer is running"
            top_h = [hyps[i].hid for i in np.argsort(-p)[:3]]
            discriminates = [h.hid for h in hyps if a.relevant(self.plant, h)]
            if not reason:
                rel = [h for h in discriminates if h in top_h]
                reason = (f"Discriminates {', '.join(rel)} from competing explanations" if rel else
                          "Reduces uncertainty across remaining hypotheses")
            out.append(RankedAction(a, ig, float(py[0]), ig / cost_term if available else 0.0, available, reason))
        out.sort(key=lambda r: (-r.score, -r.expected_information_gain))
        return out[:top]

    def update(self, hyps: list[Hypothesis], p: np.ndarray, action_id: str, outcome: str) -> np.ndarray:
        a = next(x for x in self.actions if x.action_id == action_id)
        y = 0 if outcome == "POSITIVE" else 1
        lik = np.array([a.likelihood(self.plant, h)[y] for h in hyps])
        post = np.asarray(p, dtype=float) * lik
        return post / post.sum()


def hypotheses_from_attribution(result, min_prob: float = 1e-4) -> tuple[list[Hypothesis], np.ndarray]:
    dist = result.hypothesis_distribution()
    by_id = {s.hypothesis.hid: s.hypothesis for s in result.scores}
    hyps, ps = [], []
    for hid, pr in dist.items():
        if pr >= min_prob and hid in by_id:
            hyps.append(by_id[hid])
            ps.append(pr)
    ps = np.array(ps)
    return hyps, ps / ps.sum()
