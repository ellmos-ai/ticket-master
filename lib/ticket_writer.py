r"""
ticket_writer.py — Asynchronous ticket creation for the ticket-master queue.

Lets any tool (e.g. a lock watcher GUI) drop a ticket into the queue even when
no TICKET-MASTER session is running. Draws the 9-digit random component and
writes an unclaimed ticket file T-YYYYMMDD-#########.txt (no <HOST> suffix)
into <tickets_dir>/INBOX/ using the canonical TICKET format
(fields ID/TITLE/STATUS/.../LOG/SOLUTION). New IDs must only be minted through
this helper; callers must never count or choose the numeric component manually.

User-neutral module: `tickets_dir` is required (or taken from the
TICKET_MASTER_TICKETS_DIR environment variable / config). The current date is
injectable (today=) for deterministic tests/automation. This is the canonical
home of the helper; the running instance lives in the user's _scripts/ mirror.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

try:  # package import (``from lib import ticket_writer``)
    from .routing_contract import (
        canonical_contract_name,
        contract_metadata,
        normalize_alias,
        parse_ticket_name,
        resolve_targets,
        update_fields,
    )
except ImportError:  # direct script/module import from ``lib`` on sys.path
    from routing_contract import (
        canonical_contract_name,
        contract_metadata,
        normalize_alias,
        parse_ticket_name,
        resolve_targets,
        update_fields,
    )


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
FUEHRUNGSDATEI:<noch offen — bei der Triage bestätigen>

--------------------------------------------------------------
PROBLEMBESCHREIBUNG
--------------------------------------------------------------
{body}

--------------------------------------------------------------
AUFGABENCHARAKTERISTIK  (Bearbeitungskette Schritt 2-3)
--------------------------------------------------------------
TYP:           <Bug / Feature / Doku / Recherche / Review / Refactor>
ANFORDERUNGEN: <Klarheit, Komplexität, Kreativität, Kontext, Kritikalität>
SCORE:         <0-50>

--------------------------------------------------------------
MODELL-ROUTING  (Bearbeitungskette Schritt 4-5)
--------------------------------------------------------------
KANDIDAT 1:    <Modell + Aufrufweg>     [LLM-startbar: ja/nein]
KANDIDAT 2:    <Modell + Aufrufweg>     [LLM-startbar: ja/nein]
KANDIDAT 3:    <Modell + Aufrufweg>     [LLM-startbar: ja/nein]
GEWAEHLT:      <Kandidat + Begründung>

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
<Vor Verschieben nach SOLVED ausfüllen.>
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

# Ticket-Dateiname:
#   T-<8-stelliges Datum>-<numerische ID>[_<slug>][.<HOST-oder-Suffix>].txt
# Gruppen: date, number, slug (optional, beschreibend), suffix (optional, Claim).
# Von der Nummernvergabe UND vom Audit (lib/ticket_audit.py) genutzt, damit
# beide garantiert dieselbe Grammatik sehen und nicht auseinanderlaufen.
#
# Die slug-Gruppe kam am 2026-08-15 dazu (Messung am Live-Bestand: 277
# Ticketdateien, davon 110 nicht erkannt, 101 belegte Datum/Nummer-Paare
# unsichtbar). Der Bestand traegt neben "T-DATE-NN[.HOST].txt" verbreitet die
# Form "T-DATE-NN_beschreibung.txt" mit UNTERSTRICH -- fuer das alte Muster,
# das an dieser Stelle einen Punkt verlangte, existierten diese Tickets nicht.
# Die Vergabe konnte deren Nummern daher ein zweites Mal ausgeben, und
# ticket_audit.collect_ids sah eine Kollision zwischen einer Slug- und einer
# Nicht-Slug-Fassung derselben Nummer nicht. Das ist derselbe Defekt wie in
# T-20260808-03, nur eine Ebene tiefer: dort vergaben zwei Agenten dieselbe
# Nummer, hier war eine bereits vergebene Nummer schlicht unsichtbar.
#
# WICHTIG, und der Grund fuer die getrennte Gruppe: Der Slug belegt die
# Nummer, ist aber NICHT Teil der kanonischen ID. Die ID bleibt "T-DATE-NN" --
# sonst waeren "T-20260612-01_alpha.txt" und "T-20260612-01.txt" zwei
# verschiedene Vorgaenge statt einer Kollision.
#
# Die Erweiterung ist bewusst eng: Datumsgruppe und numerische ID-Komponente
# bleiben Pflicht, die Endung bleibt .txt. Damit gelten die Altlasten unter PENDING/
# (T-41_LOESCH-REPORT.txt, T-41_cleanup.ps1, T-41_gnomad_transfer.sh)
# weiterhin NICHT als Tickets -- ein Muster, das Reports und Shell-Skripte
# mitzaehlt, waere schlechter als eines, das ein paar Tickets uebersieht.
TICKET_FILENAME_RE = re.compile(
    r"^T-(?P<date>\d{8})-(?P<number>\d+)"
    r"(?:_(?P<slug>[A-Za-z0-9][\w-]*))?"
    r"(?:\.(?P<suffix>[A-Za-z0-9_-]+))?"
    r"\.txt$"
)


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


def iter_lifecycle_files(base: Path):
    """Iteriert alle ticketfoermigen Dateien in JEDEM Lebenszyklus-Ordner.

    Liefert (pfad, datestr, nummer, suffix_oder_None) je Treffer. Suffix ist
    der Host-/Konfliktanteil vor ".txt" (z. B. "WORKSTATION-LG" bei
    "T-20260808-03.WORKSTATION-LG.txt"), None bei unclaimed Tickets.

    Ein beschreibender Slug ("T-DATE-NN_thema.txt") wird erkannt, aber NICHT
    mitgeliefert: er gehoert nicht zur ID. Fuer die Nummernvergabe zaehlt die
    Datei trotzdem voll mit -- die Nummer ist vergeben, egal wie die Datei
    darueber hinaus benannt ist.
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
                yield (entry, m.group("date"), int(m.group("number")),
                       m.group("suffix"))
                continue
            # Schema v2 may carry three orthogonal suffix axes and exact
            # selectors containing dots.  Its parser is deliberately kept
            # separate from the legacy regex so ``T-ID.<HOST>`` never changes
            # meaning for old callers.
            try:
                parsed = parse_ticket_name(entry.name)
            except ValueError:
                continue
            if parsed.is_v2:
                yield (entry, parsed.date, int(parsed.number), parsed.claim)


