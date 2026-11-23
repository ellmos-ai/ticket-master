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
            held["fence"] += 1
            return 200, {"fence": held["fence"]}
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
    monkeypatch.setenv("TICKET_MASTER_TICKETS_DIR", str(tmp_path / "tickets"))
    monkeypatch.setattr(ticket_mover.socket, "gethostname", lambda: "HOST-A")
    return fake


def make_slot(root: Path):
    (root / "_config-state" / "snapshots").mkdir(parents=True)
    (root / "_config-state" / "snapshots" / "laptop.json").write_text(json.dumps({"host": "HOST-A", "slot": "laptop"}), encoding="utf-8")
    (root / "laptop").mkdir()
    (root / "laptop" / "repos.json").write_text(json.dumps({"host": "HOST-A", "slot": "laptop"}), encoding="utf-8")


def make_ticket(root: Path, folder="ACTIONABLE", name=f"{TID}.txt") -> Path:
    queue = verified_queue(root / "tickets")
    (queue / folder).mkdir(parents=True, exist_ok=True)
    path = queue / folder / name
    path.write_text("TICKET\nTITEL:         Beispiel\nSTATUS: ACTIONABLE\n", encoding="utf-8")
    return path


def claim(tmp_path, ticket):
    make_slot(tmp_path) if not (tmp_path / "laptop").exists() else None
    return ticket_mover.claim_current_host(ticket, claim_host="HOST-A", sync_root=tmp_path)


def test_disabled_by_default_never_touches_network(tmp_path, monkeypatch):
    def boom(*_a, **_k):
        raise AssertionError("network used while disabled")

    monkeypatch.setattr(bach_lease, "_urllib_transport", boom)
    assert bach_lease.load_config({})["enabled"] is False
    assert bach_lease.claim_lease(make_ticket(tmp_path), host="HOST-A", config={}) is False
    assert bach_lease.release_lease(f"{TID}.txt", "done", config={}) is None
    assert bach_lease.renew_leases(config={}) == []


def test_claim_takes_lease_then_renames_and_is_idempotent(tmp_path, lead):
    claimed = claim(tmp_path, make_ticket(tmp_path))
    assert claimed.name == f"{TID}.HOST-A.txt"
    create = [c for c in lead.calls if c[1] == "/api/tasks"][0][2]
    assert create["source"] == f"ticket:{TID}:ticket-master" and create["assigned_to"] == "TICKET-MASTER"
    assert create["created_by"] == "ticket-master@HOST-A" and create["title"].startswith(TID)
    cache = json.loads((tmp_path / "state" / "leases.json").read_text(encoding="utf-8"))
    assert set(cache[TID]) >= {"task_id", "lease_id", "fence"}
    again = claim(tmp_path, claimed)  # same host, already claimed: renews instead of a second create
    assert again == claimed
    assert sum(1 for c in lead.calls if c[1] == "/api/tasks") == 1
    assert lead.calls[-1][1].endswith("/lease/renew")


def test_409_refuses_the_claim_and_names_the_holder(tmp_path, lead):
    ticket = make_ticket(tmp_path)
    lead.tasks[f"ticket:{TID}:ticket-master"] = 4000
    lead.leases[4000] = {"worker_id": "ticket-master@HOST-B", "lease_id": "other", "fence": 3}
    with pytest.raises(bach_lease.LeaseConflictError) as err:
        claim(tmp_path, ticket)
    assert "ticket-master@HOST-B" in str(err.value)
    assert "other" not in str(err.value).replace("another", "")  # no lease_id in the message
    assert ticket.is_file() and not (ticket.parent / f"{TID}.HOST-A.txt").exists()


def test_unreachable_lead_is_fail_closed_unless_fail_open(tmp_path, lead, monkeypatch, capsys):
    ticket = make_ticket(tmp_path)
    lead.down = True
    with pytest.raises(bach_lease.LeaseUnavailableError):
        claim(tmp_path, ticket)
    assert ticket.is_file()
    cfg = dict(bach_lease.load_config(), fail_open=True)
    monkeypatch.setattr(bach_lease, "load_config", lambda config=None, _c=cfg: _c)
    assert claim(tmp_path, ticket).name == f"{TID}.HOST-A.txt"
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
    (ticket.parent / f"{TID}.HOST-A.txt").write_text("squatter", encoding="utf-8")
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
    freed, refused, _held = ticket_mover.release_claims(claimed.parent.parent, host="HOST-A", report_refused=True)
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
    ticket = make_ticket(tmp_path, folder="QUEUED", name=f"{TID}.HOST-A.txt")
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
    other = make_ticket(tmp_path, folder="QUEUED", name="T-20261115-222222222.HOST-A.txt")
    ticket_note.update_ticket_note(other, verlauf="2026-11-15 | test | ohne Lease")
    assert len(lead.calls) == before + 1  # no cache entry -> no network


