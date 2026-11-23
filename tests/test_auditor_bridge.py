"""Tests for lib/auditor_bridge.py (T-20260830-948243522).

Covers the load-bearing logic this module owns: the three-valued sparmodus
read, decide()'s combination of detect/spar_gate/due_check (all mocked --
never a real subprocess or network call), and findings_to_tickets()'s
dry-run dedup. system-auditor itself is never invoked here.
"""
import json
import os
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

import auditor_bridge  # noqa: E402
import ticket_writer  # noqa: E402
from queue_helpers import verified_queue  # noqa: E402


def write_json(path: Path, data) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


# --------------------------------------------------------------------------
# spar_gate: off / on / unknown, and unknown must never collapse to off
# --------------------------------------------------------------------------

def test_spar_gate_off_state_authoritative_even_without_budget_file(tmp_path):
    state = write_json(tmp_path / "sparmodus_state.json", {"mode": "off"})
    missing_budget = tmp_path / "does-not-exist.json"
    assert auditor_bridge.spar_gate(state, missing_budget) == "off"


def test_spar_gate_off_state_with_low_budget(tmp_path):
    state = write_json(tmp_path / "sparmodus_state.json", {"mode": "off"})
    budget = write_json(tmp_path / "token_budget.json", {"five_hour": {"used_percentage": 9.0}})
    assert auditor_bridge.spar_gate(state, budget) == "off"


def test_spar_gate_manual_spar_is_on_regardless_of_budget(tmp_path):
    state = write_json(tmp_path / "sparmodus_state.json", {"mode": "manual-spar"})
    assert auditor_bridge.spar_gate(state, tmp_path / "missing.json") == "on"


def test_spar_gate_auto_spar_and_notaus_are_on(tmp_path):
    for mode in ("auto-spar", "notaus"):
        state = write_json(tmp_path / f"state-{mode}.json", {"mode": mode})
        assert auditor_bridge.spar_gate(state, tmp_path / "missing.json") == "on"


def test_spar_gate_stale_off_overridden_by_high_live_budget(tmp_path):
    """A stale 'off' written before the hook re-ran must not survive a live
    budget already over threshold -- the defensive cross-check catches it."""
    state = write_json(tmp_path / "sparmodus_state.json", {"mode": "off"})
    budget = write_json(tmp_path / "token_budget.json", {"five_hour": {"used_percentage": 85.0}})
    assert auditor_bridge.spar_gate(state, budget) == "on"


def test_spar_gate_unknown_when_state_file_missing(tmp_path):
    budget = write_json(tmp_path / "token_budget.json", {"five_hour": {"used_percentage": 5.0}})
    assert auditor_bridge.spar_gate(tmp_path / "missing.json", budget) == "unknown"


def test_spar_gate_unknown_when_state_file_corrupt(tmp_path):
    state = tmp_path / "sparmodus_state.json"
    state.write_text("{not valid json", encoding="utf-8")
    assert auditor_bridge.spar_gate(state, tmp_path / "missing.json") == "unknown"


def test_spar_gate_unknown_never_off_when_both_files_missing(tmp_path):
    result = auditor_bridge.spar_gate(tmp_path / "a.json", tmp_path / "b.json")
    assert result == "unknown"
    assert result != "off"


def test_spar_gate_unrecognized_mode_string_is_unknown_not_off(tmp_path):
    state = write_json(tmp_path / "sparmodus_state.json", {"mode": "totally-unrecognized"})
    assert auditor_bridge.spar_gate(state, tmp_path / "missing.json") == "unknown"


def test_spar_gate_custom_thresholds_override_default(tmp_path):
    state = write_json(tmp_path / "sparmodus_state.json", {"mode": "off"})
    budget = write_json(tmp_path / "token_budget.json", {"five_hour": {"used_percentage": 30.0}})
    assert auditor_bridge.spar_gate(state, budget, {"sparmodus_used_pct": 25}) == "on"
    assert auditor_bridge.spar_gate(state, budget, {"sparmodus_used_pct": 50}) == "off"


# --------------------------------------------------------------------------
# due_check: combine time-token/next-domain/reports without recomputing them
# --------------------------------------------------------------------------

def test_due_check_true_when_domain_not_yet_reported(tmp_path, monkeypatch):
    monkeypatch.setattr(auditor_bridge.shutil, "which", lambda name: "fake-system-auditor")

    def fake_run_json(cli_path, args, timeout=30.0):
        if args[0] == "time-token":
            return {"time_token": "20260824"}
        if args[0] == "next-domain":
            return {"domain": "ai-bundles"}
        if args[0] == "reports":
            return {"reports": ["20260817 · ai-bundles · HOST-A · fable-5 · self"]}
        return {}

    monkeypatch.setattr(auditor_bridge, "_run_json", fake_run_json)
    result = auditor_bridge.due_check(tmp_path)
    assert result == {
        "due": True, "domain": "ai-bundles", "window": "20260824",
        "raw": result["raw"],
    }


def test_due_check_false_when_domain_already_reported_this_window(tmp_path, monkeypatch):
    monkeypatch.setattr(auditor_bridge.shutil, "which", lambda name: "fake-system-auditor")

    def fake_run_json(cli_path, args, timeout=30.0):
        if args[0] == "time-token":
            return {"time_token": "20260824"}
        if args[0] == "next-domain":
            return {"domain": "ai-bundles"}
        if args[0] == "reports":
            return {"reports": ["20260824 · ai-bundles · HOST-A · fable-5 · self"]}
        return {}

    monkeypatch.setattr(auditor_bridge, "_run_json", fake_run_json)
    result = auditor_bridge.due_check(tmp_path)
    assert result["due"] is False


def test_due_check_unknown_when_cli_absent(tmp_path, monkeypatch):
    monkeypatch.setattr(auditor_bridge.shutil, "which", lambda name: None)
    result = auditor_bridge.due_check(tmp_path)
    assert result["due"] is None
    assert result["raw"] is None


def test_due_check_unknown_when_a_subcommand_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(auditor_bridge.shutil, "which", lambda name: "fake-system-auditor")

    def fake_run_json(cli_path, args, timeout=30.0):
        if args[0] == "reports":
            return None  # e.g. process crashed / bad JSON
        if args[0] == "time-token":
            return {"time_token": "20260824"}
        if args[0] == "next-domain":
            return {"domain": "ai-bundles"}
        return {}

    monkeypatch.setattr(auditor_bridge, "_run_json", fake_run_json)
    result = auditor_bridge.due_check(tmp_path)
    assert result["due"] is None


# --------------------------------------------------------------------------
# decide(): combination logic, with detect_auditor/due_check/spar_gate mocked
# --------------------------------------------------------------------------

def _absent(**_kwargs):
    return {"present": False, "path": None, "version": None}


def _present(**_kwargs):
    return {"present": True, "path": "/usr/bin/system-auditor", "version": "0.9.1"}


def test_decide_absent_short_circuits_before_anything_else(monkeypatch):
    monkeypatch.setattr(auditor_bridge, "detect_auditor", _absent)

    def boom(*a, **k):
        raise AssertionError("must not be called when auditor is absent")

    monkeypatch.setattr(auditor_bridge, "spar_gate", boom)
    monkeypatch.setattr(auditor_bridge, "due_check", boom)
    result = auditor_bridge.decide({})
    assert result["action"] == "absent"


