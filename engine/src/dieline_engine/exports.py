"""Exports and job records (docs/DESIGN-SYSTEM.md, "Exports and job records").

An export writes every part of a design: CF2 and/or DXF per part, and the
manifest `<stem>.dieline.json` that lists them with their hashes and keeps the
as-exported geometry. `publish` makes each destination change whole or not at
all; this module decides what the files are, what they replace, and whether
the sample table needs the designer before its files move in.

A manifest is a file anyone can edit, so a previous one is validated in full
(`_read_prev`) before any path in it is used: its files must be this job's
names in this job's folders.
"""

from __future__ import annotations

import json
import math
import re
import threading
from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import dataclass, field
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any, Literal

from . import __version__, publish
from . import designs as _d
from .boards import get_board
from .cf2 import read_cf2, write_cf2
from .checks import fmt_in, overall
from .geometry import Dieline
from .publish import (ChangedOnDisk, ExportExists, LockHeld, OwnedByAnother, PublishError, Unresolved, canon,
                      key, valid_label, valid_sha, valid_txn)

JOB_VERSION = 2
EXTENSIONS = {"cf2": ".cf2", "dxf": ".dxf"}
JOB_SUFFIX = publish.MANIFEST
EXPORT_LOCK = threading.Lock()  # the UI's RPC pool and the agent workers share one sidecar
Format = Literal["cf2", "dxf"]
_SLUG = re.compile(r"[^A-Za-z0-9_-]+")

__all__ = ["ExportExists", "LockHeld", "OwnedByAnother", "ChangedOnDisk", "PublishError", "Unresolved",
           "ExportPart", "export", "export_design", "pickup", "plan", "recover_all", "open_job"]


@dataclass(frozen=True, slots=True)
class ExportPart:
    part_id: str
    label: str
    params: Any  # designs.BoxParams, or a partition piece's PartitionParams
    quantity: int = 1
    piece_no: str | None = None


@dataclass(slots=True)
class _Built:
    part: ExportPart
    slug: str
    dieline: Dieline
    results: list
    texts: dict[str, str]
    targets: dict[str, Path] = field(default_factory=dict)


def part_slug(p: ExportPart) -> str:
    """From the shop's piece number when known, else the stable part id; never from the label."""
    raw = p.piece_no or p.part_id
    return (_SLUG.sub("_", raw).strip("_-") or p.part_id)[:40]


def _formats(requested: list[Any] | None) -> list[str]:
    requested = requested or ["cf2"]
    for f in requested:
        if not isinstance(f, str) or f not in EXTENSIONS:
            raise ValueError(f"unknown export format {f!r}; use cf2 or dxf")
    return list(dict.fromkeys(requested))


def _build(parts: list[ExportPart], formats: list[str]) -> list[_Built]:
    if not parts:
        raise ValueError("nothing to export: the design has no parts")
    several = len(parts) > 1
    out = []
    for p in parts:
        name = f"part {p.part_id} ({p.label})" if several else "the design"
        dieline, results = _d._validated(p.params)
        if overall(results) == "fail":
            failed = "; ".join(r.message for r in results if r.status in ("fail", "stale"))
            raise _d.DesignCheckFailed(f"not exported: {name} fails its checks ({failed})")
        try:
            _d.check_rules(dieline, formats)
            texts = {f: (_d.write_cf2(dieline) if f == "cf2" else _d.write_dxf(dieline)) for f in formats}
            _d.check_outputs(dieline, texts)
        except _d.TargetBlocked as exc:
            raise _d.TargetBlocked(f"{name}: {exc}" if several else str(exc)) from exc
        out.append(_Built(p, part_slug(p), dieline, results, texts))
    return out


@dataclass(slots=True)
class _Where:
    stem: str
    main: Path
    sample: Path | None  # a separate sample-table folder, when Settings has one
    fmts: list[str]

    @property
    def job(self) -> Path:
        return self.main / f"{self.stem}{publish.MANIFEST}"

    def is_sample(self, fmt: str) -> bool:
        return fmt == "dxf" and self.sample is not None


def _where(output_dir: Path, dxf_dir: Path | None, stem: str, fmts: list[str]) -> _Where:
    main = output_dir.resolve()
    sample = dxf_dir.resolve() if dxf_dir is not None and canon(dxf_dir) != canon(main) else None
    return _Where(stem, main, sample, fmts)


def _name_files(built: list[_Built], w: _Where) -> None:
    several = len(built) > 1
    seen: dict[str, str] = {}
    for b in built:
        base = f"{w.stem}-{b.slug}" if several else w.stem
        for f in b.texts:
            folder = w.sample if w.is_sample(f) else w.main
            path = folder / (base + EXTENSIONS[f])
            k = key(path)
            if k in seen:
                raise PublishError(f"parts {seen[k]} and {b.part.part_id} would both be written as {path.name}; "
                                   "give them different piece numbers")
            seen[k] = b.part.part_id
            b.targets[f] = path


def _family(w: _Where) -> list[Path]:
    """Files of this name a one-part export would use: an older set may have left them without a manifest."""
    out = [w.main / f"{w.stem}.cf2", w.main / f"{w.stem}.dxf"]
    if w.sample is not None and not _is_drive_root(w.sample):
        out.append(w.sample / f"{w.stem}.dxf")
    return out


def _check_lengths(built: list[_Built], w: _Where) -> None:
    """Every path the export could use, including staging, journals and archives, fits Windows' limit."""
    label = "x" * (len(w.stem) + publish.LABEL_EXTRA)
    txn = "0" * 16
    paths: list[Path] = [w.job, publish.tmp_name(w.job), publish.tmp_name(w.main / f"{w.stem}{publish.JOURNAL}"),
                         w.main / publish.STAGING / txn / f".manifest{publish.MANIFEST}.000000.tmp",
                         w.main / "superseded" / label / w.job.name]
    for b in built:
        for f, p in b.targets.items():
            sample = w.is_sample(f)
            paths.append(p)
            paths.append(publish.staging_of(p.parent, sample, txn) / f".999-{p.name}.000000.tmp")
            paths.append(publish.archive_root(p.parent, sample) / label / p.name)
            if sample:
                paths.append(publish.tmp_name(publish.pending_dir(p.parent) / f"{w.stem}{publish.JOURNAL}"))
    for p in paths:
        publish.check_length(p)


# ------------------------------------------------------------------ the previous record


