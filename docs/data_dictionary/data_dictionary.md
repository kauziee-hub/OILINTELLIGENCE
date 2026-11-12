# Data Dictionary

## Instrument tags

Defined in [`config/plant.yaml`](../../config/plant.yaml). The MQTT and OPC UA tag of every
sensor is `<asset_type>/<asset_id>/<parameter>`.

| Tag | Asset | Parameter | Unit | Range | Accuracy (1σ) | Noise (1σ) | Period |
|---|---|---|---|---|---|---|---|
| LT-001 / LT-002 / LT-003 | TK-001 / TK-002 / TK-003 | level | m | 0–17 | 3 mm | 1.2 mm + AR(1) 1 mm | 1 s |
| TT-001 / TT-002 / TT-003 | TK-00x | temperature | °C | −20–90 | 0.2 | 0.04 | 2 s |
| TT-004 | PL-001 (at 0.93) | temperature | °C | −20–90 | 0.2 | 0.05 | 2 s |
| TT-005 | MF-DIS | temperature | °C | −20–90 | 0.2 | 0.05 | 2 s |
| TT-006 | TF-01 (site) | ambient_temperature | °C | −30–60 | 0.3 | 0.08 | 2 s |
| PT-001 | PL-001 (at 0.97) | pressure | bar g | −1–16 | 0.02 | 0.006 | 1 s |
| PT-002 / PT-003 | PL-002 / PL-003 (at 0.05) | pressure | bar g | −1–16 | 0.02 | 0.006 | 1 s |
| PT-004 | PL-004 (at 0.95) | pressure | bar g | −1–16 | 0.02 | 0.006 | 1 s |
| FM-001 | PL-001 (at 0.92) | volume_flow (+ volume_total) | m³/h (m³) | 0–400 | 0.25% | 0.15% + 0.05 | 1 s |
| FM-002 | PL-002 (at 0.95) | volume_flow (+ volume_total) | m³/h (m³) | 0–400 | 0.25% | 0.15% + 0.05 | 1 s |
| FM-003 | PL-003 (at 0.95) | volume_flow (+ volume_total) | m³/h (m³) | 0–300 | 0.25% | 0.15% + 0.05 | 1 s |
| DT-001 | PL-001 (at 0.90) | density_15c | kg/m³ | 700–1000 | 0.5 | 0.15 | 5 s |
| DT-002 | MF-DIS | density_15c | kg/m³ | 700–1000 | 0.5 | 0.15 | 5 s |
| PU-00x-ST | PU-00x | status | state | 0/1 | — | — | on change, ≤ 10 s |
| PU-00x-SP | PU-00x | speed | rpm | 0–3600 | 5 | 2 | 2 s |
| PU-00x-PW | PU-00x | power | kW | 0–200 | 1% | 0.4% + 0.05 | 2 s |
| PU-00x-PD | PU-00x | discharge_pressure | bar g | −1–16 | 0.02 | 0.006 | 1 s |
| XV-00x-ZT | XV-00x | position | % | 0–100 | — | — | on change, ≤ 5 s |
| XV-00x-ZC | XV-00x | command | state | 0/1 | — | — | on change, ≤ 10 s |

Each flow meter's flow computer uses the temperature and density instruments listed in
`temperature_sensor` and `density_sensor`.

## Internal telemetry contract (schema 1.0)

`industrial/schemas/telemetry.py`. Every adapter must emit exactly this structure: the
simulator, OPC UA client, historian and MQTT plant gateway alike.

| Field | Type | Notes |
|---|---|---|
| event_id | UUID string | Duplicate event_ids are dropped at ingestion |
| asset_id | string | e.g. `PL-001` |
| sensor_id | string | e.g. `FM-001` |
| parameter | string | e.g. `volume_flow` |
| value | float or null | null when the reading is unavailable |
| unit | string | Engineering unit |
| event_timestamp | ISO-8601 UTC | Measurement time |
| ingestion_timestamp | ISO-8601 UTC | Set on arrival; may be later than event time |
| quality | GOOD \| UNCERTAIN \| BAD | |
| source | SIMULATION \| PLANT \| HISTORIAN \| MANUAL | Simulation and plant data never mix |
| source_tag | string | Origin tag or topic |
| run_id | string or null | Simulation run identifier |
| schema_version | "1.0" | |

**The fault state is never part of the contract.** It exists only in the ground-truth
database.

## MQTT topics

`industrial/mqtt/topics.py`. Access control is in `infrastructure/mqtt/acl`.