# Stellenzahl der Zufallsnummer (Nutzerentscheid 2026-08-15).
ID_DIGITS = 9
_ID_MIN = 10 ** (ID_DIGITS - 1)
_ID_MAX = 10 ** ID_DIGITS - 1

# Reine Schleifenbremse: bei 9 Stellen ist schon der erste Wurf praktisch
# immer frei.
_MAX_DRAWS = 100


def used_numbers(base: Path, datestr: str) -> set[int]:
    """Alle an diesem Datum bereits vergebenen Nummern, ueber alle
    Lebenszyklus-Ordner hinweg (claimed wie unclaimed)."""
    return {number for _path, entry_date, number, _suffix
            in iter_lifecycle_files(base) if entry_date == datestr}


def draw_number(used: set[int], rng=None) -> int:
    """Zieht eine freie 9-stellige Zufallsnummer.

    WARUM ZUFALL STATT HOCHZAEHLEN (Nutzerentscheid 2026-08-15):
    Die Queue liegt in einem cloud-synchronisierten Ordner, den mehrere Hosts
    gleichzeitig benutzen. `os.O_EXCL` beim Anlegen wirkt aber nur LOKAL, und
    ein Abgleich gegen den Bestand sieht nur, was der Cloud-Sync bereits
    zugestellt hat. Zwei Hosts, die kurz nacheinander ein Ticket anlegen,
    ziehen beim Hochzaehlen daher zwangslaeufig dieselbe Nummer -- am
    2026-08-15 real passiert: um 18:45 legte ASUS-GEI T-20260815-21 an, um
    18:46 WORKSTATION-LG einen voellig anderen Vorgang unter derselben ID.
    Der <HOST>-Suffix verhindert dabei die DATEI-Kollision, nicht die
    ID-Kollision.

    Zufall loest genau das: zwei Hosts, die einander nicht sehen koennen,
    ziehen trotzdem verschiedene Nummern. Der Abgleich gegen `used` ist NICHT
    der Schutzmechanismus -- er kann den fremden Stand ja nicht kennen --,
    sondern nur eine lokale Zusatzsicherung. Die eigentliche Sicherheit kommt
    aus der Groesse des Zahlenraums.

    WARUM 9 STELLEN: Der Raum muss das Geburtstagsparadox aushalten, nicht
    nur die Einzelkollision. Bei 35 Tickets am Tag (realer Durchsatz am
    2026-08-15) liegt die Kollisionswahrscheinlichkeit bei 2 Stellen bereits
    bei 99,95 %, bei 4 Stellen noch bei 6,4 %, bei 9 Stellen bei rund
    0,000007 % pro Tag.

    WARUM KEIN HOST-KUERZEL in der Nummer (geprueft und verworfen
    2026-08-15): Der Dateiname traegt mit dem <HOST>-Suffix bereits eine
    Host-Angabe, und die bedeutet ZUSTAENDIGKEIT (wer bearbeitet gerade). Ein
    zweites Host-Zeichen in der ID haette daneben HERKUNFT bedeutet (wer hat
    angelegt) -- zwei Bedeutungen, die sich im Alltag verwechseln lassen;
    "T-20260815-A21.WORKSTATION-LG.txt" liest sich wie ein Widerspruch.
    Ausserdem muesste man die Kuerzel aller Hosts kennen, um eine ID
    ueberhaupt zu verstehen.
    """
    rng = rng or random.SystemRandom()
    for _ in range(_MAX_DRAWS):
        number = rng.randrange(_ID_MIN, _ID_MAX + 1)
        if number not in used:
            return number
    raise RuntimeError(
        f"no free ticket number found after {_MAX_DRAWS} draws "
        f"({len(used)} numbers already taken for this date)")