def test_decide_disabled_by_default_but_reports_spar_gate(monkeypatch):
    monkeypatch.setattr(auditor_bridge, "detect_auditor", _present)
    monkeypatch.setattr(auditor_bridge, "spar_gate", lambda *a, **k: "off")
    result = auditor_bridge.decide({})  # no auditor_bridge config -> default enabled=False
    assert result["action"] == "disabled"
    assert result["spar_gate"] == "off"


def test_decide_skip_when_sparmodus_active(monkeypatch):
    monkeypatch.setattr(auditor_bridge, "detect_auditor", _present)
    monkeypatch.setattr(auditor_bridge, "spar_gate", lambda *a, **k: "on")

    def boom(*a, **k):
        raise AssertionError("due_check must not run while sparmodus gate says on")

    monkeypatch.setattr(auditor_bridge, "due_check", boom)
    config = {"auditor_bridge": {"enabled": True}, "reports_dir": "/reports"}
    result = auditor_bridge.decide(config)
    assert result["action"] == "skip"
    assert result["spar_gate"] == "on"


def test_decide_unknown_when_sparmodus_state_not_determinable(monkeypatch):
    monkeypatch.setattr(auditor_bridge, "detect_auditor", _present)
    monkeypatch.setattr(auditor_bridge, "spar_gate", lambda *a, **k: "unknown")
    config = {"auditor_bridge": {"enabled": True}, "reports_dir": "/reports"}
    result = auditor_bridge.decide(config)
    assert result["action"] == "unknown"
    assert result["spar_gate"] == "unknown"


def test_decide_spawn_when_enabled_gate_off_and_due(monkeypatch):
    monkeypatch.setattr(auditor_bridge, "detect_auditor", _present)
    monkeypatch.setattr(auditor_bridge, "spar_gate", lambda *a, **k: "off")
    monkeypatch.setattr(
        auditor_bridge, "due_check",
        lambda reports_dir: {"due": True, "domain": "ai-bundles", "window": "20260824", "raw": {}},
    )
    config = {"auditor_bridge": {"enabled": True}, "reports_dir": "/reports"}
    result = auditor_bridge.decide(config)
    assert result["action"] == "spawn"
    assert "ai-bundles" in result["reason"]


def test_decide_skip_when_enabled_gate_off_but_not_due(monkeypatch):
    monkeypatch.setattr(auditor_bridge, "detect_auditor", _present)
    monkeypatch.setattr(auditor_bridge, "spar_gate", lambda *a, **k: "off")
    monkeypatch.setattr(
        auditor_bridge, "due_check",
        lambda reports_dir: {"due": False, "domain": "ai-bundles", "window": "20260824", "raw": {}},
    )
    config = {"auditor_bridge": {"enabled": True}, "reports_dir": "/reports"}
    result = auditor_bridge.decide(config)
    assert result["action"] == "skip"


def test_decide_unknown_when_due_check_fails(monkeypatch):
    monkeypatch.setattr(auditor_bridge, "detect_auditor", _present)
    monkeypatch.setattr(auditor_bridge, "spar_gate", lambda *a, **k: "off")
    monkeypatch.setattr(
        auditor_bridge, "due_check",
        lambda reports_dir: {"due": None, "domain": None, "window": None, "raw": None},
    )
    config = {"auditor_bridge": {"enabled": True}, "reports_dir": "/reports"}
    result = auditor_bridge.decide(config)
    assert result["action"] == "unknown"


def test_decide_unknown_when_reports_dir_missing(monkeypatch):
    monkeypatch.setattr(auditor_bridge, "detect_auditor", _present)
    monkeypatch.setattr(auditor_bridge, "spar_gate", lambda *a, **k: "off")
    config = {"auditor_bridge": {"enabled": True}}  # no reports_dir
    result = auditor_bridge.decide(config)
    assert result["action"] == "unknown"


def test_decide_manual_bypasses_enabled_and_due_check(monkeypatch):
    monkeypatch.setattr(auditor_bridge, "detect_auditor", _present)
    monkeypatch.setattr(auditor_bridge, "spar_gate", lambda *a, **k: "off")

    def boom(*a, **k):
        raise AssertionError("codeword path must not consult due_check")

    monkeypatch.setattr(auditor_bridge, "due_check", boom)
    config = {"auditor_bridge": {"enabled": False}}  # disabled -- codeword still spawns
    result = auditor_bridge.decide(config, manual=True)
    assert result["action"] == "spawn"


def test_decide_manual_still_blocked_by_sparmodus(monkeypatch):
    monkeypatch.setattr(auditor_bridge, "detect_auditor", _present)
    monkeypatch.setattr(auditor_bridge, "spar_gate", lambda *a, **k: "on")
    result = auditor_bridge.decide({"auditor_bridge": {"enabled": False}}, manual=True)
    assert result["action"] == "skip"


def test_decide_spar_gate_disabled_in_config_ignores_on_reading(monkeypatch):
    monkeypatch.setattr(auditor_bridge, "detect_auditor", _present)
    monkeypatch.setattr(auditor_bridge, "spar_gate", lambda *a, **k: "on")
    monkeypatch.setattr(
        auditor_bridge, "due_check",
        lambda reports_dir: {"due": True, "domain": "ai-bundles", "window": "20260824", "raw": {}},
    )
    config = {
        "auditor_bridge": {"enabled": True, "spar_gate": False},
        "reports_dir": "/reports",
    }
    result = auditor_bridge.decide(config)
    assert result["action"] == "spawn"  # gate value shown but not used as a block
    assert result["spar_gate"] == "on"


# --------------------------------------------------------------------------
# findings_to_tickets: dry-run planning + dedup, no real ticket_writer call
# --------------------------------------------------------------------------

def make_finding(findings_dir: Path, finding_id: str, title: str, body: str = "body text") -> Path:
    findings_dir.mkdir(parents=True, exist_ok=True)
    path = findings_dir / f"{finding_id}.md"
    path.write_text(f"# {finding_id} — {title}\n\n{body}\n", encoding="utf-8")
    return path


def link_ticket(tickets_dir: Path, ticket_name: str, body: str) -> Path:
    path = tickets_dir / "BLOCKED" / ticket_name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def readback_line(
    finding_id: str,
    *,
    measured_date: str | None = None,
    referenced_id: str | None = None,
) -> str:
    referenced_id = referenced_id or finding_id
    return (
        f"{measured_date or date.today().isoformat()} | reviewer@HOST-A | "
        f"Readback {referenced_id} "
        "location=lib/auditor_bridge.py value=dedup-aware lock=clear "
        "— SHA256 deadbeef"
    )


def current_finding_id(suffix: str) -> str:
    return f"M-{date.today():%Y%m%d}-{suffix}"


def test_findings_to_tickets_dry_run_plans_without_writing(tmp_path):
    findings_dir = tmp_path / "findings"
    tickets_dir = tmp_path / "tickets"
    make_finding(findings_dir, "M-20260830-example", "Example finding title")

    results = auditor_bridge.findings_to_tickets(findings_dir, tickets_dir, dry_run=True)

    assert len(results) == 1
    assert results[0]["status"] == "planned"
    assert results[0]["title"] == "Example finding title"
    assert not tickets_dir.exists()  # dry run touches nothing


