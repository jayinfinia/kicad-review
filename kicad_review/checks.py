"""Deterministic design-review rules over the netlist model.

Each rule returns `Finding`s. Rules are deliberately conservative where KiCad's
netlist loses information (no placement, so "decoupling" means "a capacitor
between this rail and ground exists", not "it is close to the pin").
"""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import asdict, dataclass, field

from .analysis import (cap_voltage_rating, is_connector, fmt_value, ground_pin_name, ic_supply_rails, is_capacitor,
                       is_ground, is_ic, is_power, is_resistor, is_unconnected, leaf, other_pin,
                       parse_value, rail_voltage, supply_pin_name)
from .netlist import Design

SEVERITIES = ("error", "warning", "info")


@dataclass
class Finding:
    rule: str            # stable id, e.g. "missing-decoupling"
    category: str        # decoupling | connectivity | pullup | power
    severity: str        # error | warning | info
    message: str
    refs: list[str] = field(default_factory=list)
    nets: list[str] = field(default_factory=list)
    source: str = "rules"  # rules | llm
    rationale: str = ""

    def to_dict(self):
        return asdict(self)


# --------------------------------------------------------------------------- connectivity

def check_unconnected_pins(d: Design) -> list[Finding]:
    out = []
    for c in d.components.values():
        floating_inputs, floating_power = [], []
        for p in c.pins.values():
            if p.no_connect:
                continue
            # A pin alone on a *named* rail is attached to a power symbol: that's connected.
            alone = is_unconnected(p.net) or (p.net in d.nets and len(d.nets[p.net].nodes) == 1
                                              and not is_power(p.net) and not is_ground(p.net))
            if not alone:
                continue
            if p.base_type == "power_in":
                floating_power.append(p)
            elif p.base_type == "input":
                floating_inputs.append(p)
        if floating_power:
            out.append(Finding("unconnected-power-pin", "connectivity", "error",
                               f"{c.ref} ({c.value}) power pin(s) not connected: "
                               + ", ".join(p.label() for p in floating_power),
                               refs=[c.ref]))
        if floating_inputs:
            out.append(Finding("floating-input", "connectivity", "error" if is_ic(c) else "warning",
                               f"{c.ref} ({c.value}) input pin(s) left floating (no net, no no-connect flag): "
                               + ", ".join(p.label() for p in floating_inputs),
                               refs=[c.ref]))
    # Nets where every pin is an input: nothing drives them (e.g. a strap whose pull-up is missing).
    for n, net in d.nets.items():
        if is_unconnected(n) or is_power(n) or is_ground(n) or len(net.nodes) < 2:
            continue
        if all(p.base_type == "input" for p in net.nodes):
            out.append(Finding("undriven-input-net", "connectivity", "error",
                               f"Net {n} connects only inputs ({', '.join(p.label() for p in net.nodes)}); "
                               "nothing drives or biases it",
                               refs=sorted({p.ref for p in net.nodes}), nets=[n]))
    return out


def check_power_pin_nets(d: Design) -> list[Finding]:
    """Supply pin on ground, ground pin on a supply, or supply pin on an unnamed signal net."""
    out = []
    for c in d.components.values():
        for p in c.pins.values():
            if p.base_type != "power_in" or is_unconnected(p.net):
                continue
            name = p.name or ""
            if supply_pin_name(name) and is_ground(p.net):
                out.append(Finding("supply-pin-on-ground", "power", "error",
                                   f"{p.label()} looks like a supply pin but is tied to ground net {p.net}",
                                   refs=[c.ref], nets=[p.net]))
            elif ground_pin_name(name) and is_power(p.net):
                out.append(Finding("ground-pin-on-supply", "power", "error",
                                   f"{p.label()} looks like a ground pin but is tied to supply net {p.net}",
                                   refs=[c.ref], nets=[p.net]))
    return out


# --------------------------------------------------------------------------- power rails

