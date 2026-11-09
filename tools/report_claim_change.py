#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""report_claim_change.py — Claim-Änderungen an ticket-master melden statt Desktop-Ablage.

Entwickelt für Ticket T-20261102-256001001 (Nutzerentscheidung vom 25.10.2026):
1. Je Claim-Änderung exakt ein Ticket mit:
   - Betroffenem Projekt
   - Altem Wortlaut / Stand
   - Neuem Wortlaut / Stand
   - Fundstelle (Quelle / Stelle)
   - Anlass der Änderung
2. Benachrichtigung an Lukas bzw. persönlichen Assistenten im Tickettext verankert.
3. Vollständiger Verzicht auf Desktop-Dateien (keine CLAIM_LEVEL_CHANGE_NOTIFICATION auf Desktop).
4. Anbindung an ticket-master Routing Schema v2 über lib/ticket_writer.py (--systems-registry).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Sequence


def _find_lib_dir() -> Path:
    """Findet das lib-Verzeichnis von ticket-master."""
    # 1. Relativ zu dieser Datei (falls in <repo>/tools/ oder <repo>/bin/)
    self_path = Path(__file__).resolve()
    candidate = self_path.parent.parent / "lib"
    if (candidate / "ticket_writer.py").is_file():
        return candidate

    # 2. In OneDrive _scripts (wo ticket_writer.py als Single-File liegt)
    scripts_dir = self_path.parent
    if (scripts_dir / "ticket_writer.py").is_file():
        return scripts_dir

    # 3. Standard-Pfade
    standard_repo_lib = Path(r"C:\_Local_DEV\repos\ticket-master\lib")
    if (standard_repo_lib / "ticket_writer.py").is_file():
        return standard_repo_lib

    standard_scripts = Path.home() / "OneDrive" / "_scripts"
    if (standard_scripts / "ticket_writer.py").is_file():
        return standard_scripts

    raise RuntimeError("lib-Verzeichnis mit ticket_writer.py konnte nicht lokalisiert werden.")


_LIB_DIR = _find_lib_dir()
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

try:
    import ticket_writer
except ImportError as exc:
    raise ImportError(f"ticket_writer konnte aus {_LIB_DIR} nicht importiert werden: {exc}") from exc


def format_claim_ticket_body(
    *,
    project: str,
    old_claim: str,
    new_claim: str,
    location: str,
    reason: str,
    notify: bool = True,
) -> str:
    """Erstellt den standardisierten Ticket-Body für eine Claim-Änderung."""
    lines = [
        "## Gemeldete wissenschaftliche Claim-Änderung",
        f"- **Projekt:** {project.strip()}",
        f"- **Fundstelle (Quelle/Stelle):** {location.strip()}",
        f"- **Alter Wortlaut / Stand:** {old_claim.strip()}",
        f"- **Neuer Wortlaut / Stand:** {new_claim.strip()}",
        f"- **Anlass der Änderung:** {reason.strip()}",
        "- **Meldungsweg:** report_claim_change.py via ticket_writer (Routing Schema v2)",
        "- **Ablage-Richtlinie:** Keine Ablage auf dem Desktop (Nutzerentscheid 2026-10-25)",
    ]
    if notify:
        lines.extend([
            "",
            "## Benachrichtigung an Lukas / Persönlichen Assistenten",
            "Diese Claim-Änderung betrifft den formalen wissenschaftlichen Beweisstand bzw. Manuskript-Status.",
            "Bitte zur Kenntnis nehmen, im Research-Register prüfen und für nachgelagerte Veröffentlichungen/Arbeitsschritte freigeben.",
        ])
    return "\n".join(lines)


def format_claim_ticket_title(project: str, old_claim: str, new_claim: str) -> str:
    """Erstellt einen prägnanten Titel (<= 80 Zeichen)."""
    p_clean = project.strip()
    o_clean = old_claim.strip().replace("\n", " ")
    n_clean = new_claim.strip().replace("\n", " ")
    title = f"Claim-Änderung in {p_clean}: {o_clean} -> {n_clean}"
    if len(title) > 80:
        title = f"Claim-Änderung in {p_clean}: {n_clean}"
    if len(title) > 80:
        title = title[:77] + "..."
    return title


