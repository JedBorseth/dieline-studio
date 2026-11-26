"""The engine's public operations. The RPC server, the agent tools and the
plain form all go through these, so every path draws the same geometry."""

from __future__ import annotations

import json
import math
import os
import re
import threading
import uuid
from contextlib import ExitStack
from datetime import datetime, timezone
from fractions import Fraction
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Callable, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from . import __version__
from .boards import BOARDS, DEFAULT_BOARD, get_board
from .cf2 import read_cf2, write_cf2
from .checks import fmt_in, overall, run_checks
from .dxf import read_dxf, write_dxf
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


def build(params: BoxParams) -> Dieline:
    style = get_style(params.style)
    board = get_board(params.board)
    return style.make(params.dims_in(), board, params.options)


class GeometryRefused(ValueError):
    """The parameters produce geometry that can't be made (an undeclared
    crossing, overlap or open cut). Nothing becomes a design."""


def _validated(params: BoxParams) -> tuple[Dieline, list]:
    dieline = build(params)
    report = validate(dieline)
    if not report.ok:
        issues = "; ".join(i.message for i in report.invalid[:3])
        raise GeometryRefused(f"these parameters give geometry that can't be made: {issues}")
    return dieline, run_checks(dieline, report)


def create_box(params: BoxParams) -> dict:
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


def face_map(params: BoxParams, full: bool = False) -> dict:
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
_EXPORT_LOCK = threading.Lock()


def safe_stem(name: str) -> str:
    stem = _SAFE.sub("_", name).strip(" ._") or "design"
    stem = stem[:120]
    lower = stem.lower()
    for ext in (JOB_SUFFIX, *EXTENSIONS.values()):
        if lower.endswith(ext):
            stem = stem[: -len(ext)].rstrip(" .") or "design"
            break
    if stem.split(".")[0].upper() in _RESERVED:
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


class ExportExists(ValueError):
    """Raised when an export would replace files from an earlier export."""

    def __init__(self, names: list[str]) -> None:
        super().__init__(
            f"already exported under this name: {', '.join(names)}. Choose another name, or replace them "
            "(the old files move to a 'superseded' folder)."
        )
        self.names = names


def _check_destination(target: Path, root: Path) -> None:
    if target.is_symlink() or target.resolve().parent != root:
        raise ValueError(f"export destination must be a file in the export folder: {target.name}")
    if target.exists() and not target.is_file():
        raise ValueError(f"export destination is not a file: {target.name}")


def _publish_export(
    payloads: dict[Path, bytes], existing: list[Path], stem: str, check_cancel: Callable[[], None],
) -> list[str]:
    """Stage on each destination filesystem and roll back failed renames."""
    with ExitStack() as cleanup:
        stages: dict[Path, Path] = {}
        for root in dict.fromkeys(target.parent for target in payloads):
            stages[root] = Path(cleanup.enter_context(TemporaryDirectory(prefix=".dieline-", dir=root)))
        for target, data in payloads.items():
            (stages[target.parent] / target.name).write_bytes(data)

        archives: dict[Path, Path] = {}
        archived: dict[Path, Path] = {}
        published: list[Path] = []
        revision = f"{stem} {datetime.now():%Y-%m-%d %H%M%S} {uuid.uuid4().hex[:8]}"
        try:
            for root in dict.fromkeys(target.parent for target in existing):
                archive_root = root / "superseded"
                _check_destination_folder(archive_root, root)
                archive = archive_root / revision
                archive.mkdir()
                archives[root] = archive
            check_cancel()
            for target in existing:
                backup = archives[target.parent] / target.name
                os.replace(target, backup)
                archived[target] = backup
            for target in payloads:
                os.replace(stages[target.parent] / target.name, target)
                published.append(target)
        except Exception as exc:
            rollback_errors = []
            for target in reversed(published):
                if target in archived:
                    continue  # replace it directly with the previous file below
                try:
                    target.unlink(missing_ok=True)
                except OSError as err:
                    rollback_errors.append(str(err))
            for target, backup in reversed(tuple(archived.items())):
                try:
                    os.replace(backup, target)
                except OSError as err:
                    rollback_errors.append(f"{backup}: {err}")
            for archive in archives.values():
                try:
                    archive.rmdir()
                except OSError:
                    pass  # leave any backups in place if restoration failed
            if rollback_errors:
                raise OSError(f"Export failed ({exc}). Rollback incomplete: {' | '.join(rollback_errors)}") from exc
            raise
        return [str(backup) for backup in archived.values()]


