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
    from .ticket_writer import (
        _LIFECYCLE_SUBDIRS, LifecycleStatusError, iter_lifecycle_files,
        parse_lifecycle_status,
    )
    from .routing_contract import RoutingContractError, contract_errors, parse_ticket_name
except ImportError:  # direct import from lib on sys.path
    from ticket_writer import (
        _LIFECYCLE_SUBDIRS, LifecycleStatusError, iter_lifecycle_files,
        parse_lifecycle_status,
    )
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


_TITLE_LINE_RE = re.compile(r"^(?:TITEL|TITLE):[ \t]*(?P<value>.*)$", re.MULTILINE)


def _read_title(path: Path) -> str | None:
    try:
        head = path.read_text(encoding="utf-8", errors="replace")[:4096]
    except OSError:
        return None
    match = _TITLE_LINE_RE.search(head)
    return match.group("value").strip() if match else None


def classify_collision(paths: list[Path]) -> str:
    """T-20260902-379329038: a raw COLLISIONS dump doesn't say whether an ID
    is shared by two copies of the SAME ticket (each host claimed its own
    copy of one submission -- harmless, at most needs merging) or by two
    UNRELATED tickets (a real collision -- the 9-digit random ID was meant to
    make this practically impossible, but a plain file *copy* never draws a
    new ID, so no collision guard ever runs). Distinguishing them is the
    triage this ticket's own investigation had to do by hand.

    "host-duplicate": every file resolves to the exact same title value
    (including the case where none of them has a readable title at all).
    "id-collision": at least two files disagree -- including one file
    having a title and another not, which is treated as disagreement
    rather than silently assumed to be the same ticket.
    """
    titles = {_read_title(p) for p in paths}
    if len(titles) <= 1:
        return "host-duplicate"
    return "id-collision"


# "STATUS:" (Kategorien v1) plus Legacy-Markdown-Feld "**Status:**" (Tickets vom
# 2026-08-01/02; T-20260901-916096823: sonst Dauer-Fehlalarm missing-status trotz
# korrektem, ordnerkongruentem Status).
_STATUS_LINE_RE = re.compile(
    r"^(?:STATUS:|\*\*Status:\*\*)[ \t]*(?P<value>.*)$",
    re.MULTILINE,
)
_STATUS_CLUSTER_RE = re.compile(r"^(\.USER|[A-Z]+)")
_ROOT_ALIAS = "INBOX"
# Legacy folders (PENDING/.USER) are read-only; a STATUS naming them is not
# drift while the file still sits in that legacy folder.
_KNOWN_STATUS_CLUSTERS = frozenset(_STATUS_SUBDIRS) | {_ROOT_ALIAS, "PENDING", ".USER"}
_LEGACY_STATUS_ALIASES = {"OPEN": _ROOT_ALIAS}


def status_drift(base: Path | str) -> list[dict[str, str | None]]:
    """STATUS field vs. lifecycle folder (T-20260830-517795746, Befund 3).

    Reports, never repairs: ``folder-mismatch`` (STATUS cluster names another
    lifecycle folder), ``unknown-status`` (first token is no cluster at all,
    e.g. ``GELOEST`` or ``/REVIEW``), ``missing-status`` (no STATUS line in
    the first 4 KB), ``legacy-header`` (STATUS read from the legacy
    ``**Status:**`` markdown field and folder-congruent -- accepted as a
    valid field so it isn't also flagged folder-mismatch/missing-status, but
    still surfaced so it doesn't silently disappear; T-20260902-792359826).
    Only the leading cluster token is compared; the subcategory and free text
    after it are presentation. A file in the root counts as INBOX; ``OPEN``
    is the documented legacy alias for it.
    """
    base = Path(base)
    findings: list[dict[str, str | None]] = []
    for sub in _LIFECYCLE_SUBDIRS:
        directory = base / sub if sub else base
        if not directory.is_dir():
            continue
        folder_cluster = sub or _ROOT_ALIAS
        for entry in directory.iterdir():
            if not entry.is_file():
                continue
            if not _LOOKS_LIKE_TICKET_RE.match(entry.name):
                try:
                    parse_ticket_name(entry.name)
                except RoutingContractError:
                    continue
            try:
                head = entry.read_text(encoding="utf-8", errors="replace")[:4096]
            except OSError:
                continue
            match = _STATUS_LINE_RE.search(head)
            if match is None:
                findings.append({"path": str(entry), "folder": folder_cluster,
                                 "status": None, "kind": "missing-status"})
                continue
            value = match.group("value").strip()
            token = _STATUS_CLUSTER_RE.match(value)
            cluster = _LEGACY_STATUS_ALIASES.get(token.group(1), token.group(1)) if token else None
            # T-20260902-792359826: the legacy '**Status:**' branch (accepted
            # since T-20260901-916096823 to stop false folder-mismatch/
            # missing-status alarms) must not fall completely silent even when
            # folder-congruent -- that is exactly the fail-silent blind spot
            # this ticket found. It gets its own kind instead of a second,
            # tacitly-equal STATUS format.
            is_legacy_header = match.group(0).lstrip().startswith("**")
            if cluster not in _KNOWN_STATUS_CLUSTERS:
                kind = "unknown-status"
            elif cluster != folder_cluster:
                kind = "folder-mismatch"
            elif is_legacy_header:
                kind = "legacy-header"
            else:
                continue
            findings.append({"path": str(entry), "folder": folder_cluster,
                             "status": value[:80], "kind": kind})
    return sorted(findings, key=lambda f: str(f["path"]))


