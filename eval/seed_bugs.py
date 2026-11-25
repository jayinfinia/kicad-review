"""Seed known design bugs into clean KiCad netlists to build a labelled eval set.

Each variant = one clean design + exactly one injected bug, written as a KiCad
netlist plus a ground-truth label (bug type, expected category, refs/nets a
correct finding should point at). Sites are sampled with a fixed seed.

  python eval/seed_bugs.py examples/netlists/*.net -o eval/variants --per-type 6
"""
from __future__ import annotations

import argparse
import copy
import json
import random
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kicad_review import netlist  # noqa: E402
from kicad_review.analysis import (ic_supply_rails, is_capacitor, is_ground, is_ic, is_power,  # noqa: E402
                                   is_resistor, is_unconnected, other_pin, rail_voltage, supply_pin_name)
from kicad_review.netlist import Component, Design, Net, Pin  # noqa: E402


# ---------------------------------------------------------------- mutation helpers

def move_pin(d: Design, pin: Pin, new_net: str):
    if pin.net in d.nets:
        d.nets[pin.net].nodes = [p for p in d.nets[pin.net].nodes if p is not pin]
    d.nets.setdefault(new_net, Net(new_net)).nodes.append(pin)
    pin.net = new_net


def remove_component(d: Design, ref: str):
    c = d.components.pop(ref)
    for p in c.pins.values():
        if p.net in d.nets:
            d.nets[p.net].nodes = [q for q in d.nets[p.net].nodes if q is not p]


def ground_net(d: Design) -> str | None:
    gs = [n for n in d.nets if is_ground(n)]
    return max(gs, key=lambda n: len(d.nets[n].nodes)) if gs else None


def real_net(d: Design, p: Pin) -> bool:
    return p.net is not None and not is_unconnected(p.net) and len(d.nets.get(p.net, Net("")).nodes) >= 2


# ---------------------------------------------------------------- bug generators
# Each returns a list of candidate sites: (description, apply(d) -> label dict)

def sites_remove_decoupling(d: Design):
    out = []
    rails = {}
    for c in d.components.values():
        if is_ic(c):
            for r in ic_supply_rails(d, c):
                rails.setdefault(r, set()).add(c.ref)
    for rail, ics in rails.items():
        caps = sorted({p.ref for p in d.pins_on(rail) if is_capacitor(d.components[p.ref])
                       and is_ground((other_pin(d.components[p.ref], p) or Pin("", "")).net)})
        if caps:
            def apply(d, rail=rail, caps=caps, ics=ics):
                for c in caps:
                    remove_component(d, c)
                return {"category": "decoupling", "refs": sorted(ics), "nets": [rail],
                        "detail": f"removed all decoupling caps {', '.join(caps)} on {rail}"}
            out.append(apply)
    return out


def sites_float_input(d: Design):
    out = []
    for c in d.components.values():
        if not is_ic(c):
            continue
        for p in c.pins.values():
            if p.base_type == "input" and real_net(d, p) and not is_power(p.net) and not is_ground(p.net):
                def apply(d, ref=c.ref, num=p.num):
                    pin = d.components[ref].pins[num]
                    old = pin.net
                    move_pin(d, pin, f"unconnected-({ref}-Pad{num})")
                    return {"category": "connectivity", "refs": [ref], "nets": [old],
                            "detail": f"disconnected input {pin.label()} from {old}"}
                out.append(apply)
            elif p.base_type == "input" and (is_power(p.net) or is_ground(p.net)) and real_net(d, p):
                # unused input tied off to a rail: floating it is the classic bug
                def apply(d, ref=c.ref, num=p.num):
                    pin = d.components[ref].pins[num]
                    old = pin.net
                    move_pin(d, pin, f"unconnected-({ref}-Pad{num})")
                    return {"category": "connectivity", "refs": [ref], "nets": [old],
                            "detail": f"removed tie-off: {pin.label()} no longer tied to {old}"}
                out.append(apply)
    return out