def test_cli_renew_leases(tmp_path, lead, capsys):
    claim(tmp_path, make_ticket(tmp_path))
    assert ticket_mover._cli(["--renew-leases"]) == 0
    assert "RENEWED" in capsys.readouterr().out


def test_cli_renew_leases_without_queue_root_renews_nothing(tmp_path, lead, capsys, monkeypatch):
    claim(tmp_path, make_ticket(tmp_path))
    before = len(lead.calls)
    monkeypatch.setattr(ticket_mover, "resolve_tickets_dir", lambda *a, **k: None)
    assert ticket_mover._cli(["--renew-leases", "--host", "HOST-A"]) == 1
    out = capsys.readouterr().out
    assert "HYGIENE ABORTED" in out and "RENEWED:" not in out
    assert not any(call[1].endswith("/lease/renew") for call in lead.calls[before:])


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
    assert ticket.is_file() and not (ticket.parent / f"{TID}.HOST-A.txt").exists()


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
                                 "http://100.64.0.1:8000", "http://100.100.100.100:8000",
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


# --- server mode: fallback visibility, backfill, cache hygiene & fast lane -----------------


def test_fail_open_fallback_writes_verlauf_note_and_pending_lease_without_secrets(tmp_path, lead, monkeypatch, capsys):
    from datetime import date
    import ticket_audit

    cfg = dict(bach_lease.load_config(), fail_open=True)
    monkeypatch.setattr(bach_lease, "load_config", lambda config=None, _c=cfg: _c)

    # 1) Unreachable lead (even if an exception message tried to leak URL + token)
    def leaky_down(*_a, **_k):
        raise bach_lease.LeaseUnavailableError(
            f"lead http://lead.invalid:1 down with Bearer {SECRET} ({SECRET})"
        )

    monkeypatch.setattr(bach_lease, "_urllib_transport", leaky_down)
    ticket = make_ticket(tmp_path)
    ticket.write_text(
        f"ID:            {TID}\n"
        "TITEL:         Fallback-Sichtbarkeit\n"
        "ERSTELLT:      2026-11-22\n"
        "STATUS:        ACTIONABLE\n\n"
        "VERLAUF / LOG\n"
        "--------------------------------------------------------------\n\n"
        "LOESUNG / ERGEBNIS\n"
        "--------------------------------------------------------------\n",
        encoding="utf-8",
    )
    claimed = claim(tmp_path, ticket)
    out = capsys.readouterr()
    text = claimed.read_text(encoding="utf-8")
    today = date.today().isoformat()
    expected_prefix = f"{today} | ticket-master@HOST-A | Claim ohne Lease (Fallback Dateibetrieb): "
    assert expected_prefix in text
    assert SECRET not in text and "lead.invalid" not in text
    assert SECRET not in out.out + out.err
    assert ticket_audit.commentary_lint(tmp_path / "tickets") == []
    cached = _cached(tmp_path)
    assert bach_lease.is_pending_lease_entry(cached[TID])
    assert cached[TID]["status"] == "pending_lease" and cached[TID]["pending_lease"] is True

    # 2) Missing token also records fallback note + pending_lease
    tid2 = "T-20261122-222222222"
    monkeypatch.setattr(bach_lease, "_urllib_transport", lead)
    monkeypatch.delenv("TICKET_MASTER_BACH_TOKEN")
    claimed2 = claim(tmp_path, make_ticket(tmp_path, name=f"{tid2}.txt"))
    text2 = claimed2.read_text(encoding="utf-8")
    assert f"{today} | ticket-master@HOST-A | Claim ohne Lease (Fallback Dateibetrieb): no lease token available" in text2
    assert bach_lease.is_pending_lease_entry(_cached(tmp_path)[tid2])


def test_fail_open_rollback_clears_pending_lease_when_rename_collides(tmp_path, lead, monkeypatch):
    cfg = dict(bach_lease.load_config(), fail_open=True)
    monkeypatch.setattr(bach_lease, "load_config", lambda config=None, _c=cfg: _c)
    lead.down = True
    ticket = make_ticket(tmp_path)
    before_text = ticket.read_text(encoding="utf-8")
    (ticket.parent / f"{TID}.HOST-A.txt").write_text("squatter", encoding="utf-8")
    with pytest.raises(ticket_mover.TicketCollisionError):
        claim(tmp_path, ticket)
    assert ticket.read_text(encoding="utf-8") == before_text
    assert TID not in _cached(tmp_path)


