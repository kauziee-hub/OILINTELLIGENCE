"""Telemetry & record ingestion.

Bus subscriber -> (1) in-memory ring buffer + latest-value cache used by the live
twin and the monitoring loop, (2) batched append-only inserts into the telemetry
table (duplicate event_ids ignored), (3) operational records upserts,
(4) simulator ground truth into the separate truth database (simulation mode only).
"""
from __future__ import annotations

import logging
import threading
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any

import pandas as pd
from sqlalchemy import insert as sa_insert, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.core.db import SessionLocal, TruthSession, engine, truth_engine
from app.models import orm
from industrial.mqtt import topics as T

log = logging.getLogger("oilintel.ingestion")


def _parse_ts(v) -> datetime:
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _insert(eng, table):
    if eng.dialect.name == "sqlite":
        return sqlite_insert(table).on_conflict_do_nothing()
    if eng.dialect.name == "postgresql":
        return pg_insert(table).on_conflict_do_nothing()
    return sa_insert(table)


class TelemetryBuffer:
    """Thread-safe ring buffer of recent telemetry plus latest value per (sensor, parameter)."""

    def __init__(self, hours: float = 14.0):
        self.max_age_s = hours * 3600
        self._rows: deque[tuple] = deque()
        self.latest: dict[tuple[str, str], dict[str, Any]] = {}
        self._lock = threading.Lock()
        self.received = 0
        self.duplicates = 0
        self._seen: deque[str] = deque(maxlen=50000)
        self._seen_set: set[str] = set()
        self.last_event_ts: float | None = None
        self.last_ingest_wall: float | None = None
        self.max_delay_s = 0.0
        # start of the current simulation run: stored telemetry before it belongs to another run
        # (different initial state), so no balance may span it. None = continuous (plant) data.
        self.epoch: float | None = None

    def add(self, p: dict[str, Any]) -> bool:
        eid = p.get("event_id")
        with self._lock:
            if eid in self._seen_set:
                self.duplicates += 1
                return False
            if len(self._seen) == self._seen.maxlen:
                self._seen_set.discard(self._seen[0])
            self._seen.append(eid)
            self._seen_set.add(eid)
            ts = _parse_ts(p["event_timestamp"]).timestamp()
            self._rows.append((ts, p["sensor_id"], p["asset_id"], p["parameter"], p.get("value"), p.get("unit", ""),
                               p.get("quality", "GOOD"), p.get("source", "SIMULATION")))
            self.latest[(p["sensor_id"], p["parameter"])] = {**p, "ts": ts}
            self.received += 1
            self.last_event_ts = max(ts, self.last_event_ts or ts)
            self.last_ingest_wall = time.time()
            while self._rows and self._rows[0][0] < self.last_event_ts - self.max_age_s:
                self._rows.popleft()
        return True

    def frame(self, t0: float, t1: float, sensor_ids: list[str] | None = None) -> pd.DataFrame:
        with self._lock:
            rows = [r for r in self._rows if t0 <= r[0] <= t1 and (sensor_ids is None or r[1] in sensor_ids)]
        df = pd.DataFrame(rows, columns=["ts", "sensor_id", "asset_id", "parameter", "value", "unit", "quality",
                                         "source"])
        if len(df):
            df["timestamp"] = pd.to_datetime(df["ts"], unit="s", utc=True)
        else:
            df["timestamp"] = pd.Series(dtype="datetime64[ns, UTC]")
        return df

    def coverage(self) -> tuple[float | None, float | None]:
        with self._lock:
            if not self._rows:
                return None, None
            return self._rows[0][0], self._rows[-1][0]

    def complete_ts(self, stale_s: float = 600.0) -> float | None:
        """Newest time at which every live sensor has reported.

        The readings of one timestamp arrive one message at a time. Evaluating at last_event_ts while a
        batch is half ingested pairs new totaliser readings of some meters with old readings of others: a
        phantom imbalance of one sample interval of flow (~2 m3 at 120 m3/h and 60 s sampling, z ~ 10).
        Sensors silent for more than stale_s (dropouts) do not hold the clock back.
        """
        with self._lock:
            if self.last_event_ts is None:
                return None
            live = [v["ts"] for v in self.latest.values() if v["ts"] >= self.last_event_ts - stale_s]
            return min(live) if live else self.last_event_ts

    def clear(self) -> None:
        with self._lock:
            self._rows.clear()
            self.latest.clear()
            self.last_event_ts = None


