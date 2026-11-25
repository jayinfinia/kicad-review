"""DC bias solver: estimate the idle voltage of every net from the resistor network.

This is nodal analysis at DC. Capacitors are open, inductors/ferrites/jumpers are
near-shorts, resistors are conductances. Supply and ground nets are fixed-voltage
nodes. For every other net we solve the Laplacian system

    G_ff · v_f = -G_fk · v_k

per connected resistive island, which gives each net's voltage when nothing is
actively driving it, and its Thevenin resistance (how strong the pull is).

Nets touched by an output, bidirectional, tri-state or power-output pin, or by a
connector, are marked *driven*: their idle level depends on something outside
the resistor network, so bias checks skip them (they still act as unknowns in
the solve, which is the conservative choice).
"""
from __future__ import annotations

import re
from dataclasses import dataclass

import numpy as np

from .analysis import is_connector, is_ground, is_power, is_resistor, is_unconnected, parse_value, rail_voltage
from .netlist import Design

UNKNOWN_RAIL_V = 5.0  # VCC/VDD with no voltage in the name: the classic default, flagged as assumed

_DRIVER_TYPES = {"output", "bidirectional", "tri_state", "power_out", "open_collector", "open_emitter"}
_SHORT_PREFIXES = ("L", "FB", "NT")         # inductors, ferrite beads, net ties: always shorts at DC
_JUMPER_PREFIXES = ("JP", "W", "SJ", "J_", "BR")  # jumpers / wire links: closed as drawn, open when standalone
R_SHORT = 0.1


def is_jumper(c) -> bool:
    return c.prefix in _JUMPER_PREFIXES or bool(re.search(r"jumper|solder_?jumper|bridge", c.part or "", re.I))


@dataclass
class Bias:
    net: str
    volts: float | None        # None = floating (no resistive path to any rail)
    r_thevenin: float | None   # ohms; how hard the net is held at `volts`
    driven: bool               # something other than resistors may set this net
    fixed: bool = False        # supply/ground net
    assumed: bool = False      # depends on an unknown-voltage rail (VCC/VDD) assumed at 5 V
    drivers: frozenset = frozenset()   # refs that can drive this net's resistive island

    def driven_by_other(self, pin) -> bool:
        """Driven by some pin other than `pin` (a bidirectional pin used as an input doesn't bias itself)."""
        return bool(self.drivers - {(pin.ref, pin.num)})

    def describe(self) -> str:
        if self.volts is None:
            return "floating"
        s = f"{self.volts:.2f} V"
        if self.r_thevenin is not None and not self.fixed:
            s += f" via {_fmt_r(self.r_thevenin)}"
        return s + (" (assumes VCC = 5 V)" if self.assumed else "")


def _fmt_r(r: float) -> str:
    return f"{r / 1e3:.3g} kΩ" if r >= 1e3 else f"{r:.3g} Ω"


def fixed_voltage(net: str) -> tuple[float | None, bool]:
    if is_ground(net):
        return 0.0, False
    if is_power(net):
        v = rail_voltage(net)
        if v is not None:
            return v, False
        if re.match(r"^(VCC|VDD|VPP|V\+)", net.rsplit("/", 1)[-1], re.I):
            return UNKNOWN_RAIL_V, True
    return None, False


_NONLINEAR_PREFIXES = ("Q", "T", "D", "F", "LED", "ZD", "VR", "K", "RV", "TR")


def can_drive(comp, p, standalone: bool = False, d=None) -> bool:
    """Could this pin set the net's level? Outputs obviously; also IC pins whose library type is
    passive/unspecified (common for FPGAs and old symbols), transistors, diodes, fuses, and
    (unless the board is considered on its own) connectors."""
    from .analysis import is_ic
    if comp.prefix.startswith("Q") or comp.prefix == "T":
        # A transistor's control terminal is a load, not a source.
        if re.match(r"^(G|GATE|B|BASE)$", p.name or p.num or "", re.I) or re.match(r"^(G|B)$", p.num or "") \
                or p.base_type == "input":
            return False
    if is_ic(comp) and re.match(r"^(HDRV|LDRV|DRVH|DRVL|DH|DL|HO|LO|GATE\w*|GH|GL|UG|LG|TG|BG|OUT\w*|VOUT|SW)$",
                                 p.name or "", re.I):
        return True  # gate-driver / output pins mistyped as inputs in the symbol library
    if standalone:
        # Power-on reset state: MCU/FPGA I/O (bidirectional, tri-state, open-drain, unconfigured
        # passive-typed pins) is high-Z until firmware runs; only dedicated push-pull outputs drive.
        if is_ic(comp):
            return p.base_type in ("output", "power_out")
    if p.base_type in _DRIVER_TYPES:
        return True
    if is_ic(comp) and p.base_type in ("passive", "unspecified", "", "free"):
        return True
    if comp.prefix in ("D", "ZD") and len(comp.pins) == 2 and d is not None:
        # A diode passes a level only from a net that can both source and sink (rail or push-pull output);
        # behind an open-collector it can only pull one way (wired-OR reset lines).
        far = [x for x in comp.pins.values() if x is not p]
        if far and far[0].net:
            from .analysis import is_power as _pw, is_ground as _gnd
            if _pw(far[0].net) or _gnd(far[0].net):
                return True
            return any(q.base_type in ("output", "power_out") for q in d.pins_on(far[0].net))
        return False
    if comp.prefix in _NONLINEAR_PREFIXES or comp.prefix.startswith("Q"):
        return True
    return is_connector(comp) and not standalone