def test_409_is_never_treated_as_fallback_no_note_no_pending_lease(tmp_path, lead, monkeypatch):
    cfg = dict(bach_lease.load_config(), fail_open=True)
    monkeypatch.setattr(bach_lease, "load_config", lambda config=None, _c=cfg: _c)
    lead.tasks[f"ticket:{TID}:ticket-master"] = 4000
    lead.leases[4000] = {"worker_id": "ticket-master@HOST-B", "lease_id": "foreign", "fence": 2}
    ticket = make_ticket(tmp_path)
    before_text = ticket.read_text(encoding="utf-8")
    with pytest.raises(bach_lease.LeaseConflictError, match="ticket-master@HOST-B"):
        claim(tmp_path, ticket)
    assert ticket.is_file()
    assert not (ticket.parent / f"{TID}.HOST-A.txt").exists()
    assert ticket.read_text(encoding="utf-8") == before_text
    assert not (tmp_path / "state" / "leases.json").exists() or TID not in _cached(tmp_path)


def test_backfill_leases_leased_held_by_skipped_and_dry_run(tmp_path, lead, monkeypatch, capsys):
    tid_pending = "T-20261122-100000001"
    tid_uncached = "T-20261122-100000002"
    tid_conflict = "T-20261122-100000003"
    tid_already = "T-20261122-100000004"
    tid_foreign = "T-20261122-100000005"

    # Prepare pending_lease via fail_open while lead is down
    cfg_open = dict(bach_lease.load_config(), fail_open=True)
    monkeypatch.setattr(bach_lease, "load_config", lambda config=None, _c=cfg_open: _c)
    lead.down = True
    claim(tmp_path, make_ticket(tmp_path, folder="ACTIONABLE", name=f"{tid_pending}.txt"))
    capsys.readouterr()

    # Bring lead back up; create an uncached own claim in QUEUED, a foreign-held own claim,
    # an already-leased own claim, and another host's file claim (ignored).
    lead.down = False
    make_ticket(tmp_path, folder="QUEUED", name=f"{tid_uncached}.HOST-A.txt")
    make_ticket(tmp_path, folder="ACTIONABLE", name=f"{tid_conflict}.HOST-A.txt")
    lead.tasks[f"ticket:{tid_conflict}:ticket-master"] = 4500
    lead.leases[4500] = {"worker_id": "ticket-master@HOST-B", "lease_id": "held-elsewhere", "fence": 3}
    claim(tmp_path, make_ticket(tmp_path, folder="ACTIONABLE", name=f"{tid_already}.txt"))
    make_ticket(tmp_path, folder="ACTIONABLE", name=f"{tid_foreign}.HOST-B.txt")
    capsys.readouterr()

    queue_dir = str(tmp_path / "tickets")

    # Dry-run: previews via SKIPPED, zero HTTP calls, zero cache mutations
    calls_before = len(lead.calls)
    cache_before = _cached(tmp_path)
    assert ticket_mover._cli(["--backfill-leases", "--host", "HOST-A", "--tickets-dir", queue_dir, "--dry-run"]) == 0
    dry_lines = capsys.readouterr().out.strip().splitlines()
    assert len(lead.calls) == calls_before
    assert _cached(tmp_path) == cache_before
    assert any(line.startswith(f"{tid_pending}: SKIPPED dry-run") for line in dry_lines)
    assert any(line.startswith(f"{tid_uncached}: SKIPPED dry-run") for line in dry_lines)
    assert not any(tid_foreign in line for line in dry_lines)

    # Real backfill: acquires pending + uncached, reports HELD-BY on 409 without mutating, skips valid
    conflict_file = tmp_path / "tickets" / "ACTIONABLE" / f"{tid_conflict}.HOST-A.txt"
    conflict_before = conflict_file.read_bytes()
    assert ticket_mover._cli(["--backfill-leases", "--host", "HOST-A", "--tickets-dir", queue_dir]) == 0
    lines = capsys.readouterr().out.strip().splitlines()
    assert f"{tid_pending}: LEASED" in lines
    assert f"{tid_uncached}: LEASED" in lines
    assert f"{tid_conflict}: HELD-BY ticket-master@HOST-B" in lines
    assert any(line.startswith(f"{tid_already}: SKIPPED ") for line in lines)
    assert not any(tid_foreign in line for line in lines)

    # Verify cache & untouched conflict file
    after = _cached(tmp_path)
    assert bach_lease.has_lease_capability(after[tid_pending])
    assert bach_lease.has_lease_capability(after[tid_uncached])
    assert tid_conflict not in after
    assert conflict_file.read_bytes() == conflict_before


def test_backfill_leases_reports_failed_when_lead_down(tmp_path, lead, capsys):
    make_ticket(tmp_path, folder="ACTIONABLE", name=f"{TID}.HOST-A.txt")
    lead.down = True
    rc = ticket_mover._cli([
        "--backfill-leases", "--host", "HOST-A", "--tickets-dir", str(tmp_path / "tickets")
    ])
    out = capsys.readouterr().out
    assert rc == 1
    assert f"{TID}: FAILED lead not reachable" in out


