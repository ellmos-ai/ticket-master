"""BACH task-lease coupling of ticket claims (T-20261115-427953604). No network: a fake lead."""

import json
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

import bach_lease  # noqa: E402
import ticket_mover  # noqa: E402
import ticket_note  # noqa: E402
from queue_helpers import verified_queue  # noqa: E402

SECRET = "tok-SECRET-do-not-print"
TID = "T-20261115-111111111"


class FakeLead:
    """In-memory stand-in for the task DB: idempotent create, 409 on a foreign lease, renew, release."""

    def __init__(self):
        self.tasks = {}  # source -> id
        self.leases = {}  # task_id -> {"worker_id", "lease_id", "fence"}
        self.calls = []
        self.down = False
        self.release_status = 200
        self.release_body = None

    def __call__(self, method, url, headers, body, timeout):
        assert headers["Authorization"] == "Bearer " + SECRET
        if self.down:
            raise bach_lease.LeaseUnavailableError("lead not reachable (TimeoutError)")
        path = url.split("://", 1)[1].split("/", 1)[1]
        data = json.loads(body) if body else {}
        self.calls.append((method, "/" + path, data))
        if path == "api/tasks":
            task_id = self.tasks.setdefault(data["source"], 4000 + len(self.tasks))
            return 200, {"id": task_id, "already_present": True}
        parts = path.split("/")  # api tasks <id> lease [action]
        task_id = int(parts[2])
        action = parts[4] if len(parts) > 4 else "acquire"
        held = self.leases.get(task_id)
        if action == "acquire":
            if held and held["worker_id"] != data["worker_id"]:
                return 409, {"error": "leased", "holder": held["worker_id"], "lease_id": "x", "fence": 1}
            lease = held or {"worker_id": data["worker_id"], "lease_id": str(uuid.uuid4()), "fence": 7}
            self.leases[task_id] = lease
            return 200, dict(lease)
        if not held or held["lease_id"] != data["lease_id"]:
            return 409, {"error": "not holder"}
        if action == "renew":
            return 200, {"fence": held["fence"] + 1}
        if action == "release":
            if self.release_status == 200:
                del self.leases[task_id]
            ack = {"released": True, "task_id": task_id, "fence": held["fence"],
                   "outcome": data["outcome"], "status": bach_lease._RELEASE_STATUSES[data["outcome"]],
                   "server_now": "2026-11-17T07:00:00Z"}
            body = self.release_body if self.release_body is not None else (
                ack if self.release_status == 200 else {})
            return self.release_status, body
        raise AssertionError(path)

    def outcomes(self):
        return [c[2]["outcome"] for c in self.calls if c[1].endswith("/release")]


@pytest.fixture
def lead(monkeypatch, tmp_path):
    fake = FakeLead()
    cfg = {"bach_lease": {"enabled": True, "lead_url": "http://lead.invalid:1", "state_file": str(tmp_path / "state" / "leases.json")}}
    monkeypatch.setattr(bach_lease, "load_config", lambda config=None, _o=bach_lease.load_config: _o(cfg if config is None else config))
    monkeypatch.setattr(bach_lease, "_urllib_transport", fake)
    monkeypatch.setenv("TICKET_MASTER_BACH_TOKEN", SECRET)
    monkeypatch.setattr(ticket_mover.socket, "gethostname", lambda: "ASUS-GEI")
    return fake


def make_slot(root: Path):
    (root / "_config-state" / "snapshots").mkdir(parents=True)
    (root / "_config-state" / "snapshots" / "laptop.json").write_text(json.dumps({"host": "ASUS-GEI", "slot": "laptop"}), encoding="utf-8")
    (root / "laptop").mkdir()
    (root / "laptop" / "repos.json").write_text(json.dumps({"host": "ASUS-GEI", "slot": "laptop"}), encoding="utf-8")


def make_ticket(root: Path, folder="ACTIONABLE", name=f"{TID}.txt") -> Path:
    queue = verified_queue(root / "tickets")
    (queue / folder).mkdir(parents=True, exist_ok=True)
    path = queue / folder / name
    path.write_text("TICKET\nTITEL:         Beispiel\nSTATUS: ACTIONABLE\n", encoding="utf-8")
    return path


def claim(tmp_path, ticket):
    make_slot(tmp_path) if not (tmp_path / "laptop").exists() else None
    return ticket_mover.claim_current_host(ticket, claim_host="ASUS-GEI", sync_root=tmp_path)