@dataclass(slots=True)
class _Prev:
    """What the previous manifest of this name says, once it's been checked."""

    exists: bool = False
    txn: str | None = None  # the main transaction that wrote it (version 2)
    main_files: list[tuple[Path, str | None]] = field(default_factory=list)
    at_table: list[tuple[Path, str | None]] = field(default_factory=list)  # this job's files at a sample table
    pending: dict[str, Any] | None = None  # a sample-table set still waiting for its pickup
    published_txn: str | None = None
    table_folder: Path | None = None  # the sample table its last section names, when this job used it
    unclaimed: list[Path] = field(default_factory=list)  # listed at a table that doesn't record this job

    @property
    def sample_folders(self) -> set[Path]:
        out = {p.parent.resolve() for p, _ in self.at_table}
        if self.pending is not None:
            out.add(Path(self.pending["folder"]).resolve())
        if self.table_folder is not None:
            out.add(self.table_folder.resolve())
        return out


def _damaged(job: Path, why: str) -> PublishError:
    return PublishError(f"the job record {job.name} looks edited or damaged ({why}); move it aside (and check its "
                        "files by hand) before exporting under this name")


def _dxf_at_table(path: Any, stem: str, main: Path) -> Path | None:
    if not isinstance(path, str) or not Path(path).is_absolute():
        return None
    p = Path(path)
    if not p.name.lower().endswith(".dxf") or not publish.belongs(p.name, stem) or canon(p.parent) == canon(main):
        return None
    return p


def _claim_of(p: Path) -> Path | None:
    """The manifest the sample table's ownership index says owns `p` (raising if the index is damaged)."""
    try:
        index = publish.claims(p.parent)
    except PublishError:
        return None  # a drive root: never a sample table
    if index is None:
        raise PublishError(f"the sample table's ownership index beside {p.parent} can't be read; check it by "
                           "hand before exporting there")
    c = index.get(key(p))
    return Path(c) if c else None


def _uses_table(folder: Path, job: Path) -> bool:
    """Whether the table's index records this job at all: a record's word alone never makes a folder
    one of its sample tables."""
    try:
        index = publish.claims(folder)
    except PublishError:
        return False
    if index is None:
        raise PublishError(f"the sample table's ownership index beside {folder} can't be read; check it by hand "
                           "before exporting there")
    return any(key(Path(c)) == key(job) for c in index.values())


def _read_prev(job: Path, main: Path, stem: str, sample: Path | None = None) -> _Prev:
    real = publish.find(job)
    if real is not None and real.name != job.name:
        raise PublishError(f"a job record {real.name} is already in {main}, differing from {job.name} only in case; "
                           "export under that name, or rename it first")
    record = publish.read_json(job)
    if record is None:
        return _Prev()
    if not isinstance(record, dict) or record.get("_unreadable") or record.get("format") != "dieline-studio-job":
        raise _damaged(job, "it can't be read as a job record")
    version = record.get("version")
    prev = _Prev(exists=True)

    def at_table(p: Path, sha: str | None) -> None:
        owner = _claim_of(p)
        if owner is not None and key(owner) == key(job):
            prev.at_table.append((p, sha))
        else:
            prev.unclaimed.append(p)

    if version == 1:
        files = record.get("files", [])
        if not isinstance(files, list):
            raise _damaged(job, "its file list isn't a list")
        for f in files:
            if not isinstance(f, str) or not Path(f).is_absolute() or not publish.belongs(Path(f).name, stem):
                raise _damaged(job, f"it lists {f!r}, which isn't one of this job's files")
            p = Path(f)
            if canon(p.parent) == canon(main):
                prev.main_files.append((p, None))
            elif _dxf_at_table(f, stem, main) is None:
                raise _damaged(job, f"it lists {f!r}, which isn't one of this job's files")
            elif sample is not None and canon(p.parent) == canon(sample) and not _is_drive_root(sample):
                prev.at_table.append((p, None))  # a version 1 set's DXF in today's sample-table folder
            else:
                prev.unclaimed.append(p)  # another folder: not this app's to touch without a claim
        return prev
    if version != 2:
        raise _damaged(job, f"version {version!r}")
    if not valid_txn(record.get("txn")):
        raise _damaged(job, "it has no transaction id")
    prev.txn = record["txn"]
    parts = record.get("parts")
    if not isinstance(parts, list) or not parts:
        raise _damaged(job, "it has no parts")
    sample_files: list[tuple[Path, str]] = []
    for part in parts:
        files = part.get("files") if isinstance(part, dict) else None
        if not isinstance(files, list):
            raise _damaged(job, "a part has no file list")
        for f in files:
            if not isinstance(f, dict) or f.get("format") not in EXTENSIONS or not valid_sha(f.get("sha256")):
                raise _damaged(job, "a file entry isn't one this app writes")
            path = f.get("path")
            if f.get("destination") == "main":
                if (not isinstance(path, str) or not Path(path).is_absolute() or canon(Path(path).parent) != canon(main)
                        or not publish.belongs(Path(path).name, stem)
                        or not Path(path).name.lower().endswith(EXTENSIONS[f["format"]])):
                    raise _damaged(job, f"it lists {path!r}, which isn't one of this job's files")
                prev.main_files.append((Path(path), f["sha256"]))
            elif f.get("destination") == "sample_table":
                p = _dxf_at_table(path, stem, main)
                if p is None or f["format"] != "dxf":
                    raise _damaged(job, f"it lists {path!r}, which isn't one of this job's files")
                sample_files.append((p, f["sha256"]))
            else:
                raise _damaged(job, "a file entry has no destination")
    section = record.get("sample_table")
    if section is None:
        if sample_files:
            raise _damaged(job, "sample-table files with no sample-table section")
        return prev
    if not isinstance(section, dict) or section.get("status") not in ("pending", "published"):
        raise _damaged(job, "its sample-table section isn't one this app writes")
    folder = section.get("folder")
    if (not isinstance(folder, str) or not Path(folder).is_absolute() or not valid_txn(section.get("txn"))
            or not valid_label(section.get("label")) or canon(Path(folder)) == canon(main)):
        raise _damaged(job, "its sample-table section isn't one this app writes")
    folder_p = Path(folder)
    if any(canon(p.parent) != canon(folder_p) for p, _ in sample_files):
        raise _damaged(job, "a sample-table file isn't in its sample-table folder")
    listed = section.get("files")
    if not isinstance(listed, list) or {key(Path(x)) for x in listed if isinstance(x, str)} != \
            {key(p) for p, _ in sample_files} or len(listed) != len(sample_files):
        raise _damaged(job, "its sample-table files don't match its parts")
    replaces = []
    for r in section.get("replaces", []) if isinstance(section.get("replaces"), list) else [None]:
        p = _dxf_at_table(r.get("path") if isinstance(r, dict) else None, stem, main)
        if p is None or not valid_sha(r.get("sha256")):
            raise _damaged(job, "a file it replaces at the sample table isn't one of this job's files")
        replaces.append((p, r["sha256"]))
    if not _uses_table(folder_p, job):
        # The table doesn't know this job: whatever the record says is there isn't this app's to touch.
        prev.unclaimed += [p for p, _ in sample_files] + [p for p, _ in replaces]
        return prev
    prev.table_folder = folder_p
    if section["status"] == "published":
        for p, sha in sample_files:
            at_table(p, sha)
        prev.published_txn = section["txn"]
        return prev
    staging = publish.staging_of(folder_p, True, section["txn"])
    moves = section.get("moves")
    shas = {key(p): s for p, s in sample_files}
    if not isinstance(moves, list) or len(moves) != len(sample_files):
        raise _damaged(job, "its waiting sample-table files don't match its parts")
    dsts = set()
    for m in moves:
        if (not isinstance(m, dict) or m.get("kind") != "install" or not isinstance(m.get("src"), str)
                or not isinstance(m.get("dst"), str) or not Path(m["src"]).is_absolute()
                or not Path(m["dst"]).is_absolute() or canon(Path(m["src"]).parent) != canon(staging)
                or not Path(m["src"]).name.endswith(Path(m["dst"]).name)
                or shas.get(key(Path(m["dst"]))) != m.get("sha")):
            raise _damaged(job, "its waiting sample-table files aren't where this app puts them")
        dsts.add(key(Path(m["dst"])))
    if dsts != set(shas):
        raise _damaged(job, "its waiting sample-table files don't match its parts")
    for p, sha in replaces:
        at_table(p, sha)
    prev.pending = {"folder": str(folder_p.resolve()), "txn": section["txn"], "label": section["label"],
                    "files": [p for p, _ in sample_files], "moves": [dict(m) for m in moves],
                    "replaces": [(p, s) for p, s in replaces if (p, s) in prev.at_table]}
    return prev


