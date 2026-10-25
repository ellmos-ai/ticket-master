"""Optional, claim-bound AgentLauncher child dispatch for a Ticket Master session.

The controlling agent supplies current management and quota observations.  This
adapter validates their binding and freshness; it does not discover native chat
slots or grant authority by itself.  The optional SDK is imported only here.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import math
import re
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

try:
    from . import session_boot, session_profile
except ImportError:
    import session_boot
    import session_profile


ADMISSION_SCHEMA = "ticket-master.companion-admission.v1"
ATTEMPT_SCHEMA = "ticket-master.companion-dispatch-attempt.v1"
MAX_AGE = timedelta(minutes=2)


def _stamp(value: Any, label: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a UTC timestamp")
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{label} must be a UTC timestamp") from exc
    if stamp.tzinfo is None or stamp.utcoffset() != timedelta(0):
        raise ValueError(f"{label} must be a UTC timestamp")
    return stamp


def _digest(data: Any) -> str:
    raw = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _evidence(admission: dict[str, Any], key: str, now: datetime) -> dict[str, Any]:
    row = admission.get(key)
    if not isinstance(row, dict) or not isinstance(row.get("readback"), dict):
        raise ValueError(f"{key} readback missing")
    if row.get("sha256") != _digest(row["readback"]):
        raise ValueError(f"{key} readback hash differs")
    checked = _stamp(row.get("checked_at"), f"{key}.checked_at")
    if checked > now or now - checked > MAX_AGE:
        raise ValueError(f"{key} readback is stale")
    return row["readback"]


def _positive_remainder(value: Any) -> bool:
    return (not isinstance(value, bool) and isinstance(value, (int, float))
            and math.isfinite(value) and 0 < value <= 100)


def _quota_has_budget(quota: dict[str, Any]) -> bool:
    """Every reported provider window is a limit, not an alternative budget."""
    values = []
    if "remaining_percent" in quota:
        values.append(quota["remaining_percent"])
    if "windows" in quota:
        windows = quota["windows"]
        if not isinstance(windows, dict) or not windows:
            return False
        for window in windows.values():
            if not isinstance(window, dict) or "remaining_percent" not in window:
                return False
            values.append(window["remaining_percent"])
    return bool(values) and all(_positive_remainder(value) for value in values)


def _validate_admission(admission: Any, claim: dict[str, Any], profile: dict[str, Any],
                        provider: str, model: str, now: datetime,
                        sdk_active: int = 0) -> None:
    if not isinstance(admission, dict) or admission.get("schema") != ADMISSION_SCHEMA:
        raise ValueError("companion admission schema missing")
    for key in ("session_id", "claim_id", "role"):
        if admission.get(key) != claim.get(key):
            raise ValueError(f"companion admission {key} differs from claim")
    if admission.get("provider") != provider or admission.get("model") != model:
        raise ValueError("companion admission differs from configured provider/model")
    if admission.get("model_class") != "low-medium":
        raise ValueError("companion model class is not attested as low/medium")
    issued = _stamp(admission.get("issued_at"), "admission.issued_at")
    expires = _stamp(admission.get("expires_at"), "admission.expires_at")
    if issued > now or expires <= now or expires - issued > MAX_AGE:
        raise ValueError("companion admission is stale or has invalid lifetime")

    management = _evidence(admission, "management", now)
    if admission["management"].get("source") != "collaboration.list_agents":
        raise ValueError("native management source missing")
    agents = management.get("agents")
    classes = admission.get("agent_classes")
    if not isinstance(agents, list) or not isinstance(classes, dict):
        raise ValueError("management agents or agent classification missing")
    names: set[str] = set()
    active_low = active_high = 0
    for agent in agents:
        if not isinstance(agent, dict) or not isinstance(agent.get("agent_name"), str):
            raise ValueError("invalid management agent row")
        name = agent["agent_name"]
        if name in names:
            raise ValueError("duplicate management agent")
        names.add(name)
        kind = classes.get(name)
        if kind not in {"low-medium", "high", "other"}:
            raise ValueError("management agent class unknown")
        status = agent.get("agent_status")
        if status == "running":
            active_low += kind == "low-medium"
            active_high += kind == "high"
        elif not (isinstance(status, dict) and set(status) == {"completed"}):
            raise ValueError("management agent status unknown")
    if set(classes) != names:
        raise ValueError("management classification does not cover the readback")
    slot = admission.get("slot")
    limit = profile["settings"]["companion"]["max_luna"]
    high_limit = profile["settings"]["companion"]["max_high"]
    if (not isinstance(slot, dict) or type(slot.get("limit")) is not int
            or not 0 < slot["limit"] <= limit
            or active_low + sdk_active >= slot["limit"]
            or active_high > high_limit):
        raise ValueError("no attested companion slot is available")

    quota = _evidence(admission, "quota", now)
    if admission["quota"].get("provider") != provider or quota.get("status") != "OK":
        raise ValueError("provider quota readback is not OK")
    quota_checked = _stamp(quota.get("checked_at"), "quota.readback.checked_at")
    if quota_checked > now or now - quota_checked > MAX_AGE:
        raise ValueError("provider quota measurement is stale")
    if not _quota_has_budget(quota):
        raise ValueError("provider quota has no positive measured remainder")
    gate = admission.get("task_gate")
    if not isinstance(gate, dict) or (
            gate.get("status") != "ready" or gate.get("role") != claim["role"]
            or gate.get("locks") != "clear" or gate.get("scope") != "authorized"
            or not isinstance(gate.get("source"), str) or not gate["source"].strip()
            or not isinstance(gate.get("task_ref"), str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", gate["task_ref"])):
        raise ValueError("task/lock/scope gate is not attested")


def _claim(session_id: str, directory: Path) -> dict[str, Any]:
    if not session_boot.SESSION_ID.fullmatch(session_id):
        raise ValueError("invalid session ID")
    path = directory / f"{session_id}.json"
    item = json.loads(path.read_text(encoding="utf-8"))
    if (not isinstance(item, dict) or item.get("schema") != "ticket-master.companion-claim.v2"
            or item.get("session_id") != session_id or item.get("role") not in session_boot.ROLES
            or not isinstance(item.get("claim_id"), str)
            or not re.fullmatch(r"[0-9a-f]{32}", item["claim_id"])
            or item.get("dispatch_name") != session_boot._dispatch_name(session_id, item["claim_id"])):
        raise ValueError("companion claim is invalid")
    return item


def _runtime() -> tuple[Any, Any, Any, Any, Any]:
    try:
        return (importlib.import_module("taskplan.config"),
                importlib.import_module("taskplan.runtime"),
                importlib.import_module("taskplan.workflows"),
                importlib.import_module("agent_launcher"),
                importlib.import_module("filelock"))
    except ImportError as exc:
        raise ValueError("optional TASKPLAN/AgentLauncher runtime unavailable") from exc


def _process_state(pid: Any, birth: Any) -> str:
    """Only a measured missing/reused PID proves that a prior child ended."""
    if (type(pid) is not int or pid <= 0 or isinstance(birth, bool)
            or not isinstance(birth, (int, float)) or not math.isfinite(birth)
            or birth <= 0):
        return "unknown"
    try:
        psutil = importlib.import_module("psutil")  # AgentLauncher's dependency
    except ImportError:
        return "unknown"
    try:
        process = psutil.Process(pid)
        if process.create_time() != float(birth):
            return "ended"
        return "live" if process.is_running() else "ended"
    except psutil.NoSuchProcess:
        return "ended"
    except (psutil.Error, OSError, ValueError):
        # AccessDenied and other probe errors cannot establish a free slot.
        return "unknown"


def _sdk_occupancy(directory: Path, registry_dirs: set[Path],
                   launcher_module: Any, attempts: dict[str, dict[str, Any]]) -> int:
    """Read existing SDK registries and claim ledger under the dispatch lock.

    Native chat management cannot see these OS children.  Missing/corrupt
    records and ambiguous attempts fail closed; no registry is modified.
    """
    registry_type = getattr(launcher_module, "AgentProcessRegistry", None)
    if registry_type is None:
        raise ValueError("agent-launcher registry API unavailable")
    claims: dict[str, dict[str, Any]] = {}
    for path in directory.glob("*.json"):
        try:
            item = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ValueError("companion claim ledger unreadable") from exc
        if (not isinstance(item, dict)
                or item.get("schema") != "ticket-master.companion-claim.v2"
                or not isinstance(item.get("session_id"), str)
                or not isinstance(item.get("claim_id"), str)
                or not isinstance(item.get("dispatch_name"), str)
                or item["dispatch_name"] != session_boot._dispatch_name(
                    item["session_id"], item["claim_id"])):
            raise ValueError("companion claim ledger invalid")
        claims[item["dispatch_name"]] = item
    names = set(attempts) | set(claims)
    record_sources: dict[str, set[Path]] = {}
    registries = []
    for registry_dir in registry_dirs:
        registry = registry_type(registry_dir)
        registries.append((registry_dir, registry))
        # list_all() deliberately omits identity failures.  Enumerate its
        # own registry records so a corrupt/uncertain tm-* child is not free.
        try:
            mode = registry_dir.stat().st_mode
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise ValueError("companion registry is unreadable") from exc
        if not stat.S_ISDIR(mode):
            raise ValueError("companion registry is not a directory")
        try:
            for path in registry_dir.iterdir():
                if path.name.startswith("tm-") and path.suffix == ".pid":
                    record_sources.setdefault(path.stem, set()).add(registry_dir)
        except OSError as exc:
            raise ValueError("companion registry is unreadable") from exc
        names.update(record_sources)
    live_ids: set[tuple[int, float]] = set()
    for name in names:
        if not re.fullmatch(r"tm-[0-9a-f]{32}", name):
            raise ValueError("companion registry name is invalid")
        rows = []
        for registry_dir, registry in registries:
            row = registry.load_metadata(name)
            if not row and registry_dir in record_sources.get(name, set()):
                raise ValueError("companion registry record is unreadable")
            if row:
                rows.append((registry, row))
        if len(rows) > 1 and any(row != rows[0][1] for _, row in rows[1:]):
            raise ValueError("companion registry identities conflict")
        if rows:
            registry, row = rows[0]
            session_id = row.get("ticket_master_session_id")
            claim_id = row.get("ticket_master_claim_id")
            role = row.get("ticket_master_role")
            if (row.get("name") != name or row.get("launch_state") != "active"
                    or not isinstance(session_id, str)
                    or not isinstance(claim_id, str)
                    or role not in session_boot.ROLES
                    or session_boot._dispatch_name(session_id, claim_id) != name):
                raise ValueError("companion registry identity is uncertain")
            claim = claims.get(name)
            if claim and (claim["session_id"] != session_id
                          or claim["claim_id"] != claim_id or claim["role"] != role):
                raise ValueError("companion registry differs from claim")
            pid, birth = row.get("pid"), row.get("process_create_time")
            if (type(pid) is not int or pid <= 0 or isinstance(birth, bool)
                    or not isinstance(birth, (int, float))
                    or not math.isfinite(birth) or birth <= 0):
                raise ValueError("companion process identity is uncertain")
            try:
                observed = registry.probe_running(name)
                stable = registry.load_metadata(name) == row
            except (AttributeError, OSError, ValueError) as exc:
                raise ValueError("companion process identity is unreadable") from exc
            if not stable:
                raise ValueError("companion process identity changed")
            if observed == pid:
                live_ids.add((pid, float(birth)))
            elif observed == 0:
                if _process_state(pid, birth) != "ended":
                    raise ValueError("companion process identity is uncertain")
            else:
                raise ValueError("companion process identity changed")
            continue
        claim = claims.get(name)
        evidence = claim.get("acceptance_evidence") if claim else None
        if isinstance(evidence, dict) and evidence.get("name") == name:
            pid, birth = evidence.get("pid"), evidence.get("process_create_time")
            state = _process_state(pid, birth)
            if state == "unknown":
                raise ValueError("companion acceptance process is uncertain")
            if state == "live":
                live_ids.add((pid, float(birth)))
        elif name in attempts:
            raise ValueError("prior companion start cannot be reconciled")
    return len(live_ids)


def dispatch_companion(session_id: str, admission_file: str | Path, *,
                       config: dict[str, Any] | None = None,
                       now: datetime | None = None) -> dict[str, Any]:
    """Attempt one SDK child for an existing pending claim; never start twice.

    An interrupted or ambiguous attempt is deliberately not retried.  The
    controlling agent must reconcile the existing native registry/process and
    pending claim before a new session can be considered.
    """
    now = now or datetime.now(timezone.utc)
    # Freeze the caller's root and registry choices before any SDK callback
    # can mutate its configuration. The OS-wide ledger remains separate.
    dispatch_config = dict(config or {})
    profile = session_profile.effective_settings(dispatch_config)
    if not profile["settings"]["companion"]["enabled"]:
        return {"status": "disabled"}
    work_root = session_boot._sdk_work_root(dispatch_config)
    directory = session_boot._ledger_dir(profile, dispatch_config)
    claim = _claim(session_id, directory)
    if claim["status"] == "accepted":
        return {"status": "accepted", "claim_id": claim["claim_id"]}
    if claim["status"] != "pending":
        raise ValueError("companion claim is not pending")
    admission = json.loads(Path(admission_file).read_text(encoding="utf-8"))

    taskplan_config, taskplan_runtime, workflows, launcher_module, filelock_module = _runtime()
    try:
        provider = taskplan_config.provider_name()
        role_profile = taskplan_runtime.runtime_profile(claim["role"], provider)
    except (AttributeError, KeyError, TypeError) as exc:
        raise ValueError("canonical TASKPLAN role resolver unavailable") from exc
    if not isinstance(role_profile, dict):
        raise ValueError("canonical TASKPLAN role profile invalid")
    model = role_profile.get("model")
    if (not isinstance(provider, str) or not provider
            or not isinstance(model, str) or not model.strip()
            or role_profile.get("role") != claim["role"]
            or role_profile.get("provider") != provider
            or not isinstance(role_profile.get("reasoning_effort"), str)
            or not role_profile["reasoning_effort"].strip()):
        raise ValueError("role provider/model is not explicitly configured")
    try:
        prompt = workflows.get_workflow_prompt_path(claim["role"].upper())
    except (AttributeError, KeyError, TypeError) as exc:
        raise ValueError("canonical TASKPLAN role prompt unavailable") from exc
    if not isinstance(prompt, Path):
        raise ValueError("canonical TASKPLAN role prompt is not a path")
    if not prompt.is_file() or not prompt.read_text(encoding="utf-8").strip():
        raise ValueError("canonical TASKPLAN role prompt unavailable")
    _validate_admission(admission, claim, profile, provider, model, now)
    registry_dir = dispatch_config.get("agent_launcher_registry_dir")
    if registry_dir is None:
        registry_dir = Path.home() / ".agent-launcher" / "registry"
    elif not isinstance(registry_dir, str) or not registry_dir:
        raise ValueError("invalid agent-launcher registry directory")
    # companion_claim() reads every *.json in this shared directory as a claim.
    attempt = directory / f"{session_id}.dispatch-attempt"
    global_lock = filelock_module.FileLock(str(directory / ".companion-dispatch.lock"))
    try:
        global_lock.acquire(timeout=0)
    except Exception as exc:
        raise ValueError("companion dispatch reservation unavailable") from exc
    try:
        claim = _claim(session_id, directory)
        if claim["status"] != "pending" or attempt.exists():
            return {"status": "pending-reconcile", "claim_id": claim["claim_id"]}
        # The measurement, not a newly issued wrapper, must follow every
        # earlier start attempt in this ledger.
        management_checked = _stamp(admission["management"]["checked_at"],
                                    "management.checked_at")
        quota_checked = _stamp(admission["quota"]["checked_at"], "quota.checked_at")
        quota_measured = _stamp(admission["quota"]["readback"]["checked_at"],
                                "quota.readback.checked_at")
        prior_attempts: dict[str, dict[str, Any]] = {}
        for prior in directory.glob("*.dispatch-attempt"):
            data = json.loads(prior.read_text(encoding="utf-8"))
            if (not isinstance(data, dict) or data.get("schema") != ATTEMPT_SCHEMA
                    or not isinstance(data.get("dispatch_name"), str)
                    or data.get("dispatch_name") in prior_attempts):
                raise ValueError("prior companion attempt is invalid")
            prior_attempts[data["dispatch_name"]] = data
            created = _stamp(data.get("created_at"), "attempt.created_at")
            if min(management_checked, quota_checked, quota_measured) <= created:
                raise ValueError("management admission predates another dispatch attempt")
        registry_dirs = {Path(registry_dir)}
        default_registry = Path.home() / ".agent-launcher" / "registry"
        if default_registry != Path(registry_dir):
            try:
                default_mode = default_registry.stat().st_mode
            except FileNotFoundError:
                pass
            except OSError as exc:
                raise ValueError("default companion registry is unreadable") from exc
            else:
                if not stat.S_ISDIR(default_mode):
                    raise ValueError("default companion registry is not a directory")
                registry_dirs.add(default_registry)
        sdk_active = _sdk_occupancy(directory, registry_dirs, launcher_module,
                                    prior_attempts)
        _validate_admission(admission, claim, profile, provider, model,
                            datetime.now(timezone.utc), sdk_active=sdk_active)
        launcher = launcher_module.AgentLauncher(Path(registry_dir), work_root)
        work_dir, launch_cwd = session_boot._sdk_start_paths(
            work_root, claim["dispatch_name"], launcher)
        record = {"schema": ATTEMPT_SCHEMA, "session_id": session_id,
                  "claim_id": claim["claim_id"], "dispatch_name": claim["dispatch_name"],
                  "admission_sha256": _digest(admission),
                  "sdk_work_root": str(work_root),
                  "expected_work_dir": str(work_dir), "expected_cwd": str(launch_cwd),
                  "created_at": datetime.now(timezone.utc).isoformat(), "status": "invoked-unknown"}
        with attempt.open("x", encoding="utf-8") as handle:
            json.dump(record, handle, ensure_ascii=False)
            handle.write("\n")
        try:
            result = launcher.start(
                claim["dispatch_name"], provider=provider, model=model,
                effort=str(role_profile.get("reasoning_effort") or ""),
                prompt_file=prompt,
                request=(f"Run exactly one authorized TASKPLAN {claim['role']} task "
                         f"for Ticket Master session {session_id}. Match the current "
                         f"selector result to preflight task reference "
                         f"{admission['task_gate']['task_ref']}; on mismatch report "
                         "and stop without taking another task. Then report and stop."),
                permission_mode="restricted", headless=True,
                extra_metadata={"ticket_master_session_id": session_id,
                                "ticket_master_claim_id": claim["claim_id"],
                                "ticket_master_role": claim["role"],
                                "ticket_master_work_root": str(work_root)},
            )
        except Exception as exc:
            return {"status": "pending-reconcile", "reason": f"launcher raised {type(exc).__name__}"}
        if (getattr(result, "ok", None) is not True
                or getattr(result, "name", None) != claim["dispatch_name"]
                or getattr(result, "already_running", None)):
            return {"status": "pending-reconcile", "reason": "launcher did not certify a new start"}
        # This is the existing native registry/PID/birth verifier, not a new
        # acceptance rule or an acceptance based on StartResult alone.
        try:
            accepted = session_boot.companion_ack(
                session_id, f"agent-launcher:{claim['dispatch_name']}",
                config=dispatch_config)
        except (ValueError, OSError) as exc:
            return {"status": "pending-reconcile", "reason": f"native ACK unavailable: {exc}"}
        return {"status": "accepted", "claim_id": claim["claim_id"],
                "acceptance_evidence": accepted["acceptance_evidence"]}
    finally:
        global_lock.release()
