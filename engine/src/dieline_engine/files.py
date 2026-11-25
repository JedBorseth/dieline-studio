"""Read-only search and reading of the shop's reference files (old CADs, specs,
logs) in the OneDrive folders synced to this PC, or other configured folders.

Read-only by construction: nothing here opens a file for writing, and every
path is resolved and checked to lie inside a configured folder first.

OneDrive "files on demand" keeps many files in the cloud until opened. A
search reads paths and descriptions of locally available CAD files.
Explicitly reading a cloud-only file downloads that one file, as opening it
in Explorer would.
"""

from __future__ import annotations

import base64
import io
import os
import re
import stat
import threading
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path

MAX_READ_BYTES = 50 * 1024 * 1024
MAX_TEXT_CHARS = 30_000
MAX_DOCX_XML_BYTES = 8 * 1024 * 1024
INDEX_TTL = 600.0  # seconds a folder listing is reused
MAX_INDEXED = 300_000
SKIP_DIRS = {".git", "node_modules", "$RECYCLE.BIN", "System Volume Information", "__pycache__", ".venv"}

# Windows file attributes for cloud placeholders.
_OFFLINE = 0x1000
_RECALL_ON_OPEN = 0x40000
_RECALL_ON_DATA_ACCESS = 0x400000
_CLOUD_ONLY = _OFFLINE | _RECALL_ON_OPEN | _RECALL_ON_DATA_ACCESS

TEXT_EXT = {".txt", ".csv", ".tsv", ".md", ".json", ".xml", ".ini", ".log", ".cf2", ".cff2", ".dxf"}
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp"}


def default_roots() -> list[Path]:
    """The work OneDrive the OneDrive client syncs on this PC. Personal OneDrive
    is never included by default; add folders in Settings to widen the scope."""
    v = os.environ.get("OneDriveCommercial")
    return [Path(v)] if v and Path(v).is_dir() else []


def _lexical(path: str) -> str:
    """Normalised absolute path without touching the file system."""
    return os.path.normcase(os.path.normpath(path))


