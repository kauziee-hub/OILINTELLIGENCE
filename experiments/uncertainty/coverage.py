"""Uncertainty calibration experiment: are normalised residuals z = r / sigma ~ N(0, 1) under
normal operation?  Evaluated per row kind on fresh normal runs (seeds not used for calibration),
at the calibration sample spacing (10 s) and with a coarse 2-minute historian, where the
boundary-sampling term of the ticket / ledger rows has to carry the extra interpolation error.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any

import numpy as np
from scipy import stats

from industrial.schemas.plant import get_plant
from intelligence.mass_balance.engine import BalanceEngine
from intelligence.physics.measurement import MeasurementModel, RecordsView, TelemetryStore, to_epoch
from intelligence.uncertainty.propagation import assemble
from simulation.engine.export import run_scenario
from simulation.scenarios.scenario import generate_scenario


def _z_by_kind(plant, run, telemetry) -> dict[str, list[float]]:
    sc = run.scenario
    mm = MeasurementModel(plant, TelemetryStore(telemetry),
                          RecordsView(transfers=run.transfers, lab_samples=run.lab_samples,
                                      calibrations=run.calibrations))
    st = to_epoch(sc.start_time)
    edges = [st + t for t in np.arange(600.0, sc.duration_s - 60.0, 1800.0)]
    A = assemble(BalanceEngine(plant, mm).evidence(edges))
    out: dict[str, list[float]] = defaultdict(list)
    for row, z in zip(A.rows, A.z):
        if np.isfinite(z):
            out[row.kind].append(float(z))
    return out


def _summary(zs: list[float]) -> dict[str, Any]:
    z = np.asarray(zs)
    if not len(z):
        return {"n": 0}
    return {"n": int(len(z)), "mean": round(float(z.mean()), 3), "sd": round(float(z.std(ddof=1)) if len(z) > 1 else 0.0, 3),
            "within_1sigma": round(float(np.mean(np.abs(z) < 1)), 3), "within_2sigma": round(float(np.mean(np.abs(z) < 2)), 3),
            "within_3sigma": round(float(np.mean(np.abs(z) < 3)), 3), "max_abs": round(float(np.abs(z).max()), 2)}


def run(n_runs: int = 12, seed0: int = 7001, hours: float = 8.0) -> dict[str, Any]:
    plant = get_plant()
    dense: dict[str, list[float]] = defaultdict(list)
    sparse: dict[str, list[float]] = defaultdict(list)
    for k in range(n_runs):
        sc = generate_scenario(plant, "NORMAL", seed0 + k, "A", duration_s=hours * 3600, dt_s=2.0, sample_interval_s=10.0)
        res = run_scenario(sc)
        tel = res.telemetry
        for kind, z in _z_by_kind(plant, res, tel).items():
            dense[kind].extend(z)
        keep = tel.groupby(["sensor_id", "parameter"]).cumcount() % 12 == 0      # 10 s -> 120 s historian
        for kind, z in _z_by_kind(plant, res, tel[keep]).items():
            sparse[kind].extend(z)
    out: dict[str, Any] = {"n_runs": n_runs, "hours_per_run": hours, "seeds": [seed0, seed0 + n_runs - 1]}
    for name, d in (("sampling_10s", dense), ("sampling_120s", sparse)):
        allz = [z for v in d.values() for z in v]
        c2 = float(np.sum(np.square(allz)))
        out[name] = {"by_kind": {k: _summary(v) for k, v in sorted(d.items())},
                     "all": _summary(allz), "chi2_per_dof": round(c2 / max(len(allz), 1), 3),
                     "ks_p_value_vs_normal": float(stats.kstest(allz, "norm").pvalue) if allz else None}
    return out


if __name__ == "__main__":
    import json
    print(json.dumps(run(n_runs=3), indent=2))
