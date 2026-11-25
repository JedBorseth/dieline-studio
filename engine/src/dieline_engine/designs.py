"""The engine's public operations. The RPC server, the agent tools and the
plain form all go through these, so every path draws the same geometry."""

from __future__ import annotations

import re
import uuid
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .boards import BOARDS, get_board
from .cf2 import write_cf2
from .checks import overall, run_checks
from .styles import STYLES, Dims, get_style

MM_PER_INCH = 25.4


class BoxParams(BaseModel):
    """Everything an agent (or the form) may choose. No coordinates."""

    model_config = ConfigDict(extra="forbid")

    style: str = Field(description="Box style id, e.g. 'fefco-0201'")
    length: float = Field(gt=0, le=3000, description="Inside length")
    width: float = Field(gt=0, le=3000, description="Inside width")
    depth: float = Field(gt=0, le=3000, description="Inside depth")
    units: Literal["mm", "in"] = "mm"
    board: str = Field(description="Board grade id, e.g. 'C-32ECT'")

    def dims_mm(self) -> Dims:
        f = MM_PER_INCH if self.units == "in" else 1.0
        return Dims(self.length * f, self.width * f, self.depth * f)


def create_box(params: BoxParams) -> dict:
    style = get_style(params.style)
    board = get_board(params.board)
    dieline = style.build(params.dims_mm(), board)
    results = run_checks(dieline)
    return {
        "id": uuid.uuid4().hex[:12],
        "name": dieline.name,
        "params": params.model_dump(),
        "cf2": write_cf2(dieline),
        "checks": [r.to_dict() for r in results],
        "overall": overall(results),
        "summary": dieline.meta,
    }


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


def safe_filename(name: str) -> str:
    stem = _SAFE.sub("_", name).strip(" ._") or "design"
    stem = stem[:120]
    if stem.lower().endswith(".cf2"):
        stem = stem[:-4].rstrip(" .") or "design"
    if stem.split(".")[0].upper() in _RESERVED:
        stem = f"_{stem}"
    return f"{stem}.cf2"


class DesignCheckFailed(ValueError):
    """Raised when asked to export a design whose checks fail."""


def export_cf2(params: BoxParams, output_dir: Path, filename: str | None = None) -> dict:
    """Regenerate deterministically from params and write the CF2 file."""
    design = create_box(params)
    if design["overall"] == "fail":
        failed = "; ".join(c["message"] for c in design["checks"] if c["status"] == "fail")
        raise DesignCheckFailed(f"not exported: the design fails its checks ({failed})")
    output_dir.mkdir(parents=True, exist_ok=True)
    target = (output_dir / safe_filename(filename or design["name"])).resolve()
    if target.parent != output_dir.resolve():
        raise ValueError("export filename must not contain directories")
    # newline="" keeps the CRLF line endings CF2 consumers expect.
    with open(target, "w", encoding="ascii", newline="") as fh:
        fh.write(design["cf2"])
    return {"path": str(target), "overall": design["overall"], "name": design["name"]}


def catalog() -> dict:
    return {
        "styles": [
            {"id": s.id, "name": s.name, "description": s.description, "calibrated": s.calibrated}
            for s in STYLES.values()
        ],
        "boards": [
            {
                "id": b.id,
                "name": b.name,
                "flute": b.flute,
                "caliper_mm": b.caliper_mm,
                "corrugated": b.corrugated,
            }
            for b in BOARDS.values()
        ],
    }
