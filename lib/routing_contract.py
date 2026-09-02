"""Ticket routing schema v2: targets, execution bindings, leases and ledgers.

This module owns the ticket contract but deliberately owns no transport and no
model registry.  System identities are supplied as an evidence-bearing
snapshot; execution selectors are resolved by Clutch's public resolver (or an
injected compatible callable).  The only output intended for ``.SYNC`` is a
small, idempotent ``route_intent`` mapping.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence


ROUTING_SCHEMA = 2
DEFAULT_BINDING_TTL_DAYS = 7
TICKET_KINDS = frozenset({"normal", "transfer", "fork"})
TARGET_KINDS = frozenset({"any", "all", "grouped", "exact"})
BINDING_MODES = frozenset({"required", "preferred"})
LEDGER_STATES = frozenset({"pending", "claimed", "done", "blocked"})
_BASE_RE = re.compile(
    r"^(?P<stem>T-(?P<date>\d{8})-(?P<number>\d+)"
    r"(?:_(?P<slug>[A-Za-z0-9][\w-]*))?)(?P<suffixes>(?:\.[^.]+)*)\.txt$"
)
_FIELD_RE = re.compile(r"^(?P<name>[A-Z][A-Z0-9_]*):\s*(?P<value>.*)$")
_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
_SELECTOR_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")

logger = logging.getLogger(__name__)


class RoutingContractError(ValueError):
    """A routing contract is invalid or an operation must fail closed."""


class ClaimDeniedError(RoutingContractError):
    """The current host/runner may not acquire the ticket lease."""


class ReceiptConflictError(RoutingContractError):
    """A receipt is incomplete, contradictory or reuses a signature."""


def _utc(value: datetime | str | None = None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def utc_text(value: datetime | str | None = None) -> str:
    return _utc(value).isoformat().replace("+00:00", "Z")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _json_value(fields: Mapping[str, str], name: str, default: Any) -> Any:
    raw = fields.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RoutingContractError(f"{name} is not valid JSON") from exc


def parse_fields(text: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in text.splitlines():
        match = _FIELD_RE.match(line.strip())
        if match:
            fields[match.group("name")] = match.group("value").strip()
    return fields


def update_fields(text: str, updates: Mapping[str, Any], *, log: str | None = None) -> str:
    """Replace unique scalar fields and insert missing fields before HISTORY."""
    rendered = {
        key: (_json(value) if isinstance(value, (dict, list, tuple)) else str(value))
        for key, value in updates.items()
    }
    lines = text.splitlines()
    found: set[str] = set()
    for index, line in enumerate(lines):
        match = _FIELD_RE.match(line.strip())
        if match and match.group("name") in rendered:
            name = match.group("name")
            if name in found:
                raise RoutingContractError(f"duplicate metadata field: {name}")
            lines[index] = f"{name}: {rendered[name]}"
            found.add(name)
    missing = [f"{name}: {rendered[name]}" for name in rendered if name not in found]
    if missing:
        insert_at = next(
            (i for i, line in enumerate(lines) if "HISTORY / LOG" in line or "VERLAUF / LOG" in line),
            len(lines),
        )
        lines[insert_at:insert_at] = missing + [""]
    if log:
        insert_at = next(
            (i + 2 for i, line in enumerate(lines) if "HISTORY / LOG" in line or "VERLAUF / LOG" in line),
            len(lines),
        )
        lines.insert(min(insert_at, len(lines)), log)
    return "\n".join(lines).rstrip("\n") + "\n"


@dataclass(frozen=True)
class TicketName:
    filename: str
    stem: str
    date: str
    number: str
    slug: str | None = None
    target: str | None = None
    via: str | None = None
    claim: str | None = None
    legacy_claim: str | None = None

    @property
    def is_v2(self) -> bool:
        return any(value is not None for value in (self.target, self.via, self.claim))

    def render(self, *, target: str | None = None, via: str | None = None,
               claim: str | None = None) -> str:
        target = self.target if target is None else target
        via = self.via if via is None else via
        claim = self.claim if claim is None else claim
        suffix = f".to-{target}" if target else ""
        suffix += f".via-{via}" if via else ""
        suffix += f".claim-{claim}" if claim else ""
        return f"{self.stem}{suffix}.txt"

    def unclaimed(self) -> str:
        if self.is_v2:
            return self.render(claim=None) if self.claim is None else _render_name(
                self.stem, self.target, self.via, None
            )
        if self.legacy_claim:
            return f"{self.stem}.txt"
        return self.filename


def _render_name(stem: str, target: str | None, via: str | None, claim: str | None) -> str:
    suffix = f".to-{target}" if target else ""
    suffix += f".via-{via}" if via else ""
    suffix += f".claim-{claim}" if claim else ""
    return f"{stem}{suffix}.txt"


def parse_ticket_name(filename: str) -> TicketName:
    """Parse v2 axes without reinterpreting any legacy ``.<HOST>`` claim."""
    match = _BASE_RE.fullmatch(filename)
    if match is None:
        raise RoutingContractError(f"invalid ticket filename: {filename}")
    tokens = [token for token in match.group("suffixes").split(".") if token]
    if not tokens:
        return TicketName(filename, match.group("stem"), match.group("date"),
                          match.group("number"), match.group("slug"))
    if not any(token.startswith(("to-", "via-", "claim-")) for token in tokens):
        # Every old suffix, including LAPTOP-WORKSTATION-LG, remains one
        # opaque legacy claim.  It is never split into hosts or targets.
        return TicketName(filename, match.group("stem"), match.group("date"),
                          match.group("number"), match.group("slug"),
                          legacy_claim=".".join(tokens))

    target = via = claim = None
    index = 0
    if index < len(tokens) and tokens[index].startswith("to-"):
        target = tokens[index][3:]
        index += 1
        if not _TOKEN_RE.fullmatch(target):
            raise RoutingContractError("invalid target segment")
    if index < len(tokens) and tokens[index].startswith("via-"):
        via_parts = [tokens[index][4:]]
        index += 1
        while index < len(tokens) and not tokens[index].startswith("claim-"):
            if tokens[index].startswith(("to-", "via-")):
                raise RoutingContractError("duplicate or out-of-order routing segment")
            via_parts.append(tokens[index])
            index += 1
        via = ".".join(via_parts)
        if not _SELECTOR_RE.fullmatch(via):
            raise RoutingContractError("invalid via segment")
    if index < len(tokens) and tokens[index].startswith("claim-"):
        claim = tokens[index][6:]
        index += 1
        if not _TOKEN_RE.fullmatch(claim):
            raise RoutingContractError("invalid claim segment")
    if index != len(tokens) or not any((target, via, claim)):
        raise RoutingContractError("unknown or out-of-order routing segment")
    return TicketName(filename, match.group("stem"), match.group("date"),
                      match.group("number"), match.group("slug"), target, via, claim)


def _resolution_dict(result: Any, requested: str) -> dict[str, Any]:
    data = result.to_dict() if hasattr(result, "to_dict") else dict(result)
    return {
        "requested_selector": requested,
        "canonical_selector": data.get("canonical_selector"),
        "selector_type": data.get("selector_type", "unresolved"),
        "model_selection": data.get("model_selection"),
        "resolved": bool(data.get("resolved")),
        "claimable": bool(data.get("claimable")),
        "runner": data.get("runner"),
        "provider": data.get("provider"),
        "registry_name": data.get("registry_name"),
        "model_id": data.get("model_id"),
        "allowed_runners": list(data.get("allowed_runners", [])),
        "eligible_models": list(data.get("eligible_models", [])),
        "availability": data.get("availability", {}),
        "reason": data.get("reason"),
        "registry_fingerprint": data.get("registry_fingerprint"),
        "resolved_at": data.get("resolved_at"),
    }


def resolve_execution(selector: str, *, runner: str | None = None,
                      resolver: Callable[..., Any] | None = None) -> dict[str, Any]:
    """Consume Clutch's public resolver and preserve loud outage evidence."""
    def unavailable(reason: str) -> dict[str, Any]:
        return {
            "requested_selector": selector,
            "canonical_selector": None,
            "selector_type": "unresolved",
            "model_selection": None,
            "resolved": False,
            "claimable": False,
            "runner": runner,
            "provider": None,
            "registry_name": None,
            "model_id": None,
            "allowed_runners": [],
            "eligible_models": [],
            "availability": {},
            "reason": reason,
            "registry_fingerprint": None,
            "resolved_at": utc_text(),
        }

    if resolver is None:
        try:
            from clutch import resolve_execution_selector as resolver  # type: ignore
        except (ImportError, OSError) as exc:
            reason = f"resolver-import-error:{type(exc).__name__}"
            logger.error(
                "Clutch execution resolver import failed for selector %r: %s: %s",
                selector, type(exc).__name__, exc, exc_info=True,
            )
            return unavailable(reason)
    try:
        result = _resolution_dict(resolver(selector, runner=runner), selector)
    except Exception as exc:  # persist only the type; keep the cause in the local error log
        reason = f"registry-error:{type(exc).__name__}"
        logger.error(
            "Clutch execution resolver failed for selector %r: %s: %s",
            selector, type(exc).__name__, exc, exc_info=True,
        )
        return unavailable(reason)
    if not result.get("resolved"):
        logger.error(
            "Clutch execution binding is unresolved for selector %r: %s",
            selector, result.get("reason") or "reason-not-reported",
        )
    elif runner and not result.get("claimable"):
        logger.error(
            "Clutch execution binding for selector %r is not claimable by runner %r: %s",
            selector, runner, result.get("reason") or "reason-not-reported",
        )
    return result


