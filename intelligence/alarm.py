"""Alarm confirmation (on-delay) for the rolling balance monitor, shared by the live monitor and the
offline demo proof so both apply exactly the same rule.

Successive evaluations use overlapping windows, and instrument errors are not white: level gauges and
meters carry slow error components (AR noise with a correlation time of ~10 min in the plant's
instrument specification). Two abnormal evaluations 5 minutes apart can therefore see the SAME slow gauge
excursion twice; that is not independent confirmation. A non-immediate alarm is confirmed only when the
abnormality has lasted at least as long as that error memory: a real fault persists, a gauge excursion
decorrelates.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from industrial.schemas.plant import PlantConfig


def instrument_memory_s(plant: PlantConfig) -> float:
    """Longest correlation time of any instrument's slow (autocorrelated) error component, seconds."""
    return max((float(s.ar_tau_s) for s in plant.sensors.values() if (getattr(s, "ar_sigma", 0.0) or 0.0) > 0),
               default=0.0)


@dataclass
class AlarmOnDelay:
    min_count: int = 2               # successive abnormal evaluations
    min_span_s: float = 600.0        # ... spanning at least this much data time
    count: int = 0
    since: float | None = None       # data time of the first abnormal evaluation of the current streak

    def update(self, anomaly: bool, immediate: bool, now: float) -> bool:
        """Feed one evaluation; True when it confirms an alarm."""
        if not anomaly:
            self.reset()
            return False
        self.count += 1
        if self.since is None:
            self.since = now
        return immediate or (self.count >= self.min_count and now - self.since >= self.min_span_s)

    def reset(self) -> None:
        self.count, self.since = 0, None


@dataclass
class SlowShiftCusum:
    """Two-sided CUSUM per control-volume balance row over non-overlapping windows.

    A slow fault (a level gauge drifting 1 cm/h) adds the same modest residual to every window (z ~ 2),
    which a per-window test sees only as occasional flickers. Accumulating the evidence detects it:
    S <- max(0, S + z - k), alarm when S > h. Non-overlapping 30-min windows are ~independent (instrument
    error memory is 10 min). Only CV rows: consistency rows (TCONS, DCONS) carry persistent reference
    offsets by design and would accumulate them. k = 1, h = 4: in-control average run length ~15,000
    windows per row and sign for unit-variance z (about one false alarm a month over all rows); a 2-sigma
    shift is flagged after ~4 windows. Validated on 7 normal runs (56 plant-hours, no alarm).
    """
    k: float = 1.0
    h: float = 4.0
    window_s: float = 1800.0
    kinds: tuple[str, ...] = ("CV",)
    S: dict[tuple[str, int], float] = field(default_factory=dict)
    last_t: float | None = None

    def update(self, z: dict[str, float], now: float) -> list[str]:
        """Feed the latest window's z-scores; returns the rows whose CUSUM exceeds h (empty if this
        evaluation overlaps the last window used)."""
        if self.last_t is not None and now - self.last_t < self.window_s - 1.0:
            return []
        self.last_t = now
        hits = set()
        for row, v in z.items():
            if row.split(":", 1)[0] not in self.kinds:
                continue
            for sgn in (1, -1):
                s = max(0.0, self.S.get((row, sgn), 0.0) + sgn * float(v) - self.k)
                self.S[(row, sgn)] = s
                if s > self.h:
                    hits.add(row)
        return sorted(hits)

    def statistic(self) -> dict[str, float]:
        return {f"{row}{'+' if sgn > 0 else '-'}": round(s, 2) for (row, sgn), s in self.S.items() if s > 0}

    def reset(self) -> None:
        self.S.clear()
        self.last_t = None