def check_rails(d: Design) -> list[Finding]:
    out = []
    # 1. Two differently named rails at the same voltage (+3V3 vs +3.3V): likely a split rail.
    by_v = defaultdict(set)
    for n in d.nets:
        v = rail_voltage(n)
        if v is not None and is_power(n):
            by_v[v].add(n)
    for v, names in by_v.items():
        leaves = {leaf(n) for n in names}
        if len(leaves) > 1:
            out.append(Finding("split-rail", "power", "warning",
                               f"Rails {', '.join(sorted(names))} all look like {v:g} V but are separate nets; "
                               "if they should be the same supply, one side is not powered",
                               nets=sorted(names)))
    # 2. Supply net with a single connection (typo'd power label, or dead rail).
    for n, net in d.nets.items():
        if is_power(n) and len(net.nodes) == 1:
            p = net.nodes[0]
            c = d.components.get(p.ref)
            if c and is_connector(c):
                continue  # unused supply pin on a connector/bus edge: normal
            out.append(Finding("single-pin-rail", "power", "warning",
                               f"Supply net {n} connects only to {p.label()}; check for a misspelled power label",
                               refs=[p.ref], nets=[n]))
    # 3. Capacitor voltage rating below the rail it sits on.
    for c in d.components.values():
        if not is_capacitor(c):
            continue
        rating = cap_voltage_rating(c)
        if rating is None:
            continue
        rails = [p.net for p in c.pins.values() if p.net and rail_voltage(p.net) is not None]
        for r in rails:
            v = abs(rail_voltage(r))
            if v > rating:
                out.append(Finding("cap-overvoltage", "power", "error",
                                   f"{c.ref} ({c.value}) rated {rating:g} V sits on {r} ({v:g} V)",
                                   refs=[c.ref], nets=[r]))
            elif v > 0.8 * rating and v >= 5:
                out.append(Finding("cap-low-derating", "power", "info",
                                   f"{c.ref} rated {rating:g} V on {r} ({v:g} V): under 20% margin "
                                   "(ceramic caps also lose capacitance under DC bias)",
                                   refs=[c.ref], nets=[r]))
    # 4. IC supply pin on a net that is neither a named rail nor driven by any power output.
    for n, net in d.nets.items():
        if is_power(n) or is_ground(n) or is_unconnected(n):
            continue
        consumers = [p for p in net.nodes if p.base_type == "power_in"]
        if not consumers:
            continue
        drivers = [p for p in net.nodes if p.base_type in ("power_out", "output", "passive", "bidirectional", "tri_state")]
        if not drivers:
            out.append(Finding("undriven-supply", "power", "error",
                               f"Supply pin(s) {', '.join(p.label() for p in consumers)} on net {n} "
                               "with nothing that could supply it",
                               refs=sorted({p.ref for p in consumers}), nets=[n]))
    return out


# --------------------------------------------------------------------------- decoupling

def _decoupling_caps(d: Design, rail: str) -> list[str]:
    caps = []
    for p in d.pins_on(rail):
        c = d.components.get(p.ref)
        if c and is_capacitor(c):
            o = other_pin(c, p)
            if o and is_ground(o.net):
                caps.append(c.ref)
    return sorted(set(caps))


def check_decoupling(d: Design) -> list[Finding]:
    out = []
    rail_ics = defaultdict(list)
    for c in d.components.values():
        if not is_ic(c):
            continue
        for rail in ic_supply_rails(d, c):
            rail_ics[rail].append(c.ref)
    for rail, ics in sorted(rail_ics.items()):
        caps = _decoupling_caps(d, rail)
        if not caps:
            out.append(Finding("missing-decoupling", "decoupling", "error",
                               f"No capacitor from {rail} to ground; IC(s) {', '.join(sorted(ics))} have no decoupling "
                               "on this rail",
                               refs=sorted(ics), nets=[rail]))
        elif len(caps) < len(set(ics)):
            out.append(Finding("thin-decoupling", "decoupling", "warning",
                               f"{rail} feeds {len(set(ics))} ICs ({', '.join(sorted(set(ics)))}) but has only "
                               f"{len(caps)} capacitor(s) to ground ({', '.join(caps)}); expect at least one per IC",
                               refs=sorted(set(ics)) + caps, nets=[rail]))
    return out


# --------------------------------------------------------------------------- pull-ups

