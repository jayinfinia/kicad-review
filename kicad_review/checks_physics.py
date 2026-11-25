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

ACTIVE_LOW = re.compile(r"(^~|^/|^!|^n[A-Z]|^N(RST|RESET|CS|OE|WE|EN|IRQ|INT|MI|WR|RD|SS|PD)|#$|_[Nn]$|_B$|-$|^N_|"
                        r"(RESET|RST|CS|EN|OE)X$|(^|_)(RESET|RST|NRST|XRES)$)")
ANALOG_PIN = re.compile(r"(ADC|AIN|VREF|REF|FB|SENSE|REXT|ISET|RSET|COMP|OSC|XTAL|XI|XO|CLKIN|IN[+-]|^[+-]$|BIAS|"
                        r"CAP|TRIM|ADJ|THR|TRIG|DIS|CV|HV|ANA)", re.I)
CONFIG_PIN = re.compile(r"(MODE|BOOT|CFG|CONF|SEL|STRAP|TEST|CLKMOD|^M[0-2]$|^EN$|ENABLE|ADDR|^A[0-2]$|"
                        r"WP|HOLD|PROG|INIT|PD$|PWDN|SLEEP|STBY|_EN$|^EN_|ENABLE|^TCK$|^TCK/|SWCLK|SWDCLK)", re.I)
# TCK/SWCLK are listed but not TMS/TDI/TRST: IEEE 1149.1 requires internal pull-ups on those, not on TCK.
_REG_IN = re.compile(r"^(V?CC5|VIN|VBUS|IN|VI|5V\w*|VCC_?5V?|VBAT|HV)$", re.I)
_CORE = re.compile(r"(CORE|INT|18|1V8|1V2|12$|PLL|VCAP|DD_?1V|VDDA)", re.I)


def _primary_name(p: Pin) -> str:
    return (p.name or "").replace("~{", "~").replace("}", "")


def io_voltage(d: Design, c: Component, bias: dict | None = None) -> tuple[float | None, str]:
    """(max safe voltage on a signal pin, explanation)."""
    entry = parts.lookup(c) or {}
    if "io_voltage_max" in entry:
        return entry["io_voltage_max"], f"{c.value} abs max {entry['io_voltage_max']:g} V (parts DB)"
    if bias is not None and not any(p.base_type == "power_in" and is_power(p.net) for p in c.pins.values()):
        # Old symbols type supply pins as input/passive and feed them through an RC or ferrite: use the
        # solved voltage of a VCC/VDD-named pin.
        from .analysis import supply_pin_name
        vs = [bias[p.net].volts for p in c.pins.values() if supply_pin_name(p.name or "") and p.net in bias
              and bias[p.net].volts and bias[p.net].volts > 0 and not bias[p.net].assumed]
        if vs:
            return max(vs) + 0.3, f"{c.ref} supply pin at {max(vs):g} V (solved) + 0.3 V"
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


def _offboard(d: Design, net: str) -> str:
    """Jumpers/connectors in the net's resistive neighbourhood, for 'with X open/unplugged' wording."""
    from .dcbias import is_jumper
    refs = sorted({p.ref for p in d.pins_on(net) if d.components.get(p.ref)
                   and (is_jumper(d.components[p.ref]) or is_connector(d.components[p.ref]))})
    return ", ".join(refs)


