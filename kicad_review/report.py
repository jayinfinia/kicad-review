from __future__ import annotations

import json
from collections import Counter

from .checks import SEVERITIES, Finding
from .netlist import Design

_ICON = {"error": "🔴", "warning": "🟠", "info": "🔵"}


def to_markdown(d: Design, findings: list[Finding], summary: str | None = None, meta: dict | None = None) -> str:
    n_ics = sum(1 for c in d.components.values() if c.prefix in ("U", "IC"))
    counts = Counter(f.severity for f in findings)
    out = [f"# Design review: {d.name}", "",
           f"{len(d.components)} components ({n_ics} ICs), {len(d.nets)} nets. "
           + " · ".join(f"{_ICON[s]} {counts.get(s, 0)} {s}" for s in SEVERITIES), ""]
    if meta:
        out += ["_" + ", ".join(f"{k}: {v}" for k, v in meta.items()) + "_", ""]
    if summary:
        out += ["## Summary", "", summary, ""]
    out += ["## Findings", ""]
    if not findings:
        out.append("No issues found.")
    out += ["| # | Sev | Category | Source | Finding | Refs | Nets |", "|---|---|---|---|---|---|---|"]
    for i, f in enumerate(findings, 1):
        msg = f.message.replace("|", "\\|")
        out.append(f"| {i} | {_ICON[f.severity]} {f.severity} | {f.category} | {f.source}:{f.rule} | {msg} | "
                   f"{', '.join(f.refs)} | {', '.join(f.nets)} |")
    notes = [(i, f) for i, f in enumerate(findings, 1) if f.rationale]
    if notes:
        out += ["", "## Notes", ""]
        out += [f"{i}. {f.rationale}" for i, f in notes]
    out += ["", "---", "_Netlist-level review: placement, trace length and decoupling-cap proximity are not checked._"]
    return "\n".join(out) + "\n"


def to_json(d: Design, findings: list[Finding], summary: str | None = None, meta: dict | None = None) -> str:
    return json.dumps({"design": d.name, "summary": summary, "meta": meta or {},
                       "findings": [f.to_dict() for f in findings]}, indent=2)