def test_renew_leases_cache_hygiene_and_backfill(tmp_path, lead, capsys):
    tid_solved_valid = "T-20261122-300000001"
    tid_solved_invalid = "T-20261122-300000002"
    tid_blocked_valid = "T-20261122-300000003"
    tid_waiting_invalid = "T-20261122-300000004"
    tid_missing_valid = "T-20261122-300000005"
    tid_active = "T-20261122-300000006"
    tid_backfill = "T-20261122-300000007"

    c_solved_valid = claim(tmp_path, make_ticket(tmp_path, name=f"{tid_solved_valid}.txt"))
    c_solved_invalid = claim(tmp_path, make_ticket(tmp_path, name=f"{tid_solved_invalid}.txt"))
    c_blocked_valid = claim(tmp_path, make_ticket(tmp_path, name=f"{tid_blocked_valid}.txt"))
    c_waiting_invalid = claim(tmp_path, make_ticket(tmp_path, name=f"{tid_waiting_invalid}.txt"))
    c_missing_valid = claim(tmp_path, make_ticket(tmp_path, name=f"{tid_missing_valid}.txt"))
    claim(tmp_path, make_ticket(tmp_path, name=f"{tid_active}.txt"))

    # Simulate moves while lead was down so capabilities remained cached
    lead.down = True
    ticket_mover.move_ticket(c_solved_valid, "SOLVED")
    ticket_mover.move_ticket(c_solved_invalid, "SOLVED")
    ticket_mover.move_ticket(c_blocked_valid, "BLOCKED")
    ticket_mover.move_ticket(c_waiting_invalid, "WAITING")
    c_missing_valid.unlink()
    lead.down = False
    capsys.readouterr()

    # Expire the two *_invalid leases on the lead
    cache_before = _cached(tmp_path)
    del lead.leases[cache_before[tid_solved_invalid]["task_id"]]
    del lead.leases[cache_before[tid_waiting_invalid]["task_id"]]

    # Add an uncached own claim in QUEUED so renew-leases also backfills it at the end
    make_ticket(tmp_path, folder="QUEUED", name=f"{tid_backfill}.HOST-A.txt")

    rc = ticket_mover._cli(["--renew-leases", "--host", "HOST-A", "--tickets-dir", str(tmp_path / "tickets")])
    out = capsys.readouterr().out
    assert rc == 0, out

    cache_after = _cached(tmp_path)
    assert set(cache_after) == {tid_active, tid_missing_valid, tid_backfill}
    assert bach_lease.has_lease_capability(cache_after[tid_active])
    assert bach_lease.has_lease_capability(cache_after[tid_missing_valid])
    assert bach_lease.has_lease_capability(cache_after[tid_backfill])

    release_by_task = {
        int(path.split("/")[3]): data["outcome"]
        for _method, path, data in lead.calls
        if path.endswith("/release")
    }
    assert release_by_task[cache_before[tid_solved_valid]["task_id"]] == "done"
    assert release_by_task[cache_before[tid_blocked_valid]["task_id"]] == "blocked"
    assert cache_before[tid_missing_valid]["task_id"] not in release_by_task
    assert f"UNKNOWN {tid_missing_valid} (kept, not renewed)" in out
    assert f"{tid_backfill}: LEASED" in out


def test_fast_lane_takes_lease_and_releases_with_done(tmp_path, lead):
    import ticket_writer

    queue = verified_queue(tmp_path / "tickets")
    make_slot(tmp_path)
    created = Path(ticket_writer.create("Fast-Lane-Fix", "Einzeiler", tickets_dir=queue))
    created_id = bach_lease.ticket_id_of(created.name)
    actionable = ticket_mover.move_ticket(created, "ACTIONABLE")
    claimed = ticket_mover.claim_current_host(actionable, claim_host="HOST-A", sync_root=tmp_path)
    ticket_note.update_ticket_note(
        claimed,
        status="SOLVED (seit 2026-11-22)",
        verlauf="2026-11-22 | ticket-master@HOST-A | Fast-Lane erledigt — Beleg: pytest grün",
    )
    solved = ticket_mover.move_ticket(claimed, "SOLVED")

    assert solved.parent.name == "SOLVED"
    assert f"ticket:{created_id}:ticket-master" in lead.tasks
    assert lead.outcomes() == ["done"]
    assert created_id not in _cached(tmp_path)


# --- follow-ups of the review of #68 (T-20261122-659513039) -------------------------------