def _pin_findings(d: Design, c: Component, p: Pin, b: dcbias.Bias, entry: dict, vmax: float | None,
                  why: str, state: str) -> list[Finding]:
    """Bias findings for one pin in one board state ('' = as drawn, else e.g. ' with JP1 open')."""
    out = []
    sev = "error" if not state else "warning"
    rat = "DC nodal solve of the resistor network" + (f"; board state{state}" if state else "")
    others = b.driven_by_other(p)
    internal_pu = parts.pin_matches(entry.get("internal_pullups"), p.name)
    if b.volts is None:
        # Floating: no resistive path to any rail and nothing else drives it. Inputs with a capacitor on
        # the net are AC-coupled or charge-pump/timing pins, biased inside the part: skip them. On the
        # board-on-its-own pass, only configuration-type pins count (signal inputs from a cable are fine).
        # Any R/L/C on the net means a deliberate analog/bias network (often to another pin of the same part).
        ac = any(d.components.get(q.ref) and (is_capacitor(d.components[q.ref]) or is_resistor(d.components[q.ref])
                                               or d.components[q.ref].prefix in ("L", "FB", "RN", "RR", "RP"))
                 for q in d.pins_on(p.net) if q is not p)
        cfg = ACTIVE_LOW.search(_primary_name(p)) or CONFIG_PIN.search(_primary_name(p)) \
            or (not p.net.rsplit("/", 1)[-1].startswith("Net-(") and CONFIG_PIN.search(p.net.rsplit("/", 1)[-1]))
        if (p.base_type == "input" or (p.base_type == "bidirectional" and cfg)) and not others \
                and not internal_pu and not ac and not ANALOG_PIN.search(p.name or "") and (not state or cfg):
            out.append(Finding("floating-signal-input", "connectivity", sev,
                               f"{p.label()} on {p.net} floats{state}: no driver and no pull resistor",
                               refs=[c.ref], nets=[p.net], rationale=rat))
        return out
    named_net = not p.net.rsplit("/", 1)[-1].startswith("Net-(")
    leafname = p.net.rsplit("/", 1)[-1]
    pin_low = ACTIVE_LOW.search(_primary_name(p))
    net_low = named_net and ACTIVE_LOW.search(leafname)
    # Pin-name polarity applies to inputs and bidirectional pins (IRQ lines); polarity taken from the net
    # name only to true inputs (a GPIO driving a RESET net is the source, not the victim).
    # A bidirectional pin that is the only IC pin on the net is the receiving end of an off-board signal
    # (card-detect switch, cable enable), so the net name gives its polarity too.
    sole_ic = p.base_type == "bidirectional" and not any(
        q.ref != c.ref and d.components.get(q.ref) and is_ic(d.components[q.ref]) for q in d.pins_on(p.net))
    if ((pin_low and p.base_type in ("input", "bidirectional")) or (net_low and (p.base_type == "input" or sole_ic))) \
            and not others and not internal_pu:
        vref = (vmax - entry.get("io_relative", 0.3)) if vmax else (b.volts if b.volts > 0 else 3.3)
        if b.volts < 0.3 * vref:
            out.append(Finding("active-low-asserted", "pullup", sev,
                               f"Active-low input {p.label()} idles at {b.describe()} on {p.net}{state}: "
                               "held permanently asserted", refs=[c.ref] + _bias_resistors(d, p.net),
                               nets=[p.net], rationale=rat))
        elif b.volts < 0.7 * vref:
            out.append(Finding("active-low-indeterminate", "pullup", "warning",
                               f"Active-low input {p.label()} idles at {b.describe()} on {p.net}{state}: "
                               "between logic thresholds", refs=[c.ref] + _bias_resistors(d, p.net),
                               nets=[p.net], rationale=rat))
    return out


