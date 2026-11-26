"""Publishing an export so a reader sees a whole set or nothing
(docs/DESIGN-SYSTEM.md, "Exports and job records").

Two kinds of destination:

- The main folder holds the CF2 (and DXF, when it has no separate sample-table
  folder) and the manifest `<stem>.dieline.json`. One transaction per export;
  replacing the manifest is the commit point.
- The sample-table folder is read by a machine that ignores manifests. It is a
  second transaction, run only after the main one commits. Its waiting files,
  journal, archive and ownership index live in a hidden folder beside it
  (`.<name>.dieline-pending`), never inside it, so only finished DXFs ever
  appear in the folder itself.

Every root has one lock file, always at `lock_dir(root)`, whatever role the
root plays in an export. An export holds the locks of every root it touches,
taken in one canonical order, for its whole check-and-publish.

A crash leaves a journal. Recovery, on the next export to that stem or when
the app starts, finishes a transaction that committed and undoes one that
didn't. Nothing in a journal is trusted: every path is checked against the
paths this transaction could have used, and a journal that doesn't fit is kept
for the designer and blocks that name until it's dealt with.
"""

from __future__ import annotations

import errno
import json
import os
import re
import shutil
import socket
import sys
import time
import uuid
from collections.abc import Callable, Iterable, Iterator
from contextlib import ExitStack
from dataclasses import dataclass, field
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from typing import Any

MANIFEST = ".dieline.json"
JOURNAL = ".dieline.pending.json"
LOCK = ".dieline.lock"
STAGING = ".dieline-staging"
PENDING = ".dieline-pending"  # suffix of the hidden folder beside a root
INDEX = "owners.json"  # sample-table ownership claims, in the hidden folder
STALE_LOCK_S = 600.0
BREAK_GUARD_STALE_S = 60.0
LOCK_WAIT_S = 10.0
WINDOWS_MAX_PATH = 259
EXTENSIONS = (".cf2", ".dxf")

TXN = re.compile(r"[0-9a-f]{16}")
SHA = re.compile(r"[0-9a-f]{64}")
_LABEL = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9._ -]{0,199}")
_SLUG = re.compile(r"[a-z0-9_-]+")


class PublishError(ValueError):
    """An export refused before anything changed."""


class LockHeld(PublishError):
    """Another export holds a destination. `stale`: its owner looks gone, and the designer may break
    exactly this lock (`lock_id`)."""

    def __init__(self, folder: Path, owner: dict[str, Any], stale: bool) -> None:
        who = f"{owner.get('host', 'another workstation')}, process {owner.get('pid', '?')}"
        if stale:
            msg = (f"{folder} is locked by an export that looks abandoned ({who}). Break the lock only if no "
                   "export is running there.")
        else:
            msg = f"another export is writing to {folder} ({who}); try again in a moment"
        super().__init__(msg)
        self.folder, self.owner, self.stale = folder, owner, stale
        self.lock_id = str(owner.get("id", ""))


class ExportExists(PublishError):
    """Existing files no manifest of this job owns, or an earlier set of this name: replacing needs the designer."""

    def __init__(self, names: list[str]) -> None:
        super().__init__(
            f"already exported under this name: {', '.join(names)}. Choose another name, or replace them "
            "(the old files move to a 'superseded' folder)."
        )
        self.names = names


class OwnedByAnother(PublishError):
    pass


class ChangedOnDisk(PublishError):
    pass


class Unresolved(PublishError):
    """An interrupted export couldn't be finished or undone safely; its journal is kept."""


# ------------------------------------------------------------------ names and paths


def digest(data: bytes) -> str:
    return sha256(data).hexdigest()


def file_digest(path: Path) -> str | None:
    try:
        if path.is_symlink() or not path.is_file():
            return None
        return digest(path.read_bytes())
    except OSError:
        return None


def norm_name(name: str) -> str:
    """A file name as Windows compares it: case-folded, trailing dots and spaces dropped."""
    return name.rstrip(" .").casefold()


def canon(p: Path) -> str:
    """A folder as Windows compares it (resolved, case-folded)."""
    return os.path.normcase(str(p.resolve())).casefold()


def key(p: Path) -> str:
    """A file path as Windows compares it, for ownership and collision checks."""
    return canon(p.parent) + "\0" + norm_name(p.name)


def belongs(name: str, stem: str) -> bool:
    """A file name of the set `stem`: `stem.cf2`, `stem.dxf`, or `stem-<slug>.cf2/.dxf`."""
    n = norm_name(name)
    s = norm_name(stem)
    for ext in EXTENSIONS:
        if n.endswith(ext):
            base = n[: -len(ext)]
            return base == s or (base.startswith(s + "-") and _SLUG.fullmatch(base[len(s) + 1:]) is not None)
    return False


