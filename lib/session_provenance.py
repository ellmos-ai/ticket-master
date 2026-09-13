"""Provider-neutral session provenance helpers.

Explicit values win. Otherwise only provider/runtime variables whose meaning
is a session identifier are accepted. Missing evidence is rendered as
``unbekannt`` instead of guessed.
"""

from __future__ import annotations

import os
import platform
from dataclasses import dataclass
from datetime import date
from typing import Mapping


UNKNOWN = "unbekannt"


class SessionProvenanceError(ValueError):
    """An agent identity contradicts itself; nothing is stamped.

    T-20260913-695955668. The stamp is rendered as ``<agent>@<host>``, so an
    agent value that already carries ``@<host>`` has to be recognised as
    already-qualified instead of being qualified a second time. It used to be
    handled one level up, in ``ticket_mover.mark_delegated`` only -- which is
    why ``ticket_writer --session-agent`` produced

        SESSION: session_… | control1@ASUS-GEI@ASUS-GEI | 2026-09-13

    fail-silent: two ``@`` separators, unparsable for a machine, and nothing
    failed. The split now lives here, at the single point every caller passes
    through, so both options behave the same way.
    """
SESSION_ENV_KEYS = (
    "TICKET_MASTER_SESSION_ID",
    "CLAUDE_SESSION_ID",
    "CODEX_SESSION_ID",
)
AGENT_ENV_KEYS = (
    "TICKET_MASTER_AGENT",
    "CLAUDE_AGENT_NAME",
    "CODEX_AGENT_NAME",
)
HOST_ENV_KEYS = ("TICKET_MASTER_HOST", "COMPUTERNAME", "HOSTNAME")


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    value = str(value).strip()
    if not value or "\n" in value or "\r" in value or "|" in value:
        return None
    return value


def _split_agent(value: str) -> tuple[str, str | None]:
    """Splits an already-qualified ``<agent>@<host>`` into its two halves.

    Idempotent by construction: a bare agent name comes back unchanged with
    ``None``, so callers can pass either form. Two CLI options in this repo
    disagree on which form they want -- ``ticket_mover --agent`` documents
    ``claude-code@ASUS-GEI`` while ``ticket_writer --session-agent`` wants the
    bare name -- and the resulting mix-up is a bedienfehler the tool used to
    neither prevent nor report.

    Refuses rather than guesses on the two shapes that mean nothing:
    more than one ``@`` (which half is the host?) and an empty agent half
    (``@HOST`` names nobody).
    """
    if "@" not in value:
        return value, None
    name, _, host = value.partition("@")
    if "@" in host:
        raise SessionProvenanceError(
            f"agent identity has more than one '@': {value!r}")
    name, host = name.strip(), host.strip()
    if not name or not host:
        raise SessionProvenanceError(
            f"agent identity is not '<agent>@<host>': {value!r}")
    return name, host


def _first_env(environ: Mapping[str, str], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = _clean(environ.get(key))
        if value:
            return value
    return None


@dataclass(frozen=True)
class SessionProvenance:
    session: str
    agent: str
    host: str
    day: str

    @property
    def value(self) -> str:
        return f"{self.session} | {self.agent}@{self.host} | {self.day}"

    @property
    def stamp(self) -> str:
        return f"session: {self.value}"


def resolve_session_provenance(
    session: str | None = None,
    *,
    agent: str | None = None,
    host: str | None = None,
    day: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> SessionProvenance:
    """Resolve explicit > authoritative environment/runtime > unknown."""

    env = os.environ if environ is None else environ
    resolved_session = _clean(session) or _first_env(env, SESSION_ENV_KEYS) or UNKNOWN
    resolved_agent = _clean(agent) or _first_env(env, AGENT_ENV_KEYS) or UNKNOWN
    resolved_agent, agent_host = _split_agent(resolved_agent)
    resolved_host = (
        _clean(host)
        or agent_host
        or _first_env(env, HOST_ENV_KEYS)
        or _clean(platform.node())
        or UNKNOWN
    )
    if agent_host and _clean(host) and agent_host.casefold() != _clean(host).casefold():
        # Two different hosts asserted for one stamp. Guessing which one is
        # meant would put a wrong host into an audit trail, so neither is used.
        raise SessionProvenanceError(
            f"agent names host {agent_host!r} but host={host!r} was passed; "
            "pass the bare agent name, or drop the conflicting host")
    resolved_day = _clean(day) or date.today().isoformat()
    return SessionProvenance(
        session=resolved_session,
        agent=resolved_agent,
        host=resolved_host,
        day=resolved_day,
    )