def create(title: str, body: str, project: str | None = None, priority: str = "mittel",
           pipeline: str = "<offen>", tickets_dir: Path | None = None,
           today: str | None = None, rng=None) -> str:
    """Erzeugt ein unclaimed Ticket in <tickets_dir>/INBOX/. Returns den Pfad.

    tickets_dir ist erforderlich (oder via TICKET_MASTER_TICKETS_DIR gesetzt).
    rng ist injizierbar (wie today=) fuer deterministische Tests; ohne Angabe
    wird random.SystemRandom() genutzt."""
    base = Path(tickets_dir) if tickets_dir else _default_tickets_dir()
    if base is None:
        raise ValueError(
            "tickets_dir required (pass it or set TICKET_MASTER_TICKETS_DIR).")
    inbox = base / "INBOX"
    inbox.mkdir(parents=True, exist_ok=True)

    date_iso = today or datetime.now().strftime("%Y-%m-%d")
    datestr = date_iso.replace("-", "")
    used = used_numbers(base, datestr)
    while True:
        number = draw_number(used, rng)
        ticket_id = f"T-{datestr}-{number}"
        target = inbox / f"{ticket_id}.txt"
        content = _TICKET_TEMPLATE.format(
            ticket_id=ticket_id, title=title.strip() or "<ohne Titel>",
            date=date_iso, priority=priority, pipeline=pipeline,
            project=project or "<offen>",
            body=body.strip() or "<keine Beschreibung>",
        )
        try:
            # Exklusiv anlegen ("x"): schreibt NIE ueber ein bestehendes
            # Ticket. Letzte Sicherung, falls ein paralleler Erzeuger auf
            # DIESEM Host in derselben Millisekunde dieselbe Zahl zieht --
            # dann wird neu gewuerfelt statt ueberschrieben.
            with target.open("x", encoding="utf-8") as fh:
                fh.write(content)
        except FileExistsError:
            used.add(number)
            continue
        return str(target)


