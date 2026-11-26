"""Typed expressions for placement distances, builder dimensions and partition
values (docs/DESIGN-SYSTEM.md, "Expressions").

- Every value is a **length** (in the part's units) or a **scalar**.
  `2 * CAL` and `W / 2` are lengths, `L / W` is a scalar.
- A bare number takes its type from where it's used: a length when added to or
  subtracted from a length (`W/2 - 1/4`), a scalar when multiplying or dividing
  (`2 * CAL`). A literal can carry a unit (`2in`, `50mm`).
- `L * W`, and a scalar where a length is wanted, are refused.
- Numbers are decimals and mixed fractions (`1 1/2`), read as single numbers
  before any arithmetic is parsed; ArtiosCAD sixteenths (`2:12`) on inch parts only.
- Operators `+ - * /`, unary minus, parentheses; functions `min`, `max`, `abs`,
  `round(x, quantum)` where the quantum is a length (`1/16in`, `1mm`).
- Names: board values `CAL IL OG CRRV` (converted to the part's units), dims
  `L W D`, and the part's own params (which may not reuse a reserved name).
- Dependencies are ordered; a cycle is an error naming the loop. Results must be
  finite; division by zero is an error. Bounded in length, work and magnitude,
  and never run through `eval`.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any

MAX_CHARS = 200
MAX_TOKENS = 120
MAX_DEPTH = 24
MAX_MAGNITUDE = 1e6  # in the part's units: far past any blank
RESERVED = ("CAL", "IL", "OG", "CRRV", "L", "W", "D")
FUNCTIONS = ("min", "max", "abs", "round")
MM_PER_IN = 25.4

# A dimension type: 0 = scalar, 1 = length, 2 = area; None = a bare number not yet typed.
Dim = int | None


class ExpressionError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class Value:
    v: float
    dim: Dim


@dataclass(slots=True)
class Env:
    """What names mean for one part: values in the part's units."""

    units: str = "in"  # "in" | "mm"
    names: dict[str, Value] = field(default_factory=dict)

    @property
    def per_inch(self) -> float:
        return 1.0 if self.units == "in" else MM_PER_IN


def env_for(units: str, board: Any = None, dims: dict[str, float] | None = None,
            params: dict[str, float] | None = None) -> Env:
    """An environment for a part. Board values are inches and are converted;
    dims are already in the part's units; params are lengths in the part's units."""
    env = Env(units)
    k = env.per_inch
    if board is not None:
        for name, attr in (("CAL", "cal"), ("IL", "il"), ("OG", "og"), ("CRRV", "crrv")):
            env.names[name] = Value(float(getattr(board, attr)) * k, 1)
    for name, v in (dims or {}).items():
        env.names[name] = Value(float(v), 1)
    for name, v in (params or {}).items():
        if name in RESERVED or name.lower() in FUNCTIONS:
            raise ExpressionError(f"{name!r} is a reserved name; choose another parameter name")
        env.names[name] = Value(float(v), 1)
    return env


# ------------------------------------------------------------------ tokens

_NUM = r"\d+(?:\.\d*)?|\.\d+"
_TOKEN = re.compile(
    rf"""\s*(?:
    (?P<mixed>\d+\s+\d+/\d+(?![\d.]))               # 1 1/2
  | (?P<frac>\d+/\d+(?![\d.]))                      # 3/4: a fraction (no spaces), not a division
  | (?P<six>\d+:\d{{1,2}})                           # 2:12 (sixteenths)
  | (?P<num>{_NUM})
  | (?P<name>[A-Za-z_][A-Za-z0-9_]*)
  | (?P<op>[-+*/(),])
    )""",
    re.VERBOSE,
)


@dataclass(frozen=True, slots=True)
class _Tok:
    kind: str  # num | name | op | unit
    text: str
    value: float = 0.0
    unit: str | None = None  # "in" | "mm" on a literal that carries one