def report_claim_change(
    *,
    project: str,
    old_claim: str,
    new_claim: str,
    location: str,
    reason: str,
    tickets_dir: Path | str | None = None,
    systems_registry: Path | str | None = None,
    priority: str = "hoch",
    pipeline: str = "research",
    notify: bool = True,
    idempotency_key: str | None = None,
    today: str | None = None,
    session: str | None = None,
    session_agent: str | None = None,
    session_host: str | None = None,
) -> str:
    """Meldet eine Claim-Änderung atomar an ticket-master ohne Desktop-Ablage.

    Gibt den absoluten Pfad des erzeugten Tickets in INBOX/ zurück.
    """
    if not project or not project.strip():
        raise ValueError("Parameter 'project' darf nicht leer sein.")
    if not old_claim or not old_claim.strip():
        raise ValueError("Parameter 'old_claim' (alter Wortlaut) darf nicht leer sein.")
    if not new_claim or not new_claim.strip():
        raise ValueError("Parameter 'new_claim' (neuer Wortlaut) darf nicht leer sein.")
    if not location or not location.strip():
        raise ValueError("Parameter 'location' (Fundstelle) darf nicht leer sein.")
    if not reason or not reason.strip():
        raise ValueError("Parameter 'reason' (Anlass der Änderung) darf nicht leer sein.")

    # Zielverzeichnis für Tickets ermitteln
    resolved_tickets_dir = (
        Path(tickets_dir) if tickets_dir is not None
        else ticket_writer._default_tickets_dir()
    )
    if resolved_tickets_dir is None:
        raise ValueError("tickets_dir konnte nicht ermittelt werden.")

    # Systems-Registry für Routing Schema v2 ermitteln
    resolved_registry_path = (
        Path(systems_registry) if systems_registry is not None
        else ticket_writer._default_systems_registry()
    )

    # Titel und Inhalt formatieren
    title = format_claim_ticket_title(project, old_claim, new_claim)
    body = format_claim_ticket_body(
        project=project,
        old_claim=old_claim,
        new_claim=new_claim,
        location=location,
        reason=reason,
        notify=notify,
    )

    # Idempotency Key generieren, falls nicht explizit übergeben
    if not idempotency_key:
        fingerprint = f"{project}:{old_claim}:{new_claim}:{location}:{reason}"
        idempotency_key = "claim-change-" + hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()[:16]

    # Wenn Systems-Registry vorhanden: Schema v2 Contract erstellen
    if resolved_registry_path and resolved_registry_path.is_file():
        registry_data = json.loads(resolved_registry_path.read_text(encoding="utf-8-sig"))
        ticket_path = ticket_writer.create_routed_ticket(
            title=title,
            body=body,
            tickets_dir=resolved_tickets_dir,
            registry_snapshot=registry_data,
            ticket_kind="normal",
            target_kind="any",
            project=project.strip(),
            priority=priority,
            pipeline=pipeline,
            idempotency_key=idempotency_key,
            today=today,
            session=session,
            session_agent=session_agent or "report_claim_change",
            session_host=session_host,
        )
    else:
        # Fallback auf Schema v1 Create, falls Registry-Snapshot nicht existiert
        ticket_path = ticket_writer.create(
            title=title,
            body=body,
            project=project.strip(),
            priority=priority,
            pipeline=pipeline,
            tickets_dir=resolved_tickets_dir,
            today=today,
            session=session,
            session_agent=session_agent or "report_claim_change",
            session_host=session_host,
        )

    return str(ticket_path)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="report_claim_change",
        description="Meldet eine Claim-Änderung sauber an ticket-master (Routing Schema v2) statt Ablage auf dem Desktop.",
    )
    parser.add_argument("--project", "-p", required=True, help="Betroffenes Projekt (z.B. 'Feedback Equilibrium Dynamics')")
    parser.add_argument("--old-claim", "--old-wording", "-o", required=True, help="Alter Wortlaut / Claim-Level")
    parser.add_argument("--new-claim", "--new-wording", "-n", required=True, help="Neuer Wortlaut / Claim-Level")
    parser.add_argument("--source", "--location", "--fundstelle", "-s", required=True, help="Fundstelle (Quelle/Stelle/Datei)")
    parser.add_argument("--reason", "--anlass", "-r", required=True, help="Anlass / Rationale der Änderung")
    parser.add_argument("--tickets-dir", default=None, help="Optionaler Pfad zum _TICKETS-Wurzelordner")
    parser.add_argument("--systems-registry", default=None, help="Optionaler Pfad zum systems-registry.json Snapshot")
    parser.add_argument("--priority", default="hoch", help="Priorität des Tickets (Standard: 'hoch')")
    parser.add_argument("--pipeline", default="research", help="Pipeline (Standard: 'research')")
    parser.add_argument("--no-notify", action="store_true", help="Keine Benachrichtigungszeile an Lukas/Assistent anfügen")
    parser.add_argument("--idempotency-key", default=None, help="Stabiler Schlüssel zur Vermeidung von Doppel-Tickets")
    parser.add_argument("--session", default=None, help="Session-ID der meldenden Sitzung")
    parser.add_argument("--session-agent", default=None, help="Agenten-Name der meldenden Sitzung")
    parser.add_argument("--session-host", default=None, help="Host der meldenden Sitzung")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        path = report_claim_change(
            project=args.project,
            old_claim=args.old_claim,
            new_claim=args.new_claim,
            location=args.source,
            reason=args.reason,
            tickets_dir=args.tickets_dir,
            systems_registry=args.systems_registry,
            priority=args.priority,
            pipeline=args.pipeline,
            notify=not args.no_notify,
            idempotency_key=args.idempotency_key,
            session=args.session,
            session_agent=args.session_agent,
            session_host=args.session_host,
        )
        print(path)
        return 0
    except Exception as exc:
        print(f"FEHLER: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