def _listed(record: Any) -> list[Path]:
    """Every path a manifest claims, read loosely (for whether another job owns a file)."""
    if not isinstance(record, dict):
        return []
    out = []
    for f in record.get("files", []) if isinstance(record.get("files"), list) else []:
        if isinstance(f, str):
            out.append(Path(f))
    for part in record.get("parts", []) if isinstance(record.get("parts"), list) else []:
        for f in part.get("files", []) if isinstance(part, dict) and isinstance(part.get("files"), list) else []:
            if isinstance(f, dict) and isinstance(f.get("path"), str):
                out.append(Path(f["path"]))
    section = record.get("sample_table")
    if isinstance(section, dict) and isinstance(section.get("replaces"), list):
        out += [Path(r["path"]) for r in section["replaces"] if isinstance(r, dict) and isinstance(r.get("path"), str)]
    return [p for p in out if p.is_absolute()]


def _owners(folder: Path, own: Path) -> dict[str, str]:
    """key(path) -> the other job (stem) whose manifest in `folder` lists it (any folder: an export folder,
    or a sample table that another designer uses as theirs). A manifest that can't be read is taken to own
    every name of its set, so nothing it might list is touched."""
    found: dict[str, str] = {}
    try:
        entries = [m for m in folder.iterdir() if m.name.lower().endswith(publish.MANIFEST)]
    except OSError:
        return found
    unreadable = []
    for m in entries:
        if key(m) == key(own):
            continue
        other = m.name[: -len(publish.MANIFEST)]
        record = publish.read_json(m)
        if isinstance(record, dict) and record.get("_unreadable"):
            unreadable.append(other)
            continue
        for p in _listed(record):
            found.setdefault(key(p), other)
    if unreadable:
        found["\0unreadable"] = "\0".join(unreadable)
    return found


class _Ownership:
    """Who else owns a path: a job record in the path's own folder, or a claim in its folder's ownership
    index (read once per folder per export)."""

    def __init__(self, job: Path) -> None:
        self.job = job
        self._by_folder: dict[str, dict[str, str]] = {}

    def of(self, p: Path) -> str | None:
        """Checked against the job records in this job's own export folder (they list sample-table files
        too), the records in `p`'s folder, and that folder's ownership index."""
        for folder in dict.fromkeys([self.job.parent, p.parent]):
            k = canon(folder)
            if k not in self._by_folder:
                self._by_folder[k] = _owners(folder, self.job)
            other = _owned(self._by_folder[k], p)
            if other:
                return other
        return _table_owner(p, self.job)


def _owned(owned: dict[str, str], p: Path) -> str | None:
    """Which other main-folder job owns `p`, including one whose record can't be read but whose names it has."""
    if key(p) in owned:
        return owned[key(p)]
    for other in owned.get("\0unreadable", "").split("\0"):
        if other and publish.belongs(p.name, other):
            return f"{other} (its job record can't be read right now)"
    return None


def _table_owner(path: Path, own: Path) -> str | None:
    """The other job, from any export folder, whose claim on this file still stands (any folder with an
    ownership index: a sample table, whatever role another export gives it). A claim whose job record
    can't be read right now still stands."""
    claim = _claim_of(path)
    if claim is None or key(claim) == key(own):
        return None
    record = publish.read_json(claim)
    name = f"{claim.name[: -len(publish.MANIFEST)]} in {claim.parent}"
    if isinstance(record, dict) and record.get("_unreadable"):
        return f"{name} (its job record can't be read right now)"
    if any(key(p) == key(path) for p in _listed(record)):
        return name
    return None