def _registry_systems(snapshot: Mapping[str, Any]) -> tuple[dict[str, dict[str, Any]], str, str]:
    source = str(snapshot.get("source", "")).strip()
    checked_at = str(snapshot.get("checked_at", "")).strip()
    raw = snapshot.get("systems", {})
    if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
        systems = {str(item.get("id", "")): dict(item) for item in raw}
    elif isinstance(raw, Mapping):
        systems = {str(key): dict(value) for key, value in raw.items()}
    else:
        systems = {}
    if not source or not checked_at or not systems or any(not key for key in systems):
        raise RoutingContractError("system registry snapshot needs source, checked_at and systems")
    lowered = [key.casefold() for key in systems]
    if len(lowered) != len(set(lowered)):
        raise RoutingContractError("duplicate system identifiers in registry snapshot")
    return systems, source, checked_at


def resolve_targets(target_kind: str, *, target: str | None = None,
                    targets: Sequence[str] | None = None,
                    registry_snapshot: Mapping[str, Any]) -> dict[str, Any]:
    if target_kind not in TARGET_KINDS:
        raise RoutingContractError(f"unknown target kind: {target_kind}")
    systems, source, checked_at = _registry_systems(registry_snapshot)
    canonical = {key.casefold(): key for key in systems}
    if target_kind == "any":
        selected: list[str] = []
    elif target_kind == "all":
        selected = sorted(key for key, value in systems.items() if value.get("active", True))
    elif target_kind == "exact":
        if not target or target.casefold() not in canonical:
            raise RoutingContractError(f"unknown exact target: {target}")
        selected = [canonical[target.casefold()]]
    else:
        requested = list(targets or [])
        if not requested:
            raise RoutingContractError("grouped target requires an explicit target set")
        unknown = [item for item in requested if item.casefold() not in canonical]
        if unknown:
            raise RoutingContractError(f"unknown grouped targets: {', '.join(unknown)}")
        selected = sorted({canonical[item.casefold()] for item in requested})
    if target_kind != "any" and not selected:
        raise RoutingContractError("target snapshot resolved to an empty set")
    safe_keys = ("slot", "hostname", "host", "active", "capabilities", "runners")
    details = {
        key: {field: systems[key][field] for field in safe_keys if field in systems[key]}
        for key in selected
    }
    return {
        "kind": target_kind,
        "systems": selected,
        "details": details,
        "source": source,
        "checked_at": checked_at,
        "fingerprint": "sha256:" + hashlib.sha256(_json({
            "systems": details,
            "source": source,
            "checked_at": checked_at,
        }).encode("utf-8")).hexdigest(),
    }


