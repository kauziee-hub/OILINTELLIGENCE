"""Physics-guided, topology-aware Bayesian fault attribution (Model 3).

Generative model for the stacked evidence vector r (all rows, all windows):

    r = S_h theta + e ,   e ~ N(0, Sigma_r = J Sigma_e J^T) ,  theta ~ N(0, C_h)

* S_h (signature) is derived from the measurement topology of each window:
  which control volumes contain the leaking segment, which CV rows carry a
  meter with a calibration shift, which tank inventories a level/temperature
  error enters, the sign of a passing valve's flow from measured pressures, ...
  Physical rates scale with sqrt(pressure) (orifice law) or sqrt(level).
* Marginal likelihood  p(r|H) = N(r; 0, Sigma_r + S C S^T)  is closed form
  (Woodbury); unknown onset time and leak location are mixtures; one-sided
  (non-negative) magnitudes use the half-normal correction 2*Phi(mu/s).
* P(H|E) = p(E|H) P(H) / sum ...  with temperature calibration and a broad
  UNKNOWN alternative so that unexplained evidence is not forced onto a known
  cause. Two-fault combinations of the leading hypotheses are also scored.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np
from scipy import linalg
from scipy.special import logsumexp, log_ndtr

from industrial.schemas.plant import PlantConfig
from intelligence.fault_attribution.hypotheses import CATEGORY, Hypothesis, build_hypotheses
from intelligence.mass_balance.engine import Evidence
from intelligence.mass_balance.topology import TopologyBuilder
from intelligence.uncertainty.propagation import Assembled, assemble

G = 9.80665

# Prior scale of fault magnitudes (natural units)
TAU = {
    "METER_ERROR": (0.015, 0.006),        # (offset, per-window step) relative
    "LEVEL_ERROR": (0.05, 0.03),          # m
    "TEMP_ERROR": (2.0, 1.0),             # degC
    "DENSITY_ERROR": (4.0, 1.0),          # kg/m3
    "STUCK_FLOW": (0.02, 0.25),
    "STUCK_LEVEL": (0.05, 0.3),
    "STUCK_TEMP": (2.0, 3.0),
    "PIPELINE_LEAK": 3.0,                 # t/h at 5 bar
    "TANK_LEAK": 2.0,                     # t/h at 10 m level
    "VALVE_PASSING": 2.0,                 # t/h at 5 bar differential
    "PUMP_DEGRADATION": 0.15,             # head loss fraction
    "MISSING_TRANSFER": 0.7,              # unbooked fraction of metered mass per window
}
# Structured sensor-error prior: (pre-existing offset, step at onset, drift rate per hour)
TAU3 = {
    "METER_ERROR": (0.004, 0.02, 0.006),
    "LEVEL_ERROR": (0.05, 0.04, 0.008),
    "TEMP_ERROR": (0.6, 2.5, 0.05),
    "DENSITY_ERROR": (1.0, 5.0, 0.2),
}
QUALITY_LR_POS = 1e5   # a flat-lined analog signal / coverage gap is direct observation (configurable per historian)
QUALITY_LR_NEG = 1e-4


@dataclass
class Component:
    S: np.ndarray
    C: np.ndarray
    logw: float
    one_sided: bool = False
    meta: dict[str, Any] = field(default_factory=dict)
    upper: float = math.inf          # bound on a one-sided scalar magnitude (physical constraint)


def _log_ndtr_diff(a: float, b: float) -> float:
    """log(Phi(a) - Phi(b)) for a > b, evaluated in the more accurate tail."""
    if a <= b:
        return -745.0
    if b > 0:
        a, b = -b, -a
    la, lb = float(log_ndtr(a)), float(log_ndtr(b))
    return la + math.log1p(-math.exp(lb - la)) if lb < la else -745.0


def bounded_magnitude_correction(mu: float, var: float, prior_var: float, upper: float = math.inf) -> float:
    """Marginal-likelihood correction for a scalar magnitude restricted to [0, upper].

    With posterior theta | r ~ N(mu, var) under the unrestricted N(0, prior_var) prior,
        log p(r | H, 0 <= theta <= upper) = log p(r | H) + log P(0 <= theta <= upper | r)
                                                        - log P(0 <= theta <= upper).
    upper = inf is the one-sided (half-normal) case, log 2 Phi(mu / s)."""
    s = math.sqrt(max(var, 1e-18))
    if not math.isfinite(upper):
        return math.log(2.0) + float(log_ndtr(mu / s))
    upper = max(upper, 1e-9)
    return _log_ndtr_diff((upper - mu) / s, -mu / s) - _log_ndtr_diff(upper / math.sqrt(max(prior_var, 1e-18)), 0.0)


@dataclass
class HypothesisScore:
    hypothesis: Hypothesis
    loglik: float
    best: Component | None = None
    theta_mean: np.ndarray | None = None
    theta_cov: np.ndarray | None = None
    responsibilities: list[float] = field(default_factory=list)
    posterior: float = 0.0
    marginal: float = 0.0
    loss_t: float | None = None
    loss_sd: float | None = None
    magnitude: dict[str, Any] = field(default_factory=dict)
    fitted: np.ndarray | None = None


@dataclass
class AttributionResult:
    scores: list[HypothesisScore]
    pairs: list[dict[str, Any]]
    rows: list[str]
    residuals: np.ndarray
    sigma: np.ndarray
    chi2: float
    dof: int
    p_value: float
    status: str
    category_probs: dict[str, float]
    estimated_physical_loss_t: float | None
    physical_loss_interval_t: tuple[float, float] | None
    conditional_loss_t: float | None
    conditional_loss_sd_t: float | None
    decomposition: dict[str, float]
    quality_flags: dict[str, list[str]]
    temperature: float

    def ranked(self, n: int = 10) -> list[HypothesisScore]:
        return sorted(self.scores, key=lambda s: -s.marginal)[:n]

    def family_distribution(self) -> dict[str, float]:
        out: dict[str, float] = {}
        for s in self.scores:
            out[s.hypothesis.family] = out.get(s.hypothesis.family, 0.0) + s.posterior
        for p in self.pairs:
            for fam in p["families"]:
                out[fam] = out.get(fam, 0.0) + p["posterior"] / 2.0
        z = sum(out.values()) or 1.0
        return {k: v / z for k, v in out.items()}

    def hypothesis_distribution(self) -> dict[str, float]:
        out = {s.hypothesis.hid: s.posterior for s in self.scores}
        for p in self.pairs:
            for h in p["hypotheses"]:
                out[h] = out.get(h, 0.0) + p["posterior"] / 2.0
        z = sum(out.values()) or 1.0
        return {k: v / z for k, v in out.items()}

    def to_dict(self, top: int = 8) -> dict[str, Any]:
        return {
            "status": self.status,
            "chi2": self.chi2, "dof": self.dof, "p_value": self.p_value,
            "category_probabilities": self.category_probs,
            "estimated_physical_loss_tonnes": self.estimated_physical_loss_t,
            "physical_loss_interval_tonnes": self.physical_loss_interval_t,
            "conditional_loss_if_leak_tonnes": self.conditional_loss_t,
            "conditional_loss_sd_tonnes": self.conditional_loss_sd_t,
            "decomposition_tonnes": self.decomposition,
            "quality_flags": self.quality_flags,
            "hypotheses": [{
                "hypothesis_id": s.hypothesis.hid, "family": s.hypothesis.family,
                "target": s.hypothesis.target, "category": s.hypothesis.category,
                "label": s.hypothesis.label, "calibrated_probability": round(s.marginal, 4),
                "exclusive_probability": round(s.posterior, 4), "log_likelihood": round(s.loglik, 3),
                "magnitude": s.magnitude,
            } for s in self.ranked(top)],
            "multi_fault_combinations": [
                {k: v for k, v in p.items() if k in ("hypotheses", "posterior")} for p in self.pairs[:5]],
        }


class PhysicsBayesAttributor:
    def __init__(self, plant: PlantConfig, family_prior: dict[str, float] | None = None,
                 exclude_families: set[str] | None = None, temperature: float = 1.0,
                 multi_fault: bool = True, top_k_pairs: int = 6, pair_penalty: float = 0.08,
                 unknown_kappa: float = 5.0, use_kinds: set[str] | None = None,
                 loss_report_threshold: float = 0.6, prior_mix: float = 0.0,
                 loss_model_rel_error: float = 0.25):
        self.plant = plant
        # evidence discounting: posterior_new = (1 - eps) * posterior + eps * prior   (calibrated)
        self.prior_mix = prior_mix
        # relative model error of the loss estimate (pressure scaling, onset timing); calibrated
        self.loss_model_rel_error = loss_model_rel_error
        self.hypotheses = build_hypotheses(plant, family_prior, exclude_families)
        self.temperature = temperature
        self.multi_fault = multi_fault
        self.top_k_pairs = top_k_pairs
        self.pair_penalty = pair_penalty
        self.unknown_kappa = unknown_kappa
        self.use_kinds = use_kinds
        self.loss_report_threshold = loss_report_threshold
        self.segments = TopologyBuilder(plant).segments
        self.meter_temp = {m: plant.sensors[m].temperature_sensor for m in plant.meters()}
        self.meter_dens = {m: plant.sensors[m].density_sensor for m in plant.meters()}
        self.tank_of_tt = {s: plant.sensors[s].asset for s in plant.sensors_of_type("temperature")
                           if plant.sensors[s].asset in plant.tanks}
        self.tank_of_lt = {s: plant.sensors[s].asset for s in plant.sensors_of_type("level")}

    # =================================================================== API
    def attribute(self, evidence: Evidence) -> AttributionResult:
        A = assemble(evidence, kinds=self.use_kinds)
        n = len(A.r)
        idx = {}
        for i, row in enumerate(A.rows):
            idx[(row.window, row.kind, row.key)] = i
        W = len(evidence.windows)
        Lc = linalg.cholesky(A.cov, lower=True)
        r_hat = linalg.solve_triangular(Lc, A.r, lower=True)
        logdet = 2.0 * float(np.sum(np.log(np.diag(Lc))))
        base_quad = float(r_hat @ r_hat)
        base_ll = -0.5 * (n * math.log(2 * math.pi) + logdet + base_quad)
        c2 = base_quad
        from scipy import stats
        pval = float(stats.chi2.sf(c2, n)) if n else 1.0

        ctx = dict(A=A, idx=idx, W=W, Lc=Lc, r_hat=r_hat, logdet=logdet, base_quad=base_quad, n=n,
                   evidence=evidence)
        quality = self._quality_flags(evidence)
        scores: list[HypothesisScore] = []
        for h in self.hypotheses:
            if h.family == "NORMAL":
                sc = HypothesisScore(h, base_ll)
            elif h.family == "UNKNOWN":
                sc = HypothesisScore(h, self._unknown_ll(A, n))
            else:
                comps = self._components(h, ctx)
                sc = self._score(h, comps, ctx)
            if h.family in ("SENSOR_STUCK", "SENSOR_DROPOUT"):
                flagged = h.target in quality["stuck" if h.family == "SENSOR_STUCK" else "dropout"]
                sc.loglik += math.log(QUALITY_LR_POS if flagged else QUALITY_LR_NEG)
            scores.append(sc)

        # pairs
        pairs: list[dict[str, Any]] = []
        T = self.temperature
        logpost = np.array([s.loglik / T + math.log(s.hypothesis.prior) for s in scores])
        if self.multi_fault and n > 0:
            cand = [s for s in sorted(scores, key=lambda s: -(s.loglik / T + math.log(s.hypothesis.prior)))
                    if s.best is not None and s.hypothesis.family not in ("NORMAL", "UNKNOWN", "SENSOR_DROPOUT")
                    ][: self.top_k_pairs]
            for i in range(len(cand)):
                for j in range(i + 1, len(cand)):
                    a, b = cand[i], cand[j]
                    if a.hypothesis.family == b.hypothesis.family and a.hypothesis.target == b.hypothesis.target:
                        continue
                    S = np.hstack([a.best.S, b.best.S])
                    C = linalg.block_diag(a.best.C, b.best.C)
                    ll, mu, cov = self._marginal(S, C, ctx)
                    # each member enters with its component's prior weight (onset / mechanism choice), as
                    # it does in its single score: without it a pair picks the best of ~50 onsets per member
                    # for free (~4 nats each) and one noisy gauge read becomes a "second fault"
                    ll += a.best.logw + b.best.logw
                    corr = 0.0
                    off = 0
                    for comp in (a.best, b.best):
                        p = comp.S.shape[1]
                        if comp.one_sided and p == 1:
                            corr += bounded_magnitude_correction(mu[off], cov[off, off], comp.C[0, 0], comp.upper)
                        off += p
                    ll += corr
                    if a.hypothesis.family == "SENSOR_STUCK" or b.hypothesis.family == "SENSOR_STUCK":
                        for s in (a, b):
                            if s.hypothesis.family == "SENSOR_STUCK":
                                flagged = s.hypothesis.target in quality["stuck"]
                                ll += math.log(QUALITY_LR_POS if flagged else QUALITY_LR_NEG)
                    prior = a.hypothesis.prior * b.hypothesis.prior * self.pair_penalty / max(
                        a.hypothesis.prior + b.hypothesis.prior, 1e-9)
                    pairs.append({"hypotheses": [a.hypothesis.hid, b.hypothesis.hid],
                                  "families": [a.hypothesis.family, b.hypothesis.family],
                                  "loglik": ll, "logpost": ll / T + math.log(prior),
                                  "theta": mu, "S": S, "scores": (a, b)})
        all_lp = np.concatenate([logpost, np.array([p["logpost"] for p in pairs])]) if pairs else logpost
        Z = logsumexp(all_lp) if len(all_lp) else 0.0
        eps = self.prior_mix
        for s, lp in zip(scores, logpost):
            s.posterior = (1.0 - eps) * float(math.exp(lp - Z)) + eps * s.hypothesis.prior
            s.marginal = s.posterior
        for p in pairs:
            p["posterior"] = (1.0 - eps) * float(math.exp(p["logpost"] - Z))
            for s in p["scores"]:
                s.marginal += p["posterior"]
        pairs.sort(key=lambda p: -p["posterior"])

        # magnitudes / loss
        for s in scores:
            self._describe(s, ctx)
        cat: dict[str, float] = {}
        for s in scores:
            cat[s.hypothesis.category] = cat.get(s.hypothesis.category, 0.0) + s.posterior
        for p in pairs:
            for s in p["scores"]:
                cat[s.hypothesis.category] = cat.get(s.hypothesis.category, 0.0) + p["posterior"] / 2
        loss_mean, loss_iv, cond, cond_sd = self._loss_estimate(scores, pairs, cat)
        decomp = self._decomposition(scores, pairs, A)
        status = self._status(scores, pairs, cat)
        return AttributionResult(
            scores=scores, pairs=[{k: v for k, v in p.items() if k not in ("S", "scores", "theta")} for p in pairs],
            rows=[r.row_id for r in A.rows], residuals=A.r, sigma=A.sigma_r, chi2=c2, dof=n, p_value=pval,
            status=status, category_probs={k: round(v, 4) for k, v in cat.items()},
            estimated_physical_loss_t=loss_mean, physical_loss_interval_t=loss_iv, conditional_loss_t=cond,
            conditional_loss_sd_t=cond_sd,
            decomposition=decomp, quality_flags=quality, temperature=T)

    # ========================================================== likelihoods
    def _marginal(self, S: np.ndarray, C: np.ndarray, ctx) -> tuple[float, np.ndarray, np.ndarray]:
        Lc, r_hat, n = ctx["Lc"], ctx["r_hat"], ctx["n"]
        # entries built from unavailable data (e.g. a dropped-out gauge) carry no information
        S = np.nan_to_num(S, nan=0.0, posinf=0.0, neginf=0.0)
        S_hat = linalg.solve_triangular(Lc, S, lower=True)
        p = S.shape[1]
        Cinv = linalg.inv(C)
        M = Cinv + S_hat.T @ S_hat
        b = S_hat.T @ r_hat
        try:
            cf = linalg.cho_factor(M)
            sol = linalg.cho_solve(cf, b)
            logdetM = 2.0 * float(np.sum(np.log(np.diag(cf[0]))))
            covp = linalg.cho_solve(cf, np.eye(p))
        except linalg.LinAlgError:
            sol = linalg.lstsq(M, b)[0]
            sign, logdetM = np.linalg.slogdet(M)
            covp = linalg.pinv(M)
        sign_c, logdetC = np.linalg.slogdet(C)
        quad = ctx["base_quad"] - float(b @ sol)
        ll = -0.5 * (n * math.log(2 * math.pi) + ctx["logdet"] + logdetC + logdetM + quad)
        return ll, sol, covp

    def _score(self, h: Hypothesis, comps: list[Component], ctx) -> HypothesisScore:
        if not comps:
            return HypothesisScore(h, -0.5 * (ctx["n"] * math.log(2 * math.pi) + ctx["logdet"] + ctx["base_quad"]))
        lls = []
        sols = []
        for c in comps:
            if not np.any(c.S):
                ll = -0.5 * (ctx["n"] * math.log(2 * math.pi) + ctx["logdet"] + ctx["base_quad"])
                mu, cov = np.zeros(c.S.shape[1]), c.C.copy()
            else:
                ll, mu, cov = self._marginal(c.S, c.C, ctx)
                if c.one_sided and len(mu) == 1:
                    ll += bounded_magnitude_correction(mu[0], cov[0, 0], c.C[0, 0], c.upper)
            lls.append(ll + c.logw)
            sols.append((mu, cov))
        lls = np.array(lls)
        tot = float(logsumexp(lls))
        resp = np.exp(lls - tot)
        k = int(np.argmax(lls))
        sc = HypothesisScore(h, tot, best=comps[k], theta_mean=sols[k][0], theta_cov=sols[k][1],
                             responsibilities=resp.tolist())
        sc.fitted = comps[k].S @ sols[k][0]
        sc._comps = comps  # type: ignore[attr-defined]
        sc._sols = sols    # type: ignore[attr-defined]
        return sc

    def _unknown_ll(self, A: Assembled, n: int) -> float:
        if n == 0:
            return 0.0
        cov = A.cov + np.diag((self.unknown_kappa * A.sigma_r) ** 2)
        L = linalg.cholesky(cov, lower=True)
        rh = linalg.solve_triangular(L, A.r, lower=True)
        return -0.5 * (n * math.log(2 * math.pi) + 2 * float(np.sum(np.log(np.diag(L)))) + float(rh @ rh))

    # =========================================================== signatures
    # Candidate onset times: three sub-window positions per window. The exact
    # fraction matters for window-averaged rows (meter mass, consistency rows).
    ONSET_FRACTIONS = (1.0 / 6.0, 0.5, 5.0 / 6.0)
    STUCK_TAU = {"flow": 0.2, "level": 0.3, "temperature": 3.0, "density": 8.0}

    def _onsets(self, ev: Evidence) -> list[tuple[float, int]]:
        tb = ev.t_edges
        return [(tb[w] + f * (tb[w + 1] - tb[w]), w) for w in range(len(tb) - 1) for f in self.ONSET_FRACTIONS]

    @staticmethod
    def _traj(kind: str, t_on: float, tb: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """Bias trajectory -> (values at boundaries, averages over windows)."""
        a, b = tb[:-1], tb[1:]
        if kind == "offset":
            return np.ones(len(tb)), np.ones(len(a))
        if kind == "step":
            return (tb > t_on).astype(float), np.clip((b - np.maximum(a, t_on)) / (b - a), 0.0, 1.0)
        if kind == "drift":
            bnd = np.maximum(tb - t_on, 0.0) / 3600.0
            avg = np.where(b <= t_on, 0.0, np.where(a >= t_on, 0.5 * (a + b) - t_on,
                                                    (b - t_on) ** 2 / (2.0 * (b - a)))) / 3600.0
            return bnd, avg
        raise ValueError(kind)

    def _components(self, h: Hypothesis, ctx) -> list[Component]:
        fam, tg = h.family, h.target
        ev: Evidence = ctx["evidence"]
        idx, W, n = ctx["idx"], ctx["W"], ctx["n"]
        P = self.plant
        tb = np.asarray(ev.t_edges, dtype=float)

        # ---------------------------------------------------- stuck / dropout
        if fam in ("SENSOR_STUCK", "SENSOR_DROPOUT"):
            kind = P.sensors[tg].type
            S_bnd, S_avg = self._sensor_maps(tg, kind, ctx)
            if fam == "SENSOR_STUCK":
                flagged = [w for w, win in enumerate(ev.windows) if win.quality.get(tg, {}).get("stuck")]
            else:
                flagged = [w for w, win in enumerate(ev.windows) if win.quality.get(tg, {}).get("dropout")]
            if not flagged:
                return []
            aw = sorted(set(flagged) | {w + 1 for w in flagged if w + 1 < W})
            ab = sorted({w for w in aw} | {w + 1 for w in aw})
            cols = [S_bnd[:, k] for k in ab] + [S_avg[:, w] for w in aw]
            S = np.column_stack(cols) if cols else np.zeros((n, 1))
            tau = self.STUCK_TAU.get(kind, 1.0)
            return [Component(S, np.eye(S.shape[1]) * tau ** 2, 0.0,
                              meta={"param": "affected_window_errors", "windows": aw})]

        # ---------------------------------------------- calibration-type errors
        if fam in ("METER_ERROR", "LEVEL_ERROR", "TEMP_ERROR", "DENSITY_ERROR"):
            kind = P.sensors[tg].type
            S_bnd, S_avg = self._sensor_maps(tg, kind, ctx)
            if not (np.any(S_bnd) or np.any(S_avg)):
                return []
            tau0, tstep, tdrift = TAU3[fam]

            def col(kind_: str, t_on: float):
                bnd, avg = self._traj(kind_, t_on, tb)
                return S_bnd @ bnd + S_avg @ avg, bnd

            off, off_b = col("offset", 0.0)
            variants = [("drift", float(tb[0]), -1)] + [(mech, t_on, w) for t_on, w in self._onsets(ev)
                                                        for mech in ("step", "drift")]
            comps = []
            for mech, t_on, w in variants:
                c, cb = col(mech, t_on)
                S = np.column_stack([off, c])
                C = np.diag([tau0 ** 2, (tstep if mech == "step" else tdrift) ** 2])
                comps.append(Component(S, C, -math.log(len(variants)),
                                       meta={"mechanism": mech, "onset_window": w, "t_on": t_on,
                                             "B": np.column_stack([off_b, cb])}))
            return comps

        # ------------------------------------------------ rate-type physical faults
        if fam in ("PIPELINE_LEAK", "TANK_LEAK", "VALVE_PASSING", "PUMP_DEGRADATION"):
            bases: list[tuple[list[list[tuple[int | None, float]]], list[float], float, dict]] = []
            if fam == "PIPELINE_LEAK":
                for sg in self.segments[tg]:
                    per_w, loss_w = [], []
                    for w, win in enumerate(ev.windows):
                        sq = win.pipe_sqrtp.get(tg, math.nan)
                        scale = win.duration_h * sq if not math.isnan(sq) else 0.0
                        per_w.append([(idx.get((w, "CV", win.topology.cv_of_segment(sg))), scale)])
                        loss_w.append(scale)
                    bases.append((per_w, loss_w, math.log(max(sg.s1 - sg.s0, 1e-3)), {"segment": sg.id}))
            elif fam == "TANK_LEAK":
                per_w, loss_w = [], []
                for w, win in enumerate(ev.windows):
                    lvl = win.tank_level.get(tg, math.nan)
                    scale = win.duration_h * math.sqrt(max(lvl, 0.0) / 10.0) if not math.isnan(lvl) else 0.0
                    per_w.append([(idx.get((w, "CV", win.topology.cv_of_tank(tg))), scale)])
                    loss_w.append(scale)
                bases.append((per_w, loss_w, 0.0, {}))
            elif fam == "VALVE_PASSING":
                per_w = []
                for w, win in enumerate(ev.windows):
                    per_w.append(self._valve_entries(tg, w, win, ev, idx))
                bases.append((per_w, [0.0] * W, 0.0, {"_upper_w": [self._valve_rate_bound(tg, win)
                                                                   for win in ev.windows]}))
            else:  # PUMP_DEGRADATION
                # pumps in parallel share the discharge header: a degraded pump lowers the header
                # head seen by every running pump (~ d*H/n for n identical pumps)
                per_w = []
                for w, win in enumerate(ev.windows):
                    hc = win.pump_curve_head.get(tg)
                    entries = []
                    if hc is not None:
                        running = [p for p in win.pump_curve_head]
                        share = 1.0 / len(running)
                        for p in running:
                            entries.append((idx.get((w, "PUMP", p)), -hc * share))
                    per_w.append(entries)
                bases.append((per_w, [0.0] * W, 0.0, {}))
            tau = TAU[fam]
            onsets = [(-math.inf, -1)] + self._onsets(ev)
            comps = []
            for per_w, loss_w, lw, meta in bases:
                upper_w = meta.pop("_upper_w", None)
                for t_on, w_on in onsets:
                    act = np.ones(W) if t_on == -math.inf else self._traj("step", t_on, tb)[1]
                    upper = min([u for u, a in zip(upper_w, act) if a > 0] or [math.inf]) if upper_w else math.inf
                    s = np.zeros((n, 1))
                    for w in range(W):
                        if act[w] <= 0:
                            continue
                        for i, sc in per_w[w]:
                            if i is not None:
                                s[i, 0] += act[w] * sc
                    loss_scale = float(np.dot(act, loss_w))
                    comps.append(Component(s, np.array([[tau ** 2]]), lw - math.log(len(onsets)), True,
                                           meta=dict(meta, onset_window=w_on, loss_scale=loss_scale), upper=upper))
            return comps

        # ----------------------------------------------------------- accounting
        if fam == "MISSING_TRANSFER":
            route = P.routes[tg]
            S = np.zeros((n, W))
            for w, win in enumerate(ev.windows):
                plausible = set(route.open_valves) <= set(win.topology.open_valves) and all(
                    m in win.meter_mass for m in route.meters)
                if not plausible:
                    continue
                for m in route.meters:
                    i = idx.get((w, "LEDGER", m))
                    if i is not None:
                        S[i, w] += win.unbooked[m]
            if not np.any(S):
                return []
            return [Component(S, np.eye(W) * TAU[fam] ** 2, 0.0, meta={"param": "unbooked_fraction"})]
        return []

    def _valve_rate_bound(self, vid: str, win) -> float:
        """Upper bound on the passing magnitude (t/h per unit pressure term) from a silent meter.

        Flow passing a shut tank valve runs the whole length of its line. When it runs in the
        forward direction of a flow meter on that line and the meter registered nothing, the flow
        is below the meter's low-flow cut-off: a meter reading zero bounds the flow; it is not
        an absence of evidence."""
        P = self.plant
        vcfg = P.valves[vid]
        term = win.valve_dp_term.get(vid, 0.0)
        if not term:
            return math.inf
        line_to_tank = term > 0
        forward = (vcfg.position_on_line >= 0.5) == line_to_tank
        if not forward:
            return math.inf
        bound = math.inf
        for m, cutoff in win.silent_meters.items():
            if P.sensors[m].asset == vcfg.pipeline and cutoff > 0:
                # m3/h -> t/h (crude ~0.86 t/m3), 25 % margin for meter error near the cut-off
                bound = min(bound, 1.25 * cutoff * 0.86 / abs(term))
        return bound

    def _valve_entries(self, vid: str, w: int, win, ev: Evidence, idx) -> list[tuple[int | None, float]]:
        """Signature of 1 t/h (at 5 bar differential) passing through a shut valve."""
        P = self.plant
        vcfg = P.valves[vid]
        cut = next((c for c in win.topology.valve_cuts if c.valve_id == vid), None)
        if cut is None or not cut.tank_id or vid not in win.valve_dp_term:
            return []
        term = win.valve_dp_term[vid]
        if term == 0.0:
            return []
        mag = win.duration_h * abs(term)
        d = 1.0 if term > 0 else -1.0                     # +1: line -> tank
        out = [(idx.get((w, "CV", cut.line_cv)), d * mag), (idx.get((w, "CV", cut.tank_cv)), -d * mag)]
        positive_dir = (vcfg.position_on_line >= 0.5 and d > 0) or (vcfg.position_on_line < 0.5 and d < 0)
        if positive_dir:
            for m in win.meter_mass:
                if P.sensors[m].asset == cut.pipe_id:
                    out.append((idx.get((w, "LEDGER", m)), mag))
        # tank -> suction manifold while another tank is the route source: the passing crude
        # mixes into the pumped stream and shifts the discharge density meter (DT-002)
        if d < 0 and vcfg.position_on_line < 0.5 and win.source_tank and win.source_tank != cut.tank_id:
            j = idx.get((w, "DCONS", "DT-002"))
            thr = max((mm["mass_t"] for mm in win.meter_mass.values()), default=0.0) / max(win.duration_h, 1e-6)
            if j is not None and thr > 1.0:
                drho = ev.inventories[w][cut.tank_id]["rho15"] - ev.inventories[w][win.source_tank]["rho15"]
                out.append((j, drho * abs(term) / thr))
        return out

    def _sensor_maps(self, sid: str, kind: str, ctx) -> tuple[np.ndarray, np.ndarray]:
        """Linear maps from a sensor's bias trajectory to evidence rows.

        S_bnd (n x W+1): effect of the bias value at each window boundary
                         (tank inventories are evaluated at boundaries).
        S_avg (n x W)  : effect of the window-average bias (integrated meter
                         mass, consistency rows).
        """
        ev: Evidence = ctx["evidence"]
        idx, W, n = ctx["idx"], ctx["W"], ctx["n"]
        S_bnd = np.zeros((n, W + 1))
        S_avg = np.zeros((n, W))
        if kind == "flow":
            for w, win in enumerate(ev.windows):
                if sid not in win.meter_mass:
                    continue
                M = win.meter_mass[sid]["mass_t"]
                for cv, signs in win.meter_sign.items():
                    if sid in signs:
                        i = idx.get((w, "CV", cv))
                        if i is not None:
                            S_avg[i, w] += signs[sid] * M
                i = idx.get((w, "LEDGER", sid))
                if i is not None:
                    S_avg[i, w] += win.unbooked[sid]
        elif kind == "level":
            tank = self.tank_of_lt[sid]
            for w, win in enumerate(ev.windows):
                i = idx.get((w, "CV", win.topology.cv_of_tank(tank)))
                if i is None:
                    continue
                a, b = ev.inventories[w][tank], ev.inventories[w + 1][tank]
                S_bnd[i, w + 1] += -b["area_m2"] * b["rho_obs"] / 1000.0
                S_bnd[i, w] += a["area_m2"] * a["rho_obs"] / 1000.0
        elif kind == "temperature":
            if sid in self.tank_of_tt:
                tank = self.tank_of_tt[sid]
                for w, win in enumerate(ev.windows):
                    i = idx.get((w, "CV", win.topology.cv_of_tank(tank)))
                    a, b = ev.inventories[w][tank], ev.inventories[w + 1][tank]
                    if i is not None:
                        S_bnd[i, w + 1] += b["alpha"] * b["mass_t"] / (1 + b["alpha"] * (b["temperature_c"] - 15))
                        S_bnd[i, w] -= a["alpha"] * a["mass_t"] / (1 + a["alpha"] * (a["temperature_c"] - 15))
                    for row in win.rows:
                        if row.kind == "TCONS" and row.meta.get("source_tank") == tank:
                            j = idx.get((w, "TCONS", row.key))
                            if j is not None:
                                S_avg[j, w] -= 1.0
            else:
                meters = [m for m, t in self.meter_temp.items() if t == sid]
                for w, win in enumerate(ev.windows):
                    for m in meters:
                        if m not in win.meter_mass:
                            continue
                        M = win.meter_mass[m]["mass_t"]
                        for cv, signs in win.meter_sign.items():
                            if m in signs:
                                i = idx.get((w, "CV", cv))
                                if i is not None:
                                    S_avg[i, w] += signs[m] * (-0.00085 * M)
                    j = idx.get((w, "TCONS", sid))
                    if j is not None:
                        S_avg[j, w] += 1.0
        elif kind == "density":
            meters = [m for m, d in self.meter_dens.items() if d == sid]
            for w, win in enumerate(ev.windows):
                for m in meters:
                    if m not in win.meter_mass:
                        continue
                    M, rho = win.meter_mass[m]["mass_t"], win.meter_mass[m]["rho15"]
                    if not rho or math.isnan(rho):
                        continue
                    for cv, signs in win.meter_sign.items():
                        if m in signs:
                            i = idx.get((w, "CV", cv))
                            if i is not None:
                                S_avg[i, w] += signs[m] * M / rho
                j = idx.get((w, "DCONS", sid))
                if j is not None:
                    S_avg[j, w] += 1.0
        return S_bnd, S_avg

    # ============================================================ reporting
    def _quality_flags(self, ev: Evidence) -> dict[str, list[str]]:
        stuck, dropout = set(), set()
        for win in ev.windows:
            for sid, q in win.quality.items():
                if q["stuck"]:
                    stuck.add(sid)
                if q.get("dropout"):
                    dropout.add(sid)
        return {"stuck": sorted(stuck), "dropout": sorted(dropout)}

    def _describe(self, s: HypothesisScore, ctx) -> None:
        fam = s.hypothesis.family
        if s.theta_mean is None or s.best is None:
            return
        mu = s.theta_mean
        cov = s.theta_cov
        meta = s.best.meta
        d: dict[str, Any] = {}
        if "B" in meta:
            mu = meta["B"] @ mu  # boundary biases
        if fam in ("SENSOR_STUCK", "SENSOR_DROPOUT"):
            s.magnitude = {"affected_windows": meta.get("windows", [])}
            return
        if fam in ("METER_ERROR", "SENSOR_STUCK") and self.plant.sensors[s.hypothesis.target].type == "flow":
            d = {"bias_percent_latest": round(100 * float(mu[-1]), 3), "bias_percent_first": round(100 * float(mu[0]), 3)}
        elif fam == "LEVEL_ERROR" or (fam == "SENSOR_STUCK" and self.plant.sensors[s.hypothesis.target].type == "level"):
            d = {"level_error_change_mm": round(1000 * float(mu[-1] - mu[0]), 2)}
        elif fam == "TEMP_ERROR" or fam == "SENSOR_STUCK":
            d = {"temperature_bias_c_latest": round(float(mu[-1]), 3)}
        elif fam == "DENSITY_ERROR":
            d = {"density_bias_kgm3_latest": round(float(mu[-1]), 3)}
        if "onset_window" in meta and fam in ("METER_ERROR", "LEVEL_ERROR", "TEMP_ERROR", "DENSITY_ERROR"):
            d["onset_window"] = meta["onset_window"]
            d["mechanism"] = meta.get("mechanism")
        elif fam in ("PIPELINE_LEAK", "TANK_LEAK", "VALVE_PASSING"):
            sd = math.sqrt(max(float(cov[0, 0]), 0.0))
            d = {"rate_t_per_h_ref": round(float(mu[0]), 3), "rate_sd": round(sd, 3),
                 "onset_window": meta.get("onset_window"), "segment": meta.get("segment")}
            if fam != "VALVE_PASSING":
                s.loss_t = max(float(mu[0]), 0.0) * meta.get("loss_scale", 0.0)
                s.loss_sd = sd * meta.get("loss_scale", 0.0)
                d["loss_tonnes"] = round(s.loss_t, 3)
        elif fam == "PUMP_DEGRADATION":
            d = {"head_loss_percent": round(100 * float(mu[0]), 2), "onset_window": meta.get("onset_window")}
        elif fam == "MISSING_TRANSFER":
            ev: Evidence = ctx["evidence"]
            tot = 0.0
            route = self.plant.routes[s.hypothesis.target]
            for w, win in enumerate(ev.windows):
                if w < len(mu):
                    tot += float(mu[w]) * max((win.meter_mass.get(m, {}).get("mass_t", 0.0) for m in route.meters),
                                              default=0.0)
            d = {"unbooked_tonnes": round(tot, 3)}
        s.magnitude = d

    def _loss_estimate(self, scores, pairs, cat):
        """Physical loss over the evidence period, conditional on a physical-loss cause.

        Reported (not null) only when P(physical loss) >= loss_report_threshold.
        The interval combines parameter uncertainty, between-hypothesis spread and a
        calibrated relative model error.
        """
        leak = [s for s in scores if s.hypothesis.category == "physical_loss" and s.loss_t is not None]
        p_loss = cat.get("physical_loss", 0.0)
        if not leak or p_loss <= 1e-9:
            return None, None, None, None
        wsum = sum(s.marginal for s in leak) or 1.0
        cond = sum(s.marginal * s.loss_t for s in leak) / wsum
        var = sum(s.marginal * ((s.loss_sd or 0) ** 2 + (s.loss_t - cond) ** 2) for s in leak) / wsum
        var += (self.loss_model_rel_error * cond) ** 2
        sd = math.sqrt(var)
        if p_loss < self.loss_report_threshold:
            return None, None, round(cond, 3), round(sd, 3)
        return (round(cond, 3), (round(max(cond - 1.645 * sd, 0.0), 3), round(cond + 1.645 * sd, 3)),
                round(cond, 3), round(sd, 3))

    def _decomposition(self, scores, pairs, A: Assembled) -> dict[str, float]:
        cv_mask = np.array([r.kind == "CV" for r in A.rows], dtype=float)
        R = float(A.r @ cv_mask)
        comp = {"physical_loss": 0.0, "measurement": 0.0, "unrecorded_movement": 0.0}
        for s in scores:
            if s.fitted is None or s.hypothesis.category not in comp:
                continue
            comp[s.hypothesis.category] += s.posterior * float(s.fitted @ cv_mask)
        for p in pairs:
            for s in p["scores"]:
                if s.fitted is not None and s.hypothesis.category in comp:
                    comp[s.hypothesis.category] += p["posterior"] * float(s.fitted @ cv_mask)
        eps = R - sum(comp.values())
        return {"total_residual_R": round(R, 3), "L_physical_loss": round(comp["physical_loss"], 3),
                "B_measurement_bias": round(comp["measurement"], 3),
                "U_unrecorded_movement": round(comp["unrecorded_movement"], 3), "epsilon": round(eps, 3)}

    def _status(self, scores, pairs, cat) -> str:
        ranked = sorted(scores, key=lambda s: -s.marginal)
        top = ranked[0]
        if top.hypothesis.family == "NORMAL" and top.marginal >= 0.5:
            return "NO_ABNORMALITY_CONFIRMED"
        if cat.get("unknown", 0.0) >= 0.4 or top.hypothesis.family == "UNKNOWN":
            return "UNKNOWN_CAUSE"
        if top.marginal >= 0.8:
            return "PROBABLE_CAUSE_IDENTIFIED"
        second = ranked[1].marginal if len(ranked) > 1 else 0.0
        if second > 0 and top.marginal / second < 4.0:
            return "COMPETING_HYPOTHESES"
        return "INSUFFICIENT_EVIDENCE"
