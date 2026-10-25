"""Fake-only contract probes: these tests never launch a model or worker."""

from __future__ import annotations

import json
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
import companion_dispatch as dispatch  # noqa: E402
import session_boot  # noqa: E402


def _fixture(tmp_path, monkeypatch, *, compatibility="active", model="gpt-6-luna"):
    profile = tmp_path / "profile.json"
    profile.write_text(json.dumps({"schema": "ticket-master.session-profile.v1",
                                   "runtime_compatibility": compatibility,
                                   "settings": {"companion": {"enabled": True, "max_luna": 1}}}),
                       encoding="utf-8")
    monkeypatch.setenv("TM_SESSION_PROFILE", str(profile))
    claim = session_boot.companion_claim("test-session") if compatibility == "active" else None
    prompt = tmp_path / "TASKWRITER.txt"
    prompt.write_text("Authorized TASKPLAN role\n", encoding="utf-8")
    calls = []

    class FakeLauncher:
        def __init__(self, registry_dir, work_root):
            assert registry_dir == tmp_path / "registry"
            assert work_root == tmp_path

        def start(self, name, **kwargs):
            calls.append((name, kwargs))
            return SimpleNamespace(ok=True, name=name, already_running=False)

    class FakeLock:
        mutex = threading.Lock()

        def __init__(self, path):
            self.path = path

        def acquire(self, *, timeout):
            assert timeout == 0
            if not self.mutex.acquire(blocking=False):
                raise TimeoutError("occupied")

        def release(self):
            self.mutex.release()

    monkeypatch.setattr(dispatch, "_runtime", lambda: (
        SimpleNamespace(provider_name=lambda: "codex"),
        SimpleNamespace(runtime_profile=lambda role, provider: {
            "role": role, "provider": provider, "model": model, "reasoning_effort": "medium"}),
        SimpleNamespace(get_workflow_prompt_path=lambda role: prompt),
        SimpleNamespace(AgentLauncher=FakeLauncher),
        SimpleNamespace(FileLock=FakeLock)))
    return claim, calls, {"agent_launcher_registry_dir": str(tmp_path / "registry"),
                          "agent_launcher_work_root": str(tmp_path)}


def _admission(claim, *, now=None):
    now = now or datetime.now(timezone.utc)
    stamp = now.isoformat()
    management = {"agents": [
        {"agent_name": "/root", "agent_status": "running"},
        {"agent_name": "/root/luna", "agent_status": {"completed": "done"}},
    ]}
    quota = {"status": "OK", "checked_at": stamp,
             "windows": {"primary": {"remaining_percent": 34}}}
    return {"schema": dispatch.ADMISSION_SCHEMA, "session_id": claim["session_id"],
            "claim_id": claim["claim_id"], "role": claim["role"],
            "provider": "codex", "model": "gpt-6-luna", "model_class": "low-medium",
            "issued_at": stamp, "expires_at": (now + timedelta(seconds=90)).isoformat(),
            "management": {"source": "collaboration.list_agents", "checked_at": stamp,
                           "readback": management, "sha256": dispatch._digest(management)},
            "agent_classes": {"/root": "other", "/root/luna": "low-medium"},
            "slot": {"limit": 1},
            "quota": {"provider": "codex", "checked_at": stamp,
                      "readback": quota, "sha256": dispatch._digest(quota)},
            "task_gate": {"status": "ready", "role": claim["role"],
                          "locks": "clear", "scope": "authorized",
                          "task_ref": "T-20261025-123456789",
                          "source": "taskplan next --role taskwriter --json"}}


def _file(tmp_path, admission):
    path = tmp_path / "admission.json"
    path.write_text(json.dumps(admission), encoding="utf-8")
    return path


