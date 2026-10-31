"""Unified Log and Report Registry extension for Ticket-Master (T-20261028-764754572).

Extends the existing pointer-only log registry (`ellmos.ticket.log-registry.v1`)
with report locations (`log-report-registry.json` / `ellmos.ticket.log-report-registry.v1`)
as a controlled migration/alias without creating a competing second registry.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from .messages_line import (
        LOG_REGISTRY_SCHEMA,
        MessageLineError,
        _require_ref,
        _require_timestamp,
        validate_log_registry,
    )
except ImportError:
    from messages_line import (
        LOG_REGISTRY_SCHEMA,
        MessageLineError,
        _require_ref,
        _require_timestamp,
        validate_log_registry,
    )

LOG_REPORT_REGISTRY_SCHEMA = "ellmos.ticket.log-report-registry.v1"
SUPPORTED_SCHEMAS = frozenset({LOG_REGISTRY_SCHEMA, LOG_REPORT_REGISTRY_SCHEMA})
SOURCE_KINDS = frozenset({"log", "report"})
EXTENDED_WINDOWS = frozenset(
    {"24h", "monthly", "other", "analysis", "session", "result", "audit"}
)
FETCH_MODES = frozenset({"session_start", "on_demand"})
RETRIEVAL_STATUSES = frozenset({"readable", "empty", "stale", "missing", "denied", "disabled"})

_REQUIRED_SOURCE_FIELDS = frozenset(
    {
        "source_ref",
        "system_ref",
        "owner_ref",
        "window",
        "observed_at",
        "access_policy_ref",
        "redaction_policy_ref",
        "provenance_ref",
        "retention_ref",
        "materialization",
    }
)
_OPTIONAL_SOURCE_FIELDS = frozenset({"source_kind", "freshness_seconds", "fetch_modes"})


def validate_log_report_registry(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate either v1 log-registry or unified log-report-registry."""
    if not isinstance(value, Mapping):
        raise MessageLineError("log-report registry must be an object")
    schema = value.get("schema")
    if schema == LOG_REGISTRY_SCHEMA and set(value.keys()) == {"schema", "sources"}:
        # Delegate pure v1 log-registry checks when no report extensions are used
        try:
            return validate_log_registry(value)
        except MessageLineError:
            pass
    if schema not in SUPPORTED_SCHEMAS:
        raise MessageLineError("unsupported log-report registry schema")
    allowed_top = {"schema", "sources", "alias_of"}
    if not set(value.keys()).issubset(allowed_top) or "sources" not in value:
        raise MessageLineError("log-report registry has invalid top-level fields")
    if "alias_of" in value and value["alias_of"] != LOG_REGISTRY_SCHEMA:
        raise MessageLineError("alias_of must reference ellmos.ticket.log-registry.v1")

    sources = value["sources"]
    if not isinstance(sources, list):
        raise MessageLineError("log-report registry sources must be an array")

    canonical_sources: list[dict[str, Any]] = []
    seen_refs: set[str] = set()
    for idx, src in enumerate(sources):
        if not isinstance(src, Mapping):
            raise MessageLineError(f"sources[{idx}] must be an object")
        keys = set(src.keys())
        missing = _REQUIRED_SOURCE_FIELDS - keys
        extra = keys - (_REQUIRED_SOURCE_FIELDS | _OPTIONAL_SOURCE_FIELDS)
        if missing or extra:
            raise MessageLineError(f"sources[{idx}] has invalid fields")

        source_ref = _require_ref(src["source_ref"], f"sources[{idx}].source_ref")
        if source_ref in seen_refs:
            raise MessageLineError(f"duplicate source_ref: {source_ref}")
        seen_refs.add(source_ref)

        for ref_field in (
            "system_ref",
            "owner_ref",
            "access_policy_ref",
            "redaction_policy_ref",
            "provenance_ref",
            "retention_ref",
        ):
            _require_ref(src[ref_field], f"sources[{idx}].{ref_field}")

        window = src["window"]
        if not isinstance(window, str) or window not in EXTENDED_WINDOWS:
            raise MessageLineError(f"sources[{idx}].window is unsupported")
        _require_timestamp(src["observed_at"], f"sources[{idx}].observed_at")
        if src["materialization"] != "reference_only":
            raise MessageLineError("log-report registry must remain pointer-only")

        kind = src.get(
            "source_kind",
            "report" if window in {"analysis", "session", "result", "audit"} else "log",
        )
        if not isinstance(kind, str) or kind not in SOURCE_KINDS:
            raise MessageLineError(f"sources[{idx}].source_kind is unsupported")

        freshness = src.get("freshness_seconds", 86400 if window == "24h" else 2592000)
        if (
            not isinstance(freshness, int)
            or isinstance(freshness, bool)
            or freshness < 60
            or freshness > 31536000
        ):
            raise MessageLineError(f"sources[{idx}].freshness_seconds is out of range")

        modes_raw = src.get(
            "fetch_modes",
            ["session_start", "on_demand"] if window == "24h" else ["on_demand"],
        )
        if not isinstance(modes_raw, list) or not modes_raw:
            raise MessageLineError(f"sources[{idx}].fetch_modes must be a non-empty list")
        modes: list[str] = []
        for m in modes_raw:
            if not isinstance(m, str) or m not in FETCH_MODES or m in modes:
                raise MessageLineError(f"sources[{idx}].fetch_modes contains invalid mode {m!r}")
            modes.append(m)

        entry = dict(src)
        entry["source_kind"] = kind
        entry["freshness_seconds"] = freshness
        entry["fetch_modes"] = modes
        canonical_sources.append(entry)

    result: dict[str, Any] = {"schema": schema, "sources": canonical_sources}
    if "alias_of" in value:
        result["alias_of"] = LOG_REGISTRY_SCHEMA
    return result