def _check_destination_folder(folder: Path, root: Path) -> None:
    if folder.is_symlink() or folder.resolve() != root / folder.name:
        raise ValueError(f"archive folder must be inside the export folder: {folder}")
    folder.mkdir(exist_ok=True)


def export(
    params: BoxParams,
    output_dir: Path,
    filename: str | None = None,
    formats: list[Format] | None = None,
    overwrite: bool = False,
    dxf_dir: Path | None = None,
    *,
    check_cancel: Callable[[], None] | None = None,
) -> dict:
    """Regenerate deterministically from params and write CF2 and/or DXF files,
    plus a `<stem>.dieline.json` job record that reopens the design.

    `filename` is a stem (an RL number like 'RL26-0771' is typical); the
    extension follows the format. DXF goes to `dxf_dir` when given (e.g. the
    designer's sample-table folder), everything else to `output_dir`.

    A name is one file set: if any CF2, DXF or job file of that stem exists,
    nothing is published unless `overwrite`. Stage all new files before moving
    the old set into per-folder archives, and publish the job record last.
    """
    requested = formats or ["cf2"]
    for f in requested:
        if not isinstance(f, str) or f not in EXTENSIONS:
            raise ValueError(f"unknown export format {f!r}; use cf2 or dxf")
    formats = list(dict.fromkeys(requested))
    dieline, results = _validated(params)
    if overall(results) == "fail":
        failed = "; ".join(r.message for r in results if r.status in ("fail", "stale"))
        raise DesignCheckFailed(f"not exported: the design fails its checks ({failed})")
    stem = safe_stem(filename or dieline.name)
    main, dxf_root = output_dir.resolve(), (dxf_dir or output_dir).resolve()
    targets = {f: (dxf_root if f == "dxf" else main) / (stem + EXTENSIONS[f]) for f in formats}
    job = main / (stem + JOB_SUFFIX)
    # Every file that belongs to this name's set, wherever this export would put it.
    family = {main / (stem + ".cf2"), main / (stem + ".dxf"), job, dxf_root / (stem + ".dxf")}
    paths = [str(t) for t in targets.values()]
    record = job_record(params, dieline, results, paths)
    record["stem"] = stem
    check_rules(dieline, formats)
    texts = {f: (write_cf2(dieline) if f == "cf2" else write_dxf(dieline)) for f in formats}
    check_outputs(dieline, texts)
    payloads = {target: texts[f].encode("ascii") for f, target in targets.items()}
    payloads[job] = (json.dumps(record, indent=2) + "\n").encode("utf-8")

    def ensure_active() -> None:
        if check_cancel is not None:
            check_cancel()

    # The UI's RPC pool and the agent workers share one sidecar process.
    with _EXPORT_LOCK:
        ensure_active()
        # A set exported earlier may have put its DXF in a sample folder that has
        # since changed in Settings: the previous job record says where. Only a
        # file with this set's own name counts, so a job can't pull in others.
        family |= _recorded_files(job, stem)
        for target in family:
            _check_destination(target, target.parent)
        existing = sorted((t for t in family if t.exists()), key=lambda t: (t != job, str(t)))
        if existing and not overwrite:
            raise ExportExists([t.name for t in existing])
        for root in dict.fromkeys(t.parent for t in payloads):
            root.mkdir(parents=True, exist_ok=True)
        superseded = _publish_export(payloads, existing, stem, ensure_active)
    return {"path": paths[0], "paths": paths, "job_path": str(job), "overall": overall(results),
            "name": dieline.name, "superseded": superseded}


def _recorded_files(job: Path, stem: str) -> set[Path]:
    try:
        record = json.loads(job.read_text(encoding="utf-8"))
        files = record.get("files") if isinstance(record, dict) else None
    except (OSError, ValueError):
        return set()
    wanted = {stem + ".cf2", stem + ".dxf"}
    out = set()
    for f in files if isinstance(files, list) else []:
        if isinstance(f, str):
            p = Path(f)
            if p.name in wanted and p.is_absolute():
                out.add(p)
    return out


JOB_VERSION = 1


def _geometry_digest(cf2: str) -> str:
    # Include units, rule types, pointage and all manufactured segment records.
    # Omit the display name and compare the serialized precision used in exports.
    records = (row for row in cf2.splitlines()
               if row in {"UI", "UM"} or row.startswith(("L,", "A,")))
    return sha256("\n".join(records).encode("ascii")).hexdigest()


