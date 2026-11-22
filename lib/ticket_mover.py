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
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path

try:  # package import
    from .ticket_writer import (
        TICKET_FILENAME_RE, _LIFECYCLE_SUBDIRS, iter_lifecycle_files, queue_root_of,
        require_ticket_in_verified_queue, require_verified_queue_root,
    )
    from .config_paths import (
        require_tickets_dir, resolve_config_path, resolve_queue_alias, resolve_tickets_dir, tm_config,
    )
    from . import bach_lease
    from .ticket_note import update_ticket_note
    from .session_provenance import (
        SessionProvenanceError, resolve_session_provenance,
    )
    from .routing_contract import (
        ClaimDeniedError,
        RoutingContractError,
        StaleContentError,
        claim_contract,
        complete_contract,
        content_hash,
        parse_ticket_name,
        normalize_expired_binding,
        record_receipt,
        recover_expired_claim,
        release_contract,
        update_fields,
    )
    from .ticket_freigabe import FreigabeError, freigabe_state, mark_freigabe, stamp_freigabe_id
    from .decision_readback import (
        DECISIONS_INDEX_ENV, INDEX_RELATIVE_PATH,
        decision_readback, readback_log_line, rebuild_hint,
    )
    from .ticket_audit import (
        _KNOWN_STATUS_CLUSTERS,
        _LEGACY_STATUS_ALIASES,
        _ROOT_ALIAS,
        _STATUS_CLUSTER_RE,
        _STATUS_LINE_RE,
    )
except ImportError:  # direct import from lib on sys.path
    from ticket_writer import (
        TICKET_FILENAME_RE, _LIFECYCLE_SUBDIRS, iter_lifecycle_files, queue_root_of,
        require_ticket_in_verified_queue, require_verified_queue_root,
    )
    from config_paths import (
        require_tickets_dir, resolve_config_path, resolve_queue_alias, resolve_tickets_dir, tm_config,
    )
    import bach_lease
    from ticket_note import update_ticket_note
    from session_provenance import (
        SessionProvenanceError, resolve_session_provenance,
    )
    from routing_contract import (
        ClaimDeniedError,
        RoutingContractError,
        StaleContentError,
        claim_contract,
        complete_contract,
        content_hash,
        parse_ticket_name,
        normalize_expired_binding,
        record_receipt,
        recover_expired_claim,
        release_contract,
        update_fields,
    )
    from ticket_freigabe import FreigabeError, freigabe_state, mark_freigabe, stamp_freigabe_id
    from decision_readback import (
        DECISIONS_INDEX_ENV, INDEX_RELATIVE_PATH,
        decision_readback, readback_log_line, rebuild_hint,
    )
    from ticket_audit import (
        _KNOWN_STATUS_CLUSTERS,
        _LEGACY_STATUS_ALIASES,
        _ROOT_ALIAS,
        _STATUS_CLUSTER_RE,
        _STATUS_LINE_RE,
    )

