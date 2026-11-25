"""Generate examples/demo_board.net: a small synthetic 5 V -> 3.3 V sensor board
with deliberately planted mistakes, used for the README walkthrough.

Planted bugs (ground truth):
  1. LDO U1 has no input capacitor on +5V                         (rules: missing-decoupling)
  2. ATmega328P AVCC (U2.18) left unconnected                      (rules: unconnected-power-pin)
  3. BME280 CSB (U3.2) floating; must be tied to VDDIO for I2C     (rules: floating-input)
  4. I2C pull-ups R2/R3 go to +5V, BME280 is a 3.6 V-max part      (rules: pullup-wrong-rail)
  5. LED D1 driven from PB5 with no series resistor                (LLM only)
  6. 16 MHz crystal Y1 has no load capacitors                      (LLM only)
"""
from pathlib import Path

PARTS = {
    # ref: (value, lib, part, description, [(num, name, type)])
    "U1": ("AP2112K-3.3", "Regulator_Linear", "AP2112K-3.3", "600mA LDO, 3.3V fixed output",
           [("1", "VIN", "power_in"), ("2", "GND", "power_in"), ("3", "EN", "input"), ("4", "NC", "no_connect"),
            ("5", "VOUT", "power_out")]),
    "U2": ("ATmega328P-AU", "MCU_Microchip_ATmega", "ATmega328P-A", "8-bit AVR MCU, TQFP-32",
           [("4", "VCC", "power_in"), ("6", "VCC", "power_in"), ("18", "AVCC", "power_in"), ("20", "AREF", "passive"),
            ("3", "GND", "power_in"), ("5", "GND", "power_in"), ("21", "GND", "power_in"),
            ("29", "~{RESET}/PC6", "input"), ("7", "XTAL1/PB6", "input"), ("8", "XTAL2/PB7", "output"),
            ("17", "PB5", "bidirectional"), ("27", "PC4/SDA", "bidirectional"), ("28", "PC5/SCL", "bidirectional")]),
    "U3": ("BME280", "Sensor", "BME280", "Pressure/humidity/temperature sensor, 1.71-3.6V",
           [("8", "VDD", "power_in"), ("6", "VDDIO", "power_in"), ("1", "GND", "power_in"), ("7", "GND", "power_in"),
            ("2", "CSB", "input"), ("3", "SDI", "bidirectional"), ("4", "SCK", "input"), ("5", "SDO", "bidirectional")]),
    "C1": ("1uF 10V", "Device", "C", "Unpolarized capacitor", [("1", "~", "passive"), ("2", "~", "passive")]),
    "C2": ("100nF 10V", "Device", "C", "Unpolarized capacitor", [("1", "~", "passive"), ("2", "~", "passive")]),
    "C3": ("100nF 10V", "Device", "C", "Unpolarized capacitor", [("1", "~", "passive"), ("2", "~", "passive")]),
    "C4": ("100nF 10V", "Device", "C", "Unpolarized capacitor", [("1", "~", "passive"), ("2", "~", "passive")]),
    "R1": ("10k", "Device", "R", "Resistor", [("1", "~", "passive"), ("2", "~", "passive")]),
    "R2": ("4.7k", "Device", "R", "Resistor", [("1", "~", "passive"), ("2", "~", "passive")]),
    "R3": ("4.7k", "Device", "R", "Resistor", [("1", "~", "passive"), ("2", "~", "passive")]),
    "D1": ("LED_Green", "Device", "LED", "Light emitting diode", [("1", "K", "passive"), ("2", "A", "passive")]),
    "Y1": ("16MHz", "Device", "Crystal", "Two pin crystal", [("1", "1", "passive"), ("2", "2", "passive")]),
    "J1": ("Conn_01x02", "Connector", "Conn_01x02", "5V input", [("1", "Pin_1", "passive"), ("2", "Pin_2", "passive")]),
}
NETS = {
    "+5V": ["J1.1", "U1.1", "U1.3", "R2.1", "R3.1"],                      # bug 1 (no cap), bug 4 (pull-ups to 5V)
    "+3V3": ["U1.5", "C1.1", "U2.4", "U2.6", "C2.1", "U3.8", "U3.6", "C3.1", "R1.1"],
    "GND": ["J1.2", "U1.2", "C1.2", "C2.2", "C3.2", "C4.2", "U2.3", "U2.5", "U2.21", "U3.1", "U3.7", "U3.5", "D1.1"],
    "/RESET": ["U2.29", "R1.2"],
    "/SDA": ["U2.27", "U3.3", "R2.2"],
    "/SCL": ["U2.28", "U3.4", "R3.2"],
    "/LED": ["U2.17", "D1.2"],                                            # bug 5
    "/XTAL1": ["U2.7", "Y1.1"],                                           # bug 6
    "/XTAL2": ["U2.8", "Y1.2"],
    "/AREF": ["U2.20", "C4.1"],
}
# bug 2: U2.18 AVCC unconnected; bug 3: U3.2 CSB unconnected


def q(s):
    return '"' + s.replace('"', '\\"') + '"'


def build() -> str:
    pins = {ref: {n: (nm, t) for n, nm, t in p[4]} for ref, p in PARTS.items()}
    out = ['(export (version "E")', '  (design (source "demo_board.kicad_sch") (tool "make_demo_board.py"))',
           "  (components"]
    for ref, (val, lib, part, desc, _) in PARTS.items():
        out.append(f"    (comp (ref {q(ref)}) (value {q(val)}) (libsource (lib {q(lib)}) (part {q(part)}) "
                   f"(description {q(desc)})))")
    out += ["  )", "  (libparts"]
    seen = set()
    for ref, (val, lib, part, desc, pl) in PARTS.items():
        if (lib, part) in seen:
            continue
        seen.add((lib, part))
        ps = " ".join(f"(pin (num {q(n)}) (name {q(nm)}) (type {q(t)}))" for n, nm, t in pl)
        out.append(f"    (libpart (lib {q(lib)}) (part {q(part)}) (pins {ps}))")
    out += ["  )", "  (nets"]
    used = set()
    code = 1
    for name, nodes in NETS.items():
        out.append(f"    (net (code {q(str(code))}) (name {q(name)})")
        code += 1
        for node in nodes:
            ref, n = node.split(".")
            nm, t = pins[ref][n]
            used.add(node)
            out.append(f"      (node (ref {q(ref)}) (pin {q(n)}) (pinfunction {q(nm)}) (pintype {q(t)}))")
        out.append("    )")
    for ref, p in PARTS.items():
        for n, nm, t in p[4]:
            if f"{ref}.{n}" not in used:
                t2 = t if t.endswith("no_connect") or t != "no_connect" else t
                out.append(f"    (net (code {q(str(code))}) (name {q(f'unconnected-({ref}-Pad{n})')})")
                out.append(f"      (node (ref {q(ref)}) (pin {q(n)}) (pinfunction {q(nm)}) "
                           f"(pintype {q(t2 if t != 'no_connect' else 'passive+no_connect')})))")
                code += 1
    out += ["  )", ")"]
    return "\n".join(out) + "\n"


if __name__ == "__main__":
    p = Path(__file__).with_name("demo_board.net")
    p.write_text(build(), encoding="utf-8")
    print(f"wrote {p}")
