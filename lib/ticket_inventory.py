"""Read-only, source-bounded Ticketinventur capture. No completion decisions."""
from __future__ import annotations

import hashlib
import os
import re
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# These are evidence stores, not active work. Their omission is always reported.
EVIDENCE_DIRS = {"SOLVED", "DONE", "_archive"}
CACHE_DIRS = {".git", ".pytest_cache", "__pycache__", "_logs"}
OPEN_DIRS = {"INBOX", "ACTIONABLE", "QUEUED", "BLOCKED", "WAITING", "USER", "PARKED", "PENDING", ".USER"}
ID_RE = re.compile(r"(?m)^ID:\s*(T-\d{8}-\d+)\b")
NAME_RE = re.compile(r"^(T-\d{8}-\d+)(?:[._]|$)")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _identity(info: os.stat_result) -> tuple[int, int, int, int]:
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def _reparse(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)


def _sections(raw: bytes, include_content: bool) -> list[dict[str, Any]]:
    """Contiguous byte ranges cover every byte, including separators and BOM."""
    ends = [m.end() for m in re.finditer(rb"(?:\r?\n){2,}", raw)]
    if not ends or ends[-1] != len(raw):
        ends.append(len(raw))
    rows = []
    start = 0
    for end in ends:
        chunk = raw[start:end]
        row: dict[str, Any] = {
            "section": len(rows) + 1, "byte_start": start, "byte_end": end,
            "sha256": _sha(chunk), "disposition": "pending-semantic-review",
        }
        if include_content:
            row["text"] = chunk.decode("utf-8-sig" if start == 0 else "utf-8")
        rows.append(row)
        start = end
    return rows


def capture_inventory(queue: Path | str, *, include_content: bool = False) -> dict[str, Any]:
    """Capture all files in active/root/ancillary locations without following links.

    This is a sequential observation, not an atomic cloud snapshot. Each file is
    checked before/after reading; directory membership is also checked. The agent
    must still reread authority, content and locks immediately before any action.
    No network, provider, import of project code, move, write or status mutation.
    """
    base = Path(os.path.abspath(Path(queue).expanduser()))
    result: dict[str, Any] = {
        "schema": "ticket-inventory.v1", "queue": str(base),
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "state": "SEMANTIC_REVIEW_REQUIRED", "archive_go": False,
        "observation": "sequential-per-file-checked-not-atomic",
        "sources": [], "excluded": [], "errors": [],
    }

    def error(path: Path, code: str) -> None:
        result["errors"].append({"path": str(path), "code": code})

    def visit(directory: Path) -> None:
        try:
            before = directory.lstat()
            if _reparse(before) or not stat.S_ISDIR(before.st_mode):
                error(directory, "directory-link-reparse-or-nondirectory")
                return
            names = sorted(os.listdir(directory))
        except OSError:
            error(directory, "directory-unreadable")
            return
        for name in names:
            path = directory / name
            relative = path.relative_to(base).as_posix()
            try:
                info = path.lstat()
                if _reparse(info):
                    error(path, "link-or-reparse-not-followed")
                    continue
                if stat.S_ISDIR(info.st_mode):
                    if name in CACHE_DIRS or (directory == base and name in EVIDENCE_DIRS):
                        result["excluded"].append({
                            "path": relative,
                            "reason": "operational-cache" if name in CACHE_DIRS else "targeted-evidence-only",
                        })
                    else:
                        visit(path)
                    continue
                if not stat.S_ISREG(info.st_mode):
                    error(path, "not-a-regular-file")
                    continue
                with path.open("rb") as handle:
                    opened = os.fstat(handle.fileno())
                    if _identity(opened) != _identity(info):
                        error(path, "file-changed-before-read")
                        continue
                    raw = handle.read()
                    closed = os.fstat(handle.fileno())
                after = path.lstat()
                if _reparse(after) or _identity(info) != _identity(closed) or _identity(info) != _identity(after):
                    error(path, "file-changed-during-read")
                    continue
                try:
                    text = raw.decode("utf-8-sig")
                except UnicodeError:
                    text = None
                match = ID_RE.search(text or "") or NAME_RE.match(name)
                lock_file = name.startswith("LOCK.")
                ticket_file = bool(match) and name.endswith(".txt") and (directory == base or (directory.parent == base and directory.name in OPEN_DIRS))
                row = {
                    "path": relative, "id": match.group(1) if match and not lock_file else None,
                    "bytes": len(raw), "sha256": _sha(raw),
                    "kind": "lock" if lock_file else ("ticket" if ticket_file else "ancillary"),
                    "location": relative.split("/")[0] if "/" in relative else "ROOT",
                    "encoding": "utf-8" if text is not None else "opaque-needs-reader",
                    "disposition": "KEEP_ACTIVE_PENDING_REVIEW",
                    "semantic_review": "pending", "current_directive": None,
                    "rest": None, "successor": None, "acceptance": None,
                    "sections": _sections(raw, include_content) if text is not None else [],
                }
                result["sources"].append(row)
            except OSError:
                error(path, "file-unreadable")
        try:
            after = directory.lstat()
            if _reparse(after) or before.st_ino != after.st_ino or names != sorted(os.listdir(directory)):
                error(directory, "directory-changed-during-read")
        except OSError:
            error(directory, "directory-unreadable-after-read")

    # Refuse a linked queue or linked ancestor; abspath deliberately does not resolve them.
    for ancestor in (base, *base.parents):
        try:
            if _reparse(ancestor.lstat()):
                error(ancestor, "queue-or-ancestor-link-reparse")
        except OSError:
            error(ancestor, "queue-or-ancestor-unreadable")
    if not result["errors"]:
        visit(base)
    result["sources"].sort(key=lambda item: item["path"])
    result["counts"] = {
        "sources": len(result["sources"]),
        "tickets": sum(item["kind"] == "ticket" for item in result["sources"]),
        "errors": len(result["errors"]),
    }
    if result["errors"]:
        result["state"] = "INCOMPLETE_CAPTURE_HOLD"
    return result
