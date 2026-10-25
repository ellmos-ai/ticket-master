"""Fake-only contract probes: these tests never launch a model or worker."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from filelock import FileLock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
import companion_dispatch as dispatch  # noqa: E402
import session_boot  # noqa: E402


def _fixture(tmp_path, monkeypatch, *, compatibility="active", model="gpt-6-luna"):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(session_boot, "_capacity_root", lambda: tmp_path)
    profile = tmp_path / "profile.json"
    profile.write_text(json.dumps({"schema": "ticket-master.session-profile.v1",
                                   "runtime_compatibility": compatibility,
                                   "settings": {"companion": {"enabled": True, "max_luna": 1}}}),
                       encoding="utf-8")
    monkeypatch.setenv("TM_SESSION_PROFILE", str(profile))
    config = {"session_profile_path": str(profile),
              "agent_launcher_registry_dir": str(tmp_path / "registry"),
              "agent_launcher_work_root": str(tmp_path)}
    claim = session_boot.companion_claim("test-session", config=config) if compatibility == "active" else None
    prompt = tmp_path / "TASKWRITER.txt"
    prompt.write_text("Authorized TASKPLAN role\n", encoding="utf-8")
    (tmp_path / "registry").mkdir()
    calls = []

    class FakeRegistry:
        records = {}

        def __init__(self, registry_dir):
            self.registry_dir = registry_dir

        def load_metadata(self, name):
            return self.records.get(name, {})

        def probe_running(self, name):
            row = self.records.get(name, {})
            return row.get("_probe", row.get("pid", 0))

    class FakeLauncher:
        def __init__(self, registry_dir, work_root):
            assert registry_dir == tmp_path / "registry"
            assert work_root == Path(config["agent_launcher_work_root"]).resolve()
            self.work_root = work_root

        def work_dir_for(self, name):
            return self.work_root / f"agent_{name}"

        def start(self, name, **kwargs):
            calls.append((name, kwargs))
            work_dir = self.work_dir_for(name)
            FakeRegistry.records[name] = {
                "name": name, "launch_state": "active", "pid": 4242 + len(calls),
                "process_create_time": datetime.now(timezone.utc).timestamp(),
                "work_dir": str(work_dir), "cwd": str(work_dir.resolve()),
                **kwargs["extra_metadata"],
            }
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
        SimpleNamespace(AgentLauncher=FakeLauncher, AgentProcessRegistry=FakeRegistry),
        SimpleNamespace(FileLock=FakeLock)))
    return claim, calls, config


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
        "ticket_master_role": "taskwriter",
        "ticket_master_work_root": str(tmp_path)}
    # The once-only attempt record blocks even a delayed or concurrent retry.
    assert dispatch.dispatch_companion("test-session", _file(tmp_path, _admission(claim)),
                                       config=config)["status"] == "pending-reconcile"
    assert len(calls) == 1


@pytest.mark.parametrize("mutate_config,wrong_registry_root", [
    (False, False), (True, False), (False, True),
])
def test_sdk_workroot_is_distinct_from_account_ledger_and_bound_in_ack(
        tmp_path, monkeypatch, mutate_config, wrong_registry_root):
    claim, _, config = _fixture(tmp_path, monkeypatch)
    sdk_root = tmp_path / "sdk-work"
    sdk_root.mkdir()
    other_root = tmp_path / "other-sdk-work"
    other_root.mkdir()
    config["agent_launcher_work_root"] = str(sdk_root) + "/."
    registry = dispatch._runtime()[3].AgentProcessRegistry
    observed = []

    class Launcher:
        def __init__(self, registry_dir, work_root):
            assert registry_dir == tmp_path / "registry"
            observed.append((registry_dir, work_root))
            self.work_root = work_root

        def work_dir_for(self, name):
            return self.work_root / f"agent_{name}"

        def start(self, name, **kwargs):
            work_dir = self.work_dir_for(name)
            metadata = {"name": name, "launch_state": "active", "pid": 4242,
                        "process_create_time": datetime.now(timezone.utc).timestamp(),
                "work_dir": str(work_dir), "cwd": str(work_dir.resolve()),
                        **kwargs["extra_metadata"]}
            if wrong_registry_root:
                metadata["ticket_master_work_root"] = str(other_root)
            registry.records[name] = metadata
            if mutate_config:
                config["agent_launcher_work_root"] = str(other_root)
            return SimpleNamespace(ok=True, name=name, already_running=False)

    runtime = dispatch._runtime()
    sdk = SimpleNamespace(AgentLauncher=Launcher, AgentProcessRegistry=registry)
    monkeypatch.setattr(dispatch, "_runtime", lambda: (*runtime[:3], sdk, runtime[4]))
    monkeypatch.setitem(sys.modules, "agent_launcher", sdk)
    result = dispatch.dispatch_companion("test-session", _file(tmp_path, _admission(claim)),
                                         config=config)
    assert observed == [(tmp_path / "registry", sdk_root.resolve())]
    assert observed[0][1] != tmp_path  # OS capacity root is never the SDK cwd root.
    assert (tmp_path / ".ticket-master-companion" / "test-session.dispatch-attempt").is_file()
    if wrong_registry_root:
        assert result["status"] == "pending-reconcile"
        assert session_boot.companion_claim("test-session", config={**config,
            "agent_launcher_work_root": str(sdk_root)})["status"] == "pending"
    else:
        assert result["status"] == "accepted"
        assert registry.records[claim["dispatch_name"]]["ticket_master_work_root"] == str(sdk_root)
        # A caller mutation after SDK start cannot redirect the ACK or SDK cwd.
        assert session_boot.companion_claim("test-session", config={**config,
            "agent_launcher_work_root": str(sdk_root)})["status"] == "accepted"


@pytest.mark.parametrize("mutation", ["none", "work-dir", "cwd", "missing-work-dir",
                                     "missing-cwd", "changed-after-start"])
def test_native_sdk_paths_are_bound_to_prestart_intent(tmp_path, monkeypatch, mutation):
    claim, calls, config = _fixture(tmp_path, monkeypatch)
    sdk_root = tmp_path / "sdk"
    sdk_root.mkdir()
    config["agent_launcher_work_root"] = str(sdk_root)
    other = tmp_path / "other"
    other.mkdir()
    runtime = dispatch._runtime()
    registry = runtime[3].AgentProcessRegistry

    class NativeLauncher(runtime[3].AgentLauncher):
        def start(self, name, **kwargs):
            result = super().start(name, **kwargs)
            row = registry.records[name]
            if mutation == "work-dir":
                row["work_dir"] = str(other / f"agent_{name}")
            elif mutation == "cwd":
                row["cwd"] = str(other)
            elif mutation == "missing-work-dir":
                del row["work_dir"]
            elif mutation == "missing-cwd":
                del row["cwd"]
            elif mutation == "changed-after-start":
                config["agent_launcher_work_root"] = str(other)
            return result

    sdk = SimpleNamespace(AgentLauncher=NativeLauncher, AgentProcessRegistry=registry)
    monkeypatch.setattr(dispatch, "_runtime", lambda: (*runtime[:3], sdk, runtime[4]))
    monkeypatch.setitem(sys.modules, "agent_launcher", sdk)
    result = dispatch.dispatch_companion(claim["session_id"],
                                         _file(tmp_path, _admission(claim)), config=config)
    assert len(calls) == 1
    assert calls[0][0] == claim["dispatch_name"]
    attempt = json.loads((tmp_path / ".ticket-master-companion"
                          / f"{claim['session_id']}.dispatch-attempt").read_text(encoding="utf-8"))
    assert attempt["sdk_work_root"] == str(sdk_root)
    assert attempt["expected_work_dir"] == str(sdk_root / f"agent_{claim['dispatch_name']}")
    assert attempt["expected_cwd"] == attempt["expected_work_dir"]
    if mutation in ("none", "changed-after-start"):
        assert result["status"] == "accepted"
    else:
        assert result["status"] == "pending-reconcile"
        assert session_boot.companion_claim(claim["session_id"], config={
            **config, "agent_launcher_work_root": str(sdk_root)})["status"] == "pending"


def test_child_junction_escape_is_rejected_before_sdk_start(tmp_path, monkeypatch):
    claim, calls, config = _fixture(tmp_path, monkeypatch)
    sdk_root, outside = tmp_path / "sdk", tmp_path / "outside"
    sdk_root.mkdir()
    outside.mkdir()
    config["agent_launcher_work_root"] = str(sdk_root)
    child = sdk_root / f"agent_{claim['dispatch_name']}"
    if sys.platform == "win32":
        command = ("New-Item -ItemType Junction -Path '" + str(child)
                   + "' -Target '" + str(outside) + "' | Out-Null")
        subprocess.run(["powershell", "-NoProfile", "-Command",
                        command],
                       check=True, capture_output=True)
    else:
        child.symlink_to(outside, target_is_directory=True)
    try:
        with pytest.raises(ValueError, match="escapes SDK root"):
            dispatch.dispatch_companion(claim["session_id"],
                                        _file(tmp_path, _admission(claim)), config=config)
        assert calls == []
        assert not (tmp_path / ".ticket-master-companion"
                    / f"{claim['session_id']}.dispatch-attempt").exists()
    finally:
        if sys.platform == "win32":
            os.rmdir(child)
        else:
            child.unlink()


def test_missing_sdk_root_blocks_dispatch_without_blocking_report(tmp_path, monkeypatch):
    claim, calls, config = _fixture(tmp_path, monkeypatch)
    config.pop("agent_launcher_work_root")
    with pytest.raises(ValueError, match="work root is not configured"):
        dispatch.dispatch_companion(claim["session_id"],
                                    _file(tmp_path, _admission(claim)), config=config)
    assert calls == []
    assert not (tmp_path / ".ticket-master-companion"
                / f"{claim['session_id']}.dispatch-attempt").exists()
    assert session_boot.report(config)["companion"]["enabled"] is True


def test_configured_sdk_ack_without_pinned_attempt_is_not_accepted(tmp_path, monkeypatch):
    claim, calls, config = _fixture(tmp_path, monkeypatch)
    registry = dispatch._runtime()[3].AgentProcessRegistry
    work_dir = tmp_path / f"agent_{claim['dispatch_name']}"
    registry.records[claim["dispatch_name"]] = {
        "name": claim["dispatch_name"], "launch_state": "active", "pid": 4242,
        "process_create_time": datetime.now(timezone.utc).timestamp(),
        "work_dir": str(work_dir), "cwd": str(work_dir),
        "ticket_master_session_id": claim["session_id"],
        "ticket_master_claim_id": claim["claim_id"],
        "ticket_master_role": claim["role"],
        "ticket_master_work_root": str(tmp_path),
    }
    monkeypatch.setitem(sys.modules, "agent_launcher", dispatch._runtime()[3])
    with pytest.raises(ValueError, match="start intent is unavailable"):
        session_boot.companion_ack(claim["session_id"],
                                   f"agent-launcher:{claim['dispatch_name']}", config=config)
    assert session_boot.companion_claim(claim["session_id"], config=config)["status"] == "pending"
    assert calls == []


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
    assert not list((tmp_path / ".ticket-master-companion").glob("*.dispatch-attempt"))


def test_failed_or_ambiguous_start_keeps_pending_without_retry(tmp_path, monkeypatch):
    claim, calls, config = _fixture(tmp_path, monkeypatch)
    runtime = dispatch._runtime()

    class FailedLauncher(runtime[3].AgentLauncher):
        def start(self, name, **kwargs):
            super().start(name, **kwargs)
            return SimpleNamespace(ok=False, name=name, already_running=False)

    monkeypatch.setattr(dispatch, "_runtime", lambda: (*runtime[:3],
                        SimpleNamespace(AgentLauncher=FailedLauncher,
                                        AgentProcessRegistry=runtime[3].AgentProcessRegistry), runtime[4]))
    admission = _file(tmp_path, _admission(claim))
    assert dispatch.dispatch_companion("test-session", admission, config=config)["status"] == "pending-reconcile"
    assert dispatch.dispatch_companion("test-session", admission, config=config)["status"] == "pending-reconcile"
    assert len(calls) == 1
    assert session_boot.companion_claim("test-session", config=config)["status"] == "pending"


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
    second = session_boot.companion_claim("next-session", config=config)
    seen = datetime.now(timezone.utc)
    monkeypatch.setattr(session_boot, "companion_ack",
                        lambda *args, **kwargs: {"acceptance_evidence": {"pid": 123}})
    dispatch.dispatch_companion("test-session", _file(tmp_path, _admission(first, now=seen)),
                                config=config)
    with pytest.raises(ValueError, match="predates another dispatch"):
        dispatch.dispatch_companion("next-session", _file(tmp_path, _admission(second, now=seen)),
                                    config=config)
    assert len(calls) == 1


def _prior_attempt(tmp_path, claim, *, created):
    path = tmp_path / ".ticket-master-companion" / f"{claim['session_id']}.dispatch-attempt"
    path.write_text(json.dumps({"schema": dispatch.ATTEMPT_SCHEMA,
                                "session_id": claim["session_id"],
                                "claim_id": claim["claim_id"],
                                "dispatch_name": claim["dispatch_name"],
                                "created_at": created.isoformat(),
                                "status": "invoked-unknown"}), encoding="utf-8")


def test_fresh_native_snapshot_still_counts_live_sdk_child(tmp_path, monkeypatch):
    first, calls, config = _fixture(tmp_path, monkeypatch)
    second = session_boot.companion_claim("next-session", config=config)
    _prior_attempt(tmp_path, first, created=datetime.now(timezone.utc) - timedelta(seconds=20))
    registry = dispatch._runtime()[3].AgentProcessRegistry
    registry.records[first["dispatch_name"]] = {
        "name": first["dispatch_name"], "launch_state": "active", "pid": 4242,
        "process_create_time": datetime.fromisoformat(first["created_at"]).timestamp() + 1,
        "ticket_master_session_id": first["session_id"],
        "ticket_master_claim_id": first["claim_id"],
        "ticket_master_role": first["role"],
    }
    monkeypatch.setattr(dispatch, "_process_state", lambda pid, birth: "live")
    with pytest.raises(ValueError, match="no attested companion slot"):
        dispatch.dispatch_companion(second["session_id"],
                                    _file(tmp_path, _admission(second)), config=config)
    assert calls == []


def test_real_claim_ack_then_fresh_native_snapshot_still_blocks_second_sdk_child(
        tmp_path, monkeypatch):
    first, calls, config = _fixture(tmp_path, monkeypatch)
    second = session_boot.companion_claim("next-session", config=config)
    monkeypatch.setitem(sys.modules, "agent_launcher", dispatch._runtime()[3])
    assert dispatch.dispatch_companion(first["session_id"],
                                       _file(tmp_path, _admission(first)),
                                       config=config)["status"] == "accepted"
    assert session_boot.companion_claim(first["session_id"], config=config)["status"] == "accepted"
    # Isolate R1 from R2 even on Windows clocks with coarse timestamp ticks.
    prior = tmp_path / ".ticket-master-companion" / f"{first['session_id']}.dispatch-attempt"
    record = json.loads(prior.read_text(encoding="utf-8"))
    record["created_at"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    prior.write_text(json.dumps(record), encoding="utf-8")
    with pytest.raises(ValueError, match="no attested companion slot"):
        dispatch.dispatch_companion(second["session_id"],
                                    _file(tmp_path, _admission(second)), config=config)
    assert len(calls) == 1


def test_proven_ended_sdk_child_frees_slot_after_new_measurement(tmp_path, monkeypatch):
    first, calls, config = _fixture(tmp_path, monkeypatch)
    second = session_boot.companion_claim("next-session", config=config)
    _prior_attempt(tmp_path, first, created=datetime.now(timezone.utc) - timedelta(seconds=20))
    registry = dispatch._runtime()[3].AgentProcessRegistry
    registry.records[first["dispatch_name"]] = {
        "name": first["dispatch_name"], "launch_state": "active", "pid": 4242,
        "_probe": 0,
        "process_create_time": datetime.fromisoformat(first["created_at"]).timestamp() + 1,
        "ticket_master_session_id": first["session_id"],
        "ticket_master_claim_id": first["claim_id"],
        "ticket_master_role": first["role"],
    }
    monkeypatch.setattr(dispatch, "_process_state", lambda pid, birth: "ended")
    monkeypatch.setattr(session_boot, "companion_ack",
                        lambda *args, **kwargs: {"acceptance_evidence": {"pid": 43}})
    assert dispatch.dispatch_companion(second["session_id"],
                                       _file(tmp_path, _admission(second)),
                                       config=config)["status"] == "accepted"
    assert len(calls) == 1


def test_pid_birth_probe_frees_only_proven_termination(monkeypatch):
    class ProbeError(Exception):
        pass

    class Gone(ProbeError):
        pass

    class Unreadable(ProbeError):
        pass

    monkeypatch.setitem(sys.modules, "psutil", SimpleNamespace(
        Process=lambda pid: (_ for _ in ()).throw(Gone()),
        NoSuchProcess=Gone, Error=ProbeError))
    assert dispatch._process_state(4242, 1234.0) == "ended"
    monkeypatch.setitem(sys.modules, "psutil", SimpleNamespace(
        Process=lambda pid: (_ for _ in ()).throw(Unreadable()),
        NoSuchProcess=Gone, Error=ProbeError))
    assert dispatch._process_state(4242, 1234.0) == "unknown"
    monkeypatch.setitem(sys.modules, "psutil", SimpleNamespace(
        Process=lambda pid: SimpleNamespace(create_time=lambda: 9999.0),
        NoSuchProcess=Gone, Error=ProbeError))
    assert dispatch._process_state(4242, 1234.0) == "ended"


def test_unknown_prior_attempt_cannot_prove_sdk_slot_free(tmp_path, monkeypatch):
    first, calls, config = _fixture(tmp_path, monkeypatch)
    second = session_boot.companion_claim("next-session", config=config)
    _prior_attempt(tmp_path, first, created=datetime.now(timezone.utc) - timedelta(seconds=20))
    with pytest.raises(ValueError, match="cannot be reconciled"):
        dispatch.dispatch_companion(second["session_id"],
                                    _file(tmp_path, _admission(second)), config=config)
    assert calls == []


def test_reissued_wrapper_cannot_refresh_prestart_measurement(tmp_path, monkeypatch):
    first, calls, config = _fixture(tmp_path, monkeypatch)
    second = session_boot.companion_claim("next-session", config=config)
    measured = datetime.now(timezone.utc) - timedelta(seconds=30)
    _prior_attempt(tmp_path, first, created=measured + timedelta(seconds=10))
    admission = _admission(second, now=measured)
    admission["issued_at"] = datetime.now(timezone.utc).isoformat()
    admission["expires_at"] = (datetime.now(timezone.utc) + timedelta(seconds=90)).isoformat()
    with pytest.raises(ValueError, match="predates another dispatch"):
        dispatch.dispatch_companion(second["session_id"],
                                    _file(tmp_path, admission), config=config)
    assert calls == []


def test_new_management_with_old_quota_measurement_cannot_follow_attempt(tmp_path, monkeypatch):
    first, calls, config = _fixture(tmp_path, monkeypatch)
    second = session_boot.companion_claim("next-session", config=config)
    old = datetime.now(timezone.utc) - timedelta(seconds=30)
    _prior_attempt(tmp_path, first, created=old + timedelta(seconds=10))
    admission = _admission(second)
    admission["quota"]["checked_at"] = old.isoformat()
    admission["quota"]["readback"]["checked_at"] = old.isoformat()
    admission["quota"]["sha256"] = dispatch._digest(admission["quota"]["readback"])
    with pytest.raises(ValueError, match="predates another dispatch"):
        dispatch.dispatch_companion(second["session_id"],
                                    _file(tmp_path, admission), config=config)
    assert calls == []


def test_unreadable_sdk_record_is_not_a_free_slot(tmp_path, monkeypatch):
    claim, calls, config = _fixture(tmp_path, monkeypatch)
    registry_dir = tmp_path / "registry"
    (registry_dir / f"{claim['dispatch_name']}.pid").write_text("invalid", encoding="utf-8")
    with pytest.raises(ValueError, match="registry record is unreadable"):
        dispatch.dispatch_companion(claim["session_id"],
                                    _file(tmp_path, _admission(claim)), config=config)
    assert calls == []


def test_default_registry_child_counts_with_isolated_candidate_registry(tmp_path, monkeypatch):
    claim, calls, config = _fixture(tmp_path, monkeypatch)
    external_session, external_claim = "other-session", "a" * 32
    name = session_boot._dispatch_name(external_session, external_claim)
    default = tmp_path / "home" / ".agent-launcher" / "registry"
    default.mkdir(parents=True)
    (default / f"{name}.pid").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(dispatch.Path, "home", classmethod(lambda cls: tmp_path / "home"))
    dispatch._runtime()[3].AgentProcessRegistry.records[name] = {
        "name": name, "launch_state": "active", "pid": 4242,
        "process_create_time": datetime.now(timezone.utc).timestamp(),
        "ticket_master_session_id": external_session,
        "ticket_master_claim_id": external_claim,
        "ticket_master_role": "tasksolver",
    }
    with pytest.raises(ValueError, match="no attested companion slot"):
        dispatch.dispatch_companion(claim["session_id"],
                                    _file(tmp_path, _admission(claim)), config=config)
    assert calls == []


@pytest.mark.parametrize("windows,top", [
    ({"primary": 100, "secondary": 0}, None),
    ({"primary": 0}, 100),
    ({"primary": 20, "secondary": 30}, 0),
    ({"primary": 20, "secondary": None}, None),
])
def test_every_reported_quota_window_must_be_positive(tmp_path, monkeypatch, windows, top):
    claim, calls, config = _fixture(tmp_path, monkeypatch)
    admission = _admission(claim)
    quota = admission["quota"]["readback"]
    quota["windows"] = {key: {"remaining_percent": value} for key, value in windows.items()}
    if top is not None:
        quota["remaining_percent"] = top
    admission["quota"]["sha256"] = dispatch._digest(quota)
    with pytest.raises(ValueError, match="positive measured remainder"):
        dispatch.dispatch_companion(claim["session_id"],
                                    _file(tmp_path, admission), config=config)
    assert calls == []


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
                        SimpleNamespace(AgentLauncher=UncertainLauncher,
                                        AgentProcessRegistry=runtime[3].AgentProcessRegistry), runtime[4]))
    admission = _file(tmp_path, _admission(claim))
    assert dispatch.dispatch_companion("test-session", admission, config=config)["status"] == "pending-reconcile"
    assert dispatch.dispatch_companion("test-session", admission, config=config)["status"] == "pending-reconcile"
    assert len(calls) == 1
    assert session_boot.companion_claim("test-session", config=config)["status"] == "pending"


def test_parallel_same_claim_cannot_double_start(tmp_path, monkeypatch):
    claim, calls, config = _fixture(tmp_path, monkeypatch)
    runtime = dispatch._runtime()
    entered = threading.Event()
    release = threading.Event()

    class SlowLauncher(runtime[3].AgentLauncher):
        def start(self, name, **kwargs):
            result = super().start(name, **kwargs)
            entered.set()
            assert release.wait(15)
            return result

    monkeypatch.setattr(dispatch, "_runtime", lambda: (*runtime[:3],
                        SimpleNamespace(AgentLauncher=SlowLauncher,
                                        AgentProcessRegistry=runtime[3].AgentProcessRegistry), runtime[4]))
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


def _other_profile(tmp_path, config):
    second_profile = tmp_path / "other" / "profile.json"
    second_profile.parent.mkdir()
    second_profile.write_bytes(Path(config["session_profile_path"]).read_bytes())
    # The lexical alias must still select the same canonical SDK workroot.
    return {**config, "session_profile_path": str(second_profile),
            "agent_launcher_work_root": str(tmp_path) + "/."}


def test_capacity_root_is_bound_to_os_user_not_environment(tmp_path, monkeypatch):
    expected = session_boot._capacity_root()
    for name in ("HOME", "USERPROFILE", "LOCALAPPDATA"):
        monkeypatch.setenv(name, str(tmp_path))
    assert session_boot._capacity_root() == expected


@pytest.mark.parametrize("separate_workroot", [False, True])
def test_cross_profile_parallel_claims_share_real_file_reservation(
        tmp_path, monkeypatch, separate_workroot):
    first, calls, config_a = _fixture(tmp_path, monkeypatch)
    monkeypatch.delenv("TM_SESSION_PROFILE")
    config_b = _other_profile(tmp_path, config_a)
    if separate_workroot:
        other_root = tmp_path / "other-root"
        other_root.mkdir()
        config_b["agent_launcher_work_root"] = str(other_root)
    second = session_boot.companion_claim("other-session", config=config_b)
    assert session_boot._ledger_dir(session_boot.session_profile.effective_settings(config_a), config_a) == (
        session_boot._ledger_dir(session_boot.session_profile.effective_settings(config_b), config_b))
    runtime = dispatch._runtime()
    entered, release = threading.Event(), threading.Event()

    class SlowLauncher(runtime[3].AgentLauncher):
        def start(self, name, **kwargs):
            entered.set()  # Before the SDK publishes its registry record.
            assert release.wait(15)
            return super().start(name, **kwargs)

    sdk = SimpleNamespace(AgentLauncher=SlowLauncher,
                          AgentProcessRegistry=runtime[3].AgentProcessRegistry)
    monkeypatch.setattr(dispatch, "_runtime", lambda: (*runtime[:3], sdk,
                        SimpleNamespace(FileLock=FileLock)))
    monkeypatch.setitem(sys.modules, "agent_launcher", sdk)
    first_admission = tmp_path / "first.admission.json"
    second_admission = tmp_path / "second.admission.json"
    first_admission.write_text(json.dumps(_admission(first)), encoding="utf-8")
    second_admission.write_text(json.dumps(_admission(second)), encoding="utf-8")
    with ThreadPoolExecutor(max_workers=2) as pool:
        first_run = pool.submit(dispatch.dispatch_companion, first["session_id"],
                                first_admission, config=config_a)
        assert entered.wait(15), "valid first dispatch never reached SDK"
        with pytest.raises(ValueError, match="reservation unavailable"):
            dispatch.dispatch_companion(second["session_id"], second_admission,
                                        config=config_b)
        assert calls == []  # The second claim did not enter the SDK.
        release.set()
        assert first_run.result(timeout=20)["status"] == "accepted"
    assert len(calls) == 1
    assert session_boot.companion_claim(first["session_id"], config=config_b)["status"] == "accepted"
    prior = tmp_path / ".ticket-master-companion" / f"{first['session_id']}.dispatch-attempt"
    record = json.loads(prior.read_text(encoding="utf-8"))
    record["created_at"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    prior.write_text(json.dumps(record), encoding="utf-8")
    second_admission.write_text(json.dumps(_admission(second)), encoding="utf-8")
    with pytest.raises(ValueError, match="no attested companion slot"):
        dispatch.dispatch_companion(second["session_id"], second_admission,
                                    config=config_b)
    assert len(calls) == 1


@pytest.mark.parametrize("separate_workroot", [False, True])
def test_cross_profile_uncertain_attempt_remains_visible(
        tmp_path, monkeypatch, separate_workroot):
    first, calls, config_a = _fixture(tmp_path, monkeypatch)
    monkeypatch.delenv("TM_SESSION_PROFILE")
    config_b = _other_profile(tmp_path, config_a)
    if separate_workroot:
        other_root = tmp_path / "other-root"
        other_root.mkdir()
        config_b["agent_launcher_work_root"] = str(other_root)
    second = session_boot.companion_claim("other-session", config=config_b)
    runtime = dispatch._runtime()

    class UncertainLauncher(runtime[3].AgentLauncher):
        def start(self, name, **kwargs):
            calls.append((name, kwargs))
            raise RuntimeError("unknown before registry publication")

    sdk = SimpleNamespace(AgentLauncher=UncertainLauncher,
                          AgentProcessRegistry=runtime[3].AgentProcessRegistry)
    monkeypatch.setattr(dispatch, "_runtime", lambda: (*runtime[:3], sdk,
                        SimpleNamespace(FileLock=FileLock)))
    first_file = tmp_path / "first.admission.json"
    second_file = tmp_path / "second.admission.json"
    first_file.write_text(json.dumps(_admission(first)), encoding="utf-8")
    assert dispatch.dispatch_companion(first["session_id"], first_file,
                                       config=config_a)["status"] == "pending-reconcile"
    assert len(calls) == 1
    prior = tmp_path / ".ticket-master-companion" / f"{first['session_id']}.dispatch-attempt"
    record = json.loads(prior.read_text(encoding="utf-8"))
    record["created_at"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    prior.write_text(json.dumps(record), encoding="utf-8")
    second_file.write_text(json.dumps(_admission(second)), encoding="utf-8")
    with pytest.raises(ValueError, match="cannot be reconciled"):
        dispatch.dispatch_companion(second["session_id"], second_file,
                                    config=config_b)
    assert len(calls) == 1


def test_previous_workroot_ledger_requires_reconciliation(tmp_path, monkeypatch):
    _, calls, config = _fixture(tmp_path, monkeypatch)
    other_root = tmp_path / "other-root"
    old_ledger = other_root / ".ticket-master-companion"
    old_ledger.mkdir(parents=True)
    (old_ledger / "prior.dispatch-attempt").write_text("uncertain", encoding="utf-8")
    config["agent_launcher_work_root"] = str(other_root)
    with pytest.raises(ValueError, match="workroot companion ledger requires reconciliation"):
        session_boot.companion_claim("other-session", config=config)
    assert calls == []


@pytest.mark.parametrize("root", ["relative-work-root", "missing-work-root"])
def test_unresolved_sdk_workroot_never_creates_a_separate_claim_ledger(
        tmp_path, monkeypatch, root):
    _, calls, config = _fixture(tmp_path, monkeypatch)
    config["agent_launcher_work_root"] = root if root.startswith("relative") else str(tmp_path / root)
    with pytest.raises(ValueError, match="work root"):
        session_boot.companion_claim("other-session", config=config)
    assert calls == []
