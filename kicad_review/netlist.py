"""Load a KiCad (v6+/kicadsexpr) netlist into a small in-memory design model.

A `.kicad_sch` input is converted to a netlist first with `kicad-cli`, so that
KiCad itself resolves wires, labels, power symbols and hierarchy. Parsing raw
schematic geometry ourselves would be slower to write and easier to get wrong.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from . import sexpr
from .sexpr import QStr, find, find_all, value


# KiCad 5 netlists spell electrical types differently from KiCad 6+.
_LEGACY_TYPES = {"BiDi": "bidirectional", "3state": "tri_state", "openCol": "open_collector",
                 "openEm": "open_emitter", "unspc": "unspecified", "NotConnected": "no_connect",
                 "UnSpc": "unspecified"}


def norm_type(t: str) -> str:
    return _LEGACY_TYPES.get(t, t) if t else t


@dataclass
class Pin:
    ref: str
    num: str
    name: str = ""
    type: str = ""          # KiCad electrical type: input, output, power_in, passive, ...
    net: str | None = None

    @property
    def no_connect(self) -> bool:
        return "no_connect" in self.type

    @property
    def base_type(self) -> str:
        return self.type.replace("+no_connect", "")

    def label(self) -> str:
        n = f"/{self.name}" if self.name and self.name not in ("~", self.num) else ""
        return f"{self.ref}.{self.num}{n}"


@dataclass
class Component:
    ref: str
    value: str
    footprint: str = ""
    lib: str = ""
    part: str = ""
    description: str = ""
    fields: dict[str, str] = field(default_factory=dict)
    pins: dict[str, Pin] = field(default_factory=dict)

    @property
    def prefix(self) -> str:
        m = re.match(r"[A-Za-z#]+", self.ref)
        return m.group(0).upper() if m else ""


@dataclass
class Net:
    name: str
    code: str = ""
    nodes: list[Pin] = field(default_factory=list)


@dataclass
class Design:
    name: str
    components: dict[str, Component] = field(default_factory=dict)
    nets: dict[str, Net] = field(default_factory=dict)
    raw: list | None = None     # parsed netlist, kept so mutations can be written back

    def pins_on(self, net: str) -> list[Pin]:
        return self.nets[net].nodes if net in self.nets else []


def load(path: str | Path) -> Design:
    path = Path(path)
    if path.suffix == ".kicad_sch":
        text = export_netlist(path)
    else:
        text = path.read_text(encoding="utf-8")
    return parse(text, name=path.stem)


def export_netlist(sch: Path) -> str:
    cli = shutil.which("kicad-cli")
    if not cli:
        raise RuntimeError("kicad-cli not found; install KiCad 7+ or pass a .net netlist instead")
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "design.net"
        subprocess.run([cli, "sch", "export", "netlist", "-o", str(out), str(sch)],
                       check=True, capture_output=True)
        return out.read_text(encoding="utf-8")


def parse(text: str, name: str = "design") -> Design:
    root = sexpr.loads(text)
    if not root or root[0] != "export":
        raise ValueError("not a KiCad s-expression netlist (expected '(export ...')")
    d = Design(name=name, raw=root)

    # Library part pin tables (pin names/types for pins that end up unconnected too).
    libpins: dict[tuple[str, str], list[tuple[str, str, str]]] = {}
    libs = find(root, "libparts") or []
    for lp in find_all(libs, "libpart"):
        key = (value(lp, "lib", ""), value(lp, "part", ""))
        pins = find(lp, "pins") or []
        libpins[key] = [(value(p, "num", ""), value(p, "name", ""), norm_type(value(p, "type", "")))
                        for p in find_all(pins, "pin")]

    for c in find_all(find(root, "components") or [], "comp"):
        src = find(c, "libsource") or []
        comp = Component(
            ref=value(c, "ref", "?"), value=value(c, "value", ""),
            footprint=value(c, "footprint", ""), lib=value(src, "lib", ""),
            part=value(src, "part", ""), description=value(src, "description", ""),
        )
        for f in find_all(find(c, "fields") or [], "field"):
            if len(f) > 2:
                comp.fields[value(f, "name", "")] = str(f[2])
        for num, pname, ptype in libpins.get((comp.lib, comp.part), []):
            comp.pins[num] = Pin(comp.ref, num, pname, ptype)
        d.components[comp.ref] = comp

    for n in find_all(find(root, "nets") or [], "net"):
        net = Net(name=value(n, "name", ""), code=value(n, "code", ""))
        for node in find_all(n, "node"):
            ref, num = value(node, "ref", ""), value(node, "pin", "")
            comp = d.components.get(ref)
            pin = comp.pins.get(num) if comp else None
            if pin is None:
                pin = Pin(ref, num)
                if comp:
                    comp.pins[num] = pin
            pin.name = value(node, "pinfunction", pin.name) or pin.name
            pin.type = norm_type(value(node, "pintype", pin.type)) or pin.type
            pin.net = net.name
            net.nodes.append(pin)
        d.nets[net.name] = net
    return d


def to_netlist_text(d: Design) -> str:
    """Serialise the design back to a KiCad netlist (used by the bug seeder)."""
    root = [e for e in d.raw if not (isinstance(e, list) and e and e[0] in ("components", "nets"))]
    old_comps = {value(c, "ref"): c for c in find_all(find(d.raw, "components") or [], "comp")}
    comps = ["components"]
    for ref, c in d.components.items():
        if ref in old_comps:
            e = old_comps[ref]
            e = [x if not (isinstance(x, list) and x and x[0] == "value") else ["value", QStr(c.value)] for x in e]
            e = [x if not (isinstance(x, list) and x and x[0] == "fields")
                 else ["fields"] + [["field", ["name", QStr(k)], QStr(v)] for k, v in c.fields.items()] for x in e]
        else:
            e = ["comp", ["ref", QStr(ref)], ["value", QStr(c.value)],
                 ["libsource", ["lib", QStr(c.lib)], ["part", QStr(c.part)], ["description", QStr("")]]]
        comps.append(e)
    nets = ["nets"]
    for i, (nname, net) in enumerate(d.nets.items(), 1):
        if not net.nodes:
            continue
        e = ["net", ["code", QStr(str(i))], ["name", QStr(nname)]]
        for p in net.nodes:
            node = ["node", ["ref", QStr(p.ref)], ["pin", QStr(p.num)]]
            if p.name and p.name != "~":
                node.append(["pinfunction", QStr(p.name)])
            node.append(["pintype", QStr(p.type)])
            e.append(node)
        nets.append(e)
    # keep KiCad's ordering: design, components, libparts, libraries, nets
    out = [root[0], root[1], root[2] if len(root) > 2 else None]
    out = [x for x in out if x is not None]
    rest = root[len(out):]
    return sexpr.dumps(out + [comps] + rest + [nets]) + "\n"
