"""Demo scenario runner: start a known-fault scenario live and score the platform against it.

The runner knows what it injected (from the scenario catalogue), so it can show, step by step, whether
the platform detected the fault, how fast, what it diagnosed, and whether field checks confirmed it.
The diagnostic pipeline itself never sees the catalogue or the simulator's ground truth.
"""
from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select

from app.core.config import REPO_ROOT
from app.core.db import SessionLocal
from app.models import orm
from app.services.db_backend import jsonable
from simulation.scenarios.demo import BY_KEY, DEMO_SCENARIOS, build_demo_scenario, inject_s

log = logging.getLogger("oilintel.demo")


def _epoch(ts: str | None) -> float | None:
    if not ts:
        return None
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()


class DemoManager:
    def __init__(self, runtime, history_path: str | Path | None = None):
        self.rt = runtime
        self.path = Path(history_path or REPO_ROOT / "data" / "demo_history.json")
        self.session: dict[str, Any] | None = None
        self.lock = threading.RLock()
        self.history: list[dict[str, Any]] = []
        try:
            if self.path.exists():
                self.history = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            log.exception("demo history unreadable")

    # ------------------------------------------------------------ catalogue
    @staticmethod
    def catalogue() -> list[dict[str, Any]]:
        return [dict(s, inject_s=inject_s(s["key"]), duration_s=8 * 3600.0) for s in DEMO_SCENARIOS]

    # ------------------------------------------------------------ lifecycle
    def start(self, key: str, speed: float, user: str) -> dict[str, Any]:
        if key not in BY_KEY:
            raise KeyError(key)
        rt = self.rt
        if rt.sim is None:
            raise RuntimeError("simulation is not available in PLANT mode")
        with self.lock:
            self._archive("superseded by a new demo")
            sc = build_demo_scenario(rt.plant, key)
            rt.sim.start(sc, speed, not_before=rt.sim_clock_floor())
            st = rt.sim.status()
            start_epoch = (_epoch(st.get("sim_timestamp")) or time.time()) - float(st.get("sim_time_s") or 0.0)
            inj = inject_s(key)
            self.session = {"id": f"DEMO-{uuid.uuid4().hex[:6].upper()}", "key": key, "title": BY_KEY[key]["title"],
                            "category": BY_KEY[key]["category"], "expected": BY_KEY[key]["expected"],
                            "run_id": st.get("run_id"), "speed": speed, "started_by": user, "started_at": time.time(),
                            "start_epoch": start_epoch, "inject_s": inj,
                            "inject_epoch": start_epoch + inj if inj is not None else None,
                            "duration_s": sc.duration_s, "fault": [f.model_dump(include={"fault_type", "target", "params"})
                                                                   for f in sc.faults]}
            return self.status()

    def finish(self) -> dict[str, Any] | None:
        with self.lock:
            snap = self._archive("finished by the presenter")
            self.session = None
            return snap

    def _archive(self, reason: str) -> dict[str, Any] | None:
        if not self.session:
            return None
        try:
            snap = self.status()
        except Exception:
            log.exception("demo status failed while archiving")
            snap = {"session": self.session}
        snap["archived_reason"] = reason
        snap["archived_at"] = time.time()
        self.history.insert(0, jsonable(snap))
        del self.history[50:]
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.history, indent=1, default=str), encoding="utf-8")
        except Exception:
            log.exception("could not write demo history")
        return snap

    # ------------------------------------------------------------ status
    def _events(self, s: dict[str, Any]) -> list[orm.AnomalyEvent]:
        with SessionLocal() as db:
            rows = db.execute(select(orm.AnomalyEvent).where(orm.AnomalyEvent.source == "SIMULATION",
                                                             orm.AnomalyEvent.t1 >= s["start_epoch"])
                              .order_by(orm.AnomalyEvent.t0)).scalars().all()
            db.expunge_all()
            return rows

    def _first_abnormal(self, t0: float, t1: float) -> float | None:
        with SessionLocal() as db:
            v = db.execute(select(orm.BalanceResult.t1_epoch).where(
                orm.BalanceResult.source == "SIMULATION", orm.BalanceResult.anomaly.is_(True),
                orm.BalanceResult.t1_epoch >= t0, orm.BalanceResult.t1_epoch <= t1)
                .order_by(orm.BalanceResult.t1_epoch).limit(1)).scalar()
        return float(v) if v is not None else None

    def _investigation(self, inv_id: str | None) -> dict[str, Any] | None:
        if not inv_id:
            return None
        with SessionLocal() as db:
            r = db.get(orm.Investigation, inv_id)
            if r is None:
                return None
            rep = r.report or {}
            st = r.state or {}
            post = st.get("posterior") or {}
            cands = [{"hypothesis_id": h["hypothesis_id"], "label": h.get("label"),
                      "p": round(float(post.get(h["hypothesis_id"], h.get("calibrated_probability", 0.0))), 4)}
                     for h in rep.get("candidate_hypotheses", [])[:5]]
            top = max(post.items(), key=lambda kv: kv[1]) if post else (None, 0.0)
            return {"investigation_id": r.investigation_id, "status": r.status, "top": top[0], "top_p": round(top[1], 4),
                    "candidates": cands, "recommended_check": st.get("recommended_check"),
                    "verifications": st.get("verifications", []),
                    "estimated_loss_t": rep.get("estimated_physical_loss_tonnes"),
                    "loss_interval_t": rep.get("physical_loss_interval_tonnes"),
                    "observed_residual_t": rep.get("observed_residual_tonnes"),
                    "twin_best": (st.get("twin_validation") or {}).get("best_supported")}

    def status(self) -> dict[str, Any]:
        s = self.session
        if s is None:
            return {"active": False}
        sim = self.rt.sim.status() if self.rt.sim else {}
        same_run = sim.get("run_id") == s["run_id"]
        t_sim = float(sim.get("sim_time_s") or 0.0) if same_run else None
        ended = (not same_run or sim.get("state") in ("FINISHED", "STOPPED")
                 or (t_sim or 0.0) >= s["duration_s"] - 120)
        exp = s["expected"]
        events = self._events(s)
        inj_ep = s["inject_epoch"]
        win = float(getattr(self.rt.settings, "monitor_window_s", 1800.0))

        def seen(e) -> float:              # when the monitor first raised the event (it later extends t1)
            return e.t0 + win

        before = [e for e in events if inj_ep is None or seen(e) < inj_ep]
        after = [e for e in events if inj_ep is not None and seen(e) >= inj_ep]
        det = after[0] if after else None
        det_t = seen(det) if det is not None else None
        merged = False
        if det is None and inj_ep is not None:
            # an event raised before the fault and still open absorbs the fault's alarms (de-duplication):
            # the fault was detected at its first abnormal balance window
            carried = [e for e in before if e.t1 >= inj_ep]
            first = self._first_abnormal(inj_ep, carried[0].t1) if carried else None
            if first is not None:
                det, det_t, merged = carried[0], first, True
        inv = None
        if det is not None:
            inv = self._investigation(det.investigation_id)
        elif exp["kind"] == "normal" and events:
            inv = self._investigation(events[0].investigation_id)
        truth = None
        if same_run:
            try:
                snap = self.rt.sim.truth_snapshot() or {}
                truth = {"physical_loss_cum_t": snap.get("physical_loss_cum_t"), "leaks_t_h": snap.get("leaks"),
                         "active_faults": len(snap.get("active_faults") or [])}
            except Exception:
                truth = None
        cand_ids = [c["hypothesis_id"] for c in (inv or {}).get("candidates", [])]
        rank = cand_ids.index(exp["hypothesis"]) + 1 if exp["hypothesis"] in cand_ids else None
        steps: list[dict[str, Any]] = []
        lat: float | None = None

        def step(key, label, state, detail=""):
            steps.append({"key": key, "label": label, "state": state, "detail": detail})

        fault_txt = ", ".join(f"{f['fault_type'].replace('_', ' ').lower()} on {f['target']}" for f in s["fault"]) or "none"
        if exp["kind"] == "normal":
            step("baseline", "Normal operation running", "done" if t_sim and t_sim > 1800 else "active",
                 "transfers, idle period and a route change, no fault")
            n = len(events)
            step("no_alarm", "No false alarm", "failed" if n else ("done" if t_sim and t_sim >= 6 * 3600 else "active"),
                 f"{n} anomaly event(s) so far" if n else "balance stays inside its uncertainty band")
            if inv:
                ok = inv["top"] in (None, "NORMAL")
                step("investigation", "Investigation of the event", "done" if ok else "failed",
                     f"concluded {inv['top']} ({inv['status'].replace('_', ' ').lower()})")
            # any anomaly event in a fault-free run is a false alarm, whatever its investigation concluded
            verdict = "FAIL" if n else ("PASS" if (t_sim and t_sim >= 6 * 3600) or ended else "RUNNING")
        else:
            injected = t_sim is not None and t_sim >= s["inject_s"]
            step("baseline", "Normal baseline", "done" if injected else "active",
                 f"{s['inject_s'] / 3600:.2f} h of normal operation first")
            step("fault", "Fault injected", "done" if injected else "pending", fault_txt)
            lat = round((det_t - inj_ep) / 60.0, 1) if det is not None else None
            step("detect", "Anomaly detected", "done" if det else ("failed" if ended else "active" if injected else "pending"),
                 (f"{lat} min after the fault (data time), event {det.event_id}"
                  + (" (merged into an event opened before the fault)" if merged else "")) if det else
                 ("watching the balance every 5 simulated minutes" if injected else ""))
            step("investigate", "Investigation opened", "done" if inv else ("active" if det else "pending"),
                 f"{inv['investigation_id']} · {inv['status'].replace('_', ' ').lower()}" if inv else "")
            if exp["kind"] == "unknown":
                known = [c for c in (inv or {}).get("candidates", []) if c["hypothesis_id"] not in ("UNKNOWN", "NORMAL")]
                top_known = max((c["p"] for c in known), default=0.0)
                abstained = bool(inv) and (inv["top"] == "UNKNOWN" or top_known < 0.6)
                step("diagnose", "Abstains (no confident wrong cause)",
                     "done" if abstained else ("failed" if ended else "active" if inv else "pending"),
                     f"top {inv['top']} {inv['top_p']:.0%}; best known cause {top_known:.0%}" if inv else "")
                verdict = "PASS" if abstained else ("FAIL" if ended else "RUNNING")
            else:
                top_right = bool(inv) and inv["top"] == exp["hypothesis"]
                diag_state = "done" if rank and rank <= 3 else ("failed" if ended else "active" if inv else "pending")
                step("diagnose", "True cause in the top 3", diag_state,
                     f"ranked #{rank} of the candidate causes" if rank else
                     ("collecting more evidence (the case re-analyses as data arrives)" if inv else ""))
                confirmed = top_right and inv["status"] in ("RESOLVED_PENDING_APPROVAL", "CLOSED")
                step("verify", "Confirmed by field checks", "done" if confirmed else ("active" if rank else "pending"),
                     f"{inv['top']} at {inv['top_p']:.0%} after {len(inv['verifications'])} check(s)" if inv and inv["verifications"]
                     else "press 'Run field checks' to let the engineer's checks decide")
                # same pass rule as experiments/demo_check.py: confirmed, or the true cause is the top answer
                verdict = "PASS" if confirmed or (rank == 1 and top_right) else ("FAIL" if ended else "RUNNING")
        return jsonable({"active": True, "session": s, "same_run": same_run, "sim_time_s": t_sim, "ended": ended,
                         "sim_state": sim.get("state"), "progress": (t_sim / s["duration_s"]) if t_sim else 0.0,
                         "events": [{"event_id": e.event_id, "detected_t": seen(e), "t1": e.t1, "severity": e.severity, "assets": e.assets,
                                     "summary": e.summary} for e in events][-6:],
                         "false_alarms": len(before) if exp["kind"] != "normal" else len(events),
                         "investigation": inv, "expected_rank": rank, "truth": truth, "steps": steps, "latency_min": lat,
                         "verdict": verdict, "talk": BY_KEY[s["key"]]["talk"], "merged_detection": merged})

    # ------------------------------------------------------------ presenter actions
    def investigation_id(self) -> str | None:
        st = self.status()
        return ((st.get("investigation") or {}).get("investigation_id")) if st.get("active") else None