class Ingestion:
    def __init__(self, bus, buffer: TelemetryBuffer, data_mode: str, on_telemetry=None):
        self.bus = bus
        self.buffer = buffer
        self.data_mode = data_mode
        self.on_telemetry = on_telemetry
        self._pending: list[dict[str, Any]] = []
        self._truth_pending: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self.written = 0
        self.write_errors = 0
        self.records_written = 0
        self._thread = threading.Thread(target=self._writer, name="telemetry-writer", daemon=True)

    def start(self) -> None:
        topics = [f"{T.ROOT}/simulation/+/+/+"] if self.data_mode == "SIMULATION" else [f"{T.ROOT}/plant/+/+/+"]
        for t in topics:
            self.bus.subscribe(t, self._on_telemetry)
        self.bus.subscribe(f"{T.ROOT}/events/records/+", self._on_record)
        self.bus.subscribe(f"{T.ROOT}/events/runs", self._on_run)
        if self.data_mode == "SIMULATION":
            self.bus.subscribe(f"{T.TRUTH_ROOT}/state", self._on_truth)
            self.bus.subscribe(f"{T.TRUTH_ROOT}/faults", self._on_fault)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._flush()

    # ----------------------------------------------------------- handlers
    def _on_telemetry(self, topic: str, p: dict[str, Any]) -> None:
        if not self.buffer.add(p):
            return
        with self._lock:
            self._pending.append(p)
        if self.on_telemetry:
            self.on_telemetry(p)

    def _on_truth(self, topic: str, p: dict[str, Any]) -> None:
        with self._lock:
            self._truth_pending.append(p)

    def _on_fault(self, topic: str, p: dict[str, Any]) -> None:
        try:
            with TruthSession() as s:
                if p["event_type"] == "FAULT_INJECTED":
                    f = p["fault"]
                    s.merge(orm.FaultEvent(fault_id=f["fault_id"], run_id=p["run_id"], fault_type=f["fault_type"],
                                           affected_asset=f["target"], start_time=_parse_ts(p["timestamp"]),
                                           severity=f.get("severity", 0.5), fault_parameters=f.get("params", {})))
                elif p["event_type"] == "FAULT_CLEARED":
                    fe = s.get(orm.FaultEvent, p["fault_id"])
                    if fe:
                        fe.end_time = _parse_ts(p["timestamp"])
                s.commit()
        except Exception:
            log.exception("fault event write failed")

    def _on_run(self, topic: str, p: dict[str, Any]) -> None:
        try:
            with SessionLocal() as s:
                if p["event_type"] == "RUN_STARTED":
                    sc = p.get("scenario", {})
                    s.merge(orm.SimulationRun(run_id=p["run_id"], scenario_id=sc.get("scenario_id", ""),
                                              seed=sc.get("seed", 0), scenario=sc, status="RUNNING",
                                              speed=p.get("speed", 1.0)))
                    with TruthSession() as ts_:
                        for f in sc.get("faults", []):
                            ts_.merge(orm.FaultEvent(fault_id=f["fault_id"], run_id=p["run_id"],
                                                     fault_type=f["fault_type"], affected_asset=f["target"],
                                                     start_time=_parse_ts(sc["start_time"]), severity=f.get("severity", .5),
                                                     fault_parameters=f.get("params", {})))
                        ts_.commit()
                    self.buffer.clear()
                    if sc.get("start_time"):
                        self.buffer.epoch = _parse_ts(sc["start_time"]).timestamp()
                        self._close_previous_runs(s, p["run_id"], _parse_ts(sc["start_time"]))
                elif p["event_type"] == "RUN_STOPPED":
                    r = s.get(orm.SimulationRun, p["run_id"])
                    if r:
                        r.status = "STOPPED"
                        r.ended_at = datetime.now(timezone.utc)
                s.commit()
        except Exception:
            log.exception("run event write failed")

    @staticmethod
    def _close_previous_runs(s, run_id: str, start) -> None:
        """A new simulation run is a new plant state: what earlier runs left open can never progress.
        Their transfers stop at the run boundary, their events close, and their open investigations
        end (field checks on them would be answered by the NEW run's simulator)."""
        s.execute(update(orm.Transfer).where(orm.Transfer.source == "SIMULATION", orm.Transfer.status == "IN_PROGRESS",
                                             orm.Transfer.run_id != run_id).values(status="INTERRUPTED"))
        t = start.timestamp()
        s.execute(update(orm.AnomalyEvent).where(orm.AnomalyEvent.source == "SIMULATION", orm.AnomalyEvent.t1 < t,
                                                 orm.AnomalyEvent.status.in_(("OPEN", "INVESTIGATING")))
                  .values(status="CLOSED"))
        s.execute(update(orm.Investigation).where(
            orm.Investigation.source == "SIMULATION", orm.Investigation.t1 < t,
            orm.Investigation.status.in_(("RECEIVED", "COLLECTING_EVIDENCE", "NEEDS_DATA", "AWAITING_VERIFICATION",
                                          "AWAITING_REVIEW"))).values(status="RUN_ENDED"))

    def _on_record(self, topic: str, p: dict[str, Any]) -> None:
        kind = topic.rsplit("/", 1)[-1]
        r = p.get("record", {})
        run_id = p.get("run_id")
        src = "SIMULATION" if self.data_mode == "SIMULATION" else "PLANT"
        try:
            with SessionLocal() as s:
                if kind == "transfers":
                    old = s.get(orm.Transfer, r["transfer_id"])
                    if old is not None and old.run_id and run_id and old.run_id != run_id:
                        # demo scripts reuse transfer ids (D-1) in every run: keep the earlier run's record
                        s.execute(update(orm.Transfer).where(orm.Transfer.transfer_id == old.transfer_id)
                                  .values(transfer_id=f"{old.transfer_id}@{old.run_id}"[:32]))
                        s.flush()
                        s.expunge(old)
                    s.merge(orm.Transfer(
                        transfer_id=r["transfer_id"], route=r["route"], source_tank=r["source_tank"],
                        destination_tank=r["destination_tank"], start_time=_parse_ts(r["start_time"]),
                        end_time=_parse_ts(r["end_time"]) if r.get("end_time") else None,
                        recorded_quantity=r.get("recorded_quantity"), recorded_volume_m3=r.get("recorded_volume_m3"),
                        quantity_unit=r.get("quantity_unit", "t"), product_id=r.get("product_id", ""),
                        custody_meter=r.get("custody_meter", ""), status=r.get("status", ""),
                        opening_gauges=r.get("opening_gauges", {}), closing_gauges=r.get("closing_gauges", {}),
                        run_id=run_id, source=src))
                elif kind == "calibrations":
                    data = {k: v for k, v in r.items() if k not in ("record_id", "sensor_id", "record_type",
                                                                    "timestamp", "result")}
                    s.merge(orm.CalibrationRecord(record_id=r["record_id"], sensor_id=r["sensor_id"],
                                                  record_type=r["record_type"], timestamp=_parse_ts(r["timestamp"]),
                                                  result=r.get("result", ""), data=data, run_id=run_id))
                elif kind == "lab_samples":
                    s.merge(orm.LabSample(sample_id=r["sample_id"], tank_id=r["tank_id"],
                                          timestamp=_parse_ts(r["timestamp"]), density_15c=r["density_15c"],
                                          bsw_pct=r.get("bsw_pct", 0.0), method=r.get("method", ""), run_id=run_id))
                elif kind == "maintenance":
                    s.merge(orm.MaintenanceRecord(record_id=r["record_id"], asset_id=r["asset_id"],
                                                  record_type=r["record_type"], timestamp=_parse_ts(r["timestamp"]),
                                                  notes=r.get("notes", ""), run_id=run_id))
                elif kind == "operator_log":
                    s.merge(orm.OperatorLog(log_id=r["log_id"], timestamp=_parse_ts(r["timestamp"]),
                                            asset_id=r.get("asset_id"), category=r.get("category", "OPERATIONS"),
                                            text=r.get("text", ""), run_id=run_id))
                s.commit()
                self.records_written += 1
        except Exception:
            log.exception("record write failed (%s)", kind)

    # ----------------------------------------------------------- writer
    def _writer(self) -> None:
        while not self._stop.is_set():
            time.sleep(1.0)
            self._flush()

    def _flush(self) -> None:
        with self._lock:
            batch, self._pending = self._pending, []
            truth, self._truth_pending = self._truth_pending, []
        if batch:
            rows = []
            for p in batch:
                ts = _parse_ts(p["event_timestamp"])
                rows.append({"event_id": p["event_id"], "event_timestamp": ts, "ts_epoch": ts.timestamp(),
                             "ingestion_timestamp": _parse_ts(p.get("ingestion_timestamp") or datetime.now(timezone.utc)),
                             "asset_id": p["asset_id"], "sensor_id": p["sensor_id"], "parameter": p["parameter"],
                             "value": p.get("value"), "unit": p.get("unit", ""), "quality": p.get("quality", "GOOD"),
                             "source": p.get("source", "SIMULATION"), "source_tag": p.get("source_tag", ""),
                             "run_id": p.get("run_id"), "schema_version": p.get("schema_version", "1.0")})
            try:
                with engine.begin() as c:
                    for i in range(0, len(rows), 2000):
                        c.execute(_insert(engine, orm.Telemetry.__table__), rows[i:i + 2000])
                self.written += len(rows)
            except Exception:
                self.write_errors += 1
                log.exception("telemetry batch insert failed (%d rows)", len(rows))
        if truth:
            try:
                gt_rows, snaps = [], []
                for p in truth:
                    row = p["row"]
                    ts = _parse_ts(row["timestamp"])
                    snaps.append({"run_id": p["run_id"], "sim_t": row["t"], "timestamp": ts, "row": row})
                    faults = row.get("active_faults", [])
                    for tk in ("TK-001", "TK-002", "TK-003"):
                        f = next((x for x in faults if x.endswith("@" + tk)), None)
                        gt_rows.append({"run_id": p["run_id"], "timestamp": ts, "sim_t": row["t"], "asset_id": tk,
                                        "true_mass": row.get(f"{tk}.mass_t"), "true_flow": None,
                                        "true_sensor_bias": None,
                                        "true_physical_loss": row.get(f"{tk}.floor_leak_cum_t"),
                                        "true_fault_type": f.split("@")[0] if f else None,
                                        "true_fault_location": tk if f else None, "known_fault_state": bool(f)})
                    for pl in ("PL-001", "PL-002", "PL-003", "PL-004"):
                        f = next((x for x in faults if x.endswith("@" + pl)), None)
                        gt_rows.append({"run_id": p["run_id"], "timestamp": ts, "sim_t": row["t"], "asset_id": pl,
                                        "true_mass": None, "true_sensor_bias": None,
                                        "true_flow": row.get(f"{pl}.leak_rate_th"),
                                        "true_physical_loss": row.get(f"{pl}.leak_cum_t"),
                                        "true_fault_type": f.split("@")[0] if f else None,
                                        "true_fault_location": pl if f else None, "known_fault_state": bool(f)})
                with truth_engine.begin() as c:
                    c.execute(sa_insert(orm.TruthSnapshot.__table__), snaps)
                    c.execute(sa_insert(orm.GroundTruth.__table__), gt_rows)
            except Exception:
                log.exception("truth write failed")

    def status(self) -> dict[str, Any]:
        a, b = self.buffer.coverage()
        return {"received": self.buffer.received, "duplicates": self.buffer.duplicates, "written": self.written,
                "write_errors": self.write_errors, "pending": len(self._pending), "records_written": self.records_written,
                "buffer_from": a, "buffer_to": b, "last_event_ts": self.buffer.last_event_ts,
                "last_ingest_wall": self.buffer.last_ingest_wall}
