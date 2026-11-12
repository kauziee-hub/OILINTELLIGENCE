# Deployment

## 1. Local development (no Docker)

Requires Python 3.11+ and Node 22+.

```bash
python -m venv .venv
.venv/Scripts/pip install -e ".[llm,broker,dev]"      # Linux/macOS: .venv/bin/pip
npm --prefix apps/frontend install
```

Run the backend. In-process bus, local simulator and SQLite in `data/`:

```bash
.venv/Scripts/python -m uvicorn app.main:app --app-dir apps/backend --port 8000
```

Run the frontend:

```bash
npm --prefix apps/frontend run dev
```

Open http://localhost:3000.

**First start.**

- Migrations are applied, the asset registry is seeded and the simulator starts (`SIM_SPEED`,
  default 10×).
- Seed account passwords are taken from `SEED_*_PASSWORD`. When those are unset, random ones
  are written to `data/initial_credentials.txt`. Change them after the first login and delete
  the file.
- Copy `.env.example` to `.env` to change any setting.

**Local MQTT broker (optional).** To exercise the real MQTT path without Docker, set
`BUS=mqtt` and `EMBEDDED_BROKER=true`. This starts an amqtt broker inside the backend.

**Database migrations.** `app.core.db.init_db()` runs `alembic upgrade head` at startup. A
database created before migrations existed is stamped at revision `0001`, which describes
that schema. To author a schema change:

```bash
.venv/Scripts/alembic revision --autogenerate -m "describe change"
```

Review the generated file in `database/migrations/versions/` before committing it.

## 2. Docker Compose (pilot)

```bash
cp .env.example .env
docker compose up -d --build
docker compose --profile monitoring up -d
```

The first command needs every value marked REQUIRED in `.env` set first. The last command is
optional and adds Prometheus (:9090) and Grafana (:3001).

| Service | Image / build | Ports | Notes |
|---|---|---|---|
| database | timescale/timescaledb:latest-pg16 | internal | `oilintel` + `oilintel_truth` databases; hypertables created at startup |
| mqtt | eclipse-mosquitto:2 | internal | Password file generated from env; ACL in `infrastructure/mqtt/acl`; TLS listener template in `mosquitto.conf` |
| simulator | Python image | — | `python -m simulation.engine.runner live`; publishes over MQTT |
| opcua | Python image | 4840 | Virtual OPC UA server mirroring simulated instruments |
| backend | Python image | 8000 | API, ingestion, monitoring, ML, agent; `SIMULATOR=remote` |
| frontend | Next.js standalone | 3000 | `PUBLIC_API_URL` is baked in at build time |

**Health.** The backend image has a `HEALTHCHECK` on `/api/v1/health`, and `/metrics`
serves Prometheus metrics (`infrastructure/monitoring/prometheus.yml`).

**Known limitation (remote simulator).** On a full stack restart, the simulator container
autostarts a new run at wall-clock time. If the previous run was accelerated past wall time,
the run's clock will not respect stored history. Start runs from the Simulation Lab, which
passes the stored-history floor, or keep `SIM_SPEED` at 1 for long-lived pilots. In local
mode the backend applies the floor automatically.

## 3. Plant mode (read-only integration)

Set `DATA_MODE=PLANT`. Simulation endpoints are then disabled (409), and only `source=PLANT`
or `HISTORIAN` data is read. Connect one or more adapters, all of them read-only:

| Adapter | Configure | Notes |
|---|---|---|
| OPC UA client | Settings → Connectors, or `POST /api/v1/integrations/opcua/test` | Endpoint, security policy, certificate, and a tag map from node id to sensor id. Browse, read and subscribe only |
| Historian (file) | CSV or Parquet with `timestamp, tag, value[, quality]` | Tag map to sensor ids |
| Historian (REST) | PI-Web-API-style `GET {base}/streams/{web_id}/recorded` | Read-only |
| MQTT plant gateway | Gateway publishes the telemetry contract to `oilintelligence/plant/#` | The `gateway` account may write `plant/#` only |

Before diagnosis results can be trusted on a real plant:

1. Replace `config/plant.yaml` with the plant's geometry, strapping, instruments and routes.
2. Calibrate the uncertainty model on normal-operation plant data (`experiments/uncertainty`).
3. Validate attribution on reviewed historical cases.

Synthetic results do not transfer automatically (§18).

## 4. Security checklist (ISA/IEC 62443-aligned, §12.3)

- [ ] Strong, unique `JWT_SECRET`, `POSTGRES_PASSWORD` and `MQTT_*_PASSWORD`. The repository
      contains no default secrets.
- [ ] Seed passwords rotated; `data/initial_credentials.txt` deleted.
- [ ] TLS in front of the API and frontend (reverse proxy). MQTT on the TLS listener (8883)
      for any external gateway. OPC UA with Basic256Sha256 Sign&Encrypt (`OPCUA_CERT`,
      `OPCUA_KEY`).
- [ ] `CORS_ORIGINS` restricted to the frontend origin.
- [ ] Broker and database reachable only on the internal network.
- [ ] The platform sits in a DMZ or level 3.5 zone. It needs only outbound read access to OPC
      UA or historian servers, and never write access to control networks.
- [ ] `LLM_PROVIDER=none` unless sending investigation summaries to the model provider has
      been approved. The LLM receives the structured case record only, never raw telemetry
      streams.
- [ ] Audit log (`/api/v1/audit`) reviewed and retained per site policy.