@pytest.mark.parametrize(
    ("kind", "token_env_name", "token_env_val", "raw_exc", "forbidden", "expected_marker"),
    [
        (
            "bearer",
            "TICKET_MASTER_BACH_TOKEN",
            SECRET,
            "proxy rejected header Bearer keyring-only-bearer-98765",
            "keyring-only-bearer-98765",
            "Bearer [REDACTED]",
        ),
        (
            "url",
            "TICKET_MASTER_BACH_TOKEN",
            SECRET,
            "connect timeout at https://private-lead.internal:8443/v1/secret-path",
            "private-lead.internal",
            "[REDACTED-URL]",
        ),
        (
            "token_env",
            "CUSTOM_TM_BACH_SECRET_ENV",
            "raw-custom-env-secret-4242",
            "auth digest failed for raw-custom-env-secret-4242 on socket",
            "raw-custom-env-secret-4242",
            "[REDACTED]",
        ),
    ],
)
def test_fallback_note_redacts_bearer_url_and_token_env_independently(
    tmp_path, lead, monkeypatch, capsys, kind, token_env_name, token_env_val, raw_exc, forbidden, expected_marker
):
    cfg = dict(bach_lease.load_config(), fail_open=True, token_env=token_env_name)
    monkeypatch.setattr(bach_lease, "load_config", lambda config=None, _c=cfg: _c)
    monkeypatch.setenv(token_env_name, token_env_val)

    def failing_transport(*_a, **_k):
        raise bach_lease.LeaseUnavailableError(raw_exc)

    monkeypatch.setattr(bach_lease, "_urllib_transport", failing_transport)
    tid = f"T-20261122-40000000{('bearer', 'url', 'token_env').index(kind) + 1}"
    ticket = make_ticket(tmp_path, name=f"{tid}.txt")
    ticket.write_text(
        f"ID:            {tid}\n"
        "TITEL:         Redaktionsbeweis\n"
        "STATUS:        ACTIONABLE\n\n"
        "VERLAUF / LOG\n"
        "--------------------------------------------------------------\n",
        encoding="utf-8",
    )
    claimed = claim(tmp_path, ticket)
    out = capsys.readouterr()
    text = claimed.read_text(encoding="utf-8")
    assert expected_marker in text
    assert forbidden not in text
    assert forbidden not in out.out + out.err
    assert forbidden not in json.dumps(_cached(tmp_path))


def test_reclaim_during_outage_preserves_real_cached_lease_capability(tmp_path, lead, monkeypatch, capsys):
    cfg = dict(bach_lease.load_config(), fail_open=True)
    monkeypatch.setattr(bach_lease, "load_config", lambda config=None, _c=cfg: _c)

    ticket = make_ticket(tmp_path)
    claimed = claim(tmp_path, ticket)
    before_entry = dict(_cached(tmp_path)[TID])
    assert bach_lease.has_lease_capability(before_entry)

    # Outage during re-claim of an already-leased ticket must keep the real capability
    # and must NOT write a false fallback VERLAUF note.
    lead.down = True
    reclaimed = claim(tmp_path, claimed)
    capsys.readouterr()
    assert reclaimed == claimed
    assert _cached(tmp_path)[TID] == before_entry
    assert bach_lease.has_lease_capability(_cached(tmp_path)[TID])
    assert not bach_lease.is_pending_lease_entry(_cached(tmp_path)[TID])
    assert "Claim ohne Lease (Fallback Dateibetrieb):" not in reclaimed.read_text(encoding="utf-8")
    assert bach_lease.record_pending_lease(TID, host="HOST-A", reason="manual") is False
    assert _cached(tmp_path)[TID] == before_entry


def test_fallback_note_written_only_once_per_outage_phase(tmp_path, lead, monkeypatch, capsys):
    cfg = dict(bach_lease.load_config(), fail_open=True)
    monkeypatch.setattr(bach_lease, "load_config", lambda config=None, _c=cfg: _c)
    lead.down = True

    ticket = make_ticket(tmp_path)
    claimed = claim(tmp_path, ticket)
    capsys.readouterr()
    marker = "Claim ohne Lease (Fallback Dateibetrieb):"
    assert claimed.read_text(encoding="utf-8").count(marker) == 1

    # Re-claiming while still pending in the same outage phase must not add another note.
    claim(tmp_path, claimed)
    claim(tmp_path, claimed)
    capsys.readouterr()
    assert claimed.read_text(encoding="utf-8").count(marker) == 1

    # Releasing clears pending_lease; a subsequent outage claim starts a new phase and logs once.
    freed = ticket_mover.release_claim(claimed)
    assert TID not in _cached(tmp_path)
    claimed_phase2 = claim(tmp_path, freed)
    claim(tmp_path, claimed_phase2)
    capsys.readouterr()
    assert claimed_phase2.read_text(encoding="utf-8").count(marker) == 2


