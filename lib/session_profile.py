"""Private, per-OS-user session preferences for the prompt-driven master.

No profile data is stored in the public repository. A missing profile means
the historical boot behaviour, including the opt-in auditor bridge, applies.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

try:
    from . import config_paths
except ImportError:
    import config_paths

PROFILE_ENV = "TM_SESSION_PROFILE"
DEFAULT_PROFILE = Path.home() / ".config" / "ticket-master" / "session-profile.json"


def profile_path(config: dict[str, Any] | None = None) -> Path:
    raw = os.environ.get(PROFILE_ENV) or (config or {}).get("session_profile_path")
    return config_paths.resolve_config_path(raw) or DEFAULT_PROFILE


def load_profile(config: dict[str, Any] | None = None) -> dict[str, Any]:
    path = profile_path(config)
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return {"source": str(path), "configured": False, "runtime_compatibility": "pending", "settings": {}}
    except (OSError, UnicodeError, ValueError) as exc:
        raise ValueError(f"session profile unreadable: {path}: {exc}") from exc
    if not isinstance(data, dict) or data.get("schema") != "ticket-master.session-profile.v1":
        raise ValueError(f"invalid session profile schema: {path}")
    compatibility = data.get("runtime_compatibility", "pending")
    if compatibility not in {"pending", "active"}:
        raise ValueError(f"invalid runtime_compatibility: {path}")
    allowed = {"approval_routing", "decision_draw", "attention", "audit", "companion", "umbruch"}
    settings = data.get("settings")
    if not isinstance(settings, dict) or set(settings) - allowed:
        raise ValueError(f"invalid session profile settings: {path}")
    for key in allowed:
        value = settings.get(key, {})
        if not isinstance(value, dict) or not isinstance(value.get("enabled", False), bool):
            raise ValueError(f"invalid {key} settings: {path}")
    routing = settings.get("approval_routing", {})
    if not isinstance(routing.get("all_user_approvals_as_tickets", False), bool):
        raise ValueError("approval_routing.all_user_approvals_as_tickets must be boolean")
    for key in ("decision_root", "decision_index_module"):
        if key in routing and not isinstance(routing[key], str):
            raise ValueError(f"approval_routing.{key} must be a path string")
    approval_sources = routing.get("approval_sources", [])
    if not isinstance(approval_sources, list) or any(
        not isinstance(row, dict) or not isinstance(row.get("name"), str)
        or not isinstance(row.get("path"), str)
        or row.get("connector") not in {"canonical_index", "manual"}
        for row in approval_sources
    ):
        raise ValueError("approval_routing.approval_sources must name paths and connectors")
    attention = settings.get("attention", {})
    decisions = settings.get("decision_draw", {})
    decision_paths = decisions.get("sources", [])
    if not isinstance(decision_paths, list) or any(not isinstance(x, str) or not x for x in decision_paths):
        raise ValueError("decision_draw.sources must be a list of paths")
    hours = attention.get("lookback_hours", 24)
    if isinstance(hours, bool) or not isinstance(hours, int) or not 1 <= hours <= 168:
        raise ValueError("attention.lookback_hours must be an integer from 1 to 168")
    sources = attention.get("sources", [])
    if not isinstance(sources, list) or any(not isinstance(x, str) or not x for x in sources):
        raise ValueError("attention.sources must be a list of paths")
    filters = attention.get("filters", ["error", "blocked", "decision", "deadline"])
    if not isinstance(filters, list) or any(x not in {"error", "blocked", "decision", "deadline"} for x in filters):
        raise ValueError("invalid attention.filters")
    companion = settings.get("companion", {})
    if companion.get("max_luna", 2) != 2 or companion.get("max_high", 1) != 1:
        raise ValueError("unsupported companion slot ceiling")
    return {"source": str(path), "configured": True,
            "runtime_compatibility": compatibility, "settings": settings}


def effective_settings(config: dict[str, Any] | None = None) -> dict[str, Any]:
    loaded = load_profile(config)
    values = {
        "approval_routing": {"enabled": False, "all_user_approvals_as_tickets": False},
        "decision_draw": {"enabled": False},
        "attention": {"enabled": False, "lookback_hours": 24, "filters": ["error", "blocked", "decision", "deadline"], "sources": []},
        "audit": {"enabled": False},
        "companion": {"enabled": False, "max_luna": 2, "max_high": 1},
        "umbruch": {"enabled": False},
    }
    requested = loaded["settings"]
    for key, value in requested.items():
        values[key].update(value)
    if loaded["runtime_compatibility"] != "active":
        for value in values.values():
            value["enabled"] = False
    return {"source": loaded["source"], "configured": loaded["configured"],
            "runtime_compatibility": loaded["runtime_compatibility"],
            "requested": requested, "settings": values}