def sites_float_power_pin(d: Design):
    out = []
    for c in d.components.values():
        if not is_ic(c):
            continue
        for p in c.pins.values():
            if p.base_type == "power_in" and is_power(p.net):
                def apply(d, ref=c.ref, num=p.num):
                    pin = d.components[ref].pins[num]
                    old = pin.net
                    move_pin(d, pin, f"unconnected-({ref}-Pad{num})")
                    return {"category": "connectivity", "refs": [ref], "nets": [old],
                            "detail": f"disconnected supply pin {pin.label()} from {old}"}
                out.append(apply)
    return out


def sites_supply_to_ground(d: Design):
    out = []
    g = ground_net(d)
    if not g:
        return out
    for c in d.components.values():
        if not is_ic(c):
            continue
        for p in c.pins.values():
            if p.base_type == "power_in" and is_power(p.net) and supply_pin_name(p.name or ""):
                def apply(d, ref=c.ref, num=p.num, g=g):
                    pin = d.components[ref].pins[num]
                    old = pin.net
                    move_pin(d, pin, g)
                    return {"category": "power", "refs": [ref], "nets": [old, g],
                            "detail": f"supply pin {pin.label()} moved from {old} to {g}"}
                out.append(apply)
    return out


_TYPO = {"+3V3": "+3.3V", "+3.3V": "+3V3", "+5V": "+5V0", "VCC": "VCC1", "+12V": "+12V0", "VDD": "VDD1"}


def sites_rail_typo(d: Design):
    out = []
    for c in d.components.values():
        if not is_ic(c):
            continue
        pw = [p for p in c.pins.values() if p.base_type == "power_in" and is_power(p.net)]
        rails = {p.net for p in pw}
        if len(rails) != 1:
            continue
        rail = rails.pop()
        leafname = rail.rsplit("/", 1)[-1]
        typo = rail[: len(rail) - len(leafname)] + _TYPO.get(leafname, leafname + "_")
        if typo in d.nets:
            continue

        def apply(d, ref=c.ref, rail=rail, typo=typo):
            for p in d.components[ref].pins.values():
                if p.net == rail and p.base_type == "power_in":
                    move_pin(d, p, typo)
            return {"category": "power", "refs": [ref], "nets": [rail, typo],
                    "detail": f"{ref} supply pins moved from {rail} to misspelled rail {typo}"}
        out.append(apply)
    return out


def _pullups(d: Design):
    for c in d.components.values():
        if not is_resistor(c):
            continue
        a, b = list(c.pins.values())
        for sig, far in ((a, b), (b, a)):
            if is_power(far.net) and real_net(d, sig) and not is_power(sig.net) and not is_ground(sig.net) \
                    and any(is_ic(d.components[p.ref]) for p in d.pins_on(sig.net) if p.ref != c.ref):
                yield c, sig, far


def sites_remove_pullup(d: Design):
    out = []
    for c, sig, far in _pullups(d):
        def apply(d, ref=c.ref, net=sig.net, rail=far.net):
            remove_component(d, ref)
            ics = sorted({p.ref for p in d.pins_on(net) if is_ic(d.components[p.ref])})
            return {"category": "pullup", "refs": [ref] + ics, "nets": [net],
                    "detail": f"removed pull-up {ref} ({net} to {rail})"}
        out.append(apply)
    return out


def sites_pullup_to_ground(d: Design):
    out = []
    g = ground_net(d)
    for c, sig, far in _pullups(d):
        def apply(d, ref=c.ref, num=far.num, net=sig.net, g=g):
            pin = d.components[ref].pins[num]
            old = pin.net
            move_pin(d, pin, g)
            ics = sorted({p.ref for p in d.pins_on(net) if is_ic(d.components[p.ref])})
            return {"category": "pullup", "refs": [ref] + ics, "nets": [net],
                    "detail": f"pull-up {ref} on {net} re-tied from {old} to {g} (now a pull-down)"}
        out.append(apply)
    return out