def _tokens(text: str, units: str) -> list[_Tok]:
    if not isinstance(text, str):
        raise ExpressionError("an expression must be text or a number")
    if len(text) > MAX_CHARS:
        raise ExpressionError(f"expression longer than {MAX_CHARS} characters")
    out: list[_Tok] = []
    pos = 0
    s = text.rstrip()
    while pos < len(s):
        m = _TOKEN.match(s, pos)
        if not m or m.end() == pos:
            raise ExpressionError(f"can't read {s[pos:].strip()[:20]!r} in {text!r}")
        pos = m.end()
        g = m.lastgroup
        raw = m.group(g).strip()
        if g in ("mixed", "frac") and out and out[-1].kind == "op" and out[-1].text == "/":
            # W / 3/4 reads as W divided by three quarters, which is rarely what was meant.
            raise ExpressionError(f"{text!r}: a fraction right after '/' is ambiguous; use parentheses")
        if g == "mixed":
            whole, frac = raw.split(None, 1)
            out.append(_Tok("num", raw, float(whole) + _frac(frac, text)))
        elif g == "frac":
            out.append(_Tok("num", raw, _frac(raw, text)))
        elif g == "six":
            if units != "in":
                raise ExpressionError(f"sixteenths ({raw}) are for inch parts only")
            whole, six = raw.split(":")
            if int(six) >= 16:
                raise ExpressionError(f"{raw}: sixteenths must be 0-15")
            out.append(_Tok("num", raw, int(whole) + int(six) / 16))
        elif g == "num":
            out.append(_Tok("num", raw, float(raw)))
        elif g == "name":
            # A unit right after a number literal: 2in, 50mm, 1/16in, 1 1/2 in.
            if raw in ("in", "mm") and out and out[-1].kind == "num" and out[-1].unit is None:
                if ":" in out[-1].text:  # 2:12 is already inches and sixteenths
                    raise ExpressionError(f"{out[-1].text}{raw}: sixteenths ({out[-1].text}) are inches already "
                                          "and take no unit")
                last = out.pop()
                out.append(_Tok("num", last.text + raw, last.value, raw))
            else:
                out.append(_Tok("name", raw))
        else:
            out.append(_Tok("op", raw))
        if len(out) > MAX_TOKENS:
            raise ExpressionError(f"expression has more than {MAX_TOKENS} parts")
    if not out:
        raise ExpressionError("empty expression")
    return out


def _frac(raw: str, text: str) -> float:
    num, den = (int(x) for x in raw.replace(" ", "").split("/"))
    if den == 0:
        raise ExpressionError(f"zero denominator in {text!r}")
    return num / den


# ------------------------------------------------------------------ parse + evaluate


class _Parser:
    """Recursive descent over the tokens, evaluating as it goes (no tree, no eval)."""

    def __init__(self, toks: list[_Tok], env: Env, text: str) -> None:
        self.toks, self.env, self.text, self.i, self.depth = toks, env, text, 0, 0
        self.used: set[str] = set()

    def peek(self) -> _Tok | None:
        return self.toks[self.i] if self.i < len(self.toks) else None

    def take(self, op: str | None = None) -> _Tok:
        t = self.peek()
        if t is None:
            raise ExpressionError(f"{self.text!r} ends early")
        if op is not None and (t.kind != "op" or t.text != op):
            raise ExpressionError(f"expected {op!r} in {self.text!r}, found {t.text!r}")
        self.i += 1
        return t

    def nest(self) -> None:
        self.depth += 1
        if self.depth > MAX_DEPTH:
            raise ExpressionError("expression nested too deeply")

    def expr(self) -> Value:
        self.nest()
        left = self.term()
        while (t := self.peek()) is not None and t.kind == "op" and t.text in "+-":
            self.take()
            right = self.term()
            left = _add(left, right, t.text, self.text)
        self.depth -= 1
        return left

    def term(self) -> Value:
        left = self.unary()
        while (t := self.peek()) is not None and t.kind == "op" and t.text in "*/":
            self.take()
            right = self.unary()
            left = _mul(left, right, t.text, self.text)
        return left

    def unary(self) -> Value:
        t = self.peek()
        if t is not None and t.kind == "op" and t.text in "+-":
            self.take()
            self.nest()
            v = self.unary()
            self.depth -= 1
            return Value(-v.v if t.text == "-" else v.v, v.dim)
        return self.atom()

    def atom(self) -> Value:
        t = self.take()
        if t.kind == "num":
            if t.unit is None:
                return Value(t.value, None)
            k = self.env.per_inch if t.unit == "in" else self.env.per_inch / MM_PER_IN
            return Value(t.value * k, 1)
        if t.kind == "op" and t.text == "(":
            v = self.expr()
            self.take(")")
            return v
        if t.kind == "name":
            if t.text in FUNCTIONS:
                return self.call(t.text)
            if t.text not in self.env.names:
                known = ", ".join(sorted(self.env.names)) or "none"
                raise ExpressionError(f"unknown name {t.text!r} in {self.text!r}; names: {known}")
            self.used.add(t.text)
            return self.env.names[t.text]
        raise ExpressionError(f"unexpected {t.text!r} in {self.text!r}")

    def call(self, fn: str) -> Value:
        self.take("(")
        args = [self.expr()]
        while (t := self.peek()) is not None and t.kind == "op" and t.text == ",":
            self.take()
            args.append(self.expr())
        self.take(")")
        if fn == "abs":
            if len(args) != 1:
                raise ExpressionError("abs takes one value")
            return Value(abs(args[0].v), args[0].dim)
        if fn in ("min", "max"):
            if len(args) < 2:
                raise ExpressionError(f"{fn} takes two or more values")
            dim = _same_dim(args, fn, self.text)
            pick = min if fn == "min" else max
            return Value(pick(a.v for a in args), dim)
        if len(args) != 2:
            raise ExpressionError("round takes a value and a quantum, e.g. round(W/3, 1/16in)")
        x, q = args
        if q.dim not in (1, None) or x.dim not in (1, None):
            raise ExpressionError("round's value and quantum are lengths, e.g. round(W/3, 1/16in)")
        if q.dim is None:
            raise ExpressionError("round's quantum needs a unit, e.g. 1/16in or 1mm")
        if q.v <= 0:
            raise ExpressionError("round's quantum must be above zero")
        return Value(round(x.v / q.v) * q.v, 1)


