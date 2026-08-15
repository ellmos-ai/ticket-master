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

Same-host multi-agent races (the actual failure mode here) are a purely
local filesystem question: two agents on one host both hit the same local
OneDrive-mirrored folder in real time, so O_EXCL's atomicity is authoritative
immediately — no cloud sync delay is involved. Sync delay only matters for
cross-host collisions, which the existing <HOST> filename suffix already
handles separately.
"""

from __future__ import annotations

import contextlib
import os
from pathlib import Path

from ticket_writer import TICKET_FILENAME_RE

# Ordner, in denen ein Host-Suffix "ich arbeite gerade daran" bedeutet. NUR
# hier gibt die Sitzungs-Rueckgabe (release_claims) Claims frei.
WORKING_SUBDIRS = ("QUEUED", "ACTIONABLE")

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


def unclaimed_name(filename: str) -> str:
    """Dateiname ohne Claim-Suffix ("T-DATE-NN[_slug].txt").

    Ein beschreibender Slug bleibt erhalten -- freigegeben wird der Claim,
    nicht die Benennung. Ist der Name kein Ticketname oder ohnehin schon
    unclaimed, kommt er unveraendert zurueck.
    """
    m = TICKET_FILENAME_RE.match(filename)
    if not m or not m.group("suffix"):
        return filename
    slug = f"_{m.group('slug')}" if m.group("slug") else ""
    return f"T-{m.group('date')}-{m.group('number')}{slug}.txt"


def claim_suffix(filename: str) -> str | None:
    """Host-/Claim-Suffix eines Ticketnamens, oder None wenn unclaimed."""
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
    from ticket_writer import _LIFECYCLE_SUBDIRS
    if dest.name not in _LIFECYCLE_SUBDIRS:
        return dest
    return queue_root(source) / dest.name


def move_ticket(source: Path | str, dest_dir: Path | str,
                release_claim: bool | None = None,
                new_name: str | None = None) -> Path:
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
    """
    source = Path(source)
    if not source.is_file():
        raise FileNotFoundError(f"move source does not exist or is not a file: {source}")
    # Blosser Clustername ("SOLVED") wird gegen die Queue-Wurzel der Quelle
    # aufgeloest, statt still einen Ordner im Arbeitsverzeichnis anzulegen.
    dest_dir = resolve_dest_dir(source, dest_dir)

    dest_dir.mkdir(parents=True, exist_ok=True)

    if new_name:
        target_name = new_name
    else:
        if release_claim is None:
            release_claim = _should_release_on_move(source, dest_dir)
        target_name = unclaimed_name(source.name) if release_claim else source.name
        if release_claim and (dest_dir / target_name).exists():
            target_name = source.name

    target = dest_dir / target_name
    if target.exists():
        raise TicketCollisionError(
            f"move target already exists, refusing to overwrite: {target}"
        )

    data = source.read_bytes()
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

    freed = ticket.parent / unclaimed_name(ticket.name)
    if freed == ticket:
        return ticket
    if freed.exists():
        raise TicketCollisionError(
            f"unclaimed name already taken, refusing to overwrite: {freed}"
        )
    return move_ticket(ticket, ticket.parent, release_claim=True)


def release_claims(base: Path | str, *, host: str,
                   folders: tuple[str, ...] = WORKING_SUBDIRS,
                   dry_run: bool = False,
                   report_refused: bool = False):
    """Sitzungs-Rueckgabe: gibt alle Claims von `host` in den Arbeitsordnern
    frei, damit ein regulaer beendetes Sitzungsende keine Tickets fuer andere
    Hosts blockiert.

    `host` ist ein PFLICHT-Argument ohne Default. Bewusst kein automatisches
    COMPUTERNAME: der Bestand fuehrt fuer dieselbe Maschine historisch zwei
    Identitaeten (ASUS-GEI und LAPTOP), und eine falsch geratene Identitaet
    wuerde entweder nichts freigeben oder -- schlimmer -- fremde Claims
    anfassen. Verglichen wird deshalb exakt und ohne Normalisierung; das
    Zusammenfuehren veralteter Host-Identitaeten ist ein eigener, benannter
    Vorgang und passiert nicht still in der Rueckgabe.

    Standard-`folders` sind QUEUED und ACTIONABLE. In SOLVED/USER/BLOCKED/
    WAITING/PARKED bleibt der Suffix stehen: dort ist er Herkunft, nicht
    Arbeitsanspruch. Diese Tickets werden stattdessen bei ihrer Reaktivierung
    frei (siehe move_ticket / REACTIVATION_SOURCES).

    Ein einzelnes blockiertes Ticket bricht den Lauf nicht ab -- sonst bliebe
    eine ganze Sitzung geclaimed, weil ein Name belegt war. Mit
    report_refused=True kommt (freigegeben, verweigert) zurueck, sonst nur
    die Liste der freigegebenen Pfade.
    """
    base = Path(base)
    freed: list[Path] = []
    refused: list[tuple[Path, str]] = []

    for folder in folders:
        directory = base / folder
        if not directory.is_dir():
            continue
        for entry in sorted(directory.iterdir()):
            if not entry.is_file() or claim_suffix(entry.name) != host:
                continue
            if dry_run:
                freed.append(directory / unclaimed_name(entry.name))
                continue
            try:
                freed.append(release_claim(entry))
            except (TicketCollisionError, RuntimeError, OSError) as exc:
                refused.append((entry, str(exc)))

    return (freed, refused) if report_refused else freed


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
                        help=("Release this host's claims in QUEUED/ACTIONABLE "
                              "(regular session end). Requires --host."))
    parser.add_argument("--host",
                        help="Claim suffix to release, e.g. ASUS-GEI. Exact match.")
    parser.add_argument("--tickets-dir",
                        help="Ticket queue root (for --release-session).")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what --release-session would free, change nothing.")
    args = parser.parse_args(argv)

    if args.release_session:
        if not args.host or not args.tickets_dir:
            parser.error("--release-session requires --host and --tickets-dir")
        freed, refused = release_claims(
            args.tickets_dir, host=args.host, dry_run=args.dry_run,
            report_refused=True)
        label = "WOULD RELEASE" if args.dry_run else "RELEASED"
        for path in freed:
            print(f"{label}: {path}")
        for path, reason in refused:
            print(f"REFUSED: {path} -- {reason}")
        print(f"{label} {len(freed)} claim(s) for host {args.host}"
              + (f", {len(refused)} refused" if refused else ""))
        return 1 if refused else 0

    if not args.source or not args.dest_dir:
        parser.error("source and dest_dir are required unless --release-session is given")
    try:
        target = move_ticket(args.source, args.dest_dir)
    except (TicketCollisionError, FileNotFoundError, RuntimeError) as exc:
        print(f"REFUSED: {exc}")
        return 1
    print(f"MOVED: {target}")
    return 0


if __name__ == "__main__":
    import sys

    raise SystemExit(_cli(sys.argv[1:]))
