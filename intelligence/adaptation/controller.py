"""Adaptive controller - "adapt first, retrain last".

One adaptive cycle walks the ten steps of the adaptive architecture and records what each step
concluded, so the reasoning is auditable:

    1 detect drift            DriftMonitor (Layer 7)
    2 understand behaviour    mean shift vs dispersion change, step vs gradual, which rows / regime
    3 generate hypotheses     model mis-calibration, instrument noise / drift, regime change,
                              process fault, data quality
    4 estimate hidden state   uncertainty scale, row bias (t / window), envelope position
    5 validate via twin       replay recent operations fault-free in the digital twin: if the twin is
                              calibrated but the plant is not, the instruments changed (maintenance),
                              not the model
    6 run optimisation        rank adaptation actions by benefit - cost - risk
    7 recommend               plain-language actions with reasons
    8 self-calibrate          bounded uncertainty re-scaling (auto inside a narrow band, otherwise
                              proposed for engineer approval); approved meter factors
    9 learn from experience   verified cases -> learned fault-family priors (no retraining)
   10 retrain if necessary    gate: only when drift persists after self-calibration AND labelled /
                              synthetic data for the new regime exists; never deployed automatically

Governance (Layer 10): every adaptation is logged with old / new value, reason, who decided, and
can be reverted. Nothing here changes a trained model's weights.
"""
from __future__ import annotations

import json
import logging
import math
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from intelligence.adaptation.drift import SEVERITY_ORDER, DriftMonitor
from intelligence.adaptation.experience import ExperienceMemory
from intelligence.adaptation.twin import TwinValidator

log = logging.getLogger("oilintel.adaptive")

STEPS = [("detect_drift", "Detect drift"), ("understand_behaviour", "Understand behaviour"),
         ("generate_hypotheses", "Generate hypotheses"), ("estimate_hidden_state", "Estimate hidden state"),
         ("validate_via_twin", "Validate via digital twin"), ("run_optimisation", "Run optimisation"),
         ("generate_recommendation", "Generate recommendation"), ("self_calibrate", "Self-calibrate"),
         ("learn_from_experience", "Learn from experience"), ("retrain_if_necessary", "Retrain only if necessary")]
AUTO_BAND = (0.8, 1.35)           # relative change applied automatically (statistical re-scaling only)
HARD_BOUND = (0.7, 2.0)           # absolute limits of any uncertainty scale, even with approval
METER_FACTOR_TOL = 0.0015         # proving deviation beyond repeatability -> propose a new meter factor
PERSIST_CYCLES = 3                # drift must persist this many cycles after self-calibration to retrain

ACTIONS = {   # action: (operational cost, risk) on a 0-10 scale
    "NO_ACTION": (0.0, 0.0),
    "SELF_CALIBRATE_UNCERTAINTY": (0.5, 1.0),
    "REQUEST_INSTRUMENT_CHECK": (3.0, 0.5),
    "OPEN_INVESTIGATION": (2.0, 0.5),
    "FIX_DATA_PIPELINE": (2.0, 0.5),
    "COLLECT_REGIME_DATA": (2.5, 0.5),
    "RECOMMEND_RETRAIN": (8.0, 4.0),
}

