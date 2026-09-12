"""Platzhalter in Konfigurationspfaden aufloesen -- genau einmal im Repo.

`config/ticket-master.config.json` und `config/ticket-writer.config.json` duerfen
`<HOME>` und `<USER>` statt host-spezifischer Pfade tragen, damit dieselbe Datei
auf mehreren Rechnern gilt (siehe `_comment_placeholders` in den example-Configs).
Wer einen Wert aus diesen Configs zu einem Pfad macht, schickt ihn vorher hier
durch.

Warum eine eigene, geteilte Stelle: Die Routine lebte bisher privat in
`bin/ticket_master.py`, weshalb `lib/auditor_bridge.py` sie nicht erreichte und
`tickets_dir` roh weiterreichte -- der Trockenlauf von `--findings-to-tickets`
pruefte die Dedup-Frage dann gegen ein Verzeichnis namens buchstaeblich
`<HOME>/...`, fand dort nichts und meldete jedes Finding als neu
(T-20260912-206012253). Eine zweite Kopie der Routine haette denselben Fehler nur
verdoppelt; P-009 verlangt hier eine Quelle.
"""

from __future__ import annotations

import getpass
import os
from pathlib import Path
from typing import Any

__all__ = ["expand_placeholders", "resolve_config_path"]


def expand_placeholders(value: str) -> str:
    """`<HOME>` und `<USER>` durch die Werte dieses Hosts ersetzen.

    Ein Wert ohne Platzhalter kommt unveraendert zurueck, der Aufruf ist also
    immer gefahrlos.
    """
    return value.replace("<HOME>", str(Path.home())).replace("<USER>", getpass.getuser())


def resolve_config_path(raw: Any) -> Path | None:
    """Einen Konfigurationswert zu einem Pfad machen: Platzhalter, dann `~`.

    Gibt ``None`` zurueck, wenn der Wert kein nutzbarer Pfad ist (leer, None,
    falscher Typ) -- die Aufrufer melden das selbst als Konfigurationsfehler,
    statt hier zu werfen. Absichtlich ohne `resolve()`: ob ein Pfad existieren
    oder innerhalb einer Wurzel liegen muss, entscheidet der Aufrufer.
    """
    if isinstance(raw, os.PathLike):
        raw = os.fspath(raw)
    if not isinstance(raw, str) or not raw.strip():
        return None
    return Path(expand_placeholders(raw.strip())).expanduser()