def normalize_alias(ticket_stem: str, aliases: Sequence[str], *,
                    registry_snapshot: Mapping[str, Any],
                    resolver: Callable[..., Any] | None = None) -> tuple[str, dict[str, Any] | None]:
    """Normalize user-facing suffix aliases using registry evidence only."""
    if not aliases:
        return f"{ticket_stem}.txt", None
    systems, _source, _checked = _registry_systems(registry_snapshot)
    system_names = {name.casefold(): name for name in systems}
    parts = [part.lstrip(".") for part in aliases if part.lstrip(".")]
    first = parts[0]
    target: str | None = None
    via_parts = parts
    if first in {"all", "grouped"}:
        target = first
        via_parts = parts[1:]
    elif first.casefold() in system_names:
        target = system_names[first.casefold()]
        via_parts = parts[1:]
    via = None
    note = None
    if via_parts:
        requested = ".".join(via_parts)
        note = resolve_execution(requested, resolver=resolver)
        via = note.get("canonical_selector") or requested
    return _render_name(ticket_stem, target, via, None), note


@dataclass(frozen=True)
class ContractView:
    path: Path
    name: TicketName
    text: str
    fields: Mapping[str, str]
    ticket_id: str
    ticket_kind: str
    target_kind: str
    target_systems: tuple[str, ...]
    ledger: tuple[Mapping[str, Any], ...]
    execution_matrix: Mapping[str, Any]
    resolution: Mapping[str, Any]
    binding_mode: str
    binding_expires_at: str | None

    @property
    def claimed_host(self) -> str | None:
        return self.name.claim or self.fields.get("CLAIMED_BY_HOST") or None


def load_contract(path: Path | str) -> ContractView:
    path = Path(path)
    text = path.read_text(encoding="utf-8")
    fields = parse_fields(text)
    name = parse_ticket_name(path.name)
    schema = fields.get("ROUTING_SCHEMA")
    if schema != str(ROUTING_SCHEMA):
        raise RoutingContractError("ticket is not routing schema 2")
    ledger = _json_value(fields, "SYSTEM_LEDGER", [])
    # T-20260902-792359826 Nebenbefund: a row with a non-object "receipt"
    # (e.g. a bare string) used to pass this check and crash contract_errors()
    # downstream several fields later ('str' object has no attribute 'get').
    # Reject it here, at the single choke point every caller (contract_errors,
    # record_receipt, ...) already goes through -- reported, not thrown.
    if not isinstance(ledger, list) or not all(
        isinstance(row, dict) and (row.get("receipt") is None or isinstance(row["receipt"], dict))
        for row in ledger
    ):
        raise RoutingContractError("SYSTEM_LEDGER must be a JSON row list with object receipts")
    matrix = _json_value(fields, "EXECUTION_MATRIX", {})
    resolution = _json_value(fields, "RESOLUTION_NOTE", {})
    return ContractView(
        path=path,
        name=name,
        text=text,
        fields=fields,
        ticket_id=fields.get("ID", name.stem),
        ticket_kind=fields.get("TICKET_KIND", "normal"),
        target_kind=fields.get("TARGET_KIND", "any"),
        target_systems=tuple(_json_value(fields, "TARGET_SYSTEMS", [])),
        ledger=tuple(ledger),
        execution_matrix=matrix if isinstance(matrix, dict) else {},
        resolution=resolution if isinstance(resolution, dict) else {},
        binding_mode=fields.get("BINDING_MODE", "required"),
        binding_expires_at=fields.get("BINDING_EXPIRES_AT") or None,
    )