ARCHITECTURE = [
    (1, "Data Acquisition & Validation", "MQTT / OPC UA / historian adapters, idempotent ingestion, quality engine "
                                         "(stuck, dropout, range), data gate"),
    (2, "Feature Engineering & Feature Store", "physics features (z-scores, chi2, residuals) + raw/context features, "
                                               "versioned with the model registry"),
    (3, "Adaptive Prediction Engine", "hybrid ensemble: chi-square test, Isolation Forest, XGBoost (physics features), "
                                      "physics-guided Bayesian attribution"),
    (4, "AI Optimisation Engine", "verification-plan optimisation: expected information gain per cost with safety "
                                  "guardrails (no isolation during transfers)"),
    (5, "Recommendation Engine", "next-best check with rationale, expected information gain, cost and duration"),
    (6, "Operator & System Interface", "dashboard, 3D digital twin, alerts, simulation what-if lab, engineer "
                                       "feedback (confirm / reject / verification results)"),
    (7, "Continuous Monitoring & Drift Detection", "data, calibration, prediction, sensor and regime drift with "
                                                   "severity; hand-off to the reasoning core"),
    (8, "Engineering Intelligence Core", "LangGraph investigation agent around the physics constraint engine, "
                                         "state estimation, hypothesis generation, confidence engine, experience memory"),
    (9, "Adaptive Learning Pipeline (fallback)", "retrain gate -> dataset regeneration -> challenger training -> "
                                                  "evaluation against champion"),
    (10, "Model Governance & Lifecycle", "model registry with versions, adaptation log with approve / revert, audit "
                                         "trail of every tool call and decision"),
    (11, "Digital Twin - Central Plant Representation", "live twin state from measurements; physics simulator for "
                                                        "hypothesis replay and what-if"),
    (12, "Business KPI & Value Realisation", "loss located (t), loss value, investigation effort, false-alarm rate"),
]


def _now() -> float:
    return time.time()


