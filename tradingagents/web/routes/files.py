"""Serve report files for a given date — list, view, download."""

import os
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse, PlainTextResponse

from tradingagents.web.config_service import get_config_value

router = APIRouter(prefix="/files", tags=["files"])

VIEWABLE_EXTS = {".md", ".txt", ".json", ".csv", ".log"}


def _reports_dir() -> Path:
    val = get_config_value("reports_dir")
    return Path(val or str(Path.home() / ".tradingagents" / "reports"))


def _safe_resolve(base: Path, rel: str) -> Path:
    resolved = (base / rel).resolve()
    if not str(resolved).startswith(str(base.resolve())):
        raise HTTPException(403, "Path traversal not allowed")
    return resolved


def _size_display(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / (1024 * 1024):.1f} MB"


@router.get("")
def list_day_files(date: str = Query(..., description="YYYY-MM-DD")):
    base = _reports_dir()
    day_dir = base / date
    if not day_dir.is_dir():
        return {"files": []}

    files = []
    for root, _dirs, names in os.walk(day_dir):
        for name in sorted(names):
            full = Path(root) / name
            rel = str(full.relative_to(base))
            ext = full.suffix.lower()
            files.append({
                "name": name,
                "path": rel,
                "size": full.stat().st_size,
                "size_display": _size_display(full.stat().st_size),
                "viewable": ext in VIEWABLE_EXTS,
                "dir": str(Path(root).relative_to(day_dir)),
            })
    return {"files": files}


@router.get("/content")
def read_file_content(path: str = Query(...)):
    base = _reports_dir()
    full = _safe_resolve(base, path)
    if not full.is_file():
        raise HTTPException(404, "File not found")
    if full.suffix.lower() not in VIEWABLE_EXTS:
        raise HTTPException(400, "File type not viewable")
    try:
        content = full.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        raise HTTPException(500, str(e))
    return {"content": content, "name": full.name, "path": path}


@router.get("/download")
def download_file(path: str = Query(...)):
    base = _reports_dir()
    full = _safe_resolve(base, path)
    if not full.is_file():
        raise HTTPException(404, "File not found")
    return FileResponse(
        str(full),
        filename=full.name,
        media_type="application/octet-stream",
    )