# --- Fortschritts-Drift (T-20260904-141396683, Loesungsweg C) --------------
# Fehlerklasse: ein Stand war einmal wahr und wird spaeter als aktuell gelesen.
# Ein mehrwelliges Ticket mit fertiger Welle 1 sieht in ACTIONABLE aus wie eines,
# an dem nie jemand gearbeitet hat -- der Erledigungsstand steht im VERLAUF bzw.
# in DELEGIERT_AN, und genau die liest der Lean-Router aus Kontext-Oekonomie
# nicht. Beide Pruefungen melden nur; repariert wird nichts.
_OPEN_LIFECYCLE_SUBDIRS = ("", "INBOX", "ACTIONABLE")
_GATE4_RE = re.compile(
    r"GATE[ \t-]?4[^\n]{0,60}ABGENOMMEN|ABGENOMMEN[^\n]{0,60}GATE[ \t-]?4",
    re.IGNORECASE,
)
# "DELEGIERT_AN: ... Welle 1 fertig, Wellen 2-4 offen" -- der Vermerk steht
# hinter dem Empfaenger auf derselben Zeile.
_DELEGIERT_DONE_RE = re.compile(
    r"^DELEGIERT_AN:.*?\b(fertig|erledigt|abgeschlossen|abgenommen|done)\b",
    re.IGNORECASE | re.MULTILINE,
)
_VERLAUF_RE = re.compile(r"^VERLAUF", re.MULTILINE)
_TICKET_ID_RE = re.compile(r"T-\d{8}-\d+")
# git grep spricht POSIX-ERE, dort gibt es kein \d -- der Python-Pattern
# oben ist als git-Argument still wirkungslos (rc 1, keine Treffer).
_TICKET_ID_GREP = "T-[0-9]{8}-[0-9]+"


def _open_ticket_files(base: Path) -> list[Path]:
    """Ticket files sitting in a folder that still invites a dispatch."""
    files: list[Path] = []
    for sub in _OPEN_LIFECYCLE_SUBDIRS:
        directory = base / sub if sub else base
        if not directory.is_dir():
            continue
        files.extend(
            entry for entry in directory.iterdir()
            if entry.is_file() and _LOOKS_LIKE_TICKET_RE.match(entry.name)
        )
    return sorted(files)


