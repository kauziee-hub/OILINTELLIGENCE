# OILINTELLIGENCE

**Agentic AI-assisted hydrocarbon loss attribution and root-cause investigation using
physics-guided machine learning.**

OILINTELLIGENCE simulates a crude oil storage and transfer tank farm and streams realistic
instrument telemetry over MQTT and OPC UA. On that data it runs four layers of analysis:

- **Mass balance.** Topology-aware balances with propagated measurement uncertainty.
- **Attribution.** A calibrated Bayesian model separates physical losses (leaks) from
  measurement errors and unrecorded movements.
- **Active diagnosis.** It ranks the verification check with the highest information gain per
  unit cost and safety exposure.
- **Investigation.** A LangGraph agent gathers evidence and prepares a grounded report for
  engineer review.

It is read-only toward the plant: nothing in the platform can command a pump, valve or PLC.

> Research question: can a physics-guided probabilistic ML system, coordinated by an
> evidence-seeking agent, distinguish actual hydrocarbon losses from measurement and
> accounting discrepancies more accurately and efficiently than conventional mass-balance
> monitoring?

## Quick start (local, no Docker)

```bash
python -m venv .venv
.venv/Scripts/pip install -e ".[llm,broker,dev]"
npm --prefix apps/frontend install
```

On Linux or macOS, use `.venv/bin/pip`. Then start the backend (API, simulator, ingestion,
monitor and agent, with SQLite in `data/`):

```bash
.venv/Scripts/python -m uvicorn app.main:app --app-dir apps/backend --port 8000
```

Start the frontend:

```bash
npm --prefix apps/frontend run dev
```

Open http://localhost:3000 and sign in. On first start the seed account passwords are
written to `data/initial_credentials.txt`; change them, then delete the file. The OpenAPI
docs are at http://localhost:8000/docs.

**Docker Compose** (TimescaleDB, Mosquitto, simulator, OPC UA, backend, frontend):

```bash
cp .env.example .env
docker compose up -d --build
```

Set the REQUIRED values in `.env` before running compose. See
[docs/deployment](docs/deployment/deployment.md).

## MVP walk-through (§19.1)

1. **Operations Overview.** The virtual tank farm is already running on a live transfer
   schedule.
2. **Simulation Lab → Schedule a transfer.** Choose R-01 (TK-001 → TK-002), for example
   120 m³/h and 900 m³.
3. **Digital Twin.** Tanks fill and drain, flow particles run along the active lines, and the
   pumps and valves change state. Click any asset or instrument for live and historical values.
4. **Sensor Explorer.** Live readings, quality and history. Everything shown is read back
   from the database.
5. **Simulation Lab → Inject fault.** Choose Pipeline Leak on PL-002, or Meter Bias on FM-002.
6. Within one or two balance windows the monitor flags the control volume containing the
   fault, and an **AI Investigation** opens automatically.
7. **The investigation shows:**
   - the observed residual and its uncertainty;
   - competing causes with calibrated probabilities;
   - the R = L + B + U + ε decomposition;
   - an evidence register;
   - the recommended next check with its information gain.
8. **Perform check / Record result.** The posterior updates. In simulation mode the
   oracle performs the check against ground truth.
9. **Engineer review → Confirm & close.** Download the HTML, Markdown or JSON report.

## Repository layout

| Path | Contents |
|---|---|
| `config/plant.yaml` | Plant definition: the frozen Phase 1 process design |
| `simulation/` | Physics simulator, instruments, fault injection, records, ground truth, live service |
| `industrial/` | Telemetry contract, MQTT bus and broker, OPC UA server and client, historian adapters |
| `intelligence/` | Measurement model, mass balance, uncertainty, anomaly detection, sensor diagnostics, Bayesian attribution, active diagnosis, training, evaluation |
| `agents/` | LangGraph investigation workflow, permissioned tools, grounding check, reports, LLM provider |
| `apps/backend/` | FastAPI application (REST, WebSocket, auth, ingestion, monitoring) |
| `apps/frontend/` | Next.js application with the 3D digital twin (React Three Fiber) |
| `database/` | Alembic migrations, TimescaleDB init |
| `experiments/` | Research experiments, ablation, agent evaluation, results |
| `infrastructure/` | Dockerfiles, Mosquitto configuration and ACL, Prometheus |
| `docs/` | Design and reference documentation (below) |

## Documentation

| Document | Contents |
|---|---|
| [Process design package](docs/process_design/process_design_package.md) | Tanks, lines, pumps, valves, routes, instruments, scenarios, faults, topology |
| [Mathematical models](docs/mathematical_models/models.md) | Simulator physics, measurement model, balances, uncertainty, Bayesian attribution, information gain |
| [Architecture](docs/architecture/architecture.md) | Components, data flow, safety and trust boundaries, agent tools |
| [Data dictionary](docs/data_dictionary/data_dictionary.md) | Instrument tags, telemetry contract, MQTT topics, OPC UA address space, tables, dataset layers |
| [API reference](docs/API/api.md) | Endpoints, roles, WebSocket stream, report contract |
| [Testing](docs/testing/testing.md) | Test levels mapped to §13, how to run them |
| [Deployment](docs/deployment/deployment.md) | Local, Docker Compose, plant mode, security checklist |
| [Research results](docs/research/results.md) | KPI scorecard, Experiments 1–7, limitations (generated by `oil-experiments`) |

## Commands

| Command | Purpose |
|---|---|
| `python -m pytest -m "not slow"` | Unit and integration tests (`-m slow` runs the end-to-end MVP test) |
| `npm --prefix apps/frontend test` | Frontend tests (plus `run typecheck` and `run lint`) |
| `oil-sim validate` | Mass-conservation check of the simulator |
| `oil-sim run --family PIPELINE_LEAK --seed 3 --out datasets/synthetic/demo` | Run one scenario offline and export all four data layers |
| `oil-experiments --regenerate` | Regenerate the dataset, retrain Models 1–4, run every experiment, rewrite the results report |
| `oil-experiments --report-only` | Rewrite the results report from saved results |
| `alembic revision --autogenerate -m "…"` | Author a schema migration (applied automatically at startup) |

## Status and limitations

All results come from the simulator. They show that the models recognise the faults this
simulator models; they say nothing yet about accuracy on a real refinery, which needs field
validation (§18). Several §14 targets are not met. The scorecard, and the reasons each target
was missed, are in [docs/research/results.md](docs/research/results.md).

Two simplifications are documented in the
[mathematical models](docs/mathematical_models/models.md):

- the simplified VCF instead of API MPMS Ch. 11.1;
- quasi-steady hydraulics.