def test_id_link_without_readback_reports_needs_readback_and_suppresses_duplicate(tmp_path):
    findings_dir = tmp_path / "findings"
    tickets_dir = tmp_path / "tickets"
    finding_id = current_finding_id("linked")
    make_finding(findings_dir, finding_id, "Linked without proof")
    existing = link_ticket(
        tickets_dir, "T-20261020-111111111.txt",
        f"Finding: {finding_id}\n2026-10-25 | reviewer@HOST-A | "
        "Status: linked — ticket path T-20261020-111111111.txt\n",
    )

    results = auditor_bridge.findings_to_tickets(findings_dir, tickets_dir, dry_run=False)

    assert results[0]["status"] == "needs_readback"
    assert results[0]["ticket_path"] is None
    assert results[0]["existing_ticket_paths"] == [str(existing)]
    assert "duplicate creation suppressed" in results[0]["reason"]
    assert auditor_bridge._finding_already_ticketed(finding_id, tickets_dir)
    assert len(list(ticket_writer.iter_lifecycle_files(tickets_dir))) == 1


def test_cross_reference_without_finding_line_is_not_the_findings_ticket(tmp_path):
    """A ticket that only MENTIONS the ID elsewhere (cross-reference) must not suppress creation."""
    findings_dir = tmp_path / "findings"
    tickets_dir = tmp_path / "tickets"
    finding_id = current_finding_id("crossref")
    make_finding(findings_dir, finding_id, "Never delivered")
    link_ticket(
        tickets_dir, "T-20261020-333333333.txt",
        f"Other topic entirely.\nSee also {finding_id} for background.\n"
        f"Finding: {finding_id}-other\n",  # longer, different ID on the Finding: line
    )

    results = auditor_bridge.findings_to_tickets(findings_dir, tickets_dir, dry_run=True)

    assert results[0]["status"] == "planned"
    assert not auditor_bridge._finding_already_ticketed(finding_id, tickets_dir)


def test_finding_line_is_the_dedup_key_negative_control(tmp_path):
    """Same fixture as the cross-reference case, plus the exact Finding: line -> now linked."""
    findings_dir = tmp_path / "findings"
    tickets_dir = tmp_path / "tickets"
    finding_id = current_finding_id("crossref-ctrl")
    make_finding(findings_dir, finding_id, "Delivered")
    link_ticket(
        tickets_dir, "T-20261020-444444444.txt",
        f"Other topic entirely.\nSee also {finding_id} for background.\n"
        f"Finding: {finding_id}\r\n",  # CRLF line ending is tolerated
    )

    results = auditor_bridge.findings_to_tickets(findings_dir, tickets_dir, dry_run=True)

    assert results[0]["status"] == "needs_readback"


def test_apply_delivers_exactly_what_the_dry_run_planned_despite_cross_references(tmp_path):
    """Live evidence 2026-10-24: the dry run planned 15 tickets, --apply created 12.

    Tickets created earlier in the same run quote the IDs of findings processed
    later; those quotes must not count as the later findings' tickets.
    """
    findings_dir = tmp_path / "findings"
    tickets_dir = verified_queue(tmp_path / "tickets")
    first, second, third = (current_finding_id(name) for name in ("alpha", "beta", "gamma"))
    # findings_to_tickets walks sorted(M-*.md): alpha is ticketed before beta and gamma are checked
    make_finding(findings_dir, first, "First", body=f"Quotes {second} and {third} in prose.")
    make_finding(findings_dir, second, "Second", body=f"See also {third}.")
    make_finding(findings_dir, third, "Third")

    planned = auditor_bridge.findings_to_tickets(findings_dir, tickets_dir, dry_run=True)
    applied = auditor_bridge.findings_to_tickets(findings_dir, tickets_dir, dry_run=False)

    assert [r["status"] for r in planned] == ["planned"] * 3
    assert [r["status"] for r in applied] == ["created"] * 3
    assert [r["finding_id"] for r in applied] == [r["finding_id"] for r in planned]
    assert len(list(ticket_writer.iter_lifecycle_files(tickets_dir))) == 3


def test_link_with_current_readback_is_skipped_without_creating_duplicate(tmp_path):
    findings_dir = tmp_path / "findings"
    tickets_dir = tmp_path / "tickets"
    finding_id = current_finding_id("current")
    make_finding(findings_dir, finding_id, "Measured already")
    link_ticket(
        tickets_dir, "T-20261025-111111111.txt",
        f"Finding: {finding_id}\nVERLAUF / LOG\n{readback_line(finding_id)}\n",
    )

    results = auditor_bridge.findings_to_tickets(findings_dir, tickets_dir, dry_run=False)

    assert results[0]["status"] == "skipped_existing"
    assert len(list(ticket_writer.iter_lifecycle_files(tickets_dir))) == 1


def test_link_with_stale_readback_requires_a_new_readback(tmp_path):
    findings_dir = tmp_path / "findings"
    tickets_dir = tmp_path / "tickets"
    finding_id = current_finding_id("stale")
    make_finding(findings_dir, finding_id, "Old measurement")
    existing = link_ticket(
        tickets_dir, "T-20261024-111111111.txt",
        f"Finding: {finding_id}\n"
        f"{readback_line(finding_id, measured_date=(date.today() - timedelta(days=1)).isoformat())}\n",
    )

    results = auditor_bridge.findings_to_tickets(findings_dir, tickets_dir, dry_run=True)

    assert results[0]["status"] == "needs_readback"
    assert results[0]["existing_ticket_paths"] == [str(existing)]


def test_linked_ticket_status_note_is_not_a_readback(tmp_path):
    findings_dir = tmp_path / "findings"
    tickets_dir = tmp_path / "tickets"
    finding_id = current_finding_id("status-only")
    make_finding(findings_dir, finding_id, "Status note only")
    existing = link_ticket(
        tickets_dir, "T-20261024-222222222.txt",
        f"Finding: {finding_id}\n"
        f"2026-10-25 | reviewer@HOST-A | {finding_id} status=verified — "
        "ticket status is open\n",
    )

    results = auditor_bridge.findings_to_tickets(findings_dir, tickets_dir, dry_run=True)

    assert results[0]["status"] == "needs_readback"
    assert results[0]["existing_ticket_paths"] == [str(existing)]


