"""Continuous monitoring: rolling balance evaluation, anomaly events, auto-investigation.

Every `monitor_interval_s` of data time the latest `monitor_window_s` window is
reconciled (topology-aware balances + calibrated uncertainty + detectors). An
abnormal window creates (or updates) an anomaly event; if configured, the
investigation agent is started for it in a worker thread. Duplicate events for
the same assets and period attach to the open investigation instead of opening
a new one.
"""
from __future__ import annotations

import logging
import math
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select

from app.core.db import SessionLocal
from app.models import orm
from app.services.db_backend import jsonable
from industrial.mqtt import topics as T

log = logging.getLogger("oilintel.monitor")


def implicated_assets(plant, stats: dict[str, Any], quality: dict[str, Any]) -> list[str]:
    assets: set[str] = set()
    for key, z in stats.get("z", {}).items():
        if abs(z) < 3.0:
            continue
        if key.startswith("CV:"):
            for part in key[3:].split("+"):
                if part in plant.tanks:
                    assets.add(part)
                elif part == "MANIFOLD":
                    assets.update(["MF-SUC", "MF-DIS"])
                elif part.startswith("PL-"):
                    assets.add(part.split("[")[0])
        elif key.startswith(("LEDGER:", "TCONS:", "DCONS:")):
            assets.add(key.split(":", 1)[1])
        elif key.startswith("PUMP:"):
            assets.add(key.split(":", 1)[1])
    assets.update(quality.keys())
    return sorted(assets)