def contract_errors(path: Path | str, *, now: datetime | str | None = None) -> list[str]:
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
        fields = parse_fields(text)
        name = parse_ticket_name(path.name)
    except (OSError, UnicodeError, RoutingContractError) as exc:
        return [str(exc)]
    schema = fields.get("ROUTING_SCHEMA")
    if schema != str(ROUTING_SCHEMA):
        return ["v2 filename requires ROUTING_SCHEMA: 2"] if name.is_v2 else []
    try:
        view = load_contract(path)
    except RoutingContractError as exc:
        return [str(exc)]
    errors: list[str] = []
    if view.ticket_kind not in TICKET_KINDS:
        errors.append("invalid TICKET_KIND")
    if view.target_kind not in TARGET_KINDS:
        errors.append("invalid TARGET_KIND")
    expected_target = None if view.target_kind == "any" else (
        view.target_kind if view.target_kind in {"all", "grouped"} else
        (view.target_systems[0] if len(view.target_systems) == 1 else None)
    )
    if name.target != expected_target:
        errors.append("filename target and TARGET_KIND/TARGET_SYSTEMS disagree")
    selector = view.fields.get("MODEL_SELECTOR") or None
    if name.via != selector:
        errors.append("filename via selector and MODEL_SELECTOR disagree")
    metadata_claim = view.fields.get("CLAIMED_BY_HOST") or None
    if name.claim != metadata_claim:
        errors.append("filename claim and CLAIMED_BY_HOST disagree")
    if view.binding_mode not in BINDING_MODES:
        errors.append("invalid BINDING_MODE")
    if view.ticket_kind in {"transfer", "fork"}:
        for field in ("PRIMARY_TICKET", "ORIGINAL_OWNER", "RECEIPT_TO"):
            if not view.fields.get(field):
                errors.append(f"missing {field}")
        if view.fields.get("PRIMARY_TICKET") and not re.fullmatch(
            r"T-\d{8}-\d+", view.fields["PRIMARY_TICKET"]
        ):
            errors.append("invalid PRIMARY_TICKET reference")
    systems = list(view.target_systems)
    if view.target_kind != "any" and (not systems or len(systems) != len(set(systems))):
        errors.append("TARGET_SYSTEMS must be non-empty and unique")
    ledger_systems = [str(row.get("system", "")) for row in view.ledger]
    if view.target_kind == "any":
        # ``any`` starts without a fixed target. The first successful claim
        # materialises exactly one execution row, so a systemless
        # ``.via-...`` contract can use the same receipt/completion path as a
        # targeted contract without pretending that a target was known at
        # creation time.
        if len(ledger_systems) > 1 or any(not system for system in ledger_systems):
            errors.append("TARGET_KIND any permits at most one materialized ledger row")
    elif ledger_systems != systems:
        errors.append("SYSTEM_LEDGER must contain exactly one ordered row per target")
    details = _json_value(view.fields, "TARGET_SYSTEM_DETAILS", {})
    if not isinstance(details, dict) or set(details) != set(systems):
        errors.append("TARGET_SYSTEM_DETAILS must match the fixed target snapshot")
    for field in (
        "TARGET_SNAPSHOT_AT", "TARGET_REGISTRY_CHECKED_AT",
        "TARGET_SNAPSHOT_SOURCE", "TARGET_SNAPSHOT_FINGERPRINT",
    ):
        if not view.fields.get(field):
            errors.append(f"missing {field}")
    for field in ("TARGET_SNAPSHOT_AT", "TARGET_REGISTRY_CHECKED_AT"):
        if view.fields.get(field):
            try:
                _utc(view.fields[field])
            except (TypeError, ValueError):
                errors.append(f"invalid {field}")
    signatures = [row.get("receipt", {}).get("signature") for row in view.ledger if row.get("receipt")]
    if any(not signature for signature in signatures) or len(signatures) != len(set(signatures)):
        errors.append("receipt signatures are missing or duplicated")
    if any(row.get("status") not in LEDGER_STATES for row in view.ledger):
        errors.append("invalid SYSTEM_LEDGER state")
    claimed_rows = [row for row in view.ledger if row.get("status") == "claimed"]
    if name.claim:
        claimed_row = next((row for row in claimed_rows if row.get("system") == name.claim), None)
        if view.target_kind != "any" and name.claim not in systems:
            errors.append("claimed host is outside the target snapshot")
        if claimed_row is None:
            errors.append("claimed host lacks a claimed ledger row")
    elif claimed_rows:
        errors.append("ledger contains a claimed row without a filename claim")
    for row in view.ledger:
        if row.get("status") in {"done", "blocked"}:
            receipt = row.get("receipt") or {}
            missing = [field for field in _RECEIPT_FIELDS if not receipt.get(field)]
            if missing:
                errors.append(f"{row.get('system')} receipt lacks actual execution evidence")
    if name.via == "mixed" and set(view.execution_matrix) != set(systems):
        errors.append("via-mixed requires one EXECUTION_MATRIX entry per target")
    if name.via and not view.resolution.get("resolved"):
        reason = view.resolution.get("reason") or "reason-not-recorded"
        errors.append(f"execution binding is unresolved ({reason})")
    if name.via:
        if not view.resolution.get("registry_fingerprint"):
            errors.append("execution resolution lacks registry fingerprint")
        if not view.resolution.get("resolved_at"):
            errors.append("execution resolution lacks resolved_at")
    if path.parent.name == "SOLVED" and not completion_ready(view):
        errors.append("SOLVED is forbidden before every required ledger row is done")
    if (view.binding_expires_at and view.binding_expires_at != "never" and name.via
            and not name.claim
            and _utc(view.binding_expires_at) <= _utc(now)):
        errors.append("expired binding still has a physical via segment")
    return errors


