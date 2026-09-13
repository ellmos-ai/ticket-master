r"""Read back the decision register before escalating a ticket to ``USER/``.

T-20260913-867541218, open since 2026-08-26. The teaching case is
T-20260824-857836410: E8/F17 was reported as open on 2026-08-25, although
D-20260731-010 had carried the decision in full since 2026-07-31 and a register
readback of 2026-08-22 had explicitly warned against submitting it again. Cost:
the user's time, spent exactly where this system is supposed to save it.

THE DESIGN QUESTION THE TICKET POSED -- PROMPT GATE OR CODE GATE -- IS ANSWERED
HERE AS CODE. A prompt gate binds one agent's discipline; every other consumer
of ``ticket_mover`` walks straight past it. This repository has the lesson on
file twice over: a restriction that lives only as prose in a system prompt gets
walked past by an agent that has a shell (see the codex-rescue attribution
finding), and T-20260830-938608207 found the same shape inside this very
module -- a lease that existed as a field but was never read on a write path.
A field nobody reads is a note; a rule nobody enforces is a wish.

WHAT IS SEARCHED FOR -- STRONG KEYS ONLY, NOT FREE TEXT. The register holds 317
entries; matching ticket wording against titles would produce a haystack of
near-misses and train everyone to click past it, which is how a checklist dies.
So only two kinds of key are used, both unambiguous:

  * the ticket's own ID (``T-YYYYMMDD-<digits>``) -- if the register mentions
    this exact ticket, a decision about it demonstrably exists;
  * every decision ID the ticket itself names (``D-YYYYMMDD-NNN``) -- if the
    ticket already points at a decision, its state is knowable.

FAIL-CLOSED, BUT ONLY WHERE IT CAN BE. Three states, deliberately distinct:

  ``checked``      the index was read; hits (if any) carry their status.
  ``unavailable``  an index was *named* (flag or environment) and could not be
                   read -> the caller refuses. Naming it means meaning it.
  ``absent``       no index was named and none was found next to the queue ->
                   no gate, but the ticket gets a line saying the claim of
                   "open" is UNVERIFIED. That is the honest middle: this is a
                   public tool and a foreign user has no such register, so
                   blocking every escalation would be wrong -- but silently
                   claiming "open" is what the ticket is about.

The canonical index is produced by the register's own read-only tool
(``_CONTROL/_DECISIONS/_tools/decisions_index.py`` -> ``decisions.index.json``).
This module never builds an index of its own and never writes to the register:
one source, per P-009.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

__all__ = [
    "DECISIONS_INDEX_ENV",
    "INDEX_RELATIVE_PATH",
    "decision_readback",
    "find_decisions_index",
    "readback_log_line",
]

DECISIONS_INDEX_ENV = "TICKET_MASTER_DECISIONS_INDEX"
# Relative to the queue's parent: the queue lives at <control-center>/_TICKETS,
# the register at <control-center>/_CONTROL/_DECISIONS.
INDEX_RELATIVE_PATH = Path("_CONTROL") / "_DECISIONS" / "_tools" / "decisions.index.json"

_TICKET_ID_RE = re.compile(r"\bT-\d{8}-\d+\b")
_DECISION_ID_RE = re.compile(r"\bD-\d{8}-\d+(?:/[A-Z]\d+)?\b")

# A hit in one of these states means the question was already answered. OFFEN
# is not a hit in that sense -- an entry still awaiting the user is precisely
# what a fresh escalation would duplicate, so it is surfaced but does not block.
_DECIDED_STATES = frozenset({"DONE", "ENTSCHIEDEN_UMSETZUNG_OFFEN", "ARCHIVIERT"})

# Index fields searched for a key. Kept explicit rather than "the whole entry":
# a JSON dump of an entry contains its own file paths and timestamps, which
# would match digit-shaped keys by accident.
_SEARCHED_FIELDS = ("id", "key", "title", "question", "source_excerpt",
                    "decision_field_raw", "options_excerpt",
                    "recommendation_excerpt")


def find_decisions_index(queue_dir: Path | str | None = None,
                         explicit: Path | str | None = None) -> tuple[Path | None, bool]:
    """Locate the decision index. Returns ``(path, was_named)``.

    ``was_named`` is True when the path came from the caller or the
    environment -- that is what turns an unreadable index into a refusal
    instead of a shrug.
    """
    if explicit:
        return Path(explicit).expanduser(), True
    from_env = os.environ.get(DECISIONS_INDEX_ENV, "").strip()
    if from_env:
        return Path(from_env).expanduser(), True
    if queue_dir is None:
        return None, False
    candidate = Path(queue_dir).parent / INDEX_RELATIVE_PATH
    return (candidate, False) if candidate.is_file() else (None, False)


def _keys_from_ticket(text: str, ticket_name: str) -> list[str]:
    keys: list[str] = []
    for source in (ticket_name, text):
        for match in _TICKET_ID_RE.finditer(source):
            if match.group(0) not in keys:
                keys.append(match.group(0))
    for match in _DECISION_ID_RE.finditer(text):
        found = match.group(0)
        if found not in keys:
            keys.append(found)
        # A sub-decision is written "D-20260906-008/E01" in tickets, while the
        # register indexes the parent "D-20260906-008". Searching only the
        # written form finds nothing -- a false NEGATIVE, and precisely the
        # "not found" that this gate exists to stop being mistaken for "open".
        # Measured 2026-09-13 against the live register: the day's own
        # decisions are booked in exactly this notation, so without the base
        # key the gate would have been silent in the common case.
        base = found.split("/", 1)[0]
        if base not in keys:
            keys.append(base)
    return keys


def _entry_matches(entry: dict[str, Any], key: str) -> bool:
    """Whole-key match only -- a plain substring test is wrong here.

    IDs are digit-suffixed and of differing lengths: the old two-digit form
    ``T-20260808-03`` is a literal prefix of the nine-digit ``T-20260808-031234567``,
    so ``key in value`` reports a hit on an unrelated ticket. A false hit is
    worse than no gate at all: it blocks a legitimate escalation and teaches
    the next person to reach for --acknowledge-decision without reading.
    """
    pattern = re.compile(re.escape(key) + r"\b")
    for field in _SEARCHED_FIELDS:
        value = entry.get(field)
        if isinstance(value, str) and pattern.search(value):
            return True
    return False


def decision_readback(ticket: Path | str, *, queue_dir: Path | str | None = None,
                      index_path: Path | str | None = None) -> dict[str, Any]:
    """Query the register for this ticket. Read-only, writes nothing.

    Returns ``{"state", "index", "keys", "hits", "blocking"}`` where ``hits``
    are index entries matching one of the ticket's strong keys and ``blocking``
    are those among them whose status says the question is already answered.
    """
    ticket = Path(ticket)
    text = ticket.read_text(encoding="utf-8", errors="replace")
    keys = _keys_from_ticket(text, ticket.name)
    path, was_named = find_decisions_index(queue_dir, index_path)

    if path is None:
        return {"state": "absent", "index": None, "keys": keys,
                "hits": [], "blocking": []}
    try:
        index = json.loads(path.read_text(encoding="utf-8"))
        entries = index["entries"]
        if not isinstance(entries, list):
            raise TypeError("entries is not a list")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        state = "unavailable" if was_named else "absent"
        return {"state": state, "index": str(path), "keys": keys,
                "hits": [], "blocking": [], "error": str(exc)}

    hits = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        matched = [key for key in keys if _entry_matches(entry, key)]
        if matched:
            hits.append({
                "id": entry.get("key") or entry.get("id"),
                "status": entry.get("status_class"),
                "title": entry.get("title"),
                "source": entry.get("source_file"),
                "matched": matched,
            })
    blocking = [hit for hit in hits if hit["status"] in _DECIDED_STATES]
    return {"state": "checked", "index": str(path), "keys": keys,
            "hits": hits, "blocking": blocking}


def readback_log_line(result: dict[str, Any], *, stamp: str,
                      acknowledged: list[str] | None = None) -> str:
    """One VERLAUF line recording what was looked up and what came back.

    The protocol form the ticket asks for: it must be possible to tell
    "looked, found nothing" from "never looked". Hence the wording -- an
    unchecked escalation says UNGEPRUEFT, never "offen".
    """
    keys = ", ".join(result["keys"]) or "keine Kennung im Ticket"
    if result["state"] == "absent":
        return (f"{stamp}  Entscheidungs-Readback UNGEPRUEFT: kein Entscheidungsindex "
                f"erreichbar. Gesucht waere: {keys}. 'Offen' ist hier eine "
                "unbelegte Annahme, kein Befund.")
    if result["state"] == "unavailable":
        return (f"{stamp}  Entscheidungs-Readback FEHLGESCHLAGEN: benannter Index "
                f"{result['index']} nicht lesbar ({result.get('error', '?')}).")
    if not result["hits"]:
        return (f"{stamp}  Entscheidungs-Readback: NICHT GEFUNDEN. Gesucht: {keys}. "
                f"Index: {result['index']}.")
    rendered = "; ".join(
        f"{hit['id']} [{hit['status']}] ({hit['source']})" for hit in result["hits"])
    line = (f"{stamp}  Entscheidungs-Readback: TREFFER fuer {keys} -> {rendered}. "
            f"Index: {result['index']}.")
    if acknowledged:
        line += f" Geprueft und als nicht einschlaegig quittiert: {', '.join(acknowledged)}."
    return line
