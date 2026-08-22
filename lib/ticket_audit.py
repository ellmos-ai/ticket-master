r"""
ticket_audit.py — Health check for the ticket bestand: ID collisions and
structural anomalies discovered while investigating production tickets.

Four structural findings plus schema-v2 validation in one scan:

1. ID collisions: two files under the same logical ticket ID (e.g. a claimed
   and an unclaimed copy, or the same ID reused on different days' worth of
   moves). A collision means the NEXT move into a folder either file already
   occupies will hit ticket_mover's guard and be refused — this audit finds
   the standing collisions before that move is attempted, not after.
2. Claimed tickets sitting in the root/INBOX alias: per the ticket
   conventions the root only ever holds UNCLAIMED tickets
   (T-YYYYMMDD-#########.txt); a claimed file
   (T-YYYYMMDD-#########.<HOST>.txt) there is invisible to every
   status-folder-based triage glob. One such file sat unworked for seven days
   with same-day urgency before this was noticed.
3. Non-ticket files inside the ticket tree's top-level folders (root or any
   lifecycle status folder) — clutter that a naive "everything here is a
   ticket" scan would misclassify.
4. Ticket files below a nested lifecycle subfolder (for example
   ``USER/decision/T-....txt``). Categories v1 stores ``decision`` in STATUS,
   not in the filesystem; nested tickets are invisible to standard readers.

Read-only: this module never writes, moves or deletes anything.
"""

from __future__ import annotations

import re
from pathlib import Path

try:  # package import
    from .ticket_writer import _LIFECYCLE_SUBDIRS, iter_lifecycle_files
    from .routing_contract import RoutingContractError, contract_errors, parse_ticket_name
except ImportError:  # direct import from lib on sys.path
    from ticket_writer import _LIFECYCLE_SUBDIRS, iter_lifecycle_files
    from routing_contract import RoutingContractError, contract_errors, parse_ticket_name

# Status folders only (no "" root entry) -- used for the claimed-in-root and
# non-ticket-file scans below, which treat the root separately from status
# folders on purpose (point 2 of the ticket: the root is an INBOX alias for
# UNCLAIMED tickets only; a claimed file there is itself the anomaly).
_STATUS_SUBDIRS = tuple(sub for sub in _LIFECYCLE_SUBDIRS if sub)

_CLAIMED_RE = re.compile(r"^T-\d{8}-\d+\.[A-Za-z0-9_-]+\.txt$")

# Broader than TICKET_FILENAME_RE on purpose: the production bestand carries
# ~100 tickets from before the bare "T-YYYYMMDD-<number>[.HOST].txt" convention
# that add a descriptive slug after the number (e.g.
# "T-20260614-20_ticket-master-modul-repo.txt"). Those are real, legitimate
# tickets, not clutter -- flagging all of them as "non-ticket" the first
# time this ran against production would have buried the one actual find
# (a stray comic report) in ~100 false positives. This pattern is used ONLY
# to decide "is this plausibly a ticket file" for the clutter scan; ID
# extraction/collision detection stays on the stricter TICKET_FILENAME_RE,
# since a slug is not part of the canonical ID.
_LOOKS_LIKE_TICKET_RE = re.compile(r"^T-\d{8}-\d+(?:_[\w-]+)?(?:\.[A-Za-z0-9_-]+)?\.txt$")


def collect_ids(base: Path) -> dict[str, list[Path]]:
    """Maps every logical ticket ID found anywhere in the lifecycle folders
    to the list of paths currently claiming it. A key with more than one
    path is a live collision: two unrelated ticket files answer to the same
    ID right now, regardless of which folders they happen to sit in."""
    ids: dict[str, list[Path]] = {}
    for path, datestr, number, _suffix in iter_lifecycle_files(base):
        ticket_id = f"T-{datestr}-{number:02d}"
        ids.setdefault(ticket_id, []).append(path)
    return ids


