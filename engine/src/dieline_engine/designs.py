"""The engine's public operations. The RPC server, the agent tools and the
plain form all go through these, so every path draws the same geometry."""

from __future__ import annotations

import math
import re
import uuid
from fractions import Fraction
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator  # noqa: F401

from .boards import BOARDS, DEFAULT_BOARD, get_board
from .cf2 import read_cf2, write_cf2
from .checks import overall, run_checks
from .dxf import read_dxf, write_dxf  # noqa: F401  (exports writes through here, so tests can swap it)
from .geometry import Dieline, LineType
from .styles import STYLES, Dims, get_style
from .topology import reconstruct, validate

MM_PER_INCH = 25.4
MAX_INCHES = 240.0  # longer than any R&L machine takes; catches unit mix-ups

_MIXED = re.compile(r"^\s*(?:(\d+(?:\.\d+)?)\s*[-+ ]\s*)?(\d+)\s*/\s*(\d+)\s*$")
# ArtiosCAD's sixteenths notation (shared default SixteenthChar ":"): 10:03 = 10 3/16.
_SIXTEENTHS = re.compile(r"^\s*(\d+)\s*:\s*(\d{1,2})\s*$")


def parse_length(value: Any) -> float:
    """A number, or a string like '14', '14.125', '14 1/8', '14-1/8', '14+1/8'
    (ArtiosCAD's display), '7/16' or '14:02' (ArtiosCAD sixteenths: 14 2/16)."""
    if isinstance(value, bool):
        raise ValueError("expected a number")
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip().rstrip('"').strip()
        try:
            return float(text)
        except ValueError:
            pass
        m = _SIXTEENTHS.match(text)
        if m:
            whole, sixteenths = int(m.group(1)), int(m.group(2))
            if sixteenths >= 16:
                raise ValueError(f"{value!r}: sixteenths must be 0-15")
            return whole + sixteenths / 16
        m = _MIXED.match(text)
        if m:
            whole, num, den = m.groups()
            if int(den) == 0:
                raise ValueError("zero denominator")
            return float(Fraction(whole or "0") + Fraction(int(num), int(den)))
    raise ValueError(f"not a length: {value!r} (use a number or a fraction like 14 1/8)")


class BoxParams(BaseModel):
    """Everything an agent (or the form) may choose. No coordinates."""

    model_config = ConfigDict(extra="forbid")

    style: str = Field(description="Style id, e.g. 'reg-glio-nx'")
    length: float = Field(description="Inside length")
    width: float = Field(description="Inside width")
    depth: float = Field(description="Inside depth")
    units: Literal["in", "mm"] = "in"
    board: str = Field(default=DEFAULT_BOARD, description="ArtiosCAD board code, e.g. '32 C KR'")
    options: dict[str, Any] = Field(default_factory=dict, description="Style options (see list_options)")

    @field_validator("length", "width", "depth", mode="before")
    @classmethod
    def _length(cls, v: Any) -> float:
        x = parse_length(v)
        if not math.isfinite(x) or x <= 0:
            raise ValueError("must be finite and greater than 0")
        return x

    def dims_in(self) -> Dims:
        f = 1 / MM_PER_INCH if self.units == "mm" else 1.0
        dims = Dims(self.length * f, self.width * f, self.depth * f)
        if max(dims.length, dims.width, dims.depth) > MAX_INCHES:
            raise ValueError(f"a dimension is over {MAX_INCHES:g} in; check the units")
        return dims


def build(params: "BoxParams | PartParams") -> Dieline:
    if _is_partition(params):
        from . import partitions

        return partitions.build(params)  # type: ignore[arg-type]
    style = get_style(params.style)
    board = get_board(params.board)
    return style.make(params.dims_in(), board, params.options)


# A part's parameters: a standard box, or one piece of a partition set (partitions.py).
PartParams = Any


def _is_partition(params: Any) -> bool:
    return getattr(params, "style", None) == "partition-slotted"


def parse_params(raw: Any) -> Any:
    """A part's parameters from JSON (a job record, the UI): a standard box or a partition piece."""
    if isinstance(raw, BaseModel):
        return raw
    if isinstance(raw, dict) and raw.get("style") == "partition-slotted":
        from .partitions import PartitionParams

        return PartitionParams.model_validate(raw)
    return BoxParams.model_validate(raw)


def record_params(params: Any, dieline: Dieline) -> dict:
    """Parameters as a job record keeps them: options resolved to their values,
    so a later default change can't alter a reopened job."""
    if _is_partition(params):
        return params.model_dump()
    return {**params.model_dump(), "options": dieline.meta.get("options", params.options)}


class GeometryRefused(ValueError):
    """The parameters produce geometry that can't be made (an undeclared
    crossing, overlap or open cut). Nothing becomes a design."""


def _validated(params: Any) -> tuple[Dieline, list]:
    dieline = build(params)
    report = validate(dieline)
    if not report.ok:
        issues = "; ".join(i.message for i in report.invalid[:3])
        raise GeometryRefused(f"these parameters give geometry that can't be made: {issues}")
    results = run_checks(dieline, report)
    if _is_partition(params):
        from . import partitions

        results += partitions.checks_for(params)
    return dieline, results


def create_box(params: Any) -> dict:
    dieline, results = _validated(params)
    return {
        "id": uuid.uuid4().hex[:12],
        "name": dieline.name,
        "params": params.model_dump(),
        "cf2": write_cf2(dieline),
        "checks": [r.to_dict() for r in results],
        "overall": overall(results),
        "summary": dieline.meta,
    }


