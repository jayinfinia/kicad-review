# kicad-review

Automated schematic design review for KiCad. It reads a schematic or netlist and runs four kinds of
analysis. Three are deterministic and need no API key. The fourth, an optional Claude pass, reviews the
results of the first three:

1. **Structural rules.** Graph checks on the netlist: floating pins, missing decoupling, split or
   misspelled rails, supply pins on ground, capacitor voltage ratings.
2. **DC bias solver.** Nodal analysis of the resistor network (numpy) gives every net's idle voltage and
   how strongly it is held. That turns pull-up review into a physics question: is this active-low
   input held asserted? Does this pin idle above its I/O voltage limit?
3. **Part-constraint database.** Datasheet facts the netlist can't tell you, with the source cited:
   BME280 pins max 4.25 V and CSB must be high for I²C, AP2112 needs 1 µF in and out, MAX202 needs V+/V-
   reservoir caps, 24Cxx SDA is open-drain, ATmega RESET has an internal pull-up.
4. **LLM review (optional).** Claude gets the netlist, the solved bias voltages and every finding.
   It confirms or dismisses each finding with a reason and adds issues that need broader part knowledge.

```
python -m kicad_review board.kicad_sch               # analyses 1-3, no API key
python -m kicad_review board.net --llm -o review.md  # + Claude review
python -m kicad_review board.net --fail-on error     # CI gate
```

## Methods

### DC bias solver (`dcbias.py`)

Every net becomes a node. Supply and ground nets are fixed-voltage nodes, with the voltage parsed from
the name (`+3V3`, `+3.3V`, `VDD_1V8`; plain `VCC` is assumed to be 5 V and findings that depend on that
are suppressed). Resistors are conductances, inductors, ferrites and jumpers are near-shorts, and
capacitors are open at DC. For each connected resistive island it solves

```
G_ff · v_f = -G_fk · v_k
```

for the free nodes, and takes the diagonal of `G_ff⁻¹` as each net's Thevenin resistance. A net with no
resistive path to a rail is *floating*. A net touched by an output, a bidirectional or tri-state pin,
or a connector is *driven*, so checks that assume the resistor network sets its level skip it.

Checks on the result:
- `bias-overvoltage`: a signal pin idles above its abs-max (parts DB, or the IC's I/O rail + 0.3 V).
  This catches pull-ups to the wrong rail through dividers and series resistors too, not only the
  direct case.
- `active-low-asserted` / `active-low-indeterminate`: a RESET#, ~{CS}, nEN-style input (from the pin
  name or a designer-named net) idles below 30% or 30–70% of its I/O rail.
- `part-strap`: a pin the datasheet says must be high or low isn't.

### Part-constraint database (`data/parts.json`)

About fifteen part families, matched by regex on value or library part. Each entry can set an I/O
voltage limit, pins with internal pull-ups, open-drain pins, and requirements per pin
(`connected`, `high`, `low`, `cap_to_gnd` with a minimum µF). Each entry has a `source` field citing
the datasheet, and that citation is printed with the finding. Adding a part is a JSON edit.

### Structural and topology rules (`checks.py`, `checks_physics.py`)

| Category | Rules |
|---|---|
| Decoupling | `missing-decoupling`, `thin-decoupling`, `part-missing-cap`, `part-cap-too-small` |
| Connectivity | `floating-input`, `unconnected-power-pin`, `undriven-input-net`, `part-pin-unconnected` |
| Pull-ups / bias | `bias-overvoltage`, `active-low-asserted`, `part-strap`, `i2c-missing-pullup`, `i2c-pullup-value`, `open-drain-no-pullup`, `reset-floating` |
| Power | `supply-pin-on-ground`, `ground-pin-on-supply`, `split-rail`, `single-pin-rail`, `undriven-supply`, `cap-overvoltage`, `cap-low-derating`, `led-no-current-limit` |
| Other | `crystal-no-load-caps` |

Netlist-level only: placement, trace length and cap-to-pin distance are out of scope.

### LLM review (`llm.py`)

The design is rendered as compact text: components with values and MPNs, every net as
`REF.PIN/NAME[type]`, the solved DC bias table, and the numbered findings. Claude (`claude-opus-5-5`,
adaptive thinking, `high` effort) returns a Pydantic-validated `Review`. That contains a verdict for
each finding (dismissed findings stay in the report as info, with the reason) and new findings with a
confidence score.

