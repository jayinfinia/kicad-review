"""kicad-review: design review for KiCad schematics/netlists.

  python -m kicad_review board.kicad_sch            # rules only, markdown to stdout
  python -m kicad_review board.net --llm -o out.md  # rules + Claude review
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import checks, netlist, report


def main(argv=None):
    ap = argparse.ArgumentParser(prog="kicad-review", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input", help=".kicad_sch (needs kicad-cli) or KiCad .net netlist")
    ap.add_argument("--llm", action="store_true", help="add the Claude review pass (needs ANTHROPIC_API_KEY)")
    ap.add_argument("--model", default=None, help="Claude model id (default claude-opus-5-5)")
    ap.add_argument("--effort", default="high", choices=["low", "medium", "high", "xhigh", "max"])
    ap.add_argument("-o", "--output", help="write report here (.md or .json); default stdout markdown")
    ap.add_argument("--fail-on", choices=["error", "warning", "never"], default="never",
                    help="exit 1 if a finding at this severity or worse remains (for CI)")
    a = ap.parse_args(argv)

    d = netlist.load(a.input)
    findings = checks.run_all(d)
    summary, meta = None, {"mode": "rules"}
    if a.llm:
        from . import llm
        rv, usage = llm.review(d, findings, model=a.model or llm.DEFAULT_MODEL, effort=a.effort)
        findings, summary = llm.merge(findings, rv), rv.summary
        meta = {"mode": "rules+llm", **usage}

    fmt = report.to_json if a.output and a.output.endswith(".json") else report.to_markdown
    text = fmt(d, findings, summary, meta)
    if a.output:
        Path(a.output).write_text(text, encoding="utf-8")
        print(f"wrote {a.output}: {len(findings)} findings", file=sys.stderr)
    else:
        sys.stdout.write(text)

    sev = {"error": 0, "warning": 1, "info": 2}
    if a.fail_on != "never" and any(sev[f.severity] <= sev[a.fail_on] for f in findings):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
