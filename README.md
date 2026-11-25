# kicad-review

Automated schematic design review for KiCad. It reads a schematic or netlist and runs four kinds of
analysis. The first three are deterministic and need no API key. The fourth is an optional Claude pass
that reviews the results of the first three:

1. **Structural rules.** Graph checks on the netlist: floating pins, missing decoupling, split or
   misspelled rails, supply pins on ground, capacitor voltage ratings.
2. **DC bias solver.** Nodal analysis of the resistor network (numpy) gives every net's idle voltage and
   how strongly it is held, in two board states. That turns pull-up review into a physics question.
   Is this active-low input held asserted? Does this pin idle above its I/O voltage limit? Does a
   MOSFET gate float while the MCU is still in reset?
3. **Part-constraint database.** Datasheet facts the netlist can't tell you, with the source cited:
   BME280 pins max 4.25 V and CSB must be high for I²C, AP2112 needs 1 µF in and out, MAX202 needs V+/V-
   reservoir caps, 24Cxx SDA is open-drain, ATmega RESET has an internal pull-up.
4. **LLM review (optional).** Claude gets the netlist, the solved bias voltages and every finding.
   It confirms or dismisses each finding with a reason, and it adds issues that need broader part
   knowledge.

```
python -m kicad_review board.kicad_sch               # analyses 1-3, no API key
python -m kicad_review board.net --llm -o review.md  # + Claude review
python -m kicad_review board.net --fail-on error     # CI gate
```

## Methods

### DC bias solver (`dcbias.py`)

Every net becomes a node. Supply and ground nets are fixed-voltage nodes, with the voltage parsed from
the name (`+3V3`, `+3.3V`, `VDD_1V8`). Plain `VCC` is assumed to be 5 V, and findings that depend on
that assumption are suppressed. Resistors (including bussed and isolated resistor networks) are
conductances. Inductors, ferrites and net ties are near-shorts. Capacitors are open at DC. Rails bound
the islands, so each resistive island is solved on its own:

```
G_ff · v_f = -G_fk · v_k
```

This is solved for the free nodes. The diagonal of `G_ff⁻¹` gives each net's Thevenin resistance. A net
with no resistive path to a rail is *floating*.

The solver runs twice, for two board states:

- **As drawn.** Jumpers are closed. Connectors, MCU/FPGA I/O and passive-typed IC pins may drive a net.
- **Power-on.** Jumpers are open, headers are unplugged, and MCU/FPGA I/O is high-Z (as it is in reset).
  Only push-pull outputs of ICs drive. This is the state where a missing or inverted pull-up matters,
  and it is reported only when a jumper or connector makes it differ from the drawn state.

Transistor gates and bases never count as drivers. Diodes count as drivers only when fed from a rail or
a push-pull net. Old KiCad 5 pin types (`BiDi`, `3state`, `openCol`) are normalised.

