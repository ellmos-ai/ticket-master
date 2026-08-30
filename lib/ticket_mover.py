r"""
ticket_mover.py — Fail-closed move for ticket files between lifecycle folders.

Ticket T-20260808-03: no code path existed for moving a ticket between
status folders (INBOX/ACTIONABLE/QUEUED/.../SOLVED). Moves happened by hand
(an agent reading a file's content and writing it to the new location, or a
plain shell `mv`), with no check whether the destination already held an
unrelated ticket under the same ID. On 2026-08-08 that silently destroyed a
ticket that had lived in SOLVED/ since 2026-08-01: the overwriting write
looked completely normal on readback, so the loss was invisible until file
counts were compared.

move_ticket() closes that gap structurally, not procedurally: it refuses to
write over an existing destination, using the same atomic-exclusive-create
primitive already proven in ticket_writer.create() (`os.O_EXCL`), so the
result does not depend on whoever calls it remembering to check first.

Same-host target-name races are a purely local filesystem question, so
O_EXCL's atomicity is authoritative immediately.  A delayed cloud sync can
nevertheless reveal a second physical file with the same canonical ticket ID
under a different host/routing suffix or lifecycle folder.  Before any move
to QUEUED, move_ticket() therefore scans the canonical flat queue and refuses
when another visible file owns the same ID.  A second scan after writing but
before deleting the source narrows the local scan/write race as well.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import socket
import time
from pathlib import Path

try:  # package import
    from .ticket_writer import TICKET_FILENAME_RE, iter_lifecycle_files
    from .routing_contract import (
        ClaimDeniedError,
        RoutingContractError,
        claim_contract,
        complete_contract,
        parse_ticket_name,
        normalize_expired_binding,
        record_receipt,
        recover_expired_claim,
        release_contract,
    )
except ImportError:  # direct import from lib on sys.path
    from ticket_writer import TICKET_FILENAME_RE, iter_lifecycle_files
    from routing_contract import (
        ClaimDeniedError,
        RoutingContractError,
        claim_contract,
        complete_contract,
        parse_ticket_name,
        normalize_expired_binding,
        record_receipt,
        recover_expired_claim,
        release_contract,
    )

__all__ = [
    "ClaimDeniedError",
    "DuplicateTicketIdError",
    "HostIdentityError",
    "NestedLifecycleDestinationError",
    "TicketCollisionError",
    "claim_contract",
    "claim_current_host",
    "verify_claim_host",
    "complete_contract",
    "move_ticket",
    "normalize_expired_binding",
    "record_receipt",
    "recover_expired_claim",
    "release_claim",
    "release_contract",
]

# Ordner, in denen ein Host-Suffix "ich arbeite gerade daran" bedeutet. NUR
# hier gibt die Sitzungs-Rueckgabe (release_claims) Claims frei.
WORKING_SUBDIRS = ("QUEUED", "ACTIONABLE")

# T-20260815-205002196: QUEUED und ACTIONABLE bedeuten NICHT dasselbe.
# ACTIONABLE = sofort umsetzbar, noch niemand dran -> Freigabe unbedenklich.
# QUEUED = an einen Agenten uebergeben, Ergebnis aussteht -> da arbeitet
# jemand, und zwar moeglicherweise IMMER NOCH, wenn ein ANDERER Prozess
# (z. B. eine andere, gerade beendete Sitzung desselben Hosts) die
# Rueckgabe aufruft. Deshalb ist QUEUED unter den "Arbeitsordnern" die
# Ausnahme, nicht die Regel: standardmaessig nur GEMELDET, nicht freigegeben
# (Loesung c). Explizit `include_queued=True` gibt sie mit frei -- aber auch
# dann NIE ein Ticket, das eine aktive Delegation traegt (Loesung b, siehe
# is_actively_delegated()).
QUEUED_SUBDIR = "QUEUED"

# Vermerk im Tickettext, dass ein Agent gerade aktiv daran arbeitet. Regex
# statt fixer Zeilenform, weil das Feld sowohl als eigene VERLAUF-Zeile
# ("2026-08-15  DELEGIERT_AN: claude-code@ASUS-GEI") als auch als
# eigenstaendiges Feld auftreten darf -- beide Formen erlaubt der
# Ticket-Nachtrag zum Ticket.
DELEGATION_MARKER_RE = re.compile(r"DELEGIERT_AN:\s*(?P<agent>\S+)")

# Sicherheitsnetz, nicht die Hauptregel -- wie `expires_after` beim
# LOCK-System (~/CLAUDE.md, "Projekt-Sperren"). Der Marker allein reicht
# NICHT ewig als Beleg: stuerzt ein Worker ab, ohne den Marker je zu
# entfernen oder das Ticket zu bewegen, wuerde ein blosses Vorhandensein den
# Claim fuer immer schuetzen -- genau die Sorte verwaister Blockade, die
# dieses Ticket beheben soll. Frische wird ueber die mtime der TICKETDATEI
# gemessen (jede VERLAUF-Aktualisierung durch den arbeitenden Worker
# aktualisiert sie automatisch mit -- kein separates Zeitstempel-Parsing
# noetig).
DELEGATION_STALE_AFTER_HOURS = 6.0

# Ordner, in denen derselbe Suffix etwas anderes bedeutet: Herkunft. In SOLVED
# steht, WER geloest hat; in BLOCKED/host-receipt, WER auf wessen Receipt
# wartet; in USER, wem der Nutzer antworten muss. Eine pauschale Rueckgabe
# wuerde diese Information loeschen und 193 abgeschlossene Vorgaenge (Stand
# 2026-08-15) fuer einen anderen Host wieder wie unerledigte Arbeit aussehen
# lassen -- deshalb bleiben sie ausgespart.
PROVENANCE_SUBDIRS = ("SOLVED", "USER", "BLOCKED", "WAITING", "PARKED")

# Reaktivierung: verlaesst ein Ticket einen Wartezustand Richtung ACTIONABLE,
# ist es wieder freie Arbeit -- und darf von JEDEM Host uebernommen werden.
# Genau an diesem Uebergang faellt der Claim (Nutzerentscheid 2026-08-15).
# Bewusst NICHT enthalten: QUEUED -> ACTIONABLE (Fehlschlag der Delegation,
# derselbe Host faellt auf seine eigene Fallback-Kette zurueck) und
# INBOX -> ACTIONABLE (frisch triagiert, war nie ein Wartezustand).
REACTIVATION_SOURCES = ("BLOCKED", "WAITING", "USER", "PARKED")
REACTIVATION_TARGET = "ACTIONABLE"


class TicketCollisionError(RuntimeError):
    """Raised when a move target already holds a (different) ticket file."""


class DuplicateTicketIdError(TicketCollisionError):
    """Raised when QUEUED would contain an already visible ticket ID copy."""


class DestinationLooksLikeFileError(ValueError):
    """Raised when dest_dir's last path segment is itself a ticket filename.

    move_ticket() always appends the source's own filename under dest_dir --
    dest_dir must be the destination FOLDER (e.g. ".../SOLVED"), never the
    full destination FILE path (".../SOLVED/T-....txt"). Passing the file
    path there is not caught by is_absolute()/single-part checks in
    resolve_dest_dir() (those only rescue a bare cluster name), so without
    this guard dest_dir.mkdir() silently creates a directory shaped like a
    ticket filename and the ticket ends up nested one level too deep inside
    it (T-20260818-427750316: happened live on a USER->SOLVED move, the
    on-disk result was .../SOLVED/T-....txt/T-....txt).
    """


class NestedLifecycleDestinationError(ValueError):
    """Raised when a lifecycle subcategory is encoded as a nested folder.

    Categories v1 uses one flat cluster folder.  A subcategory such as
    ``USER/decision`` belongs only in the ticket's STATUS field; accepting it
    as ``.../USER/decision/`` hides the ticket from every standard scanner.
    """


def unclaimed_name(filename: str) -> str:
    """Dateiname ohne Claim-Suffix ("T-DATE-NN[_slug].txt").

    Ein beschreibender Slug bleibt erhalten -- freigegeben wird der Claim,
    nicht die Benennung. Ist der Name kein Ticketname oder ohnehin schon
    unclaimed, kommt er unveraendert zurueck.
    """
    try:
        parsed = parse_ticket_name(filename)
    except RoutingContractError:
        parsed = None
    if parsed and parsed.is_v2:
        return parsed.unclaimed()
    m = TICKET_FILENAME_RE.match(filename)
    if not m or not m.group("suffix"):
        return filename
    slug = f"_{m.group('slug')}" if m.group("slug") else ""
    return f"T-{m.group('date')}-{m.group('number')}{slug}.txt"


def claim_suffix(filename: str) -> str | None:
    """Host-/Claim-Suffix eines Ticketnamens, oder None wenn unclaimed."""
    try:
        parsed = parse_ticket_name(filename)
    except RoutingContractError:
        parsed = None
    if parsed and parsed.is_v2:
        return parsed.claim
    m = TICKET_FILENAME_RE.match(filename)
    return m.group("suffix") if m else None


def _should_release_on_move(source: Path, dest_dir: Path) -> bool:
    return (source.parent.name in REACTIVATION_SOURCES
            and dest_dir.name == REACTIVATION_TARGET)


def queue_root(source: Path) -> Path:
    """Wurzel der Queue, ausgehend von einem Ticketpfad.

    Liegt das Ticket in einem Lebenszyklus-Ordner, ist die Wurzel dessen
    Elternordner; liegt es direkt in der Wurzel (INBOX-Alias), ist sie der
    eigene Elternordner.
    """
    try:
        from .ticket_writer import _LIFECYCLE_SUBDIRS
    except ImportError:
        from ticket_writer import _LIFECYCLE_SUBDIRS
    parent = source.parent
    return parent.parent if parent.name in _LIFECYCLE_SUBDIRS else parent


def resolve_dest_dir(source: Path, dest_dir: Path | str) -> Path:
    """Loest ein Verschiebeziel auf -- und faengt dabei einen Bedienfehler ab,
    der am 2026-08-15 real passiert ist.

    Ein Worker rief move_ticket(source, "SOLVED") mit dem blossen Clusternamen
    auf. Als relativer Pfad ist das ein Ordner im AKTUELLEN Arbeitsverzeichnis;
    angelegt wurde daher ticket-master/lib/SOLVED/, und das Ticket verschwand
    still aus der Queue -- ohne Fehler, ohne Warnung. Genau die Sorte lautloser
    Fehlablage, gegen die dieses Modul sonst fail-closed arbeitet.

    Deshalb: Ist das Ziel ein RELATIVER Pfad aus genau einem Namensteil UND ist
    dieser Name ein bekannter Lebenszyklus-Ordner, wird er gegen die Queue-Wurzel
    der QUELLE aufgeloest. Das ist deterministisch (die Quelle bestimmt die
    Wurzel) und trifft immer das, was der Aufrufer gemeint hat.

    Bewusst eng gehalten: Ein beliebiger anderer relativer Pfad bleibt relativ.
    Sonst wuerde aus der Bequemlichkeit Magie, die an anderer Stelle ueberrascht.
    """
    dest = Path(dest_dir)
    if dest.is_absolute() or len(dest.parts) != 1:
        return dest
    try:
        from .ticket_writer import _LIFECYCLE_SUBDIRS
    except ImportError:
        from ticket_writer import _LIFECYCLE_SUBDIRS
    if dest.name not in _LIFECYCLE_SUBDIRS:
        return dest
    return queue_root(source) / dest.name


def _ticket_identity(filename: str) -> tuple[str, int] | None:
    """Return the canonical date/number identity for legacy or routing-v2 names."""
    try:
        parsed = parse_ticket_name(filename)
    except RoutingContractError:
        return None
    return parsed.date, int(parsed.number)


def _same_physical_file(left: Path, right: Path) -> bool:
    """Compare existing files by identity, with a path fallback for I/O races."""
    try:
        return left.samefile(right)
    except OSError:
        return os.path.normcase(os.path.abspath(left)) == os.path.normcase(
            os.path.abspath(right)
        )


def _assert_unique_id_for_queued_move(
    source: Path,
    queue_base: Path,
    *,
    allowed: tuple[Path, ...] = (),
) -> None:
    """Fail closed if another visible lifecycle file owns the source ticket ID.

    ``iter_lifecycle_files`` is the shared filename/lifecycle authority used by
    ticket creation and audit code.  Reusing it keeps this gate aware of both
    legacy host suffixes and routing-schema-v2 axes without introducing a
    second ticket-name grammar.
    """


    identity = _ticket_identity(source.name)
    if identity is None:
        raise TicketCollisionError(
            f"cannot establish ticket ID before move to QUEUED: {source}"
        )
    ignored = (source, *allowed)
    conflicts: list[Path] = []
    for entry, datestr, number, _suffix in iter_lifecycle_files(queue_base):
        if (datestr, number) != identity:
            continue
        if any(_same_physical_file(entry, candidate) for candidate in ignored):
            continue
        conflicts.append(entry)
    if not conflicts:
        return
    found = sorted((source, *conflicts), key=lambda path: str(path).casefold())
    rendered = "; ".join(str(path) for path in found)
    ticket_id = f"T-{identity[0]}-{identity[1]}"
    raise DuplicateTicketIdError(
        f"duplicate ticket ID {ticket_id} detected before move to QUEUED; "
        f"refusing without mutation. Found paths: {rendered}"
    )


class HostIdentityError(RuntimeError):
    """Raised when the live host and its canonical self-slot do not agree."""


def resolve_live_host(sync_root: Path | str) -> tuple[str, str, Path]:
    """Resolve host identity from the live OS plus the canonical self-slot."""
    root = Path(sync_root).expanduser().resolve()
    host = socket.gethostname().strip()
    if not host or not re.fullmatch(r"[A-Za-z0-9_-]+", host):
        raise HostIdentityError(f"live hostname is empty or unsafe: {host!r}")
    snapshots = root / "_config-state" / "snapshots"
    matches: list[tuple[Path, dict]] = []
    if snapshots.is_dir():
        for path in sorted(snapshots.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8-sig"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                continue
            if isinstance(data, dict) and str(data.get("host", "")).casefold() == host.casefold():
                matches.append((path, data))
    if len(matches) != 1:
        raise HostIdentityError(
            f"live host {host!r} requires exactly one canonical self-slot snapshot; "
            f"found {len(matches)} under {snapshots}"
        )
    snapshot, data = matches[0]
    slot = str(data.get("slot", "")).strip()
    if not slot or snapshot.stem.casefold() != slot.casefold():
        raise HostIdentityError(f"snapshot/slot mismatch in {snapshot}: slot={slot!r}")
    manifest = root / slot / "repos.json"
    try:
        manifest_data = json.loads(manifest.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise HostIdentityError(f"canonical self-slot manifest unreadable: {manifest}") from exc
    if (
        not isinstance(manifest_data, dict)
        or str(manifest_data.get("host", "")).casefold() != host.casefold()
        or str(manifest_data.get("slot", "")).casefold() != slot.casefold()
    ):
        raise HostIdentityError(
            f"live host, snapshot and self-slot manifest disagree: {manifest}"
        )
    return host, slot, snapshot


def verify_claim_host(claim_host: str, *, sync_root: Path | str) -> tuple[str, str, Path]:
    """Verify an asserted claim host for legacy and routing-v2 claim paths."""
    host, slot, snapshot = resolve_live_host(sync_root)
    if claim_host.casefold() != host.casefold():
        raise HostIdentityError(
            f"asserted claim host {claim_host!r} does not match verified live host {host!r}"
        )
    return host, slot, snapshot


def claim_current_host(ticket: Path | str, *, claim_host: str, sync_root: Path | str,
                       dry_run: bool = False) -> Path:
    """Fail-closed legacy claim after verifying the asserted claim host."""
    source = Path(ticket)
    if not source.is_file():
        raise FileNotFoundError(f"ticket does not exist or is not a file: {source}")
    if source.parent.name not in WORKING_SUBDIRS:
        raise HostIdentityError(
            f"current-host claims are allowed only in {WORKING_SUBDIRS}: {source}"
        )
    host, _slot, _snapshot = verify_claim_host(claim_host, sync_root=sync_root)
    try:
        parsed = parse_ticket_name(source.name)
    except RoutingContractError as exc:
        raise HostIdentityError(f"cannot claim invalid ticket name: {source.name}") from exc
    if parsed.is_v2:
        raise HostIdentityError("routing-v2 tickets must use claim_contract()")
    existing = claim_suffix(source.name)
    if existing:
        if existing.casefold() == host.casefold():
            return source
        raise HostIdentityError(
            f"ticket is already claimed by {existing!r}; verified live host is {host!r}"
        )
    match = TICKET_FILENAME_RE.match(source.name)
    if not match:
        raise HostIdentityError(f"cannot build legacy claim name: {source.name}")
    slug = f"_{match.group('slug')}" if match.group("slug") else ""
    claimed_name = f"T-{match.group('date')}-{match.group('number')}{slug}.{host}.txt"
    return move_ticket(source, source.parent, new_name=claimed_name, dry_run=dry_run)


def move_ticket(source: Path | str, dest_dir: Path | str,
                release_claim: bool | None = None,
                new_name: str | None = None,
                dry_run: bool = False) -> Path:
    """Move a ticket file into dest_dir, by default under its current filename.

    Fails closed: if dest_dir already contains a file with that name, nothing
    is written and nothing is deleted — TicketCollisionError is raised and
    both the source and the pre-existing destination file are left exactly
    as they were. The destination is created via O_CREAT|O_EXCL (atomic on
    both POSIX and Windows), so a second caller racing for the same
    destination name always loses cleanly instead of overwriting the winner.

    The source is only deleted after the destination write is confirmed via
    a byte-for-byte readback AND the source is re-read and confirmed
    unchanged since the copy was made (guards against a foreign writer
    editing the ticket while the move is in flight — the move then aborts
    with both copies intact rather than deleting a stale source under a
    changed original).

    release_claim steuert, ob der Host-Suffix beim Verschieben faellt:
      None (Default) -- automatisch: bei einer Reaktivierung (BLOCKED/WAITING/
            USER/PARKED -> ACTIONABLE) faellt der Claim, sonst bleibt er.
            Ein Ticket, das aus dem Wartezustand zurueck in die Arbeit geht,
            ist wieder fuer jeden Host frei; solange es wartet, bleibt der
            Suffix als Herkunftsnachweis stehen.
      True  -- Claim in jedem Fall freigeben.
      False -- Claim in jedem Fall behalten.

    Ist der freigegebene Name im Ziel bereits belegt, wird NICHT abgebrochen:
    das Ticket wandert dann unter seinem geclaimten Namen. Sonst wuerde eine
    Entblockung an einem fremden Namensnachbarn scheitern und das Ticket
    bliebe im Wartezustand haengen, obwohl sein Blocker weg ist.

    new_name benennt die Datei zusaetzlich um (Umnummerierung bei einer
    ID-Kollision). Derselbe Zielordner ist erlaubt -- dann ist der Aufruf ein
    reiner, fail-closed Rename. new_name schlaegt release_claim, damit der
    uebergebene Name wirklich der Zielname ist und nicht nachtraeglich
    verkuerzt wird.

    dry_run fuehrt dieselben Pfad-, Lebenszyklus-, Quellen- und
    Kollisionspruefungen aus, gibt aber nur den geplanten Zielpfad zurueck.
    Weder Zielordner noch Zieldatei werden angelegt und die Quelle bleibt
    unveraendert.
    """
    source = Path(source)
    if not source.is_file():
        raise FileNotFoundError(f"move source does not exist or is not a file: {source}")
    # Blosser Clustername ("SOLVED") wird gegen die Queue-Wurzel der Quelle
    # aufgeloest, statt still einen Ordner im Arbeitsverzeichnis anzulegen.
    dest_dir = resolve_dest_dir(source, dest_dir)

    # Bedienfehler abfangen, BEVOR mkdir() ihn in eine echte Fehlablage
    # verwandelt: dest_dir ist die Zielordner, nicht die Zieldatei. Ein
    # dest_dir, dessen letztes Pfadsegment selbst wie ein Ticketname aussieht,
    # ist so gut wie immer genau dieser Bedienfehler (T-20260818-427750316).
    looks_like_ticket = bool(TICKET_FILENAME_RE.match(dest_dir.name))
    if not looks_like_ticket:
        try:
            parse_ticket_name(dest_dir.name)
            looks_like_ticket = True
        except RoutingContractError:
            pass
    if looks_like_ticket:
        raise DestinationLooksLikeFileError(
            f"dest_dir looks like a ticket FILE path, not a destination "
            f"FOLDER: {dest_dir}. Pass the lifecycle folder only "
            f"(e.g. '.../SOLVED'), not '.../SOLVED/{dest_dir.name}' -- "
            f"move_ticket() appends the filename itself."
        )

    # Categories v1 is a flat folder contract. STATUS carries a subcategory;
    # the filesystem never does. Reject both absolute queue-root paths and
    # relative USER/decision-style paths before mkdir can create them.
    try:
        from .ticket_writer import _LIFECYCLE_SUBDIRS
    except ImportError:
        from ticket_writer import _LIFECYCLE_SUBDIRS
    lifecycle = {name for name in _LIFECYCLE_SUBDIRS if name}
    raw_parts = Path(dest_dir).parts
    if not Path(dest_dir).is_absolute() and len(raw_parts) > 1 and raw_parts[0] in lifecycle:
        raise NestedLifecycleDestinationError(
            f"nested lifecycle destination is forbidden: {dest_dir}; "
            "put the subcategory in STATUS and move to the flat cluster folder"
        )
    try:
        relative = dest_dir.resolve().relative_to(queue_root(source).resolve())
    except ValueError:
        relative = None
    if relative is not None and len(relative.parts) > 1 and relative.parts[0] in lifecycle:
        raise NestedLifecycleDestinationError(
            f"nested lifecycle destination is forbidden: {dest_dir}; "
            "put the subcategory in STATUS and move to the flat cluster folder"
        )

    if new_name:
        target_name = new_name
    else:
        if release_claim is None:
            release_claim = _should_release_on_move(source, dest_dir)
        target_name = unclaimed_name(source.name) if release_claim else source.name
        if release_claim and (dest_dir / target_name).exists():
            target_name = source.name

    target = dest_dir / target_name
    queued_id_gate = dest_dir.name == QUEUED_SUBDIR
    if queued_id_gate:
        _assert_unique_id_for_queued_move(source, dest_dir.parent)
    if target.exists():
        raise TicketCollisionError(
            f"move target already exists, refusing to overwrite: {target}"
        )

    data = source.read_bytes()
    if dry_run:
        return target

    dest_dir.mkdir(parents=True, exist_ok=True)
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0)
    try:
        fd = os.open(str(target), flags, 0o644)
    except FileExistsError as exc:
        # Lost the race between the exists() check above and this open():
        # another mover won in between. Fail closed either way.
        raise TicketCollisionError(
            f"move target already exists, refusing to overwrite: {target}"
        ) from exc

    written_ok = False
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        if target.read_bytes() != data:
            raise RuntimeError(f"post-write verification failed for {target}")
        if source.read_bytes() != data:
            raise RuntimeError(
                f"source changed during move, aborting without deleting it: {source}"
            )
        if queued_id_gate:
            # Catch a differently named copy that became visible after the
            # preflight scan but before source deletion.  The just-written
            # target is expected during this second scan and is ignored.
            _assert_unique_id_for_queued_move(
                source, dest_dir.parent, allowed=(target,)
            )
        written_ok = True
    finally:
        if not written_ok:
            # Partial/failed write or verification mismatch: remove the
            # half-written destination so a retry does not itself trip the
            # collision guard, and leave the source untouched either way.
            with contextlib.suppress(FileNotFoundError, OSError):
                target.unlink()

    source.unlink()
    return target


def release_claim(ticket: Path | str) -> Path:
    """Gibt den Claim EINES Tickets frei: benennt "T-DATE-NN[_slug].<HOST>.txt"
    in "T-DATE-NN[_slug].txt" um, sodass jeder Host es beanspruchen kann.

    Fail-closed wie move_ticket: liegt die unclaimed Fassung schon da (ein
    anderer Vorgang unter derselben Nummer), wird nichts ueberschrieben,
    sondern TicketCollisionError geworfen. Ein bereits unclaimed Ticket ist
    ein No-op und kommt unveraendert zurueck.
    """
    ticket = Path(ticket)
    if not ticket.is_file():
        raise FileNotFoundError(f"ticket does not exist or is not a file: {ticket}")

    try:
        parsed = parse_ticket_name(ticket.name)
    except RoutingContractError:
        parsed = None
    if parsed and parsed.is_v2 and parsed.claim:
        return release_contract(ticket, host=parsed.claim)
    freed = ticket.parent / unclaimed_name(ticket.name)
    if freed == ticket:
        return ticket
    if freed.exists():
        raise TicketCollisionError(
            f"unclaimed name already taken, refusing to overwrite: {freed}"
        )
    return move_ticket(ticket, ticket.parent, release_claim=True)


def is_actively_delegated(ticket: Path | str, *,
                          now: float | None = None,
                          stale_after_hours: float = DELEGATION_STALE_AFTER_HOURS) -> bool:
    """True, wenn das Ticket einen DELEGIERT_AN-Vermerk traegt UND dieser noch
    frisch ist (Dateiaenderung juenger als `stale_after_hours`).

    Ein Vermerk ohne Frische zaehlt NICHT als aktiv -- siehe
    DELEGATION_STALE_AFTER_HOURS: das ist das Sicherheitsnetz gegen einen
    Worker, der abgestuerzt ist, ohne den Claim je zurueckzugeben.
    """
    ticket = Path(ticket)
    if not ticket.is_file():
        return False
    try:
        text = ticket.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    if not DELEGATION_MARKER_RE.search(text):
        return False
    now = time.time() if now is None else now
    age_hours = (now - ticket.stat().st_mtime) / 3600.0
    return age_hours < stale_after_hours


def mark_delegated(ticket: Path | str, agent: str) -> None:
    """Traegt einen DELEGIERT_AN-Vermerk in ein Ticket ein -- die
    schreibende Haelfte von `is_actively_delegated()`.

    Aufzurufen von einem Worker, sobald er ein QUEUED-Ticket tatsaechlich zu
    bearbeiten BEGINNT (nicht erst beim Loesen). Jeder weitere Aufruf oder
    jede sonstige Bearbeitung des Tickets (VERLAUF-Eintrag) haelt den Marker
    von selbst frisch, weil beides dieselbe Dateiaenderungszeit aktualisiert
    -- kein separater Heartbeat-Prozess noetig.
    """
    ticket = Path(ticket)
    if not ticket.is_file():
        raise FileNotFoundError(f"ticket does not exist or is not a file: {ticket}")
    text = ticket.read_text(encoding="utf-8", errors="replace")
    marker = f"DELEGIERT_AN: {agent}"
    if DELEGATION_MARKER_RE.search(text):
        # Bestehenden Vermerk ersetzen statt einen zweiten anzuhaengen --
        # sonst waechst die Datei bei jedem erneuten Aufruf im selben Lauf.
        text = DELEGATION_MARKER_RE.sub(marker, text, count=1)
    else:
        text = text.rstrip("\n") + f"\n{marker}\n"
    ticket.write_text(text, encoding="utf-8")


def release_claims(base: Path | str, *, host: str,
                   folders: tuple[str, ...] = WORKING_SUBDIRS,
                   include_queued: bool = False,
                   stale_after_hours: float = DELEGATION_STALE_AFTER_HOURS,
                   dry_run: bool = False,
                   report_refused: bool = False):
    """Sitzungs-Rueckgabe: gibt Claims von `host` frei, damit ein regulaer
    beendetes Sitzungsende keine Tickets fuer andere Hosts blockiert -- ohne
    dabei Tickets freizugeben, an denen noch tatsaechlich gearbeitet wird
    (T-20260815-205002196).

    `host` ist ein PFLICHT-Argument ohne Default. Bewusst kein automatisches
    COMPUTERNAME: der Bestand fuehrt fuer dieselbe Maschine historisch zwei
    Identitaeten (ASUS-GEI und LAPTOP), und eine falsch geratene Identitaet
    wuerde entweder nichts freigeben oder -- schlimmer -- fremde Claims
    anfassen. Verglichen wird deshalb exakt und ohne Normalisierung; das
    Zusammenfuehren veralteter Host-Identitaeten ist ein eigener, benannter
    Vorgang und passiert nicht still in der Rueckgabe.

    ACTIONABLE (sofort umsetzbar, noch niemand dran) wird UNBEDINGT
    freigegeben -- das ist unbedenklich, siehe Kategorien-Doku.

    QUEUED (an einen Agenten uebergeben, Ergebnis aussteht) ist die Ausnahme,
    NICHT die Regel (Loesung c aus dem Ticket):
      - Standardmaessig (include_queued=False) wird ein QUEUED-Claim NICHT
        freigegeben, sondern nur GEMELDET (`held`, Grund "queued, not
        included") -- der Aufrufer sieht die Kandidaten, ohne dass sie
        blind mitgehen.
      - Mit include_queued=True wird ein QUEUED-Claim freigegeben, AUSSER er
        traegt einen frischen DELEGIERT_AN-Vermerk (Loesung b,
        is_actively_delegated()) -- dann bleibt er IN JEDEM FALL erhalten
        und landet ebenfalls in `held`, Grund "active delegation". Das gilt
        unabhaengig von include_queued, weil eine aktive Delegation niemals
        blind uebergangen werden darf.

    In SOLVED/USER/BLOCKED/WAITING/PARKED bleibt der Suffix stehen: dort ist
    er Herkunft, nicht Arbeitsanspruch. Diese Tickets werden stattdessen bei
    ihrer Reaktivierung frei (siehe move_ticket / REACTIVATION_SOURCES).

    Ein einzelnes blockiertes Ticket bricht den Lauf nicht ab -- sonst bliebe
    eine ganze Sitzung geclaimed, weil ein Name belegt war. Mit
    report_refused=True kommt (freigegeben, verweigert, gehalten) zurueck,
    sonst nur die Liste der freigegebenen Pfade.
    """
    base = Path(base)
    freed: list[Path] = []
    refused: list[tuple[Path, str]] = []
    held: list[tuple[Path, str]] = []

    for folder in folders:
        directory = base / folder
        if not directory.is_dir():
            continue
        for entry in sorted(directory.iterdir()):
            if not entry.is_file() or claim_suffix(entry.name) != host:
                continue

            if folder == QUEUED_SUBDIR:
                if is_actively_delegated(entry, stale_after_hours=stale_after_hours):
                    held.append((entry, "active delegation"))
                    continue
                if not include_queued:
                    held.append((entry, "queued, not included (use --include-queued)"))
                    continue

            if dry_run:
                freed.append(directory / unclaimed_name(entry.name))
                continue
            try:
                freed.append(release_claim(entry))
            except (TicketCollisionError, RuntimeError, OSError) as exc:
                refused.append((entry, str(exc)))

    return (freed, refused, held) if report_refused else freed


def _cli(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="ticket_mover",
        description=("Fail-closed move of a single ticket file into a lifecycle "
                     "folder, or release this host's claims at session end."),
    )
    parser.add_argument("source", nargs="?",
                        help="Path to the ticket file to move.")
    parser.add_argument("dest_dir", nargs="?",
                        help="Destination lifecycle folder (e.g. .../SOLVED).")
    parser.add_argument("--release-session", action="store_true",
                        help=("Release this host's claims at regular session end. "
                              "ACTIONABLE always; QUEUED only reported unless "
                              "--include-queued is given (T-20260815-205002196). "
                              "Requires --host."))
    parser.add_argument("--host",
                        help="Claim suffix to release, e.g. ASUS-GEI. Exact match.")
    parser.add_argument("--tickets-dir",
                        help="Ticket queue root (for --release-session).")
    parser.add_argument("--include-queued", action="store_true",
                        help=("Also release QUEUED claims (not just report them). "
                              "Still never releases an actively delegated ticket "
                              "(fresh DELEGIERT_AN marker)."))
    parser.add_argument("--dry-run", action="store_true",
                        help=("Preview a single-ticket move or show what "
                              "--release-session would free; change nothing."))
    parser.add_argument("--mark-delegated", metavar="TICKET",
                        help="Write a DELEGIERT_AN marker into TICKET. Requires --agent.")
    parser.add_argument("--claim-current-host", metavar="TICKET",
                        help=("Claim TICKET after live host + self-slot verification. "
                              "Requires --host and --sync-root."))
    parser.add_argument("--verify-claim-host", metavar="HOST",
                        help=("Verify an asserted host before any legacy or routing-v2 "
                              "claim. Requires --sync-root."))
    parser.add_argument("--sync-root",
                        help="Canonical .SYNC root for --claim-current-host.")
    parser.add_argument("--agent",
                        help="Agent identity for --mark-delegated, e.g. claude-code@ASUS-GEI.")
    args = parser.parse_args(argv)

    if args.verify_claim_host:
        if (args.claim_current_host or args.mark_delegated or args.release_session
                or args.source or args.dest_dir or args.dry_run):
            parser.error("--verify-claim-host cannot be combined with another operation")
        if not args.sync_root:
            parser.error("--verify-claim-host requires --sync-root")
        try:
            host, slot, snapshot = verify_claim_host(
                args.verify_claim_host, sync_root=args.sync_root
            )
        except HostIdentityError as exc:
            print(f"REFUSED: {exc}")
            return 1
        print(f"VERIFIED CLAIM HOST: {host} (slot={slot}, snapshot={snapshot})")
        return 0

    if args.claim_current_host:
        if args.mark_delegated or args.release_session or args.source or args.dest_dir:
            parser.error("--claim-current-host cannot be combined with another operation")
        if not args.host or not args.sync_root:
            parser.error("--claim-current-host requires --host and --sync-root")
        try:
            target = claim_current_host(
                args.claim_current_host, claim_host=args.host,
                sync_root=args.sync_root, dry_run=args.dry_run,
            )
        except (HostIdentityError, TicketCollisionError, FileNotFoundError,
                RuntimeError) as exc:
            print(f"REFUSED: {exc}")
            return 1
        label = "WOULD CLAIM" if args.dry_run else "CLAIMED"
        print(f"{label}: {target}")
        return 0

    if args.mark_delegated:
        if args.dry_run:
            parser.error("--dry-run cannot be combined with --mark-delegated")
        if not args.agent:
            parser.error("--mark-delegated requires --agent")
        try:
            mark_delegated(args.mark_delegated, args.agent)
        except FileNotFoundError as exc:
            print(f"REFUSED: {exc}")
            return 1
        print(f"MARKED: {args.mark_delegated} DELEGIERT_AN: {args.agent}")
        return 0

    if args.release_session:
        if not args.host or not args.tickets_dir:
            parser.error("--release-session requires --host and --tickets-dir")
        freed, refused, held = release_claims(
            args.tickets_dir, host=args.host, dry_run=args.dry_run,
            include_queued=args.include_queued, report_refused=True)
        label = "WOULD RELEASE" if args.dry_run else "RELEASED"
        for path in freed:
            print(f"{label}: {path}")
        for path, reason in refused:
            print(f"REFUSED: {path} -- {reason}")
        for path, reason in held:
            print(f"HELD: {path} -- {reason}")
        summary = f"{label} {len(freed)} claim(s) for host {args.host}"
        if refused:
            summary += f", {len(refused)} refused"
        if held:
            summary += f", {len(held)} held (queued/active)"
        print(summary)
        return 1 if refused else 0

    if not args.source or not args.dest_dir:
        parser.error("source and dest_dir are required unless --release-session "
                      "or --mark-delegated is given")
    try:
        target = move_ticket(args.source, args.dest_dir, dry_run=args.dry_run)
    except (TicketCollisionError, FileNotFoundError, RuntimeError,
            DestinationLooksLikeFileError, NestedLifecycleDestinationError) as exc:
        print(f"REFUSED: {exc}")
        return 1
    label = "WOULD MOVE" if args.dry_run else "MOVED"
    print(f"{label}: {target}")
    return 0


if __name__ == "__main__":
    import sys

    raise SystemExit(_cli(sys.argv[1:]))
