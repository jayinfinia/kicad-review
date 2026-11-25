"""Checks built on the DC bias solver, the part database, and circuit topology.

None of these call an LLM:
  * bias checks: compare each pin's solved idle voltage with what the pin needs
    (active-low inputs idle high, no pin above its I/O abs-max, required straps)
  * part-database checks: datasheet requirements per part number
  * topology checks: LED current limiting, crystal load capacitors
"""
from __future__ import annotations

import re

from . import dcbias, parts
from .analysis import (is_capacitor, is_connector, is_ground, is_ic, is_power, is_resistor, is_unconnected,
                       other_pin, parse_value, rail_voltage)
from .checks import Finding
from .netlist import Component, Design, Pin

ACTIVE_LOW = re.compile(r"(^~|^/|^!|^n[A-Z]|^N(RST|RESET|CS|OE|WE|EN|IRQ|INT|MI|WR|RD|SS|PD)|#$|_[Nn]$|_B$|-$|^N_)")
ANALOG_PIN = re.compile(r"(ADC|AIN|VREF|REF|FB|SENSE|REXT|ISET|RSET|COMP|OSC|XTAL|XI|XO|CLKIN|IN[+-]|^[+-]$|BIAS|"
                        r"CAP|TRIM|ADJ|THR|TRIG|DIS|CV|HV|ANA)", re.I)
_REG_IN = re.compile(r"^(V?CC5|VIN|VBUS|IN|VI|5V\w*|VCC_?5V?|VBAT|HV)$", re.I)
_CORE = re.compile(r"(CORE|INT|18|1V8|1V2|12$|PLL|VCAP|DD_?1V|VDDA)", re.I)


def _primary_name(p: Pin) -> str:
    return (p.name or "").replace("~{", "~").replace("}", "")


def io_voltage(d: Design, c: Component) -> tuple[float | None, str]:
    """(max safe voltage on a signal pin, explanation)."""
    entry = parts.lookup(c) or {}
    if "io_voltage_max" in entry:
        return entry["io_voltage_max"], f"{c.value} abs max {entry['io_voltage_max']:g} V (parts DB)"
    rails = []
    for p in c.pins.values():
        if p.base_type != "power_in" or not is_power(p.net):
            continue
        v = rail_voltage(p.net)
        if v is None or v <= 0:
            continue
        nm = p.name or ""
        if entry.get("io_rail_pins"):
            if parts.pin_matches(entry["io_rail_pins"], nm):
                rails.append(v)
        elif not _REG_IN.match(nm) and not _CORE.search(nm):
            rails.append(v)
    if not rails:
        return None, ""
    margin = entry.get("io_relative", 0.3)
    vio = max(rails)
    src = "parts DB" if entry else "inferred from supply pins"
    return vio + margin, f"{c.ref} I/O rail {vio:g} V + {margin:g} V ({src})"


def _caps_to_gnd(d: Design, net: str, ac_ground: bool = False) -> list[tuple[str, float | None]]:
    """Caps from `net` to ground (or, with ac_ground, to any supply rail: same thing at AC)."""
    out = []
    for p in d.pins_on(net):
        c = d.components.get(p.ref)
        if c and is_capacitor(c):
            o = other_pin(c, p)
            if o and (is_ground(o.net) or (ac_ground and is_power(o.net))):
                out.append((c.ref, parse_value(c.value)))
    return out