Checks on the result:
- `bias-overvoltage`: a signal pin idles above its abs-max (parts DB, or the IC's I/O rail + 0.3 V).
  This also catches pull-ups to the wrong rail through dividers and series resistors.
- `active-low-asserted` / `active-low-indeterminate`: a RESET#, ~{CS}, nEN-style input idles below 30%,
  or between 30% and 70%, of its I/O rail. Polarity comes from the pin name. It also comes from a
  designer-named net when the pin is an input, or the only IC pin on the net (a card-detect or cable
  signal arriving from off-board).
- `floating-signal-input`: an input, or a configuration pin (BOOT, MODE, TCK, …), with no driver and no
  bias resistor.
- `gate-floating-at-reset` / `gate-asserted-at-reset`: a MOSFET gate or BJT base that floats, or sits
  on, while the MCU driving it is still in reset.
- `supply-pin-unpowered`: a VCC/VDD pin drawn as an input in an old symbol, with no DC path to a rail.
- `part-strap`: a pin the datasheet says must be high or low isn't.

### Part-constraint database (`data/parts.json`)

About sixteen part families, matched by regex on value or library part. Each entry can set an I/O
voltage limit, pins with internal pull-ups, open-drain pins, and requirements for each pin
(`connected`, `high`, `low`, `cap_to_gnd` with a minimum µF). Each entry has a `source` field citing
the datasheet, and that citation is printed with the finding. Adding a part is a JSON edit.

### Structural, protocol and topology rules (`checks.py`, `checks_physics.py`)

| Category | Rules |
|---|---|
| Decoupling | `missing-decoupling`, `thin-decoupling`, `part-missing-cap`, `part-cap-too-small` |
| Connectivity | `floating-input`, `floating-signal-input`, `unconnected-power-pin`, `undriven-input-net`, `part-pin-unconnected` |
| Pull-ups / bias | `bias-overvoltage`, `active-low-asserted`, `part-strap`, `gate-floating-at-reset`, `open-drain-no-pullup`, `reset-floating` |
| Buses | `i2c-missing-pullup`, `i2c-pullup-value`, `sdio-missing-pullup`, `rs485-no-failsafe-bias` |
| Power | `supply-pin-on-ground`, `ground-pin-on-supply`, `split-rail`, `single-pin-rail`, `undriven-supply`, `supply-pin-unpowered`, `feedback-wrong-rail`, `cap-overvoltage`, `cap-low-derating`, `led-no-current-limit` |
| Other | `crystal-no-load-caps` |

`feedback-wrong-rail` follows a regulator's FB divider to check that its top connects to the regulator's
own output. Analysis is at netlist level only, so placement, trace length and cap-to-pin distance are
out of scope.

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
`eval/seed_bugs.py` applies one mutation per variant and records ground truth. There are 11 bug types.
`eval/run_eval.py` reviews the clean design and the variant, and scores only the findings that are new in
the variant. A bug counts as caught if a new finding names one of the affected refs or nets (or a net one
resistor away). Precision is the share of new findings that point at the planted bug.

Two sets of source designs:

- **Development:** eight demo projects that ship with KiCad 7 (USB hub, PIC programmer, ColdFire dev
  kit, video board, tube amp and others, 15–189 parts). Two random splits, seeds 7 and 42, were used
  while writing the rules.
- **Held-out:** ten designs from open-source hardware on GitHub, scored once before any tuning on them:
  OtterCast Audio V2, the MCH2022 badge, Haxophone, HackRF One, Marzipan, Neapolitan and Opera Cake,
  the splitflap sensor, BitAxe Ultra and Stack-chan's pan-tilt board. `examples/export_heldout.sh`
  rebuilds them at pinned commits. They are not committed because each project has its own license.

### Results (no LLM)

| Bug type | Structural rules only (held-out) | Full analysis, held-out first look | Full analysis, held-out after fixes | Full analysis, dev (both splits) |
|---|---|---|---|---|
| Remove all decoupling caps on a rail | 6/6 | 6/6 | 6/6 | 12/12 |
| Disconnect an IC input / remove its tie-off | 6/6 | 6/6 | 6/6 | 12/12 |
| Disconnect an IC supply pin | 6/6 | 6/6 | 6/6 | 12/12 |
| Move an IC supply pin to ground | 6/6 | 6/6 | 6/6 | 12/12 |
| Misspell an IC's rail (`+3.3V` → `+3V3`) | 6/6 | 6/6 | 6/6 | 12/12 |
| Capacitor voltage rating below its rail | 6/6 | 6/6 | 6/6 | 12/12 |
| Remove a pull-up | 2/6 | 2/6 | 6/6 | 12/12 |
| Turn a pull-up into a pull-down | 0/6 | 2/6 | 6/6 | 12/12 |
| Move a pull-up to a higher rail | 0/6 | 2/6 | 5/5 † | 12/12 |
| Replace an LED's series resistor with a wire | 0/6 | 6/6 | 6/6 | 12/12 |
| Remove a crystal's load capacitors | 0/6 | 6/6 | 6/6 | 10/10 |
| **All** | **38/66 (58%)** | **54/66 (82%)** | **65/65 (100%)** | **130/130 (100%)** |
| Precision of new findings | 100% | 100% | 100% (76/76) | 100% (155/155) |

† One held-out variant was taken out after a hand review (`eval/label_audit.json`). It moved the bias
resistor on an RS-485 B line from +3.3 V to +5 V, but RS-485 pins are rated −7 V to +12 V, so that is
not a defect. Counting it as a miss gives 65/66.

**Read the numbers this way.** 82% is the honest held-out figure: the rules had never seen those
designs. Getting to 100% meant looking at the 12 misses and adding general rules for them: the power-on
board state, gate checks, SDIO and RS-485 bias, regulator feedback dividers, resistor networks, KiCad 5
pin types, and connector detection from footprints. No rule names a specific design, net or reference.
Still, the 100% has now been tuned on every design here. A fresh set of designs is the only way to get a
new clean number. Structural-only scores on the two dev splits are 62% and 60%.

The seeder is part of the result too. Early versions planted "bugs" that were not defects. For example,
a resistor on a net that a push-pull output already drives isn't a pull-up, and pulling an idle-low
signal down is harmless. `seed_bugs.py` now skips those sites, so the variant counts differ slightly
from earlier runs.

### False positives on unmodified designs

The full analysis reports warnings and errors on designs that, as far as anyone knows, work: 24 across
the eight KiCad demos and 78 across the ten held-out boards. These are not scored, because some are real
review comments (a 74HC input left floating, an I²C bus with no pull-up on this sheet, an SD card bus
missing its CMD/DAT pull-ups). Others are false positives. On the held-out boards, 40 are floating-input
findings, and 30 of them are on the four HackRF boards. Another 12 are SD-card pull-up warnings on HackRF
and the MCH2022 badge. Those boards ship and work, so the controller probably supplies the pull-ups
internally (not checked against the datasheets). This
noise is the main weakness, and it is the job the LLM pass is meant for: dismissing findings that need
part knowledge.

### LLM results: not run yet

This version was built without API credentials. To fill in the LLM rows:

```
export ANTHROPIC_API_KEY=...
python eval/run_eval.py --variants eval/variants_heldout --mode llm-only --limit 2   # Claude alone
python eval/run_eval.py --variants eval/variants_heldout --mode hybrid               # everything
```

The questions are whether Claude alone matches the deterministic analysis, and whether the hybrid
reduces the false positives above without losing recall.

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
examples/export_heldout.sh                    # -> examples/heldout_netlists/*.net
python eval/seed_bugs.py examples/netlists/*.net -o eval/variants_test --seed 42
python eval/seed_bugs.py examples/heldout_netlists/*.net -o eval/variants_heldout --seed 1
python eval/run_eval.py --variants eval/variants_heldout --mode rules         # full deterministic analysis
python eval/run_eval.py --variants eval/variants_heldout --mode rules-basic   # structural rules only
pytest
```

## Next steps

- Run the LLM and hybrid evals and add the numbers above.
- Score a second, untouched held-out set (KiCad 8/9 projects need a newer kicad-cli).
- Cut the floating-input noise by modelling FPGA/CPLD I/O banks.
- Grow the parts DB, ideally by extracting pin constraints from datasheet PDFs.
- Give Claude datasheet excerpts for the parts on the board instead of relying on memory.
- Add PCB-level checks from `.kicad_pcb` (decoupling cap distance to pin, return paths).
- Run as a GitHub Action that comments the report on pull requests that touch `*.kicad_sch`.
