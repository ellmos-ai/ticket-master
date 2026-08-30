import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

import ticket_mover  # noqa: E402


def make_self_slot(root: Path, *, snapshot_host="ASUS-GEI", manifest_host="ASUS-GEI"):
    snapshots = root / "_config-state" / "snapshots"
    snapshots.mkdir(parents=True)
    (snapshots / "laptop.json").write_text(
        json.dumps({"host": snapshot_host, "slot": "laptop"}), encoding="utf-8"
    )
    slot = root / "laptop"
    slot.mkdir()
    (slot / "repos.json").write_text(
        json.dumps({"host": manifest_host, "slot": "laptop"}), encoding="utf-8"
    )


def make_ticket(root: Path, name="T-20260827-123456789.txt") -> Path:
    folder = root / "tickets" / "ACTIONABLE"
    folder.mkdir(parents=True)
    path = folder / name
    path.write_text("unchanged\n", encoding="utf-8")
    return path


def test_claim_requires_live_snapshot_manifest_and_asserted_host(tmp_path, monkeypatch):
    monkeypatch.setattr(ticket_mover.socket, "gethostname", lambda: "ASUS-GEI")
    monkeypatch.setenv("COMPUTERNAME", "WORKSTATION-LG")
    make_self_slot(tmp_path)
    source = make_ticket(tmp_path)

    target = ticket_mover.claim_current_host(
        source, claim_host="ASUS-GEI", sync_root=tmp_path
    )

    assert target.name == "T-20260827-123456789.ASUS-GEI.txt"
    assert target.read_text(encoding="utf-8") == "unchanged\n"


@pytest.mark.parametrize(
    ("claim_host", "snapshot_host", "manifest_host"),
    [
        ("WORKSTATION-LG", "ASUS-GEI", "ASUS-GEI"),
        ("ASUS-GEI", "WORKSTATION-LG", "ASUS-GEI"),
        ("ASUS-GEI", "ASUS-GEI", "WORKSTATION-LG"),
    ],
)
def test_claim_fails_closed_on_any_identity_disagreement(
    tmp_path, monkeypatch, claim_host, snapshot_host, manifest_host
):
    monkeypatch.setattr(ticket_mover.socket, "gethostname", lambda: "ASUS-GEI")
    make_self_slot(
        tmp_path, snapshot_host=snapshot_host, manifest_host=manifest_host
    )
    source = make_ticket(tmp_path)

    with pytest.raises(ticket_mover.HostIdentityError):
        ticket_mover.claim_current_host(
            source, claim_host=claim_host, sync_root=tmp_path
        )

    assert source.read_text(encoding="utf-8") == "unchanged\n"