def job_record(params: BoxParams, dieline: Dieline, results: list, files: list[str]) -> dict:
    """Everything needed to rebuild and audit an exported design. Reopen it by
    sending `params` back to box.create; `board` is the value snapshot at export."""
    b = get_board(params.board)
    return {
        "format": "dieline-studio-job",
        "version": JOB_VERSION,
        "engine": __version__,
        "exported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "name": dieline.name,
        # Options resolved to their values, so a later default change can't alter a reopened job.
        "params": {**params.model_dump(), "options": dieline.meta.get("options", params.options)},
        "board": {"code": b.id, "cal_in": b.cal, "il_in": b.il, "og_in": b.og, "crrv_in": b.crrv,
                  "source": b.source},
        "summary": dieline.meta,
        "geometry_sha256": _geometry_digest(write_cf2(dieline)),
        "checks": [r.to_dict() for r in results],
        "files": files,
    }


def _fmt_value(v: Any) -> str:
    """Sizes as the UI shows them: inches in fractions, lists and blank sizes spelled out."""
    if isinstance(v, bool) or v is None:
        return "none"
    if isinstance(v, (int, float)):
        return fmt_in(v)
    if isinstance(v, list):
        return " / ".join(_fmt_value(x) for x in v)
    if isinstance(v, dict) and {"width", "height"} <= v.keys():
        return f"{_fmt_value(v['width'])} x {_fmt_value(v['height'])}"
    return str(v)


_COMPARED = ("panels_in", "glue_lug_in", "lug_extension_in", "slot_in", "body_height_in", "top_depth_in",
             "bottom_depth_in", "top_flaps_in", "bottom_flaps_in", "blank_in")


def open_job(record: Any) -> dict:
    """Regenerate a design from a job record and say what differs from the export.

    Compare the regenerated design's saved board values, dimensional summary,
    and geometry digest when available.
    """
    if not isinstance(record, dict) or record.get("format") != "dieline-studio-job":
        raise ValueError("not a Dieline Studio job file")
    version = record.get("version")
    if type(version) is int and version > JOB_VERSION:
        raise ValueError(f"job file version {version!r} is newer than this app understands; update Dieline Studio")
    if type(version) is not int or version != JOB_VERSION:
        raise ValueError(f"invalid job file version {version!r}; expected {JOB_VERSION}")
    for field in ("board", "summary"):
        if field in record and not isinstance(record[field], dict):
            raise ValueError(f"job file {field} must be an object")
    for field in ("stem", "exported_at", "engine"):
        if field in record and record[field] is not None and not isinstance(record[field], str):
            raise ValueError(f"job file {field} must be a string")
    digest = record.get("geometry_sha256")
    if "geometry_sha256" in record and (not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)):
        raise ValueError("job file geometry_sha256 must be a SHA-256 digest")
    params = BoxParams.model_validate(record.get("params"))
    design = create_box(params)
    changes: list[str] = []
    snap = record.get("board", {})
    b = get_board(params.board)
    for field, now in (("cal_in", b.cal), ("il_in", b.il), ("og_in", b.og), ("crrv_in", b.crrv)):
        then = snap.get(field)
        if field not in snap:
            continue
        try:
            finite = type(then) in (int, float) and math.isfinite(then * 32)
        except OverflowError:
            finite = False
        if not finite:
            raise ValueError(f"job file board.{field} must be a finite measurement in inches")
        if abs(then - now) > 1e-9:
            changes.append(f"{b.id} {field[:-3].upper()} was {fmt_in(then)} in, now {fmt_in(now)} in")
    old = record.get("summary", {})
    for key in _COMPARED:
        if key in old and old[key] != design["summary"].get(key):
            label = key.removesuffix("_in").replace("_", " ")
            changes.append(f"{label} was {_fmt_value(old[key])}, now {_fmt_value(design['summary'].get(key))}")
    if digest is not None and _geometry_digest(design["cf2"]) != digest:
        changes.append("The regenerated cut or rule data differs from the exported geometry.")
    return {"design": design, "changes": changes, "stem": record.get("stem") or None,
            "exported_at": record.get("exported_at"), "engine": record.get("engine")}


def export_cf2(params: BoxParams, output_dir: Path, filename: str | None = None) -> dict:
    return export(params, output_dir, filename, ["cf2"])


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