def valid_txn(x: Any) -> bool:
    return isinstance(x, str) and TXN.fullmatch(x) is not None


def valid_sha(x: Any) -> bool:
    return isinstance(x, str) and SHA.fullmatch(x) is not None


def valid_label(x: Any) -> bool:
    return isinstance(x, str) and _LABEL.fullmatch(x) is not None and x.rstrip(" .") == x


def pending_dir(folder: Path) -> Path:
    """The hidden folder beside a root: a sample table's waiting files, journal, archive and index,
    and every root's lock."""
    folder = folder.resolve()
    if folder.parent == folder:
        raise PublishError(f"the sample-table folder can't be a drive root ({folder}): files would have "
                           "to wait inside it; choose a folder in Settings")
    return folder.parent / f".{folder.name}{PENDING}"


def lock_dir(folder: Path) -> Path:
    """Where a root's one lock lives, whatever role the root plays (a drive root keeps it inside)."""
    folder = folder.resolve()
    return folder if folder.parent == folder else pending_dir(folder)


def archive_root(old_parent: Path, sample: bool) -> Path:
    """Where replaced files go: `superseded/` in a main folder; beside a sample-table folder, never in it."""
    return (pending_dir(old_parent) if sample else old_parent.resolve()) / "superseded"


def _is_link(p: Path) -> bool:
    """A symbolic link, or (on Windows) an NTFS junction, which `is_symlink` doesn't report."""
    if p.is_symlink():
        return True
    is_junction = getattr(p, "is_junction", None)
    return bool(is_junction and is_junction())


def _real_child(d: Path, parent: Path) -> bool:
    """Whether `d` resolves to exactly `parent/<its name>`: no link anywhere, even one to a sibling."""
    want = os.path.normcase(str(parent.resolve() / d.name)).casefold()
    return os.path.normcase(str(d.resolve())).casefold() == want


def safe_dir(d: Path, parent: Path) -> Path:
    """Create (or accept) a folder that is really a child of `parent`: no link or junction leading elsewhere."""
    if _is_link(d) or (d.exists() and not d.is_dir()):
        raise PublishError(f"{d} must be a folder of its own, not a link")
    d.mkdir(exist_ok=True)
    if _is_link(d) or not _real_child(d, parent):
        raise PublishError(f"{d} must be a folder of its own, not a link")
    return d


def safe_dirs(d: Path, base: Path) -> Path:
    """`safe_dir` for every level from `base` (created if missing) down to `d`."""
    rel = d.relative_to(base)
    base.mkdir(parents=True, exist_ok=True)
    cur = base
    for part in rel.parts:
        cur = safe_dir(cur / part, cur)
    return cur


def no_links(d: Path, base: Path) -> bool:
    """Whether every existing level from `base` down to `d` is a real folder or file (no link or junction),
    so acting on `d` can't reach outside `base`. Nothing is created."""
    try:
        rel = d.relative_to(base)
    except ValueError:
        return False
    cur = base
    for part in rel.parts:
        parent, cur = cur, cur / part
        if _is_link(cur):
            return False
        if cur.exists() and not _real_child(cur, parent):
            return False
    return True


def check_target(p: Path) -> None:
    if p.is_symlink() or (p.exists() and not p.is_file()):
        raise PublishError(f"export destination must be a file in the export folder: {p.name}")
    check_length(p)


def check_length(p: Path | str) -> None:
    if len(str(p)) > WINDOWS_MAX_PATH:
        raise PublishError(f"{p} would be longer than Windows allows ({WINDOWS_MAX_PATH} characters); use a "
                           "shorter name or folder")


def tmp_name(path: Path) -> Path:
    return path.with_name(f".{path.name}.{uuid.uuid4().hex[:6]}.tmp")