def check_bias(d: Design, bias: dict[str, dcbias.Bias], alone: dict[str, dcbias.Bias]) -> list[Finding]:
    out = []
    for c in d.components.values():
        entry = parts.lookup(c) or {}
        if not is_ic(c) and not entry.get("connector"):
            continue  # connectors only when the parts DB knows what plugs in (e.g. a Raspberry Pi header)
        vmax, why = io_voltage(d, c, bias)
        flagged_nets = set()
        for p in c.pins.values():
            if p.base_type in ("power_in", "power_out", "no_connect") or p.no_connect:
                continue
            if p.base_type == "passive" and not entry.get("connector"):
                continue
            if entry.get("connector") and (re.search(r"(5V|3V3|3\.3V|GND|VCC|POWER)", p.name or "", re.I)):
                continue
            from .analysis import supply_pin_name
            if supply_pin_name(p.name or "") and re.match(r"^(A|D)?V(CC|DD)", p.name or "", re.I):
                # A supply pin typed as input in an old symbol: it needs a DC path to a rail.
                bs = bias.get(p.net)
                if bs is not None and bs.volts is None and not bs.driven_by_other(p) and not is_unconnected(p.net):
                    out.append(Finding("supply-pin-unpowered", "power", "error",
                                       f"Supply pin {p.label()} on {p.net} has no DC path to any rail",
                                       refs=[c.ref], nets=[p.net], rationale="DC nodal solve of the resistor network"))
                elif bs is not None and bs.volts is not None and bs.volts < 0.5 and not bs.fixed \
                        and not bs.driven_by_other(p):
                    out.append(Finding("supply-pin-unpowered", "power", "error",
                                       f"Supply pin {p.label()} on {p.net} idles at {bs.describe()}: tied to ground "
                                       "through the resistor network", refs=[c.ref] + _bias_resistors(d, p.net),
                                       nets=[p.net], rationale="DC nodal solve of the resistor network"))
                continue
            if supply_pin_name(p.name or ""):
                continue
            if not p.net or is_unconnected(p.net) or is_power(p.net) or is_ground(p.net):
                continue
            if len(d.nets.get(p.net).nodes) < 2:
                continue  # single-pin nets: reported by the connectivity rules
            b = bias.get(p.net)
            if b is None:
                continue
            # Over-voltage on a signal pin: holds whenever the net is released (open-drain, tri-state, input).
            # Checked as drawn and with jumpers open (a jumper to GND can hide a pull-up to the wrong rail).
            ba = alone.get(p.net)
            for bb, st in ((b, ""), (ba, f" with {_offboard(d, p.net) or _offboard_island(d, p.net)} open")):
                if bb is None or bb.volts is None or vmax is None or bb.volts <= vmax or bb.assumed \
                        or p.net in flagged_nets or ANALOG_PIN.search(p.name or "") or (st and st == " with  open"):
                    continue
                flagged_nets.add(p.net)
                out.append(Finding("bias-overvoltage", "pullup", "error" if not st else "warning",
                                   f"{p.label()} idles at {bb.describe()} on {p.net}{st}, above its {vmax:g} V limit "
                                   f"({why})", refs=[c.ref] + _bias_resistors(d, p.net), nets=[p.net],
                                   rationale="DC nodal solve of the resistor network"))
            if b.volts is not None and b.volts < -0.3 and not ANALOG_PIN.search(p.name or ""):
                out.append(Finding("bias-negative", "pullup", "error",
                                   f"{p.label()} idles at {b.describe()} on {p.net}: below ground",
                                   refs=[c.ref] + _bias_resistors(d, p.net), nets=[p.net]))
            # Two board states: as drawn (jumpers closed, headers may drive) and on its own (jumpers open,
            # headers unplugged). A level that only one state produces is a configuration choice.
            ext = _offboard(d, p.net) or _offboard_island(d, p.net)
            fa = _pin_findings(d, c, p, b, entry, vmax, why, "")
            ba = alone.get(p.net)
            st = " at power-on (I/O high-Z" + (f", {ext} open/unplugged)" if ext else ")")
            fb = _pin_findings(d, c, p, ba, entry, vmax, why, st) if (ba is not None and ext) else []
            ra, rb = {f.rule for f in fa}, {f.rule for f in fb}
            found = [f for f in fa if f.rule != "active-low-asserted" or not ext or "active-low-asserted" in rb]
            found += [f for f in fb if f.rule not in ra and not (f.rule == "active-low-asserted" and ra)]
            out += found
    return out


