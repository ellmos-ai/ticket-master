"""Configurable ticket bundling with bidirectional ID pointers (bundles-reg.json).

Implements T-20261027-369717637:
- Canonical BundleID (B-YYYYMMDD-#########) and TicketID normalization independent
  of host suffixes or queue lifecycle directories.
- Bidirectional pointer consistency between bundles[] and ticket_pointers{} in
  bundles-reg.json, plus optional preimage-checked BUNDLE_ID pointer updates on
  ticket files.
- Strict respect for config enablement, active claims/delegations, locks, PARKED/
  WAITING/USER deferrals, and individual ticket lifecycles (a completed bundle
  never marks member tickets SOLVED automatically).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from .ticket_writer import ID_DIGITS, draw_id
except ImportError:
    from ticket_writer import ID_DIGITS, draw_id


BUNDLES_REGISTRY_SCHEMA = "ellmos.ticket.bundles-reg.v1"
BUNDLE_STATES = frozenset({"proposed", "bound", "executing", "completed", "partial"})
MEMBER_OUTCOMES = frozenset({"pending", "bound", "executing", "done", "failed", "released"})
DISALLOWED_QUEUE_STATUSES = frozenset({"PARKED", "WAITING", "USER", "SOLVED", "ARCHIVE"})

_BUNDLE_ID_RE = re.compile(rf"^B-[0-9]{{8}}-[1-9][0-9]{{{ID_DIGITS - 1}}}$")
_TICKET_ID_EXTRACT_RE = re.compile(r"(T-[0-9]{8}-(?:[1-9][0-9]{8}|[0-9]{2,9}))")


class TicketBundleError(ValueError):
    """Raised when a bundle operation violates contract, claim, or config rules."""


class PreimageConflictError(TicketBundleError):
    """Raised when a ticket or registry preimage hash no longer matches."""


def normalize_ticket_id(value: str | Path) -> str:
    """Extract the canonical T-YYYYMMDD-... ID from a path, filename, or ID string."""
    text = Path(value).name if isinstance(value, Path) else str(value).strip()
    if "/" in text or "\\" in text:
        text = Path(text).name
    match = _TICKET_ID_EXTRACT_RE.search(text)
    if not match:
        raise TicketBundleError(f"cannot extract canonical TicketID from {value!r}")
    return match.group(1)


def is_bundling_enabled(config: Mapping[str, Any] | None) -> bool:
    """Return True only when bundling is explicitly enabled in config."""
    if not isinstance(config, Mapping):
        return False
    bundles_cfg = config.get("bundles")
    if isinstance(bundles_cfg, Mapping):
        return bundles_cfg.get("enabled") is True
    return config.get("bundles_enabled") is True


def _require_enabled(config: Mapping[str, Any] | None) -> None:
    if not is_bundling_enabled(config):
        raise TicketBundleError("ticket bundling is disabled by configuration")


def _require_timestamp(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise TicketBundleError(f"{field} must be an ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise TicketBundleError(f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise TicketBundleError(f"{field} must include a timezone")
    return value


def validate_bundles_registry(data: Mapping[str, Any]) -> dict[str, Any]:
    """Validate bidirectional consistency of bundles-reg.json."""
    if not isinstance(data, Mapping):
        raise TicketBundleError("bundles registry must be an object")
    expected_top = {"schema", "updated_at", "bundles", "ticket_pointers"}
    if set(data.keys()) != expected_top:
        raise TicketBundleError("bundles registry has invalid top-level fields")
    if data["schema"] != BUNDLES_REGISTRY_SCHEMA:
        raise TicketBundleError("unsupported bundles registry schema")
    updated_at = _require_timestamp(data["updated_at"], "updated_at")

    bundles_raw = data["bundles"]
    pointers_raw = data["ticket_pointers"]
    if not isinstance(bundles_raw, list):
        raise TicketBundleError("bundles must be an array")
    if not isinstance(pointers_raw, Mapping):
        raise TicketBundleError("ticket_pointers must be an object")

    seen_bundle_ids: set[str] = set()
    computed_pointers: dict[str, str] = {}
    canonical_bundles: list[dict[str, Any]] = []

    req_bundle_fields = {
        "bundle_id",
        "state",
        "ticket_ids",
        "rationale",
        "source_ref",
        "owner_actor",
        "scope_key",
        "created_at",
        "updated_at",
        "member_outcomes",
    }

    for idx, item in enumerate(bundles_raw):
        if not isinstance(item, Mapping) or set(item.keys()) != req_bundle_fields:
            raise TicketBundleError(f"bundles[{idx}] has invalid fields")
        bundle_id = item["bundle_id"]
        if not isinstance(bundle_id, str) or not _BUNDLE_ID_RE.fullmatch(bundle_id):
            raise TicketBundleError(f"bundles[{idx}].bundle_id is invalid")
        if bundle_id in seen_bundle_ids:
            raise TicketBundleError(f"duplicate bundle_id: {bundle_id}")
        seen_bundle_ids.add(bundle_id)

        state = item["state"]
        if not isinstance(state, str) or state not in BUNDLE_STATES:
            raise TicketBundleError(f"bundles[{idx}].state is unsupported")

        raw_tids = item["ticket_ids"]
        if not isinstance(raw_tids, list) or len(raw_tids) < 2:
            raise TicketBundleError(f"bundles[{idx}].ticket_ids must contain at least 2 tickets")
        norm_tids: list[str] = []
        seen_local: set[str] = set()
        for tid in raw_tids:
            if not isinstance(tid, str):
                raise TicketBundleError("ticket_id must be a string")
            canonical_tid = normalize_ticket_id(tid)
            if canonical_tid != tid:
                raise TicketBundleError(
                    f"ticket_ids must store canonical IDs without path/host suffix: {tid}"
                )
            if canonical_tid in seen_local:
                raise TicketBundleError(f"duplicate member {canonical_tid} in {bundle_id}")
            if canonical_tid in computed_pointers:
                raise TicketBundleError(
                    f"ticket {canonical_tid} is already bound to {computed_pointers[canonical_tid]}"
                )
            seen_local.add(canonical_tid)
            norm_tids.append(canonical_tid)
            computed_pointers[canonical_tid] = bundle_id

        for str_field in ("rationale", "source_ref", "owner_actor", "scope_key"):
            val = item[str_field]
            if not isinstance(val, str) or not val.strip():
                raise TicketBundleError(f"bundles[{idx}].{str_field} must be a non-empty string")

        c_at = _require_timestamp(item["created_at"], f"bundles[{idx}].created_at")
        u_at = _require_timestamp(item["updated_at"], f"bundles[{idx}].updated_at")

        outcomes = item["member_outcomes"]
        if not isinstance(outcomes, Mapping) or set(outcomes.keys()) != seen_local:
            raise TicketBundleError(
                f"bundles[{idx}].member_outcomes keys must match ticket_ids exactly"
            )
        clean_outcomes: dict[str, str] = {}
        for tid in norm_tids:
            out_val = outcomes[tid]
            if not isinstance(out_val, str) or out_val not in MEMBER_OUTCOMES:
                raise TicketBundleError(f"invalid member outcome for {tid}: {out_val!r}")
            clean_outcomes[tid] = out_val

        canonical_bundles.append(
            {
                "bundle_id": bundle_id,
                "state": state,
                "ticket_ids": norm_tids,
                "rationale": item["rationale"].strip(),
                "source_ref": item["source_ref"].strip(),
                "owner_actor": item["owner_actor"].strip(),
                "scope_key": item["scope_key"].strip(),
                "created_at": c_at,
                "updated_at": u_at,
                "member_outcomes": clean_outcomes,
            }
        )

    clean_pointers: dict[str, str] = {}
    for k, v in pointers_raw.items():
        if not isinstance(k, str) or not isinstance(v, str):
            raise TicketBundleError("ticket_pointers keys and values must be strings")
        clean_pointers[k] = v

    if clean_pointers != computed_pointers:
        raise TicketBundleError(
            "bidirectional pointer mismatch between bundles[] and ticket_pointers{}"
        )

    return {
        "schema": BUNDLES_REGISTRY_SCHEMA,
        "updated_at": updated_at,
        "bundles": canonical_bundles,
        "ticket_pointers": clean_pointers,
    }


def parse_ticket_metadata(ticket_path: Path) -> dict[str, Any]:
    """Parse canonical fields, delegation, status, and SHA-256 preimage from a ticket file."""
    raw_bytes = ticket_path.read_bytes()
    preimage_sha256 = hashlib.sha256(raw_bytes).hexdigest()
    text = raw_bytes.decode("utf-8")
    ticket_id = normalize_ticket_id(ticket_path.name)
    status = ""
    pipeline = ""
    project_dir = ""
    delegated_to = ""
    bundle_pointer = ""

    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("ID:"):
            raw_id = stripped.split(":", 1)[1].strip()
            if raw_id:
                ticket_id = normalize_ticket_id(raw_id)
        elif stripped.startswith("STATUS:"):
            status = stripped.split(":", 1)[1].strip()
        elif stripped.startswith("PIPELINE:"):
            pipeline = stripped.split(":", 1)[1].strip()
        elif stripped.startswith("PROJEKTORDNER:"):
            project_dir = stripped.split(":", 1)[1].strip()
        elif stripped.startswith("DELEGIERT_AN:"):
            delegated_to = stripped.split(":", 1)[1].strip()
        elif stripped.startswith("BUNDLE_ID:"):
            bundle_pointer = stripped.split(":", 1)[1].strip()

    parent_queue = ticket_path.parent.name.upper()
    return {
        "ticket_id": ticket_id,
        "path": ticket_path,
        "queue": parent_queue,
        "status": status,
        "pipeline": pipeline,
        "project_dir": project_dir,
        "delegated_to": delegated_to,
        "bundle_id": bundle_pointer,
        "preimage_sha256": preimage_sha256,
    }


def check_ticket_eligible(
    meta: Mapping[str, Any],
    *,
    actor_ref: str,
    locked_ticket_ids: Iterable[str] = (),
) -> None:
    """Ensure a ticket is eligible for bundling by actor_ref (fail-closed)."""
    tid = meta["ticket_id"]
    if tid in set(locked_ticket_ids):
        raise TicketBundleError(f"ticket {tid} is locked and cannot be bundled")
    queue = str(meta.get("queue", "")).upper()
    if queue in DISALLOWED_QUEUE_STATUSES:
        raise TicketBundleError(f"ticket {tid} in queue {queue} is not eligible for bundling")
    status_upper = str(meta.get("status", "")).upper()
    for token in ("PARKED", "WAITING", "USER/", "SOLVED", "SPÄTER", "ZURÜCKGESTELLT"):
        if token in status_upper:
            raise TicketBundleError(
                f"ticket {tid} has deferred/blocked status ({meta.get('status')}) and cannot be auto-bundled"
            )
    delegated = str(meta.get("delegated_to", "")).strip()
    if delegated and delegated != actor_ref:
        raise TicketBundleError(
            f"ticket {tid} is already delegated to foreign actor {delegated!r} (caller: {actor_ref!r})"
        )


def suggest_bundles(
    ticket_paths: Iterable[Path],
    *,
    actor_ref: str,
    config: Mapping[str, Any] | None,
    locked_ticket_ids: Iterable[str] = (),
) -> list[dict[str, Any]]:
    """Suggest context-coherent bundles across eligible tickets without mutating state."""
    _require_enabled(config)
    locked_set = {normalize_ticket_id(x) for x in locked_ticket_ids}
    groups: dict[str, list[dict[str, Any]]] = {}

    for path in sorted(ticket_paths, key=lambda p: p.name):
        meta = parse_ticket_metadata(Path(path))
        try:
            check_ticket_eligible(meta, actor_ref=actor_ref, locked_ticket_ids=locked_set)
        except TicketBundleError:
            continue
        pipeline = meta["pipeline"].strip().lower()
        proj = Path(meta["project_dir"]).name.strip().lower() if meta["project_dir"] else ""
        if not pipeline and not proj or pipeline.startswith("<") or proj.startswith("<"):
            continue
        scope_key = f"{pipeline}:{proj}" if pipeline and proj else (pipeline or proj)
        groups.setdefault(scope_key, []).append(meta)

    suggestions: list[dict[str, Any]] = []
    for scope_key, members in sorted(groups.items()):
        if len(members) >= 2:
            suggestions.append(
                {
                    "scope_key": scope_key,
                    "ticket_ids": [m["ticket_id"] for m in members],
                    "rationale": f"Gemeinsamer Kontext und Scope ({scope_key}) für autorisierten Bearbeiter {actor_ref}",
                }
            )
    return suggestions


class BundlesRegistry:
    """Manages bundles-reg.json and optional ticket-file BUNDLE_ID pointer updates."""

    def __init__(self, registry_path: Path | str):
        self.registry_path = Path(registry_path)

    def load(self) -> tuple[dict[str, Any], str]:
        """Load and validate bundles-reg.json, returning (registry_dict, sha256_hex)."""
        if not self.registry_path.exists():
            empty = {
                "schema": BUNDLES_REGISTRY_SCHEMA,
                "updated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "bundles": [],
                "ticket_pointers": {},
            }
            return empty, ""
        raw = self.registry_path.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        data = json.loads(raw.decode("utf-8"))
        return validate_bundles_registry(data), digest

    def _save_atomic(self, registry: Mapping[str, Any], expected_sha256: str | None = None) -> str:
        validated = validate_bundles_registry(registry)
        self.registry_path.parent.mkdir(parents=True, exist_ok=True)
        if expected_sha256 is not None:
            if self.registry_path.exists():
                current_sha = hashlib.sha256(self.registry_path.read_bytes()).hexdigest()
            else:
                current_sha = ""
            if current_sha != expected_sha256:
                raise PreimageConflictError(
                    "bundles-reg.json changed concurrently (preimage SHA-256 mismatch)"
                )
        payload = (
            json.dumps(validated, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        ).encode("utf-8")
        tmp_path = self.registry_path.with_suffix(f".tmp.{os.getpid()}")
        try:
            tmp_path.write_bytes(payload)
            os.replace(tmp_path, self.registry_path)
        finally:
            tmp_path.unlink(missing_ok=True)
        return hashlib.sha256(payload).hexdigest()

    def create_bundle(
        self,
        *,
        ticket_paths: Iterable[Path],
        actor_ref: str,
        rationale: str,
        source_ref: str,
        scope_key: str,
        config: Mapping[str, Any] | None,
        state: str = "bound",
        locked_ticket_ids: Iterable[str] = (),
        expected_preimages: Mapping[str, str] | None = None,
        write_ticket_pointers: bool = True,
        now: datetime | None = None,
        rng: Any = None,
    ) -> dict[str, Any]:
        """Create or idempotently return a bundle with bidirectional pointers."""
        _require_enabled(config)
        if state not in {"proposed", "bound"}:
            raise TicketBundleError("initial bundle state must be 'proposed' or 'bound'")
        paths = [Path(p) for p in ticket_paths]
        if len(paths) < 2:
            raise TicketBundleError("a bundle requires at least 2 tickets")

        metas = [parse_ticket_metadata(p) for p in paths]
        seen_ids: set[str] = set()
        canonical_ids: list[str] = []
        for m in metas:
            tid = m["ticket_id"]
            if tid in seen_ids:
                raise TicketBundleError(f"duplicate ticket {tid} in bundle request")
            seen_ids.add(tid)
            canonical_ids.append(tid)
            if expected_preimages and tid in expected_preimages:
                if m["preimage_sha256"] != expected_preimages[tid]:
                    raise PreimageConflictError(f"preimage mismatch for ticket {tid}")
            check_ticket_eligible(
                m, actor_ref=actor_ref, locked_ticket_ids=locked_ticket_ids
            )

        reg, reg_sha = self.load()
        # Idempotent replay check: if exact same ticket set already exists for same owner & scope
        for existing in reg["bundles"]:
            if (
                existing["ticket_ids"] == canonical_ids
                and existing["owner_actor"] == actor_ref
                and existing["scope_key"] == scope_key
            ):
                return dict(existing)

        for tid in canonical_ids:
            if tid in reg["ticket_pointers"]:
                raise TicketBundleError(
                    f"ticket {tid} already belongs to bundle {reg['ticket_pointers'][tid]}"
                )

        ts = now or datetime.now(timezone.utc)
        ts_str = ts.isoformat().replace("+00:00", "Z")
        date_compact = ts.date().isoformat().replace("-", "")
        used_nums = {
            int(b["bundle_id"].rsplit("-", 1)[1])
            for b in reg["bundles"]
            if b["bundle_id"].split("-")[1] == date_compact
        }
        bundle_id = draw_id("B", date_compact, used_nums, rng)
        initial_outcome = "bound" if state == "bound" else "pending"
        bundle_entry = {
            "bundle_id": bundle_id,
            "state": state,
            "ticket_ids": canonical_ids,
            "rationale": rationale.strip(),
            "source_ref": source_ref.strip(),
            "owner_actor": actor_ref.strip(),
            "scope_key": scope_key.strip(),
            "created_at": ts_str,
            "updated_at": ts_str,
            "member_outcomes": {tid: initial_outcome for tid in canonical_ids},
        }
        reg["bundles"].append(bundle_entry)
        for tid in canonical_ids:
            reg["ticket_pointers"][tid] = bundle_id
        reg["updated_at"] = ts_str

        if write_ticket_pointers:
            for m in metas:
                _write_ticket_bundle_pointer(
                    m["path"],
                    bundle_id=bundle_id,
                    peer_ids=[x for x in canonical_ids if x != m["ticket_id"]],
                    expected_sha256=m["preimage_sha256"],
                )

        self._save_atomic(reg, expected_sha256=reg_sha)
        return bundle_entry

    def record_member_outcome(
        self,
        bundle_id: str,
        *,
        ticket_id: str,
        outcome: str,
        actor_ref: str,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Record an individual member outcome; on partial failure only own bindings change."""
        canonical_tid = normalize_ticket_id(ticket_id)
        if outcome not in MEMBER_OUTCOMES:
            raise TicketBundleError(f"unsupported member outcome: {outcome!r}")
        reg, reg_sha = self.load()
        target_bundle = None
        for b in reg["bundles"]:
            if b["bundle_id"] == bundle_id:
                target_bundle = b
                break
        if target_bundle is None:
            raise TicketBundleError(f"unknown bundle_id: {bundle_id}")
        if target_bundle["owner_actor"] != actor_ref:
            raise TicketBundleError(
                f"actor {actor_ref!r} cannot mutate bundle owned by {target_bundle['owner_actor']!r}"
            )
        if canonical_tid not in target_bundle["member_outcomes"]:
            raise TicketBundleError(f"ticket {canonical_tid} is not a member of {bundle_id}")

        target_bundle["member_outcomes"][canonical_tid] = outcome
        vals = set(target_bundle["member_outcomes"].values())
        if vals == {"done"}:
            target_bundle["state"] = "completed"
        elif "failed" in vals or "released" in vals:
            target_bundle["state"] = "partial"
        elif "executing" in vals or "done" in vals:
            target_bundle["state"] = "executing"

        ts_str = (now or datetime.now(timezone.utc)).isoformat().replace("+00:00", "Z")
        target_bundle["updated_at"] = ts_str
        reg["updated_at"] = ts_str
        self._save_atomic(reg, expected_sha256=reg_sha)
        return dict(target_bundle)


def _write_ticket_bundle_pointer(
    ticket_path: Path,
    *,
    bundle_id: str,
    peer_ids: list[str],
    expected_sha256: str,
) -> None:
    raw = ticket_path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise PreimageConflictError(f"ticket file {ticket_path} changed before pointer write")
    text = raw.decode("utf-8")
    pointer_line = f"BUNDLE_ID:     {bundle_id} (Peers: {', '.join(peer_ids)})"
    if "BUNDLE_ID:" in text:
        text = re.sub(r"^BUNDLE_ID:.*$", pointer_line, text, count=1, flags=re.MULTILINE)
    else:
        text = text.rstrip("\r\n") + "\n" + pointer_line + "\n"
    ticket_path.write_text(text, encoding="utf-8")