def _check_set(built: list[_Built], w: _Where, prev: _Prev,
               overwrite: bool) -> tuple[list[Path], list[Path], dict[str, str]]:
    """Ownership, hash and replacement checks. Returns the old files to move aside (main, sample table) and
    each one's hash as checked here: the export moves exactly that file, or nothing."""
    owners = _Ownership(w.job)
    targets = [(p, w.is_sample(f)) for b in built for f, p in b.targets.items()]
    for p, _sample in targets:
        publish.check_target(p)
        other = owners.of(p)
        if other:
            raise OwnedByAnother(f"{p.name} belongs to the export {other}; choose another name")
    publish.check_target(w.job)
    recorded = {key(p): sha for p, sha in [*prev.main_files, *prev.at_table]}
    candidates: dict[str, Path] = {}
    for p in [*(p for p, _ in prev.main_files), *(p for p, _ in prev.at_table), *(p for p, _ in targets),
              *_family(w)]:
        candidates.setdefault(key(p), p)
    old: list[Path] = []
    baseline: dict[str, str] = {}
    for _k, p in candidates.items():
        real = publish.find(p)
        if real is None:
            continue
        if real.is_symlink() or not real.is_file():
            raise PublishError(f"export destination must be a file in the export folder: {real}")
        if owners.of(real):
            continue  # another job's file, which this set doesn't need
        sha = recorded.get(key(real))
        now = publish.file_digest(real)
        if now is None or (sha is not None and now != sha):
            raise ChangedOnDisk(f"{real} changed since it was exported; move it aside (or put the exported file "
                                "back) before replacing this set")
        old.append(real)
        baseline[key(real)] = now
    manifest = publish.find(w.job)
    if (old or manifest is not None) and not overwrite:
        raise ExportExists(sorted({p.name for p in old} | ({manifest.name} if manifest else set())))
    main_old = [p for p in old if canon(p.parent) == canon(w.main)]
    sample_old = [p for p in old if canon(p.parent) != canon(w.main)]
    label = "x" * (len(w.stem) + publish.LABEL_EXTRA)
    for p in main_old:
        publish.check_length(publish.archive_root(p.parent, False) / label / p.name)
    for p in sample_old:
        publish.check_length(publish.archive_root(p.parent, True) / label / p.name)
        publish.check_length(publish.tmp_name(publish.pending_dir(p.parent) / f"{w.stem}{publish.JOURNAL}"))
    return main_old, sample_old, baseline


def _is_drive_root(folder: Path) -> bool:
    folder = folder.resolve()
    return folder.parent == folder


def _roots(w: _Where, prev: _Prev) -> set[Path]:
    roots = {w.main} | {r for r in prev.sample_folders if not _is_drive_root(r)}
    if w.sample is not None and not _is_drive_root(w.sample) and ("dxf" in w.fmts or w.sample.is_dir()):
        roots.add(w.sample)
    return roots


# ------------------------------------------------------------------ the manifest


def _part_record(b: _Built, files: list[dict[str, Any]]) -> dict[str, Any]:
    params = b.part.params
    board = get_board(params.board)
    cf2 = write_cf2(b.dieline)
    return {
        "part_id": b.part.part_id,
        "label": b.part.label,
        "quantity": b.part.quantity,
        "piece_no": b.part.piece_no,
        "slug": b.slug,
        "name": b.dieline.name,
        "units": params.units,
        # Options resolved to their values, so a later default change can't alter a reopened job.
        "params": _d.record_params(params, b.dieline),
        "board": {"code": board.id, "cal_in": board.cal, "il_in": board.il, "og_in": board.og,
                  "crrv_in": board.crrv, "source": board.source},
        "summary": b.dieline.meta,
        "checks": [r.to_dict() for r in b.results],
        "geometry_sha256": _geometry_digest(cf2),
        "cf2": cf2,  # as exported: "open as exported" shows exactly this
        "files": files,
    }


def _geometry_digest(cf2: str) -> str:
    # Units, rule types, pointage and every manufactured segment record; not the display name.
    records = (row for row in cf2.splitlines() if row in {"UI", "UM"} or row.startswith(("L,", "A,")))
    return sha256("\n".join(records).encode("ascii")).hexdigest()


# ------------------------------------------------------------------ export


def export(params: _d.BoxParams, output_dir: Path, filename: str | None = None, formats: list[Any] | None = None,
           overwrite: bool = False, dxf_dir: Path | None = None, *,
           check_cancel: Callable[[], None] | None = None, **kw: Any) -> dict:
    """A design of one part, from its parameters alone."""
    return export_design([ExportPart("p1", "box", params)], output_dir, filename, formats, overwrite, dxf_dir,
                         check_cancel=check_cancel, **kw)


def export_design(
    parts: list[ExportPart],
    output_dir: Path,
    filename: str | None = None,
    formats: list[Any] | None = None,
    overwrite: bool = False,
    dxf_dir: Path | None = None,
    *,
    design_id: str | None = None,
    revision: int | None = None,
    design_checks: list[dict[str, Any]] | None = None,
    structure: dict[str, Any] | None = None,
    check_cancel: Callable[[], None] | None = None,
    break_lock: str | None = None,
    confirm_pickup: bool = False,
    on_pending: Callable[[dict[str, Any]], None] | None = None,
) -> dict:
    """Write every part's files and the manifest.

    The main folder changes as one transaction. A separate sample-table folder
    (`dxf_dir`) follows, once the main folder has committed: straight away for
    one new file, otherwise only when `confirm_pickup` (the designer said the
    table won't pick up meanwhile). Until then its files wait beside it and the
    result says `pending`. `break_lock` is the id of one abandoned lock the
    designer agreed to break.
    """
    fmts = _formats(formats)
    built = _build(parts, fmts)
    w = _where(output_dir, dxf_dir, _d.safe_stem(filename or built[0].dieline.name), fmts)
    if w.sample is not None and "dxf" in fmts and _is_drive_root(w.sample):
        publish.pending_dir(w.sample)  # raises: a drive root can't be a sample-table folder
    _name_files(built, w)
    _check_lengths(built, w)
    for b in built:
        for p in b.targets.values():
            publish.check_target(p)
    publish.check_target(w.job)

    def ensure_active() -> None:
        if check_cancel is not None:
            check_cancel()

    with EXPORT_LOCK:
        ensure_active()
        w.main.mkdir(parents=True, exist_ok=True)
        if w.sample is not None and "dxf" in fmts:
            w.sample.mkdir(parents=True, exist_ok=True)
        for _attempt in range(3):
            roots = _roots(w, _read_prev(w.job, w.main, w.stem, w.sample))
            with ExitStack() as locks:
                publish.lock_all(locks, roots, break_lock)
                if not all(publish.locked(roots, r) for r in _roots(w, _read_prev(w.job, w.main, w.stem, w.sample))):
                    continue  # another export moved where the old set lives meanwhile; lock those too
                recovered = _recover_stem(w, roots)
                prev = _read_prev(w.job, w.main, w.stem, w.sample)
                return _export_locked(built, w, prev, overwrite, design_id, revision, design_checks, structure,
                                      ensure_active, confirm_pickup, on_pending, recovered)
        raise PublishError("other exports kept moving this set; try again")