def test_negative_or_unconfirmed_receipt_text_is_not_current(tmp_path):
    cases = [
        ("no readback receipt; status note only", "unread", "unverified"),
        ("receipt not provided; status note only", "lib/auditor_bridge.py", "unknown"),
        ("readback is unavailable; no measurement was taken", "lib/auditor_bridge.py", "unknown"),
        ("without a verified receipt; pending", "lib/auditor_bridge.py", "locked"),
        ("kein Beleg vorhanden; Messung fehlt", "lib/auditor_bridge.py", "unknown"),
        ("receipt unconfirmed; SHA256 deadbeef", "lib/auditor_bridge.py", "locked"),
        ("not measured; lock_scan status=locked exit_code=1", "lib/auditor_bridge.py", "locked"),
        ("nicht gemessen; lock_scan status=locked exit_code=1", "lib/auditor_bridge.py", "locked"),
        ("no measurements collected; SHA256 deadbeef", "lib/auditor_bridge.py", "locked"),
        ("without a current measurement; report.md", "lib/auditor_bridge.py", "unknown"),
        ("measurement not recorded; SHA256 deadbeef", "lib/auditor_bridge.py", "unknown"),
        ("Messung noch ausstehend; report.md", "lib/auditor_bridge.py", "unknown"),
        ("no verification performed; SHA256 deadbeef", "lib/auditor_bridge.py", "unknown"),
        ("unconfirmed result; report.md", "lib/auditor_bridge.py", "locked"),
    ]
    for index, (evidence, location, value) in enumerate(cases):
        case_root = tmp_path / str(index)
        findings_dir = case_root / "findings"
        tickets_dir = case_root / "tickets"
        finding_id = current_finding_id(f"negative-receipt-{index}")
        make_finding(findings_dir, finding_id, "Unconfirmed receipt")
        existing = link_ticket(
            tickets_dir, f"T-20261025-{index + 1:09d}.txt",
            f"Finding: {finding_id}\n"
            f"{date.today().isoformat()} | reviewer@HOST-A | "
            f"Finding {finding_id} status=open location={location} value={value} "
            f"— {evidence}\n",
        )

        results = auditor_bridge.findings_to_tickets(findings_dir, tickets_dir, dry_run=True)

        assert results[0]["status"] == "needs_readback"
        assert results[0]["existing_ticket_paths"] == [str(existing)]


def test_unverified_value_overrides_a_hash_marker(tmp_path):
    findings_dir = tmp_path / "findings"
    tickets_dir = tmp_path / "tickets"
    finding_id = current_finding_id("unverified-value")
    make_finding(findings_dir, finding_id, "Explicitly unverified value")
    existing = link_ticket(
        tickets_dir, "T-20261025-101010101.txt",
        f"Finding: {finding_id}\n"
        f"{date.today().isoformat()} | reviewer@HOST-A | "
        f"Finding {finding_id} location=lib/auditor_bridge.py value=unverified "
        "— SHA256 deadbeef\n",
    )

    results = auditor_bridge.findings_to_tickets(findings_dir, tickets_dir, dry_run=True)

    assert results[0]["status"] == "needs_readback"
    assert results[0]["existing_ticket_paths"] == [str(existing)]


def test_bare_receipt_word_is_not_concrete_evidence(tmp_path):
    findings_dir = tmp_path / "findings"
    tickets_dir = tmp_path / "tickets"
    finding_id = current_finding_id("bare-receipt")
    make_finding(findings_dir, finding_id, "Receipt label only")
    existing = link_ticket(
        tickets_dir, "T-20261025-111111112.txt",
        f"Finding: {finding_id}\n"
        f"{date.today().isoformat()} | reviewer@HOST-A | "
        f"Finding {finding_id} location=lib/auditor_bridge.py value=unknown "
        "— receipt; status note only\n",
    )

    results = auditor_bridge.findings_to_tickets(findings_dir, tickets_dir, dry_run=True)

    assert results[0]["status"] == "needs_readback"
    assert results[0]["existing_ticket_paths"] == [str(existing)]


def test_unknown_and_locked_values_accept_concrete_measurements(tmp_path):
    cases = [
        ("unknown", "API readback returned status=unknown; SHA256 deadbeef"),
        ("locked", r"lock_scan --check-dir C:\repo exit_code=1 status=locked"),
        ("clear", r"lock_scan --check-dir C:\repo exit_code=0 status=clear; no lock found"),
    ]
    for index, (value, evidence) in enumerate(cases):
        case_root = tmp_path / str(index)
        findings_dir = case_root / "findings"
        tickets_dir = case_root / "tickets"
        finding_id = current_finding_id(f"measured-{value}-{index}")
        make_finding(findings_dir, finding_id, f"Measured value {value}")
        link_ticket(
            tickets_dir, f"T-20261025-{index + 11:09d}.txt",
            f"Finding: {finding_id}\n"
            f"{date.today().isoformat()} | reviewer@HOST-A | "
            f"Finding {finding_id} location=lib/auditor_bridge.py value={value} "
            f"— {evidence}\n",
        )

        results = auditor_bridge.findings_to_tickets(findings_dir, tickets_dir, dry_run=True)

        assert results[0]["status"] == "skipped_existing"


def test_readback_for_another_finding_does_not_validate_link(tmp_path):
    findings_dir = tmp_path / "findings"
    tickets_dir = tmp_path / "tickets"
    finding_id = current_finding_id("target")
    make_finding(findings_dir, finding_id, "Different readback ID")
    existing = link_ticket(
        tickets_dir, "T-20261024-333333333.txt",
        f"Finding: {finding_id}\n"
        f"{readback_line(finding_id, referenced_id='M-20261025-other')}\n",
    )

    results = auditor_bridge.findings_to_tickets(findings_dir, tickets_dir, dry_run=True)

    assert results[0]["status"] == "needs_readback"
    assert results[0]["existing_ticket_paths"] == [str(existing)]


def test_documented_free_text_readback_without_field_names_is_accepted(tmp_path):
    findings_dir = tmp_path / "findings"
    tickets_dir = tmp_path / "tickets"
    finding_id = current_finding_id("free-text")
    make_finding(findings_dir, finding_id, "Documented prose receipt")
    link_ticket(
        tickets_dir, "T-20261024-555555555.txt",
        f"Finding: {finding_id}\n"
        f"{date.today().isoformat()} | reviewer@HOST-A | Finding {finding_id} "
        "at lib/auditor_bridge.py currently reports 0 linked tickets — "
        "pytest tests/test_auditor_bridge.py -q: 35 passed; SHA256 deadbeef\n",
    )

    results = auditor_bridge.findings_to_tickets(findings_dir, tickets_dir, dry_run=True)

    assert results[0]["status"] == "skipped_existing"


def test_unrelated_finding_id_is_not_suppressed_by_other_ticket(tmp_path):
    findings_dir = tmp_path / "findings"
    tickets_dir = tmp_path / "tickets"
    finding_id = current_finding_id("new")
    make_finding(findings_dir, finding_id, "Not linked")
    link_ticket(
        tickets_dir, "T-20261024-444444444.txt",
        f"Finding: {current_finding_id('other')}\n"
        f"{readback_line(current_finding_id('other'))}\n",
    )

    results = auditor_bridge.findings_to_tickets(findings_dir, tickets_dir, dry_run=True)

    assert results[0]["status"] == "planned"


def test_longer_finding_id_does_not_validate_a_shorter_link(tmp_path):
    findings_dir = tmp_path / "findings"
    tickets_dir = tmp_path / "tickets"
    finding_id = current_finding_id("target")
    longer_id = finding_id + "-extra"
    make_finding(findings_dir, finding_id, "Exact ID only")
    link_ticket(
        tickets_dir, "T-20261024-666666666.txt",
        f"Finding: {longer_id}\n{readback_line(longer_id)}\n",
    )

    results = auditor_bridge.findings_to_tickets(findings_dir, tickets_dir, dry_run=True)

    assert results[0]["status"] == "planned"