```
.kicad_sch ─kicad-cli─▶ .net ─▶ netlist.py ─▶ Design ─┬─▶ checks.py ──────────────┐
                                                     ├─▶ dcbias.py ─▶ checks_physics.py (+ parts.json)
                                                     └─▶ llm.py (optional) ◀─────┘ findings + bias table
                                                                   └─▶ report.py (Markdown / JSON)
```

## Evaluation

There is no public dataset of buggy KiCad designs, so the eval plants bugs into clean ones.
`eval/seed_bugs.py` takes eight open-source demo projects that ship with KiCad 7 (USB hub, PIC
programmer, ColdFire dev kit, video board, tube amp and others: 15–189 parts each), applies one
mutation per variant and records ground truth. `eval/run_eval.py` reviews the clean design and the
variant and scores only the findings that are new in the variant. A bug counts as caught if a new
finding names one of the affected refs or nets.

65 variants across 11 bug types (seed 42 split, no LLM):

| Bug type | Structural rules only | + DC solver, parts DB, topology |
|---|---|---|
| Remove all decoupling caps on a rail | 6/6 | 6/6 |
| Disconnect an IC input / remove its tie-off | 6/6 | 6/6 |
| Disconnect an IC supply pin | 6/6 | 6/6 |
| Move an IC supply pin to ground | 6/6 | 6/6 |
| Misspell an IC's rail (`+3.3V` → `+3V3`) | 6/6 | 6/6 |
| Capacitor voltage rating below its rail | 6/6 | 6/6 |
| Move a pull-up to a higher rail | 0/6 | **5/6** |
| Replace an LED's series resistor with a wire | 0/6 | **5/6** |
| Remove a crystal's load capacitors | 0/5 | **5/5** |
| Remove a pull-up | 2/6 | 2/6 |
| Turn a pull-up into a pull-down | 0/6 | 0/6 |
| **All** | **38/65 (58%)** | **53/65 (82%)** |

Precision stayed at 100%: all 66 new findings point at the planted bug. On the eight unmodified designs
the full analysis reports 14 findings, mostly `thin-decoupling` warnings, open-drain lines with no
visible pull-up, and one crystal with no load caps on the USB hub. These are not scored because some
may be real issues. The other split (seed 7) gives 58% and 83%.

What's left: removed or inverted pull-ups on pins whose purpose only shows up in the datasheet (a
video DAC's `MCTR`, a USB hub's `VBUS_SENSE`, a 74LS688's `CS2`), and pulled-down signals that also go
to a header (treated as possibly jumpered, so skipped). The parts DB can be extended for these, and
they are what the LLM pass should catch.

**LLM results: not run yet.** This version was built without API credentials. To fill in the LLM rows:

```
export ANTHROPIC_API_KEY=...
python eval/run_eval.py --variants eval/variants_test --mode llm-only --limit 2   # 22 variants, Claude alone
python eval/run_eval.py --variants eval/variants_test --mode hybrid               # all 65, everything
```

Caveats: the seeder treats any resistor from an IC net to a rail as a pull-up, so a few planted "bugs"
may not be real defects. With eight source designs the two splits share sites for the rarer bug types,
and I looked at misses on both while writing the LED and crystal rules. A larger corpus of designs
would make a cleaner held-out test.

## Demo board

`examples/make_demo_board.py` builds a small synthetic board (5 V → AP2112 LDO → ATmega328P + BME280
over I²C) with six planted bugs: a missing LDO input cap, unconnected AVCC, a floating BME280 CSB, I²C
pull-ups to 5 V, an LED with no resistor, and a crystal with no load caps. The analysis catches all
six without an LLM ([report](examples/demo_board_review_rules.md)).

## Setup

```
pip install -e .[dev]
sudo apt install kicad kicad-demos            # KiCad 7+, only needed for .kicad_sch input and the eval
examples/export_kicad_demos.sh                # -> examples/netlists/*.net
python eval/seed_bugs.py examples/netlists/*.net -o eval/variants_test --seed 42
python eval/run_eval.py --variants eval/variants_test --mode rules         # full deterministic analysis
python eval/run_eval.py --variants eval/variants_test --mode rules-basic   # structural rules only
pytest
```

The netlists exported from KiCad's demo projects are not committed. They are regenerated from the
`kicad-demos` package.

## Next steps

- Run the LLM and hybrid evals and add the numbers above.
- Grow the parts DB, ideally by extracting pin constraints from datasheet PDFs.
- Give Claude datasheet excerpts for the parts on the board (tool use or retrieval) instead of relying on memory.
- Add PCB-level checks from `.kicad_pcb` (decoupling cap distance to pin, return paths).
- Run as a GitHub Action that comments the report on pull requests that touch `*.kicad_sch`.