def progress_drift(base: Path | str) -> list[dict[str, str | None]]:
    """Open tickets that already carry a completion mark in their own body.

    ``gate4-accepted`` -- the VERLAUF records a GATE-4 acceptance.
    ``delegated-done`` -- DELEGIERT_AN names a finished portion.

    Either one means the ticket is at least partly done, so dispatching it as
    fresh work pays for that portion twice.  Read in full, not just the head:
    the mark lives at the bottom of the file, which is exactly why the head-only
    reader missed it.
    """
    findings: list[dict[str, str | None]] = []
    for entry in _open_ticket_files(Path(base)):
        try:
            text = entry.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        # Nur ab dem VERLAUF suchen. Ein Ticket, das eine fremde Abnahme in
        # seiner PROBLEMBESCHREIBUNG zitiert, ist nicht selbst abgenommen --
        # ohne diese Grenze war der erste Treffer am echten Bestand prompt ein
        # Fehlalarm, und ein Audit, dessen Treffer man wegklickt, ist keins.
        # DELEGIERT_AN steht am Dateiende, liegt also ebenfalls dahinter.
        marker = _VERLAUF_RE.search(text)
        verlauf = text[marker.start():] if marker else ""
        # DELEGIERT_AN braucht die Eingrenzung nicht: es ist ein zeilenverankertes
        # Kopffeld am Dateiende, kein Fliesstext -- ein Zitat davon steht
        # eingerueckt und trifft `^DELEGIERT_AN:` gar nicht.
        for kind, match in (("gate4-accepted", _GATE4_RE.search(verlauf)),
                            ("delegated-done", _DELEGIERT_DONE_RE.search(text))):
            if match is not None:
                findings.append({"path": str(entry), "kind": kind,
                                 "evidence": match.group(0).strip()[:120]})
    return sorted(findings, key=lambda f: (str(f["path"]), str(f["kind"])))


def _git_grep_ticket_ids(repo: Path, timeout: float) -> dict[str, str]:
    """Ticket IDs mentioned in one repository's tracked files -> first location."""
    import subprocess
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), "grep", "-I", "-n", "-E", _TICKET_ID_GREP],
            capture_output=True, text=True, errors="replace", timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return {}
    hits: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        location, _, content = line.partition(":")
        lineno, _, content = content.partition(":")
        for found in _TICKET_ID_RE.findall(content):
            hits.setdefault(found, f"{repo.name}/{location}:{lineno}")
    return hits


def source_reference_drift(
    base: Path | str,
    roots: list[Path] | None = None,
    *,
    timeout: float = 20.0,
) -> list[dict[str, str | None]]:
    """Open ticket IDs that already appear in a work tree's source.

    T-20260903-370384774 was dispatched as fresh work while its first task was
    long done -- documented nowhere in the ticket, only in a code comment that
    quoted the ticket ID.  Reading the VERLAUF would not have caught it; only
    the source does.  A hit is a strong signal that work happened, not proof,
    so this reports and never blocks.

    ``roots`` defaults to the sibling directory of this clone, which is the
    Plan-D repository root on every host (``<...>/repos/<name>``); nothing is
    hardcoded.  Repositories are read through ``git grep``, so ignored files
    and history are skipped for free.  Paths inside the ticket base are not
    counted -- a ticket quoting its own ID is not evidence of anything.
    """
    base = Path(base).resolve()
    if roots is None:
        roots = [Path(__file__).resolve().parents[2]]
    open_ids: dict[str, Path] = {}
    for entry in _open_ticket_files(base):
        match = _TICKET_ID_RE.match(entry.name)
        if match:
            open_ids.setdefault(match.group(0), entry)
    if not open_ids:
        return []
    findings: list[dict[str, str | None]] = []
    seen: set[str] = set()
    for root in roots:
        root = Path(root)
        if not root.is_dir():
            continue
        for repo in sorted(root.iterdir()):
            if not (repo / ".git").exists():
                continue
            if base == repo or base.is_relative_to(repo):
                continue
            for found, location in _git_grep_ticket_ids(repo, timeout).items():
                if found in open_ids and found not in seen:
                    seen.add(found)
                    findings.append({"path": str(open_ids[found]), "kind": "source-reference",
                                     "ticket": found, "location": location})
    return sorted(findings, key=lambda f: str(f["ticket"]))