def _add(a: Value, b: Value, op: str, text: str) -> Value:
    if a.dim is not None and b.dim is not None and a.dim != b.dim:
        raise ExpressionError(f"can't {'add' if op == '+' else 'subtract'} a {_word(b.dim)} "
                              f"{'to' if op == '+' else 'from'} a {_word(a.dim)} in {text!r}")
    dim = a.dim if a.dim is not None else b.dim
    return Value(a.v + b.v if op == "+" else a.v - b.v, dim)


def _mul(a: Value, b: Value, op: str, text: str) -> Value:
    da, db = a.dim or 0, b.dim or 0
    if op == "*":
        dim: Dim = da + db
        v = a.v * b.v
    else:
        if b.v == 0:
            raise ExpressionError(f"division by zero in {text!r}")
        dim = da - db
        v = a.v / b.v
    if dim not in (0, 1):
        raise ExpressionError(f"{text!r} multiplies lengths together (an area or worse); a length or a "
                              "scalar is needed")
    if a.dim is None and b.dim is None:
        dim = None  # two bare numbers stay untyped: 3 / 4 * W is still a length
    return Value(v, dim)


def _same_dim(args: list[Value], fn: str, text: str) -> Dim:
    dims = {a.dim for a in args if a.dim is not None}
    if len(dims) > 1:
        raise ExpressionError(f"{fn} mixes lengths and scalars in {text!r}")
    return dims.pop() if dims else None


def _word(dim: Dim) -> str:
    return {0: "scalar", 1: "length"}.get(dim, "number")


def evaluate(text: Any, env: Env, kind: str = "length") -> float:
    """One expression's value. kind: length (the part's units) or scalar."""
    return evaluate_with_names(text, env, kind)[0]


def evaluate_with_names(text: Any, env: Env, kind: str = "length") -> tuple[float, set[str]]:
    if isinstance(text, bool):
        raise ExpressionError("expected a number or an expression")
    if isinstance(text, (int, float)):
        v = float(text)
        if not math.isfinite(v) or abs(v) > MAX_MAGNITUDE:
            raise ExpressionError(f"{text!r} isn't a finite size")
        return v, set()
    toks = _tokens(text, env.units)
    p = _Parser(toks, env, text)
    val = p.expr()
    if p.peek() is not None:
        raise ExpressionError(f"unexpected {p.peek().text!r} in {text!r}")  # type: ignore[union-attr]
    if not math.isfinite(val.v) or abs(val.v) > MAX_MAGNITUDE:
        raise ExpressionError(f"{text!r} doesn't give a finite size")
    if kind == "length" and val.dim == 0:
        raise ExpressionError(f"{text!r} is a scalar (a ratio), but a length is needed here")
    if kind == "scalar" and val.dim == 1:
        raise ExpressionError(f"{text!r} is a length, but a plain number is needed here")
    return val.v, p.used


def dimension(text: Any, env: Env, what: str, precision: float | None = None) -> float:
    """A dimension (a size, depth or width): must be above the export precision."""
    v = evaluate(text, env, "length")
    floor = precision if precision is not None else 1e-6 * env.per_inch
    if v <= floor:
        raise ExpressionError(f"{what} {text!r} comes to {v:.6g} {env.units}; it must be above zero")
    return v


def evaluate_all(exprs: dict[str, Any], env: Env) -> dict[str, float]:
    """Several named expressions that may use each other, in dependency order.
    A cycle is an error naming the loop."""
    names = list(exprs)
    for n in names:
        if n in RESERVED or n.lower() in FUNCTIONS:
            raise ExpressionError(f"{n!r} is a reserved name")
    deps: dict[str, set[str]] = {}
    for n, text in exprs.items():
        found: set[str] = set()
        if isinstance(text, str):
            for t in _tokens(text, env.units):
                if t.kind == "name" and t.text in exprs:
                    found.add(t.text)
        deps[n] = found
    done: dict[str, float] = {}
    state: dict[str, int] = {}

    def visit(n: str, path: list[str]) -> None:
        if state.get(n) == 2:
            return
        if state.get(n) == 1:
            loop = path[path.index(n):] + [n]
            raise ExpressionError(f"these expressions depend on each other in a loop: {' -> '.join(loop)}")
        state[n] = 1
        for d in sorted(deps[n]):
            visit(d, path + [n])
        local = Env(env.units, {**env.names, **{k: Value(v, 1) for k, v in done.items()}})
        done[n] = evaluate(exprs[n], local, "length")
        state[n] = 2

    for n in names:
        visit(n, [])
    return done
