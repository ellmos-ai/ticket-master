"""Phase-1 tests for the local, pointer-only Trithon projection."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from lib.trithon_shadow import (
    CorruptStoreError,
    InvalidSourceError,
    OUTCOME_RECEIPT_SCHEMA,
    ROUTE_INTENT_SCHEMA,
    ShadowStore,
    SourceConflictError,
    SourceDocument,
    TASK_PROJECTION_SCHEMA,
    main,
)


def task_id(idempotency_key: str) -> str:
    return "trithon-task-" + hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()[:32]


def route(
    *,
    ticket_id: str = "T-20260916-100000001",
    idempotency_key: str = "sha256:route-one",
    source_revision: str = "revision-1",
    target_systems: list[str] | None = None,
) -> dict:
    return {
        "route_intent": ROUTE_INTENT_SCHEMA,
        "ticket_id": ticket_id,
        "idempotency_key": idempotency_key,
        "source_revision": source_revision,
        "requested_capability": "shadow-test",
        "priority": "hoch",
        "receipt_to": ticket_id,
        "target_snapshot": {
            "kind": "grouped",
            "systems": target_systems or ["ASUS-GEI", "WORKSTATION-LG"],
            "at": "2026-09-16T10:00:00Z",
            "source": "fixture://systems",
            "fingerprint": "systems-revision-1",
        },
    }


def document(payload: dict, uri: str = "memory://routes") -> SourceDocument:
    return SourceDocument.from_payload(uri, payload)


def receipt_for(
    route_data: dict,
    *,
    signature: str = "receipt-one",
    status: str = "done",
    by_ticket: bool = False,
) -> dict:
    item = {
        "signature": signature,
        "status": status,
        "executed_by": "mock-runner",
        "actual_provider": "synthetic",
        "actual_model": "mock-model",
        "occurred_at": "2026-09-16T10:01:00Z",
        "evidence": f"receipt://synthetic/{signature}",
        "source_revision": route_data["source_revision"],
    }
    if by_ticket:
        item["ticket_id"] = route_data["ticket_id"]
    else:
        item["task_id"] = task_id(route_data["idempotency_key"])
    return item


def phase0_projection() -> dict:
    payload = {
        "contract": TASK_PROJECTION_SCHEMA,
        "projection_id": "proj-" + "0" * 32,
        "source": {
            "route_intent": ROUTE_INTENT_SCHEMA,
            "ticket_id": "T-20260916-100000003",
            "ticket_revision": "sha256:" + "2" * 64,
            "route_intent_idempotency_key": "sha256:" + "1" * 64,
            "source_status": "ACTIONABLE",
        },
        "task": {
            "task_id": "trithon-task:T-20260916-100000003:222222222222",
            "capability": "software.contracts",
            "priority": "high",
            "task_state": "ready",
        },
        "target_snapshot": {
            "kind": "grouped",
            "systems": ["ASUS-GEI", "WORKSTATION-LG"],
            "at": "2026-09-16T10:00:00Z",
            "source_ref": "registry:synthetic-systems-v1",
            "fingerprint": "sha256:" + "3" * 64,
        },
        "receipt_to": "T-20260916-100000003",
        "delivery": {
            "event_id": "evt-" + "0" * 32,
            "publisher_id": "ticket-master@ASUS-GEI",
            "publisher_epoch": "epoch-test-0001",
            "sequence": 10,
            "attempt": 1,
            "emitted_at": "2026-09-16T10:00:01Z",
        },
    }
    stable = dict(payload)
    stable.pop("delivery")
    payload["idempotency_key"] = "sha256:" + hashlib.sha256(
        json.dumps(stable, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return payload


def phase0_outcome() -> dict:
    payload = {
        "contract": OUTCOME_RECEIPT_SCHEMA,
        "receipt_id": "rcpt-" + "0" * 32,
        "dispatch_id": "dsp-" + "0" * 32,
        "assignment_id": "asgn-" + "0" * 32,
        "task_ref": {
            "projection_id": "proj-" + "0" * 32,
            "task_id": "trithon-task:T-20260916-100000003:222222222222",
            "ticket_id": "T-20260916-100000003",
            "ticket_revision": "sha256:" + "2" * 64,
            "receipt_to": "T-20260916-100000003",
        },
        "outcome": {
            "status": "completed",
            "result_code": "task_done",
            "reason_code": "none",
            "proposed_ticket_status": "SOLVED",
        },
        "evidence": [
            {
                "kind": "test_report",
                "ref": "urn:sha256:" + "4" * 64,
                "digest": "sha256:" + "4" * 64,
            }
        ],
        "delivery": {
            "event_id": "evt-" + "1" * 32,
            "publisher_id": "trithon@ASUS-GEI",
            "publisher_epoch": "epoch-test-0001",
            "sequence": 11,
            "attempt": 1,
            "emitted_at": "2026-09-16T10:01:00Z",
        },
    }
    stable = dict(payload)
    stable.pop("delivery")
    payload["idempotency_key"] = "sha256:" + hashlib.sha256(
        json.dumps(stable, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return payload


def test_import_is_pointer_only_and_idempotent(tmp_path: Path):
    store = ShadowStore(tmp_path / "shadow.sqlite3")
    source = document(route())

    first = store.import_documents([source])
    second = store.import_documents([source])

    assert first.as_dict() == {
        "documents": 1,
        "projected": 1,
        "unchanged": 0,
        "receipts": 0,
        "receipt_unchanged": 0,
        "checkpoints": 1,
    }
    assert second.projected == 0
    assert second.unchanged == 1
    assert len(store.tasks()) == 1
    assert store.verify()["counts"] == {"tasks": 1, "receipts": 0, "history": 1, "checkpoints": 1}

    connection = sqlite3.connect(store.path)
    try:
        history = connection.execute("SELECT metadata_json FROM task_history").fetchone()[0]
    finally:
        connection.close()
    assert "description" not in history
    assert "prompt" not in history
    assert "secret" not in history


def test_direct_route_root_and_receipt_by_ticket_are_supported(tmp_path: Path):
    store = ShadowStore(tmp_path / "shadow.sqlite3")
    route_data = route(idempotency_key="sha256:route-two")
    route_source = document(route_data, "memory://direct")
    receipt_source = document(
        {"receipts": [receipt_for(route_data, signature="receipt-two", by_ticket=True)]},
        "memory://receipt",
    )

    assert store.import_documents([route_source, receipt_source]).receipts == 1
    row = store.tasks()[0]
    assert row["ticket_id"] == route_data["ticket_id"]
    assert row["shadow_state"] == "done"
    assert store.verify()["counts"] == {"tasks": 1, "receipts": 1, "history": 2, "checkpoints": 2}
    replay = store.import_documents([route_source, receipt_source])
    assert replay.unchanged == 1
    assert replay.receipt_unchanged == 1


def test_sensitive_or_unknown_source_fields_fail_closed_before_write(tmp_path: Path):
    store = ShadowStore(tmp_path / "shadow.sqlite3")
    unsafe = route(idempotency_key="sha256:unsafe")
    unsafe["description"] = "raw transcript"
    with pytest.raises(InvalidSourceError, match="pointer-only"):
        store.import_documents([document(unsafe)])
    unknown = route(idempotency_key="sha256:unknown")
    unknown["extra"] = "not part of the contract"
    with pytest.raises(InvalidSourceError, match="unsupported fields"):
        store.import_documents([document(unknown)])
    for key in ("access_token", "credentials", "original_text", "raw_transcript"):
        unsafe_key = route(idempotency_key=f"sha256:{key}")
        unsafe_key[key] = "must never enter the projection"
        with pytest.raises(InvalidSourceError, match="pointer-only"):
            store.import_documents([document(unsafe_key, f"memory://{key}")])
    for key, value in (
        ("requested_capability", r"C:\private\raw-ticket.txt"),
        ("requested_capability", "password=synthetic-secret"),
        ("requested_capability", "/home/user/private-ticket.txt"),
    ):
        unsafe_value = route(idempotency_key=f"sha256:value-{len(value)}")
        unsafe_value[key] = value
        with pytest.raises(InvalidSourceError, match="path or secret"):
            store.import_documents([document(unsafe_value, f"memory://value-{len(value)}")])
    assert store.verify()["counts"] == {"tasks": 0, "receipts": 0, "history": 0, "checkpoints": 0}


def test_duplicate_target_systems_fail_closed(tmp_path: Path):
    store = ShadowStore(tmp_path / "shadow.sqlite3")
    unsafe = route(target_systems=["ASUS-GEI", "ASUS-GEI"])
    with pytest.raises(InvalidSourceError, match="unique systems"):
        store.import_documents([document(unsafe, "memory://duplicate-systems")])


def test_phase0_projection_and_outcome_contracts_are_imported_and_bound(tmp_path: Path):
    store = ShadowStore(tmp_path / "shadow.sqlite3")
    projection = document(phase0_projection(), "memory://phase0-projection")
    outcome = document(phase0_outcome(), "memory://phase0-outcome")

    first = store.import_documents([projection, outcome])
    assert first.projected == 1
    assert first.receipts == 1
    row = store.tasks()[0]
    assert row["projection_id"] == "proj-" + "0" * 32
    assert row["task_id"] == "trithon-task:T-20260916-100000003:222222222222"
    assert row["shadow_state"] == "done"
    assert store.verify()["ok"] is True

    replay = store.import_documents([projection, outcome])
    assert replay.unchanged == 1
    assert replay.receipt_unchanged == 1


def test_phase0_delivery_retry_changes_only_transport_and_remains_idempotent(tmp_path: Path):
    store = ShadowStore(tmp_path / "shadow.sqlite3")
    projection_payload = phase0_projection()
    outcome_payload = phase0_outcome()
    store.import_documents(
        [
            document(projection_payload, "memory://phase0-retry-projection"),
            document(outcome_payload, "memory://phase0-retry-outcome"),
        ]
    )

    projection_retry = json.loads(json.dumps(projection_payload))
    projection_retry["delivery"].update(
        event_id="evt-" + "9" * 32, sequence=11, attempt=2, emitted_at="2026-09-16T10:00:02Z"
    )
    outcome_retry = json.loads(json.dumps(outcome_payload))
    outcome_retry["delivery"].update(
        event_id="evt-" + "8" * 32, sequence=12, attempt=2, emitted_at="2026-09-16T10:01:01Z"
    )
    replay = store.import_documents(
        [
            document(projection_retry, "memory://phase0-retry-projection"),
            document(outcome_retry, "memory://phase0-retry-outcome"),
        ]
    )
    assert replay.unchanged == 1
    assert replay.receipt_unchanged == 1
    assert store.verify()["counts"] == {"tasks": 1, "receipts": 1, "history": 2, "checkpoints": 2}


def test_unknown_source_shape_is_rejected_instead_of_becoming_a_checkpoint(tmp_path: Path):
    store = ShadowStore(tmp_path / "shadow.sqlite3")
    with pytest.raises(InvalidSourceError, match="no supported"):
        store.import_documents([document({"schema": "foreign.v1"}, "memory://foreign")])


def test_source_drift_requires_explicit_rebuild_and_rebuild_is_complete(tmp_path: Path):
    store = ShadowStore(tmp_path / "shadow.sqlite3")
    original = document(route(), "memory://drift")
    changed_data = route(source_revision="revision-2")
    changed = document(changed_data, "memory://drift")
    store.import_documents([original])

    with pytest.raises(SourceConflictError, match="source changed"):
        store.import_documents([changed])
    assert store.tasks()[0]["source_revision"] == "revision-1"
    assert store.reset_checkpoints() == 1
    assert store.import_documents([original]).unchanged == 1

    rebuilt = store.rebuild([changed])
    assert rebuilt.projected == 1
    assert [row["source_revision"] for row in store.tasks()] == ["revision-2"]


def test_failed_import_rolls_back_and_retry_recovers(tmp_path: Path):
    store = ShadowStore(tmp_path / "shadow.sqlite3")
    source = document(route())

    def crash(point: str) -> None:
        if point == "after_task_insert":
            raise RuntimeError("simulated process crash")

    with pytest.raises(RuntimeError, match="simulated process crash"):
        store.import_documents([source], failure_hook=crash)
    assert store.verify()["counts"] == {"tasks": 0, "receipts": 0, "history": 0, "checkpoints": 0}
    assert store.import_documents([source]).projected == 1


def test_rebuild_rolls_back_to_previous_projection_on_failure(tmp_path: Path):
    store = ShadowStore(tmp_path / "shadow.sqlite3")
    old = document(route(idempotency_key="sha256:old"), "memory://old")
    new = document(route(idempotency_key="sha256:new", ticket_id="T-20260916-100000002"), "memory://new")
    store.import_documents([old])

    def crash(point: str) -> None:
        if point == "after_task_insert":
            raise RuntimeError("crash during rebuild")

    with pytest.raises(RuntimeError, match="crash during rebuild"):
        store.rebuild([new], failure_hook=crash)
    assert [row["ticket_id"] for row in store.tasks()] == ["T-20260916-100000001"]
    assert store.rebuild([new]).projected == 1
    assert [row["ticket_id"] for row in store.tasks()] == ["T-20260916-100000002"]


def test_parallel_import_has_one_projection(tmp_path: Path):
    store = ShadowStore(tmp_path / "shadow.sqlite3")
    source = document(route())

    def run_import() -> ImportResultLike:
        return store.import_documents([source])

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _item: run_import(), range(2)))
    assert sorted(result.projected for result in results) == [0, 1]
    assert len(store.tasks()) == 1


class ImportResultLike:
    """Typing-only protocol substitute kept local to avoid a runtime dependency."""

    projected: int


def test_corrupt_database_fails_closed(tmp_path: Path):
    path = tmp_path / "corrupt.sqlite3"
    path.write_bytes(b"this is not sqlite")
    with pytest.raises(CorruptStoreError):
        ShadowStore(path)


def test_receipt_conflicts_and_transactional_unknown_reference(tmp_path: Path):
    store = ShadowStore(tmp_path / "shadow.sqlite3")
    route_data = route(idempotency_key="sha256:route-three")
    source = document(route_data)
    unknown = {
        "receipts": [
            {
                "task_id": "trithon-task-does-not-exist",
                "signature": "bad-receipt",
                "status": "done",
                "executed_by": "mock",
                "actual_provider": "synthetic",
                "actual_model": "mock",
                "occurred_at": "2026-09-16T10:00:00Z",
                "evidence": "receipt://synthetic/bad",
            }
        ]
    }
    with pytest.raises(InvalidSourceError, match="no projected task"):
        store.import_documents([source, document(unknown, "memory://unknown")])
    assert store.verify()["counts"] == {"tasks": 0, "receipts": 0, "history": 0, "checkpoints": 0}

    store.import_documents([source])
    receipt_source = document(
        {"receipts": [receipt_for(route_data, signature="receipt-three")]},
        "memory://receipt-three",
    )
    store.import_documents([receipt_source])
    altered = receipt_for(route_data, signature="receipt-three")
    altered["evidence"] = "receipt://synthetic/altered"
    with pytest.raises(SourceConflictError, match="signature changed"):
        store.import_documents([document({"receipts": [altered]}, "memory://receipt-altered")])


def test_receipt_evidence_is_a_pointer_and_mock_cannot_overwrite_receipt(tmp_path: Path):
    store = ShadowStore(tmp_path / "shadow.sqlite3")
    route_data = route(idempotency_key="sha256:route-four")
    source = document(route_data, "memory://route-four")
    store.import_documents([source])

    invalid_evidence = receipt_for(route_data, signature="receipt-four")
    invalid_evidence["evidence"] = "raw execution output"
    with pytest.raises(InvalidSourceError, match="URI/pointer"):
        store.import_documents([document({"receipts": [invalid_evidence]}, "memory://bad-evidence")])

    receipt_source = document(
        {"receipts": [receipt_for(route_data, signature="receipt-four")]},
        "memory://receipt-four",
    )
    store.import_documents([receipt_source])
    with pytest.raises(InvalidSourceError, match="cannot overwrite"):
        store.mock_execute(task_id("sha256:route-four"))


def test_mock_executor_is_local_noop_and_idempotent(tmp_path: Path):
    store = ShadowStore(tmp_path / "shadow.sqlite3")
    store.import_documents([document(route())])
    identifier = task_id("sha256:route-one")
    first = store.mock_execute(identifier, evidence="mock://run/one")
    second = store.mock_execute(identifier, evidence="mock://run/one")
    assert first == second == {"task_id": identifier, "status": "done", "evidence": "mock://run/one"}
    assert store.tasks()[0]["shadow_state"] == "mock-done"
    assert store.verify()["counts"] == {"tasks": 1, "receipts": 0, "history": 2, "checkpoints": 1}


def test_ticket_source_is_never_opened_or_changed(tmp_path: Path):
    ticket = tmp_path / "T-20260916-100000099.txt"
    ticket.write_text("STATUS: ACTIONABLE\nsecret body stays here\n", encoding="utf-8")
    before = ticket.read_bytes()
    store = ShadowStore(tmp_path / "shadow.sqlite3")
    source = document(route(), ticket.as_uri())
    store.import_documents([source])
    assert ticket.read_bytes() == before
    assert "secret body" not in json.dumps(store.tasks(), ensure_ascii=False)


def test_cli_import_and_verify(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    source_path = tmp_path / "route.json"
    source_path.write_text(json.dumps(route(), ensure_ascii=False), encoding="utf-8")
    db_path = tmp_path / "cli.sqlite3"

    assert main(["--db", str(db_path), "import", "--source", str(source_path)]) == 0
    imported = json.loads(capsys.readouterr().out)
    assert imported["projected"] == 1
    assert main(["--db", str(db_path), "verify"]) == 0
    verified = json.loads(capsys.readouterr().out)
    assert verified["ok"] is True