def sites_pullup_wrong_rail(d: Design):
    out = []
    volts = {n: rail_voltage(n) for n in d.nets if is_power(n) and rail_voltage(n)}
    for c, sig, far in _pullups(d):
        v = volts.get(far.net)
        higher = [n for n, nv in volts.items() if v is not None and nv > v + 0.3]
        if not higher:
            continue
        hi = max(higher, key=lambda n: volts[n])

        def apply(d, ref=c.ref, num=far.num, net=sig.net, hi=hi):
            pin = d.components[ref].pins[num]
            old = pin.net
            move_pin(d, pin, hi)
            ics = sorted({p.ref for p in d.pins_on(net) if is_ic(d.components[p.ref])})
            return {"category": "pullup", "refs": [ref] + ics, "nets": [net, hi],
                    "detail": f"pull-up {ref} on {net} moved from {old} to higher rail {hi}"}
        out.append(apply)
    return out


def sites_cap_undervoltage(d: Design):
    out = []
    for c in d.components.values():
        if not is_capacitor(c):
            continue
        rails = [p.net for p in c.pins.values() if p.net and rail_voltage(p.net) and abs(rail_voltage(p.net)) >= 3]
        if not rails:
            continue
        v = abs(rail_voltage(rails[0]))
        rating = {3.3: 2.5, 5.0: 4.0, 12.0: 10.0}.get(v, round(v * 0.6, 1))

        def apply(d, ref=c.ref, rating=rating, rail=rails[0]):
            comp = d.components[ref]
            base = re.sub(r"[/ ]?\d+(\.\d+)?\s*V\b", "", comp.value).split()[0] if comp.value else "100n"
            comp.value = f"{base} {rating:g}V"
            comp.fields = {k: v for k, v in comp.fields.items() if k.lower() != "voltage"}
            return {"category": "power", "refs": [ref], "nets": [rail],
                    "detail": f"{ref} voltage rating set to {rating:g} V on {rail}"}
        out.append(apply)
    return out


BUG_TYPES = {
    "remove_decoupling": sites_remove_decoupling,
    "float_input": sites_float_input,
    "float_power_pin": sites_float_power_pin,
    "supply_to_ground": sites_supply_to_ground,
    "rail_typo": sites_rail_typo,
    "remove_pullup": sites_remove_pullup,
    "pullup_to_ground": sites_pullup_to_ground,
    "pullup_wrong_rail": sites_pullup_wrong_rail,
    "cap_undervoltage": sites_cap_undervoltage,
}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("netlists", nargs="+")
    ap.add_argument("-o", "--out", default="eval/variants")
    ap.add_argument("--per-type", type=int, default=6, help="variants per bug type (spread across designs)")
    ap.add_argument("--seed", type=int, default=7)
    a = ap.parse_args(argv)
    rng = random.Random(a.seed)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    designs = {Path(f).stem: f for f in sorted(a.netlists)}
    manifest = []
    for bug, gen in BUG_TYPES.items():
        pool = []
        for name, f in designs.items():
            d = netlist.load(f)
            pool += [(name, i) for i in range(len(gen(d)))]
        # round-robin across designs so one big design doesn't dominate
        by_design = {}
        for name, i in pool:
            by_design.setdefault(name, []).append(i)
        for v in by_design.values():
            rng.shuffle(v)
        picks, names = [], sorted(by_design)
        rng.shuffle(names)
        while len(picks) < a.per_type and any(by_design.values()):
            for name in names:
                if by_design[name] and len(picks) < a.per_type:
                    picks.append((name, by_design[name].pop()))
        for k, (name, i) in enumerate(picks):
            d = netlist.load(designs[name])
            label = gen(d)[i](d)
            vid = f"{name}__{bug}__{k}"
            (out / f"{vid}.net").write_text(netlist.to_netlist_text(d), encoding="utf-8")
            manifest.append({"id": vid, "design": name, "clean": str(designs[name]), "bug": bug, **label})
        print(f"{bug:18s} {len(picks):2d} variants (from {len(pool)} candidate sites)")
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"{len(manifest)} variants -> {out}/manifest.json")


if __name__ == "__main__":
    main()