| Topic | Publisher | Content |
|---|---|---|
| `oilintelligence/simulation/<type>/<asset>/<parameter>` | Simulator | Telemetry contract, QoS 1 |
| `oilintelligence/plant/<type>/<asset>/<parameter>` | Plant gateway | Telemetry contract, QoS 1 |
| `oilintelligence/simulation/status` | Simulator | Run status (retained) |
| `oilintelligence/simulation/control` | Backend | start / pause / resume / stop / speed / inject_fault / clear_fault / add_transfer / perform_check |
| `oilintelligence/simulation/replies/<id>` | Simulator | Command acknowledgements |
| `oilintelligence/events/transfers` | Simulator / gateway | Transfer tickets |
| `oilintelligence/events/records/<kind>` | Simulator / gateway | Proving, calibration, lab samples, maintenance, operator log |
| `oilintelligence/events/runs` | Simulator | RUN_STARTED / RUN_STOPPED |
| `oilintelligence/events/anomalies` | Backend | Detected anomaly events |
| `oilintelligence/events/faults` | Backend | Diagnosed faults (never simulator truth) |
| `oilintelligence/truth/#` | Simulator | **Ground truth.** The ACL restricts it to the simulator and backend evaluation path |
| `oilintelligence/system/health`, `/heartbeat` | Backend | Platform health |

## OPC UA address space

`industrial/opcua/server.py`. Node ids follow `ns=<idx>;s=OilIntelligence.<Group>.<Asset>.<Variable>`.

```
Objects
└── OilIntelligence
    ├── TankFarm   TK-001…003 : Level, Temperature, Density, Inventory
    ├── Pipelines  PL-001…004 : FlowRate, Pressure, Temperature, Density, VolumeTotal
    ├── Pumps      PU-001…002 : Status, Speed, Power, DischargePressure
    └── Valves     XV-001…004 : Position, Status
```

The OPC UA *client* adapter (`industrial/opcua/client.py`) maps external node ids to
sensor ids through a tag map. It has no write path.

## Database tables

ORM: `apps/backend/app/models/orm.py`. PostgreSQL + TimescaleDB in Docker, SQLite locally.
`telemetry` and `balance_results` become hypertables when TimescaleDB is available.

**Main database (`oilintel`)**

| Table | Purpose | Key fields |
|---|---|---|
| users | Accounts and roles (admin / engineer / operator / viewer) | username, role, password_hash |
| assets | Equipment registry | asset_id, asset_type, configuration (JSON) |
| asset_relationships | Process topology edges | parent, child, relation |
| sensors | Instrument configuration | sensor_id, asset_id, parameter, unit, config |
| telemetry | **Raw measurements, append-only** | (event_id, event_timestamp), sensor_id, value, quality, source, run_id |
| transfers | Transfer tickets | transfer_id, route, start/end, recorded_quantity, custody_meter, status |
| calibration_records | Meter proving and instrument calibration | sensor_id, date, result, data |
| lab_samples | Tank density / BS&W samples | tank_id, rho15, bsw_pct |
| maintenance_records | Maintenance history | asset_id, activity |
| operator_logs | Operator log entries | asset_id, text |
| simulation_runs | Reproducible runs (scenario JSON + seed) | run_id, scenario, status (RUNNING / STOPPED / INTERRUPTED) |
| balance_results | Derived: windowed balance results | t1, facility_residual_t, chi2, rows (JSON) |
| model_predictions | Derived: ML outputs | model, version, output |
| anomaly_events | Detected anomalies | event_id, t0, t1, assets, severity, investigation_id |
| investigations | Agent cases (full state JSON) | investigation_id, status, state |
| investigation_evidence | Evidence register entries | investigation_id, ref, kind, content |
| recommendations | Recommended verification actions | action_id, expected_information_gain |
| engineer_reviews | Human reviews and verification results | decision, verified_cause, reviewer |
| diagnostic_cases | Reviewed outcomes for future training (§8.2 step 14) | residual, uncertainty, candidate_faults, verified_cause |
| audit_logs | Every state-changing API call and agent tool execution | actor, role, action, status_code |
| connectors | Integration connectors | connector_id, kind, config, status |

**Ground-truth database (`oilintel_truth`)** — separate, simulation only (FR-018)

| Table | Purpose |
|---|---|
| ground_truth | Per-asset true mass, flow, loss, sensor bias, fault type and location |
| fault_events | Injected faults (run_id, type, asset, start/end, parameters) |
| truth_snapshots | Full simulator truth rows (Simulation Lab view) |

## Synthetic dataset layers

`intelligence/training/dataset.py` writes `datasets/`:

| Layer | Location | Content |
|---|---|---|
| 1 — Physical truth | `synthetic/<run>/ground_truth.parquet` | True mass, level, temperature, density, flows, leak rates, valve and pump states, conservation error |
| 2 — Sensor measurements | `synthetic/<run>/telemetry.parquet` | Telemetry contract rows |
| 3 — Operational records | `synthetic/<run>/{transfers,calibrations,lab_samples,maintenance,operator_log}.parquet`, `events.json` | What business systems hold |
| 4 — Labels | `synthetic/<run>/fault_injections.parquet`, `scenario.json`, `training/cases.jsonl` | Injected faults, the reproducible scenario, per-window and per-case labels |
| Features | `training/windows.parquet`, `training/sensor_windows.parquet` | Model inputs |
| Metadata | `training/dataset_meta.json` | Run plan, plan hash, plant hash, environment counts |

Full four-layer data is kept for one held-out run per environment and family. Every run is
reproducible from its (family, seed, environment).
