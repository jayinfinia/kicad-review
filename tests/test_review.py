from pathlib import Path
from types import SimpleNamespace

import pytest

from kicad_review import checks, llm, netlist
from kicad_review.analysis import is_ground, is_power, parse_value, rail_voltage

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "examples" / "demo_board.net"


@pytest.mark.parametrize("net,v", [("+3V3", 3.3), ("+3.3V", 3.3), ("/+5V", 5.0), ("+1V8", 1.8),
                                   ("-12V", -12.0), ("VDD_3V3", 3.3), ("VCC", None)])
def test_rail_voltage(net, v):
    assert rail_voltage(net) == v


def test_net_classes():
    assert is_ground("GND") and is_ground("/sub/AGND") and not is_ground("+5V")
    assert is_power("+3V3") and is_power("VCC") and not is_power("Net-(U1-Pad3)")


@pytest.mark.parametrize("s,v", [("4.7k", 4700), ("4k7", 4700), ("2,2K", 2200), ("100n", 1e-7), ("10K 1%", 1e4)])
def test_parse_value(s, v):
    assert parse_value(s) == pytest.approx(v)


def test_demo_board_rules():
    d = netlist.load(DEMO)
    rules = {f.rule for f in checks.run_all(d)}
    # all six planted bugs, without any LLM
    assert {"floating-input", "unconnected-power-pin", "missing-decoupling", "bias-overvoltage",
            "led-no-current-limit", "crystal-no-load-caps", "part-strap"} <= rules


def test_dc_bias_divider_and_floating():
    from kicad_review import dcbias
    d = netlist.load(DEMO)
    b = dcbias.solve(d)
    assert b["/RESET"].volts == pytest.approx(3.3) and b["/RESET"].r_thevenin == pytest.approx(10e3)
    assert b["/SDA"].volts == pytest.approx(5.0)
    assert b["/XTAL1"].volts is None  # no resistive path to a rail
    # add 1k from /RESET to GND: 10k/1k divider -> 0.3 V via 909 Ohm, MCU held in reset
    from kicad_review.netlist import Component, Pin
    r = Component("R99", "1k", lib="Device", part="R")
    for num, net in (("1", "/RESET"), ("2", "GND")):
        pin = Pin("R99", num, "~", "passive", net)
        r.pins[num] = pin
        d.nets[net].nodes.append(pin)
    d.components["R99"] = r
    b = dcbias.solve(d)
    assert b["/RESET"].volts == pytest.approx(0.3) and b["/RESET"].r_thevenin == pytest.approx(1e4 / 11)
    assert any(f.rule == "part-strap" and "RESET" in f.message for f in checks.run_all(d))


def test_roundtrip():
    d = netlist.load(DEMO)
    d2 = netlist.parse(netlist.to_netlist_text(d))
    assert {n: sorted(p.label() for p in net.nodes) for n, net in d.nets.items()} == \
           {n: sorted(p.label() for p in net.nodes) for n, net in d2.nets.items()}


def test_llm_merge_with_fake_client():
    d = netlist.load(DEMO)
    rules = checks.run_all(d)
    canned = llm.Review(
        summary="ok",
        rule_verdicts=[llm.RuleVerdict(index=0, verdict="dismiss", reason="test")],
        findings=[llm.LLMFinding(category="other", severity="error", refs=["D1"], nets=["/LED"],
                                 message="LED has no series resistor", rationale="r", confidence=0.9)])
    resp = SimpleNamespace(stop_reason="end_turn", parsed_output=canned, model="fake",
                           usage=SimpleNamespace(input_tokens=1, output_tokens=1))
    fake = SimpleNamespace(messages=SimpleNamespace(parse=lambda **kw: resp),
                           beta=SimpleNamespace(messages=SimpleNamespace(parse=lambda **kw: resp)))
    rv, usage = llm.review(d, rules, client=fake)
    merged = llm.merge(rules, rv)
    assert merged[0].severity == "info" and "dismissed" in merged[0].rationale
    assert merged[-1].source == "llm" and merged[-1].refs == ["D1"]
