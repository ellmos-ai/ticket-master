#!/usr/bin/env python3
"""Optional bridges to ``agenda-setting`` and ``kalender`` / Ocean calendar.

Standard-integration rules (P-009):
- Reuses ``lib/config_paths.py`` for ``<HOME>``/``<USER>`` placeholder expansion
  and ``tickets_dir`` precedence (explicit > environment > config).
- Reads the canonical ``agent-agenda.v1`` state (``profile.json`` /
  ``AGENT_AGENDA_PROFILE`` / ``<state_root>/agenda.json``) without inventing a
  second agenda store; disabled or expired agendas report ``agenda_active: False``
  and never apply active item weights.
- Reads and (only with explicit ``--apply``) idempotently writes to either the
  ``kalender`` SQLite ``events`` schema or the Ocean ``assistant_calendar``
  table (checking ``sqlite_master`` + ``PRAGMA table_info(assistant_calendar)``
  read-only first, setting ``event_origin='user'`` iff that column exists, and
  failing closed if ``assistant_calendar`` is absent).
"""

from __future__ import annotations

import argparse
import calendar
import copy
import json
import os
import re
import sqlite3
import sys
import uuid
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

try:
    from . import config_paths, routing_contract
except ImportError:
    import config_paths
    import routing_contract

__all__ = [
    "AGENDA_PROFILE_ENV",
    "AGENDA_SKILL_ENV",
    "BACH_DB_ENV",
    "DEFAULT_AGENDA_BRIDGE_CONFIG",
    "DEFAULT_CALENDAR_BRIDGE_CONFIG",
    "DEFAULT_SORT_STATUSES",
    "KALENDER_SKILL_ENV",
    "KALENDER_STORE_ENV",
    "VALID_CALENDAR_BACKENDS",
    "apply_calendar_plan",
    "boot_offer",
    "detect_agenda_skill",
    "detect_calendar_skill",
    "detect_ocean_calendar",
    "format_calendar_plan_text",
    "format_planning_status_text",
    "format_sorted_tickets_text",
    "load_agenda",
    "plan_tickets_to_calendar",
    "planning_status",
    "resolve_kalender_store",
    "sort_tickets_by_agenda",
]

AGENDA_SKILL_ENV = "TM_AGENDA_SKILL_PATH"
KALENDER_SKILL_ENV = "TM_KALENDER_SKILL_PATH"
AGENDA_PROFILE_ENV = "AGENT_AGENDA_PROFILE"
KALENDER_STORE_ENV = "KALENDER_STORE"
BACH_DB_ENV = "BACH_DB"

VALID_CALENDAR_BACKENDS = ("auto", "kalender", "ocean")
DEFAULT_SORT_STATUSES = ("ACTIONABLE", "QUEUED")
ALLOWED_LIFECYCLE_STATUSES = (
    "INBOX",
    "ACTIONABLE",
    "QUEUED",
    "BLOCKED",
    "WAITING",
    "USER",
    "PARKED",
    "PENDING",
    ".USER",
)

DEFAULT_AGENDA_BRIDGE_CONFIG: dict[str, Any] = {
    "enabled": False,
    "auto_detect": True,
    "skill_path": None,
    "profile_path": None,
    "state_root": None,
    "sort_on_start": True,
}

DEFAULT_CALENDAR_BRIDGE_CONFIG: dict[str, Any] = {
    "enabled": False,
    "auto_detect": True,
    "skill_path": None,
    "backend": "auto",
    "kalender_store": None,
    "ocean_db_path": None,
    "ticket_calendar_id": "tickets",
    "mirror_to_user": False,
    "workday_start": "09:00",
    "workday_end": "17:00",
    "default_slot_minutes": 60,
    "max_tickets_per_day": 3,
    "planning_horizon_days": 14,
    "include_weekends": False,
}

_KALENDER_EVENTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id           TEXT PRIMARY KEY,
    title        TEXT NOT NULL,
    date         TEXT NOT NULL,
    time         TEXT,
    duration_min INTEGER,
    location     TEXT,
    description  TEXT,
    recurrence   TEXT,
    ics_uid      TEXT UNIQUE,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);
"""

_KALENDER_V02_SCHEMA = """
CREATE TABLE IF NOT EXISTS calendars (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    category    TEXT NOT NULL DEFAULT 'general',
    color       TEXT NOT NULL DEFAULT '#2563eb',
    description TEXT NOT NULL DEFAULT '',
    is_default  INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id                 TEXT PRIMARY KEY,
    title              TEXT NOT NULL,
    date               TEXT NOT NULL,
    time               TEXT,
    duration_min       INTEGER,
    location           TEXT,
    description        TEXT,
    recurrence         TEXT,
    ics_uid            TEXT UNIQUE,
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL,
    calendar_id        TEXT NOT NULL DEFAULT 'user',
    category           TEXT,
    source_calendar_id TEXT,
    source_event_id    TEXT,
    link_mode          TEXT,
    external_id        TEXT,
    end_time           TEXT,
    status             TEXT DEFAULT 'planned'
);
CREATE INDEX IF NOT EXISTS idx_events_date ON events(date);
CREATE INDEX IF NOT EXISTS idx_events_calendar_date ON events(calendar_id, date);
CREATE INDEX IF NOT EXISTS idx_events_external_id ON events(external_id);
"""

_KALENDER_V02_COLUMNS = frozenset({
    "calendar_id",
    "category",
    "source_calendar_id",
    "source_event_id",
    "link_mode",
    "external_id",
    "end_time",
    "status",
})

_KALENDER_SILO_DEFAULTS: dict[str, tuple[str, str, str, str, int]] = {
    "user": ("Nutzerkalender", "personal", "#2563eb", "Persönlicher Hauptkalender", 1),
    "tickets": ("Ticket-Kalender", "tasks", "#7c3aed", "Geplante Arbeitsblöcke aus ticket-master", 0),
}
_CALENDAR_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

_REQUIRED_OCEAN_READ_COLUMNS = {"id", "title", "start_datetime"}
_REQUIRED_OCEAN_WRITE_COLUMNS = {
    "title",
    "description",
    "start_datetime",
    "end_datetime",
    "event_type",
    "status",
    "external_id",
    "created_at",
    "updated_at",
}
_CANCELLED_OCEAN_STATUSES = {"cancelled", "canceled", "abgesagt", "deleted"}

_TIMEFRAME_ATOM = re.compile(r"^(\d{4})(?:-(?:(0[1-9]|1[0-2])|Q([1-4])|H([12])))?$")
_HHMM_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
_TICKET_NAME_RE = re.compile(
    r"^T-(\d{8})-(\d+)(?:_[A-Za-z0-9_-]+)?"
    r"(?:\.[A-Za-z0-9_-]+)?\.txt$"
)
_ID_LINE_RE = re.compile(r"^ID:\s*(T-\d{8}-\d+)", re.IGNORECASE)
_HEADER_RE = re.compile(r"^([A-Z_ÄÖÜ]{2,32}):\s*(.*)$")
_TOKEN_RE = re.compile(r"[A-Za-z0-9äöüÄÖÜß][A-Za-z0-9äöüÄÖÜß_-]{1,}")

_STOPWORDS = frozenset({
    "aber", "alle", "als", "also", "auch", "auf", "aus", "bei", "beim", "bis",
    "bitte", "das", "dass", "dem", "den", "der", "des", "die", "dies", "diese",
    "dieser", "dieses", "durch", "ein", "eine", "einem", "einen", "einer",
    "eines", "erst", "fuer", "für", "gegen", "haben", "hat", "hier", "ich",
    "ihr", "ihre", "immer", "ist", "jede", "jeder", "jetzt", "kann", "kein",
    "keine", "mit", "muss", "nach", "neue", "neuer", "neues", "nicht", "noch",
    "nur", "oder", "ohne", "sein", "seine", "sich", "sie", "sind", "soll",
    "ueber", "über", "und", "uns", "unser", "unter", "vom", "von", "vor",
    "wann", "warum", "was", "weil", "wenn", "wer", "werden", "wie", "wir",
    "wird", "wurde", "zum", "zur",
    "about", "after", "all", "also", "and", "any", "are", "been", "before",
    "between", "both", "but", "can", "could", "does", "each", "for", "from",
    "has", "have", "into", "its", "just", "more", "most", "must", "new",
    "not", "now", "one", "only", "other", "our", "out", "over", "should",
    "some", "such", "than", "that", "the", "their", "them", "then", "there",
    "these", "they", "this", "through", "under", "until", "use", "using",
    "very", "via", "was", "were", "what", "when", "where", "which", "while",
    "who", "will", "with", "within", "without", "would", "your",
    "ticket", "tickets", "task", "tasks", "issue", "problem", "project",
    "projekt", "software", "pending", "open", "offen",
})


# ---------------------------------------------------------------------------
# Config extraction & path helpers
# ---------------------------------------------------------------------------

def _agenda_cfg(config: dict[str, Any] | None) -> dict[str, Any]:
    merged = dict(DEFAULT_AGENDA_BRIDGE_CONFIG)
    if not isinstance(config, dict):
        return merged
    sub = config.get("agenda_bridge")
    if isinstance(sub, dict):
        merged.update(sub)
    elif any(key in config for key in DEFAULT_AGENDA_BRIDGE_CONFIG):
        for key in DEFAULT_AGENDA_BRIDGE_CONFIG:
            if key in config:
                merged[key] = config[key]
    return merged


def _calendar_cfg(config: dict[str, Any] | None) -> dict[str, Any]:
    merged = dict(DEFAULT_CALENDAR_BRIDGE_CONFIG)
    if not isinstance(config, dict):
        return merged
    sub = config.get("calendar_bridge")
    if isinstance(sub, dict):
        merged.update(sub)
    elif any(key in config for key in DEFAULT_CALENDAR_BRIDGE_CONFIG):
        for key in DEFAULT_CALENDAR_BRIDGE_CONFIG:
            if key in config:
                merged[key] = config[key]
    return merged


def _skill_candidates(
    skill_name: str,
    explicit_raw: Any,
    env_var: str,
    *,
    auto_detect: bool,
) -> list[tuple[str, Path]]:
    explicit_path = config_paths.resolve_config_path(explicit_raw)
    if explicit_path is not None:
        return [("config", explicit_path)]
    env_path = config_paths.resolve_config_path(os.environ.get(env_var))
    if env_path is not None:
        return [("env", env_path)]
    if not auto_detect:
        return []
    home = Path.home()
    return [
        ("auto:claude", home / ".claude" / "skills" / skill_name),
        ("auto:agents", home / ".agents" / "skills" / skill_name),
        ("auto:codex", home / ".codex" / "skills" / skill_name),
    ]


def _connect_sqlite_readonly(db_path: Path) -> sqlite3.Connection:
    resolved = db_path.resolve()
    try:
        conn = sqlite3.connect(f"file:{resolved.as_posix()}?mode=ro", uri=True)
    except sqlite3.OperationalError:
        conn = sqlite3.connect(str(resolved))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


# ---------------------------------------------------------------------------
# Auto-Discovery & Status
# ---------------------------------------------------------------------------

def detect_agenda_skill(config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Locate ``agenda-setting`` (``SKILL.md`` + ``scripts/agenda.py``)."""
    cfg = _agenda_cfg(config)
    candidates = _skill_candidates(
        "agenda-setting",
        cfg.get("skill_path"),
        AGENDA_SKILL_ENV,
        auto_detect=bool(cfg.get("auto_detect", True)),
    )
    if not candidates:
        return {
            "found": False,
            "status": "absent",
            "skill_path": None,
            "script_path": None,
            "skill_md": None,
            "source": "none",
        }
    last_checked: Path | None = None
    last_source = "none"
    for source, candidate in candidates:
        last_checked = candidate
        last_source = source
        try:
            if candidate.exists() and not candidate.is_dir():
                return {
                    "found": False,
                    "status": "error",
                    "skill_path": str(candidate),
                    "script_path": None,
                    "skill_md": None,
                    "source": source,
                    "error": "skill_path is not a directory",
                }
            skill_md = candidate / "SKILL.md"
            script_py = candidate / "scripts" / "agenda.py"
            if candidate.is_dir() and skill_md.is_file() and script_py.is_file():
                return {
                    "found": True,
                    "status": "ready",
                    "skill_path": str(candidate),
                    "script_path": str(script_py),
                    "skill_md": str(skill_md),
                    "source": source,
                }
        except OSError as exc:
            return {
                "found": False,
                "status": "error",
                "skill_path": str(candidate),
                "script_path": None,
                "skill_md": None,
                "source": source,
                "error": str(exc),
            }
    return {
        "found": False,
        "status": "absent",
        "skill_path": str(last_checked) if last_source in {"config", "env"} and last_checked else None,
        "script_path": None,
        "skill_md": None,
        "source": last_source,
    }


