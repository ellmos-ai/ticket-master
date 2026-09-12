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
import sys
from pathlib import Path
from typing import Any

__all__ = [
    "QueueAliasError",
    "expand_placeholders",
    "resolve_config_path",
    "resolve_queue_alias",
]


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


# --- Rename-Toleranz fuer die Live-Queue (T-20260906-387521104) -------------
#
# Die Queue heisst `_TICKETS` und wird nach `TICKETS` umbenannt. Ordner und
# Konfiguration liegen beide in OneDrive und replizieren mit eigener Latenz;
# zwischen Rename und angekommener Config kann ein Host also den einen Namen
# in der Config und den anderen auf der Platte sehen. Ohne Toleranz legt ein
# Schreiber in diesem Fenster den alten Ordner neu an -- das Split-Queue-
# Szenario, gegen das dieser ganze Umzug abgesichert wird.
#
# Bewusst NICHT in resolve_config_path() eingebaut: das gilt fuer jeden
# Konfigurationspfad, nicht nur fuer Queue-Wurzeln.

_QUEUE_ALIASES: dict[str, str] = {"_TICKETS": "TICKETS", "TICKETS": "_TICKETS"}
_warned_redirects: set[tuple[str, str]] = set()


class QueueAliasError(RuntimeError):
    """Beide Queue-Namen existieren nebeneinander -- nicht raten, abbrechen."""


def resolve_queue_alias(path: Any) -> Path:
    """Einen Queue-Pfad auf den tatsaechlich vorhandenen Namen ziehen.

    Heisst der letzte Pfadteil weder ``_TICKETS`` noch ``TICKETS``, kommt der
    Pfad unveraendert zurueck -- der Aufruf ist also ueberall gefahrlos.
    Existiert nur der Alias, wird auf ihn umgeleitet (einmalige Warnung nach
    stderr). Existieren **beide**, ist das eine Split-Queue: dann wird
    ``QueueAliasError`` geworfen, statt eine Haelfte zu waehlen. Existiert
    **keiner**, bleibt der Pfad unveraendert -- der Aufrufer meldet das
    fehlende Verzeichnis mit seiner eigenen, aussagekraeftigeren Fehlermeldung.
    """
    resolved = Path(path)
    alias_name = _QUEUE_ALIASES.get(resolved.name)
    if alias_name is None:
        return resolved

    alias = resolved.with_name(alias_name)
    requested_exists = resolved.is_dir()
    alias_exists = alias.is_dir()

    if requested_exists and alias_exists:
        raise QueueAliasError(
            f"both queue names exist side by side: {resolved} and {alias}. "
            "This is a split queue (see T-20260906-387521104) -- merge them "
            "before writing, never overwrite."
        )
    if alias_exists and not requested_exists:
        key = (str(resolved), str(alias))
        if key not in _warned_redirects:
            _warned_redirects.add(key)
            print(
                f"NOTE: queue {resolved.name!r} not found, using {alias_name!r} "
                f"instead ({alias}). T-20260906-387521104.",
                file=sys.stderr,
            )
        return alias
    return resolved
