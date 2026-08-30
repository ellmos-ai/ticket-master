r"""
auditor_bridge.py — Thin bridge between ticket-master and the system-auditor CLI.

Ticket T-20260830-948243522 (section "ZENTRALER BEFUND"): system-auditor already
owns the window/rotation/staleness math (`time-token`, `next-domain`, `stale`,
`reports`). This module never recomputes that -- it only ASKS the installed
auditor and combines its answers. Building a second timestamp store or rotation
calculator here would be exactly the parallel standard P-009 forbids.

Four pure-ish functions plus a CLI:

* detect_auditor()      -- is system-auditor installed at all (CLI or package)?
* due_check(reports_dir)-- ask system-auditor whether something is due right now.
* spar_gate(...)        -- three-valued sparmodus read: "off" | "on" | "unknown".
  "unknown" must NEVER be treated as "off" -- an audit is an expensive
  multi-agent run, and a silent read failure looks exactly like a quiet normal
  state (the same failure mode this ticket's night was named after).
* decide(config)        -- combines the above into one spawn/skip verdict.
* findings_to_tickets() -- turns system-auditor findings/*.md into draft
  ticket-master INBOX tickets, deduplicated by the finding's own ID.

CLI:
    python lib/auditor_bridge.py --check [--manual] [--config <ticket-master.config.json>]
    python lib/auditor_bridge.py --findings-to-tickets [--apply] [--findings-dir DIR] [--tickets-dir DIR]
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

try:  # package import (``from lib import auditor_bridge``)
    from . import ticket_writer
except ImportError:  # direct script/module import from ``lib`` on sys.path
    import ticket_writer


# --------------------------------------------------------------------------
# Defaults
# --------------------------------------------------------------------------

# Config schema added to config/ticket-master.config(.example).json by this
# ticket. "min_interval" is intentionally NOT consulted by decide() below:
# system-auditor's own time-grid/period already governs when a domain becomes
# due (see due_check()), and a second cadence store here would be exactly the
# parallel standard the ticket's "ZENTRALER BEFUND" forbids. The field stays
# in the schema as documentation of intended cadence / for a future, explicit
# consumer -- it is not silently ignored, it is deliberately unused.
DEFAULT_AUDITOR_BRIDGE_CONFIG: dict[str, Any] = {
    "enabled": False,
    "min_interval": "7d",
    "spar_gate": True,
    "codeword": "audit!",
}

# Sparmodus/notaus state written by the token-budget hooks (~/CLAUDE.md,
# skills `sparmodus`/`notaus`). Defaults match the live paths measured on
# ASUS-GEI (T-20260830-948243522); callers may override every path.
DEFAULT_SPARMODUS_STATE_PATH = Path.home() / ".claude" / "state" / "sparmodus_state.json"
DEFAULT_TOKEN_BUDGET_PATH = Path.home() / ".claude" / "state" / "token_budget.json"
DEFAULT_THRESHOLDS_CONFIG_PATH = Path.home() / ".claude" / "hooks" / "token_budget_config.json"

# Fallback thresholds (ticket wording: "Default spar 80 / notaus 90"), used
# when the thresholds config file is missing/unreadable -- fail-open on the
# NUMBER, never on the three-valued verdict (see spar_gate()).
DEFAULT_THRESHOLDS = {"sparmodus_used_pct": 80, "notaus_used_pct": 90}

_KNOWN_SPARMODUS_MODES = {"off", "manual-spar", "auto-spar", "notaus"}


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

def _subprocess_env() -> dict[str, str]:
    env = dict(os.environ)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    return env


def _read_json(path: str | Path | None) -> Any | None:
    """Read+parse JSON, or None on any missing/unreadable/invalid file.

    "Absent beats wrong" (mirrors system_auditor.config's own doctrine):
    callers decide what "None" means for them (usually "unknown").
    """
    if not path:
        return None
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None


def _load_thresholds(path: str | Path | None) -> dict[str, float]:
    merged = dict(DEFAULT_THRESHOLDS)
    data = _read_json(path)
    thresholds = data.get("thresholds") if isinstance(data, dict) else None
    if isinstance(thresholds, dict):
        for key in DEFAULT_THRESHOLDS:
            value = thresholds.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                merged[key] = value
    return merged


def _run_json(cli_path: str, args: list[str], *, timeout: float = 30.0) -> dict | None:
    """Run ``system-auditor --json <args>`` and return the parsed object, or
    None on any process/parse failure. Warnings (e.g. the unset-auditor
    notice) go to stderr in system-auditor and are discarded here -- only the
    machine-readable stdout payload is consulted, never a printed message."""
    try:
        result = subprocess.run(
            [cli_path, "--json", *args],
            capture_output=True, text=True, timeout=timeout, env=_subprocess_env(),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    try:
        parsed = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


# --------------------------------------------------------------------------
# detect_auditor
# --------------------------------------------------------------------------

def detect_auditor() -> dict[str, Any]:
    """Presence check via the regular install surfaces only -- never a guessed
    path. Prefers the CLI (what due_check() actually calls); falls back to the
    importable package so a venv without the console-script entry point is
    still reported as present."""
    path = shutil.which("system-auditor")
    if path:
        version = None
        try:
            result = subprocess.run(
                [path, "--version"], capture_output=True, text=True,
                timeout=10, env=_subprocess_env(),
            )
            if result.returncode == 0:
                version = result.stdout.strip().rsplit(" ", 1)[-1] or None
        except (OSError, subprocess.SubprocessError):
            pass
        return {"present": True, "path": path, "version": version}

    import importlib.util

    spec = importlib.util.find_spec("system_auditor")
    if spec is not None:
        version = None
        try:
            import importlib

            version = getattr(importlib.import_module("system_auditor"), "__version__", None)
        except ImportError:
            pass
        return {"present": True, "path": None, "version": version}

    return {"present": False, "path": None, "version": None}


# --------------------------------------------------------------------------
# due_check
# --------------------------------------------------------------------------

def due_check(reports_dir: str | Path) -> dict[str, Any]:
    """Ask system-auditor whether the domain it is currently rotating to is
    already covered for the current window. Never computes the window,
    rotation or staleness itself -- only checks whether the CLI's own
    ``reports`` listing already contains an entry for (window, domain).

    Returns {"due": bool | None, "domain": str | None, "window": str | None,
    "raw": {...}}. ``due`` is None ("not determinable") whenever the CLI is
    missing or any of the queried subcommands fails/returns unparsable JSON --
    the caller must not treat that like False.
    """
    cli_path = shutil.which("system-auditor")
    if cli_path is None:
        return {"due": None, "domain": None, "window": None, "raw": None}

    reports_dir = str(reports_dir)
    time_token = _run_json(cli_path, ["time-token"])
    next_domain = _run_json(cli_path, ["next-domain", "--reports", reports_dir])
    stale = _run_json(cli_path, ["stale", "--reports", reports_dir])
    reports = _run_json(cli_path, ["reports", "--reports", reports_dir])
    raw = {"time_token": time_token, "next_domain": next_domain, "stale": stale, "reports": reports}

    window = time_token.get("time_token") if time_token else None
    domain = next_domain.get("domain") if next_domain else None
    if window is None or domain is None or reports is None:
        return {"due": None, "domain": domain, "window": window, "raw": raw}

    entries = reports.get("reports")
    if not isinstance(entries, list):
        return {"due": None, "domain": domain, "window": window, "raw": raw}

    already_done = any(
        isinstance(entry, str) and entry.split(" · ")[:2] == [window, domain]
        for entry in entries
    )
    return {"due": not already_done, "domain": domain, "window": window, "raw": raw}


# --------------------------------------------------------------------------
# spar_gate
# --------------------------------------------------------------------------

def spar_gate(
    state_path: str | Path | None,
    budget_path: str | Path | None,
    thresholds: dict[str, float] | None = None,
) -> str:
    """Three-valued sparmodus read: "off" | "on" | "unknown".

    "unknown" is returned whenever the sparmodus_state.json mode cannot be
    established AND the token budget does not independently show an
    over-threshold usage -- it must never collapse to "off" (that was the
    fault line this ticket is named after: a silent read failure looking like
    a quiet normal state).

    ``mode`` from state_path is authoritative for "off" (an explicitly valid
    "off" is trusted even if budget_path is missing/unreadable). Any
    non-"off" known mode ("manual-spar"/"auto-spar"/"notaus") is "on"
    regardless of the budget file. Independently, if the live 5h usage from
    budget_path is already at/over the configured sparmodus threshold, that
    always wins as "on" too -- a defensive catch for a stale "off" written
    before the hook re-ran this prompt.
    """
    merged_thresholds = dict(DEFAULT_THRESHOLDS)
    if thresholds:
        merged_thresholds.update(thresholds)

    state = _read_json(state_path)
    mode = state.get("mode") if isinstance(state, dict) else None
    mode_known = mode in _KNOWN_SPARMODUS_MODES

    budget = _read_json(budget_path)
    used_pct = None
    if isinstance(budget, dict):
        five_hour = budget.get("five_hour")
        if isinstance(five_hour, dict):
            value = five_hour.get("used_percentage")
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                used_pct = value

    if mode_known and mode != "off":
        return "on"
    if used_pct is not None and used_pct >= merged_thresholds["sparmodus_used_pct"]:
        return "on"
    if mode_known:  # mode == "off", not overridden by the budget cross-check above
        return "off"
    return "unknown"


# --------------------------------------------------------------------------
# decide
# --------------------------------------------------------------------------

def decide(config: dict[str, Any], *, manual: bool = False) -> dict[str, Any]:
    """Combine detect_auditor()/spar_gate()/due_check() into one verdict.

    ``config`` keys (all optional except where a default would be unsafe):
      auditor_bridge:        {"enabled", "min_interval", "spar_gate", "codeword"}
      reports_dir:           passed straight to due_check()
      sparmodus_state_path, token_budget_path, thresholds: passed to spar_gate()

    ``manual=True`` is the POSITION-0 codeword path: it bypasses the
    ``enabled`` switch and the due-check (the user asked for it right now,
    regardless of rotation), but still respects presence and the sparmodus
    gate -- an audit stays a multi-agent run that must not fire during
    sparmodus/notaus just because it was requested by hand.

    Returns {"action": "spawn"|"skip"|"disabled"|"absent"|"unknown",
    "reason": str, "detection": {...}, ...}. "unknown" is used whenever a
    needed signal (sparmodus state, reports_dir) could not be read/resolved --
    never silently defaulted to "spawn" or "skip".
    """
    bridge_cfg = dict(DEFAULT_AUDITOR_BRIDGE_CONFIG)
    bridge_cfg.update(config.get("auditor_bridge") or {})

    detection = detect_auditor()
    if not detection["present"]:
        return {
            "action": "absent",
            "reason": "system-auditor not found (no CLI on PATH, package not importable)",
            "detection": detection,
        }

    # Computed unconditionally (before the enabled/manual branch) so the
    # verdict is always transparent about the current sparmodus read, even
    # when the actual action is "disabled" for an unrelated reason (config
    # off). Only USED as a gate below when bridge_cfg["spar_gate"] is true.
    gate = spar_gate(
        config.get("sparmodus_state_path"),
        config.get("token_budget_path"),
        config.get("thresholds"),
    )
    gate_enabled = bridge_cfg.get("spar_gate", True)

    if not manual and not bridge_cfg.get("enabled", False):
        return {
            "action": "disabled",
            "reason": "auditor_bridge.enabled is false (conservative default); "
                      f"the codeword {bridge_cfg.get('codeword')!r} spawns manually",
            "detection": detection,
            "spar_gate": gate,
        }

    if gate_enabled:
        if gate == "unknown":
            return {
                "action": "unknown",
                "reason": "sparmodus state not determinable; refusing to spawn a "
                          "multi-agent audit on an unknown budget state",
                "detection": detection,
                "spar_gate": gate,
            }
        if gate == "on":
            return {
                "action": "skip",
                "reason": "sparmodus/notaus is active; an audit is a multi-agent "
                          "run and is exactly what the spar gate must stop",
                "detection": detection,
                "spar_gate": gate,
            }

    if manual:
        return {
            "action": "spawn",
            "reason": "manual codeword trigger",
            "detection": detection,
            "spar_gate": gate,
        }

    reports_dir = config.get("reports_dir")
    if not reports_dir:
        return {
            "action": "unknown",
            "reason": "no reports_dir resolved; cannot ask system-auditor whether "
                      "anything is due",
            "detection": detection,
            "spar_gate": gate,
        }

    due = due_check(reports_dir)
    if due["due"] is None:
        return {
            "action": "unknown",
            "reason": "system-auditor query failed; due-ness not determinable",
            "detection": detection,
            "spar_gate": gate,
            "due_check": due,
        }
    if not due["due"]:
        return {
            "action": "skip",
            "reason": f"domain {due['domain']!r} already has a report for window {due['window']}",
            "detection": detection,
            "spar_gate": gate,
            "due_check": due,
        }
    return {
        "action": "spawn",
        "reason": f"domain {due['domain']!r} is due for window {due['window']}",
        "detection": detection,
        "spar_gate": gate,
        "due_check": due,
    }


# --------------------------------------------------------------------------
# findings_to_tickets
# --------------------------------------------------------------------------

def _finding_already_ticketed(finding_id: str, tickets_dir: Path) -> bool:
    """Grep every lifecycle ticket file for the finding ID (dedup key)."""
    for entry, _date, _number, _suffix in ticket_writer.iter_lifecycle_files(tickets_dir):
        try:
            text = entry.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if finding_id in text:
            return True
    return False


def _finding_to_ticket_text(path: Path, finding_id: str) -> tuple[str, str]:
    text = path.read_text(encoding="utf-8", errors="replace")
    first_line = next((line for line in text.splitlines() if line.strip()), f"# {finding_id}")
    heading = first_line.lstrip("#").strip()
    _prefix, sep, suffix = heading.partition("—")  # em dash, as findings use it
    title = suffix.strip() if sep and suffix.strip() else heading
    body = (
        "Automatisch aus einem system-auditor-Finding erzeugter Ticket-Entwurf.\n\n"
        f"Finding: {finding_id}\n"
        f"Quelle: {path}\n\n"
        "--- Finding-Inhalt ---\n"
        f"{text.strip()}\n"
    )
    return title, body


def findings_to_tickets(
    findings_dir: str | Path, tickets_dir: str | Path, dry_run: bool = True,
) -> list[dict[str, Any]]:
    """Turn each undedicated system-auditor finding (``findings/M-*.md``) into
    a draft ticket-master INBOX ticket. Dedup key is the finding ID itself
    (``M-YYYYMMDD-slug``, from the filename): a finding already referenced by
    any ticket in any lifecycle folder is skipped, never re-created.

    dry_run=True (default) plans only -- no file is written, ticket_writer is
    never called. Pass dry_run=False (the CLI's --apply) to actually create
    tickets via ticket_writer.create()'s atomic exclusive-create.
    """
    findings_dir = Path(findings_dir)
    tickets_dir = Path(tickets_dir)
    results: list[dict[str, Any]] = []
    if not findings_dir.is_dir():
        return results

    for path in sorted(findings_dir.glob("M-*.md")):
        finding_id = path.stem
        if _finding_already_ticketed(finding_id, tickets_dir):
            results.append({
                "finding_id": finding_id, "finding_path": str(path),
                "status": "skipped_existing", "ticket_path": None,
            })
            continue

        title, body = _finding_to_ticket_text(path, finding_id)
        if dry_run:
            results.append({
                "finding_id": finding_id, "finding_path": str(path),
                "status": "planned", "ticket_path": None, "title": title,
            })
            continue

        ticket_path = ticket_writer.create(
            title, body, priority="mittel", pipeline="<offen>", tickets_dir=tickets_dir,
        )
        results.append({
            "finding_id": finding_id, "finding_path": str(path),
            "status": "created", "ticket_path": ticket_path,
        })
    return results


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _default_tm_config_path() -> Path:
    return Path(__file__).resolve().parent.parent / "config" / "ticket-master.config.json"


def _load_tm_config(path: str | Path | None) -> dict[str, Any]:
    resolved = Path(path) if path else _default_tm_config_path()
    data = _read_json(resolved)
    return data if isinstance(data, dict) else {}


def _resolve_system_auditor_config():
    """Best-effort import of system-auditor's OWN resolved Config (reports_dir/
    findings_dir with <HOME> already expanded), so this bridge never
    reimplements system-auditor's config search path or placeholder expansion
    (P-009). Returns None if system_auditor is not importable or fails to load."""
    try:
        from system_auditor.config import load as sa_load_config
    except ImportError:
        return None
    try:
        return sa_load_config()
    except Exception:  # noqa: BLE001 - "absent beats wrong", never crash the bridge
        return None


def _build_decide_config(tm_config: dict[str, Any]) -> dict[str, Any]:
    sa_config = _resolve_system_auditor_config()
    reports_dir = tm_config.get("reports_dir") or (getattr(sa_config, "reports_dir", "") or None)
    return {
        "auditor_bridge": tm_config.get("auditor_bridge") or {},
        "reports_dir": reports_dir,
        "sparmodus_state_path": tm_config.get("sparmodus_state_path") or str(DEFAULT_SPARMODUS_STATE_PATH),
        "token_budget_path": tm_config.get("token_budget_path") or str(DEFAULT_TOKEN_BUDGET_PATH),
        "thresholds": _load_thresholds(
            tm_config.get("thresholds_path") or str(DEFAULT_THRESHOLDS_CONFIG_PATH)
        ),
    }


def _cli(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="auditor_bridge",
        description="Bridge between ticket-master and the system-auditor CLI.",
    )
    parser.add_argument("--check", action="store_true", help="print the decide() verdict as JSON")
    parser.add_argument("--manual", action="store_true",
                         help="codeword path: bypass enabled/due-check, still gate on presence+sparmodus")
    parser.add_argument("--config", default=None, help="ticket-master.config.json path")
    parser.add_argument("--findings-to-tickets", action="store_true",
                         help="turn undedicated system-auditor findings into draft tickets")
    parser.add_argument("--apply", action="store_true",
                         help="with --findings-to-tickets: actually create tickets (default: dry run)")
    parser.add_argument("--findings-dir", default=None)
    parser.add_argument("--tickets-dir", default=None)
    args = parser.parse_args(argv)

    if not args.check and not args.findings_to_tickets:
        parser.error("one of --check or --findings-to-tickets is required")

    if args.findings_to_tickets:
        tm_config = _load_tm_config(args.config)
        sa_config = _resolve_system_auditor_config()
        findings_dir = args.findings_dir or tm_config.get("findings_dir") or getattr(sa_config, "findings_dir", "")
        tickets_dir = (
            args.tickets_dir or tm_config.get("tickets_dir")
            or os.environ.get("TICKET_MASTER_TICKETS_DIR")
        )
        if not findings_dir or not tickets_dir:
            print(json.dumps({
                "error": "findings_dir/tickets_dir not resolvable; pass --findings-dir/--tickets-dir",
            }))
            return 1
        results = findings_to_tickets(findings_dir, tickets_dir, dry_run=not args.apply)
        print(json.dumps(results, indent=2, ensure_ascii=False))
        return 0

    tm_config = _load_tm_config(args.config)
    result = decide(_build_decide_config(tm_config), manual=args.manual)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    import sys

    raise SystemExit(_cli(sys.argv[1:]))