def face_map(params: Any, full: bool = False) -> dict:
    """The named faces of a design: the compact view a model uses to name positions,
    or (full) every frame, reference line, edge span, keep-out and hinge."""
    d = build(params)
    if d.faces is None:
        raise ValueError("this design has no face map")
    return d.faces.to_dict() if full else d.faces.agent_view()  # type: ignore[attr-defined]


def agent_view(design: dict) -> dict:
    """What the model sees about a design: facts and checks, never geometry."""
    return {
        "name": design["name"],
        "params": design["params"],
        "summary": design["summary"],
        "overall": design["overall"],
        "checks": design["checks"],
    }


_SAFE = re.compile(r"[^A-Za-z0-9._ -]+")

# Device names Windows reserves regardless of extension.
_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(10)), *(f"LPT{i}" for i in range(10))}

EXTENSIONS = {"cf2": ".cf2", "dxf": ".dxf"}
JOB_SUFFIX = ".dieline.json"


def safe_stem(name: str) -> str:
    stem = _SAFE.sub("_", name).strip(" ._") or "design"
    stem = stem[:120].rstrip(" ._") or "design"  # cut, then trimmed again: Windows drops trailing dots and spaces
    lower = stem.lower()
    for ext in (JOB_SUFFIX, *EXTENSIONS.values()):
        if lower.endswith(ext):
            stem = stem[: -len(ext)].rstrip(" ._") or "design"
            break
    if stem.split(".")[0].rstrip(" ").upper() in _RESERVED:
        stem = f"_{stem}"
    return stem


def safe_filename(name: str, fmt: str = "cf2") -> str:
    return safe_stem(name) + EXTENSIONS[fmt]


class DesignCheckFailed(ValueError):
    """Raised when asked to export a design whose checks fail."""


class TargetBlocked(DesignCheckFailed):
    """An output file, read back from its own text, doesn't carry the design
    or breaks the topology rules after rounding. Only that target is blocked."""


# Rule types each target can carry today. Only cut and crease are verified on
# ArtiosCAD import and the sample table (docs/DESIGN-SYSTEM.md, "Rule types
# and export capability"); everything else is blocked until it is.
VERIFIED_RULES = {"cf2": {LineType.CUT, LineType.CREASE}, "dxf": {LineType.CUT, LineType.CREASE}}


def check_rules(dieline: Dieline, formats: list[str]) -> None:
    used = {s.type for s in dieline.segments if s.type != LineType.ANNOTATION}
    for fmt in formats:
        blocked = sorted(t.value for t in used - VERIFIED_RULES[fmt])
        if blocked:
            raise TargetBlocked(f"{fmt.upper()} not exported: {', '.join(blocked)} isn't verified for "
                                f"{'ArtiosCAD import' if fmt == 'cf2' else 'the sample table'} yet")


def check_outputs(dieline: Dieline, texts: dict[str, str]) -> None:
    """Validate each output as reconstructed from its own rounded geometry
    (docs/DESIGN-SYSTEM.md, "Contact and disposition rules")."""
    design_islands = len(validate(dieline).islands)
    for fmt, text in texts.items():
        try:
            # write_dxf always writes inches; read it back as such, with no unit guessing.
            back = read_cf2(text).dieline if fmt == "cf2" else read_dxf(text, "in", strict=True)[0]
        except (ValueError, OverflowError) as exc:
            raise TargetBlocked(f"{fmt.upper()} not exported: the file doesn't read back ({exc})") from exc
        try:
            recon, reason = reconstruct(dieline, back)
        except (ValueError, OverflowError) as exc:
            raise TargetBlocked(f"{fmt.upper()} not exported: the file doesn't read back ({exc})") from exc
        if recon is None:
            raise TargetBlocked(f"{fmt.upper()} not exported: {reason}")
        report = validate(recon)
        if not report.ok:
            raise TargetBlocked(f"{fmt.upper()} not exported: after rounding, {report.invalid[0].message}")
        if len(report.islands) > design_islands:
            raise TargetBlocked(f"{fmt.upper()} not exported: after rounding, {report.islands[0].message}")


Format = Literal["cf2", "dxf"]


# Exports live in `exports` (manifests, multi-part sets) and `publish` (whole-or-nothing file moves).
from .publish import ExportExists  # noqa: E402, F401  (the RPC layer offers "Replace" for this one)


def export(params: BoxParams, output_dir: Path, filename: str | None = None, formats: list | None = None,
           overwrite: bool = False, dxf_dir: Path | None = None, **kw: Any) -> dict:
    from . import exports

    return exports.export(params, output_dir, filename, formats, overwrite, dxf_dir, **kw)


def export_cf2(params: BoxParams, output_dir: Path, filename: str | None = None) -> dict:
    return export(params, output_dir, filename, ["cf2"])


def open_job(record: Any, mode: str = "regenerate") -> dict:
    from . import exports

    return exports.open_job(record, mode)


def catalog() -> dict:
    return {
        "styles": [s.to_dict() for s in STYLES.values()],
        "boards": [
            {
                "id": b.id,
                "name": b.name,
                "flute": b.flute,
                "cal_in": b.cal,
                "il_in": b.il,
                "og_in": b.og,
                "caliper_mm": b.caliper_mm,
                "verified": b.verified,
                "source": b.source,
                "evidence": b.evidence,
                "corrugated": b.corrugated,
            }
            for b in BOARDS.values()
        ],
        "default_board": DEFAULT_BOARD,
    }
