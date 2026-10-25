"""Read-only session brief and a one-companion-per-session claim ledger.

The agent owns interpretation, permission checks and real dispatch. This module
never starts a model, approves a decision or publishes anything.
"""

from __future__ import annotations

import argparse
import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

try:
    from . import auditor_bridge, config_paths, session_profile
except ImportError:
    import auditor_bridge
    import config_paths
    import session_profile

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


def decision_sources(settings: dict[str, Any]) -> dict[str, Any]:
    if not settings["enabled"]:
        return {"status": "disabled", "sources": []}
    if not settings.get("sources"):
        return {"status": "unconfigured", "sources": []}
    results = []
    for raw in settings.get("sources", []):
        path = config_paths.resolve_config_path(raw)
        if path is None:
            results.append({"source": raw, "status": "invalid"})
            continue
        try:
            data = path.read_bytes()
            if len(data) > 2_000_000:
                results.append({"source": str(path), "status": "too-large"})
                continue
            text = data.decode("utf-8-sig")
        except (OSError, UnicodeError) as exc:
            results.append({"source": str(path), "status": f"unreadable: {exc}"})
            continue
        candidates = [{"line": n, "text": line.strip()[:300]} for n, line in enumerate(text.splitlines(), 1)
                      if re.search(r"\b(D-\d{8}-\d+|OFFEN|PENDING|TO.DECIDE)\b", line, re.I)][:100]
        results.append({"source": str(path), "status": "read", "candidates": candidates})
    return {"status": "candidates-only", "sources": results,
            "caveat": "Check current chat, project registers and decisions before calling any candidate open."}


def report(config: dict[str, Any] | None = None) -> dict[str, Any]:
    config = config if config is not None else config_paths.tm_config()
    profile = session_profile.effective_settings(config)
    settings = profile["settings"]
    audit = {"action": "disabled-by-profile"}
    if settings["audit"]["enabled"]:
        audit_config = auditor_bridge._build_decide_config(config)
        audit_config["auditor_bridge"] = {**audit_config["auditor_bridge"], "enabled": True}
        audit = auditor_bridge.decide(audit_config)
    return {"profile": profile, "decisions": decision_sources(settings["decision_draw"]),
            "attention": attention_report(settings["attention"]), "audit": audit,
            "companion": {"enabled": settings["companion"]["enabled"], "roles": list(ROLES),
                          "max_luna": settings["companion"]["max_luna"],
                          "max_high": settings["companion"]["max_high"],
                          "dispatch": "agent checks live slots/authority, claims once, dispatches and records acceptance"},
            "umbruch": {"enabled": settings["umbruch"]["enabled"],
                        "rule": "Concrete version-linked user approval may replace model reviews; publication requires explicit publication-linked approval."}}


def _ledger_dir(profile: dict[str, Any]) -> Path:
    return Path(profile["source"]).parent / "session-claims"


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
        if not isinstance(item, dict) or item.get("schema") != "ticket-master.companion-claim.v1":
            raise ValueError(f"companion ledger invalid: {path}")
        if item.get("status") == "accepted" and item.get("role") in ROLES:
            accepted.append((item.get("created_at", ""), item["role"]))
    previous = max(accepted, default=("", ROLES[-1]))[1]
    role = ROLES[(ROLES.index(previous) + 1) % len(ROLES)]
    claim = {"schema": "ticket-master.companion-claim.v1", "session_id": session_id,
             "role": role, "status": "pending", "created_at": datetime.now(timezone.utc).isoformat(),
             "model_tier": "low-or-medium", "acceptance": None}
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
    if item["status"] == "accepted":
        return item
    if item["status"] != "pending":
        raise ValueError("claim is not pending")
    item.update(status="accepted", acceptance=receipt, accepted_at=datetime.now(timezone.utc).isoformat())
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
    parser.add_argument("--receipt", help="real dispatch acceptance reference, for --ack-companion")
    parser.add_argument("--config")
    args = parser.parse_args()
    config = config_paths.tm_config(args.config)
    try:
        if args.report:
            result = report(config)
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
