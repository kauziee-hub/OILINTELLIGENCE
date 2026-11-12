"""Evidence grounding check for generated narratives.

Every numeric claim in a narrative must match a number present in the
evidence record (within rounding), and every asset/sensor tag must exist in
the record. Narratives that fail are rejected (section 13.5: evidence citation
quality; "the agent must never present its own speculation as a measurement").
"""
from __future__ import annotations

import json
import re
from typing import Any

NUM = re.compile(r"(?<![A-Za-z0-9-])[-+]?\d+(?:[.,]\d+)?(?![A-Za-z0-9-])")
TAG = re.compile(r"\b(?:TK|PL|PU|XV|FM|LT|TT|PT|DT|MF|R|INV|EVT)-\d{2,4}\b")


def _collect_numbers(obj: Any, out: set[float]) -> None:
    if isinstance(obj, bool):
        return
    if isinstance(obj, (int, float)):
        out.add(float(obj))
        return
    if isinstance(obj, str):
        for m in NUM.findall(obj):
            try:
                out.add(float(m.replace(",", "")))
            except ValueError:
                pass
        return
    if isinstance(obj, dict):
        for k, v in obj.items():
            _collect_numbers(k, out)
            _collect_numbers(v, out)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            _collect_numbers(v, out)


def check_grounding(text: str, evidence: dict[str, Any]) -> tuple[bool, list[str]]:
    nums: set[float] = set()
    _collect_numbers(evidence, nums)
    derived = set()
    for n in nums:
        if 0 <= n <= 1:
            derived.add(round(n * 100, 1))   # probabilities quoted as percentages
            derived.add(round(n * 100))
    allowed = nums | derived
    blob = json.dumps(evidence, default=str)
    tags_allowed = set(TAG.findall(blob))
    problems: list[str] = []
    for m in NUM.findall(text):
        try:
            v = float(m.replace(",", ""))
        except ValueError:
            continue
        if abs(v) <= 10 and float(v).is_integer():
            continue  # small counts / ordinals ("two hypotheses", "window 3")
        ok = any(abs(v - a) <= max(0.051 * max(abs(a), 1e-9), 0.051) for a in allowed)
        if not ok:
            problems.append(f"number {m} not found in evidence")
    for t in set(TAG.findall(text)):
        if t not in tags_allowed:
            problems.append(f"identifier {t} not in evidence")
    return (not problems), problems
