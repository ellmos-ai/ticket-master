"""Tests for unified log-report-registry discovery, schema parity, and retrieval."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import jsonschema
import pytest

from lib.log_report_registry import (
    LOG_REPORT_REGISTRY_SCHEMA,
    learn_sources_from_roots,
    retrieve_registered_sources,
    validate_log_report_registry,
)
from lib.messages_line import MessageLineError

ROOT = Path(__file__).resolve().parents[1]


def test_backward_compatibility_with_v1_log_registry_and_json_schema():
    schema = json.loads(
        (
            ROOT
            / "contracts"
            / "messages"
            / "v1"
            / "ellmos.ticket.log-report-registry.v1.schema.json"
        ).read_text(encoding="utf-8")
    )
    example = json.loads(
        (ROOT / "config" / "log-report-registry.example.json").read_text(encoding="utf-8")
    )
    validated = validate_log_report_registry(example)
    jsonschema.Draft202012Validator(schema).validate(validated)
    assert validated["schema"] == LOG_REPORT_REGISTRY_SCHEMA


def test_learn_sources_deduplicates_detects_relocation_and_never_leaks_local_paths(tmp_path):
    logs_dir = tmp_path / "24-automation-logging"
    reports_dir = tmp_path / "analysis"
    logs_dir.mkdir()
    reports_dir.mkdir()

    log_file = logs_dir / "daily-24h-summary.log"
    log_file.write_text("2026-10-31 OK: 0 Fehler im 24h-Fenster\n", encoding="utf-8")
    dup_file = logs_dir / "daily-24h-copy.log"
    dup_file.write_text("2026-10-31 OK: 0 Fehler im 24h-Fenster\n", encoding="utf-8")
    rep_file = reports_dir / "FLEET_ANALYSIS_REPORT.md"
    rep_file.write_text("# Analysebericht\nAlle Prüfungen bestanden.\n", encoding="utf-8")

    now = datetime(2026, 10, 31, 12, 0, tzinfo=timezone.utc)
    learned = learn_sources_from_roots(
        [("automation-logs", logs_dir), ("analysis-reports", reports_dir)],
        system_ref="system:asus-gei",
        owner_ref="owner:ticket-master",
        now=now,
    )
    reg = learned["registry"]
    assert len(reg["sources"]) == 2
    assert len(learned["duplicates"]) == 1

    # Public registry never contains local paths
    reg_json = json.dumps(reg)
    assert str(tmp_path) not in reg_json
    assert "C:\\" not in reg_json

    # Simulate file relocation and re-learn
    moved_dir = tmp_path / "moved-reports"
    moved_dir.mkdir()
    moved_rep = moved_dir / "FLEET_ANALYSIS_REPORT_MOVED.md"
    rep_file.replace(moved_rep)

    relearned = learn_sources_from_roots(
        [("automation-logs", logs_dir), ("analysis-reports", moved_dir)],
        system_ref="system:asus-gei",
        owner_ref="owner:ticket-master",
        previous_bindings=learned["private_bindings"],
        now=now,
    )
    assert len(relearned["relocated"]) == 1
    assert relearned["relocated"][0]["new_path"] == str(moved_rep)


def test_retrieve_distinguishes_readable_empty_stale_missing_and_denied(tmp_path):
    now = datetime(2026, 10, 31, 12, 0, tzinfo=timezone.utc)
    readable_file = tmp_path / "24h.log"
    readable_file.write_text("24h Befund: alles grün", encoding="utf-8")
    empty_file = tmp_path / "empty.log"
    empty_file.write_text("   \n", encoding="utf-8")
    stale_file = tmp_path / "stale_report.md"
    stale_file.write_text("# Alter Bericht", encoding="utf-8")

    reg = {
        "schema": LOG_REPORT_REGISTRY_SCHEMA,
        "alias_of": "ellmos.ticket.log-registry.v1",
        "sources": [
            {
                "source_ref": "log:auto/24h",
                "source_kind": "log",
                "system_ref": "system:asus-gei",
                "owner_ref": "owner:ticket-master",
                "window": "24h",
                "observed_at": now.isoformat().replace("+00:00", "Z"),
                "freshness_seconds": 86400,
                "fetch_modes": ["session_start", "on_demand"],
                "access_policy_ref": "policy:local-owner-read",
                "redaction_policy_ref": "policy:no-secrets",
                "provenance_ref": "provenance:auto/24h.log",
                "retention_ref": "retention:24h",
                "materialization": "reference_only",
            },
            {
                "source_ref": "log:auto/empty",
                "source_kind": "log",
                "system_ref": "system:asus-gei",
                "owner_ref": "owner:ticket-master",
                "window": "other",
                "observed_at": now.isoformat().replace("+00:00", "Z"),
                "freshness_seconds": 86400,
                "fetch_modes": ["on_demand"],
                "access_policy_ref": "policy:local-owner-read",
                "redaction_policy_ref": "policy:no-secrets",
                "provenance_ref": "provenance:auto/empty.log",
                "retention_ref": "retention:other",
                "materialization": "reference_only",
            },
            {
                "source_ref": "report:analysis/stale",
                "source_kind": "report",
                "system_ref": "system:asus-gei",
                "owner_ref": "owner:ticket-master",
                "window": "analysis",
                "observed_at": (now - timedelta(days=5)).isoformat().replace("+00:00", "Z"),
                "freshness_seconds": 3600,
                "fetch_modes": ["on_demand"],
                "access_policy_ref": "policy:local-owner-read",
                "redaction_policy_ref": "policy:no-secrets",
                "provenance_ref": "provenance:analysis/stale_report.md",
                "retention_ref": "retention:analysis",
                "materialization": "reference_only",
            },
            {
                "source_ref": "report:analysis/missing",
                "source_kind": "report",
                "system_ref": "system:asus-gei",
                "owner_ref": "owner:ticket-master",
                "window": "result",
                "observed_at": now.isoformat().replace("+00:00", "Z"),
                "freshness_seconds": 3600,
                "fetch_modes": ["on_demand"],
                "access_policy_ref": "policy:local-owner-read",
                "redaction_policy_ref": "policy:no-secrets",
                "provenance_ref": "provenance:analysis/missing.md",
                "retention_ref": "retention:result",
                "materialization": "reference_only",
            },
            {
                "source_ref": "report:restricted/secret",
                "source_kind": "report",
                "system_ref": "system:asus-gei",
                "owner_ref": "owner:ticket-master",
                "window": "audit",
                "observed_at": now.isoformat().replace("+00:00", "Z"),
                "freshness_seconds": 3600,
                "fetch_modes": ["on_demand"],
                "access_policy_ref": "policy:restricted-admin",
                "redaction_policy_ref": "policy:no-secrets",
                "provenance_ref": "provenance:restricted/secret.md",
                "retention_ref": "retention:audit",
                "materialization": "reference_only",
            },
        ],
    }
    bindings = {
        "log:auto/24h": {"local_path": str(readable_file)},
        "log:auto/empty": {"local_path": str(empty_file)},
        "report:analysis/stale": {"local_path": str(stale_file)},
        "report:analysis/missing": {"local_path": str(tmp_path / "does_not_exist.md")},
        "report:restricted/secret": {"local_path": str(readable_file)},
    }

    start_items = retrieve_registered_sources(reg, bindings, mode="session_start", now=now)
    assert len(start_items) == 1
    assert start_items[0]["status"] == "readable"
    assert "alles grün" in start_items[0]["summary"]

    ondemand_items = {
        item["source_ref"]: item
        for item in retrieve_registered_sources(reg, bindings, mode="on_demand", now=now)
    }
    assert ondemand_items["log:auto/24h"]["status"] == "readable"
    assert ondemand_items["log:auto/empty"]["status"] == "empty"
    assert ondemand_items["report:analysis/stale"]["status"] == "stale"
    assert ondemand_items["report:analysis/missing"]["status"] == "missing"
    assert ondemand_items["report:restricted/secret"]["status"] == "denied"
    assert retrieve_registered_sources(reg, bindings, mode="on_demand", enabled=False, now=now) == []

    with pytest.raises(MessageLineError, match="pointer-only"):
        validate_log_report_registry(
            {
                **reg,
                "sources": [{**reg["sources"][0], "materialization": "inline_copy"}],
            }
        )
