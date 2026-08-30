r"""
boot_menu.py — Role-spawn menu offered at the end of the TICKET-MASTER boot
sequence (Entscheid 5A, T-20260830-446089912).

Pure data functions only: nothing here starts a process, a window, or a
subagent. The prompt's boot step reads ``offer()``'s JSON and a human (or the
agent following the prompt) decides what to actually launch.

* parse_mode()        -- "<roles>:<instances>" notation + user-facing aliases.
* list_models()        -- model list from clutch (routing contract v2's only
  model authority), with a visible fallback to this config's own "providers".
* self_model()         -- the ticket-master's OWN running model, from a
  harness self-declaration only (T-20260830-966677444: a self-report can be
  absent and must never be guessed).
* build_spawn_orders() -- turns a parsed mode + roles + model choice into
  spawn-order data objects (roles, model, execution kind, launch hint).

CLI:
    python lib/boot_menu.py --offer [--config <ticket-master.config.json>]
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

try:  # package import (``from lib import boot_menu``)
    from . import auditor_bridge
except ImportError:  # direct script/module import from ``lib`` on sys.path
    import auditor_bridge


# The three TASKPLAN roles (~/CLAUDE.md "TASKPLAN -- die drei Aufgaben-Loops").
DEFAULT_ROLES: tuple[str, ...] = ("taskwriter", "tasksolver", "maintainer")

# Canonical notation is "<roles>:<instances>"; these are the user's own
# worked examples from the ticket ("3 in 1", "3:3", "only 1", "only 2").
# ponytail: only the four combinations actually requested are wired up in
# build_spawn_orders() below (unified N:1 and one-per-role N:N) -- a general
# N:M scheduler is speculative until a real request needs it.
_MODE_ALIASES: dict[str, tuple[int, int]] = {
    "3in1": (3, 1),
    "3 in 1": (3, 1),
    "3:3": (3, 3),
    "3x3": (3, 3),
    "only1": (1, 1),
    "only2": (2, 2),
}


def parse_mode(text: str) -> tuple[int, int] | None:
    """Parses a roles:instances spawn mode. Canonical is "<roles>:<instances>"
    (e.g. "3:1", "2:2"); a fixed set of user-facing aliases from the original
    request resolve to the same tuples. Anything else -> None, never guessed."""
    if not text:
        return None
    normalized = text.strip().lower()
    if ":" in normalized:
        left, _, right = normalized.partition(":")
        if left.isdigit() and right.isdigit():
            return (int(left), int(right))
    return _MODE_ALIASES.get(normalized)


def list_models(config: dict[str, Any]) -> dict[str, Any]:
    """Model list for the boot menu. Routing contract v2: "keine eigene
    Modellliste" -- clutch is the one model authority, asked via
    ``clutch models --json``. Falls back to this config's own "providers"
    when clutch is not on PATH or the call fails, and says so via "source"
    (never a silent fallback, T-20260830-446089912 Bauvorgabe b)."""
    cli_path = shutil.which("clutch")
    if cli_path:
        try:
            result = subprocess.run(
                [cli_path, "models", "--json"],
                capture_output=True, text=True, timeout=15,
            )
            if result.returncode == 0:
                parsed = json.loads(result.stdout)
                models = parsed.get("models") if isinstance(parsed, dict) else parsed
                if isinstance(models, list):
                    return {"source": "clutch", "models": models}
        except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
            pass

    providers = config.get("providers") if isinstance(config, dict) else None
    models: list[dict[str, Any]] = []
    if isinstance(providers, dict):
        for name, spec in providers.items():
            if not isinstance(spec, dict):
                continue
            models.append({
                "name": name,
                "provider": name,
                "gang_stufe": spec.get("default_model"),
                "efforts": [],
            })
    return {"source": "config-fallback", "models": models}


def self_model(config: dict[str, Any] | None = None) -> str:
    """The ticket-master's own running model, from a harness self-declaration
    only. Checked in order: $TM_MODEL, then config["self_model"]. Neither
    present -> "unknown" -- never guessed (T-20260830-966677444: a model
    self-report is a harness fact that can be absent)."""
    import os

    env_value = os.environ.get("TM_MODEL")
    if env_value:
        return env_value
    if isinstance(config, dict):
        value = config.get("self_model")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return "unknown"


def build_spawn_orders(
    mode: tuple[int, int], roles: list[str] | tuple[str, ...] | None,
    model: Any, window: bool,
) -> list[dict[str, Any]]:
    """Pure data objects for a boot-menu spawn choice -- never starts a
    process or a window. ``mode`` is (roles_count, instances_count) from
    parse_mode(). ``roles`` names which of DEFAULT_ROLES are wanted when
    roles_count < 3; defaults to the first N of DEFAULT_ROLES. ``model`` is
    either one spec used for every instance ("same") or a list of one spec
    per instance ("different").

    Only the two combinations the ticket actually asks for are implemented:
    instances_count == 1 (one unified instance covering every requested
    role, e.g. "3:1") and instances_count == roles_count (one instance per
    role, e.g. "3:3"/"2:2"/"1:1"). Anything else raises -- see the ponytail
    note on _MODE_ALIASES above.
    """
    roles_count, instances_count = mode
    chosen_roles = list(roles) if roles else list(DEFAULT_ROLES[:roles_count])
    if len(chosen_roles) != roles_count:
        raise ValueError(
            f"mode asks for {roles_count} role(s), got {len(chosen_roles)}: {chosen_roles}"
        )

    if instances_count == 1:
        groups = [chosen_roles]
    elif instances_count == roles_count:
        groups = [[role] for role in chosen_roles]
    else:
        raise ValueError(
            f"unsupported roles:instances combination {roles_count}:{instances_count} "
            "(only N:1 unified and N:N one-per-role are implemented)"
        )

    models = model if isinstance(model, list) else [model] * len(groups)
    if len(models) != len(groups):
        raise ValueError(f"expected {len(groups)} model spec(s), got {len(models)}")

    orders: list[dict[str, Any]] = []
    for group_roles, group_model in zip(groups, models):
        order: dict[str, Any] = {
            "roles": group_roles,
            "model": group_model,
            "execution": "window" if window else "companion",
        }
        if window:
            order["launcher_hint"] = [
                f"_control-center/START-{role.upper()}.bat" for role in group_roles
            ]
        else:
            order["windowless_hint"] = "pythonw / CREATE_NO_WINDOW (kein Fenster)"
        orders.append(order)
    return orders


def _default_tm_config_path() -> Path:
    return Path(__file__).resolve().parent.parent / "config" / "ticket-master.config.json"


def _load_tm_config(path: str | Path | None) -> dict[str, Any]:
    # Reuses auditor_bridge's own loader (P-009: no second config-loading
    # standard) rather than reimplementing the same five lines here.
    return auditor_bridge._load_tm_config(path)


def offer(tm_config: dict[str, Any]) -> dict[str, Any]:
    """The boot-menu's data-only offer: available roles, spawn modes, the
    model list (with its source), the ticket-master's own self_model(), and
    the auditor codeword. Default action on empty input is "none" (Enter ->
    POSITION 0, nothing started)."""
    bridge_cfg = dict(auditor_bridge.DEFAULT_AUDITOR_BRIDGE_CONFIG)
    bridge_cfg.update(tm_config.get("auditor_bridge") or {})
    return {
        "roles": list(DEFAULT_ROLES),
        "modes": {
            "3:1": {"aliases": ["3in1", "3 in 1"], "roles": 3, "instances": 1},
            "3:3": {"aliases": ["3x3"], "roles": 3, "instances": 3},
            "2:2": {"aliases": ["only2"], "roles": 2, "instances": 2},
            "1:1": {"aliases": ["only1"], "roles": 1, "instances": 1},
        },
        "models": list_models(tm_config),
        "self_model": self_model(tm_config),
        "auditor_codeword": bridge_cfg.get("codeword"),
        "default": {"action": "none", "reason": "Enter ohne Eingabe -> POSITION 0, nichts starten"},
    }


def _cli(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="boot_menu",
        description="ticket-master boot-menu offer (data only -- starts nothing).",
    )
    parser.add_argument("--offer", action="store_true", required=True,
                         help="print the boot-menu offer as JSON")
    parser.add_argument("--config", default=None, help="ticket-master.config.json path")
    args = parser.parse_args(argv)

    tm_config = _load_tm_config(args.config)
    print(json.dumps(offer(tm_config), indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    import sys

    raise SystemExit(_cli(sys.argv[1:]))
