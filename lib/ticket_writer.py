r"""
ticket_writer.py — Asynchronous ticket creation for the ticket-master queue.

Lets any tool (e.g. a lock watcher GUI) drop a ticket into the queue even when
no TICKET-MASTER session is running. Writes an unclaimed ticket file
T-YYYYMMDD-NN.txt (no <HOST> suffix) into <tickets_dir>/INBOX/ using the
canonical TICKET format (fields ID/TITLE/STATUS/.../LOG/SOLUTION).

User-neutral module: `tickets_dir` is required (or taken from the
TICKET_MASTER_TICKETS_DIR environment variable / config). The current date is
injectable (today=) for deterministic tests/automation. This is the canonical
home of the helper; the running instance lives in the user's _scripts/ mirror.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path


def _default_tickets_dir() -> Path | None:
    env = os.environ.get("TICKET_MASTER_TICKETS_DIR")
    return Path(env) if env else None

_TICKET_TEMPLATE = """\
==============================================================
TICKET
==============================================================
ID:            {ticket_id}
TITEL:         {title}
ERSTELLT:      {date}
STATUS:        INBOX
PRIORITAET:    {priority}

--------------------------------------------------------------
PROJEKT-ZUORDNUNG
--------------------------------------------------------------
PIPELINE:      {pipeline}
PROJEKTORDNER: {project}
FUEHRUNGSDATEI:<noch offen — beim Triage bestaetigen>

--------------------------------------------------------------
PROBLEMBESCHREIBUNG
--------------------------------------------------------------
{body}

--------------------------------------------------------------
AUFGABENCHARAKTERISTIK  (Bearbeitungskette Schritt 2-3)
--------------------------------------------------------------
TYP:           <Bug / Feature / Doku / Recherche / Review / Refactor>
ANFORDERUNGEN: <Klarheit, Komplexitaet, Kreativitaet, Kontext, Kritikalitaet>
SCORE:         <0-50>

--------------------------------------------------------------
MODELL-ROUTING  (Bearbeitungskette Schritt 4-5)
--------------------------------------------------------------
KANDIDAT 1:    <Modell + Aufrufweg>     [LLM-startbar: ja/nein]
KANDIDAT 2:    <Modell + Aufrufweg>     [LLM-startbar: ja/nein]
KANDIDAT 3:    <Modell + Aufrufweg>     [LLM-startbar: ja/nein]
GEWAEHLT:      <Kandidat + Begruendung>

--------------------------------------------------------------
AUFTRAG / PROMPT
--------------------------------------------------------------
<Konkreter Arbeitsauftrag inkl. Projekt-Routing + zu lesende Root-Doks.>

--------------------------------------------------------------
VERLAUF / LOG
--------------------------------------------------------------
{date}  Aufgenommen (asynchron via Lock-Watcher-GUI / ticket_writer).

