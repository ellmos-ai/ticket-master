"""Platzhalter-Aufloesung in Konfigurationspfaden (T-20260912-206012253).

Der Fehler, den diese Tests festhalten, war kein Absturz, sondern ein stiller
Fehlalarm: `auditor_bridge --findings-to-tickets` reichte ein `tickets_dir` mit
`<HOME>` unaufgeloest an die Dedup-Pruefung weiter. Die suchte daraufhin in einem
Verzeichnis, das buchstaeblich `<HOME>/...` heisst, fand dort natuerlich kein
einziges bestehendes Ticket und meldete jedes Finding als `planned`. Wer dem
Trockenlauf glaubte, legte Dubletten fuer laengst ticketisierte Findings an.
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

import auditor_bridge  # noqa: E402
import config_paths  # noqa: E402
from queue_helpers import verified_queue  # noqa: E402


def test_expand_placeholders_resolves_home_and_user(monkeypatch):
    monkeypatch.setattr(config_paths.Path, "home", classmethod(lambda cls: Path("/tmp/h")))
    monkeypatch.setattr(config_paths.getpass, "getuser", lambda: "tester")

    out = config_paths.expand_placeholders("<HOME>/x/<USER>/y")

    assert "<HOME>" not in out and "<USER>" not in out
    assert out.replace("\\", "/").endswith("/x/tester/y")


def test_expand_placeholders_leaves_plain_path_untouched():
    assert config_paths.expand_placeholders("C:/plain/path") == "C:/plain/path"


def test_resolve_config_path_rejects_unusable_values():
    assert config_paths.resolve_config_path(None) is None
    assert config_paths.resolve_config_path("") is None
    assert config_paths.resolve_config_path("   ") is None
    assert config_paths.resolve_config_path(42) is None


def test_resolve_config_path_accepts_pathlike(tmp_path):
    assert config_paths.resolve_config_path(tmp_path) == tmp_path


def _finding(findings_dir: Path, finding_id: str) -> None:
    findings_dir.mkdir(parents=True, exist_ok=True)
    (findings_dir / f"{finding_id}.md").write_text(
        f"# {finding_id} — Titel\n\nrumpf\n", encoding="utf-8",
    )


def test_cli_dry_run_dedups_through_a_home_placeholder(tmp_path, monkeypatch, capsys):
    """Der eigentliche Regressionsfall: `<HOME>` in der Config, Ticket existiert.

    Vor dem Fix meldete der Trockenlauf hier `planned`, weil er im Verzeichnis
    "<HOME>/tickets" nachsah statt im echten. Erwartet ist `skipped_existing`.
    """
    home = tmp_path / "home"
    findings_dir = home / "findings"
    tickets_dir = verified_queue(home / "tickets")
    _finding(findings_dir, "M-20260912-already-ticketed")
    solved = tickets_dir / "SOLVED"
    solved.mkdir(parents=True)
    (solved / "T-20260901-222222222.txt").write_text(
        "PROBLEMBESCHREIBUNG\nFinding: M-20260912-already-ticketed\n", encoding="utf-8",
    )

    config = tmp_path / "ticket-master.config.json"
    config.write_text(json.dumps({
        "findings_dir": "<HOME>/findings",
        "tickets_dir": "<HOME>/tickets",
    }), encoding="utf-8")
    monkeypatch.setattr(config_paths.Path, "home", classmethod(lambda cls: home))
    # Die system-auditor-Config darf den Test nicht mit echten Hostpfaden fuellen.
    monkeypatch.setattr(auditor_bridge, "_resolve_system_auditor_config", lambda: None)

    rc = auditor_bridge._cli(["--findings-to-tickets", "--config", str(config)])

    assert rc == 0
    results = json.loads(capsys.readouterr().out)
    assert [r["status"] for r in results] == ["skipped_existing"]


def test_cli_reports_planned_for_a_genuinely_new_finding(tmp_path, monkeypatch, capsys):
    """Gegenprobe: Der Fix darf nicht einfach alles als bekannt abtun."""
    home = tmp_path / "home"
    _finding(home / "findings", "M-20260912-brand-new")
    verified_queue(home / "tickets")

    config = tmp_path / "ticket-master.config.json"
    config.write_text(json.dumps({
        "findings_dir": "<HOME>/findings",
        "tickets_dir": "<HOME>/tickets",
    }), encoding="utf-8")
    monkeypatch.setattr(config_paths.Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(auditor_bridge, "_resolve_system_auditor_config", lambda: None)

    rc = auditor_bridge._cli(["--findings-to-tickets", "--config", str(config)])

    assert rc == 0
    results = json.loads(capsys.readouterr().out)
    assert [r["status"] for r in results] == ["planned"]


def test_repo_has_exactly_one_placeholder_expander():
    """P-009: eine Routine, nicht zwei. Ein zweiter Expander driftet.

    Genau diese Doppelung war die Ursache -- die Routine lag privat in bin/ und
    war aus lib/ nicht erreichbar.
    """
    hits = [
        path
        for path in ROOT.rglob("*.py")
        if "__pycache__" not in path.parts
        and path.name != Path(__file__).name
        and "def expand_placeholders" in path.read_text(encoding="utf-8")
    ]

    assert [p.name for p in hits] == ["config_paths.py"], hits
