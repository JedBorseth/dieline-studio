"""The engine's public operations. The RPC server, the agent tools and the
plain form all go through these, so every path draws the same geometry."""

from __future__ import annotations

import math
import re
import uuid
from fractions import Fraction
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_serializer

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
    # Features on the part (features.py): parameters and placements, each with a stable engine-made id.
    features: list[dict[str, Any]] = Field(default_factory=list, description="Features (see set_features)")

    @field_validator("features", mode="before")
    @classmethod
    def _features(cls, v: Any) -> list[dict[str, Any]]:
        from .features import MAX_FEATURES, normalize  # noqa: PLC0415

        if v is None:
            return []
        if not isinstance(v, list):
            raise ValueError("features must be a list")
        if len(v) > MAX_FEATURES:
            raise ValueError(f"a part can have at most {MAX_FEATURES} features")
        out, seen, n = [], set(), 0
        for item in v:
            fid = item.get("id") if isinstance(item, dict) else None
            if fid is None:  # given without ids (a hand-written record): number them
                n += 1
                while f"f{n}" in seen or any(isinstance(x, dict) and x.get("id") == f"f{n}" for x in v):
                    n += 1
                fid = f"f{n}"
            if not isinstance(fid, str) or not (len(fid) > 1 and fid[0] == "f" and fid[1:].isdigit()):
                raise ValueError(f"feature id {fid!r} isn't a feature id (f1, f2, ...)")
            if fid in seen:
                raise ValueError(f"feature {fid} is listed twice")
            seen.add(fid)
            out.append(normalize(item, fid))
        return out

    @model_serializer(mode="wrap")
    def _dump(self, handler: Any) -> dict[str, Any]:
        out = handler(self)
        if not out.get("features"):
            out.pop("features", None)  # a part without features reads exactly as before
        return out

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


def build(params: BoxParams) -> Dieline:
    style = get_style(params.style)
    board = get_board(params.board)
    d = style.make(params.dims_in(), board, params.options)
    if params.features:
        from . import features  # noqa: PLC0415

        features.apply(d, params.features, params, board)
        _feature_meta(d)
    return d


def _feature_meta(d: Dieline) -> None:
    """Summary values recomputed from the finished drawing, never carried over from the standard's."""
    from .features import Built  # noqa: PLC0415

    built: list[Built] = d.extras.get("features") or []  # type: ignore[assignment]
    w, h = d.size()
    d.meta["blank_in"] = {"width": round(w, 6), "height": round(h, 6)}
    d.meta["blank_area_sqft"] = round(w * h / 144, 3)
    lengths = {t.value: round(sum(x.length for x in d.of_type(t)), 4)
               for t in (LineType.CUT, LineType.CREASE, LineType.PERFORATION, LineType.SCORE)}
    d.meta["rule_length_in"] = {k: v for k, v in lengths.items() if v or k in ("cut", "crease")}
    # Anything that needs a die: holes, hand flaps, notches, perforations, tear strips (an extra score doesn't).
    d.meta["die_cut_features"] = sum(1 for b in built if b.status == "ok" and b.kind != "score")
    d.meta["features"] = [{"id": b.id, "kind": b.kind, "status": b.status} for b in built]


class GeometryRefused(ValueError):
    """The parameters produce geometry that can't be made (an undeclared
    crossing, overlap or open cut). Nothing becomes a design."""


def _validated(params: BoxParams) -> tuple[Dieline, list]:
    dieline = build(params)
    report = dieline.extras.get("report") or validate(dieline)
    if not report.ok:
        issues = "; ".join(i.message for i in report.invalid[:3])
        raise GeometryRefused(f"these parameters give geometry that can't be made: {issues}")
    return dieline, run_checks(dieline, report)


def create_box(params: BoxParams) -> dict:
    dieline, results = _validated(params)
    from .rules import readiness  # noqa: PLC0415

    out = {
        "id": uuid.uuid4().hex[:12],
        "name": dieline.name,
        "params": params.model_dump(),
        "cf2": write_cf2(dieline),
        "checks": [r.to_dict() for r in results],
        "overall": overall(results),
        "summary": dieline.meta,
        # Each export target's readiness: a target-specific block (an unverified rule) isn't in overall.
        "targets": readiness(dieline),
    }
    if params.features:
        out["features"] = [b.to_dict() for b in dieline.extras.get("features", [])]  # type: ignore[union-attr]
    return out


def face_map(params: BoxParams, full: bool = False) -> dict:
    """The named faces of a design: the compact view a model uses to name positions,
    or (full) every frame, reference line, edge span, keep-out and hinge."""
    d = build(params)
    if d.faces is None:
        raise ValueError("this design has no face map")
    return d.faces.to_dict() if full else d.faces.agent_view()  # type: ignore[attr-defined]


def agent_view(design: dict) -> dict:
    """What the model sees about a design: facts and checks, never geometry."""
    out = {
        "name": design["name"],
        "params": design["params"],
        "summary": design["summary"],
        "overall": design["overall"],
        "checks": design["checks"],
    }
    if design.get("features"):
        # Compact: what the read-back needs, no coordinates.
        out["features"] = [{k: f[k] for k in ("id", "kind", "label", "status", "faces", "copies", "summary",
                                              "reason") if k in f} for f in design["features"]]
    targets = design.get("targets") or {}
    blocked = {fmt: t["blocking"] for fmt, t in targets.items() if not t["ready"]}
    if blocked:
        out["export_targets"] = {"blocked": blocked, "note": "the design is fine to show; only these formats can't "
                                                             "be exported until the rule is verified"}
    return out


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
# and export capability"); everything else is blocked until it is. The full
# table, with pointage, crease side and perforation parameters, is rules.py's.
VERIFIED_RULES = {"cf2": {LineType.CUT, LineType.CREASE}, "dxf": {LineType.CUT, LineType.CREASE}}


def check_rules(dieline: Dieline, formats: list[str], expand_perfs: bool = False) -> None:
    from .rules import blocks  # noqa: PLC0415

    for fmt in formats:
        why = blocks(dieline, fmt, expand_perfs=expand_perfs)
        if why:
            raise TargetBlocked(f"{fmt.upper()} not exported: {'; '.join(why)}")


def check_outputs(dieline: Dieline, texts: dict[str, str], expand_perfs: bool = False) -> None:
    """Validate each output as reconstructed from its own rounded geometry
    (docs/DESIGN-SYSTEM.md, "Contact and disposition rules"). An expanded
    perforation DXF is compared with the design's expanded representation, and
    that representation must pass the topology rules too."""
    design_islands = len(validate(dieline).islands)
    original = dieline
    for fmt, text in texts.items():
        dieline = original
        if fmt == "dxf" and expand_perfs:
            from .rules import expand_perforations  # noqa: PLC0415

            dieline = expand_perforations(original)
            rep = validate(dieline)
            if not rep.ok:
                raise TargetBlocked(f"DXF not exported: the perforations as dashed cuts break the rules: "
                                    f"{rep.invalid[0].message}")
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
    from .features import HANDHOLE, KINDS, PROVISIONAL  # noqa: PLC0415
    from .rules import table  # noqa: PLC0415

    return {
        "features": {"kinds": list(KINDS), "handhole_default": {**HANDHOLE, "source": "File_Box_1 (shop file)"},
                     "perforation_default": {"cut": "3/16", "tie": "3/16", "source": "File_Box_1 (shop file)"},
                     "provisional": PROVISIONAL},
        "export_capability": table(),
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
