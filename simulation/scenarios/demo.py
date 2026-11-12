"""Demo scenarios: deterministic, presentation-ready runs that each prove one capability.

Every scenario starts from the same plant state, runs a normal baseline first and injects ONE
known fault at a known time, so the platform's detection and diagnosis can be checked against the
truth live. `experiments/demo_check.py` runs all of them offline through the same pipeline.
"""
from __future__ import annotations

from typing import Any

from industrial.schemas.plant import PlantConfig
from simulation.faults.faults import FaultSpec
from simulation.scenarios.scenario import Scenario, TankInitial, TransferOrder

INJECT_S = 5400.0           # 1.5 h normal baseline before the fault (calibrates "normal" for the viewer)
DURATION_S = 8 * 3600.0

DEMO_SCENARIOS: list[dict[str, Any]] = [
    {"key": "normal", "title": "Normal operation", "category": "Baseline",
     "story": "Crude moves from TK-001 to TK-002, the line goes idle, then TK-002 sends crude to TK-003. "
              "Nothing is wrong.",
     "proves": "No false alarms: the balance never leaves its uncertainty band, so no investigation opens.",
     "expected": {"kind": "normal", "hypothesis": "NORMAL", "label": "no alarm"},
     "talk": ["Every residual is shown with its ±2σ band; real plants never balance to zero.",
              "Route changes and pump starts are normal transients, absorbed by the uncertainty model and "
              "the alarm on-delay: an alarm must persist longer than the gauges' slow error (10 min), so "
              "one gauge excursion cannot confirm itself."]},
    {"key": "pipeline_leak", "title": "Pipeline leak on PL-002", "category": "Physical loss",
     "story": "A 9 mm hole opens in the discharge line to TK-002 during a transfer. About 4 t/h of crude escapes.",
     "proves": "A real loss is detected within minutes, located to the right pipes, sized with an interval, "
               "and confirmed with the field check the system recommends.",
     "expected": {"kind": "fault", "hypothesis": "PIPELINE_LEAK:PL-002", "label": "leak on PL-002"},
     "talk": ["Flow data alone cannot separate PL-002 from PL-003 (both drain the same manifold volume), so the "
              "model keeps both and asks for a line inspection.",
              "The loss estimate comes with an interval; compare it with the simulated truth on this page."]},
    {"key": "meter_bias", "title": "Flow meter reads 2.5% high", "category": "Measurement error",
     "story": "Custody meter FM-002 drifts out of calibration and over-reads by 2.5%. No oil is lost.",
     "proves": "Apparent loss vs physical loss: the system blames the meter, not a leak, because the error "
               "moves mass between two control volumes instead of removing it.",
     "expected": {"kind": "fault", "hypothesis": "METER_ERROR:FM-002", "label": "FM-002 meter error"},
     "talk": ["Commercially this matters most: a biased custody meter mis-bills every barrel.",
              "The recommended check is a meter proving, not a costly line walk."]},
    {"key": "passing_valve", "title": "Valve XV-003 passing", "category": "Physical movement",
     "story": "The closed inlet valve of TK-003 leaks through its seat while crude flows to TK-002, so oil "
              "ends up in the wrong tank.",
     "proves": "Topology-aware reasoning: mass leaves the manifold but appears in TK-003, which a leak "
               "cannot explain.",
     "expected": {"kind": "fault", "hypothesis": "VALVE_PASSING:XV-003", "label": "XV-003 passing"},
     "talk": ["Nothing is lost from the facility; it is in the wrong place.",
              "A valve seat test needs isolation, so the system will not propose it while a transfer runs."]},
    {"key": "tank_leak", "title": "Tank TK-001 floor leak", "category": "Physical loss",
     "story": "A 14 mm hole in the floor of the source tank leaks roughly 4.5 t/h of crude into the bund "
              "(the largest hole in the range the model was designed and trained for: 5-14 mm).",
     "proves": "The leak is localised to the tank (not the lines) because the tank's own balance breaks, "
               "even when it is idle.",
     "expected": {"kind": "fault", "hypothesis": "TANK_LEAK:TK-001", "label": "leak in TK-001"},
     "talk": ["A tank leak continues when no transfer runs; a line leak mostly needs pressure. Once the transfer "
              "ends, the idle tank keeps losing level with no meter flow at all.",
              "Its rate follows the liquid head (√level), which the model uses as a signature."]},
    {"key": "level_drift", "title": "Level gauge LT-002 drifting", "category": "Measurement error",
     "story": "The radar gauge on TK-002 slowly drifts upward by about 1 cm per hour.",
     "proves": "Slow instrument drift is told apart from a real gain or loss by its shape over time.",
     "expected": {"kind": "fault", "hypothesis": "LEVEL_ERROR:LT-002", "label": "LT-002 level error"},
     "talk": ["1 cm on a 25 m tank is about 4 t: small per hour, large over a shift.",
              "The check it recommends is a manual dip, the cheapest decisive test."]},
    {"key": "missing_transfer", "title": "Unrecorded transfer", "category": "Accounting",
     "story": "A transfer from TK-001 to TK-003 runs, but nobody books it: no ticket exists.",
     "proves": "Accounting faults are caught by reconciling meters against the transfer records.",
     "expected": {"kind": "fault", "hypothesis": "MISSING_TRANSFER:R-02", "label": "unbooked transfer on R-02"},
     "talk": ["The physics is fine: oil moved exactly as metered. Only the books are wrong.",
              "The recommended check is a ledger reconciliation, which costs almost nothing."]},
    {"key": "unknown_fault", "title": "A fault the model has never seen", "category": "Robustness",
     "story": "The ticket for transfer D-2 is booked 4% too high (a data-entry error). The model has no "
              "hypothesis for a wrong ticket: it was never shown one.",
     "proves": "Honest abstention: the model answers UNKNOWN (or no confident cause) instead of forcing a "
               "confident wrong one.",
     "expected": {"kind": "unknown", "hypothesis": "UNKNOWN", "label": "abstain (no confident known cause)"},
     "talk": ["A classifier must pick one of the labels it was trained on; this model can say 'none of these'.",
              "That is what makes it safe to put in front of engineers."]},
    {"key": "pump_degradation", "title": "Pump PU-001 losing head", "category": "Equipment",
     "story": "Transfer pump PU-001 wears and delivers 18% less head than its curve.",
     "proves": "Equipment health from process data: measured head vs the pump curve.",
     "expected": {"kind": "fault", "hypothesis": "PUMP_DEGRADATION:PU-001", "label": "PU-001 degraded"},
     "talk": ["No oil is lost; the flow controller opens the valve further to compensate.",
              "The model finds it in the pump-head rows of the balance, not in the mass rows."]},
]
BY_KEY = {s["key"]: s for s in DEMO_SCENARIOS}