def detect_calendar_skill(config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Locate ``kalender`` (``SKILL.md`` + ``kalender_core.py``)."""
    cfg = _calendar_cfg(config)
    candidates = _skill_candidates(
        "kalender",
        cfg.get("skill_path"),
        KALENDER_SKILL_ENV,
        auto_detect=bool(cfg.get("auto_detect", True)),
    )
    if not candidates:
        return {
            "found": False,
            "status": "absent",
            "skill_path": None,
            "core_path": None,
            "skill_md": None,
            "default_store": None,
            "source": "none",
        }
    last_checked: Path | None = None
    last_source = "none"
    for source, candidate in candidates:
        last_checked = candidate
        last_source = source
        try:
            if candidate.exists() and not candidate.is_dir():
                return {
                    "found": False,
                    "status": "error",
                    "skill_path": str(candidate),
                    "core_path": None,
                    "skill_md": None,
                    "default_store": None,
                    "source": source,
                    "error": "skill_path is not a directory",
                }
            skill_md = candidate / "SKILL.md"
            core_py = candidate / "kalender_core.py"
            if candidate.is_dir() and skill_md.is_file() and core_py.is_file():
                return {
                    "found": True,
                    "status": "ready",
                    "skill_path": str(candidate),
                    "core_path": str(core_py),
                    "skill_md": str(skill_md),
                    "default_store": str(candidate / "store.db"),
                    "source": source,
                }
        except OSError as exc:
            return {
                "found": False,
                "status": "error",
                "skill_path": str(candidate),
                "core_path": None,
                "skill_md": None,
                "default_store": None,
                "source": source,
                "error": str(exc),
            }
    return {
        "found": False,
        "status": "absent",
        "skill_path": str(last_checked) if last_source in {"config", "env"} and last_checked else None,
        "core_path": None,
        "skill_md": None,
        "default_store": None,
        "source": last_source,
    }


def resolve_kalender_store(config: dict[str, Any] | None = None) -> tuple[Path | None, str]:
    """Resolve the SQLite ``store.db`` path for the ``kalender`` backend."""
    cfg = _calendar_cfg(config)
    explicit = config_paths.resolve_config_path(cfg.get("kalender_store"))
    if explicit is not None:
        return explicit, "config"
    env_store = config_paths.resolve_config_path(os.environ.get(KALENDER_STORE_ENV))
    if env_store is not None:
        return env_store, "env"
    skill_info = detect_calendar_skill(config)
    if skill_info["found"] and skill_info.get("default_store"):
        return Path(skill_info["default_store"]), f"skill:{skill_info['source']}"
    return None, "none"


def _inspect_kalender_store(store_path: Path | None, source: str, skill_info: dict[str, Any]) -> dict[str, Any]:
    if skill_info["status"] == "error" and source == "none":
        return {
            "found": False,
            "status": "error",
            "store_path": None,
            "store_exists": False,
            "source": skill_info["source"],
            "skill": skill_info,
            "error": skill_info.get("error", "invalid kalender skill path"),
        }
    if store_path is None:
        return {
            "found": False,
            "status": "absent",
            "store_path": None,
            "store_exists": False,
            "source": "none",
            "skill": skill_info,
        }
    try:
        if store_path.exists() and store_path.is_dir():
            return {
                "found": False,
                "status": "error",
                "store_path": str(store_path),
                "store_exists": False,
                "source": source,
                "skill": skill_info,
                "error": "kalender_store points to a directory",
            }
        store_exists = store_path.is_file()
        has_events_table = False
        kalender_schema: str | None = None
        if store_exists:
            conn = _connect_sqlite_readonly(store_path)
            try:
                row = conn.execute(
                    "SELECT type FROM sqlite_master WHERE name = 'events'"
                ).fetchone()
                has_events_table = bool(row and row[0] == "table")
                if has_events_table:
                    ev_cols = {
                        str(r[1])
                        for r in conn.execute("PRAGMA table_info(events)").fetchall()
                    }
                    kalender_schema = "v0.2" if (_KALENDER_V02_COLUMNS <= ev_cols) else "v0.1"
            finally:
                conn.close()
        return {
            "found": True,
            "status": "ready",
            "store_path": str(store_path),
            "store_exists": store_exists,
            "has_events_table": has_events_table,
            "kalender_schema": kalender_schema,
            "source": source,
            "skill": skill_info,
        }
    except (OSError, sqlite3.DatabaseError) as exc:
        return {
            "found": False,
            "status": "error",
            "store_path": str(store_path),
            "store_exists": False,
            "source": source,
            "skill": skill_info,
            "error": str(exc),
        }


def detect_ocean_calendar(config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Read-only inspection of the Ocean ``assistant_calendar`` SQLite contract.

    Checks ``ocean_db_path`` -> ``BACH_DB`` -> ``~/.bach/bach.db`` (when
    ``auto_detect`` is enabled). Never creates a database file and never alters
    schema.
    """
    cfg = _calendar_cfg(config)
    db_path = config_paths.resolve_config_path(cfg.get("ocean_db_path"))
    source = "config"
    if db_path is None:
        db_path = config_paths.resolve_config_path(os.environ.get(BACH_DB_ENV))
        source = "env"
    if db_path is None:
        if not bool(cfg.get("auto_detect", True)):
            return {
                "found": False,
                "status": "absent",
                "db_path": None,
                "source": "none",
                "has_assistant_calendar": False,
                "has_event_origin": False,
                "columns": [],
            }
        db_path = Path.home() / ".bach" / "bach.db"
        source = "auto:home"

    try:
        if db_path.exists() and db_path.is_dir():
            return {
                "found": False,
                "status": "error",
                "db_path": str(db_path),
                "source": source,
                "has_assistant_calendar": False,
                "has_event_origin": False,
                "columns": [],
                "error": "ocean_db_path points to a directory",
            }
        if not db_path.is_file():
            return {
                "found": False,
                "status": "absent",
                "db_path": str(db_path),
                "source": source,
                "has_assistant_calendar": False,
                "has_event_origin": False,
                "columns": [],
            }
        conn = _connect_sqlite_readonly(db_path)
        try:
            canonical = conn.execute(
                "SELECT type FROM sqlite_master WHERE name = 'assistant_calendar'"
            ).fetchone()
            if not canonical or canonical[0] != "table":
                return {
                    "found": False,
                    "status": "absent",
                    "db_path": str(db_path),
                    "source": source,
                    "has_assistant_calendar": False,
                    "has_event_origin": False,
                    "columns": [],
                    "reason": "assistant_calendar table missing",
                }
            info_rows = conn.execute("PRAGMA table_info(assistant_calendar)").fetchall()
            columns = [str(row[1]) for row in info_rows]
            if not (_REQUIRED_OCEAN_READ_COLUMNS <= set(columns)):
                return {
                    "found": False,
                    "status": "error",
                    "db_path": str(db_path),
                    "source": source,
                    "has_assistant_calendar": True,
                    "has_event_origin": "event_origin" in columns,
                    "columns": columns,
                    "error": "assistant_calendar missing required columns",
                }
            return {
                "found": True,
                "status": "ready",
                "db_path": str(db_path),
                "source": source,
                "has_assistant_calendar": True,
                "has_event_origin": "event_origin" in columns,
                "columns": columns,
            }
        finally:
            conn.close()
    except (OSError, sqlite3.DatabaseError) as exc:
        return {
            "found": False,
            "status": "error",
            "db_path": str(db_path),
            "source": source,
            "has_assistant_calendar": False,
            "has_event_origin": False,
            "columns": [],
            "error": str(exc),
        }


def _select_calendar_backend(
    requested_backend: str,
    cal_cfg: dict[str, Any],
    kalender_info: dict[str, Any],
    ocean_info: dict[str, Any],
) -> tuple[str | None, str, str | None]:
    """Return ``(selected_backend, status, error_or_reason)``."""
    if requested_backend not in VALID_CALENDAR_BACKENDS:
        return None, "error", f"invalid calendar backend {requested_backend!r}"
    if requested_backend == "kalender":
        return ("kalender" if kalender_info["status"] == "ready" else None,
                kalender_info["status"],
                kalender_info.get("error"))
    if requested_backend == "ocean":
        return ("ocean" if ocean_info["status"] == "ready" else None,
                ocean_info["status"],
                ocean_info.get("error") or ocean_info.get("reason"))

    # auto mode: honour explicit config overrides first, otherwise prefer
    # kalender when ready, then ocean when ready.
    explicit_ocean = config_paths.resolve_config_path(cal_cfg.get("ocean_db_path")) is not None
    explicit_kal = config_paths.resolve_config_path(cal_cfg.get("kalender_store")) is not None
    if explicit_ocean and not explicit_kal and ocean_info["status"] == "ready":
        return "ocean", "ready", None
    if kalender_info["status"] == "ready":
        return "kalender", "ready", None
    if ocean_info["status"] == "ready":
        return "ocean", "ready", None
    if kalender_info["status"] == "error" or ocean_info["status"] == "error":
        err = kalender_info.get("error") or ocean_info.get("error")
        return None, "error", err
    return None, "absent", "neither kalender skill/store nor ocean assistant_calendar found"


def planning_status(
    config: dict[str, Any] | None = None,
    *,
    include_detection: bool = False,
    backend_override: str | None = None,
) -> dict[str, Any]:
    """Return deterministic status for ``agenda_bridge`` and ``calendar_bridge``.

    When both bridges are disabled and ``include_detection`` is False (the
    ``session_boot.report()`` contract), returns the compact object::

        {"agenda": {"status": "disabled"}, "calendar": {"status": "disabled"}}
    """
    ag_cfg = _agenda_cfg(config)
    cal_cfg = _calendar_cfg(config)
    if backend_override is not None:
        cal_cfg = {**cal_cfg, "backend": backend_override}

    ag_enabled = bool(ag_cfg.get("enabled", False))
    cal_enabled = bool(cal_cfg.get("enabled", False))

    if not ag_enabled and not cal_enabled and not include_detection:
        return {
            "agenda": {"status": "disabled"},
            "calendar": {"status": "disabled"},
        }

    # Agenda inspection
    if not ag_enabled and not include_detection:
        agenda_report: dict[str, Any] = {"status": "disabled"}
    else:
        skill_info = detect_agenda_skill(config)
        agenda_data = load_agenda(config)
        if not ag_enabled:
            ag_status = "disabled"
        elif skill_info["status"] == "error" or agenda_data["status"] == "error":
            ag_status = "error"
        elif skill_info["found"] or agenda_data["status"] in {"ready", "disabled", "expired"}:
            # Ready when the skill is installed OR an explicit/canonical agenda.json state exists.
            ag_status = "ready" if (skill_info["found"] or agenda_data["agenda_exists"]) else "absent"
        else:
            ag_status = "absent"
        agenda_report = {
            "status": ag_status,
            "enabled": ag_enabled,
            "sort_on_start": bool(ag_cfg.get("sort_on_start", True)),
            "skill": skill_info,
            "agenda_state": {
                "state_status": agenda_data["status"],
                "agenda_active": agenda_data["agenda_active"],
                "agenda_enabled": agenda_data["enabled"],
                "expired": agenda_data["expired"],
                "deadline": agenda_data["deadline"],
                "name": agenda_data["name"],
                "revision": agenda_data["revision"],
                "total_items": len(agenda_data["items"]),
                "open_items": len(agenda_data["open_items"]),
                "state_root": agenda_data["state_root"],
                "agenda_path": agenda_data["agenda_path"],
            },
        }
        if agenda_data.get("error"):
            agenda_report["error"] = agenda_data["error"]
        elif skill_info.get("error"):
            agenda_report["error"] = skill_info["error"]

    # Calendar inspection
    if not cal_enabled and not include_detection:
        calendar_report: dict[str, Any] = {"status": "disabled"}
    else:
        kal_skill = detect_calendar_skill(config)
        kal_store, kal_source = resolve_kalender_store(config)
        kal_info = _inspect_kalender_store(kal_store, kal_source, kal_skill)
        ocean_info = detect_ocean_calendar(config)
        requested_backend = str(cal_cfg.get("backend") or "auto").strip().lower()
        selected, detected_status, reason = _select_calendar_backend(
            requested_backend, cal_cfg, kal_info, ocean_info
        )
        cal_status = "disabled" if not cal_enabled else detected_status
        calendar_report = {
            "status": cal_status,
            "enabled": cal_enabled,
            "requested_backend": requested_backend,
            "selected_backend": selected,
            "ticket_calendar_id": str(cal_cfg.get("ticket_calendar_id") or "tickets").strip().lower(),
            "mirror_to_user": bool(cal_cfg.get("mirror_to_user", False)),
            "workday_start": cal_cfg.get("workday_start", "09:00"),
            "workday_end": cal_cfg.get("workday_end", "17:00"),
            "default_slot_minutes": cal_cfg.get("default_slot_minutes", 60),
            "max_tickets_per_day": cal_cfg.get("max_tickets_per_day", 3),
            "planning_horizon_days": cal_cfg.get("planning_horizon_days", 14),
            "include_weekends": bool(cal_cfg.get("include_weekends", False)),
            "backends": {
                "kalender": kal_info,
                "ocean": ocean_info,
            },
        }
        if reason and cal_status in {"absent", "error"}:
            calendar_report["reason"] = reason

    return {
        "agenda": agenda_report,
        "calendar": calendar_report,
    }


def boot_offer(config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Data-only summary for ``lib/boot_menu.py`` ``offer()``."""
    status = planning_status(config, include_detection=True)
    ag = status["agenda"]
    cal = status["calendar"]
    ag_detected = bool(
        ag.get("enabled")
        or ag.get("skill", {}).get("found")
        or ag.get("agenda_state", {}).get("agenda_active")
    )
    cal_backends = cal.get("backends", {})
    cal_detected = bool(
        cal.get("enabled")
        or cal_backends.get("kalender", {}).get("found")
        or cal_backends.get("ocean", {}).get("found")
    )
    available = ag_detected or cal_detected
    return {
        "available": available,
        "agenda": {
            "status": ag["status"],
            "enabled": ag.get("enabled", False),
            "skill_found": bool(ag.get("skill", {}).get("found")),
            "skill_path": ag.get("skill", {}).get("skill_path"),
            "agenda_active": bool(ag.get("agenda_state", {}).get("agenda_active")),
            "open_items": ag.get("agenda_state", {}).get("open_items", 0),
        },
        "calendar": {
            "status": cal["status"],
            "enabled": cal.get("enabled", False),
            "selected_backend": cal.get("selected_backend"),
            "kalender_found": bool(cal_backends.get("kalender", {}).get("found")),
            "ocean_found": bool(cal_backends.get("ocean", {}).get("found")),
        },
        "actions": (
            ["--planning-status", "--sort-by-agenda", "--plan-calendar"]
            if available
            else []
        ),
    }


# ---------------------------------------------------------------------------
# Agenda loading & timeframe parsing (compatible with agent-agenda.v1)
# ---------------------------------------------------------------------------

def _parse_timeframe_atom(atom: str) -> tuple[date, date]:
    match = _TIMEFRAME_ATOM.fullmatch(atom)
    if not match:
        raise ValueError(f"invalid timeframe: {atom!r}")
    year = int(match.group(1))
    if not 1900 <= year <= 2999:
        raise ValueError(f"timeframe year out of range: {atom!r}")
    if match.group(2):
        month = int(match.group(2))
        return date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])
    if match.group(3):
        quarter = int(match.group(3))
        m1 = 3 * (quarter - 1) + 1
        m2 = 3 * quarter
        return date(year, m1, 1), date(year, m2, calendar.monthrange(year, m2)[1])
    if match.group(4):
        half = int(match.group(4))
        m1, m2 = (1, 6) if half == 1 else (7, 12)
        return date(year, m1, 1), date(year, m2, calendar.monthrange(year, m2)[1])
    return date(year, 1, 1), date(year, 12, 31)


def _timeframe_bounds(text: str | None) -> tuple[date | None, date | None, bool, bool]:
    if not text:
        return None, None, False, False
    clean = text.strip()
    if clean == "ongoing":
        return None, None, True, True
    if ".." in clean:
        parts = clean.split("..")
        if len(parts) != 2 or not parts[0] or not parts[1]:
            raise ValueError(f"invalid timeframe range: {clean!r}")
        s1, e1 = _parse_timeframe_atom(parts[0])
        s2, e2 = _parse_timeframe_atom(parts[1])
        if s1 > s2 or e1 > e2:
            raise ValueError(f"reversed timeframe range: {clean!r}")
        return s1, e2, False, False
    if clean.endswith("+"):
        s1, e1 = _parse_timeframe_atom(clean[:-1])
        return s1, e1, True, False
    s1, e1 = _parse_timeframe_atom(clean)
    return s1, e1, False, False


def _parse_agenda_deadline(value: str) -> datetime:
    clean = value.strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", clean):
        return datetime.combine(date.fromisoformat(clean), time.max, timezone.utc)
    parsed = datetime.fromisoformat(clean.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("agenda deadline requires timezone or YYYY-MM-DD")
    return parsed.astimezone(timezone.utc)


def _resolve_agenda_state_root(ag_cfg: dict[str, Any]) -> tuple[Path, Path | None]:
    explicit_root = config_paths.resolve_config_path(ag_cfg.get("state_root"))
    explicit_profile = config_paths.resolve_config_path(ag_cfg.get("profile_path"))
    env_profile = config_paths.resolve_config_path(os.environ.get(AGENDA_PROFILE_ENV))
    profile_path = explicit_profile or env_profile

    if explicit_root is not None:
        return explicit_root, profile_path

    candidate_profile = profile_path or (Path.home() / ".config" / "agent-agenda" / "profile.json")
    if candidate_profile.is_file():
        raw_prof = json.loads(candidate_profile.read_text(encoding="utf-8-sig"))
        if not isinstance(raw_prof, dict) or raw_prof.get("schema") != "agent-agenda-profile.v1":
            raise ValueError(f"invalid agenda profile schema: {candidate_profile}")
        prof_root = config_paths.resolve_config_path(raw_prof.get("state_root"))
        if prof_root is not None:
            return prof_root, candidate_profile
    elif profile_path is not None:
        # Caller explicitly set profile_path / AGENT_AGENDA_PROFILE to a missing file -> surface clearly
        raise ValueError(f"configured agenda profile not found: {profile_path}")

    return Path.home() / ".local" / "state" / "agent-agenda", (
        candidate_profile if candidate_profile.is_file() else None
    )


def load_agenda(
    config: dict[str, Any] | None = None,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Load and validate ``agent-agenda.v1`` without mutating state."""
    ag_cfg = _agenda_cfg(config)
    ref_now = now or datetime.now(timezone.utc)
    try:
        root, used_profile = _resolve_agenda_state_root(ag_cfg)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return {
            "status": "error",
            "agenda_exists": False,
            "agenda_active": False,
            "enabled": False,
            "expired": False,
            "agenda_id": None,
            "name": "Agenda",
            "revision": 0,
            "deadline": None,
            "deadline_date": None,
            "state_root": str(ag_cfg.get("state_root") or ""),
            "agenda_path": "",
            "profile_path": str(ag_cfg.get("profile_path") or ""),
            "items": [],
            "open_items": [],
            "error": str(exc),
        }

    agenda_path = root / "agenda.json"
    if not agenda_path.is_file():
        return {
            "status": "absent",
            "agenda_exists": False,
            "agenda_active": False,
            "enabled": False,
            "expired": False,
            "agenda_id": None,
            "name": "Agenda",
            "revision": 0,
            "deadline": None,
            "deadline_date": None,
            "state_root": str(root),
            "agenda_path": str(agenda_path),
            "profile_path": str(used_profile) if used_profile else None,
            "items": [],
            "open_items": [],
        }

    try:
        if agenda_path.stat().st_size > 2_000_000:
            raise ValueError("agenda.json exceeds 2 MB limit")
        data = json.loads(agenda_path.read_text(encoding="utf-8-sig"))
        if (
            not isinstance(data, dict)
            or data.get("schema") != "agent-agenda.v1"
            or type(data.get("revision")) is not int
            or data["revision"] < 0
            or type(data.get("enabled")) is not bool
            or not isinstance(data.get("items"), list)
        ):
            raise ValueError("invalid agent-agenda.v1 state")

        deadline_raw = data.get("deadline")
        deadline_dt: datetime | None = None
        if deadline_raw is not None:
            if not isinstance(deadline_raw, str) or not deadline_raw.strip():
                raise ValueError("invalid agenda deadline")
            deadline_dt = _parse_agenda_deadline(deadline_raw)

        normalized_items: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        for raw_item in data["items"]:
            if not isinstance(raw_item, dict):
                raise ValueError("agenda item must be an object")
            item_id = str(raw_item.get("id") or "").strip()
            item_text = str(raw_item.get("text") or raw_item.get("title") or "").strip()
            status = str(raw_item.get("status") or "pending").strip()
            weight = raw_item.get("weight", 50)
            if (
                not re.fullmatch(r"A[1-9][0-9]{0,2}", item_id)
                or item_id in seen_ids
                or not item_text
                or status not in {"pending", "done"}
                or type(weight) is not int
                or not 1 <= weight <= 100
            ):
                raise ValueError(f"invalid agenda item: {item_id or '<missing>'}")
            seen_ids.add(item_id)
            tf = raw_item.get("timeframe")
            if tf is not None:
                if not isinstance(tf, str):
                    raise ValueError(f"invalid timeframe on {item_id}")
                _timeframe_bounds(tf)
            plans_raw = raw_item.get("plans") or []
            if not isinstance(plans_raw, list):
                raise ValueError(f"invalid plans list on {item_id}")
            normalized_items.append({
                "id": item_id,
                "text": item_text,
                "weight": weight,
                "status": status,
                "timeframe": tf.strip() if isinstance(tf, str) and tf.strip() else None,
                "owner": raw_item.get("owner"),
                "category": (
                    str(raw_item["category"]).strip().lower()
                    if isinstance(raw_item.get("category"), str) and raw_item["category"].strip()
                    else None
                ),
                "plans": [p for p in plans_raw if isinstance(p, dict)],
            })

        normalized_items.sort(key=lambda item: (-item["weight"], int(item["id"][1:])))
        expired = bool(deadline_dt is not None and deadline_dt <= ref_now)
        enabled = bool(data["enabled"])
        agenda_active = bool(enabled and deadline_dt is not None and not expired)
        if agenda_active:
            state_status = "ready"
        elif expired:
            state_status = "expired"
        else:
            state_status = "disabled"

        open_items = [item for item in normalized_items if item["status"] == "pending"]
        return {
            "status": state_status,
            "agenda_exists": True,
            "agenda_active": agenda_active,
            "enabled": enabled,
            "expired": expired,
            "agenda_id": data.get("agenda_id"),
            "name": str(data.get("name") or "Agenda"),
            "revision": data["revision"],
            "deadline": deadline_raw,
            "deadline_date": deadline_dt.date().isoformat() if deadline_dt else None,
            "state_root": str(root),
            "agenda_path": str(agenda_path),
            "profile_path": str(used_profile) if used_profile else None,
            "items": normalized_items,
            "open_items": open_items,
        }
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return {
            "status": "error",
            "agenda_exists": True,
            "agenda_active": False,
            "enabled": False,
            "expired": False,
            "agenda_id": None,
            "name": "Agenda",
            "revision": 0,
            "deadline": None,
            "deadline_date": None,
            "state_root": str(root),
            "agenda_path": str(agenda_path),
            "profile_path": str(used_profile) if used_profile else None,
            "items": [],
            "open_items": [],
            "error": str(exc),
        }


# ---------------------------------------------------------------------------
# Ticket parsing & Agenda-driven sorting
# ---------------------------------------------------------------------------

def _tokenize(text: str) -> set[str]:
    tokens: set[str] = set()
    for raw_match in _TOKEN_RE.findall(text.lower()):
        cleaned = raw_match.strip("_-")
        if len(cleaned) >= 3 and cleaned not in _STOPWORDS:
            tokens.add(cleaned)
        if "-" in cleaned or "_" in cleaned:
            for part in re.split(r"[-_]+", cleaned):
                if len(part) >= 3 and part not in _STOPWORDS:
                    tokens.add(part)
    return tokens


def _priority_info(raw_priority: str, raw_urgency: str) -> tuple[int, str]:
    combined = f"{raw_priority} {raw_urgency}".lower()
    tokens = set(re.findall(r"[a-z0-9äöüß]+", combined))
    if tokens & {"p1", "1", "critical", "kritisch", "sofort", "high", "hoch", "urgent", "blocker"}:
        return 3, "high"
    if tokens & {"p3", "3", "low", "niedrig", "backlog", "woche", "later", "minor"}:
        if not (tokens & {"p2", "2", "medium", "mittel", "heute", "normal"}):
            return 1, "low"
    if tokens & {"p2", "2", "medium", "mittel", "heute", "normal"}:
        return 2, "medium"
    return 2, "medium"


def _ticket_id_from_file(path: Path, text: str) -> str:
    for line in text.splitlines():
        match = _ID_LINE_RE.match(line.strip())
        if match:
            return match.group(1)
    match = _TICKET_NAME_RE.match(path.name)
    if match:
        return f"T-{match.group(1)}-{int(match.group(2)):02d}"
    try:
        parsed = routing_contract.parse_ticket_name(path.name)
        return f"T-{parsed.date}-{parsed.number}"
    except routing_contract.RoutingContractError:
        return path.stem


def _is_ticket_filename(name: str) -> bool:
    if _TICKET_NAME_RE.match(name):
        return True
    try:
        routing_contract.parse_ticket_name(name)
        return True
    except routing_contract.RoutingContractError:
        return False


def _normalize_include_statuses(include_statuses: Iterable[str] | str | None) -> tuple[str, ...]:
    if include_statuses is None:
        return DEFAULT_SORT_STATUSES
    if isinstance(include_statuses, str):
        parts = [p.strip().upper() for p in include_statuses.split(",") if p.strip()]
    else:
        parts = [str(p).strip().upper() for p in include_statuses if str(p).strip()]
    if not parts:
        return DEFAULT_SORT_STATUSES
    valid_set = set(ALLOWED_LIFECYCLE_STATUSES)
    for item in parts:
        if item not in valid_set:
            raise ValueError(
                f"unsupported ticket lifecycle status {item!r}; allowed: {', '.join(ALLOWED_LIFECYCLE_STATUSES)}"
            )
    return tuple(dict.fromkeys(parts))


def _read_open_tickets_for_planning(
    tickets_dir: Path,
    include_statuses: tuple[str, ...],
) -> list[dict[str, Any]]:
    base = Path(tickets_dir).expanduser().resolve()
    scan_dirs: list[tuple[Path, str]] = []
    for folder in include_statuses:
        if folder == "INBOX":
            scan_dirs.append((base, "INBOX"))
            scan_dirs.append((base / "INBOX", "INBOX"))
        else:
            scan_dirs.append((base / folder, folder))

    seen_paths: set[Path] = set()
    tickets: list[dict[str, Any]] = []
    for directory, folder_label in scan_dirs:
        if not directory.is_dir():
            continue
        for entry in sorted(directory.iterdir(), key=lambda p: p.name):
            if not entry.is_file() or entry in seen_paths or not _is_ticket_filename(entry.name):
                continue
            seen_paths.add(entry)
            try:
                text = entry.read_text(encoding="utf-8-sig", errors="replace")
            except OSError:
                continue

            headers: dict[str, str] = {}
            body_lines: list[str] = []
            in_history = False
            for raw_line in text.splitlines():
                stripped = raw_line.strip()
                if stripped.upper().startswith(("HISTORY / LOG", "VERLAUF", "SOLUTION / RESULT", "LOESUNG")):
                    in_history = True
                h_match = _HEADER_RE.match(stripped)
                if h_match and h_match.group(1) not in headers:
                    headers[h_match.group(1)] = h_match.group(2).strip()
                elif not in_history and stripped and not stripped.startswith(("===", "---")):
                    body_lines.append(stripped)

            title = re.sub(
                r"\s+",
                " ",
                headers.get("TITEL") or headers.get("TITLE") or "<ohne Titel>",
            ).strip()[:200]
            raw_status = re.sub(
                r"\s+",
                " ",
                headers.get("STATUS") or folder_label,
            ).strip()[:200]
            raw_priority = headers.get("PRIORITAET") or headers.get("PRIORITY") or "medium"
            raw_urgency = headers.get("URGENCY") or headers.get("DRINGLICHKEIT") or ""
            prio_rank, prio_level = _priority_info(raw_priority, raw_urgency)
            project_dir = headers.get("PROJECT_DIR") or headers.get("PROJEKT") or ""
            pipeline = headers.get("PIPELINE") or ""
            domain = headers.get("DOMAIN") or headers.get("DOMAENE") or ""
            endpoint = headers.get("ENDPOINT") or headers.get("ENDPUNKT") or ""

            try:
                rel_path = entry.relative_to(base).as_posix()
            except ValueError:
                rel_path = entry.name

            tickets.append({
                "id": _ticket_id_from_file(entry, text),
                "title": title,
                "status": raw_status,
                "folder": folder_label,
                "priority": raw_priority,
                "priority_rank": prio_rank,
                "priority_level": prio_level,
                "urgency": raw_urgency,
                "pipeline": pipeline,
                "project_dir": project_dir,
                "domain": domain,
                "endpoint": endpoint,
                "path": rel_path,
                "_header_tokens": _tokenize(
                    " ".join([title, project_dir, pipeline, domain, endpoint])
                ),
                "_body_tokens": _tokenize(" ".join(body_lines[:40])),
            })
    return tickets


def _item_effective_due_date(item: dict[str, Any], agenda_deadline_date: date | None) -> tuple[date | None, bool]:
    """Return ``(effective_end_date, is_open_ended)`` for an agenda item."""
    tf = item.get("timeframe")
    if tf:
        _start, end, is_open, is_ongoing = _timeframe_bounds(tf)
        if is_ongoing:
            return agenda_deadline_date, True
        if is_open:
            return agenda_deadline_date or end, True
        if end is not None:
            if agenda_deadline_date and agenda_deadline_date < end:
                return agenda_deadline_date, False
            return end, False
    return agenda_deadline_date, False


def _urgency_bonus(due_date: date | None, ref_date: date, *, is_open_ended: bool) -> float:
    if due_date is None:
        return 2.0 if is_open_ended else 0.0
    days_left = (due_date - ref_date).days
    if days_left <= 14:
        base = 85.0
    elif days_left <= 31:
        base = 70.0
    elif days_left <= 92:
        base = 50.0
    elif days_left <= 183:
        base = 30.0
    elif days_left <= 366:
        base = 15.0
    else:
        base = 5.0
    if is_open_ended:
        base *= 0.5
    return base


def _match_ticket_to_agenda_item(
    ticket: dict[str, Any],
    item: dict[str, Any],
) -> tuple[int, list[str]]:
    overlap_points = 0
    reasons: list[str] = []

    item_tokens = _tokenize(item["text"])
    header_tokens: set[str] = ticket["_header_tokens"]
    body_tokens: set[str] = ticket["_body_tokens"]

    # 1. Project / linked plan pointer match
    proj_raw = ticket.get("project_dir") or ""
    proj_leaf = Path(proj_raw.replace("\\", "/")).name.lower() if proj_raw and proj_raw != "n/a" else ""
    if proj_leaf and len(proj_leaf) >= 3:
        plan_paths_lower = " ".join(str(p.get("path", "")).lower() for p in item.get("plans", []))
        if proj_leaf in item["text"].lower() or proj_leaf in plan_paths_lower:
            overlap_points += 4
            reasons.append(f"project:{proj_leaf}")

    # 2. Category / domain / pipeline match
    category = item.get("category")
    if category:
        cat_tokens = _tokenize(category) | {category.lower()}
        ticket_meta_tokens = _tokenize(
            f"{ticket.get('domain', '')} {ticket.get('pipeline', '')}"
        )
        if cat_tokens & ticket_meta_tokens:
            overlap_points += 3
            reasons.append(f"category:{category}")
        elif cat_tokens & (header_tokens | body_tokens):
            overlap_points += 2
            reasons.append(f"category_keyword:{category}")

    # 3. Keyword / token overlap
    header_shared = sorted(item_tokens & header_tokens)
    body_shared = sorted((item_tokens & body_tokens) - set(header_shared))
    if header_shared:
        overlap_points += 2 * len(header_shared)
        reasons.append(f"title_keywords:{','.join(header_shared[:5])}")
    if body_shared:
        overlap_points += len(body_shared)
        reasons.append(f"body_keywords:{','.join(body_shared[:5])}")

    return overlap_points, reasons


def sort_tickets_by_agenda(
    tickets_dir: str | Path,
    config: dict[str, Any] | None = None,
    *,
    agenda_data: dict[str, Any] | None = None,
    include_statuses: Iterable[str] | str | None = None,
    reference_date: date | None = None,
) -> dict[str, Any]:
    """Rank open tickets deterministically against the shared agenda."""
    statuses = _normalize_include_statuses(include_statuses)
    base = Path(tickets_dir).expanduser().resolve()
    ref_day = reference_date or date.today()
    ref_now = datetime.combine(ref_day, time(12, 0), tzinfo=timezone.utc)
    agenda = agenda_data if agenda_data is not None else load_agenda(config, now=ref_now)

    agenda_active = bool(agenda.get("agenda_active"))
    agenda_deadline_date = (
        date.fromisoformat(agenda["deadline_date"])
        if agenda.get("deadline_date")
        else None
    )
    open_items = agenda.get("open_items", []) if agenda_active else []

    raw_tickets = _read_open_tickets_for_planning(base, statuses)
    ranked_rows: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    for ticket in raw_tickets:
        matched_item: dict[str, Any] | None = None
        best_overlap = 0
        best_reasons: list[str] = []
        best_due: date | None = None
        best_open_ended = False

        if agenda_active and open_items:
            candidates: list[tuple[tuple[Any, ...], dict[str, Any], int, list[str], date | None, bool]] = []
            for item in open_items:
                pts, item_reasons = _match_ticket_to_agenda_item(ticket, item)
                if pts >= 2:
                    due_dt, is_open = _item_effective_due_date(item, agenda_deadline_date)
                    due_ord = due_dt.toordinal() if due_dt else date.max.toordinal()
                    cand_key = (pts, item["weight"], -due_ord, -int(item["id"][1:]))
                    candidates.append((cand_key, item, pts, item_reasons, due_dt, is_open))
            if candidates:
                candidates.sort(key=lambda c: c[0], reverse=True)
                _, matched_item, best_overlap, best_reasons, best_due, best_open_ended = candidates[0]

        prio_rank = ticket["priority_rank"]
        prio_bonus = float(prio_rank * 10)
        reasons: list[str] = []

        if matched_item is not None:
            weight = int(matched_item["weight"])
            urg_bonus = _urgency_bonus(best_due, ref_day, is_open_ended=best_open_ended)
            specificity = float(min(best_overlap, 9))
            score = 1000.0 + (weight * 10.0) + urg_bonus + prio_bonus + specificity
            reasons.append(f"agenda:{matched_item['id']}(weight={weight})")
            if matched_item.get("timeframe"):
                reasons.append(f"timeframe:{matched_item['timeframe']}")
            elif best_due:
                reasons.append(f"agenda_deadline:{best_due.isoformat()}")
            reasons.extend(best_reasons)
            due_sort_ord = best_due.toordinal() if best_due else date.max.toordinal()
        else:
            weight = 0
            score = prio_bonus
            if not agenda_active:
                reasons.append(f"agenda_inactive:{agenda.get('status', 'disabled')}")
            else:
                reasons.append("agenda:unmatched")
            due_sort_ord = date.max.toordinal()

        reasons.append(f"priority:{ticket['priority_level']}")

        row = {
            "id": ticket["id"],
            "title": ticket["title"],
            "status": ticket["status"],
            "folder": ticket["folder"],
            "priority": ticket["priority"],
            "priority_level": ticket["priority_level"],
            "urgency": ticket["urgency"],
            "pipeline": ticket["pipeline"],
            "project_dir": ticket["project_dir"],
            "domain": ticket["domain"],
            "path": ticket["path"],
            "matched_agenda_id": matched_item["id"] if matched_item else None,
            "matched_agenda_text": matched_item["text"] if matched_item else None,
            "agenda_weight": weight,
            "agenda_timeframe": matched_item.get("timeframe") if matched_item else None,
            "agenda_deadline_date": best_due.isoformat() if best_due else None,
            "score": round(score, 2),
            "match_reasons": reasons,
        }
        sort_key = (
            0 if matched_item is not None else 1,
            -round(score, 4),
            -weight,
            due_sort_ord,
            -prio_rank,
            ticket["id"],
            ticket["path"],
        )
        ranked_rows.append((sort_key, row))

    ranked_rows.sort(key=lambda item: item[0])
    ordered_tickets: list[dict[str, Any]] = []
    for idx, (_key, row) in enumerate(ranked_rows, start=1):
        ordered_tickets.append({"rank": idx, **row})

    return {
        "tickets_dir": str(base),
        "included_statuses": list(statuses),
        "reference_date": ref_day.isoformat(),
        "agenda_active": agenda_active,
        "agenda_status": agenda.get("status", "absent"),
        "agenda_name": agenda.get("name"),
        "agenda_deadline": agenda.get("deadline"),
        "open_agenda_items": len(open_items),
        "count": len(ordered_tickets),
        "matched_count": sum(1 for row in ordered_tickets if row["matched_agenda_id"] is not None),
        "tickets": ordered_tickets,
    }


# ---------------------------------------------------------------------------
# Calendar busy-slot inspection & conflict-free day planning
# ---------------------------------------------------------------------------

def _parse_hhmm(value: str, field_name: str) -> time:
    clean = str(value or "").strip()
    match = _HHMM_RE.fullmatch(clean)
    if not match:
        raise ValueError(f"{field_name} must be HH:MM (24h), got {value!r}")
    return time(int(match.group(1)), int(match.group(2)))


def _parse_flexible_datetime(raw: str | None) -> datetime | None:
    if not raw or not isinstance(raw, str):
        return None
    clean = raw.strip()
    if not clean:
        return None
    normalized = clean.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(normalized)
        if dt.tzinfo is not None:
            dt = dt.astimezone().replace(tzinfo=None)
        return dt.replace(second=0, microsecond=0)
    except ValueError:
        pass
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", clean):
        return datetime.combine(date.fromisoformat(clean), time(0, 0))
    return None


def _resolve_ticket_calendar_id(raw: Any) -> str:
    value = str("tickets" if raw is None else raw).strip().lower()
    if not value or value == "all" or not _CALENDAR_ID_RE.fullmatch(value):
        raise ValueError(
            f"invalid ticket_calendar_id {raw!r}; expected lowercase slug (a-z0-9_-) and not 'all'"
        )
    return value


def _ensure_kalender_silo(conn: sqlite3.Connection, cal_id: str, now_iso: str) -> None:
    has_calendars = conn.execute(
        "SELECT type FROM sqlite_master WHERE name = 'calendars'"
    ).fetchone()
    if not has_calendars or has_calendars[0] != "table":
        return
    name, category, color, description, is_default = _KALENDER_SILO_DEFAULTS.get(
        cal_id,
        (cal_id, "tasks", "#7c3aed", f"Ticket calendar ({cal_id})", 0),
    )
    conn.execute(
        """INSERT OR IGNORE INTO calendars
           (id, name, category, color, description, is_default, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (cal_id, name, category, color, description, is_default, now_iso),
    )


def _read_kalender_busy_and_existing(
    store_path: Path | None,
    start_day: date,
    end_day: date,
    default_slot_minutes: int,
    workday_start_t: time,
    workday_end_t: time,
) -> tuple[list[dict[str, Any]], set[str]]:
    busy: list[dict[str, Any]] = []
    existing_tids: set[str] = set()
    if store_path is None or not store_path.is_file():
        return busy, existing_tids

    conn = _connect_sqlite_readonly(store_path)
    try:
        tbl = conn.execute(
            "SELECT type FROM sqlite_master WHERE name = 'events'"
        ).fetchone()
        if not tbl or tbl[0] != "table":
            return busy, existing_tids

        ev_cols = {str(r[1]) for r in conn.execute("PRAGMA table_info(events)").fetchall()}
        has_link_mode = "link_mode" in ev_cols
        has_ext_id = "external_id" in ev_cols
        has_status = "status" in ev_cols
        has_src_ev = "source_event_id" in ev_cols

        all_ids = {
            str(r[0])
            for r in conn.execute("SELECT id FROM events").fetchall()
            if r[0] is not None
        }

        meta_cols = [
            "ics_uid",
            "link_mode" if has_link_mode else "'' AS link_mode",
            "external_id" if has_ext_id else "'' AS external_id",
            "status" if has_status else "'' AS status",
        ]
        meta_where = "ics_uid LIKE 'ticket-master-%@ticket-master'"
        if has_ext_id:
            meta_where = f"({meta_where} OR external_id LIKE 'ticket-master:%')"

        for row in conn.execute(f"SELECT {', '.join(meta_cols)} FROM events WHERE {meta_where}").fetchall():
            st = str(row["status"] or "").strip().lower()
            if st in _CANCELLED_OCEAN_STATUSES:
                continue
            lmode = str(row["link_mode"] or "").strip().lower()
            uid = str(row["ics_uid"] or "")
            ext = str(row["external_id"] or "")
            if (
                lmode == "mirror"
                or uid.startswith("ticket-master-mirror-")
                or ext.startswith("ticket-master-mirror:")
            ):
                continue
            if uid.startswith("ticket-master-") and uid.endswith("@ticket-master"):
                existing_tids.add(uid[len("ticket-master-"):-len("@ticket-master")])
            if ext.startswith("ticket-master:"):
                existing_tids.add(ext[len("ticket-master:"):])

        window_cols = [
            "id",
            "title",
            "date",
            "time",
            "duration_min",
            "ics_uid",
            "link_mode" if has_link_mode else "'' AS link_mode",
            "external_id" if has_ext_id else "'' AS external_id",
            "status" if has_status else "'' AS status",
            "source_event_id" if has_src_ev else "'' AS source_event_id",
        ]
        rows = conn.execute(
            f"SELECT {', '.join(window_cols)} "
            "FROM events WHERE date BETWEEN ? AND ? ORDER BY date ASC, time ASC",
            (start_day.isoformat(), end_day.isoformat()),
        ).fetchall()
        for row in rows:
            st = str(row["status"] or "").strip().lower()
            if st in _CANCELLED_OCEAN_STATUSES:
                continue
            uid_val = str(row["ics_uid"] or "")
            ext_val = str(row["external_id"] or "")
            lmode = str(row["link_mode"] or "").strip().lower()
            src_ev_id = str(row["source_event_id"] or "").strip()
            is_mirror = (
                lmode == "mirror"
                or uid_val.startswith("ticket-master-mirror-")
                or ext_val.startswith("ticket-master-mirror:")
            )
            # If this row is a mirror of an existing primary event in the same store,
            # skip it so neither busy_slots_detected nor daily ticket caps double-count it.
            if is_mirror and (src_ev_id in all_ids or uid_val.startswith("ticket-master-mirror-")):
                continue
            try:
                ev_day = date.fromisoformat(str(row["date"]).strip())
            except ValueError:
                continue
            raw_time = str(row["time"] or "").strip()
            dur_raw = row["duration_min"]
            dur_min = int(dur_raw) if isinstance(dur_raw, int) and dur_raw > 0 else default_slot_minutes
            if raw_time:
                try:
                    t_val = _parse_hhmm(raw_time[:5], "event.time")
                except ValueError:
                    continue
                ev_start = datetime.combine(ev_day, t_val)
                ev_end = ev_start + timedelta(minutes=dur_min)
            else:
                ev_start = datetime.combine(ev_day, workday_start_t)
                ev_end = datetime.combine(ev_day, workday_end_t)
            is_tm_primary = (
                not is_mirror
                and (
                    (uid_val.startswith("ticket-master-") and uid_val.endswith("@ticket-master"))
                    or ext_val.startswith("ticket-master:")
                )
            )
            busy.append({
                "backend": "kalender",
                "id": str(row["id"]),
                "title": str(row["title"] or ""),
                "start": ev_start,
                "end": ev_end,
                "is_ticket_master": is_tm_primary,
            })
    finally:
        conn.close()
    return busy, existing_tids


def _read_ocean_busy_and_existing(
    db_path: Path | None,
    start_day: date,
    end_day: date,
    default_slot_minutes: int,
) -> tuple[list[dict[str, Any]], set[str]]:
    busy: list[dict[str, Any]] = []
    existing_tids: set[str] = set()
    if db_path is None or not db_path.is_file():
        return busy, existing_tids

    conn = _connect_sqlite_readonly(db_path)
    try:
        tbl = conn.execute(
            "SELECT type FROM sqlite_master WHERE name = 'assistant_calendar'"
        ).fetchone()
        if not tbl or tbl[0] != "table":
            return busy, existing_tids

        cols = {str(r[1]) for r in conn.execute("PRAGMA table_info(assistant_calendar)").fetchall()}
        has_ext = "external_id" in cols
        has_status = "status" in cols
        has_end = "end_datetime" in cols

        if has_ext:
            if has_status:
                ext_rows = conn.execute(
                    "SELECT external_id, status FROM assistant_calendar WHERE external_id LIKE 'ticket-master:%'"
                ).fetchall()
                for row in ext_rows:
                    st = str(row["status"] or "").strip().lower()
                    if st not in _CANCELLED_OCEAN_STATUSES:
                        existing_tids.add(str(row["external_id"])[len("ticket-master:"):])
            else:
                ext_rows = conn.execute(
                    "SELECT external_id FROM assistant_calendar WHERE external_id LIKE 'ticket-master:%'"
                ).fetchall()
                for row in ext_rows:
                    existing_tids.add(str(row["external_id"])[len("ticket-master:"):])

        select_cols = ["id", "title", "start_datetime"]
        select_cols.append("end_datetime" if has_end else "NULL AS end_datetime")
        select_cols.append("status" if has_status else "'' AS status")
        select_cols.append("external_id" if has_ext else "'' AS external_id")

        window_start = f"{start_day.isoformat()} 00:00:00"
        window_end = f"{end_day.isoformat()}T23:59:59"
        rows = conn.execute(
            f"SELECT {', '.join(select_cols)} FROM assistant_calendar "
            "WHERE substr(start_datetime, 1, 10) BETWEEN ? AND ? "
            "ORDER BY start_datetime ASC",
            (start_day.isoformat(), end_day.isoformat()),
        ).fetchall()
        _ = (window_start, window_end)
        for row in rows:
            st = str(row["status"] or "").strip().lower()
            if st in _CANCELLED_OCEAN_STATUSES:
                continue
            ev_start = _parse_flexible_datetime(row["start_datetime"])
            if ev_start is None:
                continue
            ev_end = _parse_flexible_datetime(row["end_datetime"])
            if ev_end is None or ev_end <= ev_start:
                ev_end = ev_start + timedelta(minutes=default_slot_minutes)
            ext_val = str(row["external_id"] or "")
            busy.append({
                "backend": "ocean",
                "id": str(row["id"]),
                "title": str(row["title"] or ""),
                "start": ev_start,
                "end": ev_end,
                "is_ticket_master": ext_val.startswith("ticket-master:"),
            })
    finally:
        conn.close()
    return busy, existing_tids


def _find_free_slot_on_day(
    day: date,
    workday_start_t: time,
    workday_end_t: time,
    slot_minutes: int,
    day_busy: list[tuple[datetime, datetime]],
) -> tuple[datetime, datetime] | None:
    cursor = datetime.combine(day, workday_start_t)
    day_end = datetime.combine(day, workday_end_t)
    delta = timedelta(minutes=slot_minutes)

    while cursor + delta <= day_end:
        candidate_end = cursor + delta
        overlaps = [
            b_end
            for b_start, b_end in day_busy
            if cursor < b_end and b_start < candidate_end
        ]
        if not overlaps:
            return cursor, candidate_end
        latest_end = max(overlaps)
        if latest_end <= cursor:
            cursor += timedelta(minutes=1)
        else:
            cursor = latest_end
    return None


def plan_tickets_to_calendar(
    tickets_dir: str | Path,
    config: dict[str, Any] | None = None,
    *,
    days: int | None = None,
    backend: str | None = None,
    start_date: date | None = None,
    include_statuses: Iterable[str] | str | None = None,
    ticket_calendar_id: str | None = None,
    mirror_to_user: bool | None = None,
) -> dict[str, Any]:
    """Dry-run schedule of agenda-sorted tickets into conflict-free slots.

    Never writes to any file or SQLite database.
    """
    cal_cfg = _calendar_cfg(config)
    effective_ticket_cal = _resolve_ticket_calendar_id(
        ticket_calendar_id if ticket_calendar_id is not None else cal_cfg.get("ticket_calendar_id")
    )
    effective_mirror = (
        bool(mirror_to_user)
        if mirror_to_user is not None
        else bool(cal_cfg.get("mirror_to_user", False))
    )
    requested_backend = str(backend or cal_cfg.get("backend") or "auto").strip().lower()
    if requested_backend not in VALID_CALENDAR_BACKENDS:
        raise ValueError(
            f"unsupported calendar backend {requested_backend!r}; choose from {', '.join(VALID_CALENDAR_BACKENDS)}"
        )

    horizon_days = int(days if days is not None else cal_cfg.get("planning_horizon_days", 14))
    if horizon_days < 1 or horizon_days > 366:
        raise ValueError("planning horizon days must be between 1 and 366")

    slot_minutes = int(cal_cfg.get("default_slot_minutes", 60))
    if slot_minutes < 5 or slot_minutes > 720:
        raise ValueError("default_slot_minutes must be between 5 and 720")

    max_per_day = int(cal_cfg.get("max_tickets_per_day", 3))
    if max_per_day < 1 or max_per_day > 50:
        raise ValueError("max_tickets_per_day must be between 1 and 50")

    workday_start_t = _parse_hhmm(str(cal_cfg.get("workday_start", "09:00")), "workday_start")
    workday_end_t = _parse_hhmm(str(cal_cfg.get("workday_end", "17:00")), "workday_end")
    if datetime.combine(date(2026, 1, 1), workday_start_t) + timedelta(minutes=slot_minutes) > datetime.combine(
        date(2026, 1, 1), workday_end_t
    ):
        raise ValueError("workday window is shorter than default_slot_minutes")

    include_weekends = bool(cal_cfg.get("include_weekends", False))
    first_day = start_date or date.today()
    last_day = first_day + timedelta(days=horizon_days - 1)

    kal_skill = detect_calendar_skill(config)
    kal_store, kal_source = resolve_kalender_store(config)
    kal_info = _inspect_kalender_store(kal_store, kal_source, kal_skill)
    ocean_info = detect_ocean_calendar(config)
    selected_backend, backend_status, backend_reason = _select_calendar_backend(
        requested_backend, cal_cfg, kal_info, ocean_info
    )

    target_path: str | None = None
    busy_events: list[dict[str, Any]] = []
    existing_tids: set[str] = set()

    if selected_backend == "kalender":
        target_path = kal_info.get("store_path")
        k_busy, k_existing = _read_kalender_busy_and_existing(
            Path(target_path) if target_path else None,
            first_day,
            last_day,
            slot_minutes,
            workday_start_t,
            workday_end_t,
        )
        busy_events.extend(k_busy)
        existing_tids.update(k_existing)
        # If ocean_db_path was ALSO explicitly provided in config alongside auto/kalender,
        # include its busy slots so combined setups never double-book.
        if requested_backend == "auto" and config_paths.resolve_config_path(cal_cfg.get("ocean_db_path")) is not None:
            o_busy, _ = _read_ocean_busy_and_existing(
                Path(ocean_info["db_path"]) if ocean_info.get("db_path") else None,
                first_day,
                last_day,
                slot_minutes,
            )
            busy_events.extend(o_busy)
    elif selected_backend == "ocean":
        target_path = ocean_info.get("db_path")
        o_busy, o_existing = _read_ocean_busy_and_existing(
            Path(target_path) if target_path else None,
            first_day,
            last_day,
            slot_minutes,
        )
        busy_events.extend(o_busy)
        existing_tids.update(o_existing)
        if requested_backend == "auto" and config_paths.resolve_config_path(cal_cfg.get("kalender_store")) is not None:
            k_busy, _ = _read_kalender_busy_and_existing(
                Path(kal_info["store_path"]) if kal_info.get("store_path") else None,
                first_day,
                last_day,
                slot_minutes,
                workday_start_t,
                workday_end_t,
            )
            busy_events.extend(k_busy)

    sorted_result = sort_tickets_by_agenda(
        tickets_dir,
        config,
        include_statuses=include_statuses,
        reference_date=first_day,
    )

    candidate_days = [
        first_day + timedelta(days=offset)
        for offset in range(horizon_days)
        if include_weekends or (first_day + timedelta(days=offset)).weekday() < 5
    ]

    busy_by_day: dict[date, list[tuple[datetime, datetime]]] = {d: [] for d in candidate_days}
    tickets_count_by_day: dict[date, int] = {d: 0 for d in candidate_days}
    for ev in busy_events:
        ev_day = ev["start"].date()
        if ev_day in busy_by_day:
            busy_by_day[ev_day].append((ev["start"], ev["end"]))
            if ev.get("is_ticket_master"):
                tickets_count_by_day[ev_day] += 1

    planned: list[dict[str, Any]] = []
    skipped_existing: list[dict[str, Any]] = []
    unscheduled: list[dict[str, Any]] = []

    for ticket in sorted_result["tickets"]:
        tid = ticket["id"]
        if tid in existing_tids:
            skipped_existing.append({
                "ticket_id": tid,
                "title": ticket["title"],
                "reason": "already_planned",
            })
            continue

        due_date = (
            date.fromisoformat(ticket["agenda_deadline_date"])
            if ticket.get("agenda_deadline_date")
            else None
        )
        if due_date is not None:
            preferred_days = [d for d in candidate_days if d <= due_date]
            fallback_days = [d for d in candidate_days if d > due_date]
            ordered_days = preferred_days + fallback_days
        else:
            ordered_days = candidate_days

        assigned_slot: tuple[date, datetime, datetime] | None = None
        for day in ordered_days:
            if tickets_count_by_day[day] >= max_per_day:
                continue
            slot = _find_free_slot_on_day(
                day,
                workday_start_t,
                workday_end_t,
                slot_minutes,
                busy_by_day[day],
            )
            if slot is not None:
                assigned_slot = (day, slot[0], slot[1])
                busy_by_day[day].append(slot)
                tickets_count_by_day[day] += 1
                break

        if assigned_slot is None:
            unscheduled.append({
                "ticket_id": tid,
                "title": ticket["title"],
                "rank": ticket["rank"],
                "matched_agenda_id": ticket["matched_agenda_id"],
                "reason": "horizon_capacity_exhausted",
            })
            continue

        slot_day, slot_start, slot_end = assigned_slot
        within_deadline = True if due_date is None else (slot_day <= due_date)
        planned.append({
            "ticket_id": tid,
            "title": ticket["title"],
            "path": ticket["path"],
            "rank": ticket["rank"],
            "priority": ticket["priority"],
            "priority_level": ticket["priority_level"],
            "score": ticket["score"],
            "matched_agenda_id": ticket["matched_agenda_id"],
            "agenda_weight": ticket["agenda_weight"],
            "agenda_timeframe": ticket["agenda_timeframe"],
            "agenda_deadline_date": ticket["agenda_deadline_date"],
            "within_deadline": within_deadline,
            "date": slot_day.isoformat(),
            "start_time": slot_start.strftime("%H:%M"),
            "end_time": slot_end.strftime("%H:%M"),
            "start_datetime": slot_start.strftime("%Y-%m-%d %H:%M:%S"),
            "end_datetime": slot_end.strftime("%Y-%m-%d %H:%M:%S"),
            "duration_min": slot_minutes,
            "external_id": f"ticket-master:{tid}",
            "ics_uid": f"ticket-master-{tid}@ticket-master",
        })

    return {
        "dry_run": True,
        "applied": False,
        "requested_backend": requested_backend,
        "backend": selected_backend,
        "backend_status": backend_status,
        "backend_reason": backend_reason,
        "target_path": target_path,
        "ticket_calendar_id": effective_ticket_cal,
        "mirror_to_user": effective_mirror,
        "ocean_has_event_origin": bool(ocean_info.get("has_event_origin")) if selected_backend == "ocean" else None,
        "start_date": first_day.isoformat(),
        "end_date": last_day.isoformat(),
        "horizon_days": horizon_days,
        "workday_start": cal_cfg.get("workday_start", "09:00"),
        "workday_end": cal_cfg.get("workday_end", "17:00"),
        "default_slot_minutes": slot_minutes,
        "max_tickets_per_day": max_per_day,
        "include_weekends": include_weekends,
        "agenda_active": sorted_result["agenda_active"],
        "agenda_status": sorted_result["agenda_status"],
        "busy_slots_detected": len(busy_events),
        "planned_count": len(planned),
        "skipped_count": len(skipped_existing),
        "unscheduled_count": len(unscheduled),
        "planned": planned,
        "skipped_existing": skipped_existing,
        "unscheduled": unscheduled,
    }


def _slot_description(slot: dict[str, Any]) -> str:
    parts = [f"ticket-master slot for {slot['ticket_id']} ({slot['path']})"]
    if slot.get("matched_agenda_id"):
        ag_part = f"Agenda {slot['matched_agenda_id']} (w={slot['agenda_weight']}"
        if slot.get("agenda_timeframe"):
            ag_part += f", {slot['agenda_timeframe']}"
        ag_part += ")"
        parts.append(ag_part)
    parts.append(f"priority={slot['priority_level']}, score={slot['score']}")
    return " | ".join(parts)


def _mint_kalender_event_id(conn: sqlite3.Connection) -> str:
    record_id = uuid.uuid4().hex[:8]
    while conn.execute("SELECT 1 FROM events WHERE id = ?", (record_id,)).fetchone():
        record_id = uuid.uuid4().hex[:8]
    return record_id


def apply_calendar_plan(
    plan_or_tickets_dir: dict[str, Any] | str | Path,
    config: dict[str, Any] | None = None,
    *,
    days: int | None = None,
    backend: str | None = None,
    start_date: date | None = None,
    include_statuses: Iterable[str] | str | None = None,
    ticket_calendar_id: str | None = None,
    mirror_to_user: bool | None = None,
) -> dict[str, Any]:
    """Idempotently persist planned ticket slots into ``kalender`` or ``ocean``."""
    cal_cfg = _calendar_cfg(config)
    if isinstance(plan_or_tickets_dir, dict):
        plan = copy.deepcopy(plan_or_tickets_dir)
        effective_ticket_cal = _resolve_ticket_calendar_id(
            ticket_calendar_id
            if ticket_calendar_id is not None
            else (plan.get("ticket_calendar_id") or cal_cfg.get("ticket_calendar_id"))
        )
        effective_mirror = (
            bool(mirror_to_user)
            if mirror_to_user is not None
            else bool(
                plan["mirror_to_user"]
                if "mirror_to_user" in plan
                else cal_cfg.get("mirror_to_user", False)
            )
        )
    else:
        plan = plan_tickets_to_calendar(
            plan_or_tickets_dir,
            config=config,
            days=days,
            backend=backend,
            start_date=start_date,
            include_statuses=include_statuses,
            ticket_calendar_id=ticket_calendar_id,
            mirror_to_user=mirror_to_user,
        )
        effective_ticket_cal = str(plan["ticket_calendar_id"])
        effective_mirror = bool(plan["mirror_to_user"])

    selected_backend = plan.get("backend")
    if selected_backend not in {"kalender", "ocean"}:
        reason = plan.get("backend_reason") or f"calendar backend not ready ({plan.get('backend_status')})"
        raise RuntimeError(f"cannot apply calendar plan: {reason}")

    target_raw = plan.get("target_path")
    if not target_raw:
        raise RuntimeError(f"cannot apply calendar plan: no target_path resolved for {selected_backend!r}")
    target_path = Path(target_raw)

    now_iso = datetime.now(timezone.utc).isoformat()
    written: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = list(plan.get("skipped_existing", []))
    mirrored: list[dict[str, Any]] = []

    if selected_backend == "kalender":
        if target_path.exists() and target_path.is_dir():
            raise RuntimeError(f"kalender store path is a directory: {target_path}")
        target_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(target_path))
        conn.row_factory = sqlite3.Row
        try:
            existing_tbl = conn.execute(
                "SELECT type FROM sqlite_master WHERE name = 'events'"
            ).fetchone()
            if not existing_tbl or existing_tbl[0] != "table":
                conn.executescript(_KALENDER_V02_SCHEMA)

            ev_cols = {str(r[1]) for r in conn.execute("PRAGMA table_info(events)").fetchall()}
            is_v02 = _KALENDER_V02_COLUMNS <= ev_cols

            if is_v02:
                _ensure_kalender_silo(conn, effective_ticket_cal, now_iso)
                if effective_mirror and effective_ticket_cal != "user":
                    _ensure_kalender_silo(conn, "user", now_iso)

                primary_candidates_by_tid: dict[str, dict[str, Any]] = {}

                for slot in plan.get("planned", []):
                    tid = str(slot["ticket_id"])
                    ics_uid = slot.get("ics_uid") or f"ticket-master-{tid}@ticket-master"
                    ext_id = slot.get("external_id") or f"ticket-master:{tid}"
                    existing = conn.execute(
                        """SELECT id, title, date, time, end_time, duration_min,
                                  location, description, recurrence, calendar_id
                           FROM events
                           WHERE (ics_uid = ? OR external_id = ?)
                             AND COALESCE(link_mode, '') != 'mirror'""",
                        (ics_uid, ext_id),
                    ).fetchone()
                    if existing:
                        skipped.append({
                            "ticket_id": tid,
                            "title": slot["title"],
                            "reason": "already_planned",
                            "record_id": str(existing["id"]),
                        })
                        primary_candidates_by_tid[tid] = dict(existing)
                        continue

                    record_id = _mint_kalender_event_id(conn)
                    event_title = f"[{tid}] {slot['title']}"
                    desc_text = _slot_description(slot)
                    conn.execute(
                        """INSERT INTO events
                           (id, title, date, time, duration_min, location, description,
                            recurrence, ics_uid, created_at, updated_at,
                            calendar_id, category, source_calendar_id, source_event_id,
                            link_mode, external_id, end_time, status)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            record_id,
                            event_title,
                            slot["date"],
                            slot["start_time"],
                            int(slot["duration_min"]),
                            None,
                            desc_text,
                            None,
                            ics_uid,
                            now_iso,
                            now_iso,
                            effective_ticket_cal,
                            "tasks",
                            None,
                            None,
                            None,
                            ext_id,
                            slot["end_time"],
                            "planned",
                        ),
                    )
                    written.append({
                        **slot,
                        "record_id": record_id,
                        "ics_uid": ics_uid,
                        "external_id": ext_id,
                        "calendar_id": effective_ticket_cal,
                        "category": "tasks",
                        "status": "planned",
                    })
                    primary_candidates_by_tid[tid] = {
                        "id": record_id,
                        "title": event_title,
                        "date": slot["date"],
                        "time": slot["start_time"],
                        "end_time": slot["end_time"],
                        "duration_min": int(slot["duration_min"]),
                        "location": None,
                        "description": desc_text,
                        "recurrence": None,
                        "calendar_id": effective_ticket_cal,
                    }

                if effective_mirror and effective_ticket_cal != "user":
                    for sk in skipped:
                        sk_tid = str(sk.get("ticket_id") or "").strip()
                        if not sk_tid or sk_tid in primary_candidates_by_tid:
                            continue
                        sk_uid = f"ticket-master-{sk_tid}@ticket-master"
                        sk_ext = f"ticket-master:{sk_tid}"
                        row = conn.execute(
                            """SELECT id, title, date, time, end_time, duration_min,
                                      location, description, recurrence, calendar_id
                               FROM events
                               WHERE (ics_uid = ? OR external_id = ?)
                                 AND COALESCE(link_mode, '') != 'mirror'""",
                            (sk_uid, sk_ext),
                        ).fetchone()
                        if row is not None and str(row["calendar_id"] or "") != "user":
                            primary_candidates_by_tid[sk_tid] = dict(row)

                    for tid, prim in primary_candidates_by_tid.items():
                        src_cal = str(prim.get("calendar_id") or effective_ticket_cal)
                        if src_cal == "user":
                            continue
                        orig_id = str(prim["id"])
                        mirror_uid = f"ticket-master-mirror-{tid}@ticket-master"
                        mirror_ext = f"ticket-master-mirror:{tid}"
                        existing_mirror = conn.execute(
                            """SELECT id FROM events
                               WHERE ics_uid = ?
                                  OR external_id = ?
                                  OR (
                                      calendar_id = 'user'
                                      AND link_mode = 'mirror'
                                      AND source_calendar_id = ?
                                      AND source_event_id = ?
                                  )""",
                            (mirror_uid, mirror_ext, src_cal, orig_id),
                        ).fetchone()
                        if existing_mirror is not None:
                            continue
                        mirror_id = _mint_kalender_event_id(conn)
                        conn.execute(
                            """INSERT INTO events
                               (id, title, date, time, duration_min, location, description,
                                recurrence, ics_uid, created_at, updated_at,
                                calendar_id, category, source_calendar_id, source_event_id,
                                link_mode, external_id, end_time, status)
                               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'user', 'tasks', ?, ?, 'mirror', ?, ?, 'planned')""",
                            (
                                mirror_id,
                                prim["title"],
                                prim["date"],
                                prim["time"],
                                prim["duration_min"],
                                prim["location"],
                                prim["description"],
                                prim["recurrence"],
                                mirror_uid,
                                now_iso,
                                now_iso,
                                src_cal,
                                orig_id,
                                mirror_ext,
                                prim["end_time"],
                            ),
                        )
                        mirrored.append({
                            "ticket_id": tid,
                            "mirror_id": mirror_id,
                            "calendar_id": "user",
                            "source_calendar_id": src_cal,
                            "source_event_id": orig_id,
                            "link_mode": "mirror",
                            "ics_uid": mirror_uid,
                            "external_id": mirror_ext,
                            "date": prim["date"],
                            "start_time": prim["time"],
                            "end_time": prim["end_time"],
                        })
            else:
                # 100% backward-compatible v0.1 path (existing events table without calendar_id)
                for slot in plan.get("planned", []):
                    ics_uid = slot.get("ics_uid") or f"ticket-master-{slot['ticket_id']}@ticket-master"
                    existing = conn.execute(
                        "SELECT id FROM events WHERE ics_uid = ?",
                        (ics_uid,),
                    ).fetchone()
                    if existing:
                        skipped.append({
                            "ticket_id": slot["ticket_id"],
                            "title": slot["title"],
                            "reason": "already_planned",
                            "record_id": str(existing["id"]),
                        })
                        continue
                    record_id = _mint_kalender_event_id(conn)
                    event_title = f"[{slot['ticket_id']}] {slot['title']}"
                    conn.execute(
                        """INSERT INTO events
                           (id, title, date, time, duration_min, location, description,
                            recurrence, ics_uid, created_at, updated_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            record_id,
                            event_title,
                            slot["date"],
                            slot["start_time"],
                            int(slot["duration_min"]),
                            None,
                            _slot_description(slot),
                            None,
                            ics_uid,
                            now_iso,
                            now_iso,
                        ),
                    )
                    written.append({
                        **slot,
                        "record_id": record_id,
                        "ics_uid": ics_uid,
                    })
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        return {
            **plan,
            "dry_run": False,
            "applied": True,
            "kalender_schema": "v0.2" if is_v02 else "v0.1",
            "ticket_calendar_id": effective_ticket_cal,
            "mirror_to_user": effective_mirror,
            "written_count": len(written),
            "skipped_count": len(skipped),
            "mirrored_count": len(mirrored),
            "written": written,
            "skipped_existing": skipped,
            "mirrored": mirrored,
        }

    # Ocean backend: strict fail-closed verification of existing DB + assistant_calendar
    if not target_path.is_file():
        raise RuntimeError(
            f"Ocean calendar database not found at {target_path}; refusing to create database"
        )

    conn = sqlite3.connect(str(target_path))
    conn.row_factory = sqlite3.Row
    try:
        tbl = conn.execute(
            "SELECT type FROM sqlite_master WHERE name = 'assistant_calendar'"
        ).fetchone()
        if not tbl or tbl[0] != "table":
            raise RuntimeError(
                f"Ocean calendar table 'assistant_calendar' is missing in {target_path}; fail-closed"
            )
        cols = {str(r[1]) for r in conn.execute("PRAGMA table_info(assistant_calendar)").fetchall()}
        missing_cols = sorted(_REQUIRED_OCEAN_WRITE_COLUMNS - cols)
        if missing_cols:
            raise RuntimeError(
                f"Ocean 'assistant_calendar' missing required columns: {', '.join(missing_cols)}"
            )
        has_event_origin = "event_origin" in cols

        for slot in plan.get("planned", []):
            ext_id = slot.get("external_id") or f"ticket-master:{slot['ticket_id']}"
            existing = conn.execute(
                "SELECT id, status FROM assistant_calendar WHERE external_id = ?",
                (ext_id,),
            ).fetchall()
            active_existing = [
                row for row in existing
                if str(row["status"] or "").strip().lower() not in _CANCELLED_OCEAN_STATUSES
            ]
            if active_existing:
                skipped.append({
                    "ticket_id": slot["ticket_id"],
                    "title": slot["title"],
                    "reason": "already_planned",
                    "record_id": int(active_existing[0]["id"]),
                })
                continue

            event_title = f"[{slot['ticket_id']}] {slot['title']}"
            description = _slot_description(slot)
            if has_event_origin:
                cur = conn.execute(
                    """INSERT INTO assistant_calendar
                       (title, description, start_datetime, end_datetime,
                        event_type, status, external_id, event_origin,
                        created_at, updated_at)
                       VALUES (?, ?, ?, ?, 'ticket_plan', 'planned', ?, 'user', ?, ?)""",
                    (
                        event_title,
                        description,
                        slot["start_datetime"],
                        slot["end_datetime"],
                        ext_id,
                        now_iso,
                        now_iso,
                    ),
                )
            else:
                cur = conn.execute(
                    """INSERT INTO assistant_calendar
                       (title, description, start_datetime, end_datetime,
                        event_type, status, external_id,
                        created_at, updated_at)
                       VALUES (?, ?, ?, ?, 'ticket_plan', 'planned', ?, ?, ?)""",
                    (
                        event_title,
                        description,
                        slot["start_datetime"],
                        slot["end_datetime"],
                        ext_id,
                        now_iso,
                        now_iso,
                    ),
                )
            written.append({
                **slot,
                "record_id": cur.lastrowid,
                "external_id": ext_id,
                "event_origin": "user" if has_event_origin else None,
            })
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    return {
        **plan,
        "dry_run": False,
        "applied": True,
        "ticket_calendar_id": effective_ticket_cal,
        "mirror_to_user": effective_mirror,
        "ocean_has_event_origin": has_event_origin,
        "written_count": len(written),
        "skipped_count": len(skipped),
        "mirrored_count": 0,
        "written": written,
        "skipped_existing": skipped,
        "mirrored": [],
    }


# ---------------------------------------------------------------------------
# Human-readable formatters & standalone CLI
# ---------------------------------------------------------------------------

def format_planning_status_text(status: dict[str, Any]) -> str:
    ag = status["agenda"]
    cal = status["calendar"]
    lines = [
        f"AGENDA BRIDGE:   status={ag['status']} enabled={ag.get('enabled', False)}",
    ]
    skill = ag.get("skill") or {}
    if skill:
        lines.append(
            f"  agenda-setting: found={skill.get('found')} path={skill.get('skill_path') or '-'}"
        )
    ag_state = ag.get("agenda_state") or {}
    if ag_state:
        lines.append(
            f"  agenda state:   active={ag_state.get('agenda_active')} "
            f"status={ag_state.get('state_status')} "
            f"open_items={ag_state.get('open_items', 0)} "
            f"deadline={ag_state.get('deadline') or '-'}"
        )
    lines.append(
        f"CALENDAR BRIDGE: status={cal['status']} enabled={cal.get('enabled', False)} "
        f"backend={cal.get('selected_backend') or '-'} (requested={cal.get('requested_backend', 'auto')})"
    )
    backends = cal.get("backends") or {}
    if backends:
        kal = backends.get("kalender") or {}
        oc = backends.get("ocean") or {}
        lines.append(
            f"  kalender:       status={kal.get('status')} store={kal.get('store_path') or '-'}"
        )
        lines.append(
            f"  ocean:          status={oc.get('status')} db={oc.get('db_path') or '-'} "
            f"event_origin={oc.get('has_event_origin')}"
        )
    return "\n".join(lines)


def format_sorted_tickets_text(result: dict[str, Any]) -> str:
    lines = [
        f"AGENDA_ACTIVE={result['agenda_active']}\tAGENDA_STATUS={result['agenda_status']}\t"
        f"COUNT={result['count']}\tMATCHED={result['matched_count']}",
        "RANK\tSCORE\tAGENDA\tWEIGHT\tTIMEFRAME\tPRIORITY\tID\tTITLE\tREASONS",
    ]
    for row in result["tickets"]:
        lines.append(
            "\t".join([
                str(row["rank"]),
                f"{row['score']:.1f}",
                row["matched_agenda_id"] or "-",
                str(row["agenda_weight"]),
                row["agenda_timeframe"] or "-",
                row["priority_level"],
                row["id"],
                row["title"],
                ",".join(row["match_reasons"]),
            ])
        )
    return "\n".join(lines)


def format_calendar_plan_text(result: dict[str, Any]) -> str:
    mode_label = "APPLIED" if result.get("applied") else "DRY-RUN"
    lines = [
        f"MODE={mode_label}\tBACKEND={result.get('backend') or '-'}\t"
        f"WINDOW={result['start_date']}..{result['end_date']}\t"
        f"PLANNED={result['planned_count']}\tSKIPPED={result['skipped_count']}\t"
        f"UNSCHEDULED={result['unscheduled_count']}",
        "DATE\tSTART\tEND\tAGENDA\tID\tTITLE",
    ]
    rows = result.get("written") if result.get("applied") else result.get("planned", [])
    for slot in rows or []:
        lines.append(
            "\t".join([
                slot["date"],
                slot["start_time"],
                slot["end_time"],
                slot.get("matched_agenda_id") or "-",
                slot["ticket_id"],
                slot["title"],
            ])
        )
    return "\n".join(lines)


def _cli(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(
        prog="planning_bridges",
        description="Bridge ticket-master with agenda-setting and kalender / Ocean calendar.",
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--status", action="store_true", help="inspect bridge and backend status")
    mode.add_argument("--sort", action="store_true", help="rank open tickets by agenda")
    mode.add_argument("--plan", action="store_true", help="plan (or --apply) calendar slots for tickets")
    parser.add_argument("--apply", action="store_true", help="persist planned slots (with --plan; default dry-run)")
    parser.add_argument("--config", default=None, help="path to ticket-master.config.json")
    parser.add_argument("--tickets-dir", default=None, help="override configured tickets_dir")
    parser.add_argument("--days", type=int, default=None, help="planning horizon in days (with --plan)")
    parser.add_argument(
        "--backend",
        choices=VALID_CALENDAR_BACKENDS,
        default=None,
        help="calendar backend selection (auto, kalender, ocean)",
    )
    parser.add_argument(
        "--include-statuses",
        default=None,
        help="comma-separated lifecycle folders (default: ACTIONABLE,QUEUED)",
    )
    parser.add_argument(
        "--ticket-calendar",
        default=None,
        help="target kalender v0.2 silo ID for planned tickets (default: config or 'tickets', with --plan)",
    )
    parser.add_argument(
        "--mirror-to-user",
        action="store_true",
        help="idempotently mirror planned ticket slots into the 'user' kalender silo (with --plan)",
    )
    parser.add_argument("--json", action="store_true", help="emit JSON output")
    args = parser.parse_args(argv)

    if args.apply and not args.plan:
        parser.error("--apply is only valid with --plan")
    if args.days is not None and not args.plan:
        parser.error("--days is only valid with --plan")
    if args.ticket_calendar is not None and not args.plan:
        parser.error("--ticket-calendar is only valid with --plan")
    if args.mirror_to_user and not args.plan:
        parser.error("--mirror-to-user is only valid with --plan")

    tm_cfg = config_paths.tm_config(args.config)
    try:
        if args.status:
            status_data = planning_status(
                tm_cfg,
                include_detection=True,
                backend_override=args.backend,
            )
            if args.json:
                print(json.dumps(status_data, ensure_ascii=False, indent=2))
            else:
                print(format_planning_status_text(status_data))
            return 0

        tickets_dir = config_paths.require_tickets_dir(args.tickets_dir, config=tm_cfg)
        tickets_dir = config_paths.resolve_queue_alias(tickets_dir)

        if args.sort:
            sorted_data = sort_tickets_by_agenda(
                tickets_dir,
                tm_cfg,
                include_statuses=args.include_statuses,
            )
            if args.json:
                print(json.dumps(sorted_data, ensure_ascii=False, indent=2))
            else:
                print(format_sorted_tickets_text(sorted_data))
            return 0

        if args.apply:
            plan_data = apply_calendar_plan(
                tickets_dir,
                tm_cfg,
                days=args.days,
                backend=args.backend,
                include_statuses=args.include_statuses,
                ticket_calendar_id=args.ticket_calendar,
                mirror_to_user=(True if args.mirror_to_user else None),
            )
        else:
            plan_data = plan_tickets_to_calendar(
                tickets_dir,
                tm_cfg,
                days=args.days,
                backend=args.backend,
                include_statuses=args.include_statuses,
                ticket_calendar_id=args.ticket_calendar,
                mirror_to_user=(True if args.mirror_to_user else None),
            )
        if args.json:
            print(json.dumps(plan_data, ensure_ascii=False, indent=2))
        else:
            print(format_calendar_plan_text(plan_data))
        return 0
    except (ValueError, RuntimeError, OSError, config_paths.QueueAliasError) as exc:
        if args.json:
            print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        else:
            print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(_cli(sys.argv[1:]))
