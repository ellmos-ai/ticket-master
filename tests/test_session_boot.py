"""Focused user isolation, truthful coverage, and one real-session claim tests."""

from __future__ import annotations

import json
import subprocess
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


def test_decision_candidates_use_live_canonical_parser(tmp_path):
    module = tmp_path / "decisions_index.py"
    module.write_text(
        "def build_index(root):\n"
        "    return {'schema':'decisions.index/1', 'active_contract':{'valid':True},\n"
        "      'entries':[{'domain':'active','decision_ready':True,'id':'D-20261025-001',\n"
        "      'title':'Open','source_file':'TO-DECIDE-USER.txt','source_line':3},\n"
        "      {'domain':'done','decision_ready':False,'id':'D-20261024-002'}]}\n",
        encoding="utf-8",
    )
    result = session_boot.decision_sources({"enabled": True},
        {"decision_index_module": str(module), "decision_root": str(tmp_path)})
    assert result["status"] == "canonical-current-candidates"
    assert [item["id"] for item in result["candidates"]] == ["D-20261025-001"]
    assert "project authority" in result["caveat"]


def test_invalid_active_decision_contract_never_yields_user_draw(tmp_path):
    module = tmp_path / "broken_index.py"
    module.write_text("def build_index(root):\n"
        "    return {'schema':'decisions.index/1','active_contract':{'valid':False,'errors':['collision']},'entries':[]}\n",
        encoding="utf-8")
    result = session_boot.decision_sources({"enabled": True},
        {"decision_index_module": str(module), "decision_root": str(tmp_path)})
    assert result["status"] == "invalid-active-contract"
    assert result["candidates"] == []


def _approval_case(*, required=True):
    checks = {key: {"status": "checked_nohit", "source": f"readback:{key}"}
              for key in ("decision_index", "standing_authority", "profile_memory", "project_policy")}
    checks["project_policy"] = {"status": "checked_hit", "source": "current project policy"}
    return {"ticket_status": "USER", "subject": "Zenodo follow-on", "checks": checks,
            "decision_requirement": {"status": "required" if required else "not_required",
                                     "primary_source": "current project policy", "readback": "current"},
            "authorities": []}


def test_route_user_status_and_zenodo_keyword_do_not_create_approval_requirement():
    result = session_boot.route_approval_case(_approval_case(required=False))
    assert result["route"] == "authorized_work"
    assert result["authority"]["kind"] == "project_rule"


def test_route_current_scope_bound_grant_allows_work():
    case = _approval_case()
    case["checks"]["standing_authority"]["status"] = "checked_hit"
    case["authorities"] = [{"kind": "standing_authority", "primary_source": "policy#12",
                            "readback": "current", "scope_match": True, "valid": True,
                            "revoked": False, "conflict": False}]
    assert session_boot.route_approval_case(case)["route"] == "authorized_work"


def test_route_technical_blocker_to_worker_before_user_draw():
    case = _approval_case()
    case["technical_blocker"] = True
    assert session_boot.route_approval_case(case)["route"] == "worker"


@pytest.mark.parametrize("change", [
    {"valid": False}, {"revoked": True}, {"conflict": True}, {"scope_match": False},
    {"readback": "stale"},
])
def test_invalid_authority_never_becomes_grant(change):
    case = _approval_case()
    case["checks"]["standing_authority"]["status"] = "checked_hit"
    grant = {"kind": "standing_authority", "primary_source": "policy#old",
             "readback": "current", "scope_match": True, "valid": True,
             "revoked": False, "conflict": False}
    grant.update(change)
    case["authorities"] = [grant]
    result = session_boot.route_approval_case(case)
    assert result["route"] == "user_ticket"
    assert result["ticket_required"] is True


def test_avatar_high_is_prediction_not_authority():
    case = _approval_case()
    case["avatar"] = {"confidence": "high", "prediction": "yes"}
    assert session_boot.route_approval_case(case)["route"] == "user_ticket"


def test_unknown_or_unattributed_readback_cannot_grant_or_escalate():
    case = _approval_case()
    case["checks"]["decision_index"] = {"status": "unknown"}
    assert session_boot.route_approval_case(case)["route"] == "investigate"
    case = _approval_case()
    case["checks"]["decision_index"].pop("source")
    assert session_boot.route_approval_case(case)["route"] == "investigate"
    case = _approval_case()
    case["decision_requirement"]["readback"] = "stale"
    assert session_boot.route_approval_case(case)["route"] == "investigate"
    case = _approval_case(required=False)
    case["checks"]["project_policy"]["source"] = "different policy"
    assert session_boot.route_approval_case(case)["route"] == "investigate"