def is_resistor_network(c) -> bool:
    return c.prefix in ("RN", "RR", "RP", "RA") or bool(re.search(r"R_Pack|R_Network|RPACK|SIP|resistor.?(array|network)",
                                                                   (c.part or "") + (c.description or ""), re.I))


def network_pairs(c):
    """Resistor arrays: bussed (one common pin, usually pin 1 or named COM) or isolated (pin i to pin N+1-i)."""
    r = parse_value(c.value) or 10e3
    pins = sorted(c.pins.values(), key=lambda p: int(p.num) if p.num.isdigit() else 999)
    common = [p for p in pins if re.match(r"^(COM|COMMON|C)$", p.name or "", re.I)]
    if not common and pins and (is_power(pins[0].net) or is_ground(pins[0].net)) and len(pins) % 2 == 1:
        common = [pins[0]]
    if common:
        return [(common[0], p, r) for p in pins if p is not common[0]]
    n = len(pins)
    return [(pins[i], pins[n - 1 - i], r) for i in range(n // 2)]


def solve(d: Design, standalone: bool = False) -> dict[str, Bias]:
    """Nominal: jumpers closed as drawn, switches open, connectors and I/O pins may drive their nets.
    standalone=True: the board at power-on reset, on its own: jumpers open, headers unplugged, and
    programmable I/O high-Z (only push-pull outputs drive)."""
    nets = [n for n in d.nets if not is_unconnected(n)]
    idx = {n: i for i, n in enumerate(nets)}
    fixed, assumed = {}, set()
    for n in nets:
        v, a = fixed_voltage(n)
        if v is not None:
            fixed[n] = v
            if a:
                assumed.add(n)

    edges = []  # (i, j, conductance)
    for c in d.components.values():
        if len(c.pins) > 2 and is_resistor_network(c):
            for a, b, r in network_pairs(c):
                if a.net in idx and b.net in idx and a.net != b.net:
                    edges.append((idx[a.net], idx[b.net], 1.0 / r))
            continue
        if len(c.pins) != 2:
            continue
        a, b = list(c.pins.values())
        if a.net not in idx or b.net not in idx or a.net == b.net:
            continue
        if is_resistor(c):
            r = parse_value(c.value)
            if not r or r <= 0:
                continue
        elif c.prefix in _SHORT_PREFIXES or (is_jumper(c) and not standalone):
            r = R_SHORT
        else:
            continue
        edges.append((idx[a.net], idx[b.net], 1.0 / r))

    drivers: dict[str, set] = {}
    for n in nets:
        for p in d.nets[n].nodes:
            comp = d.components.get(p.ref)
            if comp is not None and can_drive(comp, p, standalone, d):
                drivers.setdefault(n, set()).add((p.ref, p.num))
    driven = set(drivers)

    # Union-find islands over resistive edges.
    parent = list(range(len(nets)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i, j, _ in edges:
        # Rails are boundaries: two pull-ups to the same rail don't make their nets one island.
        if nets[i] in fixed or nets[j] in fixed:
            continue
        parent[find(i)] = find(j)
    islands: dict[int, list[int]] = {}
    for i in range(len(nets)):
        islands.setdefault(find(i), []).append(i)

    out: dict[str, Bias] = {}
    adj: dict[int, list[tuple[int, float]]] = {}
    for i, j, g in edges:
        adj.setdefault(i, []).append((j, g))
        adj.setdefault(j, []).append((i, g))

    for n in fixed:
        out[n] = Bias(n, fixed[n], 0.0, True, fixed=True, assumed=n in assumed)
    for members in islands.values():
        free_m = [i for i in members if nets[i] not in fixed]
        if not free_m:
            continue
        fixed_m = sorted({j for i in free_m for j, _ in adj.get(i, []) if nets[j] in fixed})
        isl_drivers = frozenset().union(*[drivers.get(nets[i], set()) for i in free_m])
        isl_driven = bool(isl_drivers)
        if not fixed_m:
            for i in free_m:
                out[nets[i]] = Bias(nets[i], None, None, isl_driven, drivers=isl_drivers)
            continue
        pos = {i: k for k, i in enumerate(free_m)}
        G = np.zeros((len(free_m), len(free_m)))
        rhs = np.zeros(len(free_m))
        for i in free_m:
            for j, g in adj.get(i, []):
                G[pos[i], pos[i]] += g
                if j in pos:
                    G[pos[i], pos[j]] -= g
                else:
                    rhs[pos[i]] += g * fixed[nets[j]]
        try:
            v = np.linalg.solve(G, rhs)
            rth = np.diag(np.linalg.inv(G))
        except np.linalg.LinAlgError:
            v, rth = np.linalg.lstsq(G, rhs, rcond=None)[0], np.full(len(free_m), np.nan)
        uses_assumed = any(nets[i] in assumed for i in fixed_m)
        for i in free_m:
            k = pos[i]
            out[nets[i]] = Bias(nets[i], float(v[k]), float(rth[k]), nets[i] in driven or isl_driven,
                                assumed=uses_assumed, drivers=isl_drivers)
    return out
