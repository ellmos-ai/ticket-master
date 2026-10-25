"""Focused user isolation, truthful coverage, and one real-session claim tests."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
import session_boot  # noqa: E402
import session_profile  # noqa: E402


def _profile(path: Path, settings: dict, compatibility: str = "active") -> Path:
    path.write_text(json.dumps({"schema": "ticket-master.session-profile.v1",
                                "runtime_compatibility": compatibility, "settings": settings}), encoding="utf-8")
    return path


def test_profile_is_private_per_user_and_invalid_values_fail_closed(tmp_path, monkeypatch):
    a = _profile(tmp_path / "a.json", {"companion": {"enabled": True}})
    b = _profile(tmp_path / "b.json", {"companion": {"enabled": False}})
    monkeypatch.setenv("TM_SESSION_PROFILE", str(a))
    assert session_profile.effective_settings()["settings"]["companion"]["enabled"] is True
    monkeypatch.setenv("TM_SESSION_PROFILE", str(b))
    assert session_profile.effective_settings()["settings"]["companion"]["enabled"] is False
    _profile(b, {"attention": {"enabled": True, "lookback_hours": 0}})
    with pytest.raises(ValueError, match="lookback_hours"):
        session_profile.effective_settings()


def test_pending_profile_preserves_request_but_disables_runtime(tmp_path, monkeypatch):
    path = _profile(tmp_path / "pending.json", {"companion": {"enabled": True}}, "pending")
    monkeypatch.setenv("TM_SESSION_PROFILE", str(path))
    result = session_profile.effective_settings()
    assert result["requested"]["companion"]["enabled"] is True
    assert result["settings"]["companion"]["enabled"] is False


def test_attention_utc_window_dedup_and_missing_coverage(tmp_path):
    now = datetime(2026, 10, 25, 2, 30, tzinfo=timezone.utc)  # EU DST transition day
    recent = tmp_path / "recent.log"
    recent.write_text("2026-10-25T02:29:00Z ERROR Service blocked\n2026-10-25T02:29:00Z ERROR Service blocked\n2026-10-25T02:29:00Z Routine success\n", encoding="utf-8")
    old = tmp_path / "old.log"
    old.write_text("2026-10-23T02:29:00Z ERROR old\n", encoding="utf-8")
    import os

    os.utime(recent, (now.timestamp(), now.timestamp()))
    os.utime(old, (now.timestamp() - 90000, now.timestamp() - 90000))
    result = session_boot.attention_report(
        {"enabled": True, "lookback_hours": 24, "sources": [str(recent), str(old), str(tmp_path / "missing")],
         "filters": ["error", "blocked", "decision", "deadline"]}, now=now,
    )
    assert [item["text"] for item in result["items"]] == ["2026-10-25T02:29:00Z ERROR Service blocked"]
    assert any("unreadable" in row["status"] for row in result["coverage"])
    assert result["status"] == "partial"
    assert result["since"] == "2026-10-24T02:30:00+00:00"
    assert "line timestamps" in result["time_basis"]


def test_decision_candidates_do_not_claim_acceptance(tmp_path):
    source = tmp_path / "to-decide.txt"
    source.write_text("## D-20261025-001 OFFEN\nGETROFFEN D-20261024-002\n", encoding="utf-8")
    result = session_boot.decision_sources({"enabled": True, "sources": [str(source)]})
    assert result["status"] == "candidates-only"
    assert len(result["sources"][0]["candidates"]) == 2
    assert "Check current chat" in result["caveat"]


def test_attention_extracts_bounded_evidence_from_long_canonical_log_line(tmp_path):
    path = tmp_path / "daily.log"
    path.write_text("- 2026-10-25 " + "routine " * 100 + "BLOCKED dependency" + " detail" * 100,
                    encoding="utf-8")
    result = session_boot.attention_report(
        {"enabled": True, "lookback_hours": 24, "sources": [str(path)], "filters": ["blocked"]},
        now=datetime(2026, 10, 25, 3, 0, tzinfo=timezone.utc),
    )
    assert len(result["items"]) == 1
    assert "BLOCKED dependency" in result["items"][0]["text"]
    assert len(result["items"][0]["text"]) < 500
    assert "boundary hour" in result["items"][0]["time_basis"]


def test_companion_single_claim_acceptance_and_rotation(tmp_path, monkeypatch):
    profile = _profile(tmp_path / "profile.json", {"companion": {"enabled": True}})
    monkeypatch.setenv("TM_SESSION_PROFILE", str(profile))
    first = session_boot.companion_claim("session-one")
    assert first["role"] == "taskwriter"
    assert session_boot.companion_claim("session-one") == first
    accepted = session_boot.companion_ack("session-one", "dispatch accepted: worker-1")
    assert accepted["status"] == "accepted"
    assert session_boot.companion_claim("session-one") == accepted
    assert session_boot.companion_claim("session-two")["role"] == "tasksolver"
    with pytest.raises(ValueError, match="receipt"):
        session_boot.companion_ack("session-two", "")


def test_report_reuses_auditor_verdict_only_when_personally_enabled(tmp_path, monkeypatch):
    profile = _profile(tmp_path / "profile.json", {"audit": {"enabled": True}})
    monkeypatch.setenv("TM_SESSION_PROFILE", str(profile))
    seen = []
    monkeypatch.setattr(session_boot.auditor_bridge, "_build_decide_config", lambda config: {"auditor_bridge": {"enabled": False}})
    monkeypatch.setattr(session_boot.auditor_bridge, "decide", lambda config: seen.append(config) or {"action": "skip"})
    result = session_boot.report({})
    assert result["audit"]["action"] == "skip"
    assert seen[0]["auditor_bridge"]["enabled"] is True