def test_maintain_lease_cache_releases_only_inactive_and_keeps_unknown_and_inbox(tmp_path, lead, capsys):
    tids = {
        "SOLVED": "T-20261122-500000001",
        "BLOCKED": "T-20261122-500000002",
        "WAITING": "T-20261122-500000003",
        "USER": "T-20261122-500000004",
        "PARKED": "T-20261122-500000005",
        "INBOX": "T-20261122-500000006",
        "MISSING": "T-20261122-500000007",
    }
    claimed = {
        key: claim(tmp_path, make_ticket(tmp_path, name=f"{tid}.txt"))
        for key, tid in tids.items()
    }
    decisions_index = tmp_path / "decisions.index.json"
    decisions_index.write_text(json.dumps({"entries": []}), encoding="utf-8")

    lead.down = True
    for folder in ("SOLVED", "BLOCKED", "WAITING", "PARKED", "INBOX"):
        ticket_mover.move_ticket(claimed[folder], folder)
    ticket_mover.move_ticket(claimed["USER"], "USER", decisions_index=decisions_index)
    claimed["MISSING"].unlink()
    lead.down = False
    capsys.readouterr()

    before = _cached(tmp_path)
    hygiene = ticket_mover.maintain_lease_cache(tmp_path / "tickets")
    assert (tids["MISSING"], "unknown", f"UNKNOWN {tids['MISSING']} (kept, not renewed)") in hygiene
    assert (tids["INBOX"], "inbox", f"INBOX {tids['INBOX']} (kept, not renewed)") in hygiene

    after = _cached(tmp_path)
    assert set(after) == {tids["INBOX"], tids["MISSING"]}
    assert after[tids["MISSING"]] == before[tids["MISSING"]]
    assert after[tids["INBOX"]] == before[tids["INBOX"]]

    release_by_task = {
        int(path.split("/")[3]): data["outcome"]
        for _method, path, data in lead.calls
        if path.endswith("/release")
    }
    assert release_by_task == {
        before[tids["SOLVED"]]["task_id"]: "done",
        before[tids["BLOCKED"]]["task_id"]: "blocked",
        before[tids["WAITING"]]["task_id"]: "return",
        before[tids["USER"]]["task_id"]: "return",
        before[tids["PARKED"]]["task_id"]: "return",
    }


def test_maintain_lease_cache_aborts_when_queue_missing_or_lacks_lifecycle_folders(tmp_path, lead, capsys):
    claim(tmp_path, make_ticket(tmp_path))
    before_cache = _cached(tmp_path)
    calls_before = len(lead.calls)

    # Case 1: queue directory does not exist at all
    missing_dir = tmp_path / "missing-tickets-dir"
    res_missing = ticket_mover.maintain_lease_cache(missing_dir)
    assert len(res_missing) == 1 and res_missing[0][1] == "aborted"
    assert "does not exist" in res_missing[0][2]
    assert len(lead.calls) == calls_before
    assert _cached(tmp_path) == before_cache

    rc_missing = ticket_mover._cli(["--renew-leases", "--tickets-dir", str(missing_dir)])
    out_missing = capsys.readouterr().out
    assert rc_missing == 1
    assert "HYGIENE ABORTED" in out_missing and "does not exist" in out_missing
    assert not any(c[1].endswith("/release") for c in lead.calls)
    assert TID in _cached(tmp_path)

    # Case 2: queue directory exists (even with queue marker) without any lifecycle folders
    bare_queue = verified_queue(tmp_path / "bare-queue")
    calls_mid = len(lead.calls)
    before_mid = _cached(tmp_path)
    res_bare = ticket_mover.maintain_lease_cache(bare_queue)
    assert len(res_bare) == 1 and res_bare[0][1] == "aborted"
    assert "lifecycle" in res_bare[0][2]
    assert len(lead.calls) == calls_mid
    assert _cached(tmp_path) == before_mid

    rc_bare = ticket_mover._cli(["--renew-leases", "--tickets-dir", str(bare_queue)])
    out_bare = capsys.readouterr().out
    assert rc_bare == 1
    assert "HYGIENE ABORTED" in out_bare and "lifecycle" in out_bare
    assert not any(c[1].endswith("/release") for c in lead.calls)
    assert TID in _cached(tmp_path)


