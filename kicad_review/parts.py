"""Part-constraint database: datasheet facts the rule engine can't infer from a netlist."""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

from .netlist import Component

_DB = Path(__file__).with_name("data") / "parts.json"


@lru_cache(maxsize=1)
def _load():
    entries = json.loads(_DB.read_text(encoding="utf-8"))["parts"]
    for e in entries:
        e["_re"] = re.compile(e["match"], re.I)
    return entries


def lookup(c: Component) -> dict | None:
    for e in _load():
        if e["_re"].search(c.value or "") or e["_re"].search(c.part or ""):
            return e
    return None


def pin_matches(pattern: str | None, name: str) -> bool:
    if not pattern or not name:
        return False
    return any(re.search(pattern, part, re.I) for part in re.split(r"[/]", name.replace("~{", "").replace("}", "")))