def completion_ready(view: ContractView) -> bool:
    return bool(view.ledger) and all(row.get("status") == "done" for row in view.ledger)


def binding_expires(created_at: datetime | str, ttl: int | str | None = None) -> str:
    if ttl == "never":
        return "never"
    days = DEFAULT_BINDING_TTL_DAYS if ttl is None else int(ttl)
    if days <= 0:
        raise RoutingContractError("binding TTL days must be positive or 'never'")
    return utc_text(_utc(created_at) + timedelta(days=days))


def contract_metadata(*, ticket_id: str, ticket_kind: str, target_snapshot: Mapping[str, Any],
                      primary_ticket: str, original_owner: str, receipt_to: str,
                      via: str | None = None, binding_mode: str = "required",
                      binding_ttl: int | str | None = None,
                      resolver: Callable[..., Any] | None = None,
                      execution_matrix: Mapping[str, Any] | None = None,
                      created_at: datetime | str | None = None) -> dict[str, Any]:
    if ticket_kind not in TICKET_KINDS:
        raise RoutingContractError(f"unknown ticket kind: {ticket_kind}")
    if binding_mode not in BINDING_MODES:
        raise RoutingContractError(f"unknown binding mode: {binding_mode}")
    created = utc_text(created_at)
    systems = list(target_snapshot["systems"])
    kind = str(target_snapshot["kind"])
    matrix = {key: dict(value) for key, value in (execution_matrix or {}).items()}
    if via == "mixed" and set(matrix) != set(systems):
        raise RoutingContractError("via-mixed requires one matrix entry per target")
    resolution: dict[str, Any] = {}
    selector = None
    state = "unbound"
    expires_at = ""
    executor = "any"
    model_selection = "self"
    if via:
        if via == "mixed":
            for system, row in matrix.items():
                requested = row.get("selector")
                if not requested:
                    raise RoutingContractError(f"execution matrix entry lacks selector: {system}")
                note = resolve_execution(requested, resolver=resolver)
                row["selector"] = note.get("canonical_selector") or requested
                row["resolution"] = note
            resolution = {
                "requested_selector": "mixed",
                "canonical_selector": "mixed",
                "selector_type": "matrix",
                "model_selection": "mixed",
                "resolved": all(row["resolution"].get("resolved") for row in matrix.values()),
                "claimable": all(row["resolution"].get("claimable") for row in matrix.values()),
                "registry_fingerprint": sorted({
                    row["resolution"].get("registry_fingerprint") for row in matrix.values()
                    if row["resolution"].get("registry_fingerprint")
                }),
                "resolved_at": created,
            }
        else:
            resolution = resolve_execution(via, resolver=resolver)
        selector = resolution.get("canonical_selector") or via
        state = "active" if resolution.get("resolved") else "unresolved"
        expires_at = binding_expires(created, binding_ttl)
        executor = "mixed" if via == "mixed" else resolution.get("runner") or "any"
        model_selection = resolution.get("model_selection") or resolution.get("selector_type") or "unresolved"
    return {
        "ROUTING_SCHEMA": ROUTING_SCHEMA,
        "TICKET_KIND": ticket_kind,
        "TARGET_KIND": kind,
        "TARGET_SYSTEMS": systems,
        "TARGET_SYSTEM_DETAILS": target_snapshot.get("details", {}),
        "TARGET_SNAPSHOT_AT": created,
        "TARGET_REGISTRY_CHECKED_AT": target_snapshot["checked_at"],
        "TARGET_SNAPSHOT_SOURCE": target_snapshot["source"],
        "TARGET_SNAPSHOT_FINGERPRINT": target_snapshot["fingerprint"],
        "PRIMARY_TICKET": primary_ticket,
        "ORIGINAL_OWNER": original_owner,
        "RECEIPT_TO": receipt_to,
        "EXECUTOR": executor,
        "MODEL_SELECTION": model_selection,
        "MODEL_SELECTOR": selector or "",
        "BINDING_MODE": binding_mode,
        "BINDING_EXPIRES_AT": expires_at,
        "BINDING_STATE": state,
        "RESOLUTION_NOTE": resolution,
        "EXECUTION_MATRIX": matrix,
        "CLAIMED_BY_HOST": "",
        "CLAIMED_BY_ACTOR": "",
        "CLAIMED_AT": "",
        "CLAIM_LEASE_UNTIL": "",
        "SYSTEM_LEDGER": [
            {"system": system, "status": "pending", "actor": None, "claimed_at": None,
             "receipt": None, "variant": matrix.get(system, {}).get("variant")}
            for system in systems
        ],
    }