def test_disabled_by_default_never_touches_network(tmp_path, monkeypatch):
    def boom(*_a, **_k):
        raise AssertionError("network used while disabled")

    monkeypatch.setattr(bach_lease, "_urllib_transport", boom)
    assert bach_lease.load_config({})["enabled"] is False
    assert bach_lease.claim_lease(make_ticket(tmp_path), host="ASUS-GEI", config={}) is False
    assert bach_lease.release_lease(f"{TID}.txt", "done", config={}) is None
    assert bach_lease.renew_leases(config={}) == []


def test_claim_takes_lease_then_renames_and_is_idempotent(tmp_path, lead):
    claimed = claim(tmp_path, make_ticket(tmp_path))
    assert claimed.name == f"{TID}.ASUS-GEI.txt"
    create = [c for c in lead.calls if c[1] == "/api/tasks"][0][2]
    assert create["source"] == f"ticket:{TID}:ticket-master" and create["assigned_to"] == "TICKET-MASTER"
    assert create["created_by"] == "ticket-master@ASUS-GEI" and create["title"].startswith(TID)
    cache = json.loads((tmp_path / "state" / "leases.json").read_text(encoding="utf-8"))
    assert set(cache[TID]) >= {"task_id", "lease_id", "fence"}
    again = claim(tmp_path, claimed)  # same host, already claimed: renews instead of a second create
    assert again == claimed
    assert sum(1 for c in lead.calls if c[1] == "/api/tasks") == 1
    assert lead.calls[-1][1].endswith("/lease/renew")


def test_409_refuses_the_claim_and_names_the_holder(tmp_path, lead):
    ticket = make_ticket(tmp_path)
    lead.tasks[f"ticket:{TID}:ticket-master"] = 4000
    lead.leases[4000] = {"worker_id": "ticket-master@WORKSTATION-LG", "lease_id": "other", "fence": 3}
    with pytest.raises(bach_lease.LeaseConflictError) as err:
        claim(tmp_path, ticket)
    assert "ticket-master@WORKSTATION-LG" in str(err.value)
    assert "other" not in str(err.value).replace("another", "")  # no lease_id in the message
    assert ticket.is_file() and not (ticket.parent / f"{TID}.ASUS-GEI.txt").exists()


def test_unreachable_lead_is_fail_closed_unless_fail_open(tmp_path, lead, monkeypatch, capsys):
    ticket = make_ticket(tmp_path)
    lead.down = True
    with pytest.raises(bach_lease.LeaseUnavailableError):
        claim(tmp_path, ticket)
    assert ticket.is_file()
    cfg = dict(bach_lease.load_config(), fail_open=True)
    monkeypatch.setattr(bach_lease, "load_config", lambda config=None, _c=cfg: _c)
    assert claim(tmp_path, ticket).name == f"{TID}.ASUS-GEI.txt"
    assert "fail_open" in capsys.readouterr().err


def test_fail_open_never_overrides_a_real_conflict(tmp_path, lead, monkeypatch):
    lead.tasks[f"ticket:{TID}:ticket-master"] = 4000
    lead.leases[4000] = {"worker_id": "ticket-master@OTHER", "lease_id": "o", "fence": 1}
    cfg = dict(bach_lease.load_config(), fail_open=True)
    monkeypatch.setattr(bach_lease, "load_config", lambda config=None, _c=cfg: _c)
    with pytest.raises(bach_lease.LeaseConflictError):
        claim(tmp_path, make_ticket(tmp_path))


def test_missing_token_is_unavailable(tmp_path, lead, monkeypatch):
    monkeypatch.delenv("TICKET_MASTER_BACH_TOKEN")
    with pytest.raises(bach_lease.LeaseUnavailableError, match="no lease token"):
        claim(tmp_path, make_ticket(tmp_path))


def test_token_never_leaks_into_output_or_errors(tmp_path, lead, capsys):
    lead.down = True
    with pytest.raises(bach_lease.LeaseUnavailableError) as err:
        claim(tmp_path, make_ticket(tmp_path))
    out = capsys.readouterr()
    assert SECRET not in str(err.value) and SECRET not in out.out + out.err