def _offboard_island(d: Design, net: str) -> str:
    """Connectors/jumpers one resistor away (e.g. pull-up -> jumper -> GND)."""
    refs = set()
    for p in d.pins_on(net):
        comp = d.components.get(p.ref)
        if comp and (is_resistor(comp) or comp.prefix in ("JP", "W", "SJ")):
            o = other_pin(comp, p)
            if o and o.net and not is_power(o.net) and not is_ground(o.net):
                for p2 in d.pins_on(o.net):
                    c2 = d.components.get(p2.ref)
                    from .dcbias import is_jumper
                    if c2 and (is_connector(c2) or is_jumper(c2)) and c2 is not comp:
                        refs.add(c2.ref)
            if comp.prefix in ("JP", "W", "SJ"):
                refs.add(comp.ref)
    return ", ".join(sorted(refs))


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


def check_gates(d: Design, alone: dict[str, dcbias.Bias]) -> list[Finding]:
    """MOSFET gates / BJT bases with no DC bias at power-on: the switch state is undefined until firmware
    drives the GPIO (load switches turn on randomly, amplifiers enable themselves)."""
    out = []
    for c in d.components.values():
        if not (c.prefix.startswith("Q") or c.prefix == "T"):
            continue
        for p in c.pins.values():
            if not (re.match(r"^(G|GATE|B|BASE)$", p.name or "", re.I) or re.match(r"^(G|B)$", p.num or "")
                    or p.base_type == "input"):
                continue
            if not p.net or is_unconnected(p.net) or is_power(p.net) or is_ground(p.net):
                continue
            b = alone.get(p.net)
            if b is None or b.driven_by_other(p):
                continue
            leafname = p.net.rsplit("/", 1)[-1]
            if b.volts is not None:
                # Active-low enable (e.g. !VAA_ENABLE) held low at power-on: the switch turns on before firmware runs.
                if not leafname.startswith("Net-(") and ACTIVE_LOW.search(leafname) and b.volts < 0.5:
                    out.append(Finding("gate-asserted-at-reset", "pullup", "warning",
                                       f"{c.ref} control {p.label()} on active-low net {p.net} idles at {b.describe()} "
                                       "at power-on: the switch is enabled before firmware runs",
                                       refs=[c.ref] + _bias_resistors(d, p.net), nets=[p.net],
                                       rationale="DC nodal solve, power-on reset state"))
                continue
            if any(is_capacitor(d.components[q.ref]) for q in d.pins_on(p.net) if q is not p):
                continue
            ext = _offboard(d, p.net)
            out.append(Finding("gate-floating-at-reset", "pullup", "warning",
                               f"{c.ref} control pin {p.label()} on {p.net} has no pull resistor: it floats at "
                               f"power-on while the driving I/O is high-Z{' and ' + ext + ' is unplugged' if ext else ''}",
                               refs=[c.ref], nets=[p.net], rationale="DC nodal solve, power-on reset state"))
    return out


_SDIO = re.compile(r"(SDIO|SD|MMC|EMMC)\w*_?(D(AT)?[0-3]|CMD)$|^(DAT[0-3]|CMD)$", re.I)


def check_sdio(d: Design, bias: dict[str, dcbias.Bias]) -> list[Finding]:
    """SD/SDIO/eMMC CMD and DAT lines need pull-ups (SD spec: 10k-100k), like I2C."""
    out = []
    for n, net in d.nets.items():
        if is_unconnected(n) or is_power(n) or is_ground(n):
            continue
        names = [n.rsplit("/", 1)[-1]] + [q.name or "" for q in net.nodes]
        if not any(_SDIO.search(x) for x in names) or not any(is_ic(d.components[q.ref]) for q in net.nodes):
            continue
        ups = [q for q in net.nodes if is_resistor(d.components[q.ref])
               and is_power((other_pin(d.components[q.ref], q) or q).net)]
        if not ups:
            out.append(Finding("sdio-missing-pullup", "pullup", "warning",
                               f"SD/SDIO line {n} has no pull-up (the SD spec requires 10k-100k on CMD and DAT0-3)",
                               refs=sorted({q.ref for q in net.nodes if is_ic(d.components[q.ref])}), nets=[n]))
    return out