def canonical_contract_name(ticket_id: str, metadata: Mapping[str, Any], *, claim: str | None = None) -> str:
    kind = metadata["TARGET_KIND"]
    systems = list(metadata.get("TARGET_SYSTEMS", []))
    target = None if kind == "any" else kind if kind in {"all", "grouped"} else systems[0]
    via = metadata.get("MODEL_SELECTOR") or None
    return _render_name(ticket_id, target, via, claim)


def _atomic_rewrite(path: Path, text: str) -> None:
    fd, raw_tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp = Path(raw_tmp)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def _exclusive_rewrite_move(source: Path, target: Path, text: str) -> Path:
    """Create transformed target exclusively, verify, then remove source."""
    original = source.read_bytes()
    data = text.encode("utf-8")
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0)
    try:
        fd = os.open(str(target), flags, 0o644)
    except FileExistsError as exc:
        raise RoutingContractError(f"normalization target already exists: {target}") from exc
    complete = False
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if target.read_bytes() != data:
            raise RoutingContractError("normalization readback failed")
        if source.read_bytes() != original:
            raise RoutingContractError("source changed during normalization")
        complete = True
    finally:
        if not complete and target.exists():
            target.unlink()
    source.unlink()
    return target


def _ledger_rows(view: ContractView) -> list[dict[str, Any]]:
    return [dict(row) for row in view.ledger]


def _binding_for_host(view: ContractView, host: str) -> tuple[str | None, Mapping[str, Any]]:
    if view.fields.get("MODEL_SELECTOR") == "mixed":
        row = view.execution_matrix.get(host, {})
        return row.get("selector"), row.get("resolution", {})
    return view.fields.get("MODEL_SELECTOR") or None, view.resolution


def _binding_claimable(view: ContractView, host: str, runner: str | None,
                       resolver: Callable[..., Any] | None) -> tuple[bool, dict[str, Any]]:
    selector, previous = _binding_for_host(view, host)
    if not selector:
        return True, dict(previous)
    note = resolve_execution(selector, runner=runner, resolver=resolver)
    if view.binding_mode == "preferred":
        # The preferred selector must itself exist, but a different actual
        # runner may take over when the receipt records a fallback reason.
        baseline = resolve_execution(selector, resolver=resolver)
        return bool(baseline.get("resolved")), baseline
    if not note.get("resolved"):
        return False, note
    allowed = note.get("allowed_runners", [])
    if runner and allowed and runner not in allowed:
        return False, note
    if view.binding_mode == "required" and not note.get("claimable"):
        return False, note
    return True, note


def claim_contract(path: Path | str, *, host: str, actor: str, runner: str | None = None,
                   resolver: Callable[..., Any] | None = None,
                   now: datetime | str | None = None, lease_seconds: int = 3600) -> Path:
    """Acquire a v2 write lease after target, binding and ledger checks."""
    try:
        from .ticket_mover import move_ticket
    except ImportError:
        from ticket_mover import move_ticket

    path = Path(path)
    view = load_contract(path)
    validation = [
        error for error in contract_errors(path, now=now)
        if error != "expired binding still has a physical via segment"
    ]
    if validation:
        raise ClaimDeniedError("invalid routing contract: " + "; ".join(validation))
    if view.name.claim or view.fields.get("CLAIMED_BY_HOST"):
        raise ClaimDeniedError("ticket already has an active claim")
    rows = _ledger_rows(view)
    row = next((item for item in rows if item.get("system") == host), None)
    if view.target_kind != "any" and row is None:
        raise ClaimDeniedError("host is outside the target snapshot")
    if view.target_kind == "any" and row is None:
        if not rows:
            row = {
                "system": host, "status": "pending", "actor": None,
                "claimed_at": None, "receipt": None, "variant": None,
            }
            rows.append(row)
        elif len(rows) == 1 and rows[0].get("status") == "pending" and not rows[0].get("receipt"):
            # A released, evidence-free attempt may be picked up elsewhere;
            # ``any`` never becomes a hidden persistent target binding.
            rows[0].update(system=host, actor=None, claimed_at=None)
            row = rows[0]
        else:
            raise ClaimDeniedError("any-target contract has no open execution row")
    if row is not None and row.get("status") != "pending":
        raise ClaimDeniedError(f"target ledger state is {row.get('status')}, not pending")

    instant = _utc(now)
    expired = bool(view.binding_expires_at and view.binding_expires_at != "never"
                   and _utc(view.binding_expires_at) <= instant)
    active_claim_name = _render_name(view.name.stem, view.name.target, view.name.via, host)
    claimed = move_ticket(path, path.parent, new_name=active_claim_name)
    view = load_contract(claimed)

    selector = view.fields.get("MODEL_SELECTOR") or None
    former: list[Any] = _json_value(view.fields, "FORMER_BINDINGS", [])
    if expired:
        former.append({"selector": selector, "expired_at": utc_text(instant),
                       "resolution": dict(view.resolution)})
        selector = None
        note: dict[str, Any] = {}
        allowed = True
    else:
        allowed, note = _binding_claimable(view, host, runner, resolver)
    if not allowed:
        # Keep the acquired claim fail-closed only long enough to restore the
        # original name. No contract content was changed yet.
        move_ticket(claimed, claimed.parent, new_name=view.name.unclaimed())
        raise ClaimDeniedError(note.get("reason") or "execution binding is not claimable")

    for item in rows:
        if item.get("system") == host:
            item.update(status="claimed", actor=actor, claimed_at=utc_text(instant))
    lease_until = utc_text(instant + timedelta(seconds=lease_seconds))
    updates: dict[str, Any] = {
        "MODEL_SELECTOR": selector or "",
        "BINDING_STATE": "expired-unbound" if expired else ("active" if selector else "unbound"),
        "RESOLUTION_NOTE": note,
        "FORMER_BINDINGS": former,
        "CLAIMED_BY_HOST": host,
        "CLAIMED_BY_ACTOR": actor,
        "CLAIMED_AT": utc_text(instant),
        "CLAIM_LEASE_UNTIL": lease_until,
        "SYSTEM_LEDGER": rows,
    }
    log = f"{utc_text(instant)}  Claim acquired by {actor} on {host}."
    _atomic_rewrite(claimed, update_fields(view.text, updates, log=log))
    if expired:
        final_name = _render_name(view.name.stem, view.name.target, None, host)
        claimed = move_ticket(claimed, claimed.parent, new_name=final_name)
    return claimed