def learn_sources_from_roots(
    roots: Iterable[tuple[str, Path]],
    *,
    system_ref: str,
    owner_ref: str,
    previous_bindings: Mapping[str, Mapping[str, Any]] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Discover log and report files from explicit roots without crawling arbitrary trees.

    Returns pointer-only public registry entries plus a separate private host-binding
    map (`private_bindings`) and relocation/duplicate diagnostics.
    """
    _require_ref(system_ref, "system_ref")
    _require_ref(owner_ref, "owner_ref")
    ts = (now or datetime.now(timezone.utc)).isoformat().replace("+00:00", "Z")
    prev = dict(previous_bindings or {})

    hash_to_prev_ref: dict[str, str] = {}
    for sref, binfo in prev.items():
        if isinstance(binfo, Mapping) and isinstance(binfo.get("sha256"), str):
            hash_to_prev_ref[binfo["sha256"]] = sref

    sources: list[dict[str, Any]] = []
    private_bindings: dict[str, dict[str, Any]] = {}
    duplicates: list[dict[str, str]] = []
    relocated: list[dict[str, str]] = []
    seen_hashes: dict[str, str] = {}

    for root_label, root_path in roots:
        clean_label = re.sub(r"[^a-zA-Z0-9._-]+", "-", root_label.strip()).strip("-") or "root"
        rp = Path(root_path)
        if not rp.exists() or not rp.is_dir():
            continue
        for candidate in sorted(rp.iterdir(), key=lambda p: p.name):
            if not candidate.is_file():
                continue
            suffix = candidate.suffix.lower()
            if suffix not in {".md", ".txt", ".log", ".json", ".jsonl"}:
                continue
            raw = candidate.read_bytes()
            digest = hashlib.sha256(raw).hexdigest()
            if digest in seen_hashes:
                duplicates.append(
                    {
                        "duplicate_path": str(candidate),
                        "canonical_source_ref": seen_hashes[digest],
                        "sha256": digest,
                    }
                )
                continue

            name_lower = candidate.name.lower()
            if "24h" in name_lower or "daily" in name_lower or "antigravity-log" in name_lower:
                kind, window, modes, freshness = (
                    "log",
                    "24h",
                    ["session_start", "on_demand"],
                    86400,
                )
            elif "month" in name_lower or "monat" in name_lower:
                kind, window, modes, freshness = ("log", "monthly", ["on_demand"], 2592000)
            elif "receipt" in name_lower or "result" in name_lower:
                kind, window, modes, freshness = ("report", "result", ["on_demand"], 604800)
            elif "review" in name_lower or "audit" in name_lower:
                kind, window, modes, freshness = ("report", "audit", ["on_demand"], 604800)
            elif "Fle" in candidate.name or "analyse" in name_lower or "analysis" in name_lower or suffix == ".md":
                kind, window, modes, freshness = ("report", "analysis", ["on_demand"], 604800)
            else:
                kind, window, modes, freshness = ("log", "other", ["on_demand"], 604800)

            slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", candidate.stem).strip("-").lower()
            source_ref = hash_to_prev_ref.get(digest, f"{kind}:{clean_label}/{slug}")
            if digest in hash_to_prev_ref:
                old_path = str(prev[source_ref].get("local_path", ""))
                if old_path and old_path != str(candidate):
                    relocated.append(
                        {
                            "source_ref": source_ref,
                            "old_path": old_path,
                            "new_path": str(candidate),
                            "sha256": digest,
                        }
                    )

            seen_hashes[digest] = source_ref
            sources.append(
                {
                    "source_ref": source_ref,
                    "source_kind": kind,
                    "system_ref": system_ref,
                    "owner_ref": owner_ref,
                    "window": window,
                    "observed_at": ts,
                    "freshness_seconds": freshness,
                    "fetch_modes": modes,
                    "access_policy_ref": "policy:local-owner-read",
                    "redaction_policy_ref": "policy:no-secrets-or-raw-audio",
                    "provenance_ref": f"provenance:{clean_label}/{candidate.name}",
                    "retention_ref": f"retention:{window}",
                    "materialization": "reference_only",
                }
            )
            private_bindings[source_ref] = {
                "local_path": str(candidate),
                "sha256": digest,
                "size_bytes": len(raw),
                "updated_at": ts,
            }

    registry = validate_log_report_registry(
        {
            "schema": LOG_REPORT_REGISTRY_SCHEMA,
            "alias_of": LOG_REGISTRY_SCHEMA,
            "sources": sources,
        }
    )
    return {
        "registry": registry,
        "private_bindings": private_bindings,
        "duplicates": duplicates,
        "relocated": relocated,
    }


def retrieve_registered_sources(
    registry: Mapping[str, Any],
    private_bindings: Mapping[str, Mapping[str, Any]],
    *,
    mode: str,
    enabled: bool = True,
    allowed_policies: Iterable[str] = ("policy:local-owner-read",),
    max_excerpt_chars: int = 600,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Retrieve bounded, provenance-cited summaries for session_start or on_demand."""
    checked = validate_log_report_registry(registry)
    if mode not in FETCH_MODES:
        raise MessageLineError(f"unsupported retrieval mode: {mode!r}")
    if not enabled:
        return []

    allowed_set = set(allowed_policies)
    current_time = now or datetime.now(timezone.utc)
    results: list[dict[str, Any]] = []

    for src in checked["sources"]:
        if mode not in src["fetch_modes"]:
            continue
        sref = src["source_ref"]
        base_item = {
            "source_ref": sref,
            "source_kind": src["source_kind"],
            "window": src["window"],
            "provenance_ref": src["provenance_ref"],
            "owner_ref": src["owner_ref"],
        }
        if src["access_policy_ref"] not in allowed_set:
            results.append({**base_item, "status": "denied", "summary": ""})
            continue
        binding = private_bindings.get(sref)
        if not isinstance(binding, Mapping) or not binding.get("local_path"):
            results.append({**base_item, "status": "missing", "summary": ""})
            continue
        path = Path(str(binding["local_path"]))
        if not path.exists() or not path.is_file():
            results.append({**base_item, "status": "missing", "summary": ""})
            continue

        try:
            raw = path.read_bytes()
        except OSError:
            results.append({**base_item, "status": "denied", "summary": ""})
            continue

        if not raw.strip():
            results.append({**base_item, "status": "empty", "summary": ""})
            continue

        obs_dt = datetime.fromisoformat(src["observed_at"].replace("Z", "+00:00"))
        age_sec = (current_time - obs_dt).total_seconds()
        status = "stale" if age_sec > src["freshness_seconds"] else "readable"
        text = raw.decode("utf-8", errors="replace").strip()
        excerpt = text[:max_excerpt_chars]
        results.append(
            {
                **base_item,
                "status": status,
                "sha256": hashlib.sha256(raw).hexdigest(),
                "summary": excerpt,
            }
        )
    return results