def _export_locked(built: list[_Built], w: _Where, prev: _Prev, overwrite: bool, design_id: str | None,
                   revision: int | None, design_checks: list | None, structure: dict | None,
                   ensure_active: Callable[[], None], confirm_pickup: bool,
                   on_pending: Callable[[dict[str, Any]], None] | None, recovered: list[str]) -> dict:
    main_old, sample_old, baseline = _check_set(built, w, prev, overwrite)
    label = publish.label_for(w.stem)
    main_installs = [publish.Install(p, b.texts[f].encode("ascii"))
                     for b in built for f, p in b.targets.items() if not w.is_sample(f)]
    sample_installs = [publish.Install(p, b.texts[f].encode("ascii"))
                       for b in built for f, p in b.targets.items() if w.is_sample(f)]
    sample_folder = w.sample if sample_installs else (sample_old[0].parent.resolve() if sample_old else None)
    stxn = (publish.Txn(sample_folder, w.stem, sample_installs, sample_old, sample=True, label=label,
                        extra={"manifest": str(w.job)}, expect=baseline)
            if sample_folder is not None and (sample_installs or sample_old) else None)
    if stxn is not None:
        stxn.extra["sample_txn"] = stxn.id
    needs_pickup = stxn is not None and (len(sample_installs) > 1 or bool(sample_old))

    shas = {key(i.path): i.sha for i in [*main_installs, *sample_installs]}
    part_records = []
    for b in built:
        files = [{"format": f, "path": str(p), "sha256": shas[key(p)],
                  "destination": "sample_table" if w.is_sample(f) else "main"} for f, p in b.targets.items()]
        part_records.append(_part_record(b, files))
    txn = publish.Txn(w.main, w.stem, main_installs, main_old, label=label, expect=baseline)
    section = None
    smoves: list[dict[str, Any]] = []

    def drop_sample_staging() -> None:
        if stxn is not None and not publish.main_committed({"stem": w.stem, "txn": txn.id}, w.main) \
                and not txn.journal.exists():
            publish._remove_staging(stxn.staging, publish.pending_dir(stxn.folder).parent)

    try:
        if stxn is not None:
            smoves = publish.stage(stxn)  # before the main commit: a committed "pending" always has its files
            publish.mark_staging(stxn.staging, w.job, txn.id)  # whose it is, checked before any cleanup or pickup
            txn.extra.update(sample_folder=str(stxn.folder), sample_txn=stxn.id)
            for old_file in sample_old:  # exactly the file the checks saw, or nothing
                if publish.file_digest(old_file) != baseline[key(old_file)]:
                    raise ChangedOnDisk(f"{old_file} changed during the export; nothing was changed, export again")
            section = {
                "folder": str(stxn.folder), "txn": stxn.id, "status": "pending", "label": label,
                "files": [str(i.path) for i in sample_installs],
                "replaces": [{"path": str(p), "sha256": baseline[key(p)]} for p in sample_old],
                "moves": [{"kind": "install", "src": m["src"], "dst": m["dst"], "sha": m["sha"]} for m in smoves],
            }
        record = {
            "format": "dieline-studio-job",
            "version": JOB_VERSION,
            "engine": __version__,
            "exported_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "txn": txn.id,
            "name": built[0].dieline.name,
            "stem": w.stem,
            "folder": str(w.main),
            "design_id": design_id,
            "revision": revision,
            "exporter": {"profile": "default", "formats": w.fmts,
                         "verified_rules": {f: sorted(t.value for t in _d.VERIFIED_RULES[f]) for f in w.fmts}},
            "parts": part_records,
            "design_checks": design_checks or [],
            "groups": (structure or {}).get("groups", []),
            "links": (structure or {}).get("links", []),
            "files": [str(p) for b in built for p in b.targets.values()],
            "sample_table": section,
        }
        txn.manifest = (json.dumps(record, indent=2) + "\n").encode("utf-8")
        if stxn is not None:
            # Ownership at each table is recorded before anything is committed: no moment when the
            # committed set's names at a shared table belong to nobody.
            tables: dict[str, tuple[Path, list[Path]]] = {}
            for path in [i.path for i in sample_installs] + sample_old:
                tables.setdefault(canon(path.parent), (path.parent, []))[1].append(path)
            for table, paths in tables.values():
                publish.claim(table, paths, w.job)
        moves = publish.stage(txn)
        superseded = publish.run(txn, moves, ensure_active)  # committed from here on
    except BaseException:
        drop_sample_staging()  # the main export didn't commit: its sample set goes too
        raise
    _drop_obsolete_pending(prev, stxn.id if stxn else None, w.job)
    result: dict[str, Any] = {
        "path": str(next(iter(built[0].targets.values()))),
        "paths": [str(p) for b in built for p in b.targets.values()], "job_path": str(w.job),
        "overall": overall([r for b in built for r in b.results]), "name": built[0].dieline.name,
        "superseded": superseded, "recovered": recovered,
        "files": [{"part_id": b.part.part_id, "label": b.part.label, "quantity": b.part.quantity, "format": f,
                   "path": str(p), "destination": "sample_table" if w.is_sample(f) else "main"}
                  for b in built for f, p in b.targets.items()],
        "sample_table": {"status": "none"},
    }
    if stxn is None:
        return result
    result["sample_table"] = {"status": "pending", "folder": str(stxn.folder), "txn": stxn.id,
                              "files": section["files"], "replaces": [r["path"] for r in section["replaces"]]}
    if needs_pickup and not confirm_pickup:
        result["sample_table"]["reason"] = (
            "replaces files the sample table may be reading" if sample_old else "several files must arrive together")
        if on_pending is not None:
            on_pending({"job_path": str(w.job), **result["sample_table"]})
        return result
    try:
        result["superseded"] += _run_sample(stxn, smoves, w.job)
        result["sample_table"]["status"] = "published"
    except (OSError, PublishError) as exc:
        # A journal left behind means something needs the designer (a file that couldn't be put back, or
        # files that moved in while the record moved on); otherwise everything is back and waiting.
        kept = isinstance(exc, Unresolved) or stxn.journal.exists()
        result["sample_table"]["error"] = (f"{exc}; resolve that before a pickup" if kept else
                                           f"the sample-table files didn't move in ({exc}); they're waiting")
        if kept:
            result["sample_table"]["status"] = "blocked"
        if on_pending is not None:
            on_pending({"job_path": str(w.job), **result["sample_table"]})
    return result


def _run_sample(stxn: publish.Txn, moves: list[dict[str, Any]], job: Path) -> list[str]:
    def mark() -> None:
        if not _mark_published(job, stxn.id):
            raise PublishError("the files moved in, but the job record no longer waits for this set; its journal "
                               "is kept")
    return publish.run(stxn, moves, lambda: None, after_moves=mark)


def _mark_published(job: Path, sample_txn: str) -> bool:
    """After the sample-table files moved in: say so in the manifest. False if it's another export's now."""
    record = publish.read_json(job)
    section = record.get("sample_table") if isinstance(record, dict) else None
    if not isinstance(section, dict) or section.get("txn") != sample_txn:
        return False
    if section.get("status") != "published":
        section.update(status="published", replaces=[], moves=[])
        publish.fsync_write(job, (json.dumps(record, indent=2) + "\n").encode("utf-8"))
    return True


