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
import json
import os
import sys
from pathlib import Path
from typing import Any

__all__ = [
    "QueueAliasError",
    "TICKETS_DIR_ENV",
    "expand_placeholders",
    "require_tickets_dir",
    "resolve_config_path",
    "resolve_queue_alias",
    "resolve_tickets_dir",
    "tm_config",
    "tm_config_path",
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


# --- Die Ticket-Queue finden (T-20260913-156957497) -------------------------
#
# Vier Stellen im Paket suchten die Queue-Wurzel auf eigene Faust, und sie waren
# sich nicht einig: `ticket_writer`, `ticket_audit` und `status_drift_fixer`
# lasen NUR die Umgebungsvariable, `auditor_bridge` las Konfiguration VOR
# Umgebung. Vier private Varianten einer Frage sind drei zu viel -- und die
# Beispielkonfiguration fuehrte `tickets_dir` die ganze Zeit als Schluessel,
# den kein Schreiber las.
#
# Das ist derselbe Defekt wie bei `systems_registry` (T-20260913-734536498, PR
# #30), und aus demselben Grund gefaehrlich: Eine Umgebungsvariable ist kein
# verlaesslicher Traeger. Ein Prozess, der vor dem Setzen gestartet wurde, erbt
# sie nicht -- die Variable ist dann gesetzt und trotzdem unsichtbar. Am
# 2026-09-13 brach `ticket_writer --split-from` genau so ab, mit einem nackten
# `TypeError` aus `pathlib`, weil `None` bis in `Path()` durchlief.
#
# Praezedenz wie in PR #30: **Aufrufer > Umgebung > Konfiguration.** Der
# explizite Aufruf gewinnt immer, die Datei ist der verlaessliche Boden.

TICKETS_DIR_ENV = "TICKET_MASTER_TICKETS_DIR"


def tm_config_path() -> Path:
    """`config/ticket-master.config.json` neben dem Paket."""
    return Path(__file__).resolve().parent.parent / "config" / "ticket-master.config.json"


def tm_config(path: Any = None) -> dict:
    """Die Konfiguration lesen, oder ein leeres Dict.

    Absichtlich tolerant: Eine fehlende oder kaputte Konfiguration darf einen
    Schreibvorgang nicht verhindern -- sie ist eine Bequemlichkeit, keine
    Voraussetzung. Wer den Pfad wirklich braucht, bekommt von
    `require_tickets_dir()` einen klaren Fehler statt hier eine Ausnahme.
    """
    ziel = Path(path) if path else tm_config_path()
    try:
        with ziel.open(encoding="utf-8-sig") as fh:
            daten = json.load(fh)
    except (OSError, ValueError):
        return {}
    return daten if isinstance(daten, dict) else {}


def resolve_tickets_dir(explicit: Any = None, *, config: dict | None = None) -> Path | None:
    """Die Queue-Wurzel bestimmen: Aufrufer, dann Umgebung, dann Konfiguration.

    Gibt ``None`` zurueck, wenn keiner der drei Wege einen nutzbaren Wert traegt.
    Platzhalter (`<HOME>`, `<USER>`) und `~` werden auf allen drei Wegen
    aufgeloest -- vorher galt das nur fuer den Konfigurationswert, sodass ein
    Platzhalter aus der Umgebung als Verzeichnis namens buchstaeblich `<HOME>/...`
    endete (dieselbe Klasse wie T-20260912-206012253).
    """
    for kandidat in (
        explicit,
        os.environ.get(TICKETS_DIR_ENV),
        (tm_config() if config is None else config).get("tickets_dir"),
    ):
        pfad = resolve_config_path(kandidat)
        if pfad is not None:
            return pfad
    return None


def require_tickets_dir(explicit: Any = None, *, config: dict | None = None) -> Path:
    """Wie `resolve_tickets_dir()`, aber mit Fehler statt ``None``.

    Die Fehlermeldung nennt **alle drei** Wege samt Reihenfolge. Wer nur einen
    davon erfaehrt, richtet genau den ein und steht beim naechsten Prozess, der
    die Variable nicht geerbt hat, wieder da.
    """
    gefunden = resolve_tickets_dir(explicit, config=config)
    if gefunden is None:
        raise ValueError(
            "tickets_dir is not configured. Three ways, highest precedence first: "
            "(1) pass it explicitly (--tickets-dir), "
            f"(2) set the {TICKETS_DIR_ENV} environment variable, "
            f'(3) set "tickets_dir" in {tm_config_path()}. '
            "Prefer (3): an environment variable is not inherited by processes "
            "that were already running when it was set (T-20260913-156957497)."
        )
    return gefunden


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