def create_routed_ticket(
    title: str,
    body: str,
    *,
    tickets_dir: Path,
    registry_snapshot: dict,
    ticket_kind: str = "normal",
    target_kind: str = "any",
    target: str | None = None,
    targets: tuple[str, ...] | list[str] | None = None,
    via: str | None = None,
    route_alias: str | None = None,
    binding_mode: str = "required",
    binding_ttl: int | str | None = None,
    primary_ticket: str | None = None,
    original_owner: str | None = None,
    receipt_to: str | None = None,
    execution_matrix: dict | None = None,
    idempotency_key: str | None = None,
    resolver=None,
    project: str | None = None,
    priority: str = "mittel",
    pipeline: str = "<offen>",
    today: str | None = None,
    created_at: datetime | str | None = None,
    rng=None,
) -> str:
    """Create one schema-v2 contract through the canonical ID authority.

    ``registry_snapshot`` is an injected, evidence-bearing system inventory;
    this library contains no host list.  ``route_alias`` accepts user-facing
    forms such as ``.all.claude`` or ``.WORKSTATION-LG.claude-opus`` and
    resolves the execution portion only through Clutch's public API.
    """
    if tickets_dir is None:
        raise ValueError("tickets_dir required for routing schema v2")
    base = Path(tickets_dir)
    inbox = base / "INBOX"
    inbox.mkdir(parents=True, exist_ok=True)
    date_iso = today or datetime.now().strftime("%Y-%m-%d")
    datestr = date_iso.replace("-", "")
    if route_alias:
        aliases = [part for part in route_alias.split(".") if part]
        alias_name, _alias_note = normalize_alias(
            "T-00000000-0", aliases,
            registry_snapshot=registry_snapshot, resolver=resolver,
        )
        parsed_alias = parse_ticket_name(alias_name)
        via = parsed_alias.via
        if parsed_alias.target in {"all", "grouped"}:
            target_kind = parsed_alias.target
        elif parsed_alias.target:
            target_kind, target = "exact", parsed_alias.target
        else:
            target_kind = "any"
    snapshot = resolve_targets(
        target_kind, target=target, targets=targets,
        registry_snapshot=registry_snapshot,
    )
    request_payload = {
        "title": title.strip(), "body": body.strip(), "ticket_kind": ticket_kind,
        "target_snapshot": snapshot, "via": via, "binding_mode": binding_mode,
        "binding_ttl": binding_ttl, "primary_ticket": primary_ticket,
        "original_owner": original_owner, "receipt_to": receipt_to,
        "execution_matrix": execution_matrix or {}, "project": project,
        "priority": priority, "pipeline": pipeline,
        "created_at": str(created_at) if created_at is not None else None,
    }
    request_fingerprint = "sha256:" + hashlib.sha256(
        json.dumps(request_payload, ensure_ascii=False, sort_keys=True,
                   separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    if idempotency_key:
        matches: list[Path] = []
        for candidate, _date, _number, _suffix in iter_lifecycle_files(base):
            try:
                fields = {
                    match.group(1): match.group(2).strip()
                    for line in candidate.read_text(encoding="utf-8").splitlines()
                    if (match := re.match(r"^([A-Z][A-Z0-9_]*):\s*(.*)$", line.strip()))
                }
            except (OSError, UnicodeError):
                continue
            if fields.get("CREATE_IDEMPOTENCY_KEY") == idempotency_key:
                if fields.get("CREATE_REQUEST_FINGERPRINT") != request_fingerprint:
                    raise ValueError("idempotency key already belongs to a different routed request")
                matches.append(candidate)
        if len(matches) > 1:
            raise ValueError("idempotency key resolves to multiple ticket contracts")
        if matches:
            return str(matches[0])
    used = used_numbers(base, datestr)
    while True:
        number = draw_number(used, rng)
        ticket_id = f"T-{datestr}-{number}"
        if ticket_kind in {"transfer", "fork"}:
            if not primary_ticket or not original_owner or not receipt_to:
                raise ValueError(
                    "transfer/fork requires primary_ticket, original_owner and receipt_to"
                )
        metadata = contract_metadata(
            ticket_id=ticket_id,
            ticket_kind=ticket_kind,
            target_snapshot=snapshot,
            primary_ticket=primary_ticket or ticket_id,
            original_owner=original_owner or "n/a",
            receipt_to=receipt_to or ticket_id,
            via=via,
            binding_mode=binding_mode,
            binding_ttl=binding_ttl,
            resolver=resolver,
            execution_matrix=execution_matrix,
            created_at=(
                created_at
                if created_at is not None
                else (f"{date_iso}T00:00:00Z" if today else None)
            ),
        )
        metadata["CREATE_IDEMPOTENCY_KEY"] = idempotency_key or ""
        metadata["CREATE_REQUEST_FINGERPRINT"] = request_fingerprint
        target_path = inbox / canonical_contract_name(ticket_id, metadata)
        content = _TICKET_TEMPLATE.format(
            ticket_id=ticket_id,
            title=title.strip() or "<ohne Titel>",
            date=date_iso,
            priority=priority,
            pipeline=pipeline,
            project=project or "<offen>",
            body=body.strip() or "<keine Beschreibung>",
        )
        content = update_fields(
            content,
            metadata,
            log=f"{date_iso}  Routing schema v2 contract created through ticket_writer.",
        )
        try:
            with target_path.open("x", encoding="utf-8", newline="\n") as handle:
                handle.write(content)
        except FileExistsError:
            used.add(number)
            continue
        return str(target_path)


def _cli(argv: list[str] | None = None) -> int:
    """CLI so any agent gets a collision-free ID in one shell call instead of
    picking the next number by eye (T-20260808-03: exactly that manual
    picking, bypassing this module's atomic exclusive-create, produced a
    same-minute collision between two agents on one host)."""
    import argparse

    parser = argparse.ArgumentParser(
        prog="ticket_writer",
        description=(
            "Atomically create an unclaimed ticket with a 9-digit random ID "
            "(T-YYYYMMDD-#########.txt in INBOX/)."
        ),
    )
    parser.add_argument("--title", required=True)
    parser.add_argument("--body", default="")
    parser.add_argument("--project", default=None)
    parser.add_argument("--priority", default="mittel")
    parser.add_argument("--pipeline", default="<offen>")
    parser.add_argument("--tickets-dir", default=None)
    parser.add_argument("--ticket-kind", choices=("normal", "transfer", "fork"))
    parser.add_argument("--target-kind", choices=("any", "all", "grouped", "exact"))
    parser.add_argument("--target")
    parser.add_argument("--targets", help="comma-separated explicit grouped targets")
    parser.add_argument("--via", help="Clutch execution selector")
    parser.add_argument("--route-alias", help="user alias, e.g. .all.claude")
    parser.add_argument("--binding-mode", choices=("required", "preferred"), default="required")
    parser.add_argument("--binding-ttl", default=None, help="days or explicit 'never'")
    parser.add_argument("--systems-registry", help="JSON system snapshot (required for schema v2)")
    parser.add_argument("--primary-ticket")
    parser.add_argument("--original-owner")
    parser.add_argument("--receipt-to")
    parser.add_argument("--execution-matrix", help="optional JSON file for via-mixed")
    parser.add_argument("--idempotency-key", help="stable caller key for retry-safe creation")
    args = parser.parse_args(argv)
    try:
        if args.ticket_kind or args.target_kind or args.via or args.route_alias:
            if not args.systems_registry:
                raise ValueError("--systems-registry is required for routing schema v2")
            import json

            registry = json.loads(Path(args.systems_registry).read_text(encoding="utf-8"))
            matrix = (
                json.loads(Path(args.execution_matrix).read_text(encoding="utf-8"))
                if args.execution_matrix else None
            )
            raw_ttl = args.binding_ttl
            ttl = raw_ttl if raw_ttl in {None, "never"} else int(raw_ttl)
            path = create_routed_ticket(
                args.title, args.body,
                tickets_dir=Path(args.tickets_dir) if args.tickets_dir else _default_tickets_dir(),
                registry_snapshot=registry,
                ticket_kind=args.ticket_kind or "normal",
                target_kind=args.target_kind or "any",
                target=args.target,
                targets=args.targets.split(",") if args.targets else None,
                via=args.via,
                route_alias=args.route_alias,
                binding_mode=args.binding_mode,
                binding_ttl=ttl,
                primary_ticket=args.primary_ticket,
                original_owner=args.original_owner,
                receipt_to=args.receipt_to,
                execution_matrix=matrix,
                idempotency_key=args.idempotency_key,
                project=args.project,
                priority=args.priority,
                pipeline=args.pipeline,
            )
        else:
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
