"""MQTT topic design (section 9.1).

oilintelligence/
    simulation/<asset_type>/<asset_id>/<parameter>   telemetry from the synthetic adapter
    plant/<asset_type>/<asset_id>/<parameter>        telemetry from industrial gateways
    events/anomalies | events/transfers | events/faults (diagnosed faults, never simulator truth)
    system/health | system/heartbeat
    simulation/control                               commands to the simulator service
    truth/...                                        simulator ground truth - ACL-restricted to
                                                     the simulator and the evaluation service
"""
from __future__ import annotations

ROOT = "oilintelligence"


def telemetry_topic(source: str, asset_type: str, asset_id: str, parameter: str) -> str:
    domain = "simulation" if source == "SIMULATION" else "plant"
    return f"{ROOT}/{domain}/{asset_type}/{asset_id}/{parameter}"


TELEMETRY_WILDCARDS = [f"{ROOT}/simulation/+/+/+", f"{ROOT}/plant/+/+/+"]
EVENTS_ANOMALIES = f"{ROOT}/events/anomalies"
EVENTS_TRANSFERS = f"{ROOT}/events/transfers"
EVENTS_FAULTS = f"{ROOT}/events/faults"
SYSTEM_HEALTH = f"{ROOT}/system/health"
SYSTEM_HEARTBEAT = f"{ROOT}/system/heartbeat"
SIM_CONTROL = f"{ROOT}/simulation/control"
SIM_STATUS = f"{ROOT}/simulation/status"
TRUTH_ROOT = f"{ROOT}/truth"


def topic_matches(pattern: str, topic: str) -> bool:
    p, t = pattern.split("/"), topic.split("/")
    for i, seg in enumerate(p):
        if seg == "#":
            return True
        if i >= len(t):
            return False
        if seg != "+" and seg != t[i]:
            return False
    return len(p) == len(t)