def test_approval_source_coverage_distinguishes_manual_from_attached(tmp_path):
    existing = tmp_path / "project"
    existing.mkdir()
    rows = session_boot.approval_source_coverage({"enabled": True, "approval_sources": [
        {"name": "central", "path": str(existing), "connector": "canonical_index"},
        {"name": "outreach", "path": str(existing), "connector": "manual"},
        {"name": "zenodo", "path": str(tmp_path / "missing"), "connector": "manual"},
    ]})
    assert [row["status"] for row in rows] == [
        "canonical-index-attached", "manual-check-required; no producer connector", "missing"]


def test_approval_ticket_plan_reuses_canonical_lifecycle_ticket(tmp_path):
    (tmp_path / ".ticket-master-queue").write_text("ticket-master-queue-v1\n", encoding="utf-8")
    inbox = tmp_path / "INBOX"
    inbox.mkdir()
    case = _approval_case()
    case["approval_identity"] = {"decision_id": "D-20261025-001"}
    assert session_boot.approval_ticket_plan(case, tmp_path)["status"] == "create-with-ticket-writer"
    ticket = inbox / "T-20261025-123456789.txt"
    ticket.write_text("ID: T-20261025-123456789\nD-20261025-001\n", encoding="utf-8")
    result = session_boot.approval_ticket_plan(case, tmp_path)
    assert result["status"] == "reuse"
    assert result["ticket"] == str(ticket)
    case["decision_requirement"]["status"] = "not_required"
    assert session_boot.approval_ticket_plan(case, tmp_path)["status"] == "not-user-approval"


def test_approval_ticket_plan_requires_concrete_action_identity(tmp_path):
    (tmp_path / ".ticket-master-queue").write_text("ticket-master-queue-v1\n", encoding="utf-8")
    case = _approval_case()
    case["approval_identity"] = {"action": "publish", "version": "v2"}
    assert session_boot.approval_ticket_plan(case, tmp_path)["status"] == "identity-missing"
    case["approval_identity"]["recipient"] = "journal"
    result = session_boot.approval_ticket_plan(case, tmp_path)
    assert result["status"] == "create-with-ticket-writer"
    assert result["marker"] == "APPROVAL-REF: publish | v2 | journal"


def test_approval_ticket_cli_is_read_only(tmp_path):
    (tmp_path / ".ticket-master-queue").write_text("ticket-master-queue-v1\n", encoding="utf-8")
    case = _approval_case()
    case["approval_identity"] = {"decision_id": "D-20261025-001"}
    evidence = tmp_path / "evidence.json"
    evidence.write_text(json.dumps(case), encoding="utf-8")
    result = subprocess.run(
        [sys.executable, "-X", "utf8", str(Path(session_boot.__file__)),
         "--plan-approval-ticket", str(evidence), "--tickets-dir", str(tmp_path)],
        capture_output=True, text=True, check=True,
    )
    assert json.loads(result.stdout)["status"] == "create-with-ticket-writer"
    assert not (tmp_path / "INBOX").exists()


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


def test_companion_rotation_refuses_corrupt_ledger(tmp_path, monkeypatch):
    profile = _profile(tmp_path / "profile.json", {"companion": {"enabled": True}})
    monkeypatch.setenv("TM_SESSION_PROFILE", str(profile))
    directory = tmp_path / "session-claims"
    directory.mkdir()
    (directory / "prior.json").write_text("not JSON", encoding="utf-8")
    with pytest.raises(ValueError, match="ledger unreadable"):
        session_boot.companion_claim("session-next")
    assert not (directory / "session-next.json").exists()


def test_report_reuses_auditor_verdict_only_when_personally_enabled(tmp_path, monkeypatch):
    profile = _profile(tmp_path / "profile.json", {"audit": {"enabled": True}})
    monkeypatch.setenv("TM_SESSION_PROFILE", str(profile))
    seen = []
    monkeypatch.setattr(session_boot.auditor_bridge, "_build_decide_config", lambda config: {"auditor_bridge": {"enabled": False}})
    monkeypatch.setattr(session_boot.auditor_bridge, "decide", lambda config: seen.append(config) or {"action": "skip"})
    result = session_boot.report({})
    assert result["audit"]["action"] == "skip"
    assert seen[0]["auditor_bridge"]["enabled"] is True