def check_bias(d: Design, bias: dict[str, dcbias.Bias]) -> list[Finding]:
    out = []
    for c in d.components.values():
        if not is_ic(c):
            continue
        entry = parts.lookup(c) or {}
        vmax, why = io_voltage(d, c)
        flagged_nets = set()
        for p in c.pins.values():
            if p.base_type in ("power_in", "power_out", "passive", "no_connect") or p.no_connect:
                continue
            if not p.net or is_unconnected(p.net) or is_power(p.net) or is_ground(p.net):
                continue
            b = bias.get(p.net)
            if b is None or b.volts is None:
                continue
            # Over-voltage on a signal pin: holds whenever the net is released (open-drain, tri-state, input).
            if vmax is not None and b.volts > vmax and not b.assumed and p.net not in flagged_nets \
                    and not ANALOG_PIN.search(p.name or ""):
                flagged_nets.add(p.net)
                out.append(Finding("bias-overvoltage", "pullup", "error",
                                   f"{p.label()} idles at {b.describe()} on {p.net}, above its {vmax:g} V limit "
                                   f"({why})", refs=[c.ref] + _bias_resistors(d, p.net), nets=[p.net],
                                   rationale="DC nodal solve of the resistor network"))
            elif b.volts < -0.3 and not ANALOG_PIN.search(p.name or ""):
                out.append(Finding("bias-negative", "pullup", "error",
                                   f"{p.label()} idles at {b.describe()} on {p.net}: below ground",
                                   refs=[c.ref] + _bias_resistors(d, p.net), nets=[p.net]))
            # Active-low input held asserted by the network with nothing to drive it high.
            # Active-low intent from the pin name, or from a designer-named net (/RCON-, /RESET#, ~{CS}).
            named_net = not p.net.rsplit("/", 1)[-1].startswith("Net-(")
            active_low = ACTIVE_LOW.search(_primary_name(p)) or \
                (named_net and ACTIVE_LOW.search(p.net.rsplit("/", 1)[-1]) and not p.net.rsplit("/", 1)[-1].startswith("/"))
            if p.base_type == "input" and not b.driven and active_low \
                    and not parts.pin_matches(entry.get("internal_pullups"), p.name):
                vref = (vmax - entry.get("io_relative", 0.3)) if vmax else (b.volts if b.volts > 0 else 3.3)
                if b.volts < 0.3 * vref:
                    out.append(Finding("active-low-asserted", "pullup", "error",
                                       f"Active-low input {p.label()} idles at {b.describe()} on {p.net}: "
                                       "held permanently asserted", refs=[c.ref] + _bias_resistors(d, p.net),
                                       nets=[p.net], rationale="DC nodal solve of the resistor network"))
                elif b.volts < 0.7 * vref:
                    out.append(Finding("active-low-indeterminate", "pullup", "warning",
                                       f"Active-low input {p.label()} idles at {b.describe()} on {p.net}: "
                                       "between logic thresholds", refs=[c.ref] + _bias_resistors(d, p.net),
                                       nets=[p.net]))
    return out


def _bias_resistors(d: Design, net: str) -> list[str]:
    return sorted({p.ref for p in d.pins_on(net) if is_resistor(d.components[p.ref])})


def check_part_requirements(d: Design, bias: dict[str, dcbias.Bias]) -> list[Finding]:
    out = []
    for c in d.components.values():
        entry = parts.lookup(c)
        if not entry:
            continue
        for req in entry.get("require", []):
            for p in c.pins.values():
                if not parts.pin_matches(req["pin"], p.name):
                    continue
                rule, why = req["rule"], req["why"]
                floating = is_unconnected(p.net) or (p.net in d.nets and len(d.nets[p.net].nodes) == 1
                                                     and not is_power(p.net) and not is_ground(p.net))
                tag = f"{p.label()} ({c.value}): {why}"
                if rule == "connected" and floating:
                    out.append(Finding("part-pin-unconnected", "connectivity", req.get("severity", "error"), f"{tag}; pin is unconnected",
                                       refs=[c.ref], nets=[p.net] if p.net else [], rationale=entry["source"]))
                elif rule in ("high", "low"):
                    b = bias.get(p.net) if p.net else None
                    if floating or b is None:
                        if not parts.pin_matches(entry.get("internal_pullups"), p.name) or rule == "low":
                            out.append(Finding("part-strap", "pullup", "error", f"{tag}; pin is floating",
                                               refs=[c.ref], nets=[p.net] if p.net else [], rationale=entry["source"]))
                        continue
                    v = b.volts
                    if v is None or (b.driven and not b.fixed):
                        continue
                    vmax, _ = io_voltage(d, c)
                    vref = (vmax - entry.get("io_relative", 0.3)) if vmax else 3.3
                    hi = v > 0.5 * vref
                    if is_ground(p.net):
                        hi = False
                    elif is_power(p.net):
                        hi = True
                    if (rule == "high") != hi:
                        out.append(Finding("part-strap", "pullup", "error",
                                           f"{tag}; pin idles {'high' if hi else 'low'} ({b.describe()})",
                                           refs=[c.ref] + _bias_resistors(d, p.net), nets=[p.net],
                                           rationale=entry["source"]))
                elif rule == "cap_to_gnd" and not floating and p.net:
                    caps = _caps_to_gnd(d, p.net, ac_ground=True)
                    total = sum(v for _, v in caps if v)
                    need = req.get("min_uF", 0) * 1e-6
                    if not caps:
                        out.append(Finding("part-missing-cap", "decoupling", "error", f"{tag}; no capacitor to ground "
                                           f"on {p.net}", refs=[c.ref], nets=[p.net], rationale=entry["source"]))
                    elif need and total and total < 0.9 * need:
                        out.append(Finding("part-cap-too-small", "decoupling", "warning",
                                           f"{tag}; only {total * 1e6:g} µF on {p.net} "
                                           f"({', '.join(r for r, _ in caps)}), needs {req['min_uF']:g} µF",
                                           refs=[c.ref] + [r for r, _ in caps], nets=[p.net],
                                           rationale=entry["source"]))
        od = entry.get("open_drain")
        if od:
            for p in c.pins.values():
                if parts.pin_matches(od, p.name) and p.net and not is_unconnected(p.net):
                    b = bias.get(p.net)
                    if b is None or b.volts is None or b.volts < 1.0:
                        out.append(Finding("open-drain-no-pullup", "pullup", "error",
                                           f"{p.label()} ({c.value}) is open-drain but {p.net} has no pull-up "
                                           f"({b.describe() if b else 'floating'})", refs=[c.ref], nets=[p.net],
                                           rationale=entry["source"]))
    return out


