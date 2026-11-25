"""Score the reviewer on seeded-bug variants.

For each variant we run the reviewer on the clean design and on the variant and
look only at findings that are *new* in the variant (so pre-existing findings on
the clean design neither help nor hurt). A bug counts as detected if a new
finding points at one of the label's refs or nets.

  python eval/run_eval.py --mode rules
  python eval/run_eval.py --mode llm-only --limit 9      # needs ANTHROPIC_API_KEY
  python eval/run_eval.py --mode hybrid
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kicad_review import checks, netlist  # noqa: E402
from kicad_review.analysis import leaf  # noqa: E402


def findings_for(path: str, mode: str, model: str | None, cache: dict):
    key = (path, mode)
    if key in cache:
        return cache[key]
    d = netlist.load(path)
    rules = checks.run_all(d)
    usage = {}
    if mode == "rules":
        fs = rules
    elif mode == "rules-basic":
        fs = checks.run_all(d, physics=False)
    else:
        from kicad_review import llm
        given = rules if mode == "hybrid" else []
        rv, usage = llm.review(d, given, model=model or llm.DEFAULT_MODEL)
        fs = llm.merge(given, rv)
        fs = [f for f in fs if f.severity != "info" or f.source == "llm"]  # dismissed rule findings drop out
    cache[key] = (fs, usage)
    return fs, usage


def fkey(f):
    return (f.rule, f.message) if f.source == "rules" else ("llm", tuple(sorted(f.refs)), tuple(sorted(f.nets)), f.category)


def hits(f, label, design=None) -> bool:
    """A finding is about the seeded bug if it names one of its refs or nets, or a net one resistor
    away from them (the same bias network seen from another pin)."""
    nets = {leaf(n) for n in label["nets"]} | set(label["nets"])
    if design is not None:
        from kicad_review.analysis import is_ground, is_power, is_resistor, other_pin
        for n in list(label["nets"]):
            for p in design.pins_on(n):
                c = design.components.get(p.ref)
                if c and is_resistor(c):
                    o = other_pin(c, p)
                    if o and o.net and not is_power(o.net) and not is_ground(o.net):
                        nets |= {o.net, leaf(o.net)}
    return bool(set(f.refs) & set(label["refs"])) or bool({leaf(n) for n in f.nets} & nets)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--variants", default="eval/variants")
    ap.add_argument("--mode", choices=["rules", "rules-basic", "llm-only", "hybrid"], default="rules")
    ap.add_argument("--model", default=None)
    ap.add_argument("--limit", type=int, default=0, help="first N per bug type (LLM runs cost money)")
    ap.add_argument("--min-severity", choices=["error", "warning", "info"], default="warning")
    ap.add_argument("-o", "--out", default=None)
    a = ap.parse_args(argv)
    sev_ok = {"error": {"error"}, "warning": {"error", "warning"}, "info": {"error", "warning", "info"}}[a.min_severity]

    manifest = json.loads((Path(a.variants) / "manifest.json").read_text())
    if a.limit:
        seen = defaultdict(int)
        manifest = [m for m in manifest if (seen.__setitem__(m["bug"], seen[m["bug"]] + 1) or seen[m["bug"]]) <= a.limit]

    audit_path = Path(__file__).with_name("label_audit.json")
    excluded = json.loads(audit_path.read_text())["excluded"] if audit_path.exists() else {}
    cache, rows = {}, []
    tokens = defaultdict(int)
    t0 = time.time()
    for m in manifest:
        base, u0 = findings_for(m["clean"], a.mode, a.model, cache)
        var, u1 = findings_for(str(Path(a.variants) / f"{m['id']}.net"), a.mode, a.model, cache)
        for u in (u0, u1):
            for k in ("input_tokens", "output_tokens"):
                tokens[k] += u.get(k, 0)
        base_keys = {fkey(f) for f in base}
        new = [f for f in var if fkey(f) not in base_keys and f.severity in sev_ok]
        vd = netlist.load(str(Path(a.variants) / f"{m['id']}.net"))
        good = [f for f in new if hits(f, m, vd)]
        rows.append({"id": m["id"], "bug": m["bug"], "detail": m["detail"], "detected": bool(good),
                     "excluded": excluded.get(m["id"]),
                     "new_findings": len(new), "unrelated_new": len(new) - len(good),
                     "matched": [f"{f.rule}: {f.message}" for f in good][:3],
                     "unmatched": [f"{f.rule}: {f.message}" for f in new if f not in good][:3]})
        print(f"{'✓' if good else '✗'} {m['id']:55s} {m['detail'][:80]}")

    by = defaultdict(lambda: [0, 0, 0, 0])
    raw_n, raw_det = len(rows), sum(r["detected"] for r in rows)
    for r in rows:
        if r["excluded"]:
            continue
        b = by[r["bug"]]
        b[0] += 1
        b[1] += r["detected"]
        b[2] += r["new_findings"] - r["unrelated_new"]
        b[3] += r["unrelated_new"]
    total = [sum(v[i] for v in by.values()) for i in range(4)]

    # noise on clean designs
    clean = sorted({m["clean"] for m in manifest})
    noise = {Path(c).stem: sum(1 for f in findings_for(c, a.mode, a.model, cache)[0] if f.severity in sev_ok)
             for c in clean}

    lines = [f"## Results: mode={a.mode}, min severity={a.min_severity}", "",
             "| Bug type | Variants | Detected | Recall | Unrelated new findings |", "|---|---|---|---|---|"]
    for bug, (n, det, rel, unrel) in by.items():
        lines.append(f"| {bug} | {n} | {det} | {det / n:.0%} | {unrel} |")
    lines.append(f"| **all** | **{total[0]}** | **{total[1]}** | **{total[1] / total[0]:.0%}** | **{total[3]}** |")
    prec = total[2] / (total[2] + total[3]) if (total[2] + total[3]) else 0
    n_ex = sum(1 for r in rows if r["excluded"])
    if n_ex:
        lines += ["", f"{n_ex} variant(s) excluded after hand review as not real defects (eval/label_audit.json). "
                  f"Raw score including them: {raw_det}/{raw_n} ({raw_det / raw_n:.0%})."]
    lines += ["", f"Precision of new findings (point at the seeded bug): {prec:.0%} "
              f"({total[2]} of {total[2] + total[3]})", "",
              "Findings on the unmodified designs (not scored; may be real issues or false positives): "
              + ", ".join(f"{k} {v}" for k, v in noise.items())]
    if tokens.get("input_tokens"):
        lines.append(f"\nTokens: {tokens['input_tokens']:,} in / {tokens['output_tokens']:,} out, "
                     f"{time.time() - t0:.0f}s")
    print("\n" + "\n".join(lines))
    out = Path(a.out or f"eval/results_{a.mode}.json")
    out.write_text(json.dumps({"summary": "\n".join(lines), "rows": rows}, indent=2), encoding="utf-8")
    Path(str(out).replace(".json", ".md")).write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
