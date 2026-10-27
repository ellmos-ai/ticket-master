import copy
import json
from pathlib import Path

import pytest

from ticket_master_runtime.results.model import (
    SCHEMA, ResultsError, append, artifact_badge, open_artifact, question_prompt, read, select_events, update_session_data, write,
)


@pytest.fixture
def session():
    return {"schema": SCHEMA, "session_id": "example-session", "host": "example-host", "revision": 1,
            "updated_at": "2026-10-27T12:00:00+00:00", "events": [event("one", "Forschung", "Ärztliche Prüfung")],
            "metrics": {"skills": [], "tokens": [], "agents": []}}


def event(eid, topic, title):
    return {"id": eid, "kind": "analysis", "created_at": "2026-10-27T12:00:00+00:00", "title": title,
            "summary": "Ergebnis", "details": "Prüfgrenze", "status": "Nur Bewertung", "source": "example-source",
            "tags": [topic], "artifacts": []}


def test_atomic_cas_rejects_stale_and_foreign_session(tmp_path, session):
    path = tmp_path / "session.json"
    sha = write(path, session)
    new = copy.deepcopy(session)
    new["revision"] = 2
    write(path, new, 1, sha)
    with pytest.raises(ResultsError):
        write(path, new, 1, sha)
    assert read(path)[0]["revision"] == 2
    new["session_id"] = "another-session"
    new["revision"] = 3
    with pytest.raises(ResultsError):
        write(path, new, 2, read(path)[1])


def test_hash_changed_without_revision_and_existing_create(tmp_path, session):
    path = tmp_path / "session.json"
    sha = write(path, session)
    with pytest.raises(ResultsError):
        write(path, session)
    path.write_text(json.dumps(session) + " ", encoding="utf-8")
    session["revision"] = 2
    with pytest.raises(ResultsError):
        write(path, session, 1, sha)


def test_writer_lock_and_invalid_update_preserve_bytes(tmp_path, session):
    path = tmp_path / "session.json"
    sha = write(path, session)
    lock = tmp_path / "session.json.write-lock"
    lock.write_text("another writer", encoding="utf-8")
    session["revision"] = 2
    with pytest.raises(ResultsError):
        write(path, session, 1, sha)
    assert lock.read_text() == "another writer"
    assert read(path)[1] == sha


def test_append_duplicate_id_and_unicode(tmp_path, session):
    path = tmp_path / "session.json"
    sha = write(path, session)
    with pytest.raises(ResultsError):
        append(path, session["events"][0], 1, sha)
    append(path, event("two", "Medizin", "Öffnung prüfen"), 1, sha)
    assert "Ärztliche" in path.read_text(encoding="utf-8")
    assert len(read(path)[0]["events"]) == 2


def test_topic_search_and_time_order(session):
    newer = event("two", "Medizin", "Prüfung abgeschlossen")
    newer["created_at"] = "2026-10-27T15:00:00+02:00"
    session["events"].append(newer)
    assert [e["id"] for e in select_events(session, "Medizin", "prüfung abgeschlossen")] == ["two"]
    assert len(select_events(session)) == 2
    assert select_events(session, order="Neueste zuerst")[0]["id"] == "two"
    assert select_events(session, query="nicht vorhanden") == []


def test_passive_artifacts_only(tmp_path, monkeypatch):
    malicious = tmp_path / "launch.cmd"
    malicious.write_text("echo unsafe")
    with pytest.raises(ResultsError):
        open_artifact({"path": str(malicious)})
    with pytest.raises(ResultsError):
        open_artifact({"path": "relative.pdf"})
    target = tmp_path / "ein Dokument & ungefähr.pdf"
    target.write_bytes(b"%PDF")
    import ticket_master_runtime.results.model as model
    calls = []
    if model.sys.platform == "win32":
        monkeypatch.setattr(model.os, "startfile", lambda p: calls.append(p))
        open_artifact({"path": str(target)})
        assert calls == [str(target.resolve())]
    assert "PDF" in artifact_badge({"path": str(target), "mime": "application/pdf"})
    assert "BILD" in artifact_badge({"path": "a.png", "mime": "image/png"})
    with pytest.raises(ResultsError):
        open_artifact({"path": str(target), "sha256": "0" * 64})


def test_unknown_tokens_not_zero(tmp_path, session):
    session["metrics"]["tokens"] = [{"label": "Native Tokens", "value": None, "source": "keine Quelle", "observed_at": session["updated_at"], "scope": "Session"}]
    path = tmp_path / "session.json"
    write(path, session)
    assert read(path)[0]["metrics"]["tokens"][0]["value"] is None
    session["metrics"]["tokens"][0]["value"] = "unbekannt"
    with pytest.raises(ResultsError):
        write(tmp_path / "invalid.json", session)