def test_failed_rename_hands_the_lease_back(tmp_path, lead):
    ticket = make_ticket(tmp_path)
    (ticket.parent / f"{TID}.ASUS-GEI.txt").write_text("squatter", encoding="utf-8")
    with pytest.raises(ticket_mover.TicketCollisionError):
        claim(tmp_path, ticket)
    assert lead.outcomes() == ["return"]
    assert lead.calls[-1][2]["note"] == "ticket_claim_rollback"
    assert not (tmp_path / "state" / "leases.json").read_text(encoding="utf-8").count(TID)


@pytest.mark.parametrize(("dest", "outcome"), [("SOLVED", "done"), ("BLOCKED", "blocked"), ("WAITING", "return")])
def test_move_releases_with_matching_outcome(tmp_path, lead, dest, outcome):
    claimed = claim(tmp_path, make_ticket(tmp_path))
    target = ticket_mover.move_ticket(claimed, dest)
    assert target.parent.name == dest
    assert lead.outcomes() == [outcome]
    release = [c for c in lead.calls if c[1].endswith("/release")][0][2]
    assert release["result_ref"] == f"{dest}/{target.name}"
    assert release["note"] == f"ticket_moved:{dest.lower()}"
    assert "ticket-master@" not in release["note"]
    assert TID not in json.loads((tmp_path / "state" / "leases.json").read_text(encoding="utf-8"))


def test_move_inside_working_folders_keeps_the_lease(tmp_path, lead):
    claimed = claim(tmp_path, make_ticket(tmp_path))
    ticket_mover.move_ticket(claimed, "QUEUED")
    assert lead.outcomes() == []


def test_release_session_returns_the_lease(tmp_path, lead):
    claimed = claim(tmp_path, make_ticket(tmp_path))
    freed, refused, _held = ticket_mover.release_claims(claimed.parent.parent, host="ASUS-GEI", report_refused=True)
    assert [p.name for p in freed] == [f"{TID}.txt"] and not refused
    assert lead.outcomes() == ["return"]
    assert lead.calls[-1][2]["note"] == "ticket_claim_released"


def test_release_failure_only_warns_and_keeps_the_move(tmp_path, lead, capsys):
    claimed = claim(tmp_path, make_ticket(tmp_path))
    lead.down = True
    target = ticket_mover.move_ticket(claimed, "SOLVED")
    assert target.is_file()
    assert "not released" in capsys.readouterr().err


def test_ticket_without_cached_lease_causes_no_network_on_move(tmp_path, lead):
    ticket = make_ticket(tmp_path, folder="QUEUED", name=f"{TID}.ASUS-GEI.txt")
    ticket_mover.move_ticket(ticket, "SOLVED")
    assert lead.calls == []


def test_state_file_inside_onedrive_is_refused():
    with pytest.raises(bach_lease.LeaseUnavailableError, match="OneDrive"):
        bach_lease.state_path({"state_file": "<HOME>/OneDrive/x/leases.json"})


def test_renew_leases_and_note_heartbeat(tmp_path, lead):
    claimed = claim(tmp_path, make_ticket(tmp_path))
    assert bach_lease.renew_leases() == [(TID, True, "renewed")]
    before = len(lead.calls)
    ticket_note.update_ticket_note(claimed, verlauf="2026-11-15 | test | Notiz")
    assert len(lead.calls) == before + 1 and lead.calls[-1][1].endswith("/lease/renew")
    other = make_ticket(tmp_path, folder="QUEUED", name="T-20261115-222222222.ASUS-GEI.txt")
    ticket_note.update_ticket_note(other, verlauf="2026-11-15 | test | ohne Lease")
    assert len(lead.calls) == before + 1  # no cache entry -> no network


def test_cli_renew_leases(tmp_path, lead, capsys):
    claim(tmp_path, make_ticket(tmp_path))
    assert ticket_mover._cli(["--renew-leases"]) == 0
    assert "RENEWED" in capsys.readouterr().out


def test_lead_url_must_be_http():
    with pytest.raises(bach_lease.LeaseUnavailableError, match="http"):
        bach_lease.BachLeaseClient({"lead_url": "file:///etc/passwd"})


# --- follow-ups of the review of #64 (T-20261115-529774551) -------------------------------


