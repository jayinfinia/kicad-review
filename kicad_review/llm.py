"""LLM review layer: Claude reads a compact text rendering of the netlist plus
the rule findings, triages the rule findings (confirm / dismiss with a reason)
and adds issues that need part knowledge the rules don't have (datasheet pin
requirements, regulator caps, crystal load caps, LED current limiting...)."""
from __future__ import annotations

import os
from typing import Literal

from pydantic import BaseModel, Field

from .analysis import is_capacitor, is_ground, is_ic, is_power, is_resistor, is_unconnected
from .checks import Finding
from .netlist import Design

DEFAULT_MODEL = os.environ.get("KICAD_REVIEW_MODEL", "claude-opus-5-5")

SYSTEM = """You are a senior hardware engineer doing a schematic design review from a KiCad netlist.
You see components (reference, value, library part, description, MPN when present) and every net with
the pins on it (REF.PIN/PINNAME[type]). Placement and routing are not available.

Do two things:
1. Triage each automated rule finding: confirm it, or dismiss it with a concrete reason (for example
   the "missing" pull-up is internal to the part, or the open-drain pin is wired to something that
   drives it). Do not dismiss a finding just because it is minor.
2. Report additional issues the rules could not find. Focus on what would make a board fail or be
   unreliable: missing or wrong decoupling and bulk capacitance, regulator input/output capacitors,
   floating inputs and enable/reset pins, wrong or missing pull-ups/pull-downs, power rail mistakes
   (wrong voltage to a part, swapped supply/ground, a rail with no source), crystal load capacitors,
   LEDs without current limiting, reversed polarity, pin-function mistakes you can identify from the
   part number. Use your knowledge of the named parts, and say when a claim depends on a datasheet
   detail you are not certain of.

Rules: cite exact reference designators and net names from the netlist. Do not invent parts or nets.
Prefer a short list of real problems over a long list of generic advice; skip style nits and
"consider adding" suggestions unless the risk is concrete. Set confidence honestly."""


class RuleVerdict(BaseModel):
    index: int = Field(description="index of the rule finding in the list given")
    verdict: Literal["confirm", "dismiss"]
    reason: str


class LLMFinding(BaseModel):
    category: Literal["decoupling", "connectivity", "pullup", "power", "other"]
    severity: Literal["error", "warning", "info"]
    refs: list[str]
    nets: list[str]
    message: str = Field(description="one-sentence statement of the problem")
    rationale: str = Field(description="why it is a problem and what to change")
    confidence: float = Field(description="0 to 1")


class Review(BaseModel):
    summary: str = Field(description="2-4 sentence overview of the design and its overall health")
    rule_verdicts: list[RuleVerdict]
    findings: list[LLMFinding]


def render_design(d: Design, max_net_pins: int = 40) -> str:
    lines = [f"# Design: {d.name}", "", "## Components"]
    for c in sorted(d.components.values(), key=lambda c: (c.prefix, len(c.ref), c.ref)):
        extra = []
        for k in ("MPN", "Voltage", "Dielectric", "Tolerance"):
            if c.fields.get(k):
                extra.append(f"{k}={c.fields[k]}")
        desc = f" — {c.description}" if c.description else ""
        lines.append(f"{c.ref}: {c.value} [{c.lib}:{c.part}]{desc}" + (f" ({', '.join(extra)})" if extra else ""))
    lines += ["", "## Nets (REF.PIN/NAME[type])"]
    for n, net in sorted(d.nets.items(), key=lambda kv: (not (is_power(kv[0]) or is_ground(kv[0])), kv[0])):
        if is_unconnected(n):
            continue
        pins = [f"{p.label()}[{p.type}]" for p in net.nodes]
        if len(pins) > max_net_pins:
            # Huge rails (GND, VCC): keep the IC pins, summarise passives.
            ic_pins = [f"{p.label()}[{p.type}]" for p in net.nodes
                       if d.components.get(p.ref) and is_ic(d.components[p.ref])]
            passives = sorted({p.ref for p in net.nodes if d.components.get(p.ref)
                               and (is_capacitor(d.components[p.ref]) or is_resistor(d.components[p.ref]))})
            pins = ic_pins + [f"+ passives: {' '.join(passives)}",
                              f"+ {len(net.nodes) - len(ic_pins) - len(passives)} other pins"]
        lines.append(f"{n}: " + ", ".join(pins))
    from . import dcbias
    biased = [(n, b) for n, b in sorted(dcbias.solve(d).items())
              if not b.fixed and b.volts is not None and not is_unconnected(n)]
    if biased:
        lines += ["", "## Solved DC idle voltage of resistor-biased nets (nodal analysis; 'driven' = an output can override)"]
        lines += [f"{n}: {b.describe()}{' driven' if b.driven else ''}" for n, b in biased]
    unc = [p for c in d.components.values() for p in c.pins.values() if is_unconnected(p.net)]
    if unc:
        lines += ["", "## Pins with no connection",
                  ", ".join(f"{p.label()}[{p.type}]" for p in unc)]
    return "\n".join(lines)


def render_findings(findings: list[Finding]) -> str:
    if not findings:
        return "(no rule findings)"
    return "\n".join(f"[{i}] {f.severity} {f.rule}: {f.message}" for i, f in enumerate(findings))


def review(d: Design, rule_findings: list[Finding], model: str = DEFAULT_MODEL,
           effort: str = "high", client=None) -> tuple[Review, dict]:
    import anthropic

    client = client or anthropic.Anthropic()
    user = (render_design(d) + "\n\n## Automated rule findings\n" + render_findings(rule_findings)
            + "\n\nReview this design.")
    kwargs = dict(
        model=model,
        max_tokens=16000,
        system=SYSTEM,
        messages=[{"role": "user", "content": user}],
        output_format=Review,
        output_config={"effort": effort},
    )
    # Server-side refusal fallback (beta) for the current Opus/Sonnet models: if a safety
    # classifier declines, the API retries on a fallback model inside the same call.
    if model in ("claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5", "claude-fable-5-2", "claude-fable-5-1"):
        resp = client.beta.messages.parse(betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kwargs)
    else:
        resp = client.messages.parse(**kwargs)
    if resp.stop_reason == "refusal":
        raise RuntimeError(f"model declined the request: {getattr(resp, 'stop_details', None)}")
    if resp.stop_reason == "max_tokens" or resp.parsed_output is None:
        raise RuntimeError(f"no structured review returned (stop_reason={resp.stop_reason})")
    usage = {"model": resp.model, "input_tokens": resp.usage.input_tokens,
             "output_tokens": resp.usage.output_tokens}
    return resp.parsed_output, usage


def merge(rule_findings: list[Finding], rv: Review) -> list[Finding]:
    """Rule findings the LLM dismissed are kept but downgraded to info with its reason."""
    verdicts = {v.index: v for v in rv.rule_verdicts}
    out = []
    for i, f in enumerate(rule_findings):
        v = verdicts.get(i)
        if v and v.verdict == "dismiss":
            f = Finding(**{**f.to_dict(), "severity": "info", "rationale": f"LLM dismissed: {v.reason}"})
        elif v:
            f = Finding(**{**f.to_dict(), "rationale": v.reason})
        out.append(f)
    for lf in rv.findings:
        out.append(Finding(rule="llm", category=lf.category, severity=lf.severity, message=lf.message,
                           refs=lf.refs, nets=lf.nets, source="llm",
                           rationale=f"{lf.rationale} (confidence {lf.confidence:.2f})"))
    return out
