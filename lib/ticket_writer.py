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
import random
import re
from datetime import datetime
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

# Ticket-Dateiname:
#   T-<8-stelliges Datum>-<laufende Nummer>[_<slug>][.<HOST-oder-Suffix>].txt
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
# Die Erweiterung ist bewusst eng: Datumsgruppe und laufende Nummer bleiben
# Pflicht, die Endung bleibt .txt. Damit gelten die Altlasten unter PENDING/
# (T-41_LOESCH-REPORT.txt, T-41_cleanup.ps1, T-41_gnomad_transfer.sh)
# weiterhin NICHT als Tickets -- ein Muster, das Reports und Shell-Skripte
# mitzaehlt, waere schlechter als eines, das ein paar Tickets uebersieht.
TICKET_FILENAME_RE = re.compile(
    r"^T-(?P<date>\d{8})-(?P<number>\d+)"
    r"(?:_(?P<slug>[A-Za-z0-9][\w-]*))?"
    r"(?:\.(?P<suffix>[A-Za-z0-9_-]+))?"
    r"\.txt$"
)


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
