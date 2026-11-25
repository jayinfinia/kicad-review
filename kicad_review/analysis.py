"""Electrical heuristics shared by the rule checks: what is ground, what is a
supply rail, what voltage a rail carries, which parts are ICs/caps/resistors."""
from __future__ import annotations

import re

from .netlist import Component, Design, Pin

_GND_RE = re.compile(r"^(GND\w*|\w*GND|VSS\w*|0V|EARTH|COM|AGND|DGND|PGND)$", re.I)
_PWR_RE = re.compile(r"^([+-]\d+(\.\d+)?V\d*|[+-]?\d+V\d+|VCC\w*|VDD\w*|VBUS\w*|VBAT\w*|VIN\w*|V\+|VEE\w*|"
                     r"\+?VPP\w*|VAA\w*|AVDD\w*|DVDD\w*|IOVDD\w*|VIO\w*|VREF\w*|VSYS\w*|\+?\d+V\d*_\w+)$", re.I)
_SUPPLY_PIN_RE = re.compile(r"^(V(CC|DD|BAT|BUS|IN|IO|DDA|DDIO|CCA|CCIO|CCINT|CCAUX|\+)\w*|AVDD\w*|DVDD\w*|IOVDD\w*|V\d\w*|PWR\w*|[+]?\d+V\d*)$", re.I)
_GND_PIN_RE = re.compile(r"^(GND\w*|\w*GND|VSS\w*|AGND|DGND|PGND|EP|PAD|0V)$", re.I)


def leaf(net: str) -> str:
    """'/sheet/+3V3' -> '+3V3'"""
    return net.rsplit("/", 1)[-1] if net else ""


def is_ground(net: str | None) -> bool:
    return bool(net) and bool(_GND_RE.match(leaf(net)))


def is_power(net: str | None) -> bool:
    return bool(net) and not is_ground(net) and bool(_PWR_RE.match(leaf(net)))


def is_unconnected(net: str | None) -> bool:
    return not net or leaf(net).startswith("unconnected-")


def rail_voltage(net: str | None) -> float | None:
    """'+3V3' -> 3.3, '+3.3V' -> 3.3, '+5V' -> 5, '+1V8' -> 1.8, '-12V' -> -12, 'VCC' -> None."""
    if not net:
        return None
    s = leaf(net).upper()
    m = re.match(r"^([+-]?)(\d+)V(\d+)", s) or re.match(r"^([+-]?)(\d+(?:\.\d+)?)V", s)
    if not m:
        m2 = re.match(r"^V(?:CC|DD|IN|BUS)?_?(\d+)V(\d*)$", s)
        if m2:
            return float(f"{m2.group(1)}.{m2.group(2) or 0}")
        return None
    sign = -1.0 if m.group(1) == "-" else 1.0
    if len(m.groups()) == 3 and m.group(3) is not None:
        return sign * float(f"{m.group(2)}.{m.group(3)}")
    return sign * float(m.group(2))


def supply_pin_name(name: str) -> bool:
    return bool(_SUPPLY_PIN_RE.match(name.lstrip("~/")))


def ground_pin_name(name: str) -> bool:
    return bool(_GND_PIN_RE.match(name.lstrip("~/")))


# ---- component classification ------------------------------------------------------

def is_capacitor(c: Component) -> bool:
    return c.prefix in ("C", "CP") and len(c.pins) == 2


def is_resistor(c: Component) -> bool:
    return c.prefix == "R" and len(c.pins) == 2


_CONNECTOR_PREFIXES = ("J", "P", "CONN", "X", "BUS", "CN", "SLOT")


def is_connector(c: Component) -> bool:
    return c.prefix in _CONNECTOR_PREFIXES or "conn" in c.lib.lower() or "conn" in c.part.lower()


def is_ic(c: Component) -> bool:
    if is_connector(c):
        return False
    if c.prefix in ("U", "IC"):
        return True
    return c.prefix not in ("J", "P", "CONN", "X", "Y", "SW", "TP", "BT", "D", "LED", "Q", "F", "FB", "L", "K", "H", "MH") \
        and len(c.pins) >= 5 and any(p.base_type == "power_in" for p in c.pins.values())


def other_pin(c: Component, pin: Pin) -> Pin | None:
    for p in c.pins.values():
        if p is not pin:
            return p
    return None


_UNITS = {"p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6, "m": 1e-3, "k": 1e3, "K": 1e3, "M": 1e6, "R": 1.0, "": 1.0}


def parse_value(s: str) -> float | None:
    """'4.7k' -> 4700, '4k7' -> 4700, '100n' -> 1e-7, '10K 1%' -> 10000, '1uF 10V' -> 1e-6."""
    if not s:
        return None
    tok = s.strip().split()[0].replace(",", ".")
    m = re.match(r"^(\d+)([pnuµmkKMR])(\d+)", tok)
    if m:
        return float(f"{m.group(1)}.{m.group(3)}") * _UNITS[m.group(2)]
    m = re.match(r"^(\d+(?:\.\d+)?)\s*([pnuµmkKMR]?)", tok)
    if m:
        return float(m.group(1)) * _UNITS[m.group(2)]
    return None


def cap_voltage_rating(c: Component) -> float | None:
    for k in ("Voltage", "voltage", "Rating", "V"):
        if k in c.fields:
            m = re.search(r"(\d+(?:\.\d+)?)\s*V", c.fields[k])
            if m:
                return float(m.group(1))
    m = re.search(r"(\d+(?:\.\d+)?)\s*V\b", c.value)
    return float(m.group(1)) if m else None


def ic_supply_rails(d: Design, c: Component) -> set[str]:
    return {p.net for p in c.pins.values()
            if p.base_type == "power_in" and p.net and not is_ground(p.net) and not is_unconnected(p.net)}


def fmt_value(v: float) -> str:
    for unit, mul in (("M", 1e6), ("k", 1e3), ("", 1)):
        if abs(v) >= mul:
            return f"{v / mul:g}{unit}"
    return f"{v:g}"
