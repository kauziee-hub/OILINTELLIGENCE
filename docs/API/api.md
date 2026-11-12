# API Reference (v1)

Base URL: `http://<host>:8000`. The interactive OpenAPI documentation is served at `/docs`
(Swagger) and `/redoc`, and is generated from the code.

**The platform is read-only toward the plant.** No endpoint, role or agent tool can send a
command to a pump, valve or PLC. The simulation-control endpoints act on the simulator only,
and they return `409` in `DATA_MODE=PLANT`.

## Authentication

```
POST /api/v1/auth/token        form fields: username, password
→ {"access_token": "<JWT>", "token_type": "bearer", "role": "engineer", "username": "engineer"}
```

Send the token as `Authorization: Bearer <JWT>`. The WebSocket takes it as a query
parameter, `/ws/live?token=<JWT>`. Tokens expire after `JWT_EXPIRE_MINUTES`, which defaults to
720.

**Roles** are ordered by rank; each role inherits everything granted to the roles below it.

| Role | Rank | Grants |
|---|---|---|
| viewer | lowest | Read access |
| operator | | Simulation control (simulation mode only) |
| engineer | | Investigations: create, re-run, verify, review, approve; simulator ground truth |
| admin | highest | Users, audit log, connector tests |

**Cross-cutting behaviour:**

- Every `POST`, `PUT` and `DELETE` under `/api/v1` is written to the audit log with actor,
  role, path and status.
- A per-client rate limit applies (`RATE_LIMIT_PER_MINUTE`).
- Every response carries `X-Response-Time-ms`.

## Endpoints

"auth" means any authenticated user. A named role means that role or higher.

### System

| Method | Path | Access | Purpose |
|---|---|---|---|
| GET | `/api/v1/health` | Public | Mode, ingestion, bus, simulator and monitor status |
| GET | `/metrics` | Public | Prometheus metrics: ingestion counts and delay, bus buffer and drops, monitor, simulation step rate, API p95, WebSocket clients |
| WS | `/ws/live?token=` | Token | Live stream (see below) |
| GET | `/api/v1/audit` | admin | Audit log |

### Auth and users

| Method | Path | Access | Purpose |
|---|---|---|---|
| POST | `/api/v1/auth/token` | Public | Log in and receive a JWT |
| GET | `/api/v1/auth/me` | auth | Current principal |
| GET, POST | `/api/v1/users` | admin | List or create users |

### Assets and telemetry

| Method | Path | Access | Purpose |
|---|---|---|---|
| GET | `/api/v1/assets` | auth | Asset registry |
| GET | `/api/v1/assets/{asset_id}` | auth | Asset details and configuration |
| GET | `/api/v1/assets/{asset_id}/sensors` | auth | Instruments on an asset |
| GET | `/api/v1/topology` | auth | Facility graph: nodes plus physical and measurement edges |
| GET | `/api/v1/telemetry/latest` | auth | Latest value per sensor and parameter |
| GET | `/api/v1/telemetry/history?sensor_id=…&start=&end=&max_points=` | auth | Historical series, downsampled |
| GET | `/api/v1/telemetry/quality` | auth | Completeness, staleness and quality per instrument |
| GET | `/api/v1/sensors/{sensor_id}` | auth | Sensor configuration and recent values |
| GET | `/api/v1/transfers` | auth | Transfer tickets |
| GET | `/api/v1/events/anomalies` | auth | Anomaly events |
| GET | `/api/v1/records/operator-log` | auth | Operator log |
| GET | `/api/v1/twin/state` | auth | Full digital-twin state: tanks, pipes, pumps, valves, sensors, alerts, balance |
| GET | `/api/v1/monitoring/latest` | auth | Latest windowed balance result |

### Physics and ML

| Method | Path | Access | Purpose |
|---|---|---|---|
| POST | `/api/v1/physics/reconcile` | auth | Topology-aware mass balance with propagated uncertainty, for a period |
| GET | `/api/v1/physics/residuals` | auth | Stored balance results |
| POST | `/api/v1/ml/anomaly-detection` | auth | Model 1 detectors for a period |
| POST | `/api/v1/ml/fault-attribution` | auth | Model 3: calibrated hypothesis probabilities, decomposition, loss estimate |
| POST | `/api/v1/ml/next-best-test` | auth | Model 4: ranked verification checks |
| POST | `/api/v1/ml/sensor-health` | auth | Model 2: per-instrument fault classes |
| GET | `/api/v1/ml/model-performance` | auth | Evaluation results (`experiments/results`) |

### Investigations (agent)

| Method | Path | Access | Purpose |
|---|---|---|---|
| POST | `/api/v1/investigations` | engineer | Open an investigation for an event or period. An event matching an open case is attached to it (`DUPLICATE`) |
| GET | `/api/v1/investigations` | auth | List investigations |
| GET | `/api/v1/investigations/{id}` | auth | Full state: report, evidence register, tool calls, posterior, reviews |
| POST | `/api/v1/investigations/{id}/run` | engineer | Re-run the workflow |
| POST | `/api/v1/investigations/{id}/refresh` | engineer | Re-analyse with all data available now, keeping verifications |
| POST | `/api/v1/investigations/{id}/verify` | engineer | `{action_id, outcome?, use_simulation_oracle}`: record a check result (POSITIVE or NEGATIVE). In simulation mode the oracle performs the check against ground truth |
| POST | `/api/v1/investigations/{id}/review` | engineer | `decision`: CONFIRM (with `verified_cause`), REJECT, REQUEST_MORE_EVIDENCE or VERIFICATION_RESULT (with `action_id`, `outcome`) |
| GET | `/api/v1/investigations/{id}/evidence` | auth | Evidence register |
| GET | `/api/v1/investigations/{id}/report?format=json\|markdown\|html` | auth | Investigation report |
| GET | `/api/v1/reports/kpis` | auth | Platform and diagnostic KPIs |