--------------------------------------------------------------
LOESUNG / ERGEBNIS
--------------------------------------------------------------
<Vor Verschieben nach SOLVED ausfuellen.>
==============================================================
"""


# Alle Orte, an denen Tickets desselben Tages liegen koennen (Kategorien v1,
# docs/CATEGORIES.*.md: Intake im Root/INBOX, dann ACTIONABLE/QUEUED/BLOCKED/
# WAITING/USER/PARKED/SOLVED). Fuer die Nummernvergabe zaehlen sie ALLE —
# sonst entstehen doppelte IDs, sobald ein Ticket weiterverschoben wurde.
# Rueckwaertskompatibilitaet: PENDING und .USER bleiben als Legacy-Aliase
# (Flachmodell vor v1) in der Liste, damit Alt-Bestaende weiter mitzaehlen;
# PENDING-Eingaenge werden bei der Instanz-Migration auf ACTIONABLE/USER/
# BLOCKED/WAITING/PARKED verteilt, .USER wird durch USER abgeloest.
#
# PFLICHT (T-20260808-03): jede deployte Kopie dieser Datei (Modul-Spiegel UND
# die _scripts/-Laufinstanz) MUSS dieselbe Liste tragen. Eine veraltete Kopie
# ohne die Kategorien-v1-Ordner sieht Tickets in ACTIONABLE/BLOCKED/WAITING/
# USER/PARKED nicht und kann eine bereits vergebene Nummer erneut ausgeben —
# genau das war am 2026-08-08 ein Mitverursacher der ID-Kollision, die zu
# diesem Ticket fuehrte.
_LIFECYCLE_SUBDIRS = ("", "INBOX", "ACTIONABLE", "QUEUED", "BLOCKED", "WAITING",
                      "USER", "PARKED", "SOLVED", "PENDING", ".USER")

# Canonical categories-v1 contract.  Keep this in one machine-readable place so
# a strict parser does not drift away from docs/CATEGORIES.*.md and the ticket
# template.  ``marker`` intentionally belongs to both WAITING and USER:
# WAITING/marker is autonomously observable, whereas USER/marker requires the
# user to provide or confirm that the marker has occurred (T-20260728-12).
LIFECYCLE_SUBCATEGORIES: dict[str, frozenset[str]] = {
    "INBOX": frozenset(),
    "ACTIONABLE": frozenset(),
    "QUEUED": frozenset(),
    "BLOCKED": frozenset(
        {"host-receipt", "foreign-state", "lock", "quota", "dependency"}
    ),
    "WAITING": frozenset({"scheduled", "review-due", "marker"}),
    "USER": frozenset({"decision", "data", "freigabe", "hardware", "session", "marker"}),
    "PARKED": frozenset({"skip", "backlog", "until-trigger"}),
    "SOLVED": frozenset(),
}
_LEGACY_LIFECYCLE_CLUSTERS = frozenset({"OPEN", "PENDING", ".USER"})
_REQUIRES_SUBCATEGORY = frozenset({"BLOCKED", "WAITING", "USER", "PARKED"})
_STATUS_VALUE_RE = re.compile(
    r"^(?P<cluster>\.USER|[A-Z]+)"
    r"(?:/(?P<subcategory>[a-z][a-z0-9-]*))?"
    r"(?:\s+\((?P<label>seit|since)\s+(?P<since>\d{4}-\d{2}-\d{2})\))?$"
)


class LifecycleStatusError(ValueError):
    """Raised when a STATUS value violates the categories-v1 contract."""


@dataclass(frozen=True)
class LifecycleStatus:
    cluster: str
    subcategory: str | None = None
    since: str | None = None


def parse_lifecycle_status(value: str, *, allow_legacy: bool = True) -> LifecycleStatus:
    """Parse and validate a STATUS value from a ticket.

    Accepted date suffixes are bilingual: ``(seit YYYY-MM-DD)`` and
    ``(since YYYY-MM-DD)``.  The language label is presentation-only, so the
    returned value keeps the ISO date but not the label.
    """

    match = _STATUS_VALUE_RE.fullmatch(value.strip())
    if match is None:
        raise LifecycleStatusError(f"invalid STATUS syntax: {value!r}")
    cluster = match.group("cluster")
    subcategory = match.group("subcategory")
    since = match.group("since")
    if since is not None:
        try:
            date.fromisoformat(since)
        except ValueError as exc:
            raise LifecycleStatusError(f"invalid STATUS date: {since!r}") from exc

    if cluster in _LEGACY_LIFECYCLE_CLUSTERS:
        if not allow_legacy:
            raise LifecycleStatusError(f"legacy STATUS is read-only: {cluster}")
        if subcategory is not None:
            raise LifecycleStatusError(
                f"legacy STATUS does not accept a subcategory: {cluster}/{subcategory}"
            )
        return LifecycleStatus(cluster=cluster, since=since)

    allowed = LIFECYCLE_SUBCATEGORIES.get(cluster)
    if allowed is None:
        raise LifecycleStatusError(f"unknown lifecycle cluster: {cluster}")
    if cluster in _REQUIRES_SUBCATEGORY and subcategory is None:
        raise LifecycleStatusError(f"STATUS {cluster} requires a subcategory")
    if cluster not in _REQUIRES_SUBCATEGORY and subcategory is not None:
        raise LifecycleStatusError(f"STATUS {cluster} does not accept a subcategory")
    if subcategory is not None and subcategory not in allowed:
        raise LifecycleStatusError(
            f"unknown subcategory for {cluster}: {subcategory}"
        )
    return LifecycleStatus(cluster=cluster, subcategory=subcategory, since=since)


def format_lifecycle_status(status: LifecycleStatus, *, language: str = "en") -> str:
    """Render a parsed STATUS value without losing its canonical meaning."""

    # Revalidate constructed dataclass instances before rendering them.
    base = status.cluster
    if status.subcategory is not None:
        base += f"/{status.subcategory}"
    label = "seit" if language == "de" else "since"
    rendered = f"{base} ({label} {status.since})" if status.since else base
    parse_lifecycle_status(rendered)
    return rendered


def validate_lifecycle_status(
    value: str,
    *,
    folder: str | None = None,
    allow_legacy: bool = True,
) -> LifecycleStatus:
    """Validate a STATUS value and optionally its lifecycle-folder mirror."""

    status = parse_lifecycle_status(value, allow_legacy=allow_legacy)
    if folder is None:
        return status
    folder_name = str(folder).strip("/\\") or "INBOX"
    expected = {
        "OPEN": "INBOX",
        "PENDING": "PENDING",
        ".USER": ".USER",
    }.get(status.cluster, status.cluster)
    if folder_name != expected:
        raise LifecycleStatusError(
            f"STATUS {status.cluster} is not congruent with folder {folder_name}"
        )
    return status

# Ticket-Dateiname: T-<8-stelliges Datum>-<laufende Nummer>[.<HOST-oder-Suffix>].txt
# Eine Gruppe fuer Datum, eine fuer die Nummer, eine optionale fuer den Claim-/
# Konflikt-Suffix. Von _next_number UND vom Audit (lib/ticket_audit.py) genutzt,
# damit beide garantiert dieselbe Grammatik sehen und nicht auseinanderlaufen.
TICKET_FILENAME_RE = re.compile(r"^T-(\d{8})-(\d+)(?:\.([A-Za-z0-9_-]+))?\.txt$")


def iter_lifecycle_files(base: Path):
    """Iteriert alle ticketfoermigen Dateien in JEDEM Lebenszyklus-Ordner.

    Liefert (pfad, datestr, nummer, suffix_oder_None) je Treffer. Suffix ist
    der Host-/Konfliktanteil vor ".txt" (z. B. "WORKSTATION-LG" bei
    "T-20260808-03.WORKSTATION-LG.txt"), None bei unclaimed Tickets.
    """
    for sub in _LIFECYCLE_SUBDIRS:
        directory = base / sub if sub else base
        if not directory.is_dir():
            continue
        for entry in directory.iterdir():
            if not entry.is_file():
                continue
            m = TICKET_FILENAME_RE.match(entry.name)
            if m:
                datestr, number, suffix = m.group(1), int(m.group(2)), m.group(3)
                yield entry, datestr, number, suffix


def _next_number(base: Path, datestr: str) -> int:
    """Naechste freie laufende Nummer fuer ein Datum (beruecksichtigt claimed
    Tickets T-DATE-NN.<HOST>.txt und unclaimed T-DATE-NN.txt ueber alle
    Lebenszyklus-Ordner)."""
    highest = 0
    for _path, entry_date, number, _suffix in iter_lifecycle_files(base):
        if entry_date == datestr:
            highest = max(highest, number)
    return highest + 1


def create(title: str, body: str, project: str | None = None, priority: str = "mittel",
           pipeline: str = "<offen>", tickets_dir: Path | None = None,
           today: str | None = None) -> str:
    """Erzeugt ein unclaimed Ticket in <tickets_dir>/INBOX/. Returns den Pfad.

    tickets_dir ist erforderlich (oder via TICKET_MASTER_TICKETS_DIR gesetzt)."""
    base = Path(tickets_dir) if tickets_dir else _default_tickets_dir()
    if base is None:
        raise ValueError(
            "tickets_dir required (pass it or set TICKET_MASTER_TICKETS_DIR).")
    inbox = base / "INBOX"
    inbox.mkdir(parents=True, exist_ok=True)

    date_iso = today or datetime.now().strftime("%Y-%m-%d")
    datestr = date_iso.replace("-", "")
    number = _next_number(base, datestr)
    while True:
        ticket_id = f"T-{datestr}-{number:02d}"
        target = inbox / f"{ticket_id}.txt"
        content = _TICKET_TEMPLATE.format(
            ticket_id=ticket_id, title=title.strip() or "<ohne Titel>",
            date=date_iso, priority=priority, pipeline=pipeline,
            project=project or "<offen>",
            body=body.strip() or "<keine Beschreibung>",
        )
        try:
            # Exklusiv anlegen ("x"): schreibt NIE ueber ein bestehendes Ticket.
            # Vergibt ein paralleler Erzeuger (anderes System, Cloud-Sync)
            # dieselbe Nummer, kollidiert der zweite hier und zaehlt hoch.
            with target.open("x", encoding="utf-8") as fh:
                fh.write(content)
        except FileExistsError:
            number += 1
            continue
        return str(target)


def _cli(argv: list[str] | None = None) -> int:
    """CLI so any agent gets a collision-free ID in one shell call instead of
    picking the next number by eye (T-20260808-03: exactly that manual
    picking, bypassing this module's atomic exclusive-create, produced a
    same-minute collision between two agents on one host)."""
    import argparse

    parser = argparse.ArgumentParser(
        prog="ticket_writer",
        description="Atomically create an unclaimed ticket (T-YYYYMMDD-NN.txt in INBOX/).",
    )
    parser.add_argument("--title", required=True)
    parser.add_argument("--body", default="")
    parser.add_argument("--project", default=None)
    parser.add_argument("--priority", default="mittel")
    parser.add_argument("--pipeline", default="<offen>")
    parser.add_argument("--tickets-dir", default=None)
    args = parser.parse_args(argv)
    try:
        path = create(
            args.title, args.body, project=args.project, priority=args.priority,
            pipeline=args.pipeline,
            tickets_dir=Path(args.tickets_dir) if args.tickets_dir else None,
        )
    except ValueError as exc:
        print(f"ERROR: {exc}")
        return 1
    print(path)
    return 0


if __name__ == "__main__":
    import sys

    raise SystemExit(_cli(sys.argv[1:]))