__all__ = [
    "ClaimDeniedError",
    "DuplicateTicketIdError",
    "FreigabeError",
    "HostIdentityError",
    "NestedLifecycleDestinationError",
    "TicketCollisionError",
    "backfill_leases",
    "claim_contract",
    "claim_contract_current_host",
    "claim_current_host",
    "maintain_lease_cache",
    "verify_claim_host",
    "complete_contract",
    "move_ticket",
    "normalize_expired_binding",
    "record_receipt",
    "recover_expired_claim",
    "DecisionReadbackError",
    "SessionProvenanceError",
    "freigabe_state",
    "mark_freigabe",
    "release_claim",
    "release_contract",
    "stamp_freigabe_id",
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
USER_SUBDIR = "USER"

# Vermerk im Tickettext, dass ein Agent gerade aktiv daran arbeitet. Das Feld
# ist entweder eine eigenstaendige, linksbuendige Zeile oder Teil einer
# linksbuendigen, datierten VERLAUF-Zeile
# ("2026-08-15  DELEGIERT_AN: claude-code@ASUS-GEI"). Ein eingeruecktes Zitat
# eines alten Markers ist dagegen nur Historie und darf weder einen Claim
# schuetzen noch beim erneuten Markieren ueberschrieben werden.
DELEGATION_MARKER_RE = re.compile(
    r"(?m)^(?:\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2}(?::\d{2})?)?[ \t]{2,})?"
    r"DELEGIERT_AN:[ \t]*(?P<agent>\S+)"
)

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


class DecisionReadbackError(RuntimeError):
    """An escalation to USER/ was refused by the decision readback gate.

    T-20260913-867541218. Either the register already answers this ticket, or
    a named index could not be read. Nothing is written; the ticket stays where
    it is. Acknowledge a hit explicitly (see ``acknowledged_decisions``) once it
    has been checked and found not to apply -- naming the ID is the evidence
    that somebody looked.
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
    reactivation_sources = {s.upper() for s in REACTIVATION_SOURCES}
    return (source.parent.name.upper() in reactivation_sources
            and dest_dir.name.upper() == REACTIVATION_TARGET.upper())


def queue_root(source: Path) -> Path:
    """Wurzel der Queue, ausgehend von einem Ticketpfad.

    Liegt das Ticket in einem Lebenszyklus-Ordner, ist die Wurzel dessen
    Elternordner; liegt es direkt in der Wurzel (INBOX-Alias), ist sie der
    eigene Elternordner. Die Regel steht in ``ticket_writer.queue_root_of``,
    damit Mover und Queue-Pruefung nie auseinanderlaufen.
    """
    return queue_root_of(source)


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
    lifecycle_map = {name.upper(): name for name in _LIFECYCLE_SUBDIRS if name}
    canonical = lifecycle_map.get(dest.name.upper())
    if canonical is None:
        return dest
    return queue_root(source) / canonical


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


def _normalize_host_token(raw: str) -> str:
    """Strip whitespace and a trailing `.local` mDNS suffix (case-insensitive)."""
    host = raw.strip()
    if host.casefold().endswith(".local"):
        host = host[:-6].strip()
    return host


def _discover_sync_root(queue_dir: Path | None = None, *, config: dict | None = None) -> Path | None:
    """Locate `.SYNC` root from env/config or queue ancestors when `--sync-root` is omitted."""
    cfg = tm_config() if config is None else config
    env_sync = os.environ.get("TICKET_MASTER_SYNC_ROOT")
    cfg_sync = cfg.get("sync_root") if isinstance(cfg, dict) else None
    for raw in (env_sync, cfg_sync):
        candidate = resolve_config_path(raw) if raw else None
        if candidate is not None and candidate.is_dir():
            return candidate
    if queue_dir is not None:
        resolved_queue = Path(os.path.abspath(queue_dir))
        for ancestor in (resolved_queue, *resolved_queue.parents):
            if (ancestor / "_config-state" / "snapshots").is_dir():
                return ancestor
            sync_dir = ancestor / ".SYNC"
            if (sync_dir / "_config-state" / "snapshots").is_dir():
                return sync_dir
    return None


def _resolve_lease_cli_host(
    explicit_host: str | None,
    *,
    queue_dir: Path | None = None,
    sync_root: Path | str | None = None,
    config: dict | None = None,
) -> str:
    """Resolve and normalize the host for `--backfill-leases` / `--renew-leases`.

    Strips a trailing `.local`. When `explicit_host` is omitted, uses the same
    `resolve_live_host()` verification as `--claim-current-host` instead of raw
    `socket.gethostname()`.
    """
    if explicit_host is not None:
        if sync_root is not None:
            host, _slot, _snapshot = verify_claim_host(explicit_host, sync_root=sync_root)
            return host
        normalized = _normalize_host_token(explicit_host)
        if not normalized or not re.fullmatch(r"[A-Za-z0-9_-]+", normalized):
            raise HostIdentityError(f"invalid host name: {explicit_host!r}")
        return normalized
    resolved_sync = (
        Path(sync_root) if sync_root is not None else _discover_sync_root(queue_dir, config=config)
    )
    if resolved_sync is None:
        raise HostIdentityError(
            "cannot resolve live host without --host or a verifiable .SYNC root "
            "(pass --sync-root or --host)"
        )
    host, _slot, _snapshot = resolve_live_host(resolved_sync)
    return host


def resolve_live_host(sync_root: Path | str) -> tuple[str, str, Path]:
    """Resolve host identity from the live OS plus the canonical self-slot."""
    root = Path(sync_root).expanduser().resolve()
    host = _normalize_host_token(socket.gethostname())
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
            if (
                isinstance(data, dict)
                and _normalize_host_token(str(data.get("host", ""))).casefold() == host.casefold()
            ):
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
    manifest_host = (
        _normalize_host_token(str(manifest_data.get("host", "")))
        if isinstance(manifest_data, dict)
        else ""
    )
    if (
        not isinstance(manifest_data, dict)
        or manifest_host.casefold() != host.casefold()
        or str(manifest_data.get("slot", "")).casefold() != slot.casefold()
    ):
        raise HostIdentityError(
            f"live host, snapshot and self-slot manifest disagree: {manifest}"
        )
    canonical_host = _normalize_host_token(str(data.get("host", ""))) or host
    return canonical_host, slot, snapshot


def verify_claim_host(claim_host: str, *, sync_root: Path | str) -> tuple[str, str, Path]:
    """Verify an asserted claim host for legacy and routing-v2 claim paths."""
    host, slot, snapshot = resolve_live_host(sync_root)
    normalized_claim = _normalize_host_token(claim_host)
    if not normalized_claim or normalized_claim.casefold() != host.casefold():
        raise HostIdentityError(
            f"asserted claim host {claim_host!r} does not match verified live host {host!r}"
        )
    return host, slot, snapshot


def _lease_take(source: Path, host: str, dry_run: bool) -> tuple[bool, str | None]:
    """Take the optional BACH task lease BEFORE a claim rename (lib/bach_lease.py).

    Off by default and a no-op for `dry_run`. Returns `(leased, fallback_reason)`.
    Raises (a RuntimeError subclass, so the CLI reports `REFUSED: ...`) on 409 or
    when enabled with `fail_open = false` and the lead is unusable.
    """
    if dry_run:
        return False, None
    return bach_lease.claim_lease_status(source, host=host)


def _record_fallback_verlauf(ticket: Path, *, host: str, reason: str) -> None:
    """Append the visible fallback note after a fail-open file-only claim."""
    worker = bach_lease.worker_id_for_host(host)
    line = f"{date.today().isoformat()} | {worker} | Claim ohne Lease (Fallback Dateibetrieb): {reason}"
    update_ticket_note(ticket, verlauf=line)


def _lease_warn(ticket_name: str, outcome: str, result_ref: str = "", *,
                reason_code: str | None = None, reason_detail: str | None = None) -> None:
    """Best-effort lease release; a failure only warns, the file operation stays done."""
    warning = bach_lease.release_lease(
        ticket_name, outcome, result_ref=result_ref, reason_code=reason_code, reason_detail=reason_detail)
    if warning:
        print(warning, file=sys.stderr)


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
            _leased, fallback_reason = _lease_take(source, host, dry_run)  # idempotent: renews a lease this host already holds
            if fallback_reason is not None:
                _record_fallback_verlauf(source, host=host, reason=fallback_reason)
            return source
        raise HostIdentityError(
            f"ticket is already claimed by {existing!r}; verified live host is {host!r}"
        )
    match = TICKET_FILENAME_RE.match(source.name)
    if not match:
        raise HostIdentityError(f"cannot build legacy claim name: {source.name}")
    slug = f"_{match.group('slug')}" if match.group("slug") else ""
    claimed_name = f"T-{match.group('date')}-{match.group('number')}{slug}.{host}.txt"
    leased, fallback_reason = _lease_take(source, host, dry_run)
    try:
        claimed = move_ticket(source, source.parent, new_name=claimed_name, dry_run=dry_run)
    except BaseException:
        if leased:
            _lease_warn(source.name, "return", reason_code="ticket_claim_rollback")  # the claim did not happen: hand the lease back
        elif fallback_reason is not None:
            bach_lease.clear_pending_lease(source.name)
        raise
    if fallback_reason is not None:
        _record_fallback_verlauf(claimed, host=host, reason=fallback_reason)
    return claimed


def claim_contract_current_host(ticket: Path | str, *, claim_host: str,
                                sync_root: Path | str, actor: str,
                                runner: str | None = None,
                                dry_run: bool = False) -> Path:
    """Fail-closed routing-v2 claim via claim_contract() after host verification.

    The CLI counterpart that `claim_current_host` deliberately refuses for v2
    tickets (which carry a ROUTING_SCHEMA/SYSTEM_LEDGER contract). Verifies the
    asserted claim host and self-slot, requires a routing-v2 ticket name, then
    delegates the target, binding and ledger gates to `claim_contract`. With
    `dry_run=True` nothing is renamed and the path the claim would take is
    returned.
    """
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
    if not parsed.is_v2:
        raise HostIdentityError(
            "legacy tickets must use --claim-current-host, not claim_contract()"
        )
    leased, fallback_reason = _lease_take(source, host, dry_run)
    try:
        claimed = claim_contract(source, host=host, actor=actor, runner=runner, dry_run=dry_run)
    except BaseException:
        if leased:
            _lease_warn(source.name, "return", reason_code="ticket_claim_rollback")
        elif fallback_reason is not None:
            bach_lease.clear_pending_lease(source.name)
        raise
    if fallback_reason is not None:
        _record_fallback_verlauf(claimed, host=host, reason=fallback_reason)
    return claimed


def _dest_cluster_name(dest_dir: Path) -> str:
    """Cluster token for a resolved destination dir, mirroring ticket_audit's
    folder_cluster (root/INBOX-alias vs. a named lifecycle folder)."""
    try:
        from .ticket_writer import _LIFECYCLE_SUBDIRS
    except ImportError:
        from ticket_writer import _LIFECYCLE_SUBDIRS
    lifecycle_map = {name.upper(): name for name in _LIFECYCLE_SUBDIRS if name}
    return lifecycle_map.get(dest_dir.name.upper(), _ROOT_ALIAS)


def suggested_status_line(value: str, dest_cluster: str) -> str | None:
    """Computes the STATUS value that pulls `value` forward onto
    `dest_cluster`, preserving its free text and dating it today. Shared by
    `_status_mismatch_warning` (warns, doesn't write) and
    status_drift_fixer.py (writes the SAFE subset, T-20260903-778818739).

    The subcategory is dropped, not carried over (T-20260903-778818739
    review): it names something about the SOURCE cluster -- "dependency" is
    a way of being BLOCKED, "cleanup"/"bug"/"lizenz" are ACTIONABLE work
    kinds -- none of them are a way of being SOLVED. A blind carry-over
    produced nonsense like "SOLVED/dependency".

    Returns None when there is nothing to change (no parsable cluster
    token, or it already matches dest_cluster).
    """
    token = _STATUS_CLUSTER_RE.match(value)
    if not token:
        return None
    cluster = _LEGACY_STATUS_ALIASES.get(token.group(1), token.group(1))
    if cluster not in _KNOWN_STATUS_CLUSTERS or cluster == dest_cluster:
        return None
    # Rebuild "<cluster> (seit <today>)<free text>", moving the date to the
    # move day per the ticket's own request -- not just swapping the leading
    # cluster token and leaving a stale "(seit ...)" behind.
    remainder = value[token.end():]
    subcat_match = re.match(r"(?:/[\w-]+)?\s*\(seit\s+[\d-]+\)(.*)$", remainder, re.DOTALL)
    free_text = subcat_match.group(1) if subcat_match else remainder
    return f"{dest_cluster} (seit {date.today().isoformat()}){free_text}"


def _status_mismatch_warning(target: Path, dest_cluster: str) -> str | None:
    """T-20260902-776693293: the prompt-only warning ("always pull the
    STATUS line forward after ticket_mover.py") is routinely missed -- 97 of
    125 live drift findings are exactly this one miss. move_ticket() is the
    only place that reliably sees both the old and the new location for
    every caller, so it checks here instead of relying on discipline.

    Deliberately non-mutating (rung 2 of this ticket's own recommendation,
    not rung 1): rewriting STATUS in place would need move_ticket()'s
    byte-for-byte source/target verification to work against two different
    byte strings instead of one, in the exact function this module exists
    to make crash-safe. A stderr warning with copy-ready replacement text
    gets the same 97-cases-prevented outcome without that risk.

    Returns None when there is nothing to warn about (no parsable STATUS
    line, or its cluster already matches the destination -- both handled
    identically and silently by ticket_audit.py's own status_drift()).
    """
    try:
        head = target.read_text(encoding="utf-8", errors="replace")[:4096]
    except OSError:
        return None
    match = _STATUS_LINE_RE.search(head)
    if match is None:
        return None
    value = match.group("value").strip()
    suggested = suggested_status_line(value, dest_cluster)
    if suggested is None:
        return None
    return (
        f"STATUS DRIFT: {target} now lives in {dest_cluster!r} but its STATUS "
        f"line still says {value!r}. Pull it forward, e.g.:\n"
        f"  STATUS:        {suggested}"
    )


def _decision_readback_gate(source: Path, dest_dir: Path,
                            acknowledged: list[str] | None,
                            index_path: Path | str | None) -> str:
    """Refuse an escalation to USER/ that the register already answers.

    T-20260913-867541218. Returns the protocol line to record; raises
    DecisionReadbackError instead of returning when the move must not happen.

    Three outcomes, and the difference between them is the whole point:
      * the register answers this ticket (a hit in a decided state)  -> refuse,
        unless the caller names that ID as checked-and-not-applicable;
      * an EXPECTED index (named, or a register sits next to the queue)
        cannot be read                                               -> refuse;
      * nothing found, or no index at all                            -> allow,
        but the line says so, so "not found" can never be read as "open".
    """
    acknowledged = list(acknowledged or [])
    result = decision_readback(
        source, queue_dir=dest_dir.parent, index_path=index_path)
    if result["state"] == "unavailable":
        raise DecisionReadbackError(
            f"decision index unreadable: {result['index']} "
            f"({result.get('error', '?')}). Escalating to USER/ would claim "
            f"'open' without having looked. {rebuild_hint(result['index'])}")
    unhandled = [hit for hit in result["blocking"]
                 if hit["id"] not in acknowledged]
    if unhandled:
        rendered = "; ".join(
            f"{hit['id']} [{hit['status']}] {hit['title']!r} in {hit['source']}"
            for hit in unhandled)
        # T-20260920-716303992: hit["id"] is the qualified "D-ID@source" form
        # (see decision_readback._entry_matches / entry["key"]), not the bare
        # D-ID used everywhere else in the ticket conventions. Spelling the
        # exact flags out here (instead of a generic "<ID>" placeholder) saves
        # the caller from guessing wrong and being refused again silently.
        flags = " ".join(f"--acknowledge-decision {hit['id']!r}" for hit in unhandled)
        raise DecisionReadbackError(
            f"the decision register already answers this ticket: {rendered}. "
            "Read it; if it does not apply, quote the id exactly as shown "
            f"above (including any '@source' suffix) with: {flags} "
            "-- the acknowledgement is then recorded in the ticket.")
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    return readback_log_line(result, stamp=stamp, acknowledged=acknowledged)


def move_ticket(source: Path | str, dest_dir: Path | str,
                release_claim: bool | None = None,
                new_name: str | None = None,
                dry_run: bool = False,
                expected_hash: str | None = None,
                acknowledged_decisions: list[str] | None = None,
                decisions_index: Path | str | None = None) -> Path:
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

    expected_hash (optional, T-20260903-965930417): der Hash aus einem
    frueheren routing_contract.read_for_update(source) -- wird die Quelle
    noch VOR dem Move gegen diesen Hash geprueft, verweigert bei Abweichung
    mit StaleContentError statt eine Entscheidung auf Basis veralteten
    Inhalts auszufuehren. Die bereits bestehende
    "source unveraendert seit dem Kopieren"-Pruefung unten schuetzt nur das
    schmale Fenster INNERHALB dieses Aufrufs; expected_hash schuetzt das
    Fenster zwischen dem Lesen des Aufrufers und diesem Aufruf.
    """
    source = Path(source)
    if not source.is_file():
        raise FileNotFoundError(f"move source does not exist or is not a file: {source}")
    src_queue = require_ticket_in_verified_queue(source)  # auch im dry_run: dieselben Pruefungen wie beim Schreiben
    # Blosser Clustername ("SOLVED") wird gegen die Queue-Wurzel der Quelle
    # aufgeloest, statt still einen Ordner im Arbeitsverzeichnis anzulegen.
    dest_dir = resolve_dest_dir(source, dest_dir)

    # Zielordner muss in einer verifizierten Queue liegen (T-20261107-907789834)
    try:
        dest_dir.resolve().relative_to(src_queue.resolve())
    except ValueError:
        try:
            from .ticket_writer import _LIFECYCLE_SUBDIRS, require_verified_queue_root
        except ImportError:
            from ticket_writer import _LIFECYCLE_SUBDIRS, require_verified_queue_root
        lifecycle_names = {sub.upper() for sub in _LIFECYCLE_SUBDIRS if sub}
        dest_root = dest_dir.parent if dest_dir.name.upper() in lifecycle_names else dest_dir
        require_verified_queue_root(dest_root)

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
    readback_line = None
    if (dest_dir.name.upper() == USER_SUBDIR.upper()
            and dest_dir.resolve() != source.parent.resolve()):
        readback_line = _decision_readback_gate(
            source, dest_dir, acknowledged_decisions, decisions_index)
    queued_id_gate = dest_dir.name.upper() == QUEUED_SUBDIR.upper()
    if queued_id_gate:
        _assert_unique_id_for_queued_move(source, dest_dir.parent)
    if target.exists():
        raise TicketCollisionError(
            f"move target already exists, refusing to overwrite: {target}"
        )

    if expected_hash is not None:
        # Text mode (universal newlines), matching read_for_update() -- a
        # raw-bytes hash would disagree with it on any file using \r\n line
        # endings even when nothing actually changed.
        current_text = source.read_text(encoding="utf-8")
        if content_hash(current_text) != expected_hash:
            raise StaleContentError(source, current_text)
    if readback_line and not dry_run:
        # Written into the SOURCE before the copy, so the protocol travels with
        # the ticket in the same atomic move instead of needing a second write
        # at the destination -- which would also break move_ticket's own
        # "source unchanged since the copy" readback guard.
        source.write_text(
            update_fields(source.read_text(encoding="utf-8"), {}, log=readback_line),
            encoding="utf-8")
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
    warning = _status_mismatch_warning(target, _dest_cluster_name(dest_dir))
    if warning:
        print(warning, file=sys.stderr)
    if dest_dir.resolve() != source.parent.resolve():
        cluster = _dest_cluster_name(dest_dir)
        outcome = bach_lease.outcome_for_cluster(cluster)
        if outcome:  # a move that stays in QUEUED keeps the lease
            _lease_warn(source.name, outcome, f"{cluster}/{target.name}",
                        reason_code="ticket_moved", reason_detail=cluster)
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
    require_ticket_in_verified_queue(ticket)

    try:
        parsed = parse_ticket_name(ticket.name)
    except RoutingContractError:
        parsed = None
    if parsed and parsed.is_v2 and parsed.claim:
        freed_v2 = release_contract(ticket, host=parsed.claim)
        _lease_warn(ticket.name, "return", reason_code="ticket_claim_released")
        return freed_v2
    freed = ticket.parent / unclaimed_name(ticket.name)
    if freed == ticket:
        return ticket
    if freed.exists():
        raise TicketCollisionError(
            f"unclaimed name already taken, refusing to overwrite: {freed}"
        )
    released = move_ticket(ticket, ticket.parent, release_claim=True)
    _lease_warn(ticket.name, "return", reason_code="ticket_claim_released")
    return released


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


def mark_delegated(ticket: Path | str, agent: str, *,
                   session: str | None = None,
                   session_host: str | None = None) -> None:
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
    require_ticket_in_verified_queue(ticket)
    text = ticket.read_text(encoding="utf-8", errors="replace")
    marker = f"DELEGIERT_AN: {agent}"
    provenance = resolve_session_provenance(
        session, agent=agent, host=session_host,
    )
    header = f"SESSION:       {provenance.value}"
    if re.search(r"(?m)^SESSION:\s*.*$", text):
        text = re.sub(r"(?m)^SESSION:\s*.*$", header, text, count=1)
    else:
        priority = re.search(r"(?m)^PRIORITAET:\s*.*$|^PRIORITY:\s*.*$", text)
        if priority:
            text = text[:priority.end()] + "\n" + header + text[priority.end():]
        else:
            text = header + "\n" + text
    delegation_match = DELEGATION_MARKER_RE.search(text)
    if delegation_match:
        # Nur das Agent-Token austauschen. So bleiben der optionale
        # VERLAUF-Zeitstempel, Abstaende und ein nachfolgender Statusvermerk
        # bytegenau erhalten; historische eingerueckte Zitate sind vom
        # zeilenverankerten Muster ausgeschlossen.
        text = (
            text[:delegation_match.start("agent")]
            + agent
            + text[delegation_match.end("agent"):]
        )
    else:
        text = text.rstrip("\n") + f"\n{marker}\n"
    if provenance.stamp not in text:
        text = text.rstrip("\n") + f"\n{provenance.stamp}\n"
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
    base = require_verified_queue_root(Path(base))
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


def _ticket_locations_by_id(base: Path) -> dict[str, list[tuple[str, Path]]]:
    """Map exact `T-YYYYMMDD-...` IDs to `(cluster, path)` across all lifecycle folders."""
    locations: dict[str, list[tuple[str, Path]]] = {}
    for sub in _LIFECYCLE_SUBDIRS:
        directory = base / sub if sub else base
        if not directory.is_dir():
            continue
        cluster = sub or "INBOX"
        for entry in sorted(directory.iterdir()):
            if not entry.is_file():
                continue
            if not TICKET_FILENAME_RE.match(entry.name):
                try:
                    parsed = parse_ticket_name(entry.name)
                except ValueError:
                    continue
                if not parsed.is_v2:
                    continue
            ticket_id = bach_lease.ticket_id_of(entry.name)
            if ticket_id:
                locations.setdefault(ticket_id, []).append((cluster, entry))
    return locations


HYGIENE_INACTIVE_SUBDIRS = ("SOLVED", "BLOCKED", "WAITING", "USER", "PARKED")
HYGIENE_EXPECTED_SUBDIRS = ("INBOX", "ACTIONABLE", "QUEUED", *HYGIENE_INACTIVE_SUBDIRS)


def maintain_lease_cache(tickets_dir: Path | str, *, host: str | None = None,
                         config: dict | None = None,
                         client: bach_lease.BachLeaseClient | None = None) -> list[tuple[str, str, str]]:
    """Fail-safe cache hygiene for `--renew-leases`.

    Releases an own cached lease only when the ticket is positively found in an inactive
    folder (`SOLVED`, `BLOCKED`, `WAITING`, `USER`, `PARKED`) of the resolved queue.
    If the ticket is not found anywhere, the cache entry is kept and reported as
    `"UNKNOWN <ticket> (kept)"`. If the queue directory does not exist or has none of
    the expected lifecycle folders, hygiene aborts with a clear message and releases nothing.
    """
    cfg = bach_lease.load_config(config)
    if not cfg["enabled"]:
        return []
    raw_path = Path(tickets_dir)
    try:
        candidate = resolve_queue_alias(raw_path)
    except (RuntimeError, ValueError, OSError) as exc:
        return [("-", "aborted", f"cache hygiene aborted: {exc}")]
    if not candidate.is_dir():
        return [(
            "-",
            "aborted",
            f"cache hygiene aborted: queue directory does not exist: {candidate}",
        )]
    if not any((candidate / sub).is_dir() for sub in HYGIENE_EXPECTED_SUBDIRS):
        expected = ", ".join(HYGIENE_EXPECTED_SUBDIRS)
        return [(
            "-",
            "aborted",
            f"cache hygiene aborted: queue directory {candidate} has none of the expected "
            f"lifecycle folders ({expected}); releasing nothing",
        )]
    try:
        base = require_verified_queue_root(candidate)
    except (ValueError, RuntimeError, OSError) as exc:
        return [("-", "aborted", f"cache hygiene aborted: {exc}")]
    try:
        state = bach_lease.load_state(cfg)
    except bach_lease.LeaseError as exc:
        return [("-", "error", bach_lease._safe_fallback_reason(exc, cfg))]
    if not state:
        return []
    own_worker = (
        bach_lease.worker_id_for_host(_normalize_host_token(host)).casefold()
        if host and _normalize_host_token(host)
        else None
    )
    locations = _ticket_locations_by_id(base)
    results: list[tuple[str, str, str]] = []
    for ticket_id in sorted(state):
        entry = state.get(ticket_id)
        if (
            own_worker is not None
            and isinstance(entry, dict)
            and entry.get("worker_id")
            and str(entry["worker_id"]).casefold() != own_worker
        ):
            continue
        locs = locations.get(ticket_id, [])
        if not locs:
            results.append((ticket_id, "unknown", f"UNKNOWN {ticket_id} (kept)"))
            continue
        if any(cluster in WORKING_SUBDIRS for cluster, _path in locs):
            continue
        inactive = [(c, p) for c, p in locs if c in HYGIENE_INACTIVE_SUBDIRS]
        if not inactive:
            continue
        solved_loc = next(((c, p) for c, p in inactive if c == "SOLVED"), None)
        cluster, ticket_path = solved_loc or inactive[0]
        outcome = bach_lease.outcome_for_cluster(cluster) or "return"
        result_ref = f"{cluster}/{ticket_path.name}"
        action, message = bach_lease.clean_inactive_lease(
            ticket_id, outcome, result_ref=result_ref,
            reason_code="ticket_moved", reason_detail=cluster,
            config=config, client=client,
        )
        if action != "absent":
            results.append((ticket_id, action, message))
    return results


def backfill_leases(tickets_dir: Path | str | None = None, *, host: str | None = None,
                    sync_root: Path | str | None = None,
                    dry_run: bool = False, config: dict | None = None,
                    client: bach_lease.BachLeaseClient | None = None,
                    already_valid: set[str] | None = None) -> list[tuple[str, str, str]]:
    """Acquire leases for every own claim (case-insensitive `host` suffix, `.local` stripped)
    in ACTIONABLE and QUEUED without a valid cached lease or marked `pending_lease`.

    When `host` is omitted, resolves the live host via `resolve_live_host()` (same host
    verification as `--claim-current-host`). Returns `(ticket_id, status, detail)` where
    `status` is one of `"LEASED"`, `"HELD-BY"`, `"SKIPPED"`, or `"FAILED"`.
    """
    if host is not None and not _normalize_host_token(host):
        raise ValueError("host is required for backfill_leases")
    base = require_verified_queue_root(require_tickets_dir(tickets_dir, config=config))
    resolved_host = _resolve_lease_cli_host(
        host, queue_dir=base, sync_root=sync_root, config=config
    )
    cfg = bach_lease.load_config(config)
    if not cfg["enabled"]:
        return []
    try:
        state = bach_lease.load_state(cfg)
    except bach_lease.LeaseError as exc:
        return [("-", "FAILED", bach_lease._safe_fallback_reason(exc, cfg))]

    results: list[tuple[str, str, str]] = []
    seen_ids: set[str] = set()
    validated = already_valid or set()
    target_host = resolved_host.casefold()

    for folder in ("ACTIONABLE", "QUEUED"):
        directory = base / folder
        if not directory.is_dir():
            continue
        for entry in sorted(directory.iterdir()):
            if not entry.is_file():
                continue
            suffix = claim_suffix(entry.name)
            if suffix is None or _normalize_host_token(suffix).casefold() != target_host:
                continue
            ticket_id = bach_lease.ticket_id_of(entry.name)
            if ticket_id is None:
                continue
            if ticket_id in seen_ids:
                results.append((ticket_id, "SKIPPED", "duplicate ticket id in active folders"))
                continue
            seen_ids.add(ticket_id)

            cached = state.get(ticket_id)
            pending = bach_lease.is_pending_lease_entry(cached)
            has_cap = bach_lease.has_lease_capability(cached)

            if has_cap and ticket_id in validated:
                results.append((ticket_id, "SKIPPED", "already holds valid lease"))
                continue

            if dry_run:
                if has_cap:
                    results.append((ticket_id, "SKIPPED", "already cached (dry-run)"))
                elif pending:
                    results.append((ticket_id, "SKIPPED", "dry-run (pending_lease; would acquire lease)"))
                else:
                    results.append((ticket_id, "SKIPPED", "dry-run (would acquire lease)"))
                continue

            try:
                lease_client = bach_lease._client(cfg, client, resolved_host)
                if has_cap and lease_client.renew(ticket_id):
                    results.append((ticket_id, "SKIPPED", "already holds valid lease"))
                    continue
                lease_client.acquire(ticket_id, bach_lease._title_of(entry, ticket_id))
                results.append((ticket_id, "LEASED", ""))
            except bach_lease.LeaseConflictError as exc:
                holder = exc.holder or str(exc)
                results.append((ticket_id, "HELD-BY", holder))
            except bach_lease.LeaseError as exc:
                results.append((ticket_id, "FAILED", bach_lease._safe_fallback_reason(exc, cfg)))

    return results


def _format_backfill_line(ticket_id: str, status: str, detail: str) -> str:
    if status == "LEASED":
        return f"{ticket_id}: LEASED"
    return f"{ticket_id}: {status} {detail}".rstrip()


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
    parser.add_argument("--renew-leases", action="store_true",
                        help=("Renew every BACH task lease this host holds, clean stale cache "
                              "entries for solved/inactive tickets, and backfill own claims at "
                              "the end (needs bach_lease.enabled in the config; see lib/bach_lease.py). "
                              "Run it at least every 15 minutes while working on claimed tickets."))
    parser.add_argument("--backfill-leases", action="store_true",
                        help=("Acquire leases for every own claim (case-insensitive --host suffix, "
                              "trailing .local stripped; resolves live host via resolve_live_host "
                              "when --host is omitted) in ACTIONABLE and QUEUED without a valid "
                              "cached lease or marked pending_lease."))
    parser.add_argument("--release-session", action="store_true",
                        help=("Release this host's claims at regular session end. "
                              "ACTIONABLE always; QUEUED only reported unless "
                              "--include-queued is given (T-20260815-205002196). "
                              "Requires --host."))
    parser.add_argument("--host",
                        help="Host suffix (exact match) for claim/release/backfill/renew operations, e.g. WORKSTATION.")
    parser.add_argument("--tickets-dir",
                        help="Ticket queue root (for --release-session, --backfill-leases, --renew-leases).")
    parser.add_argument("--include-queued", action="store_true",
                        help=("Also release QUEUED claims (not just report them). "
                              "Still never releases an actively delegated ticket "
                              "(fresh DELEGIERT_AN marker)."))
    parser.add_argument("--dry-run", action="store_true",
                        help=("Preview a single-ticket move, claim, --backfill-leases, or "
                              "--release-session; change nothing."))
    parser.add_argument("--mark-delegated", metavar="TICKET",
                        help="Write a DELEGIERT_AN marker into TICKET. Requires --agent.")
    parser.add_argument("--claim-current-host", metavar="TICKET",
                        help=("Claim TICKET after live host + self-slot verification. "
                              "Requires --host and --sync-root."))
    parser.add_argument("--claim-contract-current-host", metavar="TICKET",
                        help=("Claim a routing-v2 TICKET via claim_contract() after live "
                              "host + self-slot verification. Requires --host, --sync-root "
                              "and --agent; optional --runner and --dry-run."))
    parser.add_argument("--verify-claim-host", metavar="HOST",
                        help=("Verify an asserted host before any legacy or routing-v2 "
                              "claim. Requires --sync-root."))
    parser.add_argument("--sync-root",
                        help="Canonical .SYNC root for --claim-current-host.")
    parser.add_argument("--agent",
                        help="Agent identity for --mark-delegated, e.g. claude-code@ASUS-GEI.")
    parser.add_argument("--runner",
                        help="Optional Clutch execution selector for --claim-contract-current-host.")
    parser.add_argument("--session",
                        help="Parent session ID for --mark-delegated; explicit value wins over provider runtime variables.")
    parser.add_argument("--session-host",
                        help="Session host; defaults to the host in --agent or runtime identity.")
    # T-20260913-744071825. Deliberately NOT called --mark-released: in this
    # module "release" already means handing back the host claim
    # (release_claim/release_contract/release_claims). --mark-freigabe matches
    # the STATUS subcategory `freigabe`, which both doc languages use untranslated.
    parser.add_argument("--freigabe-status", metavar="TICKET",
                        help="Report the hash-bound approval state of TICKET; writes nothing.")
    parser.add_argument("--stamp-freigabe-id", metavar="TICKET",
                        help=("Write the FREIGABE_ID of TICKET's 'ZUR FREIGABE' section "
                              "so the user can quote it. Idempotent."))
    parser.add_argument("--mark-freigabe", metavar="TICKET",
                        help=("Record the user's approval for TICKET. Requires "
                              "--freigabe-id and --agent; fail-closed on any mismatch."))
    parser.add_argument("--freigabe-id",
                        help="The FREIGABE_ID the user quoted, for --mark-freigabe.")
    # T-20260913-867541218: a code gate, not a prompt gate -- it binds every
    # consumer of this module, not one agent's discipline.
    parser.add_argument("--decisions-index",
                        help=(f"Decision index JSON for the USER/ readback gate. "
                              f"Default: <queue>/../{INDEX_RELATIVE_PATH.as_posix()}, "
                              f"or ${DECISIONS_INDEX_ENV}. Naming one that cannot be "
                              "read refuses the move."))
    parser.add_argument("--acknowledge-decision", action="append", metavar="D-ID",
                        default=[],
                        help=("A register hit that was read and found not to apply. "
                              "Repeatable; recorded in the ticket."))
    parser.add_argument("--decision-readback", metavar="TICKET",
                        help="Report the register readback for TICKET; writes nothing.")
    args = parser.parse_args(argv)

    if args.decision_readback:
        if args.source or args.dest_dir or args.backfill_leases or args.renew_leases:
            parser.error("--decision-readback cannot be combined with a move")
        # queue_root_of makes the path absolute and knows root-level tickets;
        # Path(...).parent.parent of a relative path was '.' (T-20261107-456970984).
        queue = Path(args.tickets_dir) if args.tickets_dir else queue_root_of(args.decision_readback)
        try:
            result = decision_readback(args.decision_readback, queue_dir=queue,
                                       index_path=args.decisions_index)
        except OSError as exc:
            print(f"REFUSED: {exc}")
            return 1
        print(f"DECISION READBACK {result['state'].upper()}: {args.decision_readback}")
        print(f"  index: {result['index'] or '-'}")
        if result["state"] == "unavailable":
            print(f"  error: {result.get('error', '?')}")
            print(f"  fix:   {rebuild_hint(result['index'])}")
        print(f"  keys:  {', '.join(result['keys']) or '-'}")
        for hit in result["hits"]:
            mark = "BLOCKING" if hit in result["blocking"] else "info"
            print(f"  [{mark}] {hit['id']} [{hit['status']}] {hit['source']}: {hit['title']}")
        if not result["hits"]:
            print("  hits:  none")
        return 1 if result["state"] == "unavailable" else 0

    freigabe_ops = [args.freigabe_status, args.stamp_freigabe_id, args.mark_freigabe]
    if sum(1 for value in freigabe_ops if value) > 1:
        parser.error("the freigabe operations are mutually exclusive")
    if any(freigabe_ops) and (args.source or args.dest_dir or args.release_session
                              or args.claim_current_host
                              or args.claim_contract_current_host
                              or args.mark_delegated
                              or args.backfill_leases
                              or args.renew_leases):
        parser.error("a freigabe operation cannot be combined with another operation")

    if args.freigabe_status:
        try:
            state = freigabe_state(args.freigabe_status)
        except OSError as exc:
            print(f"REFUSED: {exc}")
            return 1
        print(f"FREIGABE {state['state'].upper()}: {args.freigabe_status}")
        print(f"  current_id: {state['current_id'] or '-'}")
        print(f"  stamped_id: {state['stamped_id'] or '-'}")
        print(f"  granted:    {state['granted'] or '-'}")
        return 0

    if args.stamp_freigabe_id:
        try:
            stamped = stamp_freigabe_id(args.stamp_freigabe_id)
        except (FreigabeError, OSError) as exc:
            print(f"REFUSED: {exc}")
            return 1
        print(f"FREIGABE_ID: {stamped} ({args.stamp_freigabe_id})")
        return 0

    if args.mark_freigabe:
        if not args.freigabe_id:
            parser.error("--mark-freigabe requires --freigabe-id")
        if not args.agent:
            parser.error("--mark-freigabe requires --agent (who recorded the approval)")
        try:
            granted = mark_freigabe(
                args.mark_freigabe, freigabe_id_value=args.freigabe_id, by=args.agent)
        except (FreigabeError, OSError) as exc:
            print(f"REFUSED: {exc}")
            return 1
        print(f"FREIGABE ERTEILT: {granted} ({args.mark_freigabe})")
        return 0

    if args.verify_claim_host:
        if (args.claim_current_host or args.claim_contract_current_host
                or args.mark_delegated or args.release_session
                or args.backfill_leases or args.renew_leases
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

    if args.backfill_leases:
        if (args.renew_leases or args.claim_current_host or args.claim_contract_current_host
                or args.mark_delegated or args.release_session or args.source or args.dest_dir):
            parser.error("--backfill-leases cannot be combined with another operation")
        try:
            queue_dir = require_verified_queue_root(require_tickets_dir(args.tickets_dir))
            host = _resolve_lease_cli_host(
                args.host, queue_dir=queue_dir, sync_root=args.sync_root
            )
        except (HostIdentityError, ValueError, RuntimeError, OSError) as exc:
            print(f"REFUSED: {exc}")
            return 1
        if not bach_lease.load_config()["enabled"]:
            print("bach_lease is not enabled; nothing to backfill")
            return 0
        backfill_results = backfill_leases(
            queue_dir, host=host, dry_run=args.dry_run
        )
        for ticket_id, status, detail in backfill_results:
            print(_format_backfill_line(ticket_id, status, detail))
        return 1 if any(status == "FAILED" for _, status, _ in backfill_results) else 0

    if args.renew_leases:
        if (args.backfill_leases or args.claim_current_host or args.claim_contract_current_host
                or args.mark_delegated or args.release_session or args.source or args.dest_dir
                or args.dry_run):
            parser.error("--renew-leases cannot be combined with another operation")
        raw_queue_dir: Path | None = None
        if args.tickets_dir is not None:
            try:
                raw_queue_dir = require_tickets_dir(args.tickets_dir)
            except (ValueError, RuntimeError, OSError) as exc:
                print(f"REFUSED: {exc}")
                return 1
        else:
            raw_queue_dir = resolve_tickets_dir()
        if not bach_lease.load_config()["enabled"]:
            print("bach_lease is not enabled; nothing to renew")
            return 0
        hygiene_results: list[tuple[str, str, str]] = []
        hygiene_aborted = False
        if raw_queue_dir is not None:
            hygiene_results = maintain_lease_cache(raw_queue_dir, host=args.host)
            for ticket_id, action, message in hygiene_results:
                if action == "unknown":
                    print(message)
                elif action == "aborted":
                    hygiene_aborted = True
                    print(f"HYGIENE ABORTED: {message}")
                else:
                    label = "RELEASED" if action == "released" else (
                        "REMOVED" if action == "removed" else "NOT RELEASED"
                    )
                    print(f"{label}: {ticket_id} -- {message}")
        results = bach_lease.renew_leases()
        for ticket_id, ok, message in results:
            print(f"{'RENEWED' if ok else 'NOT RENEWED'}: {ticket_id} -- {message}")
        print(f"{sum(1 for _, ok, _ in results if ok)}/{len(results)} lease(s) renewed")
        backfill_results = []
        host_failed = False
        if raw_queue_dir is not None and not hygiene_aborted:
            try:
                queue_dir = require_verified_queue_root(raw_queue_dir)
                host = _resolve_lease_cli_host(
                    args.host, queue_dir=queue_dir, sync_root=args.sync_root
                )
            except (HostIdentityError, ValueError, RuntimeError, OSError) as exc:
                print(f"REFUSED: {exc}")
                host_failed = True
            else:
                already_valid = {tid for tid, ok, _ in results if ok}
                backfill_results = backfill_leases(
                    queue_dir, host=host, already_valid=already_valid
                )
                for ticket_id, status, detail in backfill_results:
                    print(_format_backfill_line(ticket_id, status, detail))
        recovered = {tid for tid, status, _ in backfill_results if status == "LEASED"}
        failed_renew = any(not ok and tid not in recovered for tid, ok, _ in results)
        failed_hygiene = any(action in ("error", "aborted") for _, action, _ in hygiene_results)
        failed_backfill = any(status == "FAILED" for _, status, _ in backfill_results)
        return 1 if (failed_renew or failed_hygiene or failed_backfill or host_failed) else 0

    if args.claim_current_host:
        if (args.claim_contract_current_host or args.mark_delegated
                or args.release_session or args.source or args.dest_dir):
            parser.error("--claim-current-host cannot be combined with another operation")
        if not args.host or not args.sync_root:
            parser.error("--claim-current-host requires --host and --sync-root")
        try:
            target = claim_current_host(
                args.claim_current_host, claim_host=args.host,
                sync_root=args.sync_root, dry_run=args.dry_run,
            )
        except (HostIdentityError, TicketCollisionError, FileNotFoundError,
                RuntimeError, ValueError) as exc:
            print(f"REFUSED: {exc}")
            return 1
        label = "WOULD CLAIM" if args.dry_run else "CLAIMED"
        print(f"{label}: {target}")
        return 0

    if args.claim_contract_current_host:
        if args.mark_delegated or args.release_session or args.source or args.dest_dir:
            parser.error("--claim-contract-current-host cannot be combined with another operation")
        if not args.host or not args.sync_root or not args.agent:
            parser.error("--claim-contract-current-host requires --host, --sync-root and --agent")
        try:
            target = claim_contract_current_host(
                args.claim_contract_current_host, claim_host=args.host,
                sync_root=args.sync_root, actor=args.agent, runner=args.runner,
                dry_run=args.dry_run,
            )
        except (HostIdentityError, RoutingContractError, TicketCollisionError,
                FileNotFoundError, RuntimeError, ValueError) as exc:
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
            mark_delegated(
                args.mark_delegated, args.agent,
                session=args.session, session_host=args.session_host)
        except (FileNotFoundError, SessionProvenanceError) as exc:
            print(f"REFUSED: {exc}")
            return 1
        print(f"MARKED: {args.mark_delegated} DELEGIERT_AN: {args.agent}")
        return 0

    if args.release_session:
        if not args.host or not args.tickets_dir:
            parser.error("--release-session requires --host and --tickets-dir")
        args.tickets_dir = resolve_queue_alias(args.tickets_dir)
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
        target = move_ticket(args.source, args.dest_dir, dry_run=args.dry_run,
                             acknowledged_decisions=args.acknowledge_decision,
                             decisions_index=args.decisions_index)
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