def _drop_obsolete_pending(prev: _Prev, keep: str | None, job: Path) -> None:
    """The replaced set's sample-table files never moved in; they never will now (only if they say
    they're that set's)."""
    if prev.pending is None or prev.pending["txn"] == keep:
        return
    folder = Path(prev.pending["folder"])
    waiting = publish.staging_of(folder, True, prev.pending["txn"])
    if publish.staging_owner(waiting) == (key(job), prev.txn):
        publish._remove_staging(waiting, publish.pending_dir(folder).parent)


# ------------------------------------------------------------------ pickup and recovery


def pickup(job_path: Path, txn: str, *, break_lock: str | None = None) -> dict:
    """The designer confirmed the sample table won't pick up: move the waiting files in."""
    if not job_path.name.lower().endswith(publish.MANIFEST) or not valid_txn(txn):
        raise ValueError("not a job record's waiting sample-table set")
    job = job_path.resolve()
    stem = job.name[: -len(publish.MANIFEST)]
    main = job.parent
    w = _Where(stem, main, None, ["dxf"])

    def current() -> _Prev:
        prev = _read_prev(job, main, stem)
        if prev.published_txn == txn:
            return prev
        if prev.pending is None or prev.pending["txn"] != txn:
            raise PublishError("this sample-table set was replaced by a newer export (or its sample table doesn't "
                               "record this job); nothing to move")
        return prev

    with EXPORT_LOCK:
        roots = {main} | current().sample_folders
        with ExitStack() as locks:
            publish.lock_all(locks, roots, break_lock)
            if not all(publish.locked(roots, r) for r in current().sample_folders):
                raise PublishError("another export changed this job meanwhile; try again")
            _recover_stem(w, roots)
            prev = current()
            if prev.published_txn == txn:
                return {"status": "published", "files": [str(p) for p, _ in prev.at_table], "superseded": []}
            pend = prev.pending
            assert pend is not None
            folder = Path(pend["folder"])
            moves = [{**m, "done": False} for m in pend["moves"]]
            waiting = publish.staging_of(folder, True, txn)
            mine = publish.staging_owner(waiting) == (key(job), prev.txn)
            states = [publish.move_state(m) for m in moves]
            new = {key(f) for f in pend["files"]}
            leftover = [p for p, _ in pend["replaces"] if key(p) not in new and publish.find(p) is not None]
            if (moves or pend["replaces"]) and all(s == "done" for s in states) and not leftover:
                # They moved in before a crash; the record just didn't say so.
                if not _mark_published(job, txn):
                    raise PublishError("this sample-table set was replaced by a newer export; nothing to move")
                if mine:
                    publish._remove_staging(waiting, publish.pending_dir(folder).parent)
                return {"status": "published", "files": [m["dst"] for m in moves], "superseded": []}
            if any(s != "not_done" for s in states):
                raise PublishError("the waiting sample-table files are missing or damaged; export the job again")
            if not mine:  # a removal-only set too: its folder holds just the owner file
                raise PublishError("the waiting sample-table files don't say they're this job's; export the job again")
            owners = _Ownership(job)
            replaces = {key(p): sha for p, sha in pend["replaces"]}
            archive = []
            for p, sha in pend["replaces"]:
                real = publish.find(p)
                if real is None:
                    continue
                if owners.of(real):
                    raise OwnedByAnother(f"{real.name} belongs to another export now; export this job again")
                if publish.file_digest(real) != sha:
                    raise ChangedOnDisk(f"{real} changed since the export; move it aside before the pickup")
                archive.append(real)
            label = "x" * (len(stem) + publish.LABEL_EXTRA)
            for p in archive:
                publish.check_length(publish.archive_root(p.parent, True) / label / p.name)
            for f in pend["files"]:
                publish.check_target(f)
                other = owners.of(f)
                if other:
                    raise OwnedByAnother(f"{f.name} belongs to the export {other} now; export this job again")
                real = publish.find(f)
                if real is not None and key(real) not in replaces:
                    raise ChangedOnDisk(f"{real} appeared in the sample-table folder after the export; move it "
                                        "aside before the pickup")
            # A fresh archive folder for each attempt: an earlier attempt's kept files are never overwritten.
            stxn = publish.Txn(folder, stem, [], archive, sample=True, label=publish.label_for(stem), id=txn,
                               extra={"manifest": str(job), "sample_txn": txn}, expect=replaces)
            superseded = _run_sample(stxn, moves, job)
            return {"status": "published", "files": [str(f) for f in pend["files"]], "superseded": superseded}


def _sample_committed(journal: dict[str, Any], job: Path) -> bool:
    """A sample-table transaction committed once its manifest says this set is published."""
    if not isinstance(journal.get("manifest"), str) or key(Path(journal["manifest"])) != key(job):
        raise PublishError("it belongs to an export from another export folder")
    record = publish.read_json(job)
    section = record.get("sample_table") if isinstance(record, dict) else None
    return isinstance(section, dict) and section.get("txn") == journal["txn"] and section.get("status") == "published"


def _sample_roll_forward(journal: dict[str, Any], job: Path) -> None:
    """Every file moved before the crash: the manifest is brought up to date (or the journal is kept)."""
    if not _mark_published(job, journal["txn"]):
        raise PublishError("its job record no longer waits for this set")


def _recover_stem(w: _Where, roots: set[Path]) -> list[str]:
    """Finish or undo an interrupted export of this name. The roots must be locked. One that can't be
    resolved safely blocks the export, with its journal kept."""
    notes, blocked = [], []
    sample_roots = [r for r in roots if canon(r) != canon(w.main)]
    parents = {canon(x) for x in sample_roots}
    note, ok = publish.recover_journal(w.main / f"{w.stem}{publish.JOURNAL}", folder=w.main, sample=False,
                                       archive_parents={canon(w.main)}, sample_roots=sample_roots,
                                       committed=lambda j: publish.main_committed(j, w.main))
    if note:
        (notes if ok else blocked).append(note)
    for r in sample_roots:
        note, ok = publish.recover_journal(
            publish.pending_dir(r) / f"{w.stem}{publish.JOURNAL}", folder=r, sample=True, archive_parents=parents,
            committed=lambda j: _sample_committed(j, w.job), roll_forward=lambda j: _sample_roll_forward(j, w.job))
        if note:
            (notes if ok else blocked).append(note)
    publish.sweep_staging(w.main)
    if blocked:
        raise Unresolved(" ".join(blocked))
    return notes