def test_backfill_and_renew_host_case_insensitive_strip_local_and_resolve_live_host(
    tmp_path, lead, monkeypatch, capsys
):
    make_slot(tmp_path)
    tid_upper = "T-20261122-600000001"
    tid_lower = "T-20261122-600000002"
    tid_renew = "T-20261122-600000003"
    tid_foreign = "T-20261122-600000004"

    make_ticket(tmp_path, folder="ACTIONABLE", name=f"{tid_upper}.HOST-A.txt")
    make_ticket(tmp_path, folder="QUEUED", name=f"{tid_lower}.host-a.txt")
    make_ticket(tmp_path, folder="ACTIONABLE", name=f"{tid_foreign}.HOST-B.txt")
    queue_dir = str(tmp_path / "tickets")

    # 1) Explicit --host with lowercase + trailing .local matches both .HOST-A and .host-a
    rc = ticket_mover._cli([
        "--backfill-leases", "--host", "host-a.local", "--tickets-dir", queue_dir
    ])
    out = capsys.readouterr().out
    assert rc == 0, out
    assert f"{tid_upper}: LEASED" in out
    assert f"{tid_lower}: LEASED" in out
    assert tid_foreign not in out
    assert _cached(tmp_path)[tid_upper]["worker_id"] == "ticket-master@HOST-A"
    assert _cached(tmp_path)[tid_lower]["worker_id"] == "ticket-master@HOST-A"

    # 2) Omitted --host in --backfill-leases and --renew-leases uses resolve_live_host
    #    (strips .local from gethostname() and verifies against self-slot snapshot).
    monkeypatch.setattr(ticket_mover.socket, "gethostname", lambda: "host-a.local")
    tid_auto = "T-20261122-600000005"
    make_ticket(tmp_path, folder="ACTIONABLE", name=f"{tid_auto}.HOST-A.txt")
    rc_auto = ticket_mover._cli(["--backfill-leases", "--tickets-dir", queue_dir])
    out_auto = capsys.readouterr().out
    assert rc_auto == 0, out_auto
    assert f"{tid_auto}: LEASED" in out_auto

    make_ticket(tmp_path, folder="QUEUED", name=f"{tid_renew}.host-a.txt")
    rc_renew = ticket_mover._cli(["--renew-leases", "--tickets-dir", queue_dir])
    out_renew = capsys.readouterr().out
    assert rc_renew == 0, out_renew
    assert f"{tid_renew}: LEASED" in out_renew

    # 3) Proof it uses resolve_live_host instead of raw socket.gethostname():
    #    when gethostname() disagrees with the self-slot snapshot, omitted --host fails closed.
    monkeypatch.setattr(ticket_mover.socket, "gethostname", lambda: "HOST-B.local")
    calls_before = len(lead.calls)
    rc_mismatch = ticket_mover._cli(["--backfill-leases", "--tickets-dir", queue_dir])
    out_mismatch = capsys.readouterr().out
    assert rc_mismatch == 1
    assert "REFUSED:" in out_mismatch
    assert len(lead.calls) == calls_before
    assert tid_foreign not in _cached(tmp_path)


# --- follow-ups of the review of #69 (T-20261122-659513039) -------------------------------


def test_renew_leases_does_not_renew_unknown_or_inbox_and_still_renews_active(
    tmp_path, lead, capsys
):
    tid_unknown = "T-20261122-700000001"
    tid_inbox = "T-20261122-700000002"
    tid_actionable = "T-20261122-700000003"
    tid_queued = "T-20261122-700000004"

    c_unknown = claim(tmp_path, make_ticket(tmp_path, name=f"{tid_unknown}.txt"))
    c_inbox = claim(tmp_path, make_ticket(tmp_path, name=f"{tid_inbox}.txt"))
    claim(tmp_path, make_ticket(tmp_path, name=f"{tid_actionable}.txt"))
    c_queued = claim(tmp_path, make_ticket(tmp_path, name=f"{tid_queued}.txt"))
    ticket_mover.move_ticket(c_queued, "QUEUED")

    # Simulate outage during move to INBOX and missing file so capabilities stay cached
    lead.down = True
    inbox_ticket = ticket_mover.move_ticket(c_inbox, "INBOX")
    c_unknown.unlink()
    lead.down = False
    capsys.readouterr()

    cache_before = _cached(tmp_path)
    fences_before = {
        tid: lead.leases[cache_before[tid]["task_id"]]["fence"]
        for tid in (tid_unknown, tid_inbox, tid_actionable, tid_queued)
    }
    calls_before = len(lead.calls)

    rc = ticket_mover._cli([
        "--renew-leases", "--host", "HOST-A", "--tickets-dir", str(tmp_path / "tickets")
    ])
    out = capsys.readouterr().out
    assert rc == 0, out

    new_calls = lead.calls[calls_before:]
    renewed_task_ids = [
        int(path.split("/")[3]) for _m, path, _d in new_calls if path.endswith("/lease/renew")
    ]
    released_task_ids = [
        int(path.split("/")[3]) for _m, path, _d in new_calls if path.endswith("/release")
    ]

    # Negative control: fails if UNKNOWN or INBOX leases are renewed (or prematurely released)
    assert cache_before[tid_unknown]["task_id"] not in renewed_task_ids
    assert cache_before[tid_inbox]["task_id"] not in renewed_task_ids
    assert released_task_ids == []
    assert lead.leases[cache_before[tid_unknown]["task_id"]]["fence"] == fences_before[tid_unknown]
    assert lead.leases[cache_before[tid_inbox]["task_id"]]["fence"] == fences_before[tid_inbox]
    assert f"UNKNOWN {tid_unknown} (kept, not renewed)" in out
    assert f"INBOX {tid_inbox} (kept, not renewed)" in out

    # Positive control: fails if ACTIONABLE or QUEUED leases stop being renewed
    assert set(renewed_task_ids) == {
        cache_before[tid_actionable]["task_id"],
        cache_before[tid_queued]["task_id"],
    }
    assert lead.leases[cache_before[tid_actionable]["task_id"]]["fence"] == fences_before[tid_actionable] + 1
    assert lead.leases[cache_before[tid_queued]["task_id"]]["fence"] == fences_before[tid_queued] + 1
    assert f"RENEWED: {tid_actionable} -- renewed" in out
    assert f"RENEWED: {tid_queued} -- renewed" in out
    assert "2/2 lease(s) renewed" in out

    # Cache entries for UNKNOWN and INBOX stay unchanged until invalidated on a later inactive pass
    cache_after = _cached(tmp_path)
    assert cache_after[tid_unknown] == cache_before[tid_unknown]
    assert cache_after[tid_inbox] == cache_before[tid_inbox]

    # Once TTL expires them on the lead, another host can acquire without 409, and a later inactive
    # move removes the expired capability from this host's cache.
    del lead.leases[cache_before[tid_unknown]["task_id"]]
    del lead.leases[cache_before[tid_inbox]["task_id"]]
    lead.down = True
    ticket_mover.move_ticket(inbox_ticket, "SOLVED")
    lead.down = False
    capsys.readouterr()
    rc2 = ticket_mover._cli([
        "--renew-leases", "--host", "HOST-A", "--tickets-dir", str(tmp_path / "tickets")
    ])
    out2 = capsys.readouterr().out
    assert rc2 == 0, out2
    assert f"REMOVED: {tid_inbox}" in out2
    assert tid_inbox not in _cached(tmp_path)
    assert tid_unknown in _cached(tmp_path)


