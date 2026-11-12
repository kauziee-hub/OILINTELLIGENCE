"""Seed the asset registry, topology, sensors, users and connectors from the plant config."""
from __future__ import annotations

import logging
import secrets
from pathlib import Path

from app.core.config import REPO_ROOT, Settings
from app.core.security import hash_password
from app.models import orm

log = logging.getLogger("oilintel.seed")


def seed_assets(session, plant) -> None:
    P = plant

    def add(aid, atype, name, parent=None, cfg=None):
        session.merge(orm.Asset(asset_id=aid, asset_type=atype, asset_name=name, parent_asset_id=parent,
                                configuration=cfg or {}))

    add(P.plant_id, "facility", P.name)
    for k, t in P.tanks.items():
        add(k, "tank", t.name, P.plant_id, t.model_dump() | {"area_m2": t.area_m2, "capacity_m3": t.nominal_capacity_m3})
    for k, j in P.junctions.items():
        add(k, "manifold", j.name, P.plant_id, j.model_dump())
    for k, p in P.pipelines.items():
        add(k, "pipeline", p.name, P.plant_id, p.model_dump(by_alias=True))
    for k, p in P.pumps.items():
        add(k, "pump", p.name, P.plant_id, p.model_dump(by_alias=True))
    for k, v in P.valves.items():
        add(k, "valve", v.name, v.pipeline, v.model_dump())
    session.query(orm.AssetRelationship).delete()
    for k, p in P.pipelines.items():
        session.add(orm.AssetRelationship(from_asset=p.from_, to_asset=k, relation_type="FLOWS_INTO",
                                          properties={"pipeline": k}))
        session.add(orm.AssetRelationship(from_asset=k, to_asset=p.to, relation_type="FLOWS_INTO",
                                          properties={"pipeline": k}))
    for k, p in P.pumps.items():
        session.add(orm.AssetRelationship(from_asset=p.from_, to_asset=k, relation_type="FLOWS_INTO"))
        session.add(orm.AssetRelationship(from_asset=k, to_asset=p.to, relation_type="FLOWS_INTO"))
    for k, v in P.valves.items():
        session.add(orm.AssetRelationship(from_asset=k, to_asset=v.pipeline, relation_type="ISOLATES",
                                          properties={"position_on_line": v.position_on_line}))
    for sid, s in P.sensors.items():
        session.merge(orm.Sensor(sensor_id=sid, asset_id=s.asset, sensor_type=s.type, parameter=s.parameter,
                                 unit=s.unit, configuration=s.model_dump()))
        session.add(orm.AssetRelationship(from_asset=sid, to_asset=s.asset, relation_type="MEASURES",
                                          properties={"location": s.location}))
    session.commit()


def seed_users(session, s: Settings) -> None:
    """Create the four role accounts on first start. Passwords come from the environment
    (SEED_*_PASSWORD); if absent, random passwords are generated and written to
    data/initial_credentials.txt (never logged)."""
    if session.query(orm.User).count():
        return
    creds = []
    for user, role, pw in (("admin", "admin", s.seed_admin_password), ("engineer", "engineer", s.seed_engineer_password),
                           ("operator", "operator", s.seed_operator_password),
                           ("viewer", "viewer", s.seed_viewer_password)):
        if not pw:
            pw = secrets.token_urlsafe(12)
            creds.append(f"{user}: {pw}")
        session.add(orm.User(username=user, full_name=f"{role.title()} (seed)", role=role,
                             password_hash=hash_password(pw)))
    session.commit()
    if creds:
        path = REPO_ROOT / "data" / "initial_credentials.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("Generated seed credentials - change after first login\n" + "\n".join(creds) + "\n",
                        encoding="utf-8")
        log.warning("Seed users created with generated passwords; see %s", path)


def seed_connectors(session, s: Settings) -> None:
    defaults = [
        ("mqtt-internal", "mqtt", "Internal telemetry broker", {"host": s.mqtt_host, "port": s.mqtt_port}, s.bus == "mqtt"),
        ("opcua-virtual", "opcua", "Virtual OPC UA server (simulation)", {"url": s.opcua_endpoint.replace("0.0.0.0", "127.0.0.1")},
         s.opcua_enabled),
        ("opcua-plant", "opcua", "Plant OPC UA server (read-only)", {"url": "opc.tcp://plant-opc:4840", "tag_map": {}}, False),
        ("historian-file", "historian", "Historian export (CSV/Parquet)", {"path": "datasets/historian/export.csv",
                                                                            "tag_map": {}}, False),
        ("historian-rest", "historian", "REST historian (PI Web API style)", {"base_url": "https://historian.example/piwebapi",
                                                                              "tag_map": {}}, False),
    ]
    for cid, ctype, name, cfg, enabled in defaults:
        if session.get(orm.Connector, cid) is None:
            session.add(orm.Connector(connector_id=cid, connector_type=ctype, name=name, config=cfg, enabled=enabled,
                                      read_only=True))
    session.commit()