def release_contract(path: Path | str, *, host: str,
                     now: datetime | str | None = None) -> Path:
    """Release only the temporary claim; persistent target/via axes survive."""
    try:
        from .ticket_mover import move_ticket
    except ImportError:
        from ticket_mover import move_ticket

    path = Path(path)
    view = load_contract(path)
    if view.name.claim != host or view.fields.get("CLAIMED_BY_HOST") != host:
        raise ClaimDeniedError("only the current claimed host may release")
    rows = _ledger_rows(view)
    for row in rows:
        if row.get("system") == host and row.get("status") == "claimed":
            row.update(status="pending", actor=None, claimed_at=None)
    updates = {
        "CLAIMED_BY_HOST": "", "CLAIMED_BY_ACTOR": "", "CLAIMED_AT": "",
        "CLAIM_LEASE_UNTIL": "", "SYSTEM_LEDGER": rows,
    }
    _atomic_rewrite(path, update_fields(
        view.text, updates, log=f"{utc_text(now)}  Claim released by {host}."
    ))
    refreshed = load_contract(path)
    return move_ticket(path, path.parent, new_name=_render_name(
        refreshed.name.stem, refreshed.name.target,
        refreshed.fields.get("MODEL_SELECTOR") or None, None
    ))


def recover_expired_claim(path: Path | str, *, now: datetime | str | None = None) -> Path:
    """Recover a crashed claim only after its absolute lease has expired."""
    view = load_contract(path)
    lease = view.fields.get("CLAIM_LEASE_UNTIL")
    if not view.name.claim or not lease or _utc(lease) > _utc(now):
        raise ClaimDeniedError("claim lease is not expired")
    return release_contract(path, host=view.name.claim, now=now)


def normalize_expired_binding(path: Path | str, *,
                              now: datetime | str | None = None) -> Path:
    """Proactively remove an expired via axis without changing any other axis."""
    path = Path(path)
    view = load_contract(path)
    if view.name.claim or view.fields.get("CLAIMED_BY_HOST"):
        raise ClaimDeniedError("an active claim keeps its binding stable")
    expiry = view.binding_expires_at
    if not view.name.via or not expiry or expiry == "never" or _utc(expiry) > _utc(now):
        return path
    former: list[Any] = _json_value(view.fields, "FORMER_BINDINGS", [])
    former.append({
        "selector": view.fields.get("MODEL_SELECTOR"),
        "expired_at": utc_text(now),
        "resolution": dict(view.resolution),
    })
    text = update_fields(view.text, {
        "MODEL_SELECTOR": "",
        "BINDING_STATE": "expired-unbound",
        "RESOLUTION_NOTE": {},
        "FORMER_BINDINGS": former,
    }, log=f"{utc_text(now)}  Expired execution binding normalized proactively.")
    target = path.with_name(_render_name(view.name.stem, view.name.target, None, None))
    return _exclusive_rewrite_move(path, target, text)


_RECEIPT_FIELDS = (
    "signature", "status", "executed_by", "actual_provider", "actual_model",
    "occurred_at", "evidence",
)