def _scripted(monkeypatch, answers):
    """Replace the transport by a script: answers is a list of (status, body); records the paths."""
    seen = []
    queue = list(answers)

    def transport(method, url, headers, body, timeout):
        seen.append(url.split("://", 1)[1].split("/", 1)[1])
        return queue.pop(0)

    monkeypatch.setattr(bach_lease, "_urllib_transport", transport)
    return seen


def test_409_on_task_create_is_fail_closed_even_with_fail_open(tmp_path, lead, monkeypatch):
    cfg = dict(bach_lease.load_config(), fail_open=True)
    monkeypatch.setattr(bach_lease, "load_config", lambda config=None, _c=cfg: _c)
    seen = _scripted(monkeypatch, [(409, {"error": "task exists", "holder": "ticket-master@OTHER"})])
    ticket = make_ticket(tmp_path)
    with pytest.raises(bach_lease.LeaseConflictError, match="ticket-master@OTHER"):
        claim(tmp_path, ticket)
    assert seen == ["api/tasks"]  # no lease request after the refused create
    assert ticket.is_file() and not (ticket.parent / f"{TID}.ASUS-GEI.txt").exists()


@pytest.mark.parametrize("bad", ["../evil", "12/../x", "1 2", "", "-3", 0, -1, True, 1.5, None, [7], "٣"])
def test_unusable_task_id_from_the_lead_never_reaches_a_url(tmp_path, lead, monkeypatch, bad):
    seen = _scripted(monkeypatch, [(200, {"id": bad})])
    with pytest.raises(bach_lease.LeaseUnavailableError):
        claim(tmp_path, make_ticket(tmp_path))
    assert seen == ["api/tasks"]


def test_task_id_as_decimal_string_or_int_is_accepted(tmp_path, lead, monkeypatch):
    for index, good in enumerate(("4144", 4144)):
        sub = tmp_path / f"run{index}"
        sub.mkdir()
        (tmp_path / "state" / "leases.json").unlink(missing_ok=True)  # fresh cache: acquire, not renew
        seen = _scripted(monkeypatch, [(200, {"id": good}), (200, {"lease_id": "l", "fence": 1})])
        claim(sub, make_ticket(sub))
        assert seen[-1] == "api/tasks/4144/lease"


def test_corrupt_cache_entry_is_not_used_in_a_url(tmp_path, lead, monkeypatch):
    state = tmp_path / "state" / "leases.json"
    state.parent.mkdir(parents=True)
    state.write_text(json.dumps({TID: {"task_id": "../x", "lease_id": "l", "fence": 1}}), encoding="utf-8")
    seen = _scripted(monkeypatch, [])
    assert bach_lease.renew_leases() == [(TID, False, "not renewed (no cache entry or lead refused)")]
    warning = bach_lease.release_lease(f"{TID}.txt", "done")
    assert "unusable task id" in warning and seen == []
    assert TID not in json.loads(state.read_text(encoding="utf-8"))


@pytest.mark.parametrize("url", ["http://127.0.0.1:8000", "http://localhost:8000", "http://[::1]:8000",
                                 "http://100.64.0.1:8000", "http://100.119.69.90:8000",
                                 "http://100.127.255.254", "https://lead.example.org"])
def test_no_warning_for_loopback_overlay_or_https(url):
    assert bach_lease._plaintext_lead_warning(url) is None


@pytest.mark.parametrize("url", ["http://lead.example.org:8000", "http://192.168.1.5", "http://100.128.0.1",
                                 "http://8.8.8.8", "http://100.63.255.255"])
def test_warning_for_plain_http_over_other_networks(url):
    assert "clear text" in bach_lease._plaintext_lead_warning(url)


def test_client_prints_the_plaintext_warning_but_still_works(capsys):
    client = bach_lease.BachLeaseClient({"lead_url": "http://lead.example.org:8000"})
    assert client.lead == "http://lead.example.org:8000"
    assert "clear text" in capsys.readouterr().err
    bach_lease.BachLeaseClient({"lead_url": "http://127.0.0.1:8000"})
    assert capsys.readouterr().err == ""


def _cached(tmp_path):
    return json.loads((tmp_path / "state" / "leases.json").read_text(encoding="utf-8"))


def _native_denial(reason, **changes):
    return {"granted": False, "task_id": 4000, "reason": reason,
            "server_now": "2026-11-17T07:00:00Z", **changes}