def _within(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def _opened_path(fd: int, path: Path, st: os.stat_result) -> Path:
    """Find the path on the open handle before reading any contents."""
    if os.name == "nt":
        import ctypes
        import msvcrt
        from ctypes import wintypes

        final_path = ctypes.WinDLL("kernel32", use_last_error=True).GetFinalPathNameByHandleW
        final_path.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
        final_path.restype = wintypes.DWORD
        buffer = ctypes.create_unicode_buffer(32768)
        size = final_path(msvcrt.get_osfhandle(fd), buffer, len(buffer), 0)
        if not size or size >= len(buffer):
            raise ValueError("could not verify the opened reference file path")
        name = buffer.value
        if name.startswith("\\\\?\\UNC\\"):
            return Path("\\\\" + name[8:])
        return Path(name[4:] if name.startswith("\\\\?\\") else name)
    if os.path.isdir("/proc/self/fd"):
        return Path(os.readlink(f"/proc/self/fd/{fd}"))
    real = path.resolve(strict=True)
    if not os.path.samestat(real.stat(), st):
        raise ValueError("the reference file changed while it was being opened")
    return real


@dataclass(frozen=True, slots=True)
class Entry:
    path: str
    rel: str  # path relative to its root, lowercased for matching
    size: int
    mtime: float
    cloud_only: bool


class ReferenceFiles:
    def __init__(self, roots: list[Path]) -> None:
        self.roots = [r.resolve() for r in roots if r.is_dir()]
        self._lex_roots = [_lexical(str(r)) for r in self.roots]
        self._index: dict[Path, tuple[float, list[Entry]]] = {}
        self._meta: dict[tuple[str, float], str] = {}  # (path, mtime) -> lowercased ArtiosCAD text
        self._lock = threading.Lock()

    # ------------------------------------------------------------ search

    def _scan(self, root: Path) -> list[Entry]:
        entries: list[Entry] = []
        stack = [root]
        while stack and len(entries) < MAX_INDEXED:
            folder = stack.pop()
            try:
                if folder != root:
                    st = folder.lstat()
                    if getattr(st, "st_file_attributes", 0) & _CLOUD_ONLY:
                        continue
                    if folder.resolve(strict=True).parts != folder.parts:
                        continue
                with os.scandir(folder) as it:
                    for e in it:
                        if e.name.startswith((".", "~$")) or e.name in SKIP_DIRS:
                            continue
                        try:
                            # Links and junctions could lead outside the folder: never followed.
                            if e.is_symlink() or (hasattr(e, "is_junction") and e.is_junction()):
                                continue
                            if e.is_dir(follow_symlinks=False):
                                stack.append(Path(e.path))
                            elif e.is_file(follow_symlinks=False):
                                st = e.stat(follow_symlinks=False)
                                attrs = getattr(st, "st_file_attributes", 0)
                                cloud = bool(attrs & _CLOUD_ONLY)
                                rel = os.path.relpath(e.path, root)
                                entries.append(Entry(e.path, rel.lower(), st.st_size, st.st_mtime, cloud))
                        except OSError:
                            continue
            except OSError:
                continue
        return entries

    def _entries(self) -> list[Entry]:
        now = time.monotonic()
        out: list[Entry] = []
        with self._lock:
            for root in self.roots:
                cached = self._index.get(root)
                if cached is None or now - cached[0] > INDEX_TTL:
                    cached = (now, self._scan(root))
                    self._index[root] = cached
                out.extend(cached[1])
        return out

    def search(self, query: str, extensions: list[str] | None = None, limit: int = 25) -> dict:
        if not self.roots:
            raise ValueError("no reference folders: OneDrive isn't synced on this PC and none are set in Settings")
        terms = [t for t in re.split(r"[\s,]+", query.lower()) if t]
        if not terms:
            raise ValueError("query is empty")
        exts = {e.lower() if e.startswith(".") else f".{e.lower()}" for e in (extensions or [])}
        scored: list[tuple[float, Entry]] = []
        deadline = time.monotonic() + 15.0
        skipped = 0
        for e in self._entries():
            suffix = Path(e.path).suffix.lower()
            if exts and suffix not in exts:
                continue
            name = Path(e.rel).name
            hits = [t for t in terms if t in e.rel]
            missing = [t for t in terms if t not in e.rel]
            inside: list[str] = []
            if missing and suffix == ".ard" and not e.cloud_only:
                # ArtiosCAD descriptions ("HSC Gaylord Lid", board, sizes) are
                # inside the file. Only files already on this PC are read.
                if time.monotonic() < deadline:
                    text = self._ard_text(e)
                    inside = [t for t in missing if t in text]
                else:
                    skipped += 1
            if len(hits) + len(inside) < len(terms):
                continue  # every term must appear in the path or the description
            score = sum(3 if t in name else 1 for t in hits) + len(inside) + e.mtime / 1e10  # newer first on ties
            scored.append((score, e))
        scored.sort(key=lambda x: -x[0])
        limit = max(1, min(int(limit), 100))
        return {
            "roots": [str(r) for r in self.roots],
            "matches": [
                {
                    "path": e.path,
                    "size_kb": round(e.size / 1024, 1),
                    "modified": time.strftime("%Y-%m-%d", time.localtime(e.mtime)),
                    "cloud_only": e.cloud_only,
                }
                for _, e in scored[:limit]
            ],
            "total_matches": len(scored),
            **({"note": f"{skipped} ArtiosCAD files weren't searched inside (time limit); refine the query"}
               if skipped else {}),
        }

    def _ard_text(self, e: Entry) -> str:
        key = (e.path, e.mtime)
        with self._lock:
            hit = self._meta.get(key)
        if hit is not None:
            return hit
        try:
            st = os.lstat(e.path)
            attrs = getattr(st, "st_file_attributes", 0)
            if (attrs & _CLOUD_ONLY or not stat.S_ISREG(st.st_mode)
                    or st.st_mtime != e.mtime or st.st_size != e.size):
                return ""  # went cloud-only or changed since the listing: don't open it
            _, data = self._read_bytes(e.path, limit=2 * 1024 * 1024, indexed=e)
        except (OSError, ValueError):
            return ""
        facts = artioscad_facts(data)
        text = " ".join([*facts["standard_refs"], *facts["boards"], facts["sheet_size_in"] or "",
                         *facts["dimension_strings"], *facts["text"][:60]]).lower()
        with self._lock:
            self._meta[key] = text
        return text

    # -------------------------------------------------------------- read

    def check(self, path: str) -> Path:
        """The real path of a reference file, after checking it lies inside a
        reference folder both as written (before any file-system access, so a
        UNC path elsewhere is never contacted) and after links are resolved."""
        if not isinstance(path, str) or not path or "\0" in path:
            raise ValueError("give the full path from search_files")
        if path.startswith(("\\\\?\\", "\\\\.\\", "//?/", "//./")):
            raise ValueError("device and extended-length paths aren't accepted; use the path from search_files")
        p = Path(path)
        if not p.is_absolute():
            raise ValueError("give the full path from search_files")
        lex = _lexical(path)
        if not any(_within(lex, r) for r in self._lex_roots):
            raise ValueError("that file is outside the reference folders")
        real = p.resolve()
        if not any(_within(_lexical(str(real)), r) for r in self._lex_roots):
            raise ValueError("that file is outside the reference folders")
        return real

    def resolve(self, path: str) -> Path:
        """Kept for callers that only need the checked path."""
        real = self.check(path)
        st = real.stat()
        if not stat.S_ISREG(st.st_mode):
            raise ValueError("not a file")
        if st.st_size > MAX_READ_BYTES:
            raise ValueError(f"file is over {MAX_READ_BYTES // (1024 * 1024)} MB")
        return real

    def read_bytes(self, path: str) -> tuple[Path, bytes]:
        """Check, then open once and verify the opened handle itself, so the file
        can't be swapped between the check and the read."""
        return self._read_bytes(path, limit=MAX_READ_BYTES)

    def _read_bytes(self, path: str, limit: int, indexed: Entry | None = None) -> tuple[Path, bytes]:
        real = self.check(path)
        before = real.stat()
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("not a file")
        if indexed and getattr(before, "st_file_attributes", 0) & _CLOUD_ONLY:
            raise ValueError("the reference is no longer available locally")
        with open(real, "rb") as fh:
            st = os.fstat(fh.fileno())
            if not stat.S_ISREG(st.st_mode) or (os.name != "nt" and not os.path.samestat(st, before)):
                raise ValueError("the reference file changed while it was being opened")
            opened = _opened_path(fh.fileno(), real, st)
            # Compare canonical components with case preserved for case-sensitive NTFS directories.
            if not any(opened.parts[:len(root.parts)] == root.parts for root in self.roots):
                raise ValueError("the opened file is outside the reference folders")
            if indexed:
                if st.st_size != indexed.size or st.st_mtime != indexed.mtime:
                    raise ValueError("the reference file changed after it was indexed")
                return opened, fh.read(limit)
            if st.st_size > limit:
                raise ValueError(f"file is over {limit // (1024 * 1024)} MB")
            data = fh.read(limit + 1)
        if len(data) > limit:
            raise ValueError(f"file is over {limit // (1024 * 1024)} MB")
        return opened, data


def read_reference(name: str, data: bytes, page: int = 1) -> dict:
    """What can usefully be read from a reference file's bytes, by type. May
    include `_image` (PNG bytes) and `_dieline` (a Dieline, for CF2/DXF)."""
    ext = Path(name).suffix.lower()
    if ext in (".ard", ".mfg", ".a3d"):
        return {"kind": "artioscad", **artioscad_facts(data)}
    if ext in (".cf2", ".cff2"):
        from .cf2 import read_cf2

        res = read_cf2(data.decode("latin-1"))
        return {"kind": "cf2", "_dieline": res.dieline, "warnings": res.warnings[:20], "file_units": res.units}
    if ext == ".dxf":
        from .dxf import read_dxf

        d, warnings = read_dxf(data.decode("latin-1"))
        return {"kind": "dxf", "_dieline": d, "warnings": warnings[:20], "file_units": d.units}
    if ext == ".pdf":
        return {"kind": "pdf", **_pdf_text(data, page)}
    if ext in (".xlsx", ".xlsm"):
        return {"kind": "spreadsheet", **_xlsx_text(data, page)}
    if ext == ".docx":
        return {"kind": "document", **_docx_text(data, page)}
    if ext in IMAGE_EXT:
        return {"kind": "image", "_image": _image_png(data)}
    if ext in TEXT_EXT:
        return {"kind": "text", **_page_of(data.decode("utf-8", errors="replace"), page)}
    return {"kind": "other", "note": f"{ext or 'this'} files aren't readable here; only the name and path are known"}


def read_path(path: Path, page: int = 1) -> dict:
    """Read a file the caller has already confined (tests, local tools)."""
    return read_reference(path.name, path.read_bytes(), page)


def _page_of(text: str, page: int) -> dict:
    pages = max(1, -(-len(text) // MAX_TEXT_CHARS))
    page = max(1, min(page, pages))
    start = (page - 1) * MAX_TEXT_CHARS
    return {"text": text[start:start + MAX_TEXT_CHARS], "page": page, "pages": pages}


_PRINTABLE = re.compile(rb"[\x20-\x7e]{3,}")
_BOARD = re.compile(r"^\s*(\d{2,3}\s?(?:ECT\s?)?(?:E|B|C|BC|EB|CC)\s+[A-Z]{1,3}(?:\s.*)?)\s*$")
_SHEET = re.compile(r"^(\d+(?:\+\d+/\d+)?) x (\d+(?:\+\d+/\d+)?)S")
_DIMS = re.compile(r"\d+(?:\.\d+)?(?:[ +-]\d+/\d+)?\s*[xX]\s*\d+(?:\.\d+)?(?:[ +-]\d+/\d+)?(?:\s*[xX]\s*\d+(?:\.\d+)?(?:[ +-]\d+/\d+)?)?")
_DATE = re.compile(r"^(20\d\d-\d\d-\d\d)")


def artioscad_facts(data: bytes) -> dict:
    """Facts visible as plain text in an ArtiosCAD file: description, board,
    the standard it was built from, sheet size, dates. Nothing is decoded
    beyond printable text; the geometry itself isn't readable this way (open
    an exported CF2/DXF of the design for that)."""
    strings = [m.group().decode("ascii", "replace").strip() for m in _PRINTABLE.finditer(data)]
    strings = [s for s in strings if s]
    standards = sorted({s.strip() for s in strings
                        if s.lower().endswith(".ard") and "\\" not in s and "/" not in s})
    boards = sorted({m.group(1).strip() for s in strings if (m := _BOARD.match(s))})
    # ArtiosCAD's sheet-size string is height x width; reported here as width x height.
    sheet = next((f"{m.group(2).replace('+', ' ')} x {m.group(1).replace('+', ' ')}"
                  for s in strings if (m := _SHEET.match(s))), None)
    dims = sorted({m.group() for s in strings for m in _DIMS.finditer(s) if len(s) < 80})[:12]
    dates = sorted({m.group(1) for s in strings if (m := _DATE.match(s))})
    texts = [s for s in strings if len(s) >= 6 and re.search(r"[A-Za-z]{3}", s)
             and not re.search(r"(ITEMREF|offset2|con \(|move to|\*#I|FONT `)", s)][:120]
    return {
        "standard_refs": standards[:20],
        "boards": boards[:10],
        "sheet_size_in": sheet,  # blank width x height
        "dimension_strings": dims,
        "dates": dates[-5:],
        "text": texts,
        "note": "Plain text from the ArtiosCAD file. L x W x D, board and blank size usually appear in the "
                "description and sheet size; exact geometry needs an exported CF2 or DXF.",
    }


MAX_PDF_PAGES_PER_READ = 25
MAX_PAGE_CHARS = 20_000
MAX_SHEET_ROWS = 5_000
MAX_SHEET_COLS = 60
MAX_IMAGE_PIXELS = 40_000_000


def _pdf_text(data: bytes, page: int) -> dict:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    n = len(reader.pages)
    start = max(1, min(page, n)) if n else 1
    out, chars, last = [], 0, start - 1
    # At most a fixed number of pages per call, whatever their size.
    for i in range(start - 1, min(n, start - 1 + MAX_PDF_PAGES_PER_READ)):
        t = (reader.pages[i].extract_text() or "")[:MAX_PAGE_CHARS]
        if chars + len(t) > MAX_TEXT_CHARS and out:
            break  # this page starts the next read
        out.append(f"--- page {i + 1} ---\n{t}")
        chars += len(t)
        last = i + 1
    return {"text": "\n".join(out)[:MAX_TEXT_CHARS], "pages": n, "from_page": start, "to_page": last,
            **({"next_page": last + 1} if last < n else {})}


def _xlsx_text(data: bytes, page: int) -> dict:
    from openpyxl import load_workbook

    wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    try:
        rows: list[str] = []
        clipped = False
        for ws in wb.worksheets:
            rows.append(f"### sheet {ws.title}")
            # Bounded by row and column count, not by the sheet's declared size.
            for row in ws.iter_rows(max_row=MAX_SHEET_ROWS, max_col=MAX_SHEET_COLS, values_only=True):
                if any(v is not None for v in row):
                    rows.append("\t".join("" if v is None else str(v)[:500] for v in row).rstrip("\t"))
            if (ws.max_row or 0) > MAX_SHEET_ROWS or (ws.max_column or 0) > MAX_SHEET_COLS:
                clipped = True
    finally:
        wb.close()
    out = _page_of("\n".join(rows), page)
    if clipped:
        out["note"] = f"large sheets were read up to {MAX_SHEET_ROWS} rows x {MAX_SHEET_COLS} columns"
    return out


def _docx_text(data: bytes, page: int) -> dict:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z, z.open("word/document.xml") as document:
            xml_bytes = document.read(MAX_DOCX_XML_BYTES + 1)
    except (KeyError, zipfile.BadZipFile) as exc:
        raise ValueError("invalid Word document") from exc
    if len(xml_bytes) > MAX_DOCX_XML_BYTES:
        raise ValueError(f"Word document XML is over {MAX_DOCX_XML_BYTES // (1024 * 1024)} MB")
    xml = xml_bytes.decode("utf-8", "replace")
    text = re.sub(r"<[^>]+>", "", re.sub(r"</w:p>", "\n", xml))
    return _page_of(text, page)


def _image_png(data: bytes) -> bytes:
    from PIL import Image

    with Image.open(io.BytesIO(data)) as im:
        if im.width * im.height > MAX_IMAGE_PIXELS:
            raise ValueError("image is too large to read")
        im.draft("RGB", (1568, 1568))  # JPEG: decode at reduced size
        im = im.convert("RGB")
        im.thumbnail((1568, 1568))
        buf = io.BytesIO()
        im.save(buf, format="PNG", optimize=True)
        return buf.getvalue()


def b64(data: bytes) -> str:
    return base64.standard_b64encode(data).decode("ascii")