class AdaptiveController:
    def __init__(self, engine, plant, state_path: str | Path, reference: dict[str, Any] | None,
                 cases_provider: Callable[[], list[dict[str, Any]]] | None = None,
                 data_provider: Callable[[float, float], tuple[Any, dict[str, Any]] | None] | None = None,
                 overlap_factor: float = 300.0 / 1800.0, window_s: float = 1800.0,
                 synthetic_data_available: bool = True):
        self.engine = engine
        self.synthetic_data_available = synthetic_data_available   # simulation mode: scenarios can be generated
        self.plant = plant
        self.path = Path(state_path)
        self.drift = DriftMonitor(reference, overlap_factor=overlap_factor)
        self.memory = ExperienceMemory()
        self.twin = TwinValidator(plant, engine)
        self.cases_provider = cases_provider
        self.data_provider = data_provider
        self.window_s = window_s
        self.lock = threading.RLock()
        self.state: dict[str, Any] = {"kind_scale": {}, "meter_factors": {}, "adaptations": [], "cycles": [],
                                      "streaks": {"drift_after_calibration": 0, "regime_red": 0},
                                      "twin_validations": 0}
        self.last_drift: dict[str, Any] | None = None
        self._load()
        self.apply_to_engine()
        self.learn_from_experience()

    # ------------------------------------------------------------ persistence
    def _load(self) -> None:
        try:
            if self.path.exists():
                self.state.update(json.loads(self.path.read_text(encoding="utf-8")))
        except Exception:
            log.exception("adaptive state unreadable; starting fresh")

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.state, indent=1, default=str), encoding="utf-8")
            tmp.replace(self.path)
        except Exception:
            log.exception("could not persist adaptive state")

    def apply_to_engine(self) -> None:
        self.engine.kind_scale = dict(self.state.get("kind_scale") or {})
        self.engine.meter_factors = dict(self.state.get("meter_factors") or {})

    # ------------------------------------------------------------ inputs
    def observe(self, result: dict[str, Any], features: dict[str, Any] | None = None) -> None:
        with self.lock:
            self.drift.observe(result, features)

    def reset_run(self) -> None:
        """New simulation run / plant restart: drift statistics must not span the boundary."""
        with self.lock:
            self.drift.reset()

    def on_verification(self, action_id: str, outcome: str, detail: dict[str, Any] | None, user: str = "engineer"):
        """A field check result. Meter proving yields a meter factor -> proposed adaptation."""
        if not action_id.startswith("RETRIEVE_METER_PROVING_RECORD:") or not detail:
            return None
        meter = action_id.split(":", 1)[1]
        mf = detail.get("meter_factor")
        if mf is None or abs(float(mf) - 1.0) <= METER_FACTOR_TOL:
            return None
        with self.lock:
            old = float(self.state["meter_factors"].get(meter, 1.0))
            return self._log("METER_FACTOR", meter, old, round(old * float(mf), 6), "PROPOSED",
                             f"proving of {meter} found meter factor {float(mf):.5f} "
                             f"(as-found error {detail.get('as_found_error_pct')} %); apply after engineer approval",
                             user)

    # ------------------------------------------------------------ governance
    def _log(self, kind: str, target: str, old: float, new: float, status: str, reason: str,
             by: str = "adaptive-controller") -> dict[str, Any]:
        if status == "PROPOSED":
            # one pending proposal per target: a later cycle refreshes it instead of piling up duplicates
            pending = next((x for x in self.state["adaptations"]
                            if x["status"] == "PROPOSED" and x["type"] == kind and x["target"] == target), None)
            if pending is not None:
                pending.update(old=old, new=new, reason=reason, created_at=_now())
                self._save()
                return pending
        a = {"id": f"AD-{uuid.uuid4().hex[:6].upper()}", "type": kind, "target": target, "old": old, "new": new,
             "status": status, "reason": reason, "created_at": _now(), "decided_by": by if status != "PROPOSED" else None,
             "decided_at": _now() if status != "PROPOSED" else None}
        self.state["adaptations"].insert(0, a)
        del self.state["adaptations"][200:]
        if status == "APPLIED":
            self._set(kind, target, new)
        self._save()
        return a

    def _set(self, kind: str, target: str, value: float) -> None:
        key = "kind_scale" if kind == "UNCERTAINTY_SCALE" else "meter_factors"
        if abs(value - 1.0) < 1e-9:
            self.state[key].pop(target, None)
        else:
            self.state[key][target] = value
        self.apply_to_engine()

    def decide(self, adaptation_id: str, decision: str, user: str) -> dict[str, Any]:
        """decision: APPROVE | REJECT | REVERT"""
        with self.lock:
            a = next((x for x in self.state["adaptations"] if x["id"] == adaptation_id), None)
            if a is None:
                raise KeyError(adaptation_id)
            if decision == "APPROVE":
                if a["status"] != "PROPOSED":
                    raise ValueError(f"{adaptation_id} is {a['status']}, not PROPOSED")
                self._set(a["type"], a["target"], a["new"])
                a["status"] = "APPLIED"
            elif decision == "REJECT":
                if a["status"] != "PROPOSED":
                    raise ValueError(f"{adaptation_id} is {a['status']}, not PROPOSED")
                a["status"] = "REJECTED"
            elif decision == "REVERT":
                if a["status"] != "APPLIED":
                    raise ValueError(f"{adaptation_id} is {a['status']}, not APPLIED")
                self._set(a["type"], a["target"], a["old"])
                a["status"] = "REVERTED"
            else:
                raise ValueError("decision must be APPROVE, REJECT or REVERT")
            a["decided_by"], a["decided_at"] = user, _now()
            self.drift.reset()        # statistics were computed under the previous configuration
            self._save()
            return a

    # ------------------------------------------------------------ learning
    def learn_from_experience(self) -> dict[str, Any]:
        cases = []
        if self.cases_provider:
            try:
                cases = self.cases_provider() or []
            except Exception:
                log.exception("experience provider failed")
        self.memory.update(cases)
        self.engine.family_prior = self.memory.priors() if self.memory.n_cases else None
        return self.memory.summary()

    def twin_validate(self, telemetry, records, edges, hypotheses, max_hypotheses: int = 3) -> dict[str, Any]:
        res = self.twin.validate(telemetry, records, edges, hypotheses, max_hypotheses)
        with self.lock:
            self.state["twin_validations"] = int(self.state.get("twin_validations", 0)) + 1
            self._save()
        return res

    # ------------------------------------------------------------ the cycle
    def run_cycle(self, trigger: str = "scheduled", now: float | None = None, use_twin: bool = True) -> dict[str, Any]:
        with self.lock:
            t_start = _now()
            steps: dict[str, dict[str, Any]] = {}

            def step(key: str, status: str, summary: str, **detail):
                steps[key] = {"status": status, "summary": summary, "detail": detail}

            rep = self.drift.report()
            self.last_drift = rep
            sig = [s for s in rep.get("signals", []) if s["severity"] != "GREEN"]
            if rep["status"] == "WARMING_UP":
                step("detect_drift", "waiting", f"collecting windows ({rep['n_windows']}/{rep['required']})")
            else:
                step("detect_drift", "alert" if sig else "ok",
                     f"{len(sig)} drift signal(s): " + ", ".join(f"{s['kind']} {s['severity']}" for s in sig)
                     if sig else "no drift - all signals GREEN", overall=rep["overall"])

            # 2 understand behaviour
            behaviours = []
            for s in sig:
                d = s.get("detail") or {}
                if s["kind"] == "calibration":
                    k = d.get("worst_kind")
                    kd = d.get(k, {}) if k else {}
                    mean = abs(kd.get("mean_z", 0.0))
                    behaviours.append({"signal": "calibration", "row_kind": k,
                                       "pattern": "dispersion change with zero mean" if mean < 0.5 else
                                       "dispersion change WITH mean shift", "mean_z": kd.get("mean_z"),
                                       "sd_scale": kd.get("sd_scale")})
                elif s["kind"] == "sensor":
                    behaviours.append({"signal": "sensor", "row": d.get("row"), "pattern": f"persistent mean shift, "
                                       f"{d.get('shape')}", "mean_z": d.get("mean_z")})
                elif s["kind"] in ("data", "regime"):
                    behaviours.append({"signal": s["kind"], "pattern": "operating point moved", "detail": d})
                elif s["kind"] == "prediction":
                    behaviours.append({"signal": "prediction", "pattern": "alarms that do not persist", "detail": d})
                elif s["kind"] == "data_gate":
                    behaviours.append({"signal": "data_gate", "pattern": "instrument data failing the quality gate"})
            step("understand_behaviour", "ok" if behaviours else "skipped",
                 "; ".join(f"{b['signal']}: {b['pattern']}" for b in behaviours) or "nothing to explain",
                 behaviours=behaviours)

            # 3 hypotheses
            hyps = []
            for b in behaviours:
                if b["signal"] == "calibration" and "zero mean" in b["pattern"]:
                    hyps += [{"id": "MODEL_MISCALIBRATION", "about": b["row_kind"], "plausibility": 0.5},
                             {"id": "INSTRUMENT_NOISE_INCREASE", "about": b["row_kind"], "plausibility": 0.5}]
                elif b["signal"] == "calibration":
                    hyps.append({"id": "PROCESS_FAULT_OR_BIAS", "about": b["row_kind"], "plausibility": 0.7})
                elif b["signal"] == "sensor":
                    hyps += [{"id": "INSTRUMENT_DRIFT", "about": b["row"], "plausibility": 0.5},
                             {"id": "SMALL_PHYSICAL_LOSS", "about": b["row"], "plausibility": 0.5}]
                elif b["signal"] in ("data", "regime"):
                    hyps.append({"id": "REGIME_CHANGE", "about": b["signal"], "plausibility": 0.8})
                elif b["signal"] == "prediction":
                    hyps.append({"id": "ALARM_MISCALIBRATION", "about": "detectors", "plausibility": 0.6})
                elif b["signal"] == "data_gate":
                    hyps.append({"id": "DATA_QUALITY", "about": "instruments", "plausibility": 0.9})
            step("generate_hypotheses", "ok" if hyps else "skipped",
                 ", ".join(f"{h['id']}({h['about']})" for h in hyps) or "no hypotheses needed", hypotheses=hyps)

            # 4 hidden state
            state_est: dict[str, Any] = {}
            for b in behaviours:
                if b["signal"] == "calibration" and b.get("row_kind"):
                    state_est[f"sd_scale:{b['row_kind']}"] = b.get("sd_scale")
                if b["signal"] == "sensor":
                    state_est[f"mean_z:{b['row']}"] = b.get("mean_z")
            step("estimate_hidden_state", "ok" if state_est else "skipped",
                 ", ".join(f"{k} = {v}" for k, v in state_est.items()) or "no hidden state to estimate", **state_est)

            # 5 twin validation (calibration hypotheses): fault-free replay of the recent period
            twin_note, twin_detail = "not required", {}
            cal = next((h for h in hyps if h["id"] == "MODEL_MISCALIBRATION"), None)
            if cal and use_twin and self.data_provider and now:
                try:
                    data = self.data_provider(now - 3 * self.window_s - 900, now)
                    if data is not None:
                        tel, rec = data
                        edges = [now - k * self.window_s for k in (3, 2, 1, 0)]
                        twin_ratio = self.twin.normal_dispersion(tel, rec, edges)
                        plant_ratio = (cal.get("about") and next((b["sd_scale"] for b in behaviours
                                                                  if b.get("row_kind") == cal["about"]), None))
                        tr = twin_ratio.get(cal["about"])
                        twin_detail = {"twin_mean_z2": twin_ratio, "plant_sd_scale": plant_ratio}
                        if tr is not None and plant_ratio is not None:
                            twin_sd = math.sqrt(max(tr, 1e-6))
                            if twin_sd < 1.3 and plant_ratio > 1.3:
                                for h in hyps:
                                    h["plausibility"] = 0.75 if h["id"] == "INSTRUMENT_NOISE_INCREASE" else (
                                        0.25 if h["id"] == "MODEL_MISCALIBRATION" else h["plausibility"])
                                twin_note = (f"twin replay is calibrated (sd x{twin_sd:.2f}) while the plant shows "
                                             f"x{plant_ratio:.2f}: instruments behave worse than specified")
                            else:
                                for h in hyps:
                                    h["plausibility"] = 0.75 if h["id"] == "MODEL_MISCALIBRATION" else (
                                        0.25 if h["id"] == "INSTRUMENT_NOISE_INCREASE" else h["plausibility"])
                                twin_note = (f"twin replay shows the same dispersion (sd x{twin_sd:.2f}): the uncertainty "
                                             "model does not fit this operating regime")
                            self.state["twin_validations"] = int(self.state.get("twin_validations", 0)) + 1
                except Exception as e:  # report, do not hide
                    twin_note = f"twin replay failed: {e!r}"[:200]
            step("validate_via_twin", "ok" if twin_detail else ("skipped" if twin_note == "not required" else "error"),
                 twin_note, **twin_detail)

            # 6 optimisation: benefit - cost - risk per candidate action
            sev = {s["kind"]: SEVERITY_ORDER[s["severity"]] for s in rep.get("signals", [])}
            cand = {"NO_ACTION": 0.0}
            hp = {h["id"]: h["plausibility"] for h in hyps}
            if "MODEL_MISCALIBRATION" in hp or "ALARM_MISCALIBRATION" in hp:
                cand["SELF_CALIBRATE_UNCERTAINTY"] = 4.0 * max(sev.get("calibration", 0), sev.get("prediction", 0)) * \
                    max(hp.get("MODEL_MISCALIBRATION", 0), hp.get("ALARM_MISCALIBRATION", 0), 0.5)
            if "INSTRUMENT_NOISE_INCREASE" in hp or "INSTRUMENT_DRIFT" in hp:
                cand["REQUEST_INSTRUMENT_CHECK"] = 6.0 * max(sev.get("calibration", 0), sev.get("sensor", 0)) * \
                    max(hp.get("INSTRUMENT_NOISE_INCREASE", 0), hp.get("INSTRUMENT_DRIFT", 0))
            if "SMALL_PHYSICAL_LOSS" in hp or "PROCESS_FAULT_OR_BIAS" in hp:
                cand["OPEN_INVESTIGATION"] = 7.0 * max(sev.get("sensor", 0), sev.get("calibration", 0)) * \
                    max(hp.get("SMALL_PHYSICAL_LOSS", 0), hp.get("PROCESS_FAULT_OR_BIAS", 0))
            if "DATA_QUALITY" in hp:
                cand["FIX_DATA_PIPELINE"] = 5.0 * sev.get("data_gate", 0)
            if "REGIME_CHANGE" in hp:
                cand["COLLECT_REGIME_DATA"] = 4.0 * max(sev.get("data", 0), sev.get("regime", 0))
            gate = self._retrain_gate(rep)
            if gate["status"] == "RECOMMENDED":
                cand["RECOMMEND_RETRAIN"] = 14.0
            ranked = sorted(({"action": a, "benefit": round(b, 2), "cost": ACTIONS[a][0], "risk": ACTIONS[a][1],
                              "net": round(b - ACTIONS[a][0] - ACTIONS[a][1], 2)} for a, b in cand.items()),
                            key=lambda x: -x["net"])
            chosen = [r for r in ranked if r["net"] > 0] or [r for r in ranked if r["action"] == "NO_ACTION"]
            step("run_optimisation", "ok", "selected: " + ", ".join(r["action"] for r in chosen), ranked=ranked)

            # 7 recommendation
            recs = [self._explain(r["action"], behaviours, hyps, gate) for r in chosen]
            step("generate_recommendation", "ok", recs[0] if recs else "no action", recommendations=recs)

            # 8 self-calibration (bounded; outside the auto band it is proposed, not applied)
            applied = []
            if any(r["action"] == "SELF_CALIBRATE_UNCERTAINTY" for r in chosen):
                for b in behaviours:
                    if b["signal"] == "calibration" and b.get("row_kind") and "zero mean" in b["pattern"]:
                        k = b["row_kind"]
                        old = float(self.state["kind_scale"].get(k, 1.0))
                        rel = float(b["sd_scale"] or 1.0)
                        new = round(min(max(old * rel, HARD_BOUND[0]), HARD_BOUND[1]), 3)
                        inside = AUTO_BAND[0] <= new / old <= AUTO_BAND[1]
                        a = self._log("UNCERTAINTY_SCALE", k, old, new, "APPLIED" if inside else "PROPOSED",
                                      f"{k} residuals on normal windows show sd x{rel:.2f} with zero mean; "
                                      + ("re-scaled automatically (inside the safe band)" if inside else
                                         "change exceeds the automatic band - engineer approval required"))
                        applied.append(a)
                        if inside:
                            self.drift.reset()
                            self.state["streaks"]["drift_after_calibration"] = 0
            step("self_calibrate", "ok" if applied else "skipped",
                 "; ".join(f"{a['target']}: {a['old']} -> {a['new']} ({a['status']})" for a in applied)
                 or "no calibration change needed", adaptations=[a["id"] for a in applied],
                 kind_scale=dict(self.state["kind_scale"]), meter_factors=dict(self.state["meter_factors"]))

            # 9 experience
            mem = self.learn_from_experience()
            shifted = [f for f in mem["families"] if abs(f["shift"] - 1.0) > 0.05]
            step("learn_from_experience", "ok",
                 f"{mem['verified_cases']} verified case(s); " + (", ".join(
                     f"{f['family']} prior x{f['shift']}" for f in shifted[:4]) if shifted else "priors at engineering "
                                                                                                "defaults"),
                 verified_cases=mem["verified_cases"])

            # 10 retrain gate
            step("retrain_if_necessary", "alert" if gate["status"] == "RECOMMENDED" else "ok",
                 f"{gate['status']}: {gate['reason']}", gate=gate)

            cycle = {"id": f"AC-{uuid.uuid4().hex[:6].upper()}", "trigger": trigger, "at": _now(), "data_time": now,
                     "seconds": round(_now() - t_start, 2), "drift_overall": rep.get("overall"),
                     "steps": [{"key": k, "label": lbl, **steps.get(k, {"status": "skipped", "summary": ""})}
                               for k, lbl in STEPS]}
            self.state["cycles"].insert(0, cycle)
            del self.state["cycles"][20:]
            self._save()
            return cycle

    def _retrain_gate(self, rep: dict[str, Any]) -> dict[str, Any]:
        st = self.state["streaks"]
        sev = {s["kind"]: s["severity"] for s in rep.get("signals", [])}
        if sev.get("regime") == "RED" or sev.get("data") == "RED":
            st["regime_red"] = st.get("regime_red", 0) + 1
        else:
            st["regime_red"] = 0
        if sev.get("calibration") in ("AMBER", "RED") and self.state["kind_scale"]:
            st["drift_after_calibration"] = st.get("drift_after_calibration", 0) + 1
        elif sev.get("calibration") == "GREEN":
            st["drift_after_calibration"] = 0
        persistent = st["regime_red"] >= PERSIST_CYCLES or st["drift_after_calibration"] >= PERSIST_CYCLES
        # retraining needs data for the new regime: labelled plant cases or simulator-generated scenarios
        data_ok = self.memory.n_cases >= 10 or self.synthetic_data_available
        if persistent and not data_ok:
            return {"status": "WATCH", "reason": "drift persisted but fewer than 10 verified cases exist to train on; "
                    "keep adapting and collect verified cases", "streaks": dict(st)}
        if persistent:
            return {"status": "RECOMMENDED", "reason": "drift persisted for "
                    f"{max(st['regime_red'], st['drift_after_calibration'])} cycles after adaptation; train a challenger "
                    "on regenerated data and promote only if it beats the champion on held-out runs (engineer review)",
                    "streaks": dict(st)}
        if st["regime_red"] or st["drift_after_calibration"]:
            return {"status": "WATCH", "reason": "drift present but adaptation has not been exhausted yet",
                    "streaks": dict(st)}
        return {"status": "NOT_NEEDED", "reason": "adaptation keeps the models within specification",
                "streaks": dict(st)}

    @staticmethod
    def _explain(action: str, behaviours, hyps, gate) -> str:
        return {
            "NO_ACTION": "No adaptation needed - models are operating within their calibrated envelope.",
            "SELF_CALIBRATE_UNCERTAINTY": "Re-scale the uncertainty model for the affected balance rows "
                                          "(bounded, reversible, logged) instead of retraining.",
            "REQUEST_INSTRUMENT_CHECK": "Instruments behave worse than specified - schedule calibration / maintenance "
                                        "of the gauges and meters feeding the affected rows.",
            "OPEN_INVESTIGATION": "A persistent mean shift is a fault signature, not noise - open an investigation "
                                  "(self-calibration would hide it).",
            "FIX_DATA_PIPELINE": "Restore the instruments / connectors failing the data gate before trusting balances.",
            "COLLECT_REGIME_DATA": "Operation moved outside the training envelope - collect labelled data / simulate "
                                   "scenarios for the new regime; keep physics checks as the primary guard.",
            "RECOMMEND_RETRAIN": "Drift persisted after adaptation - " + gate.get("reason", ""),
        }[action]

    # ------------------------------------------------------------ status
    def status(self) -> dict[str, Any]:
        with self.lock:
            rep = self.drift.report()
            return {"drift": rep, "last_cycle": (self.state["cycles"] or [None])[0],
                    "cycles": [{k: c[k] for k in ("id", "trigger", "at", "drift_overall", "seconds")}
                               for c in self.state["cycles"][:10]],
                    "kind_scale": dict(self.state["kind_scale"]), "meter_factors": dict(self.state["meter_factors"]),
                    "adaptations": self.state["adaptations"][:50], "experience": self.memory.summary(),
                    "retrain_gate": self._peek_gate(), "twin_validations": self.state.get("twin_validations", 0),
                    "steps": [{"key": k, "label": lbl} for k, lbl in STEPS],
                    "architecture": [{"layer": n, "name": nm, "implementation": impl} for n, nm, impl in ARCHITECTURE],
                    "bounds": {"auto_band": AUTO_BAND, "hard_bound": HARD_BOUND, "persist_cycles": PERSIST_CYCLES}}

    def _peek_gate(self) -> dict[str, Any]:
        st = self.state["streaks"]
        if st.get("regime_red", 0) >= PERSIST_CYCLES or st.get("drift_after_calibration", 0) >= PERSIST_CYCLES:
            return {"status": "RECOMMENDED", "streaks": dict(st)}
        return {"status": "WATCH" if any(st.values()) else "NOT_NEEDED", "streaks": dict(st)}