def test_release_without_context_records_missing_reason_and_confirms_ack(tmp_path, lead):
    claimed = claim(tmp_path, make_ticket(tmp_path))
    assert bach_lease.release_lease(claimed.name, "return") is None
    payload = lead.calls[-1][2]
    assert payload["note"] == "reason_not_provided"
    assert payload["outcome"] == "return"
    assert TID not in _cached(tmp_path)


@pytest.mark.parametrize("status, body", [
    (409, _native_denial("completion_guard")),
    (409, _native_denial("stale_task_version")),
    (409, _native_denial(SECRET, detail=SECRET)),
    (409, _native_denial("expired", task_id=4001)),
    (409, _native_denial("expired", server_now="invalid")),
    (409, {"reason": "expired"}),
    (404, {"detail": SECRET}),
    (410, {}),
    (200, {}),
])
def test_unconfirmed_release_preserves_capability_and_redacts_warning(tmp_path, lead, status, body):
    claimed = claim(tmp_path, make_ticket(tmp_path))
    before = _cached(tmp_path)
    lead.release_status, lead.release_body = status, body
    warning = bach_lease.release_lease(claimed.name, "return", reason_code="ticket_claim_released")
    assert warning and "not released" in warning and SECRET not in warning
    assert _cached(tmp_path) == before


@pytest.mark.parametrize("change", [
    {"task_id": 4001}, {"fence": 8}, {"fence": True}, {"released": False},
    {"outcome": "done"}, {"status": "done"}, {"server_now": "2026-11-17T07:00:00"},
])
def test_success_ack_must_match_requested_release(tmp_path, lead, change):
    claimed = claim(tmp_path, make_ticket(tmp_path))
    before = _cached(tmp_path)
    lead.release_body = {"released": True, "task_id": 4000, "fence": 7,
                         "outcome": "return", "status": "pending",
                         "server_now": "2026-11-17T07:00:00Z", **change}
    assert bach_lease.release_lease(claimed.name, "return")
    assert _cached(tmp_path) == before


@pytest.mark.parametrize("reason", ["expired", "stale_fence"])
def test_confirmed_native_invalidation_clears_old_capability_without_claiming_release(tmp_path, lead, reason):
    claimed = claim(tmp_path, make_ticket(tmp_path))
    lead.release_status, lead.release_body = 409, _native_denial(reason)
    warning = bach_lease.release_lease(claimed.name, "return")
    assert warning and "no longer valid" in warning and "outcome was not applied" in warning
    assert TID not in _cached(tmp_path)


@pytest.mark.parametrize("kwargs", [
    {"reason_code": SECRET},
    {"reason_code": "ticket_moved", "reason_detail": SECRET},
    {"reason_code": "ticket_claim_released", "reason_detail": "PARKED"},
])
def test_release_reason_metadata_is_closed_before_http(tmp_path, lead, kwargs):
    claimed = claim(tmp_path, make_ticket(tmp_path))
    before = _cached(tmp_path)
    count = len(lead.calls)
    warning = bach_lease.release_lease(claimed.name, "return", **kwargs)
    assert warning and SECRET not in warning
    assert len(lead.calls) == count and _cached(tmp_path) == before


@pytest.mark.parametrize("response", ["success", "expired", "stale_fence"])
@pytest.mark.parametrize("changed", [
    {"fence": 8}, {"lease_id": "replacement-capability"},
    {"task_id": 4999}, {"worker_id": "ticket-master@REPLACEMENT"},
])
def test_delayed_release_does_not_remove_replacement_capability(tmp_path, lead, monkeypatch, response, changed):
    claimed = claim(tmp_path, make_ticket(tmp_path))
    expected = dict(_cached(tmp_path)[TID], **changed)
    if response != "success":
        lead.release_status, lead.release_body = 409, _native_denial(response)

    def transport(*args):
        reply = lead(*args)
        current = _cached(tmp_path)
        current[TID] = expected
        bach_lease.save_state(bach_lease.load_config(), current)
        return reply

    monkeypatch.setattr(bach_lease, "_urllib_transport", transport)
    warning = bach_lease.release_lease(claimed.name, "return")
    assert (warning is None) == (response == "success")
    assert _cached(tmp_path)[TID] == expected