def test_future_dated_readback_is_not_current(tmp_path):
    findings_dir = tmp_path / "findings"
    tickets_dir = tmp_path / "tickets"
    finding_id = current_finding_id("future")
    make_finding(findings_dir, finding_id, "Future receipt")
    existing = link_ticket(
        tickets_dir, "T-20261024-777777777.txt",
        f"Finding: {finding_id}\n"
        f"{readback_line(finding_id, measured_date=(date.today() + timedelta(days=1)).isoformat())}\n",
    )

    results = auditor_bridge.findings_to_tickets(findings_dir, tickets_dir, dry_run=True)

    assert results[0]["status"] == "needs_readback"
    assert results[0]["existing_ticket_paths"] == [str(existing)]


def test_findings_to_tickets_ignores_non_finding_files(tmp_path):
    findings_dir = tmp_path / "findings"
    tickets_dir = tmp_path / "tickets"
    findings_dir.mkdir(parents=True)
    (findings_dir / "README.md").write_text("# not a finding\n", encoding="utf-8")

    results = auditor_bridge.findings_to_tickets(findings_dir, tickets_dir, dry_run=True)

    assert results == []


def test_findings_to_tickets_missing_findings_dir_returns_empty(tmp_path):
    results = auditor_bridge.findings_to_tickets(
        tmp_path / "does-not-exist", tmp_path / "tickets", dry_run=True,
    )
    assert results == []


def test_findings_to_tickets_apply_creates_ticket_then_dedups_on_rerun(tmp_path):
    findings_dir = tmp_path / "findings"
    tickets_dir = verified_queue(tmp_path / "tickets")
    make_finding(findings_dir, "M-20260830-apply-me", "Apply me title")

    first = auditor_bridge.findings_to_tickets(
        findings_dir, tickets_dir, dry_run=False,
    )
    assert first[0]["status"] == "created"
    ticket_path = Path(first[0]["ticket_path"])
    assert ticket_path.is_file()
    assert ticket_path.parent.name == "INBOX"
    text = ticket_path.read_text(encoding="utf-8")
    assert "Finding: M-20260830-apply-me" in text
    assert "Apply me title" in text

    # A second run must not create a duplicate ticket for the same finding.
    # This source finding contains no receipt, so the link stays visible as
    # needs_readback instead of being promoted to a current duplicate.
    second = auditor_bridge.findings_to_tickets(findings_dir, tickets_dir, dry_run=False)
    assert second[0]["status"] == "needs_readback"
    assert second[0]["existing_ticket_paths"] == [str(ticket_path)]
    assert len(list(ticket_writer.iter_lifecycle_files(tickets_dir))) == 1


@pytest.mark.parametrize("qualification", [
    "measurement failed", "measurement never performed",
    "the measurement could not be taken", "die Messung ist gescheitert",
    "no measurements collected", "not measured", "nicht gemessen",
    "receipt unconfirmed", "Messung noch ausstehend",
    "measurement remains hypothetical", "measurement was merely requested",
    "the reported result was copied from an earlier note",
])
@pytest.mark.parametrize("position", ["statement", "before-evidence", "after-evidence"])
def test_unrecognized_qualification_cannot_borrow_positive_markers(qualification, position):
    """All clauses matter, including unseen wording beyond a negation list."""
    finding_id = current_finding_id("qualified")
    statement = f"Finding {finding_id} location=lib/auditor_bridge.py value=clear"
    evidence = "SHA256 deadbeef; report=readback.log"
    if position == "statement":
        statement += f"; {qualification}"
    elif position == "before-evidence":
        evidence = f"{qualification}; {evidence}"
    else:
        evidence += f"; {qualification}"
    line = f"{date.today()} | reviewer@host | {statement} — {evidence}"
    assert not auditor_bridge._has_current_readback(finding_id, line)


@pytest.mark.parametrize("value", [
    "unmeasured", "ungemessen", "unverified", "unconfirmed", "unread",
    "missing", "unavailable", "failed",
])
def test_nonmeasurement_or_ambiguous_failure_with_hash_alone_is_observed(value):
    finding_id = current_finding_id("no-measurement")
    line = (
        f"{date.today()} | reviewer@host | Finding {finding_id} "
        f"location=lib/auditor_bridge.py value={value} — SHA256 deadbeef"
    )
    assert not auditor_bridge._has_current_readback(finding_id, line)


@pytest.mark.parametrize(("value", "evidence"), [
    ("missing", "command=stat exit_code=1 result=missing; log=stat.log"),
    ("unavailable", "API returned http=503 status=unavailable; log=api.log"),
    ("error", "command=probe exit_code=2 result=error; log=probe.log"),
    ("failed", "pytest tests/test_probe.py -q: 2 failed; log=pytest.log"),
    ("unknown", "API readback returned status=unknown; SHA256 deadbeef"),
    ("locked", r"lock_scan --check-dir C:\repo exit_code=1 status=locked"),
    ("clear", r"lock_scan --check-dir C:\repo exit_code=0 status=clear; no lock found"),
])
def test_executed_failure_and_absence_are_real_observations(value, evidence):
    finding_id = current_finding_id("executed")
    line = (
        f"{date.today()} | reviewer@host | Finding {finding_id} "
        f"location=lib/auditor_bridge.py value={value} — {evidence}"
    )
    assert auditor_bridge._has_current_readback(finding_id, line)


def test_commit_alone_is_not_independent_readback_evidence(tmp_path):
    (tmp_path / "artifact.md").write_text("present\n", encoding="utf-8")
    finding_id = current_finding_id("commit-only")
    line = (
        f"{date.today()} | reviewer@localhost | Finding {finding_id} "
        "location=artifact.md status=clear — commit deadbee"
    )

    assert not auditor_bridge._has_current_readback(
        finding_id, line, location_base=tmp_path,
    )


@pytest.mark.parametrize("hash_clause", [
    "sha256: " + "a" * 64,
    "receipt sha256= " + "B" * 64,
    "Beleg SHA256: " + "c" * 64,
])
def test_commit_with_full_sha256_is_current_readback(hash_clause):
    finding_id = current_finding_id("commit-full-sha256")
    line = (
        f"{date.today()} | reviewer@remote-host | Finding {finding_id} "
        f"location=lib/auditor_bridge.py status=clear — {hash_clause}; commit deadbee"
    )

    assert auditor_bridge._has_current_readback(finding_id, line)


def test_commit_with_short_sha256_is_not_current_readback():
    finding_id = current_finding_id("commit-short-sha256")
    line = (
        f"{date.today()} | reviewer@remote-host | Finding {finding_id} "
        "location=lib/auditor_bridge.py status=clear — sha256: abc1234; commit deadbee"
    )

    assert not auditor_bridge._has_current_readback(finding_id, line)


def test_commit_with_numeric_count_is_current_readback(tmp_path):
    (tmp_path / "artifact.md").write_text("present\n", encoding="utf-8")
    finding_id = current_finding_id("commit-count")
    line = (
        f"{date.today()} | reviewer@localhost | Finding {finding_id} "
        "location=artifact.md value=0 — commit deadbee"
    )

    assert auditor_bridge._has_current_readback(
        finding_id, line, location_base=tmp_path,
    )


