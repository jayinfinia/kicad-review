# kicad-review

Automated schematic design review for KiCad. It reads a KiCad schematic or netlist, runs deterministic
electrical rule checks, then (optionally) hands the netlist and the rule findings to Claude, which
triages the rule hits and adds the issues that need part knowledge. The output is a review report in
Markdown or JSON, and the tool can fail a CI job on errors.

```
python -m kicad_review board.kicad_sch               # rules only
python -m kicad_review board.net --llm -o review.md  # rules + Claude review
```

## Why two layers

| | Rules (`checks.py`) | LLM pass (`llm.py`) |
|---|---|---|
| Good at | Exhaustive structural checks: every pin, every rail, every net | Part-specific knowledge: "AP2112 needs 1 µF on VIN", "BME280 CSB must be tied high for I²C", "this LED has no series resistor" |
| Weak at | Anything that needs a datasheet | Exhaustive bookkeeping on 500-net designs; can hallucinate |
| Cost | Free, ~50 ms | One API call per review |

So the rules run first and their findings go into the prompt. Claude confirms or dismisses each one
(with a reason, kept in the report) and adds its own findings with a confidence score. Structured
outputs (Pydantic schema) keep the reply machine-readable, so findings from both layers merge into one
report.

## What it checks

| Category | Rules |
|---|---|
| Decoupling | `missing-decoupling`: IC supply rail with no capacitor to ground. `thin-decoupling`: fewer caps than ICs on a rail |
| Connectivity | `floating-input`, `unconnected-power-pin`: pins with no net and no no-connect flag. `undriven-input-net`: net made only of inputs |
| Pull-ups | `i2c-missing-pullup` (also flags a pull-down where a pull-up should be), `i2c-pullup-value` (outside 1k–10k), `pullup-wrong-rail` (pulled above the IC's supply), `active-low-pulled-down` (RESET#, ~{CS} held asserted), `open-drain-no-pullup`, `reset-floating` |
| Power rails | `supply-pin-on-ground`, `ground-pin-on-supply`, `split-rail` (+3V3 vs +3.3V), `single-pin-rail` (misspelled power label), `undriven-supply`, `cap-overvoltage`, `cap-low-derating` |

Netlist-level only: placement, trace length and cap-to-pin distance are out of scope.

## How it works

```
.kicad_sch ──kicad-cli──▶ .net ──▶ netlist.py ──▶ Design model ──▶ checks.py ──▶ findings
                                                       │                           │
                                                       └──▶ llm.py (Claude) ◀──────┘
                                                              │
                                                              ▼
                                                   report.py (Markdown / JSON)
```

- `kicad_review/sexpr.py`: small S-expression reader/writer (no dependencies).
- `kicad_review/netlist.py`: loads KiCad 6/7/8 netlists. A `.kicad_sch` is converted with `kicad-cli`
  first, so KiCad itself resolves wires, labels, power symbols and hierarchy.
- `kicad_review/analysis.py`: heuristics for ground/supply nets, rail voltage from net names
  (`+3V3`, `+3.3V`, `VDD_1V8`), component values (`4k7`, `2,2K`, `100n`) and part classes.
- `kicad_review/checks.py`: the rules.
- `kicad_review/llm.py`: renders the design as compact text (`REF.PIN/NAME[type]` per net), calls
  Claude (`claude-opus-5-5` by default, adaptive thinking at `high` effort, structured output, server-side
  refusal fallback), and merges its review with the rule findings.

## Evaluation

There is no public dataset of buggy KiCad designs, so the eval plants bugs into clean ones.
`eval/seed_bugs.py` takes eight open-source demo projects that ship with KiCad 7 (USB hub, PIC
programmer, ColdFire dev kit, video board, tube amp and others: 15–189 parts each), applies one
mutation per variant and records ground truth. `eval/run_eval.py` reviews the clean design and the
variant and scores only the findings that are new in the variant. A bug counts as caught if a new
finding names one of the affected refs or nets.

Nine bug types, six variants each (54 variants), on a held-out split sampled with a different seed
from the one used while writing the rules:

| Bug type | Recall (rules) |
|---|---|
| Remove all decoupling caps on a rail | 6/6 |
| Disconnect an IC input / remove its tie-off | 6/6 |
| Disconnect an IC supply pin | 6/6 |
| Move an IC supply pin to ground | 6/6 |
| Misspell an IC's rail (`+3.3V` → `+3V3`) | 6/6 |
| Capacitor voltage rating below its rail | 6/6 |
| Remove a pull-up | 2/6 |
| Turn a pull-up into a pull-down | 0/6 |
| Move a pull-up to a higher rail | 2/6 |
| **All** | **40/54 (74%)**, precision 100% (50/50 new findings point at the bug) |

The rules catch structural bugs reliably and miss the semantic pull-up bugs: knowing that `TEST1#` on
a USB hub must sit high, or that an FE1.1s's I/O runs at 3.3 V even though it has a 5 V supply pin,
needs part knowledge. That gap is what the LLM pass is for.

**LLM results: not run yet.** This first version was built without API credentials. To fill in the
LLM rows:

```
export ANTHROPIC_API_KEY=...
python eval/run_eval.py --mode llm-only --limit 2   # 18 variants, Claude alone
python eval/run_eval.py --mode hybrid               # all 54, rules + Claude
```

Caveats: the pull-up seeder treats any resistor from an IC net to a rail as a pull-up, so a few
"bugs" (for example a resistor on a diode anode in the PIC programmer) may not be real defects, and an
LLM that says so is scored as a miss. With only eight source designs, the dev and test splits share
some sites for the rarer bug types.

Findings on the unmodified designs are reported but not scored: 11 across the eight designs, mostly
`thin-decoupling` warnings and two I²C lines on the PIC programmer with no pull-up.

## Demo board

`examples/make_demo_board.py` builds a small synthetic board (5 V → AP2112 LDO → ATmega328P + BME280
over I²C) with six planted bugs. The rules find four ([report](examples/demo_board_review_rules.md)).
The other two, an LED with no series resistor and a crystal with no load caps, are there for the LLM
pass to find.

## Setup

```
pip install -e .[dev]
sudo apt install kicad kicad-demos            # KiCad 7+, only needed for .kicad_sch input and the eval
examples/export_kicad_demos.sh                # -> examples/netlists/*.net
python eval/seed_bugs.py examples/netlists/*.net -o eval/variants_test --seed 42
python eval/run_eval.py --variants eval/variants_test --mode rules
pytest
```

The netlists exported from KiCad's demo projects are not committed. They are regenerated from the
`kicad-demos` package.

## Next steps

- Run the LLM and hybrid evals and add the numbers above.
- Give Claude datasheet excerpts for the parts on the board (tool use or retrieval) instead of relying on memory.
- Add PCB-level checks from `.kicad_pcb` (decoupling cap distance to pin, return paths).
- Run as a GitHub Action that comments the report on pull requests that touch `*.kicad_sch`.