**Investigation status values:** `RECEIVED`, `COLLECTING_EVIDENCE`, `NEEDS_DATA`,
`AWAITING_VERIFICATION`, `AWAITING_REVIEW`, `RESOLVED_PENDING_APPROVAL`, `CLOSED`, `REJECTED`,
`DUPLICATE`, `SUPERSEDED`.

### Simulation (SIMULATION mode only)

| Method | Path | Access | Purpose |
|---|---|---|---|
| POST | `/api/v1/simulation/start` (alias `/scenarios`) | operator | Start a run: live schedule, a generated scenario `{family, seed}`, or a full scenario JSON |
| POST | `/api/v1/simulation/pause`, `/resume`, `/stop` | operator | Run control |
| PUT | `/api/v1/simulation/speed` | operator | 0.25–120× real time |
| GET | `/api/v1/simulation/status` | auth | Run id, simulation time, active and pending transfers, conservation error |
| GET | `/api/v1/simulation/runs` | auth | Run history: RUNNING, STOPPED or INTERRUPTED |
| GET | `/api/v1/simulation/catalog` | auth | Fault types, targets, default parameters, routes |
| POST | `/api/v1/simulation/faults` | operator | Inject a fault at the current simulation time |
| DELETE | `/api/v1/simulation/faults/{fault_id}` | operator | Clear a fault |
| POST | `/api/v1/simulation/transfers` | operator | Schedule a transfer |
| POST | `/api/v1/simulation/verification` | engineer | Perform a field check against simulator ground truth |
| GET | `/api/v1/simulation/truth/latest` | engineer | **Simulated ground truth** (never available to diagnostic models) |
| GET | `/api/v1/simulation/truth/faults?all_runs=false` | engineer | Injected faults of the current run |

A new run never starts before the newest stored simulation telemetry. Accelerated runs end
ahead of the wall clock, and two runs must not share timestamps.

### Integrations

| Method | Path | Access | Purpose |
|---|---|---|---|
| GET | `/api/v1/integrations` | auth | Connectors and their status |
| POST | `/api/v1/integrations/opcua/test` | admin | Read-only OPC UA connection test (browse and read) |
| POST | `/api/v1/integrations/mqtt/test` | admin | MQTT connection test |
| POST | `/api/v1/integrations/historian/test` | admin | Historian adapter test (file or REST) |
| GET | `/api/v1/integrations/{id}/status` | auth | Connector status |

## Live stream (`/ws/live`)

On connect, the server sends `{"type": "state", "data": <twin state>}`. After that:

| type | When | data |
|---|---|---|
| `state` | About once per second | Twin state: equipment, sensor values with quality and staleness, simulation status, alerts |
| `balance` | Every monitor evaluation (`MONITOR_INTERVAL_S` of data time) | Facility residual, σ, χ²/dof, per-row z, anomaly flag |
| `anomaly` | New anomaly event | event_id, severity, assets, t0, t1, summary |
| `investigation` | Investigation created or refreshed | investigation_id, status |

The client may send any text to keep the connection alive. A missing or invalid token closes
the socket with code 4401.

## Investigation report contract

`GET /investigations/{id}/report?format=json` follows the agent output contract in §8.4:

```json
{
  "investigation_id": "INV-93EB87",
  "status": "AWAITING_VERIFICATION",
  "diagnosis_status": "COMPETING_HYPOTHESES",
  "observed_residual_tonnes": 2.87,
  "residual_uncertainty_tonnes": 0.41,
  "estimated_physical_loss_tonnes": null,
  "physical_loss_interval_tonnes": null,
  "residual_decomposition_tonnes": {"L_physical_loss": 2.1, "B_measurement_bias": 0.3, "U_unrecorded_movement": 0.2, "epsilon": 0.27},
  "candidate_hypotheses": [{"hypothesis_id": "PIPELINE_LEAK:PL-002", "cause": "PIPELINE_LEAK", "calibrated_probability": 0.55}],
  "recommended_check": {"action_id": "LINE_INSPECTION:PL-002", "reason": "...", "expected_information_gain_bits": 0.71},
  "evidence": [{"ref": "E1", "kind": "balance", "source": "calculate_mass_balance", "...": "..."}],
  "narrative": "... [E1] ...",
  "grounding": {"passed": true},
  "requires_engineer_approval": true
}
```

The values above are illustrative.

- `estimated_physical_loss_tonnes` stays `null` until P(physical loss) ≥ 0.6.
- Every number in `narrative` is checked against the evidence register. A narrative that
  fails the check is replaced by the deterministic template.