_I2C_RE = re.compile(r"(^|[^A-Z])(SDA|SCL|I2C_?SDA|I2C_?SCL|SDA\d|SCL\d)([^A-Z]|$)", re.I)
_ACTIVE_LOW_RE = re.compile(r"(^~|^/|^!|^n[A-Z]|^N(RST|RESET|CS|OE|WE|EN|IRQ|INT|MI|WR|RD|SS|PD)|#$|_[Nn]$|_B$|-$|^N_)")
_RESET_RE = re.compile(r"^[~/!]?\{?N?(RESET|RST|MCLR)(_?N|#|_B)?\}?($|[/_ ])", re.I)


def _resistors_to(d: Design, net: str):
    """Yield (resistor ref, value, net at the far end) for each 2-pin resistor on `net`."""
    for p in d.pins_on(net):
        c = d.components.get(p.ref)
        if c and is_resistor(c):
            o = other_pin(c, p)
            yield c.ref, parse_value(c.value), (o.net if o else None)


def _is_i2c_net(d: Design, net: str) -> bool:
    if _I2C_RE.search(leaf(net)):
        return True
    return any(_I2C_RE.fullmatch(p.name or "") or (p.name or "").upper() in ("SDA", "SCL")
               for p in d.pins_on(net) if d.components.get(p.ref) and is_ic(d.components[p.ref]))


def check_pullups(d: Design) -> list[Finding]:
    out = []
    for n, net in d.nets.items():
        if is_power(n) or is_ground(n) or is_unconnected(n) or len(net.nodes) < 2:
            continue
        ups = [(r, v, far) for r, v, far in _resistors_to(d, n) if is_power(far)]
        downs = [(r, v, far) for r, v, far in _resistors_to(d, n) if is_ground(far)]
        ic_pins = [p for p in net.nodes if d.components.get(p.ref) and is_ic(d.components[p.ref])]

        if _is_i2c_net(d, n) and ic_pins:
            if not ups:
                msg = f"I2C line {n} has no pull-up resistor to a supply"
                if downs:
                    msg += f" ({', '.join(r for r, _, _ in downs)} goes to ground instead: pull-down, not pull-up)"
                out.append(Finding("i2c-missing-pullup", "pullup", "error", msg,
                                   refs=[r for r, _, _ in downs], nets=[n]))
            for r, v, far in ups:
                if v is not None and not (1e3 <= v <= 10e3):
                    out.append(Finding("i2c-pullup-value", "pullup", "warning",
                                       f"I2C pull-up {r} on {n} is {fmt_value(v)}Ω; typical range is 1k–10k "
                                       "(too high: slow edges; too low: exceeds 3 mA sink)",
                                       refs=[r], nets=[n]))
        # Pull-up to a too-high rail and active-low pins held low: see checks_physics (DC bias solve).

        oc = [p for p in net.nodes if p.base_type == "open_collector"]
        if oc and not ups and not any(p.base_type in ("open_emitter", "output", "power_out") for p in net.nodes):
            out.append(Finding("open-drain-no-pullup", "pullup", "warning",
                               f"Open-drain/collector output(s) {', '.join(p.label() for p in oc)} on {n} "
                               "have no pull-up resistor",
                               refs=sorted({p.ref for p in oc}), nets=[n]))

        resets = [p for p in ic_pins if p.base_type == "input" and _RESET_RE.match(p.name or "")]
        if resets and not ups and not any(p.base_type in ("output", "open_collector", "power_out", "bidirectional", "tri_state")
                                           for p in net.nodes if p not in resets):
            if not any(d.components.get(p.ref) and d.components[p.ref].prefix in ("SW", "J", "P", "CONN") for p in net.nodes):
                out.append(Finding("reset-floating", "pullup", "warning",
                                   f"Reset input(s) {', '.join(p.label() for p in resets)} on {n} have no pull-up "
                                   "and no driver",
                                   refs=sorted({p.ref for p in resets}), nets=[n]))
    return out


ALL_CHECKS = [check_unconnected_pins, check_power_pin_nets, check_rails, check_decoupling, check_pullups]


def run_all(d: Design, physics: bool = True) -> list[Finding]:
    out = []
    for chk in ALL_CHECKS:
        out.extend(chk(d))
    if physics:
        from . import checks_physics
        out.extend(checks_physics.run(d))
    order = {s: i for i, s in enumerate(SEVERITIES)}
    return sorted(out, key=lambda f: (order[f.severity], f.category, f.rule, f.message))
