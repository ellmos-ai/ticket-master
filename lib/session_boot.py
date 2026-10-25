"""Read-only session brief and a one-companion-per-session claim ledger.

The agent owns interpretation, permission checks and real dispatch. This module
never starts a model, approves a decision or publishes anything.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import importlib.util
import json
import math
import os
import re
import secrets
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

try:
    from . import auditor_bridge, config_paths, session_profile, ticket_writer
except ImportError:
    import auditor_bridge
    import config_paths
    import session_profile
    import ticket_writer

ROLES = ("taskwriter", "tasksolver", "maintainer")
SIGNALS = {
    "error": re.compile(r"\b(error|failed|failure|fehler|fehlgeschlagen|no.go)\b", re.I),
    "blocked": re.compile(r"\b(blocked|blockiert|hold|gesperrt)\b", re.I),
    "decision": re.compile(r"\b(decision|entscheidung|freigabe|approval)\b", re.I),
    "deadline": re.compile(r"\b(deadline|frist|fällig|due)\b", re.I),
}
SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{3,127}$")
LINE_DATE = re.compile(r"^\s*(?:[-*]\s*)?(\d{4}-\d{2}-\d{2})(?:T(\d{2}:\d{2}:\d{2})(Z|[+-]\d{2}:\d{2}))?")


def _files(raw: list[str], *, since: datetime) -> tuple[list[Path], list[dict[str, str]]]:
    files: list[Path] = []
    coverage: list[dict[str, str]] = []
    for source in raw:
        path = config_paths.resolve_config_path(source)
        if path is None:
            coverage.append({"source": source, "status": "invalid"})
            continue
        if not path.exists():
            coverage.append({"source": str(path), "status": "unreadable: source missing"})
            continue
        try:
            candidates = sorted(path.iterdir()) if path.is_dir() else [path]
        except OSError as exc:
            coverage.append({"source": str(path), "status": f"unreadable: {exc}"})
            continue
        if len(candidates) > 100:
            coverage.append({"source": str(path), "status": "too-many-files"})
            continue
        seen = 0
        for candidate in candidates:
            try:
                if candidate.is_file() and candidate.stat().st_mtime >= since.timestamp():
                    files.append(candidate)
                    seen += 1
            except OSError as exc:
                coverage.append({"source": str(candidate), "status": f"unreadable: {exc}"})
        coverage.append({"source": str(path), "status": "scanned", "recent_files": str(seen)})
    return files, coverage


def attention_report(settings: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    if not settings["enabled"]:
        return {"status": "disabled", "items": [], "coverage": []}
    if not settings["sources"]:
        return {"status": "unconfigured", "items": [], "coverage": []}
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(hours=settings["lookback_hours"])
    files, coverage = _files(settings["sources"], since=since)
    items: list[dict[str, str]] = []
    dedup: set[str] = set()
    undated = 0
    for path in files:
        try:
            if path.stat().st_size > 2_000_000:
                coverage.append({"source": str(path), "status": "too-large"})
                continue
            lines = path.read_text(encoding="utf-8-sig").splitlines()
        except (OSError, UnicodeError) as exc:
            coverage.append({"source": str(path), "status": f"unreadable: {exc}"})
            continue
        for number, line in enumerate(lines, 1):
            clean = line.strip()
            if not clean:
                continue
            match = LINE_DATE.match(clean)
            if match is None:
                undated += 1
                continue
            try:
                day = datetime.fromisoformat(match.group(1)).date()
                if match.group(2) and match.group(3):
                    stamp = datetime.fromisoformat(match.group(0).strip().lstrip("-* ").replace("Z", "+00:00"))
                    if not since <= stamp.astimezone(timezone.utc) <= now:
                        continue
                    time_basis = "line-timestamp"
                else:
                    if not since.date() <= day <= now.date():
                        continue
                    time_basis = "date-only; boundary hour unverified"
            except ValueError:
                undated += 1
                continue
            signal = next(((key, SIGNALS[key].search(clean)) for key in settings["filters"]
                           if SIGNALS[key].search(clean)), None)
            if signal is None:
                continue
            kind, hit = signal
            snippet = clean if len(clean) <= 500 else clean[max(0, hit.start() - 150):hit.end() + 250]
            key = re.sub(r"\s+", " ", snippet).casefold()
            if key in dedup:
                continue
            dedup.add(key)
            items.append({"kind": kind, "text": snippet, "source": f"{path}:{number}", "time_basis": time_basis})
            if len(items) >= 50:
                coverage.append({"source": "all", "status": "item-cap-50"})
                return {"status": "partial", "since": since.isoformat(), "items": items, "coverage": coverage,
                        "time_basis": "UTC line timestamps when present; date-only candidates need boundary check",
                        "undated_lines_excluded": undated}
    status = "partial" if any(row["status"] != "scanned" for row in coverage) else "scanned"
    return {"status": status, "since": since.isoformat(), "items": items, "coverage": coverage,
            "time_basis": "UTC line timestamps when present; date-only candidates need boundary check",
            "undated_lines_excluded": undated}


def decision_sources(settings: dict[str, Any], routing: dict[str, Any] | None = None) -> dict[str, Any]:
    if not settings["enabled"]:
        return {"status": "disabled", "sources": []}
    routing = routing or {}
    module_path = config_paths.resolve_config_path(routing.get("decision_index_module"))
    root = config_paths.resolve_config_path(routing.get("decision_root"))
    if not module_path or not root:
        return {"status": "unconfigured", "sources": settings.get("sources", []),
                "caveat": "No canonical decision parser configured; do not infer open approvals."}
    try:
        spec = importlib.util.spec_from_file_location("ticket_master_private_decision_index", module_path)
        if spec is None or spec.loader is None:
            raise ValueError("cannot load decision index module")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        index = module.build_index(root)  # canonical parser, live read-only build
        if index.get("schema") != "decisions.index/1":
            raise ValueError("unexpected decision index schema")
        contract = index["active_contract"]
        if not contract["valid"]:
            return {"status": "invalid-active-contract", "module": str(module_path),
                    "root": str(root), "errors": contract["errors"], "candidates": []}
        entries = [entry for entry in index["entries"]
                   if entry.get("domain") == "active" and entry.get("decision_ready") is True]
        return {"status": "canonical-current-candidates", "module": str(module_path),
                "root": str(root), "candidates": [
                    {key: entry.get(key) for key in ("id", "title", "source_file", "source_line", "recommendation_excerpt")}
                    for entry in entries],
                "caveat": "Candidates still need current chat, project authority and ticket checks before user escalation."}
    except Exception as exc:  # noqa: BLE001 - untrusted/stale local parser is unknown, never approval
        return {"status": "unknown", "module": str(module_path), "root": str(root),
                "error": str(exc), "candidates": []}


def route_approval_case(case: dict[str, Any]) -> dict[str, Any]:
    """Classify a *documented* case; never infer a grant from ticket text.

    The caller supplies fresh readbacks from the canonical decision source,
    project policy and permitted profile/memory checks. This function does not
    read or mutate a queue and its verdict is not a dispatch receipt.
    """
    checks = case.get("checks")
    required_checks = {"decision_index", "standing_authority", "profile_memory", "project_policy"}
    if not isinstance(checks, dict) or not required_checks <= checks.keys():
        return {"route": "investigate", "reason": "required authority readbacks missing", "checks": checks}
    valid_checks = {"checked_nohit", "checked_hit", "unknown", "not_applicable"}
    if any(not isinstance(checks[key], dict) or checks[key].get("status") not in valid_checks
           for key in required_checks):
        return {"route": "investigate", "reason": "invalid authority readback status", "checks": checks}
    if any(checks[key]["status"].startswith("checked_") and not checks[key].get("source")
           for key in required_checks):
        return {"route": "investigate", "reason": "authority readback source missing", "checks": checks}
    if case.get("technical_blocker") is True:
        return {"route": "worker", "reason": "technical blocker needs worker diagnosis", "checks": checks}
    if any(checks[key]["status"] == "unknown" for key in required_checks):
        return {"route": "investigate", "reason": "authority check unresolved", "checks": checks}
    requirement = case.get("decision_requirement")
    if not isinstance(requirement, dict) or requirement.get("readback") != "current" or not requirement.get("primary_source"):
        return {"route": "investigate", "reason": "project decision requirement unverified", "checks": checks}
    if (checks["project_policy"]["status"] != "checked_hit"
            or checks["project_policy"].get("source") != requirement["primary_source"]):
        return {"route": "investigate", "reason": "project policy readback does not support requirement", "checks": checks}
    status = requirement.get("status")
    if status not in {"required", "not_required"}:
        return {"route": "investigate", "reason": "project decision requirement unresolved", "checks": checks}
    authorities = case.get("authorities", [])
    if not isinstance(authorities, list):
        return {"route": "investigate", "reason": "malformed authority evidence", "checks": checks}
    for item in authorities:
        if not isinstance(item, dict):
            return {"route": "investigate", "reason": "malformed authority evidence", "checks": checks}
        lookup = {"direct_user_decision": "decision_index", "standing_authority": "standing_authority"}.get(item.get("kind"))
        if (lookup is not None and checks[lookup]["status"] == "checked_hit"
                and item.get("primary_source") and item.get("readback") == "current"
                and item.get("scope_match") is True and item.get("valid") is True
                and item.get("revoked") is False and item.get("conflict") is False):
            return {"route": "authorized_work", "reason": "current scope-matching primary authority",
                    "authority": {"kind": item["kind"], "primary_source": item["primary_source"]},
                    "checks": checks}
    if status == "not_required":
        return {"route": "authorized_work", "reason": "current project rule does not require new user approval",
                "authority": {"kind": "project_rule", "primary_source": requirement["primary_source"]},
                "checks": checks}
    reason = ("prior authority is expired, revoked, conflicting or out of scope"
              if any(checks[key]["status"] == "checked_hit"
                     for key in ("decision_index", "standing_authority", "profile_memory"))
              else "current project rule requires a user choice; no current authority found")
    return {"route": "user_ticket", "reason": reason,
            "ticket_required": True, "checks": checks}


def approval_source_coverage(routing: dict[str, Any]) -> list[dict[str, str]]:
    """Declare attached approval sources without pretending manual roots were read."""
    if not routing["enabled"]:
        return []
    rows = []
    for item in routing.get("approval_sources", []):
        path = config_paths.resolve_config_path(item["path"])
        if path is None or not path.exists():
            status = "missing"
        elif item["connector"] == "canonical_index":
            status = "canonical-index-attached"
        else:
            status = "manual-check-required; no producer connector"
        rows.append({"name": item["name"], "source": str(path) if path else item["path"],
                     "status": status})
    return rows


def approval_ticket_plan(case: dict[str, Any], tickets_dir: str | Path) -> dict[str, Any]:
    """Find an existing approval ticket; never create a second ticket here."""
    verdict = route_approval_case(case)
    if verdict["route"] != "user_ticket":
        return {"status": "not-user-approval", "routing": verdict}
    identity = case.get("approval_identity")
    if not isinstance(identity, dict):
        return {"status": "identity-missing", "routing": verdict}
    did = identity.get("decision_id")
    if isinstance(did, str) and re.fullmatch(r"D-\d{8}-\d{3,}", did):
        marker = did
    else:
        parts = [identity.get(key) for key in ("action", "version", "recipient")]
        if any(not isinstance(part, str) or not part.strip() for part in parts):
            return {"status": "identity-missing", "routing": verdict}
        marker = "APPROVAL-REF: " + " | ".join(part.strip() for part in parts)
    try:
        base = ticket_writer.require_verified_queue_root(Path(tickets_dir))
        matches = []
        for path, *_rest in ticket_writer.iter_lifecycle_files(base):
            content = path.read_text(encoding="utf-8-sig")
            if marker.startswith("D-"):
                # A prefix such as D-...-001 must not reuse D-...-0012.
                found = marker in re.findall(
                    r"(?<![A-Za-z0-9_-])D-\d{8}-\d{3,}(?![A-Za-z0-9_-])", content
                )
            else:
                found = marker in (line.strip() for line in content.splitlines())
            if found:
                matches.append(str(path))
    except (OSError, UnicodeError, ValueError) as exc:
        return {"status": "unknown", "reason": str(exc), "routing": verdict}
    if len(matches) > 1:
        return {"status": "duplicate-conflict", "marker": marker, "tickets": matches}
    if matches:
        return {"status": "reuse", "marker": marker, "ticket": matches[0]}
    return {"status": "create-with-ticket-writer", "marker": marker,
            "reason": "verified approval still lacks a lifecycle ticket"}


def report(config: dict[str, Any] | None = None) -> dict[str, Any]:
    config = config if config is not None else config_paths.tm_config()
    profile = session_profile.effective_settings(config)
    settings = profile["settings"]
    audit = {"action": "disabled-by-profile"}
    if settings["audit"]["enabled"]:
        audit_config = auditor_bridge._build_decide_config(config)
        audit_config["auditor_bridge"] = {**audit_config["auditor_bridge"], "enabled": True}
        audit = auditor_bridge.decide(audit_config)
    return {"profile": profile,
            "approval_routing": {"enabled": settings["approval_routing"]["enabled"],
                                 "all_user_approvals_as_tickets": settings["approval_routing"]["all_user_approvals_as_tickets"],
                                 "sources": approval_source_coverage(settings["approval_routing"]),
                                 "rule": "USER status or an avatar prediction is never an authority grant"},
            "decisions": decision_sources(settings["decision_draw"], settings["approval_routing"]),
            "attention": attention_report(settings["attention"]), "audit": audit,
            "companion": {"enabled": settings["companion"]["enabled"], "roles": list(ROLES),
                          "max_luna": settings["companion"]["max_luna"],
                          "max_high": settings["companion"]["max_high"],
                          "dispatch": "agent checks live slots/authority, claims once, dispatches and records acceptance"},
            "umbruch": {"enabled": settings["umbruch"]["enabled"],
                        "rule": "Concrete version-linked user approval may replace model reviews; publication requires explicit publication-linked approval."}}


def _ledger_dir(profile: dict[str, Any]) -> Path:
    return Path(profile["source"]).parent / "session-claims"


def _dispatch_name(session_id: str, claim_id: str) -> str:
    digest = hashlib.sha256(f"{session_id}:{claim_id}".encode("utf-8")).hexdigest()[:32]
    return f"tm-{digest}"


def _verified_launcher_dispatch(item: dict[str, Any], receipt: str,
                                config: dict[str, Any] | None) -> dict[str, Any]:
    """Read the installed agent-launcher registry; a caller string is no proof."""
    name = item.get("dispatch_name")
    claim_id = item.get("claim_id")
    role = item.get("role")
    if (not isinstance(claim_id, str) or not re.fullmatch(r"[0-9a-f]{32}", claim_id)
            or role not in ROLES or name != _dispatch_name(item["session_id"], claim_id)
            or receipt != f"agent-launcher:{name}"):
        raise ValueError("unsupported or mismatched dispatch receipt")
    try:
        launcher = importlib.import_module("agent_launcher")
    except ImportError as exc:
        raise ValueError("agent-launcher readback unavailable; claim remains pending") from exc
    registry_dir = (config or {}).get("agent_launcher_registry_dir")
    if registry_dir is None:
        registry_dir = Path.home() / ".agent-launcher" / "registry"
    elif not isinstance(registry_dir, str) or not registry_dir:
        raise ValueError("invalid agent-launcher registry directory")
    registry_type = getattr(launcher, "AgentProcessRegistry", None)
    if registry_type is None:
        raise ValueError("agent-launcher registry API unavailable")
    registry = registry_type(Path(registry_dir))
    metadata = registry.load_metadata(name)
    if not isinstance(metadata, dict) or metadata.get("launch_state") != "active":
        raise ValueError("agent-launcher active receipt missing")
    pid = metadata.get("pid")
    birth = metadata.get("process_create_time")
    created = datetime.fromisoformat(item["created_at"])
    if (not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0
            or not isinstance(birth, (int, float)) or isinstance(birth, bool)
            or not math.isfinite(birth) or birth <= 0
            or created.tzinfo is None
            or birth < created.timestamp()
            or metadata.get("name") != name
            or metadata.get("ticket_master_session_id") != item["session_id"]
            or metadata.get("ticket_master_claim_id") != item["claim_id"]
            or metadata.get("ticket_master_role") != item["role"]):
        raise ValueError("agent-launcher receipt is not bound to this claim")
    try:
        observed = registry.probe_running(name)
    except (AttributeError, OSError, ValueError) as exc:
        raise ValueError("agent-launcher process identity is unreadable") from exc
    if (not isinstance(observed, int) or isinstance(observed, bool)
            or observed != pid or registry.load_metadata(name) != metadata):
        raise ValueError("agent-launcher process identity is not verifiable")
    return {"provider": "agent-launcher", "name": name, "pid": pid,
            "process_create_time": birth, "claim_id": item["claim_id"]}


def companion_claim(session_id: str, *, config: dict[str, Any] | None = None) -> dict[str, Any]:
    if not SESSION_ID.fullmatch(session_id):
        raise ValueError("invalid session ID")
    profile = session_profile.effective_settings(config)
    if not profile["settings"]["companion"]["enabled"]:
        return {"status": "disabled"}
    directory = _ledger_dir(profile)
    directory.mkdir(parents=True, exist_ok=True)
    own = directory / f"{session_id}.json"
    accepted = []
    for path in directory.glob("*.json"):
        try:
            item = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ValueError(f"companion ledger unreadable: {path}: {exc}") from exc
        if not isinstance(item, dict) or item.get("schema") != "ticket-master.companion-claim.v2":
            raise ValueError(f"companion ledger invalid: {path}")
        if item.get("status") == "accepted" and item.get("role") in ROLES:
            if not isinstance(item.get("acceptance_evidence"), dict):
                raise ValueError(f"companion acceptance unverified: {path}")
            accepted.append((item.get("created_at", ""), item["role"]))
    previous = max(accepted, default=("", ROLES[-1]))[1]
    role = ROLES[(ROLES.index(previous) + 1) % len(ROLES)]
    claim_id = secrets.token_hex(16)
    claim = {"schema": "ticket-master.companion-claim.v2", "session_id": session_id,
             "role": role, "status": "pending", "created_at": datetime.now(timezone.utc).isoformat(),
             "model_tier": "low-or-medium", "claim_id": claim_id,
             "dispatch_name": _dispatch_name(session_id, claim_id), "acceptance": None}
    try:
        with own.open("x", encoding="utf-8") as handle:
            json.dump(claim, handle, ensure_ascii=False)
            handle.write("\n")
    except FileExistsError:
        return json.loads(own.read_text(encoding="utf-8"))
    return claim


def companion_ack(session_id: str, receipt: str, *, config: dict[str, Any] | None = None) -> dict[str, Any]:
    if not SESSION_ID.fullmatch(session_id) or not receipt.strip():
        raise ValueError("session ID and real dispatch receipt required")
    own = _ledger_dir(session_profile.effective_settings(config)) / f"{session_id}.json"
    item = json.loads(own.read_text(encoding="utf-8"))
    if (not isinstance(item, dict) or item.get("schema") != "ticket-master.companion-claim.v2"
            or item.get("session_id") != session_id):
        raise ValueError("companion claim is not verifiable")
    if item["status"] == "accepted":
        return item
    if item["status"] != "pending":
        raise ValueError("claim is not pending")
    evidence = _verified_launcher_dispatch(item, receipt, config)
    item.update(status="accepted", acceptance=receipt, acceptance_evidence=evidence,
                accepted_at=datetime.now(timezone.utc).isoformat())
    temp = own.with_suffix(f".{os.getpid()}.tmp")
    with temp.open("x", encoding="utf-8") as handle:
        json.dump(item, handle, ensure_ascii=False)
        handle.write("\n")
    os.replace(temp, own)
    return item


def _cli() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--report", action="store_true")
    group.add_argument("--claim-companion", metavar="SESSION_ID")
    group.add_argument("--ack-companion", metavar="SESSION_ID")
    group.add_argument("--route-evidence", metavar="JSON_FILE",
                       help="classify current approval evidence without changing a ticket")
    group.add_argument("--plan-approval-ticket", metavar="JSON_FILE",
                       help="find a lifecycle ticket for a documented user approval")
    parser.add_argument("--receipt", help="agent-launcher:<claim dispatch_name>, verified against its native registry")
    parser.add_argument("--tickets-dir", help="verified canonical queue root for --plan-approval-ticket")
    parser.add_argument("--config")
    args = parser.parse_args()
    config = config_paths.tm_config(args.config)
    try:
        if args.report:
            result = report(config)
        elif args.route_evidence:
            evidence = json.loads(Path(args.route_evidence).read_text(encoding="utf-8-sig"))
            if not isinstance(evidence, dict):
                raise ValueError("route evidence must be a JSON object")
            result = route_approval_case(evidence)
        elif args.plan_approval_ticket:
            if not args.tickets_dir:
                raise ValueError("--tickets-dir is required for --plan-approval-ticket")
            evidence = json.loads(Path(args.plan_approval_ticket).read_text(encoding="utf-8-sig"))
            if not isinstance(evidence, dict):
                raise ValueError("approval evidence must be a JSON object")
            result = approval_ticket_plan(evidence, args.tickets_dir)
        elif args.claim_companion:
            result = companion_claim(args.claim_companion, config=config)
        else:
            result = companion_ack(args.ack_companion, args.receipt or "", config=config)
    except (OSError, ValueError, KeyError) as exc:
        print(json.dumps({"status": "unknown", "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
