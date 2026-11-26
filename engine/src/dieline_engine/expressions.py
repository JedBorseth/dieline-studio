"""Typed length expressions (docs/DESIGN-SYSTEM.md, "Expressions").

Partition values (and later builder dimensions and placement distances) may be
written as expressions such as `D - 1/4`, `2 * CAL` or `round(W / 3, 1/16in)`.

- Each value is a **length** (in the part's units) or a **scalar**. A bare
  number takes its type from where it's used: a length when added to or
  subtracted from a length, a scalar when multiplying or dividing. A literal
  may carry a unit (`2in`, `50mm`, `2"`).
- A length times a length, or a scalar where a length is wanted, is refused.
- Numbers: decimals, mixed fractions (`1 1/2`, read as one number) and, on
  inch parts, ArtiosCAD sixteenths (`2:12`).
- Operators `+ - * /`, unary minus, parentheses; functions `min`, `max`,
  `abs` and `round(x, quantum)` with a length quantum.
- Names: `CAL`, `IL`, `OG`, `CRRV` (converted to the part's units by the
  caller) and the part's dimensions `L`, `W`, `D`; a part's own parameters may
  not reuse them. `evaluate_all` orders parameters by their dependencies and
  names a cycle.
- Bounded in length, work and magnitude; never run through `eval`.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any

RESERVED = frozenset({"CAL", "IL", "OG", "CRRV", "L", "W", "D"})
FUNCTIONS = frozenset({"min", "max", "abs", "round"})
MAX_CHARS = 200
MAX_TOKENS = 120
MAX_DEPTH = 24
MAX_MAGNITUDE = 1e6
MM_PER_INCH = 25.4

LENGTH, SCALAR, NUMBER = "length", "scalar", "number"  # NUMBER: a bare literal, typed by its use


class ExpressionError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class Value:
    v: float
    kind: str  # length | scalar | number


_NUM = r"\d+(?:\.\d+)?|\.\d+"
_TOKEN = re.compile(
    rf"""\s*(?:
        (?P<mixed>(?:\d+\s+)?\d+\s*/\s*\d+(?![\d.]))(?P<munit>\s*(?:in|mm|")(?![A-Za-z_]))  # 1/16in, 1 1/2in
      | (?P<mixed2>\d+\s+\d+\s*/\s*\d+(?![\d.]))           # 1 1/2
      | (?P<six>\d+:\d{{1,2}}(?!\d))                        # 2:12 (ArtiosCAD sixteenths)
      | (?P<num>(?:{_NUM}))(?P<unit>\s*(?:in|mm|")(?![A-Za-z_]))?
      | (?P<name>[A-Za-z_][A-Za-z_0-9]*)
      | (?P<op>[-+*/(),])
    )""",
    re.VERBOSE,
)


def _tokens(text: str, units: str) -> list[tuple[str, Any]]:
    if len(text) > MAX_CHARS:
        raise ExpressionError(f"expression is longer than {MAX_CHARS} characters")
    out: list[tuple[str, Any]] = []
    pos = 0
    while pos < len(text):
        if text[pos:].strip() == "":
            break
        m = _TOKEN.match(text, pos)
        if m is None or m.end() == pos:
            raise ExpressionError(f"can't read {text[pos:].strip()[:12]!r} in {text!r}")
        pos = m.end()
        if m.group("mixed") or m.group("mixed2"):
            text_ = (m.group("mixed") or m.group("mixed2")).replace("/", " / ").split()
            whole = int(text_[0]) if len(text_) == 4 else 0
            num, den = int(text_[-3]), int(text_[-1])
            if den == 0:
                raise ExpressionError("a fraction with a zero denominator")
            v = whole + num / den
            unit = (m.group("munit") or "").strip() if m.group("mixed") else ""
            if unit:
                inches = v if unit in ("in", '"') else v / MM_PER_INCH
                out.append(("num", Value(inches if units == "in" else inches * MM_PER_INCH, LENGTH)))
            else:
                out.append(("num", Value(v, NUMBER)))
        elif m.group("six"):
            if units != "in":
                raise ExpressionError("ArtiosCAD sixteenths (2:12) are only for inch parts")
            whole, six = (int(x) for x in m.group("six").split(":"))
            if six >= 16:
                raise ExpressionError(f"{m.group('six')}: sixteenths must be 0-15")
            out.append(("num", Value(whole + six / 16, NUMBER)))
        elif m.group("num"):
            v = float(m.group("num"))
            unit = (m.group("unit") or "").strip()
            if unit:
                inches = v if unit in ("in", '"') else v / MM_PER_INCH
                out.append(("num", Value(inches if units == "in" else inches * MM_PER_INCH, LENGTH)))
            else:
                out.append(("num", Value(v, NUMBER)))
        elif m.group("name"):
            out.append(("name", m.group("name")))
        else:
            out.append(("op", m.group("op")))
        if len(out) > MAX_TOKENS:
            raise ExpressionError(f"expression has more than {MAX_TOKENS} parts")
    if not out:
        raise ExpressionError("empty expression")
    return out


def _check(v: float) -> float:
    if not math.isfinite(v) or abs(v) > MAX_MAGNITUDE:
        raise ExpressionError("the result isn't a finite number of a sensible size")
    return v


def _add(a: Value, b: Value, sign: float) -> Value:
    kinds = {a.kind, b.kind}
    if kinds == {LENGTH, SCALAR}:
        raise ExpressionError("can't add or subtract a length and a scalar")
    kind = LENGTH if LENGTH in kinds else SCALAR if SCALAR in kinds else NUMBER
    return Value(_check(a.v + sign * b.v), kind)


def _mul(a: Value, b: Value) -> Value:
    if a.kind == LENGTH and b.kind == LENGTH:
        raise ExpressionError("a length times a length isn't a length")
    kind = LENGTH if LENGTH in (a.kind, b.kind) else SCALAR if SCALAR in (a.kind, b.kind) else NUMBER
    return Value(_check(a.v * b.v), kind)


def _div(a: Value, b: Value) -> Value:
    if b.v == 0:
        raise ExpressionError("division by zero")
    if b.kind == LENGTH and a.kind != LENGTH:
        raise ExpressionError("a number divided by a length isn't a length")
    if a.kind == LENGTH and b.kind == LENGTH:
        kind = SCALAR
    elif a.kind == LENGTH:
        kind = LENGTH
    else:
        kind = SCALAR if SCALAR in (a.kind, b.kind) else NUMBER
    return Value(_check(a.v / b.v), kind)


class _Parser:
    def __init__(self, toks: list[tuple[str, Any]], names: dict[str, Value]) -> None:
        self.toks, self.names, self.i, self.depth, self.used = toks, names, 0, 0, set()

    def peek(self) -> tuple[str, Any] | None:
        return self.toks[self.i] if self.i < len(self.toks) else None

    def take(self, op: str | None = None) -> tuple[str, Any]:
        t = self.peek()
        if t is None or (op is not None and t != ("op", op)):
            raise ExpressionError(f"expected {op!r}" if op else "the expression ends too early")
        self.i += 1
        return t

    def expr(self) -> Value:
        self.depth += 1
        if self.depth > MAX_DEPTH:
            raise ExpressionError("expression is nested too deeply")
        v = self.term()
        while self.peek() in (("op", "+"), ("op", "-")):
            sign = 1.0 if self.take()[1] == "+" else -1.0
            v = _add(v, self.term(), sign)
        self.depth -= 1
        return v

    def term(self) -> Value:
        v = self.unary()
        while self.peek() in (("op", "*"), ("op", "/")):
            op = self.take()[1]
            rhs = self.unary()
            v = _mul(v, rhs) if op == "*" else _div(v, rhs)
        return v

    def unary(self) -> Value:
        if self.peek() == ("op", "-"):
            self.take()
            v = self.unary()
            return Value(-v.v, v.kind)
        if self.peek() == ("op", "+"):
            self.take()
            return self.unary()
        return self.atom()

    def atom(self) -> Value:
        kind, val = self.take()
        if kind == "num":
            return val
        if kind == "op" and val == "(":
            v = self.expr()
            self.take(")")
            return v
        if kind == "name":
            if self.peek() == ("op", "("):
                return self.call(val)
            if val not in self.names:
                known = ", ".join(sorted(self.names)) or "none"
                raise ExpressionError(f"unknown name {val!r} (known: {known})")
            self.used.add(val)
            return self.names[val]
        raise ExpressionError(f"unexpected {val!r}")

    def call(self, fn: str) -> Value:
        if fn not in FUNCTIONS:
            raise ExpressionError(f"unknown function {fn!r}; use min, max, abs or round")
        self.take("(")
        args = [self.expr()]
        while self.peek() == ("op", ","):
            self.take()
            args.append(self.expr())
        self.take(")")
        if fn == "abs":
            if len(args) != 1:
                raise ExpressionError("abs takes one value")
            return Value(abs(args[0].v), args[0].kind)
        if fn in ("min", "max"):
            if len(args) < 2:
                raise ExpressionError(f"{fn} takes two or more values")
            out = args[0]
            for a in args[1:]:
                _add(out, a, 1.0)  # same compatibility as adding
                kind = LENGTH if LENGTH in (out.kind, a.kind) else SCALAR if SCALAR in (out.kind, a.kind) else NUMBER
                out = Value((min if fn == "min" else max)(out.v, a.v), kind)
            return out
        if len(args) != 2:
            raise ExpressionError("round takes a value and a quantum: round(x, 1/16in)")
        x, q = args
        if q.kind == SCALAR or (x.kind == LENGTH and q.kind not in (LENGTH, NUMBER)):
            raise ExpressionError("round's quantum is a length, e.g. 1/16in or 1mm")
        if q.v <= 0:
            raise ExpressionError("round's quantum must be greater than 0")
        kind = LENGTH if LENGTH in (x.kind, q.kind) else x.kind
        return Value(_check(round(x.v / q.v) * q.v), kind)


def evaluate(text: Any, names: dict[str, float] | None = None, units: str = "in", kind: str = LENGTH,
             lengths: frozenset[str] | set[str] | None = None) -> float:
    """The value of one expression, or a plain number.

    names: values of the names it may use, in the part's units; `lengths` says
    which of them are lengths (by default every name is: CAL, L, ...).
    kind: what the field wants, `length` or `scalar`."""
    return evaluate_typed(text, names, units, kind, lengths)[0]


def evaluate_typed(text: Any, names: dict[str, float] | None = None, units: str = "in", kind: str = LENGTH,
                   lengths: frozenset[str] | set[str] | None = None) -> tuple[float, set[str]]:
    if isinstance(text, bool):
        raise ExpressionError("expected a number or an expression")
    if isinstance(text, (int, float)):
        return _check(float(text)), set()
    if not isinstance(text, str):
        raise ExpressionError("expected a number or an expression")
    names = names or {}
    lengths = set(names) if lengths is None else set(lengths)
    typed = {k: Value(float(v), LENGTH if k in lengths else SCALAR) for k, v in names.items()}
    p = _Parser(_tokens(text, units), typed)
    v = p.expr()
    if p.peek() is not None:
        raise ExpressionError(f"unexpected {p.peek()[1]!r} in {text!r}")
    if kind == LENGTH and v.kind == SCALAR:
        raise ExpressionError(f"{text!r} is a scalar (a ratio), not a length")
    if kind == SCALAR and v.kind == LENGTH:
        raise ExpressionError(f"{text!r} is a length, not a scalar")
    return v.v, p.used


def evaluate_all(params: dict[str, Any], names: dict[str, float], units: str = "in") -> dict[str, float]:
    """A part's own parameters, which may refer to each other and to the reserved
    names, evaluated in dependency order. A cycle is an error naming the loop."""
    clash = sorted(set(params) & RESERVED)
    if clash:
        raise ExpressionError(f"{', '.join(clash)} is a reserved name; choose another parameter name")
    deps: dict[str, set[str]] = {}
    for key, text in params.items():
        found = set(re.findall(r"[A-Za-z_][A-Za-z_0-9]*", text)) if isinstance(text, str) else set()
        deps[key] = {n for n in found if n in params}
    done: dict[str, float] = {}
    visiting: list[str] = []

    def visit(k: str) -> None:
        if k in done:
            return
        if k in visiting:
            loop = visiting[visiting.index(k):] + [k]
            raise ExpressionError(f"parameters refer to each other in a loop: {' -> '.join(loop)}")
        visiting.append(k)
        for d in sorted(deps[k]):
            visit(d)
        visiting.pop()
        done[k] = evaluate(params[k], {**names, **done}, units)

    for k in params:
        visit(k)
    return done
