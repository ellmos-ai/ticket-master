"""The routing-v2 contract writers and the status drift fixer refuse a shadow queue (review of ticket-master#54).

A byte copy of a real contract ticket is put into a folder that only looks like a queue: a lifecycle folder, no
marker, no README. Every writer must refuse before it writes anything -- no rewrite, no half state ("STATUS:
SOLVED" in a file that stays where it was), no new file. Only temporary queues are used.
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

import routing_contract as rc  # noqa: E402
import status_drift_fixer  # noqa: E402
from queue_helpers import verified_queue  # noqa: E402
from test_routing_contract import make_contract, receipt, resolver  # noqa: E402

NOW = "2026-08-22T09:00:00Z"
LATER = "2026-08-22T09:30:00Z"


def snapshot(root: Path) -> dict[str, bytes]:
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


def shadow_of(ticket: Path, tmp_path: Path) -> Path:
    """Byte copy of *ticket* below a folder that is not a verified queue."""
    folder = tmp_path / "shadow" / ticket.parent.name
    folder.mkdir(parents=True)
    copy = folder / ticket.name
    copy.write_bytes(ticket.read_bytes())
    return copy


def assert_refused(call, shadow_ticket: Path) -> None:
    root = shadow_ticket.parent.parent
    before = snapshot(root)
    with pytest.raises(ValueError, match="verified ticket queue"):
        call()
    assert snapshot(root) == before, "a refused writer must leave the shadow queue byte-identical"


def claimed_any_contract(tmp_path: Path) -> Path:
    path = make_contract(tmp_path / "real", target_kind="any", targets=[], ticket_kind="normal", via="claude")
    return rc.claim_contract(path, host="ASUS-GEI", actor="claude", runner="claude", resolver=resolver, now=NOW)


def test_record_receipt_refuses_a_shadow_queue(tmp_path):
    shadow = shadow_of(claimed_any_contract(tmp_path), tmp_path)

    assert_refused(
        lambda: rc.record_receipt(shadow, host="ASUS-GEI", receipt=receipt("ASUS-GEI", "sig-1"), now=LATER),
        shadow,
    )


def test_release_contract_refuses_before_it_writes(tmp_path):
    shadow = shadow_of(claimed_any_contract(tmp_path), tmp_path)

    assert_refused(lambda: rc.release_contract(shadow, host="ASUS-GEI", now=LATER), shadow)


def test_complete_contract_leaves_no_half_solved_ticket(tmp_path):
    claimed = claimed_any_contract(tmp_path)
    rc.record_receipt(claimed, host="ASUS-GEI", receipt=receipt("ASUS-GEI", "sig-1"), now=LATER)
    shadow = shadow_of(claimed, tmp_path)

    assert_refused(
        lambda: rc.complete_contract(shadow, host="ASUS-GEI", solved_dir=shadow.parent.parent / "SOLVED", now=LATER),
        shadow,
    )
    assert "STATUS:        SOLVED" not in shadow.read_text(encoding="utf-8")
    assert not (shadow.parent.parent / "SOLVED").exists()


def test_normalize_expired_binding_refuses_a_shadow_queue(tmp_path):
    shadow = shadow_of(make_contract(tmp_path / "real", via="claude", binding_ttl=1), tmp_path)

    assert_refused(lambda: rc.normalize_expired_binding(shadow, now="2026-08-24T00:00:00Z"), shadow)


def test_refresh_resolution_refuses_a_shadow_queue(tmp_path):
    shadow = shadow_of(make_contract(tmp_path / "real", via="claude"), tmp_path)

    def unresolved(selector, runner=None):
        return resolver("no-such-selector", runner)

    assert_refused(lambda: rc.refresh_resolution(shadow, resolver=unresolved, now=NOW), shadow)


def test_status_drift_fixer_apply_refuses_a_shadow_queue(tmp_path):
    from test_status_drift_fixer import _DONE_ENTRY, _FILLED, _ticket

    shadow_root = tmp_path / "shadow"
    ticket = _ticket(shadow_root / "SOLVED" / "T-20260816-000000001.txt", "INBOX", _FILLED, _DONE_ENTRY)

    assert_refused(lambda: status_drift_fixer.run(shadow_root, dry_run=False), ticket)


def test_the_same_writers_still_work_in_a_verified_queue(tmp_path):
    claimed = claimed_any_contract(tmp_path)
    assert rc.record_receipt(claimed, host="ASUS-GEI", receipt=receipt("ASUS-GEI", "sig-1"), now=LATER)
    solved = rc.complete_contract(claimed, host="ASUS-GEI", solved_dir=tmp_path / "real" / "SOLVED", now=LATER)
    assert solved.parent.name == "SOLVED"

    root = verified_queue(tmp_path / "drift")
    from test_status_drift_fixer import _DONE_ENTRY, _FILLED, _ticket

    _ticket(root / "SOLVED" / "T-20260816-000000001.txt", "INBOX", _FILLED, _DONE_ENTRY)
    assert status_drift_fixer.run(root, dry_run=False)["applied"]
