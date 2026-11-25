"""Minimal S-expression reader/writer for KiCad files (netlists, schematics).

KiCad's format is simple: parenthesised lists of atoms and double-quoted strings.
We keep atoms as Python str; quoted strings are returned as `QStr` so that the
writer can round-trip them faithfully.
"""
from __future__ import annotations


class QStr(str):
    """A string that was double-quoted in the source."""


def _tokenize(text: str):
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c in " \t\r\n":
            i += 1
        elif c in "()":
            yield c
            i += 1
        elif c == '"':
            i += 1
            buf = []
            while i < n and text[i] != '"':
                if text[i] == "\\" and i + 1 < n:
                    nxt = text[i + 1]
                    buf.append({"n": "\n", "t": "\t", '"': '"', "\\": "\\"}.get(nxt, nxt))
                    i += 2
                else:
                    buf.append(text[i])
                    i += 1
            i += 1
            yield QStr("".join(buf))
        else:
            j = i
            while j < n and text[j] not in ' \t\r\n()"':
                j += 1
            yield text[i:j]
            i = j


def loads(text: str):
    stack: list[list] = [[]]
    for tok in _tokenize(text):
        if tok == "(" and not isinstance(tok, QStr):
            stack.append([])
        elif tok == ")" and not isinstance(tok, QStr):
            done = stack.pop()
            stack[-1].append(done)
        else:
            stack[-1].append(tok)
    if len(stack) != 1:
        raise ValueError("unbalanced parentheses")
    return stack[0][0] if len(stack[0]) == 1 else stack[0]


def _atom(x) -> str:
    if isinstance(x, QStr) or (isinstance(x, str) and (x == "" or any(c in x for c in ' ()"\n\t'))):
        return '"' + str(x).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'
    return str(x)


def dumps(expr, indent: int = 0) -> str:
    if not isinstance(expr, list):
        return _atom(expr)
    if all(not isinstance(e, list) for e in expr):
        return "(" + " ".join(_atom(e) for e in expr) + ")"
    pad = "  " * (indent + 1)
    parts = []
    for e in expr:
        parts.append(dumps(e, indent + 1) if isinstance(e, list) else _atom(e))
    head = []
    while parts and not isinstance(expr[len(head)], list):
        head.append(parts.pop(0))
    return "(" + " ".join(head) + "".join("\n" + pad + p for p in parts) + ")"


def find(expr, key):
    """First child list whose head is `key`."""
    for e in expr:
        if isinstance(e, list) and e and e[0] == key:
            return e
    return None


def find_all(expr, key):
    return [e for e in expr if isinstance(e, list) and e and e[0] == key]


def value(expr, key, default=None):
    f = find(expr, key)
    return str(f[1]) if f is not None and len(f) > 1 else default