def test_bound_dispatch_uses_taskplan_role_prompt_config_and_existing_ack(tmp_path, monkeypatch):
    claim, calls, config = _fixture(tmp_path, monkeypatch)
    accepted = []

    def ack(session_id, receipt, *, config):
        accepted.append((session_id, receipt))
        return {"acceptance_evidence": {"pid": 123, "claim_id": claim["claim_id"]}}

    monkeypatch.setattr(session_boot, "companion_ack", ack)
    result = dispatch.dispatch_companion("test-session", _file(tmp_path, _admission(claim)),
                                         config=config)
    assert result["status"] == "accepted"
    assert accepted == [("test-session", f"agent-launcher:{claim['dispatch_name']}")]
    assert len(calls) == 1
    name, kwargs = calls[0]
    assert name == claim["dispatch_name"]
    assert kwargs["provider"] == "codex" and kwargs["model"] == "gpt-6-luna"
    assert kwargs["prompt_file"] == tmp_path / "TASKWRITER.txt"
    assert "T-20261025-123456789" in kwargs["request"]
    assert kwargs["headless"] is True and kwargs["permission_mode"] == "restricted"
    assert kwargs["extra_metadata"] == {
        "ticket_master_session_id": "test-session",
        "ticket_master_claim_id": claim["claim_id"],
        "ticket_master_role": "taskwriter"}
    # The once-only attempt record blocks even a delayed or concurrent retry.
    assert dispatch.dispatch_companion("test-session", _file(tmp_path, _admission(claim)),
                                       config=config)["status"] == "pending-reconcile"
    assert len(calls) == 1


@pytest.mark.parametrize("mutation", [
    "wrong-claim", "wrong-role", "wrong-provider", "wrong-model", "wrong-hash",
    "stale", "zero-slot", "missing-agent", "unknown-status", "no-budget",
    "no-task-gate", "no-scope", "no-lock-gate", "high-over-limit",
    "quota-stale", "quota-hash", "wrong-tier", "slot-over-ceiling",
])
def test_admission_errors_never_reach_sdk(tmp_path, monkeypatch, mutation):
    claim, calls, config = _fixture(tmp_path, monkeypatch)
    admission = _admission(claim)
    if mutation == "wrong-claim":
        admission["claim_id"] = "0" * 32
    elif mutation == "wrong-role":
        admission["role"] = "maintainer"
    elif mutation == "wrong-provider":
        admission["provider"] = "claude"
    elif mutation == "wrong-model":
        admission["model"] = "other"
    elif mutation == "wrong-hash":
        admission["management"]["sha256"] = "0" * 64
    elif mutation == "stale":
        admission["issued_at"] = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    elif mutation == "zero-slot":
        admission["management"]["readback"]["agents"][1]["agent_status"] = "running"
        admission["management"]["sha256"] = dispatch._digest(admission["management"]["readback"])
    elif mutation == "missing-agent":
        del admission["agent_classes"]["/root/luna"]
    elif mutation == "unknown-status":
        admission["management"]["readback"]["agents"][1]["agent_status"] = "idle"
        admission["management"]["sha256"] = dispatch._digest(admission["management"]["readback"])
    elif mutation == "no-budget":
        admission["quota"]["readback"]["windows"]["primary"]["remaining_percent"] = 0
        admission["quota"]["sha256"] = dispatch._digest(admission["quota"]["readback"])
    elif mutation == "no-task-gate":
        admission["task_gate"]["status"] = "unknown"
    elif mutation == "no-scope":
        admission["task_gate"]["scope"] = "unknown"
    elif mutation == "no-lock-gate":
        admission["task_gate"]["locks"] = "unknown"
    elif mutation == "high-over-limit":
        admission["agent_classes"]["/root"] = "high"
        admission["agent_classes"]["/root/luna"] = "high"
        admission["management"]["readback"]["agents"][1]["agent_status"] = "running"
        admission["management"]["sha256"] = dispatch._digest(admission["management"]["readback"])
    elif mutation == "quota-stale":
        admission["quota"]["readback"]["checked_at"] = (
            datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        admission["quota"]["sha256"] = dispatch._digest(admission["quota"]["readback"])
    elif mutation == "quota-hash":
        admission["quota"]["sha256"] = "0" * 64
    elif mutation == "wrong-tier":
        admission["model_class"] = "high"
    elif mutation == "slot-over-ceiling":
        admission["slot"]["limit"] = 2
    with pytest.raises(ValueError):
        dispatch.dispatch_companion("test-session", _file(tmp_path, admission), config=config)
    assert calls == []
    assert not list((tmp_path / "session-claims").glob("*.dispatch-attempt"))


def test_failed_or_ambiguous_start_keeps_pending_without_retry(tmp_path, monkeypatch):
    claim, calls, config = _fixture(tmp_path, monkeypatch)
    runtime = dispatch._runtime()

    class FailedLauncher(runtime[3].AgentLauncher):
        def start(self, name, **kwargs):
            super().start(name, **kwargs)
            return SimpleNamespace(ok=False, name=name, already_running=False)

    monkeypatch.setattr(dispatch, "_runtime", lambda: (*runtime[:3],
                        SimpleNamespace(AgentLauncher=FailedLauncher), runtime[4]))
    admission = _file(tmp_path, _admission(claim))
    assert dispatch.dispatch_companion("test-session", admission, config=config)["status"] == "pending-reconcile"
    assert dispatch.dispatch_companion("test-session", admission, config=config)["status"] == "pending-reconcile"
    assert len(calls) == 1
    assert session_boot.companion_claim("test-session")["status"] == "pending"


def test_disabled_profile_never_loads_runtime_or_admission(tmp_path, monkeypatch):
    _fixture(tmp_path, monkeypatch, compatibility="pending")
    assert dispatch.dispatch_companion("test-session", tmp_path / "missing.json") == {"status": "disabled"}


def test_failed_native_ack_stays_pending_and_cannot_double_start(tmp_path, monkeypatch):
    claim, calls, config = _fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(session_boot, "companion_ack",
                        lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("no PID")))
    admission = _file(tmp_path, _admission(claim))
    assert dispatch.dispatch_companion("test-session", admission, config=config)["status"] == "pending-reconcile"
    assert dispatch.dispatch_companion("test-session", admission, config=config)["status"] == "pending-reconcile"
    assert len(calls) == 1