def audit(base: Path | str, *, scan_sources: bool = False,
          source_roots: list[Path] | None = None) -> dict:
    """Runs all structural checks. Returns a JSON-serializable report:

    {
      "collisions": {ticket_id: [path, ...]},   # only entries with len > 1
      "claimed_in_root": [path, ...],
      "non_ticket_files": [path, ...],
      "informal_entries": [path, ...],  # INBOX/ files without "T-" prefix
                                         # (Entscheid 3A) -- not clutter
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

    colliding_ids = {
        ticket_id: paths for ticket_id, paths in collect_ids(base).items() if len(paths) > 1
    }
    collisions = {
        ticket_id: sorted(str(p) for p in paths) for ticket_id, paths in colliding_ids.items()
    }
    collision_kinds = {
        ticket_id: classify_collision(paths) for ticket_id, paths in colliding_ids.items()
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

    # Nutzerentscheid 3A (T-20260830-145228426): a file in INBOX/ without the
    # "T-" ticket prefix is a formless entry awaiting formalization via
    # ticket_writer.formalize_informal_entry(), not clutter -- it must not be
    # reported under non_ticket_files.
    informal_entries: list[str] = []
    inbox_dir = base / "INBOX"
    if inbox_dir.is_dir():
        for entry in inbox_dir.iterdir():
            if entry.is_file() and entry.name != ".gitkeep" and not entry.name.startswith("T-"):
                informal_entries.append(str(entry))
    informal_paths = set(informal_entries)

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
            if str(entry) in informal_paths:
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
                # T-20260902-792359826 Nebenbefund: contract_errors() crashed
                # the whole audit on one malformed v2 ticket (a shape
                # load_contract() didn't validate). A single broken file must
                # become a finding, not take down the run for every other
                # ticket -- defense in depth alongside the load_contract()
                # shape check itself.
                try:
                    errors = contract_errors(entry)
                except Exception as exc:  # noqa: BLE001 -- reported, never repaired
                    errors = [f"contract_errors crashed: {exc!r}"]
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
        "collision_kinds": collision_kinds,
        "claimed_in_root": sorted(claimed_in_root),
        "non_ticket_files": sorted(non_ticket_files),
        "informal_entries": sorted(informal_entries),
        "routing_errors": dict(sorted(routing_errors.items())),
        "nested_lifecycle_tickets": sorted(nested_lifecycle_tickets),
        "nested_lifecycle_details": sorted(
            nested_lifecycle_details, key=lambda detail: str(detail["source"])
        ),
        "status_drift": status_drift(base),
        "progress_drift": progress_drift(base),
        # Opt-in: ein git grep je Repo ueber alle Arbeitsbaeume kostet
        # Sekunden, und ein Audit, das niemand mehr startet, faengt nichts.
        "source_reference_drift": (
            source_reference_drift(base, roots=source_roots) if scan_sources else []
        ),
    }


# Required-field lint (Entscheid 3A, T-20260830-145228426 EMPFEHLUNG A): each
# canonical field name maps to its accepted aliases (DE/EN template forms).
_REQUIRED_FIELDS: dict[str, tuple[str, ...]] = {
    "ID": ("ID",),
    "TITLE": ("TITEL", "TITLE"),
    "CREATED": ("ERSTELLT", "CREATED"),
    "STATUS": ("STATUS",),
}
_FIELD_LINE_RE_CACHE: dict[str, re.Pattern] = {
    alias: re.compile(rf"^{re.escape(alias)}:", re.MULTILINE)
    for aliases in _REQUIRED_FIELDS.values() for alias in aliases
}

# Duplicate-block headings: the template's own section markers. A heading
# appearing twice means the template was pasted in twice (T-20260830-145228426
# measured 3 live cases -- PROJEKT-ZUORDNUNG 2x, PROBLEMBESCHREIBUNG 2x, one
# LOESUNG 2x).
_DUPLICATE_HEADING_PATTERNS: dict[str, re.Pattern] = {
    "PROJEKT-ZUORDNUNG": re.compile(r"^PROJEKT-ZUORDNUNG\s*$", re.MULTILINE),
    "PROBLEMBESCHREIBUNG": re.compile(r"^PROBLEMBESCHREIBUNG\s*$", re.MULTILINE),
    "LOESUNG": re.compile(r"^LOESUNG\b.*$", re.MULTILINE),
}


def _lint_ticket(entry: Path, text: str) -> list[dict[str, str | int]]:
    findings: list[dict[str, str | int]] = []
    for field, aliases in _REQUIRED_FIELDS.items():
        if not any(_FIELD_LINE_RE_CACHE[alias].search(text) for alias in aliases):
            findings.append({"path": str(entry), "kind": "missing-field", "field": field})

    status_match = _STATUS_LINE_RE.search(text[:4096])
    if status_match:
        try:
            parse_lifecycle_status(status_match.group("value").strip())
        except LifecycleStatusError as exc:
            findings.append({"path": str(entry), "kind": "invalid-status", "detail": str(exc)})

    for heading, pattern in _DUPLICATE_HEADING_PATTERNS.items():
        count = len(pattern.findall(text))
        if count > 1:
            findings.append({
                "path": str(entry), "kind": "duplicate-block",
                "heading": heading, "count": count,
            })
    return findings


def lint(base: Path | str) -> list[dict[str, str | int]]:
    """Ticket-content lint beyond audit()'s structural checks: required
    fields (ID/TITEL|TITLE/ERSTELLT|CREATED/STATUS), STATUS vocabulary (via
    parse_lifecycle_status), and duplicate section headings (a pasted-twice
    template). Reports only, never repairs -- same doctrine as audit()."""
    base = Path(base)
    findings: list[dict[str, str | int]] = []
    for sub in _LIFECYCLE_SUBDIRS:
        directory = base / sub if sub else base
        if not directory.is_dir():
            continue
        for entry in directory.iterdir():
            if not entry.is_file():
                continue
            if not _LOOKS_LIKE_TICKET_RE.match(entry.name):
                try:
                    parse_ticket_name(entry.name)
                except RoutingContractError:
                    continue
            try:
                text = entry.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            findings.extend(_lint_ticket(entry, text))
    return sorted(findings, key=lambda f: (str(f["path"]), str(f["kind"])))


def _print_lint_human(findings: list[dict[str, str | int]]) -> None:
    if not findings:
        print("LINT: none")
        return
    print(f"LINT ({len(findings)} finding(s)):")
    for finding in findings:
        detail = {k: v for k, v in finding.items() if k not in ("path", "kind")}
        print(f"  {finding['path']}")
        print(f"    KIND: {finding['kind']}  {detail}")


def _print_human(report: dict) -> None:
    collisions = report["collisions"]
    claimed_in_root = report["claimed_in_root"]
    non_ticket_files = report["non_ticket_files"]
    routing_errors = report.get("routing_errors", {})
    nested_lifecycle_tickets = report.get("nested_lifecycle_tickets", [])
    nested_lifecycle_details = report.get("nested_lifecycle_details", [])

    collision_kinds = report.get("collision_kinds", {})
    if collisions:
        print(f"COLLISIONS ({len(collisions)} ID(s) with more than one file):")
        for ticket_id, paths in sorted(collisions.items()):
            kind = collision_kinds.get(ticket_id, "unknown")
            print(f"  {ticket_id} [{kind}]:")
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

    informal_entries = report.get("informal_entries", [])
    if informal_entries:
        print(f"INFORMAL-ENTRIES ({len(informal_entries)}):")
        for path in informal_entries:
            print(f"  {path}")
    else:
        print("INFORMAL-ENTRIES: none")

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
    status_drift_findings = report.get("status_drift", [])
    if status_drift_findings:
        print(f"STATUS-DRIFT ({len(status_drift_findings)}):")
        for finding in status_drift_findings:
            print(f"  {finding['path']}")
            print(f"    KIND: {finding['kind']}  FOLDER: {finding['folder']}  STATUS: {finding['status']}")
    else:
        print("STATUS-DRIFT: none")
    progress_findings = report.get("progress_drift", [])
    if progress_findings:
        print(f"PROGRESS-DRIFT ({len(progress_findings)}):")
        for finding in progress_findings:
            print(f"  {finding['path']}")
            print(f"    KIND: {finding['kind']}  EVIDENCE: {finding['evidence']}")
    else:
        print("PROGRESS-DRIFT: none")
    source_findings = report.get("source_reference_drift", [])
    if source_findings:
        print(f"SOURCE-REFERENCE-DRIFT ({len(source_findings)}):")
        for finding in source_findings:
            print(f"  {finding['path']}")
            print(f"    TICKET: {finding['ticket']}  SEEN-AT: {finding['location']}")
    else:
        print("SOURCE-REFERENCE-DRIFT: none")


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
    parser.add_argument("--no-source-scan", action="store_false", dest="scan_sources",
                        help="skip the per-repo git grep for open ticket ids")
    parser.add_argument("--lint", action="store_true",
                         help="required fields, STATUS vocabulary, duplicate blocks (reports only)")
    args = parser.parse_args(argv)
    if not args.tickets_dir:
        parser.error("tickets_dir required (pass it or set TICKET_MASTER_TICKETS_DIR).")

    if args.lint:
        findings = lint(args.tickets_dir)
        if args.as_json:
            print(json.dumps(findings, ensure_ascii=False, indent=2))
        else:
            _print_lint_human(findings)
        return 1 if findings else 0

    report = audit(args.tickets_dir, scan_sources=args.scan_sources)
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
