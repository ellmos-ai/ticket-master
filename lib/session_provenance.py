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
    resolved_host = (
        _clean(host)
        or _first_env(env, HOST_ENV_KEYS)
        or _clean(platform.node())
        or UNKNOWN
    )
    resolved_day = _clean(day) or date.today().isoformat()
    return SessionProvenance(
        session=resolved_session,
        agent=resolved_agent,
        host=resolved_host,
        day=resolved_day,
    )