def fsync_write(path: Path, data: bytes) -> None:
    """Write a file whole: a temporary beside it, flushed, then renamed over."""
    tmp = tmp_name(path)
    try:
        with open(tmp, "xb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
    _fsync_dir(path.parent)


def _fsync_dir(folder: Path) -> None:
    if os.name == "nt":
        return  # Windows has no directory handles to flush; NTFS journals the rename
    try:
        fd = os.open(folder, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def read_json(path: Path) -> Any:
    """None if missing; {"_unreadable": True} if it can't be read or parsed."""
    try:
        if path.is_symlink():
            return {"_unreadable": True}
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        return {"_unreadable": True}


def find(p: Path) -> Path | None:
    """The existing file Windows would open for `p`: an exact match, or one differing only in case or
    trailing dots and spaces (on a case-sensitive host)."""
    if p.exists() or p.is_symlink():
        return p
    try:
        want = norm_name(p.name)
        for entry in p.parent.iterdir():
            if norm_name(entry.name) == want:
                return entry
    except OSError:
        pass
    return None


# ------------------------------------------------------------------ locks


def _pid_alive(pid: int) -> bool:
    if pid == os.getpid():
        return False  # this process holds no lock outside the export it's running now
    if pid <= 0 or pid > 0xFFFFFFFF:
        return False
    if os.name == "nt":
        return _pid_alive_windows(pid)  # os.kill(pid, 0) would terminate it there
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except (OSError, OverflowError, ValueError):
        return False
    return True


def _pid_alive_windows(pid: int) -> bool:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return ctypes.get_last_error() == 5  # access denied: it exists
    try:
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return True
        return code.value == 259  # STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def is_stale(held: dict[str, Any]) -> bool:
    """A lock whose owner is gone. On this workstation that's a process that isn't running; from another
    workstation (or a lock whose owner can't be read) it's one older than ten minutes."""
    try:
        age = time.time() - float(held.get("time"))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        age = float("inf")
    pid = held.get("pid")
    if held.get("host") == socket.gethostname() and type(pid) is int:
        return not _pid_alive(pid)
    return age > STALE_LOCK_S


class FolderLock:
    """An exclusive lock file for one root, shared by every workstation writing there. A stale lock is
    broken only when `break_id` names it (the designer confirmed that one)."""

    def __init__(self, folder: Path, break_id: str | None = None) -> None:
        self.folder = folder.resolve()
        self.where = lock_dir(self.folder)
        self.path = self.where / LOCK
        self.break_id = break_id
        self.owner = {"host": socket.gethostname(), "pid": os.getpid(), "time": time.time(),
                      "id": uuid.uuid4().hex}

    def __enter__(self) -> FolderLock:
        if self.where != self.folder:
            safe_dirs(self.where, self.where.parent)
        deadline = time.monotonic() + LOCK_WAIT_S
        while True:
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                held = self._read()
                if held is None:
                    continue  # released between the two calls
                stale = is_stale(held)
                late = time.monotonic() > deadline
                if stale and self.break_id and held.get("id") == self.break_id and not late:
                    self._break(held)
                    continue
                if stale or late:
                    raise LockHeld(self.folder, held, stale) from None
                time.sleep(0.05)
                continue
            try:
                with os.fdopen(fd, "w") as fh:
                    json.dump(self.owner, fh)
                    fh.flush()
                    os.fsync(fh.fileno())
            except BaseException:
                self.path.unlink(missing_ok=True)
                raise
            return self

    def __exit__(self, *exc: object) -> None:
        held = self._read()
        if held is not None and held.get("id") == self.owner["id"]:
            self.path.unlink(missing_ok=True)

    def _read(self) -> dict[str, Any] | None:
        try:
            text = self.path.read_text()
        except FileNotFoundError:
            return None
        except OSError:
            # Busy (another process writing it, an antivirus scan): wait, never judge it abandoned.
            return {"time": time.time(), "id": "busy"}
        try:
            data = json.loads(text)
            if isinstance(data, dict) and isinstance(data.get("id"), str) and data["id"]:
                return data
        except ValueError:
            pass
        try:
            stat = self.path.stat()
        except FileNotFoundError:
            return None
        except OSError:
            return {"time": time.time(), "id": "busy"}
        # Half-written (an owner between create and write) or damaged: judged by the file's own age,
        # and named by it, so a confirmation can't break a different one.
        return {"time": stat.st_mtime, "id": f"unreadable-{stat.st_mtime_ns}"}

    def _break(self, held: dict[str, Any]) -> None:
        """Remove exactly the stale lock the designer confirmed. Breakers take turns through a guard file
        that only its maker removes, so no breaker can remove a lock (or a guard) another has just taken."""
        guard = self.where / f"{LOCK}.break"
        try:
            fd = os.open(guard, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                age = time.time() - guard.stat().st_mtime
            except OSError:
                age = 0.0
            if age > BREAK_GUARD_STALE_S:
                raise PublishError(f"an earlier attempt to break the lock in {self.where} was interrupted; if no "
                                   f"export is running there, delete {guard.name} in that folder") from None
            time.sleep(0.05)
            return
        os.close(fd)
        try:
            now = self._read()
            if now is not None and now.get("id") == held.get("id") and is_stale(now):
                self.path.unlink(missing_ok=True)
        finally:
            guard.unlink(missing_ok=True)

def lock_all(stack: ExitStack, folders: Iterable[Path], break_id: str | None) -> list[Path]:
    """Lock every root once (by physical identity), in one order, so two exports can't deadlock."""
    unique: dict[str, Path] = {}
    for f in folders:
        unique.setdefault(canon(lock_dir(f)), f.resolve())
    for _, f in sorted(unique.items()):
        stack.enter_context(FolderLock(f, break_id))
    return list(unique.values())


def locked(roots: Iterable[Path], folder: Path) -> bool:
    return canon(lock_dir(folder)) in {canon(lock_dir(r)) for r in roots}


# ------------------------------------------------------------------ transactions


@dataclass(slots=True)
class Install:
    path: Path
    data: bytes
    sha: str = ""

    def __post_init__(self) -> None:
        self.sha = digest(self.data)


def label_for(stem: str) -> str:
    return f"{stem} {datetime.now():%Y-%m-%d %H%M%S} {uuid.uuid4().hex[:8]}"


LABEL_EXTRA = 27  # characters label_for adds to the stem


@dataclass(slots=True)
class Txn:
    """One destination's part of an export."""

    folder: Path
    stem: str
    installs: list[Install]
    archive: list[Path] = field(default_factory=list)  # old files to move aside
    manifest: bytes | None = None  # main destination only: written last, the commit point
    sample: bool = False
    label: str = ""
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])
    extra: dict[str, Any] = field(default_factory=dict)  # recorded in the journal
    expect: dict[str, str] = field(default_factory=dict)  # key(old file) -> the hash the checks saw

    def __post_init__(self) -> None:
        self.folder = self.folder.resolve()

    @property
    def meta(self) -> Path:
        return pending_dir(self.folder) if self.sample else self.folder

    @property
    def journal(self) -> Path:
        return self.meta / f"{self.stem}{JOURNAL}"

    @property
    def staging(self) -> Path:
        return staging_of(self.folder, self.sample, self.id)

    def archive_dir(self, old_parent: Path) -> Path:
        return archive_root(old_parent, self.sample) / self.label


def staging_of(folder: Path, sample: bool, txn: str) -> Path:
    folder = folder.resolve()
    return (pending_dir(folder) / txn) if sample else (folder / STAGING / txn)


OWNER = ".owner.json"  # in a waiting sample set: the job and main transaction it belongs to


def mark_staging(staging: Path, manifest: Path, main_txn: str) -> None:
    fsync_write(staging / OWNER, json.dumps({"manifest": str(manifest), "main_txn": main_txn}).encode())


def staging_owner(staging: Path) -> tuple[str, str] | None:
    """(key of the job's manifest, its main transaction) a waiting sample set says it belongs to."""
    data = read_json(staging / OWNER)
    if (not isinstance(data, dict) or not isinstance(data.get("manifest"), str)
            or not valid_txn(data.get("main_txn"))):
        return None
    return key(Path(data["manifest"])), data["main_txn"]


def stage(t: Txn) -> list[dict[str, Any]]:
    """Write every new file into the transaction's staging folder and read each back."""
    safe_dirs(t.staging, t.meta.parent if t.sample else t.folder)
    moves = []
    for i, inst in enumerate(t.installs):
        sp = t.staging / f"{i}-{inst.path.name}"
        fsync_write(sp, inst.data)
        if file_digest(sp) != inst.sha:
            raise OSError(f"staged {inst.path.name} didn't read back the same")
        moves.append({"kind": "install", "src": str(sp), "dst": str(inst.path), "sha": inst.sha, "done": False})
    return moves


def _write_journal(t: Txn, body: dict[str, Any]) -> None:
    fsync_write(t.journal, json.dumps(body, indent=1).encode())


def run(t: Txn, moves: list[dict[str, Any]], before_commit: Callable[[], None],
        after_moves: Callable[[], None] | None = None) -> list[str]:
    """Archive the old files, install the staged ones and commit, journalling each step.
    The destinations must be locked. Anything that fails before the commit is undone; if undoing
    fails, the journal stays and the error says so. Returns where the old files went."""
    archive_moves = []
    for old in t.archive:
        sha = file_digest(old)
        if sha is None or (key(old) in t.expect and sha != t.expect[key(old)]):
            raise ChangedOnDisk(f"{old} changed or disappeared during the export; nothing was changed")
        archive_moves.append({"kind": "archive", "src": str(old), "dst": str(t.archive_dir(old.parent) / old.name),
                              "sha": sha, "done": False})
    moves = archive_moves + moves
    live = t.folder / f"{t.stem}{MANIFEST}"
    base = t.meta.parent if t.sample else t.folder
    body: dict[str, Any] = {"txn": t.id, "stem": t.stem, "sample": t.sample, "label": t.label, "moves": moves,
                            "copied": None, **t.extra}
    made: list[Path] = []
    committed = False
    try:
        _write_journal(t, body)
        before_commit()
        if t.manifest is not None and live.exists():
            copy_dir = _archive_dir_made(t, t.folder, made)
            body["copied"] = str(copy_dir / live.name)
            _write_journal(t, body)  # recorded first, so a rollback can find the copy
            shutil.copy2(live, copy_dir / live.name)  # the live manifest stays until the commit
        for m in moves:
            if m["kind"] == "archive":
                _archive_dir_made(t, Path(m["src"]).parent, made)
                # Never over a file already in the archive (an earlier attempt's kept version, say).
                if not move_no_clobber(Path(m["src"]), Path(m["dst"])):
                    # Refused before anything moved: this move never happened, and the journal says so.
                    moves.remove(m)
                    _write_journal(t, body)
                    raise ChangedOnDisk(f"{m['dst']} is already in the archive; nothing was changed, export again")
            elif not move_no_clobber(Path(m["src"]), Path(m["dst"])):
                # Its path should be empty now (its old file went aside, or there was none): something was
                # saved there meanwhile. That save stays; this move never happened.
                moves.remove(m)
                _write_journal(t, body)
                raise ChangedOnDisk(f"{m['dst']} was saved during the export; nothing was changed, export again")
            archived = file_digest(Path(m["dst"])) if m["kind"] == "archive" else m["sha"]
            if archived != m["sha"]:
                # Saved by someone between the check and the move. The transaction is abandoned (recorded
                # first, so recovery never finishes it), and those bytes go back only to an empty path:
                # a newer save there is never overwritten.
                body["aborted"] = True
                if archived is None:  # can't even read what went aside: nothing is guessed
                    _write_journal(t, body)
                    raise Unresolved(f"{m['src']} changed while it was being moved aside and {m['dst']} can't be "
                                     "read; check both by hand")
                m["observed"] = archived  # the bytes that went aside, to put back
                _write_journal(t, body)
                if not move_no_clobber(Path(m["dst"]), Path(m["src"])):
                    raise Unresolved(f"{m['src']} was saved again while it was being moved aside; both versions "
                                     f"are kept (the earlier save is {m['dst']}). Check them, then export again")
                moves.remove(m)
                _write_journal(t, body)
                raise ChangedOnDisk(f"{m['src']} changed during the export; nothing was changed, export again")
            m["done"] = True
            _write_journal(t, body)
        for folder in {Path(m["dst"]).parent for m in moves}:
            _fsync_dir(folder)
        if t.manifest is not None:
            staged = t.staging / f"manifest{MANIFEST}"
            fsync_write(staged, t.manifest)
            os.replace(staged, live)  # the commit point
        committed = True
        if t.manifest is not None:
            _fsync_dir(t.folder)
        if after_moves is not None:
            after_moves()
    except BaseException as exc:
        if not committed:
            try:
                _rollback(moves)  # raises (keeping the journal) if a file can't be put back
            except Unresolved as rb:
                if isinstance(exc, Unresolved):
                    raise Unresolved(f"{exc} {rb}") from exc
                raise
            _undo_copy(body)
            for d in reversed(made):
                _rmdir_if_empty(d)
            t.journal.unlink(missing_ok=True)
            if not t.sample:
                _remove_staging(t.staging, base)
        raise
    t.journal.unlink(missing_ok=True)  # before the staging goes: a replay never sees a half-removed staging
    _remove_staging(t.staging, base)
    out = [m["dst"] for m in moves if m["kind"] == "archive"]
    if body["copied"]:
        out.append(body["copied"])
    return out


def _archive_dir_made(t: Txn, old_parent: Path, made: list[Path]) -> Path:
    root = archive_root(old_parent, t.sample)
    if t.sample:
        safe_dirs(root.parent, root.parent.parent)  # the hidden folder beside the table
    for d in (root, root / t.label):
        existed = d.exists()
        safe_dir(d, d.parent)
        if not existed:
            made.append(d)
    return root / t.label


def _undo_copy(body: dict[str, Any]) -> None:
    if body.get("copied"):
        Path(body["copied"]).unlink(missing_ok=True)


def _remove_staging(staging: Path, base: Path) -> None:
    """Remove a staging folder, only if nothing on the way to it from `base` is a link."""
    if not no_links(staging, base) or not staging.exists():
        return
    shutil.rmtree(staging, ignore_errors=True)
    if staging.parent.name == STAGING:
        _rmdir_if_empty(staging.parent)


def _rmdir_if_empty(d: Path) -> None:
    try:
        d.rmdir()
    except OSError:
        pass


class NoSafeRename(PublishError):
    """This file system can't move a file without the risk of replacing another one."""


def rename_noreplace(src: Path, dst: Path) -> None:
    """Rename `src` to `dst` in one step that refuses (FileExistsError) if `dst` exists. Windows' rename
    already refuses; Linux has renameat2(RENAME_NOREPLACE), macOS renamex_np(RENAME_EXCL). Anywhere else, or
    on a file system that can't do it, nothing moves and NoSafeRename says why: never a two-step move a
    concurrent save could fall into."""
    if os.name == "nt":
        os.rename(src, dst)
        return
    import ctypes

    libc = ctypes.CDLL(None, use_errno=True)
    a, b = os.fsencode(str(src)), os.fsencode(str(dst))
    if sys.platform.startswith("linux") and hasattr(libc, "renameat2"):
        fn = libc.renameat2
        fn.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
        fn.restype = ctypes.c_int
        rc = fn(-100, a, -100, b, 1)  # AT_FDCWD, RENAME_NOREPLACE
    elif sys.platform == "darwin" and hasattr(libc, "renamex_np"):
        fn = libc.renamex_np
        fn.argtypes = (ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint)
        fn.restype = ctypes.c_int
        rc = fn(a, b, 0x4)  # RENAME_EXCL
    else:
        raise NoSafeRename(f"this system can't move {src.name} without risking an overwrite; export from Windows")
    if rc == 0:
        return
    err = ctypes.get_errno()
    if err == errno.EEXIST:
        raise FileExistsError(err, os.strerror(err), str(dst))
    if err in (errno.EINVAL, errno.ENOSYS, getattr(errno, "ENOTSUP", -1), getattr(errno, "EOPNOTSUPP", -1)):
        raise NoSafeRename(f"the file system holding {src.parent} can't move files without risking an overwrite")
    raise OSError(err, os.strerror(err), str(src))


def move_no_clobber(src: Path, dst: Path) -> bool:
    """Move a file to a path only if nothing is there, in one atomic step: False (and nothing moved) if
    it's taken."""
    try:
        rename_noreplace(src, dst)
    except FileExistsError:
        return False
    return True





def move_state(m: dict[str, Any]) -> str:
    """Where a move is: "done" (the file is at its destination, its source gone), "not_done" (the file is
    still at its source) or "inconsistent" (anything else: someone moved, changed or removed it, or a file
    now sits at both ends of an archive move, which always needs the designer)."""
    src, dst, sha = Path(m["src"]), Path(m["dst"]), m["sha"]
    src_there = src.exists() or src.is_symlink()
    if m["kind"] == "archive":
        dst_there = dst.exists() or dst.is_symlink()
        if src_there and dst_there:
            return "inconsistent"
        observed = m.get("observed")
        if observed is not None:  # a file saved during the move: those bytes are the ones to put back
            if dst_there:
                return "done" if file_digest(dst) == observed else "inconsistent"
            return "not_done" if src_there and file_digest(src) == observed else "inconsistent"
    if src_there:
        return "not_done" if file_digest(src) == sha else "inconsistent"
    return "done" if file_digest(dst) == sha else "inconsistent"


def _rollback(moves: list[dict[str, Any]]) -> None:
    """Move back every finished move, newest first. Anything that doesn't match the journal stops the
    rollback with the files named; nothing is guessed. An old file is never put back over a path whose
    new file couldn't be taken away."""
    errors = []
    blocked: set[str] = set()
    for m in reversed(moves):
        state = move_state(m)
        if state == "not_done":
            continue
        if m["kind"] == "archive" and key(Path(m["src"])) in blocked:
            errors.append(f"{m['src']} wasn't put back: the file at that path isn't the export's")
            continue
        if state == "inconsistent":
            errors.append(f"{m['dst'] if m['kind'] == 'install' else m['src']} isn't where the export left it")
            if m["kind"] == "install":
                blocked.add(key(Path(m["dst"])))
            continue
        try:
            if m["kind"] == "archive":
                # The old file goes back only to an empty path: a newer save there is never overwritten.
                if not move_no_clobber(Path(m["dst"]), Path(m["src"])):
                    errors.append(f"{m['src']} was saved again meanwhile; the earlier file is kept at {m['dst']}")
                    continue
            else:
                os.replace(m["dst"], m["src"])  # back into this transaction's own staging folder
        except OSError as err:
            errors.append(f"{m['dst']}: {err}")
            if m["kind"] == "install":
                blocked.add(key(Path(m["dst"])))
    if errors:
        raise Unresolved("the export couldn't put every file back: " + "; ".join(errors))


def all_moved(moves: list[dict[str, Any]]) -> bool:
    """Whether a transaction's moves have all happened, judged by the files: every install is at its
    destination, every archived file in its archive, and its old path empty or holding the new file."""
    if not moves:
        return False
    installed = set()
    for m in moves:
        if m["kind"] == "install":
            if move_state(m) != "done":
                return False
            installed.add(key(Path(m["dst"])))
    for m in moves:
        if m["kind"] == "archive":
            src = Path(m["src"])
            if file_digest(Path(m["dst"])) != m["sha"]:
                return False
            if (src.exists() or src.is_symlink()) and key(src) not in installed:
                return False
    return True


# ------------------------------------------------------------------ recovery


def _valid_move(m: Any, *, stem: str, folder: Path, staging: Path, sample: bool, label: str,
                archive_parents: set[str]) -> bool:
    if not isinstance(m, dict) or m.get("kind") not in ("install", "archive") or not valid_sha(m.get("sha")):
        return False
    if "observed" in m and not (m["kind"] == "archive" and valid_sha(m["observed"])):
        return False
    if not isinstance(m.get("src"), str) or not isinstance(m.get("dst"), str):
        return False
    src, dst = Path(m["src"]), Path(m["dst"])
    if not (src.is_absolute() and dst.is_absolute()):
        return False
    if m["kind"] == "install":
        return (canon(src.parent) == canon(staging) and src.name.endswith(dst.name)
                and canon(dst.parent) == canon(folder) and belongs(dst.name, stem)
                and (not sample or dst.name.lower().endswith(".dxf")))
    if not belongs(src.name, stem) or canon(src.parent) not in archive_parents:
        return False
    return key(dst) == key(archive_root(src.parent, sample) / label / src.name)


def _basic(journal: Any, *, stem: str, sample: bool) -> str | None:
    if not isinstance(journal, dict) or journal.get("_unreadable"):
        return "it can't be read"
    if not valid_txn(journal.get("txn")) or journal.get("stem") != stem or journal.get("sample") is not sample:
        return "it doesn't name this export"
    if not valid_label(journal.get("label")):
        return "its archive folder name isn't one this app makes"
    if not isinstance(journal.get("moves"), list):
        return "it has no list of moves"
    return None


def check_journal(journal: Any, *, stem: str, folder: Path, sample: bool, archive_parents: set[str]) -> str | None:
    """Why a journal isn't one this transaction could have written (or its folders are reached through a
    link), or None if it fits."""
    why = _basic(journal, stem=stem, sample=sample)
    if why is not None:
        return why
    label = journal["label"]
    staging = staging_of(folder, sample, journal["txn"])
    base = pending_dir(folder).parent if sample else folder
    if not no_links(staging, base):
        return "its staging folder is reached through a link"
    for m in journal["moves"]:
        if not _valid_move(m, stem=stem, folder=folder, staging=staging, sample=sample, label=label,
                           archive_parents=archive_parents):
            return "a file move in it isn't one this export could make"
        if m["kind"] == "archive":
            src = Path(m["src"])
            abase = pending_dir(src.parent).parent if sample else src.parent.resolve()
            if not no_links(Path(m["dst"]).parent, abase) or not no_links(src, src.parent.resolve()):
                return "its archive folder is reached through a link"
    copied = journal.get("copied")
    if copied is not None:
        want = archive_root(folder, False) / label / f"{stem}{MANIFEST}"
        if sample or not isinstance(copied, str) or key(Path(copied)) != key(want) or not no_links(want, folder):
            return "its manifest copy isn't where this export would put it"
    if sample and ("sample_folder" in journal or "sample_staging" in journal):
        return "it names sample-table staging, which only a main export does"
    sf, st = journal.get("sample_folder"), journal.get("sample_txn")
    if not sample and sf is not None:
        if not (isinstance(sf, str) and Path(sf).is_absolute() and valid_txn(st)):
            return "its sample-table staging isn't one this export would use"
        try:
            pending_dir(Path(sf))
        except PublishError:
            return "its sample-table folder is a drive root, which this app never uses"
    return None


def recover_journal(jpath: Path, *, folder: Path, sample: bool, archive_parents: set[str],
                    committed: Callable[[dict[str, Any]], bool],
                    roll_forward: Callable[[dict[str, Any]], None] | None = None,
                    sample_roots: Iterable[Path] = ()) -> tuple[str | None, bool]:
    """Finish or undo the transaction a journal describes. The roots it touches must be locked.

    `committed(journal)` says whether its commit point was reached (then only cleanup is left).
    Otherwise, if every move has happened by the files and `roll_forward` is given, that finishes it
    (a sample table: the manifest is brought up to date); else every move is undone. Returns
    (note, resolved): an unresolved journal is kept and blocks its name."""
    journal = read_json(jpath)
    if journal is None:
        return None, True
    stem = jpath.name[: -len(JOURNAL)]
    where = "sample-table folder" if sample else "export folder"

    def kept(why: str) -> tuple[str, bool]:
        return (f"An interrupted export of {stem} left a journal this app can't act on ({why}): {jpath}. Check "
                "that folder by hand, then move the journal aside.", False)

    why = _basic(journal, stem=stem, sample=sample)
    if why is not None:
        return kept(why)
    staging = staging_of(folder, sample, journal["txn"])
    base = pending_dir(folder).parent if sample else folder
    if not no_links(staging, base):
        return kept("its staging folder is reached through a link")
    try:
        done = committed(journal)
    except (OSError, PublishError) as err:
        return kept(str(err))
    if done:  # nothing moves: the journal and the staging folder go
        jpath.unlink(missing_ok=True)
        _remove_staging(staging, base)
        return f"An interrupted export of {stem} in the {where} was finished.", True
    why = check_journal(journal, stem=stem, folder=folder, sample=sample, archive_parents=archive_parents)
    if why is not None:
        return kept(why)
    if roll_forward is not None and not journal.get("aborted") and all_moved(journal["moves"]):
        try:
            roll_forward(journal)
        except (OSError, PublishError) as err:
            return kept(f"its files moved but the record couldn't be updated: {err}")
        jpath.unlink(missing_ok=True)
        _remove_staging(staging, base)
        return f"An interrupted export of {stem} in the {where} was finished.", True
    try:
        _rollback(journal["moves"])
    except (OSError, PublishError) as err:
        return f"An interrupted export of {stem} couldn't be undone ({err}); its journal {jpath} is kept.", False
    _undo_copy(journal)
    label = journal["label"]
    for m in journal["moves"]:
        if m["kind"] == "archive":
            _rmdir_if_empty(archive_root(Path(m["src"]).parent, sample) / label)
    if journal.get("copied"):
        _rmdir_if_empty(Path(journal["copied"]).parent)
    jpath.unlink(missing_ok=True)  # first: a replay must never see a half-removed staging
    note = ""
    if not sample:
        _remove_staging(staging, base)
        sf, st = journal.get("sample_folder"), journal.get("sample_txn")
        if isinstance(sf, str) and valid_txn(st):
            # The main export never committed: its waiting sample set goes too, if that table is locked
            # and the set says it is this export's.
            waiting = staging_of(Path(sf), True, st)
            manifest = folder / f"{stem}{MANIFEST}"
            if locked(sample_roots, Path(sf)) and staging_owner(waiting) == (key(manifest), journal["txn"]):
                _remove_staging(waiting, pending_dir(Path(sf)).parent)
            elif waiting.exists():
                note = f" Its unused sample-table files were left in {waiting}."
    return f"An interrupted export of {stem} in the {where} was undone; the earlier files are back.{note}", True


def main_committed(journal: dict[str, Any], folder: Path) -> bool:
    record = read_json(folder / f"{journal['stem']}{MANIFEST}")
    return isinstance(record, dict) and valid_txn(record.get("txn")) and record["txn"] == journal["txn"]


def journals(where: Path) -> Iterator[Path]:
    try:
        yield from sorted(p for p in where.iterdir() if p.name.lower().endswith(JOURNAL) and p.is_file())
    except OSError:
        return


def sweep_staging(folder: Path) -> None:
    """Staging folders no journal names: left by a crash after the journal went. Nothing refers to them.
    Skipped while any journal there can't be read: its staging might be one of these."""
    root = folder / STAGING
    if not root.is_dir() or not no_links(root, folder):
        return
    named = set()
    for j in journals(folder):
        body = read_json(j)
        if not (isinstance(body, dict) and valid_txn(body.get("txn"))):
            return
        named.add(body["txn"])
    for d in root.iterdir():
        if valid_txn(d.name) and d.name not in named:
            _remove_staging(d, folder)
    _rmdir_if_empty(root)


# ------------------------------------------------------------------ the sample-table ownership index


def claims(sample_folder: Path) -> dict[str, str] | None:
    """key(path) -> the manifest that claims it, for files at (or waiting for) a sample table; None when the
    index exists but isn't one this app writes (ownership there is then unknown, and nothing is replaced)."""
    path = pending_dir(sample_folder) / INDEX
    if not path.exists() and not path.is_symlink():
        return {}
    data = read_json(path)
    if not isinstance(data, dict) or data.get("_unreadable"):
        return None
    if not all(isinstance(k, str) and isinstance(v, str) and Path(v).is_absolute() for k, v in data.items()):
        return None
    return dict(data)


def claim(sample_folder: Path, paths: Iterable[Path], manifest: Path, release: Iterable[Path] = ()) -> None:
    """Record that `manifest` owns `paths` at this sample table (the folder must be locked)."""
    index = claims(sample_folder)
    if index is None:
        raise PublishError(f"the sample table's ownership index {pending_dir(sample_folder) / INDEX} can't be read; "
                           "check it by hand")
    for p in release:
        c = index.get(key(p))
        if c is not None and key(Path(c)) == key(manifest):
            index.pop(key(p))
    for p in paths:
        index[key(p)] = str(manifest)
    where = pending_dir(sample_folder)
    safe_dirs(where, where.parent)
    fsync_write(where / INDEX, json.dumps(index, indent=1, sort_keys=True).encode())