def test_second_session_requires_new_management_readback_after_attempt(tmp_path, monkeypatch):
    first, calls, config = _fixture(tmp_path, monkeypatch)
    second = session_boot.companion_claim("next-session")
    seen = datetime.now(timezone.utc)
    monkeypatch.setattr(session_boot, "companion_ack",
                        lambda *args, **kwargs: {"acceptance_evidence": {"pid": 123}})
    dispatch.dispatch_companion("test-session", _file(tmp_path, _admission(first, now=seen)),
                                config=config)
    with pytest.raises(ValueError, match="predates another dispatch"):
        dispatch.dispatch_companion("next-session", _file(tmp_path, _admission(second, now=seen)),
                                    config=config)
    assert len(calls) == 1


def test_missing_configured_model_fails_before_attempt(tmp_path, monkeypatch):
    claim, calls, config = _fixture(tmp_path, monkeypatch, model="")
    with pytest.raises(ValueError, match="configured"):
        dispatch.dispatch_companion("test-session", _file(tmp_path, _admission(claim)), config=config)
    assert calls == []


def test_uncertain_sdk_exception_is_not_retried(tmp_path, monkeypatch):
    claim, calls, config = _fixture(tmp_path, monkeypatch)
    runtime = dispatch._runtime()

    class UncertainLauncher(runtime[3].AgentLauncher):
        def start(self, name, **kwargs):
            super().start(name, **kwargs)
            raise RuntimeError("spawn result unknown")

    monkeypatch.setattr(dispatch, "_runtime", lambda: (*runtime[:3],
                        SimpleNamespace(AgentLauncher=UncertainLauncher), runtime[4]))
    admission = _file(tmp_path, _admission(claim))
    assert dispatch.dispatch_companion("test-session", admission, config=config)["status"] == "pending-reconcile"
    assert dispatch.dispatch_companion("test-session", admission, config=config)["status"] == "pending-reconcile"
    assert len(calls) == 1
    assert session_boot.companion_claim("test-session")["status"] == "pending"


def test_parallel_same_claim_cannot_double_start(tmp_path, monkeypatch):
    claim, calls, config = _fixture(tmp_path, monkeypatch)
    runtime = dispatch._runtime()
    entered = threading.Event()
    release = threading.Event()

    class SlowLauncher(runtime[3].AgentLauncher):
        def start(self, name, **kwargs):
            result = super().start(name, **kwargs)
            entered.set()
            assert release.wait(5)
            return result

    monkeypatch.setattr(dispatch, "_runtime", lambda: (*runtime[:3],
                        SimpleNamespace(AgentLauncher=SlowLauncher), runtime[4]))
    monkeypatch.setattr(session_boot, "companion_ack",
                        lambda *args, **kwargs: {"acceptance_evidence": {"pid": 123}})
    admission = _file(tmp_path, _admission(claim))
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(dispatch.dispatch_companion, "test-session", admission, config=config)
        assert entered.wait(5)
        with pytest.raises(ValueError, match="reservation unavailable"):
            dispatch.dispatch_companion("test-session", admission, config=config)
        release.set()
        assert first.result(timeout=5)["status"] == "accepted"
    assert len(calls) == 1
