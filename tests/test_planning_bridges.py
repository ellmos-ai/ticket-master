"""Comprehensive tests for lib/planning_bridges.py and its CLI/boot integrations."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import re
import sqlite3
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "lib") not in sys.path:
    sys.path.insert(0, str(ROOT / "lib"))

import boot_menu  # noqa: E402
import planning_bridges  # noqa: E402
import session_boot  # noqa: E402


def _load_tm_cli():
    path = ROOT / "bin" / "ticket_master.py"
    spec = importlib.util.spec_from_file_location("ticket_master_cli_planning_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _isolate_planning_env(monkeypatch: pytest.MonkeyPatch, fake_home: Path) -> None:
    fake_home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("USERPROFILE", str(fake_home))
    for var in (
        "TM_AGENDA_SKILL_PATH",
        "TM_KALENDER_SKILL_PATH",
        "AGENT_AGENDA_PROFILE",
        "KALENDER_STORE",
        "BACH_DB",
        "TM_SESSION_PROFILE",
        "TICKET_MASTER_TICKETS_DIR",
    ):
        monkeypatch.delenv(var, raising=False)


def _make_agenda_skill(base: Path) -> Path:
    skill_dir = base / "agenda-setting"
    (skill_dir / "scripts").mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text("---\nname: agenda-setting\n---\n", encoding="utf-8")
    (skill_dir / "scripts" / "agenda.py").write_text("# stub\n", encoding="utf-8")
    return skill_dir


def _make_kalender_skill(base: Path) -> Path:
    skill_dir = base / "kalender"
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text("---\nname: kalender\n---\n", encoding="utf-8")
    (skill_dir / "kalender_core.py").write_text("# stub\n", encoding="utf-8")
    return skill_dir


def _write_agenda(
    state_root: Path,
    *,
    enabled: bool = True,
    deadline: str | None = "2027-03-31",
    items: list[dict] | None = None,
    revision: int = 1,
) -> Path:
    state_root.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": "agent-agenda.v1",
        "revision": revision,
        "agenda_id": "ag-fixture-001",
        "name": "Roadmap Q4/Q1",
        "enabled": enabled,
        "deadline": deadline,
        "items": items if items is not None else [],
    }
    path = state_root / "agenda.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _write_ticket(
    tickets_dir: Path,
    folder: str,
    ticket_id: str,
    *,
    title: str,
    priority: str = "medium",
    urgency: str = "",
    project_dir: str = "n/a",
    pipeline: str = "software",
    domain: str = "n/a",
    body: str = "Synthetic ticket description.",
) -> Path:
    target_dir = tickets_dir / folder if folder else tickets_dir
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / f"{ticket_id}.txt"
    lines = [
        "==============================================================",
        "TICKET",
        "==============================================================",
        f"ID:            {ticket_id}",
        f"TITLE:         {title}",
        f"STATUS:        {folder or 'INBOX'}",
        f"PRIORITY:      {priority}",
        f"PIPELINE:      {pipeline}",
        f"PROJECT_DIR:   {project_dir}",
        f"DOMAIN:        {domain}",
    ]
    if urgency:
        lines.append(f"URGENCY:       {urgency}")
    lines.extend([
        "--------------------------------------------------------------",
        "PROBLEM DESCRIPTION",
        "--------------------------------------------------------------",
        body,
    ])
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _create_ocean_db(db_path: Path, *, with_event_origin: bool) -> Path:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    origin_col = ", event_origin TEXT NOT NULL DEFAULT 'unknown'" if with_event_origin else ""
    conn.execute(
        f"""
        CREATE TABLE assistant_calendar (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            description TEXT,
            start_datetime TEXT NOT NULL,
            end_datetime TEXT,
            location TEXT,
            is_recurring INTEGER DEFAULT 0,
            recurrence_rule TEXT,
            reminder_minutes INTEGER,
            event_type TEXT DEFAULT 'termin',
            status TEXT DEFAULT 'geplant',
            external_id TEXT,
            dist_type INTEGER DEFAULT 0
            {origin_col},
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.commit()
    conn.close()
    return db_path


# ---------------------------------------------------------------------------
# 1. Config defaults & example parity
# ---------------------------------------------------------------------------

def test_example_config_matches_default_bridge_configs():
    example = json.loads(
        (ROOT / "config" / "ticket-master.config.example.json").read_text(encoding="utf-8")
    )
    ag_example = {
        k: v for k, v in example["agenda_bridge"].items() if not k.startswith("_comment")
    }
    cal_example = {
        k: v for k, v in example["calendar_bridge"].items() if not k.startswith("_comment")
    }
    assert ag_example == planning_bridges.DEFAULT_AGENDA_BRIDGE_CONFIG
    assert cal_example == planning_bridges.DEFAULT_CALENDAR_BRIDGE_CONFIG
    assert ag_example["enabled"] is False
    assert cal_example["enabled"] is False


# ---------------------------------------------------------------------------
# 2. Auto-discovery & status modes (disabled, absent, ready, error)
# ---------------------------------------------------------------------------

def test_planning_status_compact_when_both_disabled(tmp_path, monkeypatch):
    _isolate_planning_env(monkeypatch, tmp_path / "home")
    assert planning_bridges.planning_status({}) == {
        "agenda": {"status": "disabled"},
        "calendar": {"status": "disabled"},
    }


def test_auto_discovery_across_standard_skill_dirs_and_env_overrides(tmp_path, monkeypatch):
    fake_home = tmp_path / "home"
    _isolate_planning_env(monkeypatch, fake_home)

    # Absent when nothing is installed
    st_absent = planning_bridges.planning_status(
        {"agenda_bridge": {"enabled": True}, "calendar_bridge": {"enabled": True}}
    )
    assert st_absent["agenda"]["status"] == "absent"
    assert st_absent["calendar"]["status"] == "absent"

    # Auto-detect from ~/.agents/skills and ~/.claude/skills
    ag_skill = _make_agenda_skill(fake_home / ".agents" / "skills")
    kal_skill = _make_kalender_skill(fake_home / ".claude" / "skills")
    st_ready = planning_bridges.planning_status(
        {"agenda_bridge": {"enabled": True}, "calendar_bridge": {"enabled": True}}
    )
    assert st_ready["agenda"]["status"] == "ready"
    assert st_ready["agenda"]["skill"]["skill_path"] == str(ag_skill)
    assert st_ready["agenda"]["skill"]["source"] == "auto:agents"
    assert st_ready["calendar"]["status"] == "ready"
    assert st_ready["calendar"]["selected_backend"] == "kalender"
    assert st_ready["calendar"]["backends"]["kalender"]["skill"]["skill_path"] == str(kal_skill)

    # Env override beats home auto-discovery
    env_ag = _make_agenda_skill(tmp_path / "env_skills")
    monkeypatch.setenv("TM_AGENDA_SKILL_PATH", str(env_ag))
    det_env = planning_bridges.detect_agenda_skill({})
    assert det_env["found"] is True
    assert det_env["source"] == "env"
    assert det_env["skill_path"] == str(env_ag)

    # Explicit config override with <HOME> placeholder beats env
    cfg_ag = _make_agenda_skill(fake_home / "custom_skills")
    det_cfg = planning_bridges.detect_agenda_skill(
        {"agenda_bridge": {"skill_path": "<HOME>/custom_skills/agenda-setting"}}
    )
    assert det_cfg["found"] is True
    assert det_cfg["source"] == "config"
    assert det_cfg["skill_path"] == str(cfg_ag)


def test_detect_ocean_calendar_never_creates_missing_db_and_handles_errors(tmp_path, monkeypatch):
    fake_home = tmp_path / "home"
    _isolate_planning_env(monkeypatch, fake_home)

    missing_db = tmp_path / "nonexistent" / "ocean.db"
    info_missing = planning_bridges.detect_ocean_calendar(
        {"calendar_bridge": {"ocean_db_path": str(missing_db)}}
    )
    assert info_missing["found"] is False
    assert info_missing["status"] == "absent"
    assert not missing_db.exists()

    # Empty SQLite DB without assistant_calendar -> absent, never modified
    empty_db = tmp_path / "empty.db"
    sqlite3.connect(str(empty_db)).close()
    info_no_table = planning_bridges.detect_ocean_calendar(
        {"calendar_bridge": {"ocean_db_path": str(empty_db)}}
    )
    assert info_no_table["found"] is False
    assert info_no_table["status"] == "absent"
    assert info_no_table["has_assistant_calendar"] is False

    # Corrupt file -> graceful status="error" without raising
    corrupt_db = tmp_path / "corrupt.db"
    corrupt_db.write_bytes(b"not-a-sqlite-database")
    info_err = planning_bridges.detect_ocean_calendar(
        {"calendar_bridge": {"ocean_db_path": str(corrupt_db)}}
    )
    assert info_err["found"] is False
    assert info_err["status"] == "error"

    # Valid Ocean DB via BACH_DB env var
    valid_db = _create_ocean_db(tmp_path / "ocean_valid.db", with_event_origin=True)
    monkeypatch.setenv("BACH_DB", str(valid_db))
    info_ready = planning_bridges.detect_ocean_calendar({})
    assert info_ready["found"] is True
    assert info_ready["status"] == "ready"
    assert info_ready["source"] == "env"
    assert info_ready["has_event_origin"] is True


# ---------------------------------------------------------------------------
# 3. Agenda loading & deterministic ticket sorting
# ---------------------------------------------------------------------------

def test_sort_tickets_by_agenda_weights_timeframes_and_priorities(tmp_path, monkeypatch):
    fake_home = tmp_path / "home"
    _isolate_planning_env(monkeypatch, fake_home)

    state_root = tmp_path / "agenda_state"
    _write_agenda(
        state_root,
        enabled=True,
        deadline="2027-06-30",
        items=[
            {
                "id": "A1",
                "text": "Harden billing invoice export and tax validation",
                "weight": 95,
                "status": "pending",
                "timeframe": "2027-01",
                "category": "finance",
                "plans": [],
            },
            {
                "id": "A2",
                "text": "Upgrade router firmware diagnostics",
                "weight": 80,
                "status": "pending",
                "timeframe": "2026-12",
                "category": "network",
                "plans": [],
            },
            {
                "id": "A3",
                "text": "Refactor telemetry dashboard charts",
                "weight": 80,
                "status": "pending",
                "timeframe": "2027-Q2",
                "category": "analytics",
                "plans": [],
            },
            {
                "id": "A4",
                "text": "Completed legacy migration",
                "weight": 100,
                "status": "done",
                "timeframe": "2026-11",
                "plans": [],
            },
        ],
    )

    # Indirect resolution through agent-agenda-profile.v1
    profile_file = tmp_path / "profile.json"
    profile_file.write_text(
        json.dumps({"schema": "agent-agenda-profile.v1", "state_root": str(state_root)}),
        encoding="utf-8",
    )

    queue = tmp_path / "queue"
    _write_ticket(
        queue,
        "ACTIONABLE",
        "T-20261127-100000001",
        title="Minor cosmetic typo in changelog",
        priority="low",
    )
    _write_ticket(
        queue,
        "ACTIONABLE",
        "T-20261127-100000002",
        title="Critical production outage on auth gateway",
        priority="critical",
        urgency="sofort",
    )
    _write_ticket(
        queue,
        "QUEUED",
        "T-20261127-100000003",
        title="Telemetry dashboard chart axis fix",
        priority="medium",
        domain="analytics",
    )
    _write_ticket(
        queue,
        "ACTIONABLE",
        "T-20261127-100000004",
        title="Router firmware diagnostics timeout",
        priority="medium",
        domain="network",
    )
    _write_ticket(
        queue,
        "ACTIONABLE",
        "T-20261127-100000005",
        title="Fix billing invoice export rounding",
        priority="low",
        domain="finance",
    )
    # Ticket matching DONE item A4 must NOT receive active agenda weight
    _write_ticket(
        queue,
        "ACTIONABLE",
        "T-20261127-100000006",
        title="Completed legacy migration cleanup note",
        priority="medium",
    )
    # INBOX ticket excluded by default, included when requested
    _write_ticket(
        queue,
        "INBOX",
        "T-20261127-100000007",
        title="New billing invoice tax check request",
        priority="high",
    )

    cfg = {"agenda_bridge": {"enabled": True, "profile_path": str(profile_file)}}
    res = planning_bridges.sort_tickets_by_agenda(
        queue,
        cfg,
        reference_date=date(2026, 11, 27),
    )

    assert res["agenda_active"] is True
    assert res["count"] == 6
    assert res["matched_count"] == 3
    ordered_ids = [t["id"] for t in res["tickets"]]
    # Expected order:
    # 1. T-...005 matches A1 (weight 95)
    # 2. T-...004 matches A2 (weight 80, earlier timeframe 2026-12)
    # 3. T-...003 matches A3 (weight 80, later timeframe 2027-Q2)
    # 4. T-...002 unmatched, critical/sofort (high priority)
    # 5. T-...006 unmatched (A4 is done!), medium priority
    # 6. T-...001 unmatched, low priority
    assert ordered_ids == [
        "T-20261127-100000005",
        "T-20261127-100000004",
        "T-20261127-100000003",
        "T-20261127-100000002",
        "T-20261127-100000006",
        "T-20261127-100000001",
    ]
    assert res["tickets"][0]["matched_agenda_id"] == "A1"
    assert res["tickets"][0]["agenda_weight"] == 95
    assert res["tickets"][1]["matched_agenda_id"] == "A2"
    assert res["tickets"][1]["agenda_timeframe"] == "2026-12"
    assert res["tickets"][2]["matched_agenda_id"] == "A3"
    assert res["tickets"][3]["matched_agenda_id"] is None
    assert all(t["score"] > 0 and t["match_reasons"] for t in res["tickets"])

    # Including INBOX brings in T-...007 matched to A1 with high priority -> ranks #1 above low-priority T-...005
    res_with_inbox = planning_bridges.sort_tickets_by_agenda(
        queue,
        cfg,
        include_statuses=("ACTIONABLE", "QUEUED", "INBOX"),
        reference_date=date(2026, 11, 27),
    )
    assert res_with_inbox["count"] == 7
    assert res_with_inbox["tickets"][0]["id"] == "T-20261127-100000007"
    assert res_with_inbox["tickets"][1]["id"] == "T-20261127-100000005"


def test_expired_or_disabled_agenda_does_not_weight_tickets(tmp_path, monkeypatch):
    fake_home = tmp_path / "home"
    _isolate_planning_env(monkeypatch, fake_home)

    state_root = tmp_path / "agenda_expired"
    _write_agenda(
        state_root,
        enabled=True,
        deadline="2026-10-01",  # expired before 2026-11-27
        items=[
            {
                "id": "A1",
                "text": "Low priority documentation polish",
                "weight": 100,
                "status": "pending",
                "plans": [],
            }
        ],
    )
    queue = tmp_path / "queue"
    _write_ticket(
        queue,
        "ACTIONABLE",
        "T-20261127-200000001",
        title="Low priority documentation polish",
        priority="P3",
    )
    _write_ticket(
        queue,
        "ACTIONABLE",
        "T-20261127-200000002",
        title="Urgent kernel crash fix",
        priority="P1",
    )

    cfg = {"agenda_bridge": {"enabled": True, "state_root": str(state_root)}}
    expired_res = planning_bridges.sort_tickets_by_agenda(
        queue,
        cfg,
        reference_date=date(2026, 11, 27),
    )
    assert expired_res["agenda_active"] is False
    assert expired_res["agenda_status"] == "expired"
    assert expired_res["matched_count"] == 0
    assert [t["id"] for t in expired_res["tickets"]] == [
        "T-20261127-200000002",
        "T-20261127-200000001",
    ]
    assert expired_res["tickets"][0]["matched_agenda_id"] is None

    # Disabled agenda behaves identically
    _write_agenda(
        state_root,
        enabled=False,
        deadline="2027-12-31",
        items=[
            {
                "id": "A1",
                "text": "Low priority documentation polish",
                "weight": 100,
                "status": "pending",
                "plans": [],
            }
        ],
    )
    disabled_res = planning_bridges.sort_tickets_by_agenda(
        queue,
        cfg,
        reference_date=date(2026, 11, 27),
    )
    assert disabled_res["agenda_active"] is False
    assert disabled_res["agenda_status"] == "disabled"
    assert disabled_res["matched_count"] == 0


# ---------------------------------------------------------------------------
# 4. Calendar slot planning (collision avoidance, weekends, daily caps)
#    & idempotent --apply on kalender + Ocean (with/without event_origin)
# ---------------------------------------------------------------------------

def test_kalender_plan_collision_avoidance_weekends_cap_and_idempotent_apply(tmp_path, monkeypatch):
    fake_home = tmp_path / "home"
    _isolate_planning_env(monkeypatch, fake_home)

    kal_store = tmp_path / "kalender" / "store.db"
    kal_store.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(kal_store))
    conn.executescript(planning_bridges._KALENDER_EVENTS_SCHEMA)
    # Friday 2026-11-27 has an existing meeting 09:00-10:30 (90 min)
    now_iso = datetime(2026, 11, 27, 8, 0, tzinfo=timezone.utc).isoformat()
    conn.execute(
        """INSERT INTO events
           (id, title, date, time, duration_min, location, description,
            recurrence, ics_uid, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            "meet0001",
            "Architecture Review",
            "2026-11-27",
            "09:00",
            90,
            "Room A",
            "",
            None,
            "existing-1@kalender",
            now_iso,
            now_iso,
        ),
    )
    conn.commit()
    conn.close()

    queue = tmp_path / "queue"
    for idx in range(1, 4):
        _write_ticket(
            queue,
            "ACTIONABLE",
            f"T-20261127-30000000{idx}",
            title=f"Planned work item {idx}",
            priority="P1" if idx == 1 else "P2",
        )

    cfg = {
        "calendar_bridge": {
            "enabled": True,
            "backend": "kalender",
            "kalender_store": str(kal_store),
            "workday_start": "09:00",
            "workday_end": "17:00",
            "default_slot_minutes": 60,
            "max_tickets_per_day": 2,
            "planning_horizon_days": 5,
            "include_weekends": False,
        }
    }

    # Start on Friday 2026-11-27 -> max 2 tickets on Friday (10:30-11:30, 11:30-12:30),
    # Saturday 2026-11-28 & Sunday 2026-11-29 skipped -> 3rd ticket lands Monday 2026-11-30 09:00-10:00!
    dry = planning_bridges.plan_tickets_to_calendar(
        queue,
        cfg,
        start_date=date(2026, 11, 27),
    )
    assert dry["dry_run"] is True
    assert dry["applied"] is False
    assert dry["backend"] == "kalender"
    assert dry["planned_count"] == 3
    assert [(p["date"], p["start_time"], p["end_time"]) for p in dry["planned"]] == [
        ("2026-11-27", "10:30", "11:30"),
        ("2026-11-27", "11:30", "12:30"),
        ("2026-11-30", "09:00", "10:00"),
    ]

    # Verify dry-run wrote nothing to SQLite
    conn = sqlite3.connect(str(kal_store))
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1
    conn.close()

    # Apply #1 writes 3 events
    applied1 = planning_bridges.apply_calendar_plan(
        queue,
        cfg,
        start_date=date(2026, 11, 27),
    )
    assert applied1["applied"] is True
    assert applied1["written_count"] == 3
    assert applied1["skipped_count"] == 0

    conn = sqlite3.connect(str(kal_store))
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 4
    conn.close()

    # Apply #2 is strictly idempotent: 0 written, 3 skipped_existing, row count stays 4
    applied2 = planning_bridges.apply_calendar_plan(
        queue,
        cfg,
        start_date=date(2026, 11, 27),
    )
    assert applied2["written_count"] == 0
    assert applied2["skipped_count"] == 3

    conn = sqlite3.connect(str(kal_store))
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 4
    conn.close()


@pytest.mark.parametrize("with_event_origin", [False, True])
def test_ocean_assistant_calendar_apply_with_and_without_event_origin(
    tmp_path, monkeypatch, with_event_origin
):
    fake_home = tmp_path / "home"
    _isolate_planning_env(monkeypatch, fake_home)

    ocean_db = _create_ocean_db(
        tmp_path / f"ocean_{with_event_origin}.db",
        with_event_origin=with_event_origin,
    )
    # Seed one active busy appointment (09:00-10:00) and one cancelled appointment (10:00-11:00)
    conn = sqlite3.connect(str(ocean_db))
    now_iso = "2026-11-27T08:00:00+00:00"
    conn.execute(
        """INSERT INTO assistant_calendar
           (title, description, start_datetime, end_datetime, event_type, status, created_at, updated_at)
           VALUES (?, ?, ?, ?, 'termin', 'geplant', ?, ?)""",
        ("Morning Standup", "", "2026-11-27 09:00:00", "2026-11-27 10:00:00", now_iso, now_iso),
    )
    conn.execute(
        """INSERT INTO assistant_calendar
           (title, description, start_datetime, end_datetime, event_type, status, created_at, updated_at)
           VALUES (?, ?, ?, ?, 'termin', 'abgesagt', ?, ?)""",
        ("Cancelled Call", "", "2026-11-27 10:00:00", "2026-11-27 11:00:00", now_iso, now_iso),
    )
    conn.commit()
    conn.close()

    queue = tmp_path / "queue"
    _write_ticket(
        queue,
        "ACTIONABLE",
        "T-20261127-400000001",
        title="Implement Ocean calendar sync test",
        priority="high",
    )

    cfg = {
        "calendar_bridge": {
            "enabled": True,
            "backend": "ocean",
            "ocean_db_path": str(ocean_db),
            "include_weekends": True,
        }
    }

    applied = planning_bridges.apply_calendar_plan(
        queue,
        cfg,
        days=3,
        start_date=date(2026, 11, 27),
    )
    assert applied["applied"] is True
    assert applied["backend"] == "ocean"
    assert applied["ocean_has_event_origin"] is with_event_origin
    assert applied["written_count"] == 1
    # Must skip 09:00-10:00 (busy) and reuse 10:00-11:00 (cancelled event ignored!)
    slot = applied["written"][0]
    assert (slot["date"], slot["start_time"], slot["end_time"]) == ("2026-11-27", "10:00", "11:00")

    conn = sqlite3.connect(str(ocean_db))
    conn.row_factory = sqlite3.Row
    cols = {r[1] for r in conn.execute("PRAGMA table_info(assistant_calendar)").fetchall()}
    assert ("event_origin" in cols) is with_event_origin
    row = conn.execute(
        "SELECT * FROM assistant_calendar WHERE external_id = 'ticket-master:T-20261127-400000001'"
    ).fetchone()
    assert row is not None
    assert row["event_type"] == "ticket_plan"
    assert row["status"] == "planned"
    assert row["start_datetime"] == "2026-11-27 10:00:00"
    assert row["end_datetime"] == "2026-11-27 11:00:00"
    if with_event_origin:
        assert row["event_origin"] == "user"
    conn.close()

    # Second apply is idempotent
    applied_again = planning_bridges.apply_calendar_plan(
        queue,
        cfg,
        days=3,
        start_date=date(2026, 11, 27),
    )
    assert applied_again["written_count"] == 0
    assert applied_again["skipped_count"] == 1


def test_ocean_apply_fails_closed_when_assistant_calendar_table_missing(tmp_path, monkeypatch):
    fake_home = tmp_path / "home"
    _isolate_planning_env(monkeypatch, fake_home)

    bad_db = tmp_path / "no_table.db"
    conn = sqlite3.connect(str(bad_db))
    conn.execute("CREATE TABLE something_else (id INTEGER PRIMARY KEY)")
    conn.commit()
    conn.close()

    queue = tmp_path / "queue"
    _write_ticket(queue, "ACTIONABLE", "T-20261127-500000001", title="Test fail-closed")

    cfg = {
        "calendar_bridge": {
            "enabled": True,
            "backend": "ocean",
            "ocean_db_path": str(bad_db),
        }
    }
    # Plan dry-run reports absent backend without crashing; apply fails closed!
    plan = planning_bridges.plan_tickets_to_calendar(queue, cfg, start_date=date(2026, 11, 27))
    assert plan["backend"] is None
    assert plan["backend_status"] == "absent"
    with pytest.raises(RuntimeError, match="cannot apply calendar plan"):
        planning_bridges.apply_calendar_plan(plan, cfg)


# ---------------------------------------------------------------------------
# 5. Boot & CLI integrations (session_boot, boot_menu, planning_bridges, ticket_master)
# ---------------------------------------------------------------------------

def test_session_boot_report_and_boot_menu_offer_integration(tmp_path, monkeypatch):
    fake_home = tmp_path / "home"
    _isolate_planning_env(monkeypatch, fake_home)
    monkeypatch.setattr(boot_menu.shutil, "which", lambda name: None)
    monkeypatch.delenv("TM_MODEL", raising=False)

    # Disabled + nothing installed -> compact report and empty boot_menu actions
    rep_disabled = session_boot.report({})
    assert rep_disabled["planning_bridges"] == {
        "agenda": {"status": "disabled"},
        "calendar": {"status": "disabled"},
    }
    offer_empty = boot_menu.offer({})
    assert offer_empty["planning_bridges"]["available"] is False
    assert offer_empty["planning_bridges"]["actions"] == []

    # Install agenda-setting skill and enable agenda_bridge -> reported in both
    _make_agenda_skill(fake_home / ".agents" / "skills")
    state_root = fake_home / ".local" / "state" / "agent-agenda"
    _write_agenda(
        state_root,
        enabled=True,
        deadline="2027-05-01",
        items=[{"id": "A1", "text": "Core reliability", "weight": 90, "status": "pending", "plans": []}],
    )
    cfg_enabled = {"agenda_bridge": {"enabled": True}}
    rep_enabled = session_boot.report(cfg_enabled)
    assert rep_enabled["planning_bridges"]["agenda"]["status"] == "ready"
    assert rep_enabled["planning_bridges"]["agenda"]["agenda_state"]["agenda_active"] is True
    assert rep_enabled["planning_bridges"]["calendar"] == {"status": "disabled"}

    offer_ready = boot_menu.offer(cfg_enabled)
    assert offer_ready["planning_bridges"]["available"] is True
    assert offer_ready["planning_bridges"]["actions"] == [
        "--planning-status",
        "--sort-by-agenda",
        "--plan-calendar",
    ]


def test_planning_bridges_standalone_cli_and_ticket_master_cli(tmp_path, monkeypatch, capsys):
    fake_home = tmp_path / "home"
    _isolate_planning_env(monkeypatch, fake_home)

    _make_agenda_skill(fake_home / ".agents" / "skills")
    state_root = tmp_path / "agenda_state"
    _write_agenda(
        state_root,
        enabled=True,
        deadline="2027-08-01",
        items=[
            {
                "id": "A1",
                "text": "Queue scheduler hardening",
                "weight": 88,
                "status": "pending",
                "timeframe": "2026-12",
                "plans": [],
            }
        ],
    )
    kal_store = tmp_path / "kal.db"
    queue = tmp_path / "tickets_live"
    _write_ticket(
        queue,
        "ACTIONABLE",
        "T-20261127-600000001",
        title="Queue scheduler edge-case",
        priority="high",
    )

    cfg_path = tmp_path / "tm.config.json"
    cfg_path.write_text(
        json.dumps({
            "tickets_dir": str(queue),
            "agenda_bridge": {"enabled": True, "state_root": str(state_root)},
            "calendar_bridge": {
                "enabled": True,
                "backend": "kalender",
                "kalender_store": str(kal_store),
                "include_weekends": True,
            },
        }),
        encoding="utf-8",
    )

    # 1. Standalone lib/planning_bridges.py CLI
    assert planning_bridges._cli(["--status", "--config", str(cfg_path), "--json"]) == 0
    st_json = json.loads(capsys.readouterr().out)
    assert st_json["agenda"]["status"] == "ready"
    assert st_json["calendar"]["status"] == "ready"

    assert planning_bridges._cli(["--sort", "--config", str(cfg_path), "--json"]) == 0
    sort_json = json.loads(capsys.readouterr().out)
    assert sort_json["matched_count"] == 1
    assert sort_json["tickets"][0]["matched_agenda_id"] == "A1"

    assert planning_bridges._cli(
        ["--plan", "--days", "4", "--backend", "kalender", "--config", str(cfg_path), "--json"]
    ) == 0
    plan_json = json.loads(capsys.readouterr().out)
    assert plan_json["dry_run"] is True
    assert plan_json["planned_count"] == 1
    assert not kal_store.exists()

    # 2. Main bin/ticket_master.py CLI
    tm_cli = _load_tm_cli()

    def run_tm(argv: list[str]) -> tuple[int, str, str]:
        out_buf = io.StringIO()
        err_buf = io.StringIO()
        with contextlib.redirect_stdout(out_buf), contextlib.redirect_stderr(err_buf):
            rc = tm_cli.main(argv)
        return rc, out_buf.getvalue(), err_buf.getvalue()

    rc, out, _ = run_tm(["--planning-status", "--config", str(cfg_path), "--json"])
    assert rc == 0
    assert json.loads(out)["calendar"]["selected_backend"] == "kalender"

    rc, out, _ = run_tm(["--sort-by-agenda", "--config", str(cfg_path), "--json"])
    assert rc == 0
    assert json.loads(out)["tickets"][0]["id"] == "T-20261127-600000001"

    # Dry-run via ticket_master.py --plan-calendar
    rc, out, _ = run_tm(
        ["--plan-calendar", "--days", "4", "--backend", "kalender", "--config", str(cfg_path), "--json"]
    )
    assert rc == 0
    assert json.loads(out)["applied"] is False
    assert not kal_store.exists()

    # Apply via ticket_master.py --plan-calendar --apply
    rc, out, _ = run_tm(
        ["--plan-calendar", "--apply", "--days", "4", "--backend", "kalender", "--config", str(cfg_path), "--json"]
    )
    assert rc == 0
    applied_payload = json.loads(out)
    assert applied_payload["applied"] is True
    assert applied_payload["written_count"] == 1
    assert kal_store.is_file()

    # Invalid flag combinations fail cleanly with exit code 2
    rc, _out, err = run_tm(["--apply", "--config", str(cfg_path)])
    assert rc == 2
    assert "--apply requires --plan-calendar" in err


def test_public_privacy_cleanliness_of_planning_bridge_files():
    """Verify new module and config example contain zero absolute user paths or private LAN IPs."""
    from test_smoke import FORBIDDEN_PATTERNS

    new_and_config_files = [
        ROOT / "lib" / "planning_bridges.py",
        ROOT / "config" / "ticket-master.config.example.json",
    ]
    private_ip_re = re.compile(
        r"\b(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}|192\.168\.\d{1,3}\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})\b"
    )
    abs_home_re = re.compile(r"(?:[A-Za-z]:[\\/]+Users[\\/]+|/Users/[A-Za-z0-9_-]+/|/home/[A-Za-z0-9_-]+/)")
    for file_path in new_and_config_files:
        text = file_path.read_text(encoding="utf-8")
        assert private_ip_re.search(text) is None, f"Private LAN IP found in {file_path.name}"
        assert abs_home_re.search(text) is None, f"Absolute home path found in {file_path.name}"

    for prompt_name in ("TICKET-MASTER.de.md", "TICKET-MASTER.en.md"):
        prompt_text = (ROOT / "prompts" / prompt_name).read_text(encoding="utf-8")
        assert "(c9)" in prompt_text
        assert "--planning-status" in prompt_text
        assert "--sort-by-agenda" in prompt_text
        assert "--plan-calendar" in prompt_text
        for pattern in FORBIDDEN_PATTERNS:
            assert pattern not in prompt_text, f"Forbidden pattern {pattern!r} in {prompt_name}"