def record_receipt(path: Path | str, *, host: str, receipt: Mapping[str, Any],
                   now: datetime | str | None = None) -> bool:
    """Idempotently reconcile one transport receipt into the domain ledger."""
    path = Path(path)
    view = load_contract(path)
    if view.name.claim != host or view.fields.get("CLAIMED_BY_HOST") != host:
        raise ClaimDeniedError("receipt reconciliation requires the matching claim")
    missing = [field for field in _RECEIPT_FIELDS if not receipt.get(field)]
    if missing:
        raise ReceiptConflictError(f"receipt is missing: {', '.join(missing)}")
    if receipt["status"] not in {"done", "blocked"}:
        raise ReceiptConflictError("transport state is not a domain ledger state")
    rows = _ledger_rows(view)
    signature = receipt["signature"]
    for row in rows:
        existing = row.get("receipt")
        if existing and existing.get("signature") == signature:
            if row.get("system") == host and dict(existing) == dict(receipt):
                return False
            raise ReceiptConflictError("receipt signature already belongs to another payload")
    row = next((item for item in rows if item.get("system") == host), None)
    if row is None or row.get("status") != "claimed":
        raise ReceiptConflictError("target ledger row is not claimed")
    selector, note = _binding_for_host(view, host)
    if selector and view.binding_mode == "required":
        expected_runner = note.get("runner")
        allowed = note.get("allowed_runners", [])
        if expected_runner and receipt["executed_by"] != expected_runner:
            raise ReceiptConflictError("required runner does not match actual execution")
        if allowed and receipt["executed_by"] not in allowed:
            raise ReceiptConflictError("actual runner is outside the required binding")
        expected_models = {value for value in (note.get("model_id"), note.get("registry_name")) if value}
        if expected_models and receipt["actual_model"] not in expected_models:
            raise ReceiptConflictError("required exact model does not match actual execution")
    if selector and view.binding_mode == "preferred" and not receipt.get("fallback_reason"):
        expected = note.get("runner")
        if expected and receipt["executed_by"] != expected:
            raise ReceiptConflictError("preferred fallback requires a reason")
    row.update(status=receipt["status"], actor=receipt["executed_by"], receipt=dict(receipt))
    _atomic_rewrite(path, update_fields(
        view.text, {"SYSTEM_LEDGER": rows},
        log=f"{utc_text(now)}  Receipt {signature} reconciled for {host}."
    ))
    return True


def complete_contract(path: Path | str, *, host: str, solved_dir: Path | str,
                      now: datetime | str | None = None) -> Path:
    """Move to SOLVED only under the last claim and with every receipt done."""
    try:
        from .ticket_mover import move_ticket
    except ImportError:
        from ticket_mover import move_ticket

    path = Path(path)
    view = load_contract(path)
    if view.name.claim != host or view.fields.get("CLAIMED_BY_HOST") != host:
        raise ClaimDeniedError("completion requires the matching active claim")
    if not completion_ready(view):
        raise ClaimDeniedError("not every required system ledger row is done")
    text = update_fields(view.text, {
        "STATUS": "SOLVED",
        "CLAIMED_BY_HOST": "", "CLAIMED_BY_ACTOR": "", "CLAIMED_AT": "",
        "CLAIM_LEASE_UNTIL": "",
    }, log=f"{utc_text(now)}  Contract completed by final target {host}.")
    _atomic_rewrite(path, text)
    refreshed = load_contract(path)
    target_name = _render_name(refreshed.name.stem, refreshed.name.target,
                               refreshed.fields.get("MODEL_SELECTOR") or None, None)
    return move_ticket(path, solved_dir, new_name=target_name)


def refresh_resolution(path: Path | str, *, resolver: Callable[..., Any] | None = None,
                       now: datetime | str | None = None) -> bool:
    """Retry an unresolved Clutch lookup without changing target or claim axes."""
    path = Path(path)
    view = load_contract(path)
    if view.name.claim:
        raise ClaimDeniedError("resolution refresh is forbidden during an active claim")
    selector = view.fields.get("MODEL_SELECTOR")
    if not selector:
        return False
    note = resolve_execution(selector, resolver=resolver)
    state = "active" if note.get("resolved") else "unresolved"
    changed = _json(note) != _json(view.resolution) or state != view.fields.get("BINDING_STATE")
    if changed:
        _atomic_rewrite(path, update_fields(
            view.text, {"RESOLUTION_NOTE": note, "BINDING_STATE": state},
            log=f"{utc_text(now)}  Clutch resolution refreshed: {state}."
        ))
    return changed


def build_route_intent(view: ContractView) -> dict[str, Any]:
    """Return only the ticket-master-to-transport boundary payload."""
    snapshot = {
        "kind": view.target_kind,
        "systems": list(view.target_systems),
        "at": view.fields.get("TARGET_SNAPSHOT_AT"),
        "source": view.fields.get("TARGET_SNAPSHOT_SOURCE"),
        "fingerprint": view.fields.get("TARGET_SNAPSHOT_FINGERPRINT"),
    }
    stable = {
        "ticket_id": view.ticket_id,
        "target_snapshot": snapshot,
        "receipt_to": view.fields.get("RECEIPT_TO"),
    }
    return {
        "route_intent": "ellmos.ticket.route-intent.v1",
        **stable,
        "idempotency_key": "sha256:" + hashlib.sha256(_json(stable).encode("utf-8")).hexdigest(),
    }