_FB = re.compile(r"^(FB|VFB|ADJ|FEEDBACK|VSENSE|SENSE)\d*$", re.I)
_SW = re.compile(r"^(SW|LX|PH|PHASE|OUT|VOUT|VO|OUTPUT)\d*$", re.I)


def check_feedback(d: Design) -> list[Finding]:
    """A regulator's feedback divider must sense the rail that regulator produces. If the top resistor
    goes to some other rail, the output regulates to the wrong voltage."""
    out = []
    for c in d.components.values():
        if not is_ic(c):
            continue
        outs = set()
        for p in c.pins.values():
            if (p.base_type == "power_out" or _SW.match(p.name or "")) and p.net and not is_unconnected(p.net):
                outs.add(p.net)
                for q in d.pins_on(p.net):  # through the inductor of a switcher
                    comp = d.components.get(q.ref)
                    if comp and comp.prefix in ("L", "FB") and len(comp.pins) == 2:
                        o = other_pin(comp, q)
                        if o and o.net:
                            outs.add(o.net)
        if not outs:
            continue
        for p in c.pins.values():
            if not _FB.match(p.name or "") or not p.net or is_unconnected(p.net):
                continue
            tops = set()
            for q in d.pins_on(p.net):
                comp = d.components.get(q.ref)
                if comp and is_resistor(comp):
                    o = other_pin(comp, q)
                    if o and o.net and is_power(o.net):
                        tops.add(o.net)
            wrong = sorted(t for t in tops if t not in outs)
            if wrong and not (tops & outs):
                out.append(Finding("feedback-wrong-rail", "power", "error",
                                   f"{c.ref} ({c.value}) feedback {p.label()} is divided from {', '.join(wrong)}, but the "
                                   f"regulator drives {', '.join(sorted(outs))}: it will regulate the wrong rail",
                                   refs=[c.ref], nets=[p.net] + wrong))
    return out


_RS485 = re.compile(r"MAX3?48[0-9]|SP3?48[0-9]|SN65HVD[0-9]+|ADM3?48[0-9]|ST3?485|THVD|LTC28[0-9]+|ISL3?8[0-9]", re.I)


def check_rs485(d: Design, bias: dict[str, dcbias.Bias]) -> list[Finding]:
    """RS-485 A/B need fail-safe bias (A pulled up, B pulled down) so an idle or open bus reads a valid
    level, unless the transceiver has built-in true fail-safe. Flag a bus line with no bias resistor."""
    out = []
    for c in d.components.values():
        if not _RS485.search(c.value or "") and not _RS485.search(c.part or ""):
            continue
        for p in c.pins.values():
            if (p.name or "").upper() not in ("A", "B", "Y", "Z") or not p.net or is_unconnected(p.net):
                continue
            rails = [o.net for q in d.pins_on(p.net) if is_resistor(d.components[q.ref])
                     for o in [other_pin(d.components[q.ref], q)] if o and (is_power(o.net) or is_ground(o.net))]
            if not rails:
                out.append(Finding("rs485-no-failsafe-bias", "pullup", "warning",
                                   f"RS-485 line {p.label()} on {p.net} has no fail-safe bias resistor: an idle or "
                                   "open bus reads as noise unless the transceiver has true fail-safe",
                                   refs=[c.ref], nets=[p.net]))
    return out


def run(d: Design) -> list[Finding]:
    bias = dcbias.solve(d)
    alone = dcbias.solve(d, standalone=True)
    return (check_bias(d, bias, alone) + check_part_requirements(d, bias) + check_leds(d) + check_crystals(d)
            + check_gates(d, alone) + check_sdio(d, bias) + check_feedback(d)
            + check_rs485(d, bias))