@pytest.mark.parametrize("response", ["success", "expired", "stale_fence"])
def test_release_keeps_independent_cache_edits_during_http(tmp_path, lead, monkeypatch, response):
    claimed = claim(tmp_path, make_ticket(tmp_path))
    cfg = bach_lease.load_config()
    old = _cached(tmp_path)[TID]
    bach_lease.save_state(cfg, {TID: old, "removed": dict(old), "updated": dict(old)})
    if response != "success":
        lead.release_status, lead.release_body = 409, _native_denial(response)
    edited = dict(old, fence=90)

    def transport(*args):
        reply = lead(*args)
        bach_lease.save_state(cfg, {TID: old, "updated": edited, "added": edited})
        return reply

    monkeypatch.setattr(bach_lease, "_urllib_transport", transport)
    bach_lease.release_lease(claimed.name, "return")
    assert _cached(tmp_path) == {"updated": edited, "added": edited}


@pytest.mark.parametrize("operation", ["acquire", "renew"])
def test_delayed_acquire_or_renew_preserves_other_ticket_writes(tmp_path, lead, operation):
    cfg = bach_lease.load_config()
    client = bach_lease.BachLeaseClient(cfg, transport=lead)
    if operation == "renew":
        client.acquire(TID, "first")
    other = "T-20261117-222222222"
    other_client = bach_lease.BachLeaseClient(cfg, transport=lead)

    def transport(*args):
        reply = lead(*args)
        if args[1].endswith("/lease") or args[1].endswith("/renew"):
            other_client.acquire(other, "parallel")
        return reply

    client._transport = transport
    if operation == "renew":
        assert client.renew(TID)
    else:
        client.acquire(TID, "first")
    assert set(_cached(tmp_path)) == {TID, other}


@pytest.mark.parametrize("operation", ["acquire", "renew"])
def test_delayed_acquire_or_renew_does_not_overwrite_replacement(tmp_path, lead, operation):
    cfg = bach_lease.load_config()
    client = bach_lease.BachLeaseClient(cfg, transport=lead)
    if operation == "renew":
        client.acquire(TID, "first")
    replacement = {"task_id": 4000, "lease_id": "new-capability", "fence": 99, "worker_id": client.worker_id}

    def transport(*args):
        reply = lead(*args)
        if args[1].endswith("/lease") or args[1].endswith("/renew"):
            bach_lease.save_state(cfg, {TID: replacement})
        return reply

    client._transport = transport
    with pytest.raises(bach_lease.LeaseUnavailableError, match="current capability retained"):
        client.renew(TID) if operation == "renew" else client.acquire(TID, "first")
    assert _cached(tmp_path) == {TID: replacement}


def test_concurrent_acquires_merge_distinct_tickets(tmp_path, lead):
    cfg = bach_lease.load_config()
    tickets = [TID, "T-20261117-333333333"]
    barrier = threading.Barrier(2)

    def transport(method, url, headers, body, timeout):
        data = json.loads(body)
        if url.endswith("/api/tasks"):
            index = tickets.index(data["source"].split(":")[1])
            return 200, {"id": 4000 + index}
        barrier.wait(timeout=3)
        return 200, {"lease_id": "fake-" + url.rsplit("/", 2)[1], "fence": 7}

    def acquire(ticket_id):
        client = bach_lease.BachLeaseClient(cfg, transport=transport)
        return client.acquire(ticket_id, "concurrent")

    with ThreadPoolExecutor(max_workers=2) as executor:
        assert len(list(executor.map(acquire, tickets))) == 2
    assert set(_cached(tmp_path)) == set(tickets)


def test_cache_writers_coordinate_across_processes(tmp_path, lead):
    cfg = bach_lease.load_config()
    script = """
import json, sys
sys.path.insert(0, sys.argv[1])
import bach_lease
print("ready", flush=True)
bach_lease.save_state(json.loads(sys.argv[2]), {"child": {"fence": 8}})
print("written", flush=True)
"""
    process = None
    try:
        with bach_lease._state_write_lock(cfg):
            process = subprocess.Popen(
                [sys.executable, "-c", script, str(ROOT / "lib"), json.dumps(cfg)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            assert process.stdout.readline().strip() == "ready"
            with pytest.raises(subprocess.TimeoutExpired):
                process.wait(timeout=0.1)
        output, errors = process.communicate(timeout=5)
        assert process.returncode == 0, errors
        assert output.strip() == "written"
        assert _cached(tmp_path) == {"child": {"fence": 8}}
    finally:
        if process is not None and process.poll() is None:
            process.kill()
            process.communicate(timeout=5)