def recover_all(output_dir: Path, dxf_dir: Path | None) -> list[str]:
    """At app start: deal with every interrupted export in the export folder and the sample tables its jobs
    use. Folders another export holds are left for that export (it recovers what it finds)."""
    main = output_dir.resolve()
    if not main.is_dir():
        return []

    def discover() -> set[Path]:
        roots = {main}
        if (dxf_dir is not None and dxf_dir.is_dir() and canon(dxf_dir) != canon(main)
                and not _is_drive_root(dxf_dir)):
            roots.add(dxf_dir.resolve())
        try:
            manifests = [m for m in main.iterdir() if m.name.lower().endswith(publish.MANIFEST)]
        except OSError:
            manifests = []
        for m in manifests:
            try:
                roots |= {r for r in _read_prev(m, main, m.name[: -len(publish.MANIFEST)]).sample_folders
                          if not _is_drive_root(r)}
            except PublishError:
                continue
        return roots

    notes: list[str] = []
    with EXPORT_LOCK:
        for _attempt in range(3):
            roots = discover()
            try:
                with ExitStack() as locks:
                    publish.lock_all(locks, roots, None)
                    if not all(publish.locked(roots, r) for r in discover()):
                        continue  # another export changed which tables the jobs use: lock those too
                    _recover_locked(main, roots, notes)
                    return notes
            except LockHeld:
                return notes
    notes.append("Other exports kept changing the job records while the app started; interrupted exports "
                 "will be recovered on their next export.")
    return notes


def _recover_locked(main: Path, roots: set[Path], notes: list[str]) -> None:
    """Recover every journal in the export folder and the locked sample tables (all must be locked)."""
    sample_roots = [r for r in roots if canon(r) != canon(main)]
    parents = {canon(x) for x in sample_roots}
    for jpath in publish.journals(main):
        note, _ = publish.recover_journal(jpath, folder=main, sample=False, archive_parents={canon(main)},
                                          sample_roots=sample_roots,
                                          committed=lambda j: publish.main_committed(j, main))
        if note:
            notes.append(note)
    for r in sample_roots:
        try:
            where = publish.pending_dir(r)
        except PublishError:
            continue
        for jpath in publish.journals(where):
            job = main / f"{jpath.name[: -len(publish.JOURNAL)]}{publish.MANIFEST}"
            body = publish.read_json(jpath)
            if (isinstance(body, dict) and not body.get("_unreadable")
                    and isinstance(body.get("manifest"), str) and key(Path(body["manifest"])) != key(job)):
                continue  # another export folder's job: its own app recovers it
            note, _ = publish.recover_journal(
                jpath, folder=r, sample=True, archive_parents=parents,
                committed=lambda j, job=job: _sample_committed(j, job),
                roll_forward=lambda j, job=job: _sample_roll_forward(j, job))
            if note:
                notes.append(note)
    publish.sweep_staging(main)


# ------------------------------------------------------------------ preview of an export


def plan(parts: list[ExportPart], output_dir: Path, filename: str | None, formats: list[Any] | None,
         dxf_dir: Path | None) -> dict:
    """What an export would write, and whether it can: for the export bar, before the designer commits.
    The same checks as the export itself, without locking or writing."""
    fmts = _formats(formats)

    def refused(msg: str, files: list | None = None, w: _Where | None = None) -> dict:
        return {"ready": False, "blocking": [msg], "files": files or [], "job_path": str(w.job) if w else None,
                "replaces": [], "pickup": False}

    try:
        built = _build(parts, fmts)
    except (_d.DesignCheckFailed, _d.GeometryRefused) as exc:
        return refused(str(exc))
    w = _where(output_dir, dxf_dir, _d.safe_stem(filename or built[0].dieline.name), fmts)
    try:
        _name_files(built, w)
    except PublishError as exc:
        return refused(str(exc), w=w)
    files = [{"part_id": b.part.part_id, "label": b.part.label, "quantity": b.part.quantity, "format": f,
              "path": str(p), "destination": "sample_table" if w.is_sample(f) else "main"}
             for b in built for f, p in b.targets.items()]
    try:
        _check_lengths(built, w)
        prev = _read_prev(w.job, w.main, w.stem, w.sample)
        main_old, sample_old, _ = _check_set(built, w, prev, overwrite=True)
    except PublishError as exc:
        return refused(str(exc), files, w)
    manifest = publish.find(w.job)
    replaces = sorted({p.name for p in [*main_old, *sample_old]} | ({manifest.name} if manifest else set()))
    sample_new = [f for f in files if f["destination"] == "sample_table"]
    return {"ready": True, "blocking": [], "files": files, "job_path": str(w.job), "stem": w.stem,
            "replaces": replaces, "pickup": len(sample_new) > 1 or bool(sample_old)}


# ------------------------------------------------------------------ reopening


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
             "bottom_depth_in", "top_flaps_in", "bottom_flaps_in", "blank_in",
             "piece_in", "slots_in", "end_legs_in")  # the last three: partition pieces


def _changes(part: dict[str, Any], params: Any, design: dict[str, Any]) -> list[str]:
    """What regenerating with today's rules changes, against what the record kept."""
    for fld in ("board", "summary"):
        if fld in part and not isinstance(part[fld], dict):
            raise ValueError(f"job file {fld} must be an object")
    digest = part.get("geometry_sha256")
    if "geometry_sha256" in part and not valid_sha(digest):
        raise ValueError("job file geometry_sha256 must be a SHA-256 digest")
    changes: list[str] = []
    snap = part.get("board", {})
    b = get_board(params.board)
    for fld, now in (("cal_in", b.cal), ("il_in", b.il), ("og_in", b.og), ("crrv_in", b.crrv)):
        if fld not in snap:
            continue
        then = snap.get(fld)
        try:
            finite = type(then) in (int, float) and math.isfinite(then * 32)
        except OverflowError:
            finite = False
        if not finite:
            raise ValueError(f"job file board.{fld} must be a finite measurement in inches")
        if abs(then - now) > 1e-9:
            changes.append(f"{b.id} {fld[:-3].upper()} was {fmt_in(then)} in, now {fmt_in(now)} in")
    old = part.get("summary", {})
    for k in _COMPARED:
        if k in old and old[k] != design["summary"].get(k):
            label = k.removesuffix("_in").replace("_", " ")
            changes.append(f"{label} was {_fmt_value(old[k])}, now {_fmt_value(design['summary'].get(k))}")
    if digest is not None and _geometry_digest(design["cf2"]) != digest:
        changes.append("The regenerated cut or rule data differs from the exported geometry.")
    return changes