class Monitor:
    def __init__(self, runtime):
        self.rt = runtime
        self.s = runtime.settings
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="monitor", daemon=True)
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="investigator")
        self._adapt_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="adaptive")
        self._epoch = None
        self.last_eval_ts: float | None = None
        self.last_result: dict[str, Any] | None = None
        self.evaluations = 0
        self.errors = 0
        self.inflight: set[str] = set()
        self.refresh_after_s = 1800.0        # data seconds of new evidence before an open case is re-analysed
        from intelligence.alarm import AlarmOnDelay, SlowShiftCusum, instrument_memory_s
        span = self.s.alarm_min_span_s if self.s.alarm_min_span_s is not None else instrument_memory_s(runtime.plant)
        self.on_delay = AlarmOnDelay(self.s.alarm_confirmations, span)   # alarm confirmation (on-delay)
        self._on_delay_epoch = None
        self.cusum = SlowShiftCusum(window_s=self.s.monitor_window_s)  # slow faults: accumulated evidence
        self.suppressed = 0                  # abnormal windows that did not persist (no event raised)

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _loop(self):
        while not self._stop.is_set():
            time.sleep(1.0)
            try:
                now = self.rt.buffer.complete_ts()   # not last_event_ts: a half-ingested batch fakes an imbalance
                a, _ = self.rt.buffer.coverage()
                if now is None or a is None or now - a < self.s.monitor_window_s:
                    continue
                if self.last_eval_ts is not None and now - self.last_eval_ts < self.s.monitor_interval_s:
                    continue
                self.evaluate(now)
            except Exception:
                self.errors += 1
                log.exception("monitoring evaluation failed")

    def evaluate(self, now: float) -> dict[str, Any]:
        W = self.s.monitor_window_s
        rec = self.rt.backend.records(now - 30 * 86400, now)
        # a transfer completing in this window is reconciled against its ticket: load its whole movement
        tel = self.rt.buffer.frame(self.rt.engine.telemetry_start(rec, now - W, now), now + self.rt.engine.READ_AHEAD_S)
        ctx = self.rt.engine.context(tel, rec)
        edges = [now - W, now]
        ev = ctx.engine.evidence(edges)
        from intelligence.uncertainty.propagation import window_statistics
        stats = window_statistics(ev)[0]
        import numpy as np
        from intelligence.uncertainty.propagation import assemble
        A = assemble(ev)
        mask = np.array([r.kind == "CV" for r in A.rows], dtype=float)
        fac_sigma = float(np.sqrt(max(float(mask @ A.cov @ mask), 0.0))) if len(mask) else None
        import pandas as pd
        from intelligence.features.windows import window_features
        feats = pd.DataFrame(window_features(self.rt.plant, ev))
        scores = {"statistical_chi2": float(max(feats["phys_log_p"].iloc[0], feats["phys_max_abs_z_cv"].iloc[0] / 4 * 3))}
        alarms = {"statistical_chi2": scores["statistical_chi2"] >= 3.0}
        for name in ("isolation_forest", "xgboost_physics"):
            m = self.rt.engine.models.get(name)
            if m is not None:
                try:
                    scores[name] = float(m.score(feats)[0])
                    alarms[name] = bool(scores[name] >= m.threshold)
                except Exception:
                    pass
        win = ev.windows[0]
        quality = {k: v for k, v in win.quality.items() if v["stuck"] or v["dropout"]}
        if self.rt.buffer.epoch != self._on_delay_epoch:        # new simulation run: start afresh
            self._on_delay_epoch = self.rt.buffer.epoch
            self.on_delay.reset()
            self.cusum.reset()
        # slow faults (a drifting gauge) stay just under the per-window test; their evidence accumulates
        slow = self.cusum.update(stats["z"], now)
        alarms["cusum_slow_shift"] = bool(slow)
        anomaly = bool(alarms.get("statistical_chi2") or (alarms.get("xgboost_physics") and stats["max_abs_z"] > 2.5)
                       or quality or slow)
        # Alarm on-delay (ISA-18.2 practice). A persistent fault (leak, bias, passing valve) stays
        # abnormal, while a start-up transient or a slow gauge excursion drops out. Unless the evidence
        # is overwhelming or an instrument is stuck / dropped out, an event needs `alarm_confirmations`
        # successive abnormal evaluations spanning at least the instruments' error memory
        # (intelligence.alarm): windows 5 min apart share the same slow gauge error.
        # a CUSUM alarm already spans several independent windows: it needs no further on-delay
        immediate = bool(quality) or bool(slow) or stats["max_abs_z"] >= 6.0 or scores["statistical_chi2"] >= 8.0
        confirmed = self.on_delay.update(anomaly, immediate, now)
        self.last_eval_ts = now
        self.evaluations += 1
        result = {"t0": now - W, "t1": now, "route": win.route_guess, "facility_residual_t": stats["facility_residual_t"],
                  "facility_sigma_t": fac_sigma,
                  "chi2": stats["chi2"], "dof": stats["dof"], "p_value": stats["p_value"], "max_abs_z": stats["max_abs_z"],
                  "z": stats["z"], "residuals": stats["residuals"], "sigma": stats["sigma"], "scores": scores,
                  "alarms": alarms, "quality_issues": quality, "anomaly": anomaly, "alarm_confirmed": confirmed,
                  "slow_shift_rows": slow, "cusum": self.cusum.statistic(),
                  "throughput_t": float(feats["ctx_throughput_t"].iloc[0])}
        self.last_result = result
        self._feed_adaptive(result, feats, now)
        with SessionLocal() as s:
            s.add(orm.BalanceResult(id=str(uuid.uuid4()), t0=datetime.fromtimestamp(now - W, tz=timezone.utc),
                                    t1=datetime.fromtimestamp(now, tz=timezone.utc), t1_epoch=now, window_s=W,
                                    route=win.route_guess, facility_residual_t=stats["facility_residual_t"],
                                    chi2=stats["chi2"], dof=stats["dof"], p_value=stats["p_value"],
                                    max_abs_z=stats["max_abs_z"], anomaly=anomaly, scores=jsonable(scores),
                                    rows=jsonable({"z": stats["z"], "residuals": stats["residuals"],
                                                   "sigma": stats["sigma"], "facility_sigma_t": fac_sigma,
                                                   "quality_issues": list(quality)}),
                                    source=self.rt.source))
            s.commit()
        self.rt.broadcast({"type": "balance", "data": jsonable(result)})
        if confirmed:
            self._raise_event(result, quality)
        elif anomaly:
            self.suppressed += 1
        return result

    def _feed_adaptive(self, result: dict[str, Any], feats, now: float) -> None:
        ad = self.rt.adaptive
        if ad is None:
            return
        if self.rt.buffer.epoch != self._epoch:        # new simulation run: new plant state
            self._epoch = self.rt.buffer.epoch
            ad.reset_run()
        ad.observe(result, feats.iloc[0].to_dict())
        if self.evaluations % max(self.s.adaptive_every, 1) == 0:
            self._adapt_pool.submit(self._adapt, now)

    def _adapt(self, now: float) -> None:
        try:
            cyc = self.rt.adaptive.run_cycle("scheduled", now)
            self.rt.broadcast({"type": "adaptive", "data": {"id": cyc["id"], "drift_overall": cyc["drift_overall"]}})
        except Exception:
            log.exception("adaptive cycle failed")

    def _raise_event(self, result: dict[str, Any], quality: dict[str, Any]) -> None:
        plant = self.rt.plant
        # a balance anomaly is attributed to the abnormal balance rows; an episode driven only by
        # instrument data quality is a separate event type, so it can never absorb a later process fault
        balance_assets = implicated_assets(plant, result, {})
        if result.get("slow_shift_rows"):         # CUSUM rows: small per-window z, large accumulated evidence
            balance_assets = sorted(set(balance_assets) | set(
                implicated_assets(plant, {"z": {r: 9.0 for r in result["slow_shift_rows"]}}, {})))
        if balance_assets or result["scores"]["statistical_chi2"] >= 3.0:
            etype, assets = "BALANCE_ANOMALY", balance_assets or ["TF-01"]
        else:
            etype, assets = "DATA_QUALITY", sorted(quality) or ["TF-01"]
        severity = "CRITICAL" if result["max_abs_z"] > 8 or result["scores"]["statistical_chi2"] > 10 else "WARNING"
        with SessionLocal() as s:
            open_ev = s.execute(select(orm.AnomalyEvent).where(orm.AnomalyEvent.status.in_(["OPEN", "INVESTIGATING"]),
                                                               orm.AnomalyEvent.event_type == etype,
                                                               orm.AnomalyEvent.source == self.rt.source)).scalars().all()
            epoch = self.rt.buffer.epoch
            for ev in open_ev:
                if epoch is not None and (ev.t0 < epoch or ev.t1 < epoch):
                    continue        # began in an earlier simulation run: a different plant state
                # same episode: shared assets, or still alarming within 90 min of the open event
                # (implicated assets legitimately shift as evidence accumulates)
                related = bool(set(ev.assets or []) & set(assets)) or result["t0"] - ev.t1 < 5400
                if related and result["t0"] - ev.t1 < 3 * 3600:
                    ev.t1 = result["t1"]
                    ev.scores = jsonable(result["scores"])
                    ev.assets = sorted(set(ev.assets or []) | set(assets))
                    s.commit()
                    if ev.investigation_id and self.s.auto_investigate:
                        inv = s.get(orm.Investigation, ev.investigation_id)
                        if inv is not None and inv.status in ("AWAITING_VERIFICATION", "AWAITING_REVIEW", "NEEDS_DATA")                                 and result["t1"] - (inv.t1 or 0) >= self.refresh_after_s:
                            self.refresh_investigation(ev.investigation_id, result["t1"])
                    return
            eid = f"EVT-{uuid.uuid4().hex[:6].upper()}"
            top_rows = sorted(result["z"].items(), key=lambda kv: -abs(kv[1]))[:3]
            summary = "; ".join(f"{k} z={v:+.1f}" for k, v in top_rows)
            if result.get("slow_shift_rows"):
                summary = "slow shift (CUSUM) on " + ", ".join(result["slow_shift_rows"]) + "; " + summary
            if quality:
                summary += "; data quality: " + ", ".join(quality)
            ev = orm.AnomalyEvent(event_id=eid, t0=result["t0"], t1=result["t1"], event_type=etype,
                                  severity=severity, assets=assets, summary=summary, scores=jsonable(result["scores"]),
                                  source=self.rt.source)
            s.add(ev)
            s.commit()
        payload = {"event_id": eid, "event_type": etype, "severity": severity, "assets": assets,
                   "t0": result["t0"], "t1": result["t1"], "summary": summary}
        self.rt.bus.publish(T.EVENTS_ANOMALIES, payload)
        self.rt.broadcast({"type": "anomaly", "data": payload})
        if self.s.auto_investigate:
            self.launch_investigation(payload)

    def launch_investigation(self, event: dict[str, Any]):
        key = event["event_id"]
        if key in self.inflight:
            return None
        self.inflight.add(key)

        def run():
            try:
                st = self.rt.investigator().investigate(event)
                self.rt.broadcast({"type": "investigation", "data": {"investigation_id": st.get("investigation_id"),
                                                                     "status": st.get("status")}})
                if st.get("status") not in ("DUPLICATE", "REJECTED"):
                    pass
                return st
            except Exception:
                log.exception("investigation failed for %s", key)
            finally:
                self.inflight.discard(key)

        return self._pool.submit(run)

    def refresh_investigation(self, inv_id: str, t1: float):
        key = f"refresh:{inv_id}"
        if key in self.inflight:
            return None
        self.inflight.add(key)

        def run():
            try:
                st = self.rt.investigator().refresh(inv_id, t1)
                self.rt.broadcast({"type": "investigation", "data": {"investigation_id": inv_id,
                                                                     "status": st.get("status"), "refreshed": True}})
            except Exception:
                log.exception("refresh failed for %s", inv_id)
            finally:
                self.inflight.discard(key)

        return self._pool.submit(run)

    def status(self) -> dict[str, Any]:
        return {"evaluations": self.evaluations, "errors": self.errors, "last_eval_ts": self.last_eval_ts,
                "interval_s": self.s.monitor_interval_s, "window_s": self.s.monitor_window_s,
                "alarm_confirmations": self.s.alarm_confirmations, "alarm_min_span_s": self.on_delay.min_span_s, "unconfirmed_abnormal_windows": self.suppressed,
                "auto_investigate": self.s.auto_investigate, "investigations_in_flight": len(self.inflight)}