def audit(base: Path | str) -> dict:
    """Runs all structural checks. Returns a JSON-serializable report:

    {
      "collisions": {ticket_id: [path, ...]},   # only entries with len > 1
      "claimed_in_root": [path, ...],
      "non_ticket_files": [path, ...],
      "nested_lifecycle_tickets": [path, ...],  # backwards-compatible list
      "nested_lifecycle_details": [
        {
          "source": path,
          "expected_target": flat_cluster_path,
          "target_collision": bool,
        },
      ],
    }

    ``expected_target`` is derived structurally by removing every path segment
    below the lifecycle cluster. STATUS congruence is a separate migration
    concern: the read-only audit must not silently reinterpret ticket content.
    """
    base = Path(base)

    collisions = {
        ticket_id: sorted(str(p) for p in paths)
        for ticket_id, paths in collect_ids(base).items()
        if len(paths) > 1
    }

    claimed_in_root: list[str] = []
    if base.is_dir():
        for entry in base.iterdir():
            if not entry.is_file():
                continue
            try:
                parsed = parse_ticket_name(entry.name)
                claimed = bool(parsed.claim) if parsed.is_v2 else bool(_CLAIMED_RE.match(entry.name))
            except RoutingContractError:
                claimed = bool(_CLAIMED_RE.match(entry.name))
            if claimed:
                claimed_in_root.append(str(entry))

    non_ticket_files: list[str] = []
    scan_dirs = [base] + [base / sub for sub in _STATUS_SUBDIRS]
    for directory in scan_dirs:
        if not directory.is_dir():
            continue
        for entry in directory.iterdir():
            if not entry.is_file():
                continue
            # Lifecycle folders are kept in the repository by .gitkeep when
            # empty; this structural placeholder is not ticket clutter.
            if entry.name == ".gitkeep":
                continue
            if _LOOKS_LIKE_TICKET_RE.match(entry.name):
                continue
            try:
                parse_ticket_name(entry.name)
                continue
            except RoutingContractError:
                pass
            non_ticket_files.append(str(entry))

    routing_errors: dict[str, list[str]] = {}
    for directory in scan_dirs:
        if not directory.is_dir():
            continue
        for entry in directory.iterdir():
            if not entry.is_file():
                continue
            try:
                parsed = parse_ticket_name(entry.name)
            except RoutingContractError:
                continue
            if parsed.is_v2:
                errors = contract_errors(entry)
                if errors:
                    routing_errors[str(entry)] = errors

    nested_lifecycle_tickets: list[str] = []
    nested_lifecycle_details: list[dict[str, str | bool]] = []
    for subdir in _STATUS_SUBDIRS:
        directory = base / subdir
        if not directory.is_dir():
            continue
        for entry in directory.rglob("*"):
            if not entry.is_file() or len(entry.relative_to(directory).parts) < 2:
                continue
            try:
                parse_ticket_name(entry.name)
            except RoutingContractError:
                continue
            source = str(entry)
            expected_target = directory / entry.name
            nested_lifecycle_tickets.append(source)
            nested_lifecycle_details.append(
                {
                    "source": source,
                    "expected_target": str(expected_target),
                    "target_collision": expected_target.exists(),
                }
            )

    return {
        "collisions": collisions,
        "claimed_in_root": sorted(claimed_in_root),
        "non_ticket_files": sorted(non_ticket_files),
        "routing_errors": dict(sorted(routing_errors.items())),
        "nested_lifecycle_tickets": sorted(nested_lifecycle_tickets),
        "nested_lifecycle_details": sorted(
            nested_lifecycle_details, key=lambda detail: str(detail["source"])
        ),
    }


def _print_human(report: dict) -> None:
    collisions = report["collisions"]
    claimed_in_root = report["claimed_in_root"]
    non_ticket_files = report["non_ticket_files"]
    routing_errors = report.get("routing_errors", {})
    nested_lifecycle_tickets = report.get("nested_lifecycle_tickets", [])
    nested_lifecycle_details = report.get("nested_lifecycle_details", [])

    if collisions:
        print(f"COLLISIONS ({len(collisions)} ID(s) with more than one file):")
        for ticket_id, paths in sorted(collisions.items()):
            print(f"  {ticket_id}:")
            for path in paths:
                print(f"    {path}")
    else:
        print("COLLISIONS: none")

    if claimed_in_root:
        print(f"CLAIMED-IN-ROOT ({len(claimed_in_root)}):")
        for path in claimed_in_root:
            print(f"  {path}")
    else:
        print("CLAIMED-IN-ROOT: none")

    if non_ticket_files:
        print(f"NON-TICKET-FILES ({len(non_ticket_files)}):")
        for path in non_ticket_files:
            print(f"  {path}")
    else:
        print("NON-TICKET-FILES: none")

    if routing_errors:
        print(f"ROUTING-ERRORS ({len(routing_errors)}):")
        for path, errors in routing_errors.items():
            print(f"  {path}:")
            for error in errors:
                print(f"    {error}")
    else:
        print("ROUTING-ERRORS: none")

    if nested_lifecycle_tickets:
        print(f"NESTED-LIFECYCLE-TICKETS ({len(nested_lifecycle_tickets)}):")
        details_by_source = {
            detail["source"]: detail for detail in nested_lifecycle_details
        }
        for path in nested_lifecycle_tickets:
            detail = details_by_source.get(path)
            if detail is None:  # backwards-compatible third-party report
                print(f"  {path}")
                continue
            print(f"  SOURCE: {path}")
            print(f"    EXPECTED-TARGET: {detail['expected_target']}")
            print(f"    TARGET-COLLISION: {str(detail['target_collision']).lower()}")
    else:
        print("NESTED-LIFECYCLE-TICKETS: none")


def _cli(argv: list[str] | None = None) -> int:
    import argparse
    import json
    import os

    parser = argparse.ArgumentParser(
        prog="ticket_audit",
        description="Health check: ID collisions, claimed-in-root and non-ticket files.",
    )
    default_dir = os.environ.get("TICKET_MASTER_TICKETS_DIR")
    parser.add_argument(
        "tickets_dir", nargs="?" if default_dir else None, default=default_dir,
        help="Ticket bestand root (default: $TICKET_MASTER_TICKETS_DIR).",
    )
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args(argv)
    if not args.tickets_dir:
        parser.error("tickets_dir required (pass it or set TICKET_MASTER_TICKETS_DIR).")

    report = audit(args.tickets_dir)
    if args.as_json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        _print_human(report)

    problems = bool(
        report["collisions"] or report["claimed_in_root"]
        or report["non_ticket_files"] or report["routing_errors"]
        or report["nested_lifecycle_tickets"]
    )
    return 1 if problems else 0


if __name__ == "__main__":
    import sys

    raise SystemExit(_cli(sys.argv[1:]))