def inject_s(key: str) -> float | None:
    """Simulated second at which the scenario's fault starts (None: no fault)."""
    if key == "normal":
        return None
    return 6300.0 if key in ("missing_transfer", "unknown_fault") else INJECT_S


def build_demo_scenario(plant: PlantConfig, key: str, seed: int = 4242, dt_s: float = 1.0,
                        sample_interval_s: float = 1.0) -> Scenario:
    if key not in BY_KEY:
        raise KeyError(key)
    initial = {"TK-001": TankInitial(level_m=13.0, temperature_c=36.0),
               "TK-002": TankInitial(level_m=5.0, temperature_c=32.0),
               "TK-003": TankInitial(level_m=4.5, temperature_c=30.0)}
    main = TransferOrder(transfer_id="D-1", route="R-01", start_s=900.0, setpoint_m3h=120.0, pumps=["PU-001"],
                         target_volume_m3=1000.0)
    ops = [main]
    faults: list[FaultSpec] = []
    if key == "normal":
        ops = [TransferOrder(transfer_id="D-1", route="R-01", start_s=900.0, setpoint_m3h=120.0, pumps=["PU-001"],
                             target_volume_m3=400.0),
               TransferOrder(transfer_id="D-2", route="R-03", start_s=5.0 * 3600, setpoint_m3h=80.0, pumps=["PU-001"],
                             target_volume_m3=200.0)]
    elif key == "pipeline_leak":
        faults = [FaultSpec(fault_type="PIPELINE_LEAK", target="PL-002", start_s=INJECT_S,
                            params={"position": 0.5, "hole_diameter_mm": 9.0, "ramp_s": 60.0})]
    elif key == "meter_bias":
        faults = [FaultSpec(fault_type="METER_BIAS", target="FM-002", start_s=INJECT_S, params={"bias_rel": 0.025})]
    elif key == "passing_valve":
        faults = [FaultSpec(fault_type="VALVE_PASSING", target="XV-003", start_s=INJECT_S,
                            params={"passing_fraction": 0.004})]
    elif key == "tank_leak":
        # the transfer ends at ~2.75 h; the leaking tank then sits idle, where a tank leak is unmistakable and
        # the decisive tank isolation test is possible. (At 400 m3 it ended at 3.6 h, only minutes after the
        # presenter's re-analysis an hour after detection, so the outcome hinged on detection timing.)
        ops = [TransferOrder(transfer_id="D-1", route="R-01", start_s=900.0, setpoint_m3h=120.0, pumps=["PU-001"],
                             target_volume_m3=300.0)]
        faults = [FaultSpec(fault_type="TANK_LEAK", target="TK-001", start_s=INJECT_S, params={"hole_diameter_mm": 14.0})]
    elif key == "level_drift":
        faults = [FaultSpec(fault_type="LEVEL_DRIFT", target="LT-002", start_s=INJECT_S, params={"drift_m_per_h": 0.01})]
    elif key == "missing_transfer":
        # D-1 (booked) finishes at ~1.4 h; D-2 starts at 1.75 h and is never booked
        ops = [TransferOrder(transfer_id="D-1", route="R-01", start_s=900.0, setpoint_m3h=120.0, pumps=["PU-001"],
                             target_volume_m3=140.0),
               TransferOrder(transfer_id="D-2", route="R-02", start_s=inject_s(key), setpoint_m3h=110.0,
                             pumps=["PU-001"], target_volume_m3=330.0)]
        faults = [FaultSpec(fault_type="MISSING_TRANSACTION", target="D-2", start_s=inject_s(key))]
    elif key == "unknown_fault":
        ops = [TransferOrder(transfer_id="D-1", route="R-01", start_s=900.0, setpoint_m3h=120.0, pumps=["PU-001"],
                             target_volume_m3=140.0),
               TransferOrder(transfer_id="D-2", route="R-02", start_s=inject_s(key), setpoint_m3h=110.0,
                             pumps=["PU-001"], target_volume_m3=220.0)]
        faults = [FaultSpec(fault_type="WRONG_QUANTITY", target="D-2", start_s=inject_s(key), params={"error_frac": 0.04})]
    elif key == "pump_degradation":
        faults = [FaultSpec(fault_type="PUMP_DEGRADATION", target="PU-001", start_s=INJECT_S,
                            params={"head_loss_frac": 0.18, "eff_loss_frac": 0.08})]
    return Scenario(scenario_id=f"DEMO-{key}", family=key.upper(), seed=seed, description=BY_KEY[key]["title"],
                    duration_s=DURATION_S, dt_s=dt_s, sample_interval_s=sample_interval_s, initial=initial,
                    operations=ops, faults=faults, metadata={"demo": key, "inject_s": inject_s(key)})
