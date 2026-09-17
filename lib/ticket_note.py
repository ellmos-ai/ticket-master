r"""
ticket_note.py — Atomic updates for STATUS and VERLAUF/LOG in ticket files.

Ticket T-20260906-543388409: ticket_mover.py writes the cluster token into the
STATUS line or warns on drift, but neither sets custom reason text nor appends
dated progress notes to VERLAUF/LOG. Sessions had to craft ad-hoc scripts to
maintain STATUS and VERLAUF consistency.

This module provides a reliable CLI and Python API to:
  1. Atomically update the STATUS line (preserving custom suffix text).
  2. Atomically append dated progress notes before the LOESUNG/SOLUTION block.
  3. Preserve line endings (CRLF vs LF) and UTF-8 encoding (including UTF-8 BOM).
  4. Perform atomic replacement so readers never see half-written tickets.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from datetime import date
from pathlib import Path
from typing import Any

try:
    from .ticket_audit import _STATUS_LINE_RE
    from .routing_contract import StaleContentError, content_hash
except ImportError:
    # Fallback when running directly or from lib/
    from ticket_audit import _STATUS_LINE_RE
    from routing_contract import StaleContentError, content_hash

__all__ = [
    "append_ticket_note",
    "update_ticket_note",
]

_LOESUNG_BLOCK_RE = re.compile(
    r"(?m)^(?P<delim>-{10,}\s*\n)?(?P<header>(?:##\s*)?(?:LOESUNG|SOLUTION|RESOLUTION)[^\n]*)",
    re.IGNORECASE,
)

_CLOSING_DELIM_RE = re.compile(r"(?m)^={10,}\s*$")

_STATUS_PREFIX_STRIP_RE = re.compile(r"^(?:STATUS:|\*\*Status:\*\*)\s*", re.IGNORECASE)


def update_ticket_note(
    path: Path | str,
    *,
    status: str | None = None,
    verlauf: str | None = None,
    entry_date: str | None = None,
    dry_run: bool = False,
    expected_hash: str | None = None,
) -> dict[str, Any]:
    """Atomically update STATUS and/or append to VERLAUF in a ticket file.

    Parameters:
      path: Path to ticket file.
      status: New value for the STATUS line (e.g. 'ACTIONABLE (seit 2026-09-17) — Blocker geloest').
      verlauf: Text of note to append to the VERLAUF/LOG section.
      entry_date: ISO date string (YYYY-MM-DD). Defaults to today's date.
      dry_run: When True, computes changes without modifying the file.
      expected_hash: Optional compare-and-swap hash to protect against concurrent edits.

    Returns:
      dict with details of the operation (path, status_updated, verlauf_appended, etc.).
    """
    ticket_path = Path(path)
    if not ticket_path.is_file():
        raise FileNotFoundError(f"ticket does not exist or is not a file: {ticket_path}")

    if status is None and verlauf is None:
        raise ValueError("at least one of status or verlauf must be provided")

    raw_bytes = ticket_path.read_bytes()
    has_crlf = b"\r\n" in raw_bytes
    eol = "\r\n" if has_crlf else "\n"
    has_bom = raw_bytes.startswith(b"\xef\xbb\xbf")
    decoded_text = raw_bytes.decode("utf-8-sig" if has_bom else "utf-8")

    normalized_text = decoded_text.replace("\r\n", "\n")

    if expected_hash is not None:
        current_hash = content_hash(normalized_text)
        if current_hash != expected_hash:
            raise StaleContentError(ticket_path, normalized_text)

    status_updated = False
    old_status = None
    new_status_val = None

    if status is not None:
        cleaned_val = _STATUS_PREFIX_STRIP_RE.sub("", status).strip()
        match = _STATUS_LINE_RE.search(normalized_text)
        if match is None:
            raise ValueError(f"no STATUS line found in ticket: {ticket_path}")
        old_status = match.group("value").strip()
        # Preserve original line prefix (e.g. 'STATUS:        ')
        line_start = match.start()
        line_end = match.end()
        raw_line = normalized_text[line_start:line_end]
        val_start = match.start("value") - line_start
        prefix = raw_line[:val_start]
        if not prefix.endswith(" "):
            prefix = "STATUS:        "
        new_line = f"{prefix}{cleaned_val}"
        normalized_text = (
            normalized_text[:line_start] + new_line + normalized_text[line_end:]
        )
        status_updated = True
        new_status_val = cleaned_val

    verlauf_appended = False
    formatted_entry = None

    if verlauf is not None:
        d = entry_date if entry_date else date.today().isoformat()
        lines = verlauf.strip().splitlines()
        first_line = lines[0]
        if not re.match(r"^\d{4}-\d{2}-\d{2}\b", first_line):
            first_line = f"{d}  {first_line}"
        formatted_entry = "\n".join([first_line, *lines[1:]])

        # Locate LOESUNG / SOLUTION block
        loesung_match = _LOESUNG_BLOCK_RE.search(normalized_text)
        if loesung_match is not None:
            insert_pos = loesung_match.start()
            before = normalized_text[:insert_pos].rstrip("\n")
            after = normalized_text[insert_pos:].lstrip("\n")
            normalized_text = f"{before}\n{formatted_entry}\n\n{after}"
        else:
            # Fallback: search for closing delimiter (e.g. ====================)
            closing_match = _CLOSING_DELIM_RE.search(normalized_text)
            if closing_match is not None:
                insert_pos = closing_match.start()
                before = normalized_text[:insert_pos].rstrip("\n")
                after = normalized_text[insert_pos:].lstrip("\n")
                normalized_text = f"{before}\n{formatted_entry}\n\n{after}"
            else:
                # Ultimate fallback: append to end of file
                normalized_text = (
                    normalized_text.rstrip("\n") + f"\n{formatted_entry}\n"
                )
        verlauf_appended = True

    # Reconstitute target newline and byte encoding
    final_text = normalized_text.replace("\n", eol)
    final_bytes = (b"\xef\xbb\xbf" if has_bom else b"") + final_text.encode("utf-8")

    if not dry_run:
        dest_dir = ticket_path.parent
        fd, raw_tmp = tempfile.mkstemp(
            prefix=f".{ticket_path.name}.", suffix=".tmp", dir=dest_dir
        )
        tmp_path = Path(raw_tmp)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(final_bytes)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, ticket_path)
        finally:
            if tmp_path.exists():
                tmp_path.unlink()

    return {
        "path": str(ticket_path),
        "status_updated": status_updated,
        "old_status": old_status,
        "new_status": new_status_val,
        "verlauf_appended": verlauf_appended,
        "verlauf_entry": formatted_entry,
        "dry_run": dry_run,
        "newline": "CRLF" if has_crlf else "LF",
        "has_bom": has_bom,
    }


# Convenience alias matching naming pattern
append_ticket_note = update_ticket_note


def _cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ticket_note",
        description="Atomically updates STATUS and/or appends a dated note to VERLAUF/LOG in a ticket file.",
    )
    parser.add_argument("ticket", help="Path to ticket file (.txt)")
    parser.add_argument(
        "--status",
        help="New STATUS value (cluster, subcategory, date, notes)",
    )
    parser.add_argument(
        "--verlauf",
        help="Text to append to VERLAUF / LOG",
    )
    parser.add_argument(
        "--date",
        dest="entry_date",
        default=None,
        help="Date for VERLAUF entry (default: today YYYY-MM-DD)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview changes without writing to file",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="Output result as JSON",
    )

    args = parser.parse_args(argv)

    if not args.status and not args.verlauf:
        parser.error("at least one of --status or --verlauf is required")

    try:
        res = update_ticket_note(
            args.ticket,
            status=args.status,
            verlauf=args.verlauf,
            entry_date=args.entry_date,
            dry_run=args.dry_run,
        )
    except Exception as exc:
        if args.as_json:
            print(json.dumps({"error": str(exc)}, ensure_ascii=False, indent=2))
        else:
            print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    if args.as_json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        label = "WOULD UPDATE" if args.dry_run else "UPDATED"
        print(f"{label}: {res['path']}")
        if res["status_updated"]:
            print(f"  STATUS: {res['old_status']!r} -> {res['new_status']!r}")
        if res["verlauf_appended"]:
            print(f"  VERLAUF: {res['verlauf_entry']}")

    return 0


if __name__ == "__main__":
    raise SystemExit(_cli(sys.argv[1:]))
