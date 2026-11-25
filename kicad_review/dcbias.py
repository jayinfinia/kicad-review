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
_SHORT_PREFIXES = ("L", "FB", "JP", "NT")   # inductors, ferrite beads, jumpers, net ties
R_SHORT = 0.1


@dataclass
class Bias:
    net: str
    volts: float | None        # None = floating (no resistive path to any rail)
    r_thevenin: float | None   # ohms; how hard the net is held at `volts`
    driven: bool               # something other than resistors may set this net
    fixed: bool = False        # supply/ground net
    assumed: bool = False      # depends on an unknown-voltage rail (VCC/VDD) assumed at 5 V

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


def solve(d: Design) -> dict[str, Bias]:
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
        if len(c.pins) != 2:
            continue
        a, b = list(c.pins.values())
        if a.net not in idx or b.net not in idx or a.net == b.net:
            continue
        if is_resistor(c):
            r = parse_value(c.value)
            if not r or r <= 0:
                continue
        elif c.prefix in _SHORT_PREFIXES:
            r = R_SHORT
        else:
            continue
        edges.append((idx[a.net], idx[b.net], 1.0 / r))

    driven = set()
    for n in nets:
        for p in d.nets[n].nodes:
            comp = d.components.get(p.ref)
            if p.base_type in _DRIVER_TYPES or (comp and is_connector(comp)):
                driven.add(n)
                break

    # Union-find islands over resistive edges.
    parent = list(range(len(nets)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i, j, _ in edges:
        parent[find(i)] = find(j)
    islands: dict[int, list[int]] = {}
    for i in range(len(nets)):
        islands.setdefault(find(i), []).append(i)

    out: dict[str, Bias] = {}
    adj: dict[int, list[tuple[int, float]]] = {}
    for i, j, g in edges:
        adj.setdefault(i, []).append((j, g))
        adj.setdefault(j, []).append((i, g))

    for members in islands.values():
        fixed_m = [i for i in members if nets[i] in fixed]
        free_m = [i for i in members if nets[i] not in fixed]
        for i in fixed_m:
            out[nets[i]] = Bias(nets[i], fixed[nets[i]], 0.0, True, fixed=True, assumed=nets[i] in assumed)
        if not free_m:
            continue
        isl_driven = any(nets[i] in driven for i in members if nets[i] not in fixed)
        if not fixed_m:
            for i in free_m:
                out[nets[i]] = Bias(nets[i], None, None, isl_driven)
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
                                assumed=uses_assumed)
    return out
