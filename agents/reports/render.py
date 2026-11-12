"""Investigation report: structured contract + evidence register + rendering.

Every statement in the narrative cites an evidence item [E#]; each evidence
item points to a measurement, a deterministic calculation, a record or a
model output. The numeric content of the report is produced here from tool
results - the LLM (if enabled) only rephrases it and is grounding-checked.
"""
from __future__ import annotations

import html
import math
from datetime import datetime, timezone
from typing import Any

from agents.evidence.grounding import check_grounding
from intelligence.fault_attribution.hypotheses import LABELS


def _fmt_time(ts: float | None) -> str:
    if ts is None:
        return "-"
    return datetime.fromtimestamp(float(ts), tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def build_evidence_register(state: dict[str, Any]) -> list[dict[str, Any]]:
    ev: list[dict[str, Any]] = []

    def add(kind, source, summary, values=None):
        ev.append({"id": f"E{len(ev) + 1}", "type": kind, "source": source, "summary": summary,
                   "values": values or {}})
        return ev[-1]["id"]

    bal = state.get("balance") or {}
    if bal:
        add("CALCULATION", "calculate_mass_balance",
            f"Facility balance residual {bal['facility_residual_t']} t (1-sigma {bal['facility_residual_sigma_t']} t) "
            f"over {bal['windows']} windows; chi-square {bal['chi2']} on {bal['dof']} rows",
            {"facility_residual_t": bal["facility_residual_t"], "sigma_t": bal["facility_residual_sigma_t"],
             "chi2": bal["chi2"], "dof": bal["dof"]})
        for r in bal.get("rows_most_abnormal", [])[:4]:
            if abs(r["z"]) >= 2.5:
                add("CALCULATION", "calculate_mass_balance",
                    f"Row {r['row']}: residual {r['value']} {r['unit']}, sigma {r['sigma']}, z = {r['z']}",
                    {"row": r["row"], "value": r["value"], "sigma": r["sigma"], "z": r["z"]})
        for sid, issues in (bal.get("data_quality_issues") or {}).items():
            kinds = sorted({k for i in issues for k in ("stuck", "dropout") if i.get(k)})
            add("MEASUREMENT", "data quality validation",
                f"{sid}: {', '.join(kinds)} detected in {len(issues)} window(s)", {"sensor": sid, "issues": kinds})
    att = state.get("attribution") or {}
    for h in att.get("hypotheses", [])[:4]:
        add("MODEL_OUTPUT", f"run_fault_attribution ({att.get('model', {}).get('version', '')})",
            f"{h['label']}: calibrated probability {h['calibrated_probability']}",
            {"hypothesis": h["hypothesis_id"], "p": h["calibrated_probability"], "magnitude": h.get("magnitude", {})})
    for rec in state.get("records_evidence", []):
        add("RECORD", rec["source"], rec["summary"], rec.get("values"))
    for v in state.get("verifications", []):
        add("RECORD", "engineer verification",
            f"{v['action_id']}: {v['outcome']}" + (f" ({v.get('detail')})" if v.get("detail") else ""),
            {"action_id": v["action_id"], "outcome": v["outcome"]})
    return ev


def template_narrative(rep: dict[str, Any], evidence: list[dict[str, Any]]) -> str:
    cite = {e["source"]: e["id"] for e in evidence}
    e_bal = next((e["id"] for e in evidence if e["source"] == "calculate_mass_balance"), None)
    parts = []
    R = rep.get("observed_residual_tonnes")
    if R is not None:
        parts.append(f"Over the analysis period ({rep['period']['start']} to {rep['period']['end']}) the facility "
                     f"mass balance closed with a residual of {R} t against a 1-sigma measurement uncertainty of "
                     f"{rep['residual_uncertainty_tonnes']} t; a positive residual is an apparent loss [{e_bal}].")
    rows = [e for e in evidence if e["source"] == "calculate_mass_balance" and "Row" in e["summary"]]
    if rows:
        parts.append("The largest deviations were " + "; ".join(
            f"{e['values']['row']} with z = {e['values']['z']} [{e['id']}]" for e in rows[:3]) + ".")
    q = [e for e in evidence if e["source"] == "data quality validation"]
    if q:
        parts.append("Data quality checks flagged " + "; ".join(f"{e['summary']} [{e['id']}]" for e in q) + ".")
    hyps = rep.get("candidate_hypotheses", [])
    model_ev = [e for e in evidence if e["type"] == "MODEL_OUTPUT"]
    if rep["status"] == "NEEDS_DATA":
        parts.append("Required evidence was missing, so no diagnosis has been produced: "
                     + "; ".join(rep.get("missing_evidence", [])) + ".")
    elif hyps:
        top = hyps[0]
        s = (f"The most probable explanation is {top['label']} with a calibrated probability of "
             f"{top['calibrated_probability']} [{model_ev[0]['id'] if model_ev else e_bal}].")
        if len(hyps) > 1:
            s += " Competing explanations remain: " + ", ".join(
                f"{h['label']} ({h['calibrated_probability']})" for h in hyps[1:3]) + "."
        parts.append(s)
        if rep.get("diagnosis_status") == "UNKNOWN_CAUSE":
            parts.append("No known fault mechanism explains the evidence well; the abnormality is reported as "
                         "unexplained rather than assigned to a known cause.")
    if rep.get("estimated_physical_loss_tonnes") is not None:
        lo, hi = rep.get("physical_loss_interval_tonnes") or (None, None)
        parts.append(f"The evidence supports an estimated physical loss of {rep['estimated_physical_loss_tonnes']} t "
                     f"(90% interval {lo} to {hi} t).")
    else:
        parts.append("The evidence does not yet support a defensible physical loss estimate, so none is reported.")
    for c in rep.get("conflicts", []):
        parts.append(f"Note: {c}")
    rc = rep.get("recommended_check")
    if rc:
        parts.append(f"Recommended next verification: {rc['description']} ({rc['action_id']}), because it "
                     f"{rc['reason'][0].lower() + rc['reason'][1:]}; expected information gain "
                     f"{rc['expected_information_gain_bits']} bits.")
    parts.append("All findings require engineering review; the platform does not issue operating instructions.")
    return " ".join(parts)


def build_report(state: dict[str, Any], llm=None) -> dict[str, Any]:
    att = state.get("attribution") or {}
    bal = state.get("balance") or {}
    hyps = []
    for h in att.get("hypotheses", [])[:6]:
        hyps.append({"cause": h["family"], "target": h["target"], "label": h["label"],
                     "hypothesis_id": h["hypothesis_id"], "calibrated_probability": round(h["calibrated_probability"], 3),
                     "category": h["category"], "magnitude": h.get("magnitude", {})})
    rc = state.get("recommended_check")
    rep: dict[str, Any] = {
        "investigation_id": state["investigation_id"],
        "event_id": state.get("event", {}).get("event_id"),
        "status": state.get("status"),
        "diagnosis_status": att.get("status"),
        "period": {"start": _fmt_time(state.get("t0")), "end": _fmt_time(state.get("t1")),
                   "t0": state.get("t0"), "t1": state.get("t1")},
        "observed_residual_tonnes": bal.get("facility_residual_t"),
        "residual_uncertainty_tonnes": bal.get("facility_residual_sigma_t"),
        "estimated_physical_loss_tonnes": att.get("estimated_physical_loss_tonnes"),
        "physical_loss_interval_tonnes": att.get("physical_loss_interval_tonnes"),
        "residual_decomposition_tonnes": att.get("decomposition_tonnes"),
        "category_probabilities": att.get("category_probabilities"),
        "candidate_hypotheses": hyps,
        "recommended_check": ({"action_id": rc["action_id"], "action": rc["action"], "target": rc["target"],
                               "description": rc["description"], "reason": rc["rationale"],
                               "expected_information_gain_bits": rc["expected_information_gain_bits"],
                               "cost": rc["cost"], "duration_h": rc["duration_h"],
                               "requires_isolation": rc["requires_isolation"]} if rc else None),
        "alternative_checks": [{"action_id": a["action_id"], "expected_information_gain_bits":
                                a["expected_information_gain_bits"], "available": a["available"]}
                               for a in (state.get("ranked_tests") or [])[1:5]],
        "verifications": state.get("verifications", []),
        "conflicts": state.get("conflicts", []),
        "missing_evidence": state.get("missing_evidence", []),
        "requires_engineer_approval": True,
        "model_versions": state.get("model_versions", {}),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    evidence = build_evidence_register(state)
    rep["evidence"] = evidence
    narrative = template_narrative(rep, evidence)
    rep["narrative_source"] = "template"
    rep["grounding"] = {"checked": False}
    if llm is not None and getattr(llm, "name", "none") != "none":
        payload = {k: v for k, v in rep.items() if k not in ("generated_at",)}
        res = llm.narrate(payload)
        if res.text:
            ok, problems = check_grounding(res.text, payload)
            rep["grounding"] = {"checked": True, "passed": ok, "problems": problems[:10], "provider": res.provider,
                                "model": res.model}
            if ok:
                narrative = res.text
                rep["narrative_source"] = f"llm:{res.model}"
        else:
            rep["grounding"] = {"checked": False, "provider": res.provider, "error": res.error}
    rep["narrative"] = narrative
    rep["markdown"] = to_markdown(rep)
    return rep


def to_markdown(rep: dict[str, Any]) -> str:
    L = [f"# Investigation report {rep['investigation_id']}", "",
         f"**Status:** {rep['status']}  |  **Diagnosis:** {rep.get('diagnosis_status')}  |  "
         f"**Engineer approval required:** yes", "",
         f"**Period:** {rep['period']['start']} - {rep['period']['end']}", "",
         "## Summary", "", rep.get("narrative", ""), "", "## Balance", "",
         f"- Observed facility residual: {rep['observed_residual_tonnes']} t "
         f"(1-sigma {rep['residual_uncertainty_tonnes']} t)",
         f"- Estimated physical loss: {rep['estimated_physical_loss_tonnes'] if rep['estimated_physical_loss_tonnes'] is not None else 'not supported by current evidence'}",
         ]
    d = rep.get("residual_decomposition_tonnes") or {}
    if d:
        L.append(f"- Residual decomposition R = L + B + U + e: R={d.get('total_residual_R')}, "
                 f"L={d.get('L_physical_loss')}, B={d.get('B_measurement_bias')}, "
                 f"U={d.get('U_unrecorded_movement')}, e={d.get('epsilon')}")
    L += ["", "## Candidate causes", "", "| # | Cause | Probability | Magnitude |", "|---|---|---|---|"]
    for i, h in enumerate(rep["candidate_hypotheses"], 1):
        mag = ", ".join(f"{k}={v}" for k, v in (h.get("magnitude") or {}).items() if v is not None)
        L.append(f"| {i} | {h['label']} | {h['calibrated_probability']} | {mag} |")
    rc = rep.get("recommended_check")
    L += ["", "## Recommended verification", ""]
    if rc:
        L.append(f"**{rc['description']}** (`{rc['action_id']}`) - {rc['reason']}. Expected information gain "
                 f"{rc['expected_information_gain_bits']} bits; cost {rc['cost']}; duration {rc['duration_h']} h.")
    else:
        L.append("None - see status.")
    if rep.get("verifications"):
        L += ["", "## Verification history", ""]
        for v in rep["verifications"]:
            L.append(f"- {v['action_id']}: **{v['outcome']}** {v.get('detail') or ''}")
    L += ["", "## Evidence register", "", "| ID | Type | Source | Summary |", "|---|---|---|---|"]
    for e in rep["evidence"]:
        L.append(f"| {e['id']} | {e['type']} | {e['source']} | {e['summary']} |")
    L += ["", f"_Narrative source: {rep['narrative_source']}. Generated {rep['generated_at']}._"]
    return "\n".join(L)


def to_html(rep: dict[str, Any]) -> str:
    md = rep.get("markdown") or to_markdown(rep)
    body = []
    in_table = False
    for line in md.split("\n"):
        if line.startswith("|"):
            cells = [html.escape(c.strip()) for c in line.strip("|").split("|")]
            if set("".join(cells)) <= set("-: "):
                continue
            if not in_table:
                body.append("<table>")
                in_table = True
                body.append("<tr>" + "".join(f"<th>{c}</th>" for c in cells) + "</tr>")
            else:
                body.append("<tr>" + "".join(f"<td>{c}</td>" for c in cells) + "</tr>")
            continue
        if in_table:
            body.append("</table>")
            in_table = False
        if line.startswith("# "):
            body.append(f"<h1>{html.escape(line[2:])}</h1>")
        elif line.startswith("## "):
            body.append(f"<h2>{html.escape(line[3:])}</h2>")
        elif line.startswith("- "):
            body.append(f"<li>{html.escape(line[2:])}</li>")
        elif line.strip():
            body.append(f"<p>{html.escape(line).replace('**', '')}</p>")
    if in_table:
        body.append("</table>")
    css = ("body{font-family:system-ui,sans-serif;max-width:960px;margin:24px auto;padding:0 16px;color:#111}"
           "table{border-collapse:collapse;width:100%;font-size:13px}td,th{border:1px solid #ccc;padding:4px 6px;"
           "text-align:left}h1{font-size:22px}h2{font-size:17px;margin-top:24px}")
    return f"<!doctype html><html><head><meta charset='utf-8'><title>{rep['investigation_id']}</title>" \
           f"<style>{css}</style></head><body>{''.join(body)}</body></html>"