def test_commit_with_separate_count_clause_is_current_readback(tmp_path):
    (tmp_path / "artifact.md").write_text("present\n", encoding="utf-8")
    finding_id = current_finding_id("commit-count-clause")
    line = (
        f"{date.today()} | reviewer@localhost | Finding {finding_id} "
        "location=artifact.md status=clear — commit deadbee; count=0"
    )

    assert auditor_bridge._has_current_readback(
        finding_id, line, location_base=tmp_path,
    )


def test_missing_resolvable_location_needs_readback_with_reason(tmp_path):
    findings_dir = tmp_path / "findings"
    tickets_dir = tmp_path / "tickets"
    finding_id = current_finding_id("missing-location")
    make_finding(findings_dir, finding_id, "Missing local artifact")
    link_ticket(
        tickets_dir, "T-20261025-121212121.txt",
        f"Finding: {finding_id}\n"
        f"{date.today()} | reviewer@localhost | Finding {finding_id} "
        "location=does-not-exist.md value=clear "
        "— pytest tests/test_probe.py -q: 1 passed\n",
    )

    results = auditor_bridge.findings_to_tickets(
        findings_dir, tickets_dir, dry_run=True, location_base=tmp_path,
    )

    assert results[0]["status"] == "needs_readback"
    assert results[0]["reason"] == "location not found"


def test_missing_location_reason_survives_commit_only_evidence(tmp_path):
    findings_dir = tmp_path / "findings"
    tickets_dir = tmp_path / "tickets"
    finding_id = current_finding_id("missing-location-commit")
    make_finding(findings_dir, finding_id, "Missing local artifact")
    link_ticket(
        tickets_dir, "T-20261025-141414141.txt",
        f"Finding: {finding_id}\n"
        f"{date.today()} | reviewer@localhost | Finding {finding_id} "
        "location=does-not-exist.md status=clear — commit deadbee\n",
    )

    results = auditor_bridge.findings_to_tickets(
        findings_dir, tickets_dir, dry_run=True, location_base=tmp_path,
    )

    assert results[0]["status"] == "needs_readback"
    assert results[0]["reason"] == "location not found"


def test_existing_resolvable_location_is_current_readback(tmp_path):
    (tmp_path / "artifact.md").write_text("present\n", encoding="utf-8")
    finding_id = current_finding_id("existing-location")
    line = (
        f"{date.today()} | reviewer@localhost | Finding {finding_id} "
        "location=artifact.md value=clear "
        "— pytest tests/test_probe.py -q: 1 passed"
    )

    assert auditor_bridge._has_current_readback(
        finding_id, line, location_base=tmp_path,
    )