def test_schema_example_matches_runtime():
    root = Path(__file__).resolve().parents[1]
    import jsonschema
    schema = json.loads((root / "contracts/session-results/session.schema.json").read_text(encoding="utf-8"))
    example = json.loads((root / "contracts/session-results/example.json").read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator(schema).validate(example)
    from ticket_master_runtime.results.model import validate
    validate(example)


def test_agent_schema_and_runtime_reject_same_invalid_records():
    import jsonschema
    from ticket_master_runtime.results.model import validate
    root = Path(__file__).resolve().parents[1]
    schema = json.loads((root / "contracts/session-results/session.schema.json").read_text(encoding="utf-8"))
    example = json.loads((root / "contracts/session-results/example.json").read_text(encoding="utf-8"))
    validator = jsonschema.Draft202012Validator(schema)
    assert example["metrics"]["agents"]
    assert validator.is_valid(example)
    validate(example)
    for field in ("id", "status", "task"):
        for invalid in (None, 7, "", " \t\n", "name\x00tail"):
            broken = copy.deepcopy(example)
            broken["metrics"]["agents"][0][field] = invalid
            assert not validator.is_valid(broken), (field, invalid)
            with pytest.raises(ResultsError):
                validate(broken)
        missing = copy.deepcopy(example)
        del missing["metrics"]["agents"][0][field]
        assert not validator.is_valid(missing)
        with pytest.raises(ResultsError):
            validate(missing)
    for field in ("requested_model", "actual_model"):
        for valid in (None, "configured-model"):
            accepted = copy.deepcopy(example)
            accepted["metrics"]["agents"][0][field] = valid
            assert validator.is_valid(accepted)
            validate(accepted)
        broken = copy.deepcopy(example)
        broken["metrics"]["agents"][0][field] = 7
        assert not validator.is_valid(broken)
        with pytest.raises(ResultsError):
            validate(broken)


def test_cli_utf8_under_windows_legacy_stream(tmp_path, session):
    import os
    import subprocess
    import sys
    session["events"][0]["details"] = "Prüfung → Änderung: äöü"
    path = tmp_path / "session.json"
    write(path, session)
    process = subprocess.run([sys.executable, "-m", "ticket_master_runtime.results", "read", str(path)],
                             env={**os.environ, "PYTHONIOENCODING": "cp1252"}, capture_output=True)
    assert process.returncode == 0, process.stderr.decode("utf-8")
    assert json.loads(process.stdout.decode("utf-8"))["data"]["events"][0]["details"] == "Prüfung → Änderung: äöü"


def test_session_data_patch_preserves_events_and_cas(tmp_path, session):
    path = tmp_path / "session.json"
    sha = write(path, session)
    update_session_data(path, {"agent_coverage": "Beobachtete Teilmenge; Gesamtzahl unbekannt"}, 1, sha)
    current, _ = read(path)
    assert current["events"] == session["events"] and current["revision"] == 2
    with pytest.raises(ResultsError):
        update_session_data(path, {"agents": []}, 1, sha)
    with pytest.raises(ResultsError):
        update_session_data(path, {"events": []}, 2, read(path)[1])


def test_question_prompt_contains_only_stable_reference(tmp_path, session):
    session["events"][0]["title"] = 'Prüfung → "Änderung"\nTitel'
    session["events"][0]["details"] = "PRIVATE FULL REPORT NOT TO COPY"
    prompt = question_prompt(session, "one", tmp_path / "session.json")
    payload = json.loads(prompt.split("\nBitte löse", 1)[0].split("\n", 1)[1])
    assert payload["title"] == session["events"][0]["title"]
    assert payload["event_id"] == "one" and payload["displayed_revision"] == 1
    assert payload["session_file"] == str((tmp_path / "session.json").absolute())
    assert "ticket_id" not in payload and "PRIVATE FULL REPORT" not in prompt
    session["events"][0]["ticket_id"] = "example-ticket"
    assert '"ticket_id": "example-ticket"' in question_prompt(session, "one", tmp_path / "session.json")
    with pytest.raises(ResultsError):
        question_prompt(session, "missing", tmp_path / "session.json")


@pytest.mark.skipif(__import__('os').environ.get('TM_NATIVE_UI_TEST') != '1', reason="Opt-in native desktop smoke")
def test_native_ui_filters_details_and_refresh(tmp_path, session):
    import tkinter as tk
    from ticket_master_runtime.results.gui import ResultsWindow
    path = tmp_path / "session.json"
    sha = write(path, session)
    root = tk.Tk()
    previous_clipboard = None
    try:
        try:
            previous_clipboard = root.clipboard_get()
        except tk.TclError:
            pass
        window = ResultsWindow(root, path)
        root.title("TEST · Ergebnisanzeige")
        root.update()
        assert window.evidence()["viewable"]
        # Native visibility is proved above. Run the remaining programmatic
        # interactions hidden so desktop input cannot change this test's filter.
        root.withdraw()
        assert window.topic == "Alle"
        window.copy_question("one")
        assert '"event_id": "one"' in root.clipboard_get()
        window.query.set("nicht vorhanden")
        assert "0 von 1" in window.status.get()
        window.query.set("")
        window.toggle("one")
        assert "one" in window.expanded
        window.show_session()
        assert window.session_view
        session["revision"] = 2
        session["events"].append(event("two", "Medizin", "Zweite Nachricht"))
        write(path, session, 1, sha)
        window.refresh()
        assert window.data["revision"] == 2
        assert window.session_view
        assert window.topic == "Alle"
        window.session_view = False
        window.render()
        assert "2 von 2" in window.status.get()
        window.topics.selection_clear(0, "end")
        window.topics.selection_set(1)
        window.choose_topic()
        assert window.topic == "Forschung" and "1 von 2" in window.status.get()
        current, current_sha = read(path)
        current["revision"] = 3
        write(path, current, 2, current_sha)
        window.refresh()
        root.update()
        assert window.topic == "Forschung" and "1 von 2" in window.status.get()
    finally:
        if previous_clipboard is not None:
            root.clipboard_clear()
            root.clipboard_append(previous_clipboard)
            root.update()
        for timer in root.tk.call("after", "info"):
            root.after_cancel(timer)
        root.destroy()