def _strings(record: dict[str, Any], *fields: str) -> None:
    for fld in fields:
        if fld in record and record[fld] is not None and not isinstance(record[fld], str):
            raise ValueError(f"job file {fld} must be a string")


def verify_set(record: dict[str, Any]) -> tuple[str, list[str]]:
    """Whether the files the record lists are still the set it exported: complete, pending (the sample
    table hasn't taken its files yet), incomplete, or unverified (opened away from its folder)."""
    stem = record.get("stem")
    folder = record.get("folder")
    if not isinstance(stem, str) or not isinstance(folder, str) or not Path(folder).is_absolute():
        return "unverified", ["This record doesn't say where it was exported, so its files weren't checked."]
    main = Path(folder)
    if not main.is_dir():
        return "unverified", ["Its export folder isn't reachable from here, so the files weren't checked."]
    job = main / f"{stem}{publish.MANIFEST}"
    live = publish.read_json(job)
    if not isinstance(live, dict) or not valid_txn(live.get("txn")) or live.get("txn") != record.get("txn"):
        return "incomplete", ["A different export of this name is in its folder now; this record isn't the "
                              "current set."]
    try:
        prev = _read_prev(job, main, stem)
    except PublishError as exc:
        return "incomplete", [str(exc)]
    notes: list[str] = []
    journals = [main / f"{stem}{publish.JOURNAL}"]
    for r in prev.sample_folders:
        try:
            journals.append(publish.pending_dir(r) / f"{stem}{publish.JOURNAL}")
        except PublishError:
            pass
    if any(j.exists() for j in journals):
        notes.append("An export of this set was interrupted; export again (or restart the app) to recover it.")
    for p, sha in [*prev.main_files, *prev.at_table]:
        now = publish.file_digest(p)
        if now is None:
            notes.append(f"{p.name} is missing.")
        elif sha is not None and now != sha:
            notes.append(f"{p.name} changed since it was exported.")
    for p in prev.unclaimed:
        notes.append(f"{p.name} isn't recorded as this job's at its sample table, so it wasn't checked.")
    if prev.pending is not None:
        for m in prev.pending["moves"]:
            if publish.move_state(m) != "not_done":
                notes.append(f"The waiting {Path(m['dst']).name} is missing or damaged; export the job again.")
    if notes:
        return "incomplete", notes
    if prev.pending is not None:
        return "pending", ["The sample-table files are waiting for pickup."]
    return "complete", []


def _structure(record: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    groups = record.get("groups", [])
    links = record.get("links", [])
    if not isinstance(groups, list) or not all(isinstance(g, dict) for g in groups):
        raise ValueError("job file groups must be a list")
    if not isinstance(links, list) or not all(isinstance(lk, dict) for lk in links):
        raise ValueError("job file links must be a list")
    return groups, links


def _part_meta(p: dict[str, Any]) -> dict[str, Any]:
    pid, label, q = p.get("part_id"), p.get("label"), p.get("quantity", 1)
    if not isinstance(pid, str) or not pid:
        raise ValueError("job file part has no id")
    if label is not None and not isinstance(label, str):
        raise ValueError("job file part label must be a string")
    if type(q) is not int or q < 1:
        raise ValueError("job file part quantity must be a whole number of at least 1")
    piece = p.get("piece_no")
    return {"part_id": pid, "label": label or "box", "quantity": q,
            "piece_no": piece if isinstance(piece, str) else None}


def open_job(record: Any, mode: str = "regenerate") -> dict:
    """Reopen a job record.

    - `exported`: exactly what was exported, from the record's own snapshot (version 2 only).
    - `regenerate`: rebuild every part with today's board values and rules, and say what changed.
    """
    if not isinstance(record, dict) or record.get("format") != "dieline-studio-job":
        raise ValueError("not a Dieline Studio job file")
    if mode not in ("exported", "regenerate"):
        raise ValueError("mode must be 'exported' or 'regenerate'")
    version = record.get("version")
    if type(version) is int and version > JOB_VERSION:
        raise ValueError(f"job file version {version!r} is newer than this app understands; update Dieline Studio")
    if type(version) is not int or version not in (1, 2):
        raise ValueError(f"invalid job file version {version!r}; expected 1 or {JOB_VERSION}")
    _strings(record, "stem", "exported_at", "engine")
    meta = {"stem": record.get("stem") or None, "exported_at": record.get("exported_at"),
            "engine": record.get("engine"), "version": version}
    if version == 1:
        if mode == "exported":
            raise ValueError("a version 1 job record keeps no exported geometry; it can only be regenerated")
        params = _d.BoxParams.model_validate(record.get("params"))
        design = _d.create_box(params)
        return {**meta, "mode": "regenerate", "design": design, "changes": _changes(record, params, design),
                "parts": [{"part_id": "p1", "label": "box", "quantity": 1, "piece_no": None,
                           "params": design["params"], "changes": []}],
                "groups": [], "links": [],
                "set_status": "unverified",
                "set_notes": ["A version 1 job record: it can only be regenerated with today's rules."]}
    parts = record.get("parts")
    if not isinstance(parts, list) or not parts or not all(isinstance(p, dict) for p in parts):
        raise ValueError("job file parts must be a list of parts")
    status, notes = verify_set(record)
    if mode == "exported":
        shown = []
        for p in parts:
            m = _part_meta(p)
            cf2 = p.get("cf2")
            if not isinstance(cf2, str):
                raise ValueError("job file part has no exported geometry")
            try:
                read_cf2(cf2)
            except ValueError as exc:
                raise ValueError(f"job file part {m['part_id']!r} geometry doesn't read: {exc}") from exc
            files = p.get("files") if isinstance(p.get("files"), list) else []
            name = next((Path(f["path"]).name for f in files if isinstance(f, dict) and f.get("format") == "cf2"
                         and isinstance(f.get("path"), str)), f"{p.get('name') or m['part_id']}.cf2")
            shown.append({"part_id": m["part_id"], "label": m["label"], "quantity": m["quantity"], "name": name,
                          "cf2": cf2})
        return {**meta, "mode": "exported", "parts": shown, "set_status": status, "set_notes": notes}
    groups, links = _structure(record)
    out_parts = []
    first = None
    for p in parts:
        m = _part_meta(p)
        params = _d.parse_params(p.get("params"))
        design = _d.create_box(params)
        first = first or design
        out_parts.append({**m, "params": design["params"], "changes": _changes(p, params, design)})
    several = len(out_parts) > 1
    changes = [f"{p['label']}: {c}" if several else c for p in out_parts for c in p["changes"]]
    return {**meta, "mode": "regenerate", "design": first, "changes": changes, "parts": out_parts,
            "groups": groups, "links": links, "set_status": status, "set_notes": notes}
