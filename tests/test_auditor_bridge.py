"""Tests for lib/auditor_bridge.py (T-20260830-948243522).

Covers the load-bearing logic this module owns: the three-valued sparmodus
read, decide()'s combination of detect/spar_gate/due_check (all mocked --
never a real subprocess or network call), and findings_to_tickets()'s
dry-run dedup. system-auditor itself is never invoked here.
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

import auditor_bridge  # noqa: E402
import ticket_writer  # noqa: E402


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
            return {"reports": ["20260817 · ai-bundles · ASUS-GEI · fable-5 · self"]}
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
            return {"reports": ["20260824 · ai-bundles · ASUS-GEI · fable-5 · self"]}
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


def test_findings_to_tickets_dry_run_plans_without_writing(tmp_path):
    findings_dir = tmp_path / "findings"
    tickets_dir = tmp_path / "tickets"
    make_finding(findings_dir, "M-20260830-example", "Example finding title")

    results = auditor_bridge.findings_to_tickets(findings_dir, tickets_dir, dry_run=True)

    assert len(results) == 1
    assert results[0]["status"] == "planned"
    assert results[0]["title"] == "Example finding title"
    assert not tickets_dir.exists()  # dry run touches nothing


def test_findings_to_tickets_skips_already_ticketed_finding(tmp_path):
    findings_dir = tmp_path / "findings"
    tickets_dir = tmp_path / "tickets"
    make_finding(findings_dir, "M-20260830-already-done", "Already handled")
    solved = tickets_dir / "SOLVED"
    solved.mkdir(parents=True)
    (solved / "T-20260829-111111111.txt").write_text(
        "PROBLEMBESCHREIBUNG\nFinding: M-20260830-already-done\n", encoding="utf-8",
    )

    results = auditor_bridge.findings_to_tickets(findings_dir, tickets_dir, dry_run=True)

    assert len(results) == 1
    assert results[0]["status"] == "skipped_existing"


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
    tickets_dir = tmp_path / "tickets"
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
    second = auditor_bridge.findings_to_tickets(findings_dir, tickets_dir, dry_run=True)
    assert second[0]["status"] == "skipped_existing"
    assert len(list(ticket_writer.iter_lifecycle_files(tickets_dir))) == 1