def test_renew_leases_resolves_host_before_any_lease_action(tmp_path, lead, monkeypatch, capsys):
    # Separate sync_slot from isolated_queue so queue ancestors hold no .SYNC root
    sync_slot = tmp_path / "sync_slot"
    make_slot(sync_slot)
    isolated_root = tmp_path / "isolated"
    queue_dir = verified_queue(isolated_root / "tickets")
    monkeypatch.setenv("TICKET_MASTER_TICKETS_DIR", str(queue_dir))
    monkeypatch.delenv("TICKET_MASTER_SYNC_ROOT", raising=False)
    monkeypatch.setattr(ticket_mover, "tm_config", lambda *_a, **_k: {})

    active_ticket = make_ticket(isolated_root, folder="ACTIONABLE", name=f"{TID}.txt")
    ticket_mover.claim_current_host(active_ticket, claim_host="HOST-A", sync_root=sync_slot)
    tid_solved = "T-20261122-800000001"
    solved_ticket = make_ticket(isolated_root, folder="ACTIONABLE", name=f"{tid_solved}.txt")
    claimed_solved = ticket_mover.claim_current_host(
        solved_ticket, claim_host="HOST-A", sync_root=sync_slot
    )
    lead.down = True
    ticket_mover.move_ticket(claimed_solved, "SOLVED")
    lead.down = False
    capsys.readouterr()

    cache_before = _cached(tmp_path)
    calls_before = list(lead.calls)

    def must_not_touch_http(*_a, **_k):
        raise AssertionError("HTTP transport called before host resolution")

    monkeypatch.setattr(bach_lease, "_urllib_transport", must_not_touch_http)
    rc = ticket_mover._cli(["--renew-leases", "--tickets-dir", str(queue_dir)])
    out = capsys.readouterr().out

    assert rc == 1
    assert "REFUSED:" in out
    assert "--host" in out
    assert "TICKET_MASTER_SYNC_ROOT" in out
    assert lead.calls == calls_before
    assert _cached(tmp_path) == cache_before

    # Providing TICKET_MASTER_SYNC_ROOT resolves the host and lets --renew-leases succeed
    monkeypatch.setattr(bach_lease, "_urllib_transport", lead)
    monkeypatch.setenv("TICKET_MASTER_SYNC_ROOT", str(sync_slot))
    rc_ok = ticket_mover._cli(["--renew-leases", "--tickets-dir", str(queue_dir)])
    out_ok = capsys.readouterr().out
    assert rc_ok == 0, out_ok
    assert f"RELEASED: {tid_solved}" in out_ok
    assert f"RENEWED: {TID} -- renewed" in out_ok