# --------------------------------------------------------------------------- topology

def _is_led(c: Component) -> bool:
    tokens = re.split(r"[_\-\s]", (c.part or "").upper())
    return "LED" in tokens or c.prefix == "LED" or c.ref.upper().startswith("LED")


def check_leds(d: Design) -> list[Finding]:
    """An LED needs something to limit its current: a resistor in series, or a driver
    that limits current (transistor, constant-current IC). Flag LEDs whose path from a
    supply or IC pin to ground/IC pin contains no resistor at all."""
    out = []
    for c in d.components.values():
        if not _is_led(c) or len(c.pins) != 2:
            continue
        nets = [p.net for p in c.pins.values()]
        if any(n is None or is_unconnected(n) for n in nets):
            continue
        # Only the signal side can hold a series limiter: resistors hanging off GND/VCC elsewhere don't count.
        limited = False
        for n in nets:
            if is_power(n) or is_ground(n):
                continue
            for q in d.pins_on(n):
                comp = d.components.get(q.ref)
                if comp is None or comp is c:
                    continue
                if is_resistor(comp) or comp.prefix in ("RN", "RR"):
                    limited = True
                if is_ic(comp) and re.search(r"(LED|ISET|IOUT|SINK|DRV)", q.name or "", re.I):
                    limited = True  # constant-current LED driver output
        if limited:
            continue
        sources = [q for n in nets for q in d.pins_on(n) if q.ref != c.ref and d.components.get(q.ref)
                   and (is_ic(d.components[q.ref]) or d.components[q.ref].prefix in ("Q", "T"))]
        if sources or any(is_power(n) for n in nets):
            out.append(Finding("led-no-current-limit", "power", "error",
                               f"LED {c.ref} sits between {nets[0]} and {nets[1]} with no series resistor or current "
                               "driver: current is limited only by the driving pin", refs=[c.ref], nets=nets))
    return out


def check_crystals(d: Design) -> list[Finding]:
    out = []
    for c in d.components.values():
        if not (c.prefix in ("Y", "X", "XTAL", "Q") and re.search(r"crystal|xtal", c.part + c.description, re.I)):
            continue
        sig = [p for p in c.pins.values() if p.net and not is_ground(p.net) and not is_unconnected(p.net)]
        if len(sig) != 2:
            continue
        ic_entries = [parts.lookup(d.components[q.ref]) or {} for p in sig for q in d.pins_on(p.net)
                      if is_ic(d.components[q.ref])]
        if any(e.get("internal_load_caps") for e in ic_entries):
            continue
        # Any capacitor on either crystal net (to ground, across, or a series trimmer) counts as a load network.
        has_cap = any(is_capacitor(d.components[q.ref]) for p in sig for q in d.pins_on(p.net))
        missing = [p.net for p in sig]
        if not has_cap:
            out.append(Finding("crystal-no-load-caps", "other", "warning",
                               f"Crystal {c.ref} ({c.value}) has no load capacitors to ground on {' or '.join(missing)}; "
                               "frequency will be off unless the oscillator has internal load caps",
                               refs=[c.ref], nets=missing))
    return out


def run(d: Design) -> list[Finding]:
    bias = dcbias.solve(d)
    return check_bias(d, bias) + check_part_requirements(d, bias) + check_leds(d) + check_crystals(d)
