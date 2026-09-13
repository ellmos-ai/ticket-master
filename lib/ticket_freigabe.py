r"""Hash-bound one-time user approval for tickets in ``USER/freigabe``.

T-20260913-744071825. Adopted from ellmos-ai/FolderHome @ f9fdb6a
(``src/folderhome/application/master_agent.py::confirm_master_agent_plan``,
read under its judging lock, not copied): there a plan carries
``plan_id = 'plan_' + sha256(plan)[:20]``, an approval counts only on an exact
``plan_id`` match, and a ``MasterConfirmationReceipt`` makes it single-use.

The problem it solves here: an approval given for one wording silently covers a
later, different wording. A formless "yes" binds to nothing, so nobody can tell
afterwards what exactly was approved -- and a text edited after the fact carries
the old approval along with it.

WHY A SEPARATE SECTION AND NOT THE WHOLE TICKET (measured, 2026-09-13).
The obvious candidate for the hash source was the AUFTRAG section. It does not
carry: of the 17 tickets standing in ``USER/freigabe``, only 7 have real
AUFTRAG text, 9 hold the empty template placeholder and 1 has no such section
at all. Hashing the whole file is worse still -- every VERLAUF line would
invalidate the approval. So what is being approved has to be stated
deliberately, in its own section, by whoever escalates. That is not extra
ceremony: an escalation that cannot say what it wants approved is the actual
defect this ticket is about.

WHY THE EXISTING STOCK IS NOT RETROFITTED (decided here, deliberately).
Those 17 tickets keep the formless approval they were filed under. Computing an
ID for them now would fake a binding that never happened -- the user never saw
that text under that ID, which is the one thing the ID is supposed to attest.
A retrofit would make the field look like evidence while being a reconstruction.
Legacy stays legacy, visibly: ``freigabe_state()`` reports ``legacy`` for a
ticket without the section, and the fail-closed CLI refuses to pretend.

NAMING: this is NOT ``release_claim``/``release_contract``/``release_claims``,
which all mean "hand back the host claim". Calling this ``--mark-released``
(as the ticket sketched) would put a fourth, unrelated meaning on an
already-loaded word, so the CLI says ``--mark-freigabe`` -- matching the
subcategory ``freigabe``, which both documentation languages already use
untranslated.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path

try:  # package import
    from .routing_contract import _atomic_rewrite, parse_fields, update_fields
except ImportError:  # direct import from lib on sys.path
    from routing_contract import _atomic_rewrite, parse_fields, update_fields


ID_PREFIX = "frg_"
ID_HEX_LENGTH = 20
SECTION_TITLE = "ZUR FREIGABE"

# An unfilled template placeholder, e.g. "<Der genaue Wortlaut, ...>".
_PLACEHOLDER_RE = re.compile(r"^<.*>$", re.DOTALL)
# The section runs from its own dashed header to the next dashed header, the
# closing "=" rule, or end of file -- the shape every other ticket section has.
_SECTION_RE = re.compile(
    r"^-{10,}[ \t]*\n[ \t]*" + SECTION_TITLE + r"[^\n]*\n-{10,}[ \t]*\n"
    r"(?P<body>.*?)(?=\n-{10,}[ \t]*\n|\n={10,}|\Z)",
    re.MULTILINE | re.DOTALL,
)


class FreigabeError(Exception):
    """A hash-bound approval was refused; the ticket file stays unchanged."""


def freigabe_id(text: str) -> str:
    """``frg_`` + the first 20 hex digits of the text's SHA-256.

    Normalized before hashing so that a line-ending change (this queue lives
    in OneDrive and is edited from Windows and Linux) does not invalidate an
    approval, while every change to the wording does.
    """
    normalized = "\n".join(line.rstrip() for line in text.strip().splitlines())
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    return ID_PREFIX + digest[:ID_HEX_LENGTH]


def freigabe_text(text: str) -> str | None:
    """The body of the ZUR FREIGABE section, or None if there is none.

    An empty section counts as absent: a header with nothing under it states
    nothing, and an ID over the empty string would be the same for every
    ticket -- a single shared 'approval' for anything at all. The unfilled
    ``<...>`` template placeholder counts as absent for exactly that reason:
    measured on 2026-09-13, 9 of 17 tickets in ``USER/freigabe`` carry the
    AUFTRAG section's placeholder verbatim, so "someone pasted the section and
    did not fill it" is the normal case, not an exotic one.
    """
    match = _SECTION_RE.search(text)
    if match is None:
        return None
    body = match.group("body").strip()
    if not body or _PLACEHOLDER_RE.match(body):
        return None
    return body


def freigabe_state(path: Path | str) -> dict[str, str | None]:
    """What this ticket's approval currently is -- read-only.

    ``state`` is one of:
      ``legacy``    no ZUR FREIGABE section; formless approval, not assessable
      ``unstamped`` section present, FREIGABE_ID missing or stale
      ``pending``   FREIGABE_ID matches the current text, not yet approved
      ``granted``   approved, and the text has not changed since
      ``stale``     approved, but the text changed afterwards -- the old
                    approval no longer covers it
    """
    path = Path(path)
    text = path.read_text(encoding="utf-8")
    fields = parse_fields(text)
    body = freigabe_text(text)
    if body is None:
        return {"state": "legacy", "current_id": None,
                "stamped_id": fields.get("FREIGABE_ID") or None,
                "granted": fields.get("FREIGABE_ERTEILT") or None}
    current = freigabe_id(body)
    stamped = fields.get("FREIGABE_ID") or None
    granted = fields.get("FREIGABE_ERTEILT") or None
    if granted:
        state = "granted" if granted.split()[-1] == current else "stale"
    elif stamped == current:
        state = "pending"
    else:
        state = "unstamped"
    return {"state": state, "current_id": current,
            "stamped_id": stamped, "granted": granted}


def stamp_freigabe_id(path: Path | str, *, now: datetime | None = None) -> str:
    """Write the ID of the current ZUR FREIGABE text into the ticket.

    Idempotent: stamping an already-correct ID changes nothing and adds no log
    line. Re-stamping after the text changed is allowed and is the documented
    way to ask again -- but only for a ticket that is not currently granted;
    silently re-stamping under a standing approval would erase the evidence of
    what was approved.
    """
    path = Path(path)
    text = path.read_text(encoding="utf-8")
    body = freigabe_text(text)
    if body is None:
        raise FreigabeError(
            f"no '{SECTION_TITLE}' section: state what is to be approved before "
            "an ID can bind to it")
    state = freigabe_state(path)
    if state["state"] == "granted":
        raise FreigabeError(
            f"already approved under {state['granted']}; withdraw that approval "
            "before re-stamping, or the record of what was approved is lost")
    new_id = freigabe_id(body)
    if state["stamped_id"] == new_id:
        return new_id
    stamp = _utc_text(now)
    _atomic_rewrite(path, update_fields(
        text, {"FREIGABE_ID": new_id},
        log=f"{stamp}  FREIGABE_ID {new_id} vergeben (Text der Freigabe-Sektion).",
    ))
    return new_id


def mark_freigabe(path: Path | str, *, freigabe_id_value: str,
                  by: str, now: datetime | None = None) -> str:
    """Record the user's approval -- only for the exact current text, once.

    Fail-closed throughout: every refusal leaves the file byte-identical, the
    same contract ``--claim-current-host`` follows. Refused when the ticket has
    no section (legacy stock), when the offered ID does not match the current
    text (the wording changed after the user read it, or the wrong ID was
    pasted), and when an approval already stands for that same text.
    """
    path = Path(path)
    text = path.read_text(encoding="utf-8")
    body = freigabe_text(text)
    if body is None:
        raise FreigabeError(
            f"no '{SECTION_TITLE}' section: this is legacy stock approved "
            "formlessly; it is not retrofitted with a hash binding")
    current = freigabe_id(body)
    state = freigabe_state(path)
    if state["state"] == "granted":
        raise FreigabeError(
            f"approval already recorded: {state['granted']} -- an approval is "
            "single-use")
    if freigabe_id_value != current:
        raise FreigabeError(
            f"FREIGABE_ID does not match the current text: offered "
            f"{freigabe_id_value!r}, text is {current!r}. The wording changed "
            "after it was read, or the wrong ID was given; nothing was written")
    stamp = _utc_text(now)
    granted = f"{stamp} {by} {current}"
    _atomic_rewrite(path, update_fields(
        text, {"FREIGABE_ID": current, "FREIGABE_ERTEILT": granted},
        log=f"{stamp}  Freigabe erteilt durch {by} fuer {current}.",
    ))
    return granted


def _utc_text(now: datetime | None) -> str:
    value = now or datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