def test_location_base_defaults_to_invoking_working_directory(tmp_path, monkeypatch):
    (tmp_path / "artifact.md").write_text("present\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    finding_id = current_finding_id("cwd-location")
    line = (
        f"{date.today()} | reviewer@localhost | Finding {finding_id} "
        "location=artifact.md value=clear "
        "— pytest tests/test_probe.py -q: 1 passed"
    )

    assert auditor_bridge._has_current_readback(finding_id, line)


def test_placeholder_location_remains_syntactically_checked(tmp_path):
    finding_id = current_finding_id("placeholder-location")
    line = (
        f"{date.today()} | reviewer@localhost | Finding {finding_id} "
        "location=<OneDrive>/project/artifact.md value=0 — commit deadbee"
    )

    assert auditor_bridge._has_current_readback(
        finding_id, line, location_base=tmp_path,
    )


def test_foreign_host_location_is_not_disproved_by_local_absence(tmp_path):
    finding_id = current_finding_id("foreign-location")
    line = (
        f"{date.today()} | reviewer@remote-host | Finding {finding_id} "
        "location=does-not-exist.md value=0 — commit deadbee"
    )

    assert auditor_bridge._has_current_readback(
        finding_id, line, location_base=tmp_path,
    )


def test_unc_location_is_unresolved_without_filesystem_access(tmp_path, monkeypatch):
    def unexpected_stat(_path):
        raise AssertionError("UNC readback locations must not trigger filesystem access")

    monkeypatch.setattr(auditor_bridge.Path, "stat", unexpected_stat)
    finding_id = current_finding_id("unc-location")
    line = (
        f"{date.today()} | reviewer@localhost | Finding {finding_id} "
        r"location=\\remote-host\share\artifact.md value=0 — commit deadbee"
    )

    assert auditor_bridge._has_current_readback(
        finding_id, line, location_base=tmp_path,
    )


def test_foreign_absolute_path_flavor_is_unresolved_without_filesystem_access(
    tmp_path, monkeypatch,
):
    def unexpected_stat(_path):
        raise AssertionError("foreign path syntax must not trigger filesystem access")

    monkeypatch.setattr(auditor_bridge.Path, "stat", unexpected_stat)
    foreign_path = "/var/lib/artifact.md" if os.name == "nt" else r"C:\repo\artifact.md"
    finding_id = current_finding_id("foreign-path-flavor")
    line = (
        f"{date.today()} | reviewer@localhost | Finding {finding_id} "
        f"location={foreign_path} value=0 — commit deadbee"
    )

    assert auditor_bridge._has_current_readback(
        finding_id, line, location_base=tmp_path,
    )


def test_existing_native_absolute_location_is_current_readback(tmp_path):
    artifact = tmp_path / "artifact.md"
    artifact.write_text("present\n", encoding="utf-8")
    finding_id = current_finding_id("native-absolute-existing")
    line = (
        f"{date.today()} | reviewer@localhost | Finding {finding_id} "
        f"location={artifact.as_posix()} value=0 — commit deadbee"
    )

    assert auditor_bridge._has_current_readback(finding_id, line)


def test_missing_native_absolute_location_reports_reason(tmp_path):
    finding_id = current_finding_id("native-absolute-missing")
    line = (
        f"{date.today()} | reviewer@localhost | Finding {finding_id} "
        f"location={(tmp_path / 'missing.md').as_posix()} status=clear "
        "— pytest tests/test_probe.py -q: 1 passed"
    )

    assert auditor_bridge._current_readback_status(finding_id, line) == (
        False, "location not found",
    )


def test_commit_accepts_an_existing_independent_artifact_path(tmp_path):
    (tmp_path / "artifact.md").write_text("present\n", encoding="utf-8")
    (tmp_path / "receipt.log").write_text("measured\n", encoding="utf-8")
    finding_id = current_finding_id("commit-existing-path")
    line = (
        f"{date.today()} | reviewer@localhost | Finding {finding_id} "
        "location=artifact.md status=clear — commit deadbee; report=receipt.log"
    )

    assert auditor_bridge._has_current_readback(
        finding_id, line, location_base=tmp_path,
    )


def test_commit_rejects_a_missing_independent_artifact_path(tmp_path):
    (tmp_path / "artifact.md").write_text("present\n", encoding="utf-8")
    finding_id = current_finding_id("commit-missing-path")
    line = (
        f"{date.today()} | reviewer@localhost | Finding {finding_id} "
        "location=artifact.md status=clear — commit deadbee; report=missing.log"
    )

    assert not auditor_bridge._has_current_readback(
        finding_id, line, location_base=tmp_path,
    )


@pytest.mark.parametrize(("statement", "evidence"), [
    ("location=lib/target.py value=clear", "command=probe exit_code=1 result=failed; log=probe.log"),
    ("location=lib/target.py value=clear", "API returned status=clear status=failed"),
    ("location=lib/target.py value=clear value=failed", "SHA256 deadbeef"),
    ("location=lib/target.py value=clear state=failed", "SHA256 deadbeef"),
    ("location=lib/target.py value=failed", "pytest tests/test_probe.py -q: 2 passed"),
    ("location=lib/target.py value=clear", "pytest tests/test_probe.py -q: 2 failed"),
    ("location=unknown value=clear", "SHA256 deadbeef"),
    ("value=clear", "log=lib/target.py"),
    ("location=lib/target.py", "API returned value=clear"),
    ("location=lib/target.py value=clear", "SHA256 deadbeef not measured"),
    ("location=lib/target.py value=clear", "not measured SHA256 deadbeef"),
    ("location=lib/target.py value=clear", "no lock found"),
    ("at lib/target.py currently reports 0 linked tickets; measurement failed", "SHA256 deadbeef"),
])
def test_fields_and_evidence_cannot_be_spliced_into_an_assertion(statement, evidence):
    finding_id = current_finding_id("binding")
    line = f"{date.today()} | reviewer@host | Finding {finding_id} {statement} — {evidence}"
    assert not auditor_bridge._has_current_readback(finding_id, line)


@pytest.mark.parametrize("statement", [
    "at lib/target.py currently reports 0 linked tickets",
    "bei lib/target.py zeigt 0 verknüpfte Tickets",
    "in /repo/target.py ist unknown",
    "Ort=lib/target.py Wert=0",
    "path=lib/target.py observed=0",
    "location=lib/target.py state=unknown",
])
def test_free_text_and_existing_field_aliases_remain_supported(statement):
    finding_id = current_finding_id("legacy")
    line = f"{date.today()} | reviewer@host | Finding {finding_id} {statement} — SHA256 deadbeef"
    assert auditor_bridge._has_current_readback(finding_id, line)


@pytest.mark.parametrize(("finding_id", "when"), [
    ("M-20260230-invalid", "2026-03-01"),
    ("M-20260101-invalid", "2026-02-30"),
    ("M-20261301-invalid", "2026-12-01"),
])
def test_invalid_calendar_dates_do_not_validate_receipts(finding_id, when):
    assert not auditor_bridge._has_current_readback(
        finding_id, readback_line(finding_id, measured_date=when),
    )


def test_readback_id_is_case_sensitive_and_cannot_be_rebound_in_evidence():
    finding_id = current_finding_id("exact-case")
    assert not auditor_bridge._has_current_readback(
        finding_id, readback_line(finding_id, referenced_id=finding_id.upper()),
    )
    other_id = current_finding_id("other")
    assert not auditor_bridge._has_current_readback(
        finding_id, readback_line(other_id) + f"; Finding {finding_id}",
    )


def test_cli_uses_configured_readback_location_base(tmp_path, monkeypatch, capsys):
    base = tmp_path / "project"
    base.mkdir()
    (base / "artifact.md").write_text("present\n", encoding="utf-8")
    findings = tmp_path / "findings"
    tickets = tmp_path / "tickets"
    finding_id = current_finding_id("configured-base")
    make_finding(findings, finding_id, "Configured location base")
    link_ticket(
        tickets, "T-20261025-131313131.txt",
        f"Finding: {finding_id}\n"
        f"{date.today()} | reviewer@localhost | Finding {finding_id} "
        "location=artifact.md value=0 — commit deadbee\n",
    )
    config = write_json(
        tmp_path / "config.json",
        {"auditor_bridge": {"location_base": str(base)}},
    )
    monkeypatch.setattr(auditor_bridge, "_resolve_system_auditor_config", lambda: None)

    rc = auditor_bridge._cli([
        "--findings-to-tickets", "--config", str(config),
        "--findings-dir", str(findings), "--tickets-dir", str(tickets),
    ])

    assert rc == 0
    assert json.loads(capsys.readouterr().out)[0]["status"] == "skipped_existing"


def test_real_cli_apply_preserves_links_and_distinguishes_measurement(tmp_path):
    findings = tmp_path / "findings"
    tickets = verified_queue(tmp_path / "tickets")
    absent, measured, fresh = [current_finding_id(name) for name in ("absent", "measured", "fresh")]
    for finding_id in (absent, measured, fresh):
        make_finding(findings, finding_id, "Isolated CLI fixture")
    linked = []
    for index in (1, 2):
        linked.append(link_ticket(
            tickets, f"T-20261025-{index:09d}.txt",
            f"Finding: {absent}\n"
            f"{date.today()} | reviewer@host | Finding {absent} "
            "location=lib/probe.py value=clear; measurement never performed — SHA256 deadbeef",
        ))
    link_ticket(tickets, "T-20261025-000000003.txt",
                f"Finding: {measured}\n"
                f"{date.today()} | reviewer@host | Finding {measured} "
                "location=lib/probe.py value=missing — command=stat exit_code=1 result=missing; log=stat.log")
    config = write_json(tmp_path / "config.json", {})
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    command = [sys.executable, "-S", "-X", "utf8", str(ROOT / "lib" / "auditor_bridge.py"),
               "--findings-to-tickets", "--config", str(config), "--findings-dir", str(findings),
               "--tickets-dir", str(tickets), "--apply"]
    for expected in ("created", "needs_readback"):
        run = subprocess.run(command, capture_output=True, encoding="utf-8", env=env, timeout=30)
        assert run.returncode == 0, run.stderr
        results = {row["finding_id"]: row for row in json.loads(run.stdout)}
        assert results[absent]["status"] == "needs_readback"
        assert set(results[absent]["existing_ticket_paths"]) == {str(path) for path in linked}
        assert results[absent]["reason"]
        assert results[measured]["status"] == "skipped_existing"
        assert results[fresh]["status"] == expected
        assert len(list(ticket_writer.iter_lifecycle_files(tickets))) == 4


@pytest.mark.parametrize("key", ["status", "value", "result", "output"])
def test_command_output_scalar_is_not_truncated_to_a_prefix(key):
    finding_id = current_finding_id("whole-output")
    line = (f"{date.today()} | reviewer@host | Finding {finding_id} "
            f"location=lib/probe.py value=passed — command=probe exit_code=0 {key}=passed")
    assert auditor_bridge._has_current_readback(finding_id, line)


@pytest.mark.parametrize("codes", [
    "exit=0 exit_code=1",
    "exit_code=0 exit-code=1",
    "exitcode=0 exit code=1",
    "exit-code=0 exit=1",
    "exitcode=0 exit_code=0",
])
def test_exitcode_aliases_cannot_duplicate_a_measurement(codes):
    finding_id = current_finding_id("duplicate-exit")
    line = (f"{date.today()} | reviewer@host | Finding {finding_id} "
            f"location=lib/probe.py value=clear — command=probe {codes} result=clear")
    assert not auditor_bridge._has_current_readback(finding_id, line)


@pytest.mark.parametrize("evidence", [
    "command=probe exit_code=0 http=200 status=clear",
    "command=probe exit_code=0 status=clear value=clear",
])
def test_command_output_retains_distinct_measurement_fields(evidence):
    finding_id = current_finding_id("distinct-output")
    line = (f"{date.today()} | reviewer@host | Finding {finding_id} "
            f"location=lib/probe.py value=clear — {evidence}")
    assert auditor_bridge._has_current_readback(finding_id, line)


@pytest.mark.parametrize("evidence", [
    "command=probe exit_code=0 status=failedstatus=clear",
    "command=probe exitcode=0exit_code=1 result=clear",
    "command=probe exit_code=0 status=clearvalue=failed",
])
def test_command_output_requires_complete_scalar_tokens(evidence):
    finding_id = current_finding_id("abutted-output")
    line = (f"{date.today()} | reviewer@host | Finding {finding_id} "
            f"location=lib/probe.py value=clear — {evidence}")
    assert not auditor_bridge._has_current_readback(finding_id, line)


def _write_lifecycle_ticket(tickets_dir: Path, folder: str, name: str, body: str) -> Path:
    target = tickets_dir / folder / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")
    return target


def test_single_pass_index_matches_single_queries_and_scans_lifecycle_once(tmp_path, monkeypatch):
    """T-20261122-376591702: O(F + T) pre-indexing must match per-finding lookups
    100% across lifecycle folders while walking iter_lifecycle_files at most once."""
    findings_dir = tmp_path / "findings"
    tickets_dir = verified_queue(tmp_path / "tickets")
    base_dir = tmp_path / "project"
    base_dir.mkdir()
    (base_dir / "present.py").write_text("ok\n", encoding="utf-8")

    fid_current = current_finding_id("01-current")
    fid_needs_rb = current_finding_id("02-needs-rb")
    fid_missing_loc = current_finding_id("03-missing-loc")
    fid_multi_mixed = current_finding_id("04-multi-mixed")
    fid_fresh_a = current_finding_id("05-fresh-a")
    fid_fresh_b = current_finding_id("06-fresh-b")
    all_ids = [
        fid_current,
        fid_needs_rb,
        fid_missing_loc,
        fid_multi_mixed,
        fid_fresh_a,
        fid_fresh_b,
    ]
    for fid in all_ids:
        make_finding(
            findings_dir,
            fid,
            f"Title for {fid}",
            body=f"Cross-references {fid_fresh_b} in prose without a Finding header.",
        )

    # Populate tickets across multiple lifecycle directories, including CRLF,
    # repeated Finding lines in one ticket, shared multi-finding tickets, and noise.
    _write_lifecycle_ticket(
        tickets_dir,
        "SOLVED",
        "T-20261025-100000001.HOST-A.txt",
        f"Finding:   {fid_current}  \r\n"
        f"Finding: {fid_current}\n"
        f"{readback_line(fid_current)}\n",
    )
    t_needs = _write_lifecycle_ticket(
        tickets_dir,
        "BLOCKED",
        "T-20261025-100000002.txt",
        f"Finding: {fid_needs_rb}\n2026-10-25 | reviewer@HOST-A | Status: open — T-20261025-100000002.txt\n",
    )
    t_missing = _write_lifecycle_ticket(
        tickets_dir,
        "WAITING",
        "T-20261025-100000003.txt",
        f"Finding: {fid_missing_loc}\n"
        f"{date.today().isoformat()} | reviewer@localhost | Finding {fid_missing_loc} "
        "location=vanished.py value=clear — pytest tests/test_probe.py -q: 1 passed\n",
    )
    _write_lifecycle_ticket(
        tickets_dir,
        "ACTIONABLE",
        "T-20261025-100000004.txt",
        f"Finding: {fid_multi_mixed}\nStatus note without current receipt.\n",
    )
    _write_lifecycle_ticket(
        tickets_dir,
        "QUEUED",
        "T-20261025-100000005.txt",
        f"Finding:\t{fid_multi_mixed}\n"
        f"{date.today().isoformat()} | reviewer@localhost | Finding {fid_multi_mixed} "
        "location=present.py value=clear — pytest tests/test_probe.py -q: 1 passed\n",
    )
    _write_lifecycle_ticket(
        tickets_dir,
        "PARKED",
        "T-20261025-100000006.txt",
        f"Unrelated ticket mentioning {fid_fresh_a} and Finding: {fid_fresh_a}-other\n",
    )

    # (a) Reference equality: prebuilt index vs unindexed single-finding queries
    built_index = auditor_bridge._build_finding_ticket_index(tickets_dir)
    for fid in all_ids:
        single_links = auditor_bridge._finding_ticket_links(fid, tickets_dir)
        indexed_links = auditor_bridge._finding_ticket_links(
            fid, tickets_dir, ticket_index=built_index,
        )
        assert indexed_links == single_links

    # Compare full findings_to_tickets() output against per-finding invocations
    expected_per_finding = []
    for fid in all_ids:
        single_dir = tmp_path / "single" / fid
        make_finding(
            single_dir,
            fid,
            f"Title for {fid}",
            body=f"Cross-references {fid_fresh_b} in prose without a Finding header.",
        )
        single_res = auditor_bridge.findings_to_tickets(
            single_dir, tickets_dir, dry_run=True, location_base=base_dir,
        )
        assert len(single_res) == 1
        expected_row = dict(single_res[0])
        expected_row["finding_path"] = str(findings_dir / f"{fid}.md")
        expected_per_finding.append(expected_row)

    # (b) Spy/counter on iter_lifecycle_files: N=6 findings -> at most 1 traversal
    real_iter = ticket_writer.iter_lifecycle_files
    iter_calls = 0

    def spy_iter_lifecycle_files(base):
        nonlocal iter_calls
        iter_calls += 1
        return real_iter(base)

    monkeypatch.setattr(ticket_writer, "iter_lifecycle_files", spy_iter_lifecycle_files)

    batch_results = auditor_bridge.findings_to_tickets(
        findings_dir, tickets_dir, dry_run=True, location_base=base_dir,
    )
    assert iter_calls == 1
    assert batch_results == expected_per_finding

    by_id = {row["finding_id"]: row for row in batch_results}
    assert by_id[fid_current]["status"] == "skipped_existing"
    assert by_id[fid_needs_rb]["status"] == "needs_readback"
    assert by_id[fid_needs_rb]["existing_ticket_paths"] == [str(t_needs)]
    assert by_id[fid_missing_loc]["status"] == "needs_readback"
    assert by_id[fid_missing_loc]["existing_ticket_paths"] == [str(t_missing)]
    assert by_id[fid_missing_loc]["reason"] == "location not found"
    assert by_id[fid_multi_mixed]["status"] == "skipped_existing"
    assert by_id[fid_fresh_a]["status"] == "planned"
    assert by_id[fid_fresh_b]["status"] == "planned"

    # Empty findings directory must not walk iter_lifecycle_files at all
    empty_findings = tmp_path / "empty_findings"
    empty_findings.mkdir()
    iter_calls = 0
    assert auditor_bridge.findings_to_tickets(empty_findings, tickets_dir, dry_run=True) == []
    assert iter_calls == 0
