import copy
import json
from pathlib import Path

import pytest

from ticket_master_runtime.results.model import (
    SCHEMA, ResultsError, append, artifact_badge, artifact_kind, open_artifact, question_prompt, read, select_events, update_session_data, write,
)


@pytest.fixture
def session():
    return {"schema": SCHEMA, "session_id": "example-session", "host": "example-host", "revision": 1,
            "updated_at": "2026-10-27T12:00:00+00:00", "events": [event("one", "Forschung", "Ärztliche Prüfung")],
            "metrics": {"skills": [], "tokens": [], "agents": []}}


@pytest.fixture(scope="module")
def native_tk_app():
    """Use one Tcl/Tk interpreter; each smoke owns a disposable Toplevel."""
    import tkinter as tk
    app = tk.Tk()
    app.withdraw()
    yield app
    for timer in app.tk.call("after", "info"):
        app.after_cancel(timer)
    app.destroy()


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


def test_ui_language_uses_existing_selector_and_defaults_to_german(monkeypatch):
    from ticket_master_runtime.results.i18n import translate, ui_language
    monkeypatch.delenv("TM_LANG", raising=False)
    assert ui_language() == "de"
    assert translate(None, "read_action") == "Als gelesen markieren"
    monkeypatch.setenv("TM_LANG", "en")
    assert ui_language() == "en"
    assert translate(None, "all_topics") == "All topics"
    assert ui_language("en-US") == "en"
    assert ui_language("fr") == "de"
    assert translate("fr", "read_action") == "Als gelesen markieren"


def test_action_palette_contrast_and_requested_file_icons():
    from ticket_master_runtime.results.icons import PALETTES, contrast_ratio
    assert len({p["background"] for p in PALETTES.values()}) == 4
    for palette in PALETTES.values():
        assert contrast_ratio(palette["foreground"], palette["background"]) >= 4.5
        assert contrast_ratio(palette["foreground"], palette["hover"]) >= 4.5
    for path, expected in (("a.md", "MD"), ("a.PDF", "PDF"), ("a.html", "HTML"),
                           ("https://example.invalid/document", "URL"), ("a.url", "URL"),
                           ("a.doc", "WORD"), ("a.docx", "WORD"), ("a.txt", "TXT"),
                           ("a.py", "PY"), ("a.unknown", "FILE")):
        assert artifact_kind({"path": path}) == expected


@pytest.mark.skipif(__import__('os').environ.get('TM_NATIVE_UI_TEST') != '1', reason="Opt-in native design smoke")
def test_native_action_design_and_python_path_only(tmp_path, session, monkeypatch, native_tk_app):
    import tkinter as tk
    from ticket_master_runtime.results.gui import ActionTile, ResultsWindow
    from ticket_master_runtime.results.icons import PALETTES
    py = tmp_path / "inspection.py"
    py.write_text("raise RuntimeError('must never execute')", encoding="utf-8")
    session["events"][0]["artifacts"] = [{"path": str(py), "label": "Python-Quelle", "mime": "text/x-python"}]
    path = tmp_path / "session.json"
    write(path, session)
    root = tk.Toplevel(native_tk_app)
    copied = []
    monkeypatch.setattr(root, "clipboard_clear", lambda: None)
    monkeypatch.setattr(root, "clipboard_append", copied.append)
    try:
        window = ResultsWindow(root, path, language="de")
        root.title("TEST · Aktionsdesign")
        root.update()
        assert window.evidence()["viewable"]
        root.withdraw()
        window.cards["one"].toggle_overview()
        def collect(parent):
            found = []
            for child in parent.winfo_children():
                if isinstance(child, ActionTile):
                    found.append(child)
                else:
                    found.extend(collect(child))
            return found
        tiles = collect(window.feed)
        chat = next(t for t in tiles if t.button.cget("text") == "Nachfrage / Auftrag kopieren")
        assert abs((chat.winfo_x() + chat.winfo_width()) - chat.master.winfo_width()) <= 2
        assert chat.palette == PALETTES["chat"] and "nichts automatisch gesendet" in chat.tooltip_text
        assert any(t.palette == PALETTES["details"] for t in tiles)
        source = next(t for t in tiles if "PY ·" in t.button.cget("text"))
        assert source.palette == PALETTES["file"] and "Pfad kopieren" in source.button.cget("text")
        assert all(t.icon.find_all() for t in tiles)
        source.button.invoke()
        assert copied == [str(py)] and "keine Datei ausgeführt" in window.status.get()
        root.geometry("620x700+0+0")
        root.update()
        assert all(t.winfo_reqwidth() <= window.canvas.winfo_width() - 20 for t in collect(window.feed))
    finally:
        for timer in root.tk.call("after", "info"):
            root.after_cancel(timer)
        root.destroy()


@pytest.mark.skipif(__import__('os').environ.get('TM_NATIVE_UI_TEST') != '1', reason="Opt-in English header and read-state smoke")
def test_native_english_header_and_manual_result_receipt(tmp_path, session, native_tk_app):
    import tkinter as tk
    from ticket_master_runtime.results.gui import ResultsWindow
    path = tmp_path / "session.json"
    write(path, session)
    root = tk.Toplevel(native_tk_app)
    try:
        window = ResultsWindow(root, path, language="en")
        root.withdraw()
        root.update()
        card = window.cards["one"]
        assert window.archive.get() == "Unread"
        assert window.order.get() == "Oldest first"
        assert window.topics.get(0) == "All topics"
        assert card.more_button.cget("text") == "More"
        assert card.more_tooltip.text == "Open overview"
        assert card.read_button.button.cget("text") == "Mark as read"
        assert card.read_button.button.accessible_name == "Mark as read"
        assert card.read_button.tooltip.text == "Mark as read"
        assert card.read_button.icon_kind == "READ"
        assert card.read_button.icon.find_all()
        assert card.more_button.cget("style") == "Link.TButton"
        assert card.more_button.winfo_rootx() < card.read_button.button.winfo_rootx()
        assert card.title_label.cget("text") == "Ärztliche Prüfung"

        window.archive.set("All")
        card.toggle_overview()
        window.toggle("one")
        root.update()
        assert "one" not in read(path)[0].get("result_reads", {})
        assert card.read_button.icon_kind == "READ"

        window.read_event("one")
        root.update()
        current = read(path)[0]
        assert current["events"][0]["title"] == "Ärztliche Prüfung"
        assert current["events"][0]["tags"] == ["Forschung"]
        assert "one" in current["result_reads"]
        assert window.archive.get() == "All" and card.winfo_manager() == "pack"
        assert card.read_button.icon_kind == "READ_CHECK"
        assert card.read_button.button.cget("text") == "Read"
        assert card.title_label.cget("text") == "Ärztliche Prüfung"
    finally:
        for timer in root.tk.call("after", "info"):
            root.after_cancel(timer)
        root.destroy()


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
def test_native_ui_filters_details_and_refresh(tmp_path, session, native_tk_app, monkeypatch):
    import tkinter as tk
    from ticket_master_runtime.results.gui import ResultsWindow
    path = tmp_path / "session.json"
    sha = write(path, session)
    root = tk.Toplevel(native_tk_app)
    clipboard = []
    monkeypatch.setattr(root, "clipboard_clear", clipboard.clear)
    monkeypatch.setattr(root, "clipboard_append", clipboard.append)
    monkeypatch.setattr(root, "clipboard_get", lambda: clipboard[-1])
    try:
        window = ResultsWindow(root, path, language="de")
        root.title("TEST · Ergebnisanzeige")
        root.update()
        assert window.evidence()["viewable"]
        # Native visibility is proved above. Run the remaining programmatic
        # interactions hidden so desktop input cannot change this test's filter.
        root.withdraw()
        assert window.topic == "Alle"
        # This adapter exercises the payload without reading or changing any
        # OS clipboard format, including a previously empty clipboard.
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
        for timer in root.tk.call("after", "info"):
            root.after_cancel(timer)
        root.destroy()


def canonical_message(text="Externe Meldung"):
    return {"message_id": "M-20261027-123456789", "receipt_id": "R-20261027-234567891",
            "thread_id": "TH-20261027-345678912", "content_text": text, "origin_ref": "example:source",
            "recipient_ref": "example:user", "created_at": "2026-10-27T12:00:00+00:00",
            "delivery_mode": "on_demand", "target_snapshot": [{"target_ref": "example:local", "surface": "local_gui"}],
            "delivery_policy_snapshot": {"gui_open_counts_as_delivery": True, "config_updated_at": "2026-10-27T12:00:00+00:00"}}


def test_observed_message_cas_dedupe_and_stable_ids(tmp_path, session):
    from ticket_master_runtime.results.messages import event_from_message, ingest
    class Backend:
        calls = 0
        def observe(self, notice):
            self.calls += 1
            return canonical_message()
        def resolve(self, mid):
            assert mid == canonical_message()["message_id"]
            return canonical_message()
    backend = Backend()
    path = tmp_path / "session.json"
    sha = write(path, session)
    result = ingest(path, backend, 1, sha, notice={})
    current, current_sha = read(path)
    assert current["metrics"] == session["metrics"] and current["events"][0] == session["events"][0]
    assert len(current["events"]) == 2 and current["revision"] == 2
    assert result["event_id"] == "message:M-20261027-123456789"
    assert ingest(path, backend, 2, current_sha, message_id=canonical_message()["message_id"])["deduplicated"]
    assert read(path)[1] == current_sha
    with pytest.raises(ResultsError):
        ingest(path, backend, 1, sha, notice={})
    assert backend.calls == 1  # stale session never reaches owner.create
    duplicate = copy.deepcopy(current)
    duplicate["events"].append({**event_from_message(canonical_message()), "id": "different-event"})
    from ticket_master_runtime.results.model import validate
    with pytest.raises(ResultsError):
        validate(duplicate)


def test_message_projection_counts_schema_and_settings_cas(tmp_path, session):
    from ticket_master_runtime.results.messages import event_from_message, message_counts
    from ticket_master_runtime.results.model import update_message_settings, validate
    import jsonschema
    notice = event_from_message(canonical_message())
    session["events"].append(notice)
    assert message_counts(session["events"], {}) == (0, 1)
    assert message_counts(session["events"], {notice["id"]: {"state": "prepared"}}) == (1, 0)
    assert message_counts(session["events"], {notice["id"]: {"state": "delivered"}}) == (0, 0)
    schema = json.loads((Path(__file__).resolve().parents[1] / "contracts/session-results/session.schema.json").read_text(encoding="utf-8"))
    validator = jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker())
    validator.validate(session)
    validate(session)
    for field, value in (("message_id", "T-20261027-123456789"), ("receipt_id", "R-bad")):
        broken = copy.deepcopy(session)
        broken["events"][-1][field] = value
        assert not validator.is_valid(broken)
        with pytest.raises(ResultsError):
            validate(broken)
    path = tmp_path / "session.json"
    sha = write(path, session)
    update_message_settings(path, "on_open", 1, sha)
    current, current_sha = read(path)
    assert current["events"] == session["events"] and current["message_settings"] == {"read_mode": "on_open"}
    for mode, rev, expected in (("manual", 1, sha), ("automatic-approval", 2, current_sha)):
        with pytest.raises(ResultsError):
            update_message_settings(path, mode, rev, expected)


def test_https_links_are_explicit_validated_and_never_commands(session, monkeypatch):
    from ticket_master_runtime.results.model import validate, validate_https_url
    import ticket_master_runtime.results.model as model
    calls = []
    monkeypatch.setattr(model.webbrowser, "open", lambda url, **kw: calls.append((url, kw)) or True)
    url = "https://example.invalid/paper?q=%C3%A4&scope=review#details"
    session["events"][0]["artifacts"] = [{"path": url, "label": "Quelle", "mime": "text/html"}]
    validate(session)
    assert calls == []
    open_artifact(session["events"][0]["artifacts"][0])
    assert calls == [(url, {"new": 2})]
    for bad in ("http://example.invalid", "javascript:alert(1)", "file:///C:/a.py", "https://user:password@example.invalid", "https://example.invalid:22/x", "https://example.invalid\\@other.invalid", "https://example.invalid/\n--command"):
        with pytest.raises(ResultsError):
            validate_https_url(bad)
    with pytest.raises(ResultsError):
        open_artifact({"path": url, "sha256": "0" * 64})


@pytest.fixture
def messages_owner_backend(tmp_path):
    """Optional integration against the unchanged, explicitly pinned owner copy.

    CI without Messages-Line still runs projection/GUI-fence tests. The author
    and reviewer can supply the independently approved core and manifest hash.
    """
    import os
    from ticket_master_runtime.results.messages import ApprovedMessages
    folder = os.environ.get("TM_MESSAGES_CORE_TEST_DIR")
    manifest = os.environ.get("TM_MESSAGES_CORE_TEST_SHA256")
    if not folder or not manifest:
        pytest.skip("Explicit approved Messages-Line copy required for owner integration")
    config = {"schema": "ellmos.ticket.messages-line-config.v1", "updated_at": "2026-10-27T12:00:00Z", "enabled": True,
              "delivery_modes": {"chat_start": False, "on_demand": True, "active_chat": False},
              "targets": [{"target_ref": "example:local", "surface": "local_gui"}], "gui_open_counts_as_delivery": True,
              "message_content_logging": False, "audio_logging": False, "transport_contract_ref": "ellmos.ticket.route-intent.v1"}
    path = tmp_path / "owner-config.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    return ApprovedMessages(folder, manifest, tmp_path / "messages", path, "example:local", "example:user", "example:user")


def sample_notice():
    return {"source": "example:external-automation", "external_id": "external:request-001", "title": "Veröffentlichung wartet",
            "text": "Äußere Meldung: Freigabe steht aus. Lesen ist keine Freigabe.", "observed_at": "2026-10-27T12:00:00+00:00", "kind": "system", "tags": ["Prüfung"]}


def test_real_owner_ingestion_receipt_readback_and_cross_session_idempotence(tmp_path, session, messages_owner_backend):
    from ticket_master_runtime.results.messages import binding_for, event_from_message, ingest
    backend = messages_owner_backend
    notice = sample_notice()
    first = backend.observe(notice)
    again = backend.observe({**notice, "observed_at": "2026-10-27T13:00:00+00:00"})
    assert first == again
    with pytest.raises(ResultsError):
        backend.observe({**notice, "text": "changed content"})
    binding = binding_for(event_from_message(first))
    assert backend.status(binding)["state"] == "prepared"
    result = backend.acknowledge(binding)
    assert result["event"] == "explicit_ack" and result["observation_id"].startswith("O-")
    assert backend.status(binding)["state"] == "delivered"
    assert backend.acknowledge(binding)["already_acknowledged"]
    for index in range(2):
        path = tmp_path / f"session-{index}.json"
        session["session_id"] = f"independent-{index}"
        sha = write(path, session)
        result = ingest(path, backend, 1, sha, notice=notice)
        assert result["message_id"] == first["message_id"]
    assert len(list(backend.messages_dir.glob("M-*.json"))) == 1
    assert not (backend.messages_dir / ".observed-notice-write-lock").exists()


def test_real_owner_open_policy_gates_and_changed_binding_fail_closed(messages_owner_backend):
    from ticket_master_runtime.results.messages import binding_for, event_from_message
    backend = messages_owner_backend
    config = json.loads(backend.config_path.read_text(encoding="utf-8"))
    config["gui_open_counts_as_delivery"] = False
    backend.config_path.write_text(json.dumps(config), encoding="utf-8")
    message = backend.observe(sample_notice())
    binding = binding_for(event_from_message(message))
    # Snapshot false continues to block after current policy is enabled.
    config["gui_open_counts_as_delivery"] = True
    backend.config_path.write_text(json.dumps(config), encoding="utf-8")
    with pytest.raises(ValueError):
        backend.acknowledge(binding, "gui_opened")
    assert backend.status(binding)["state"] == "prepared"
    with pytest.raises(ResultsError):
        backend.acknowledge({**binding, "receipt_id": "R-20261027-987654321"})
    with pytest.raises(ResultsError):
        backend.acknowledge({**binding, "message_sha256": "0" * 64})
    with pytest.raises(ResultsError):
        backend.acknowledge({**binding, "projection_sha256": "0" * 64})
    # Manual user read remains possible while automatic routing is disabled.
    config["enabled"] = False
    backend.config_path.write_text(json.dumps(config), encoding="utf-8")
    assert backend.acknowledge(binding)["state"] == "delivered"
    backend.manifest_sha256 = "0" * 64
    with pytest.raises(ResultsError):
        backend.status(binding)


def test_real_owner_current_policy_blocks_open_and_success_records_gui_event(messages_owner_backend):
    from ticket_master_runtime.results.messages import binding_for, event_from_message
    backend = messages_owner_backend
    message = backend.observe(sample_notice())
    binding = binding_for(event_from_message(message))
    config = json.loads(backend.config_path.read_text(encoding="utf-8"))
    config["gui_open_counts_as_delivery"] = False
    backend.config_path.write_text(json.dumps(config), encoding="utf-8")
    with pytest.raises(ValueError):
        backend.acknowledge(binding, "gui_opened")
    assert backend.status(binding)["observations"] == []
    config["gui_open_counts_as_delivery"] = True
    backend.config_path.write_text(json.dumps(config), encoding="utf-8")
    assert backend.acknowledge(binding, "gui_opened")["event"] == "gui_opened"


def test_real_owner_adopts_legacy_notice_without_duplicate_or_lost_artifacts(tmp_path, session, messages_owner_backend):
    from ticket_master_runtime.results.messages import binding_for, ingest
    legacy = session["events"][0]
    legacy.update(kind="system", artifacts=[{"path": str(tmp_path / "evidence.md"), "label": "Beleg", "mime": "text/markdown"}])
    path = tmp_path / "session.json"
    sha = write(path, session)
    result = ingest(path, messages_owner_backend, 1, sha, event_id="one")
    current, current_sha = read(path)
    assert result["event_id"] == "one" and len(current["events"]) == 1
    projected = current["events"][0]
    for field in ("id", "source", "title", "summary", "details", "status", "artifacts"):
        assert projected[field] == legacy[field]
    assert messages_owner_backend.status(binding_for(projected))["state"] == "prepared"
    assert ingest(path, messages_owner_backend, 2, current_sha, event_id="one")["deduplicated"]
    assert read(path)[1] == current_sha
    assert messages_owner_backend.acknowledge(binding_for(projected))["state"] == "delivered"


def test_real_owner_bytecode_injection_tamper_and_ingestion_lock_fail_closed(tmp_path, messages_owner_backend):
    import shutil
    backend = messages_owner_backend
    copied = tmp_path / "owner-copy"
    shutil.copytree(backend.core_dir, copied)
    backend.core_dir = copied
    message = backend.observe(sample_notice())
    from ticket_master_runtime.results.messages import event_from_message, binding_for
    binding = binding_for(event_from_message(message))
    lock = backend.messages_dir / ".observed-notice-write-lock"
    lock.write_text("another ingestion", encoding="utf-8")
    with pytest.raises(ResultsError):
        backend.observe(sample_notice())
    assert lock.read_text() == "another ingestion"
    injected = copied / "lib/messages_line.pyd"
    injected.write_bytes(b"untrusted binary")
    with pytest.raises(ResultsError):
        backend.status(binding)
    injected.unlink()
    source = copied / "lib/messages_line.py"
    source.write_bytes(source.read_bytes() + b"\n# changed after approval\n")
    with pytest.raises(ResultsError):
        backend.acknowledge(binding)


@pytest.mark.parametrize("field,value", [
    ("external_id", "external:forged-999"),
    ("thread_id", "TH-20261027-999999999"),
    ("observed_at", "2026-10-28T12:00:00+00:00"),
])
def test_real_owner_rejects_forged_display_mapping_before_status_or_ack(messages_owner_backend, field, value):
    from ticket_master_runtime.results.messages import binding_for, event_from_message
    backend = messages_owner_backend
    message = backend.observe(sample_notice())
    event = event_from_message(message)
    forged = copy.deepcopy(event)
    forged["notification"][field] = value
    # The attacker may recompute every session-derived hash. The comparison
    # must still come from the canonical owner's stored envelope.
    forged_binding = binding_for(forged)
    before = {p.name: p.read_bytes() for p in backend.messages_dir.glob("*.json")}
    with pytest.raises(ResultsError):
        backend.status(forged_binding)
    for action in ("explicit_ack", "gui_opened"):
        with pytest.raises(ResultsError):
            backend.acknowledge(forged_binding, action)
    assert {p.name: p.read_bytes() for p in backend.messages_dir.glob("*.json")} == before
    assert backend.status(binding_for(event))["state"] == "prepared"


def test_real_owner_rejects_forged_display_time_kind_and_tags(messages_owner_backend):
    from ticket_master_runtime.results.messages import binding_for, event_from_message
    backend = messages_owner_backend
    event = event_from_message(backend.observe(sample_notice()))
    for field, value in (("created_at", "2026-10-28T12:00:00+00:00"), ("kind", "message"), ("tags", ["Fremde Zuordnung"])):
        with pytest.raises(ResultsError):
            backend.acknowledge(binding_for({**event, field: value}))
    assert backend.status(binding_for(event))["observations"] == []


def test_real_owner_does_not_trust_prepopulated_hash_namespace(messages_owner_backend, monkeypatch):
    import sys
    from types import ModuleType
    backend = messages_owner_backend
    name = "_approved_message_owner_" + backend.manifest_sha256
    package = ModuleType(name)
    package.__path__ = []
    fake = ModuleType(name + ".messages_line")
    fake.validate_messages_config = lambda value: {"substituted": True}
    fake.MessagesLine = lambda path: "foreign cached owner"
    monkeypatch.setitem(sys.modules, name, package)
    monkeypatch.setitem(sys.modules, fake.__name__, fake)
    owner, line, config = backend._owner()
    assert owner is not fake and line != "foreign cached owner"
    assert Path(owner.__file__).resolve() == backend.core_dir / "lib/messages_line.py"
    assert config.get("schema") == "ellmos.ticket.messages-line-config.v1"
    assert sys.modules[name] is package and sys.modules[fake.__name__] is fake


def test_real_owner_multiple_instances_relative_imports_and_fresh_config(tmp_path, messages_owner_backend):
    import shutil
    from ticket_master_runtime.results.messages import ApprovedMessages, binding_for, event_from_message
    first = messages_owner_backend
    other_core = tmp_path / "independent-approved-copy"
    shutil.copytree(first.core_dir, other_core)
    second = ApprovedMessages(other_core, first.manifest_sha256, first.messages_dir, first.config_path,
                              first.target, first.recipient, first.actor)
    a, _, _ = first._owner()
    b, _, _ = second._owner()
    assert a is not b
    assert Path(b.__file__).resolve() == other_core / "lib/messages_line.py"
    message = first.observe(sample_notice())
    assert second.observe(sample_notice()) == message
    binding = binding_for(event_from_message(message))
    assert second.status(binding)["state"] == "prepared"
    config = json.loads(first.config_path.read_text(encoding="utf-8"))
    config["gui_open_counts_as_delivery"] = False
    first.config_path.write_text(json.dumps(config), encoding="utf-8")
    with pytest.raises(ValueError):
        second.acknowledge(binding, "gui_opened")
    assert first.status(binding)["observations"] == []


def test_real_owner_cached_namespace_replacement_is_rejected(messages_owner_backend, monkeypatch):
    import sys
    from types import ModuleType
    backend = messages_owner_backend
    owner, _, _ = backend._owner()
    monkeypatch.setitem(sys.modules, owner.__name__, ModuleType(owner.__name__))
    with pytest.raises(ResultsError):
        backend._owner()


def test_real_owner_private_namespace_collision_is_not_overwritten(messages_owner_backend, monkeypatch):
    import sys
    from types import ModuleType, SimpleNamespace
    backend = messages_owner_backend
    monkeypatch.setattr("ticket_master_runtime.results.messages.uuid.uuid4", lambda: SimpleNamespace(hex="a" * 32))
    name = "_approved_message_owner_" + backend.manifest_sha256 + "_" + "a" * 32
    foreign = ModuleType(name)
    monkeypatch.setitem(sys.modules, name, foreign)
    with pytest.raises(ResultsError):
        backend._owner()
    assert sys.modules[name] is foreign


def test_real_owner_generic_message_projection_uses_stable_owner_time(messages_owner_backend, monkeypatch):
    from ticket_master_runtime.results.messages import binding_for, event_from_message
    backend = messages_owner_backend
    _, line, config = backend._owner()
    message = line.create(config=config, origin_ref="example:ordinary-message", recipient_ref=backend.recipient,
                          content_text="Gewöhnliche externe Nachricht · äöü", delivery_mode="on_demand")
    monkeypatch.setattr("ticket_master_runtime.results.messages.now", lambda: "2026-10-28T12:00:00+00:00")
    first = event_from_message(message)
    monkeypatch.setattr("ticket_master_runtime.results.messages.now", lambda: "2026-10-29T12:00:00+00:00")
    second = event_from_message(message)
    assert first == second and first["notification"]["observed_at"] == message["created_at"]
    assert backend.status(binding_for(first))["state"] == "prepared"


def test_cli_backend_requires_complete_explicit_configuration():
    from argparse import Namespace
    from ticket_master_runtime.results.__main__ import make_backend
    values = {"messages_" + name: None for name in ("core_dir", "core_manifest_sha256", "dir", "config", "target", "recipient", "actor")}
    assert make_backend(Namespace(**values)) is None
    with pytest.raises(ResultsError):
        make_backend(Namespace(**values), required=True)
    values["messages_core_dir"] = "/explicit/approved-owner"
    with pytest.raises(ResultsError):
        make_backend(Namespace(**values))


@pytest.mark.skipif(__import__('os').environ.get('TM_NATIVE_UI_TEST') != '1', reason="Opt-in native message smoke")
def test_native_messages_navigation_manual_default_refresh_and_race(tmp_path, session, native_tk_app, monkeypatch):
    import tkinter as tk
    import time
    from ticket_master_runtime.results.gui import ResultsWindow
    from ticket_master_runtime.results.messages import binding_for, event_from_message
    message = event_from_message(canonical_message())
    session["events"].append(message)
    path = tmp_path / "session.json"
    write(path, session)
    class Backend:
        reads = []
        def status(self, binding):
            return {"state": "prepared", "can_ack": True}
        def acknowledge(self, binding, event):
            self.reads.append((binding, event))
            return {"state": "delivered"}
    backend = Backend()
    root = tk.Toplevel(native_tk_app)
    monkeypatch.setattr("ticket_master_runtime.results.gui.messagebox.showerror", lambda *a, **kw: pytest.fail(str(a)))
    try:
        window = ResultsWindow(root, path, backend, language="de")
        root.title("TEST · Meldungen")
        root.update()
        assert window.evidence()["viewable"]
        root.withdraw()
        assert window.message_nav.winfo_y() < window.settings_nav.winfo_y()
        assert window.message_nav.icon.find_all() and window.settings_nav.icon.find_all()
        window.show_messages()
        for _ in range(40):
            root.update()
            if not window.message_status_busy:
                break
            time.sleep(.01)
            window.drain_message_jobs()
        assert backend.reads == []  # navigation and default fulltext open do not ack
        window.toggle(message["id"])
        assert backend.reads == []
        window.only_unread.set(True)
        window.read_notice(message["id"])
        for _ in range(40):
            root.update()
            window.drain_message_jobs()
            if not window.message_busy:
                break
            time.sleep(.01)
        assert backend.reads == [(binding_for(message), "explicit_ack")]
        assert "0 von 2" in window.status.get()
        # A pre-ack asynchronous response must not undo the confirmed state.
        window.message_jobs.put(("states", [(message["id"], binding_for(message), 0)], {message["id"]: {"state": "prepared"}}))
        window.drain_message_jobs()
        assert window.message_states[message["id"]]["state"] == "delivered"
        current, sha = read(path)
        current["revision"] += 1
        current["events"].append(event("new-result", "Forschung", "Neues Ergebnis"))
        write(path, current, 1, sha)
        window.refresh()
        assert window.messages_view and window.only_unread.get()
        assert window.message_states[message["id"]]["state"] == "delivered"
        # Fresh session mode is checked at the exact opening action too.
        current, sha = read(path)
        current["message_settings"] = {"read_mode": "on_open"}
        current["revision"] += 1
        write(path, current, 2, sha)
        window.refresh()
        window.toggle(message["id"])  # close
        window.toggle(message["id"])  # explicit fulltext open, not navigation/refresh
        for _ in range(40):
            root.update()
            window.drain_message_jobs()
            if not window.message_busy:
                break
            time.sleep(.01)
        assert backend.reads[-1][1] == "gui_opened" and len(backend.reads) == 2
        window.open_attempts.clear()
        current, sha = read(path)
        current["message_settings"] = {"read_mode": "manual"}
        current["revision"] += 1
        write(path, current, 3, sha)
        # No refresh yet: the UI still displays the old mode, but must not ack.
        window.toggle(message["id"])
        window.toggle(message["id"])
        assert len(backend.reads) == 2 and "Lesemodus wurde geändert" in window.status.get()
        current, sha = read(path)
        current["events"][-2]["notification"]["message_sha256"] = "0" * 64
        current["revision"] += 1
        write(path, current, 4, sha)
        window.read_notice(message["id"])
        assert len(backend.reads) == 2 and "Meldungsbezug verändert" in window.status.get()
    finally:
        for timer in root.tk.call("after", "info"):
            root.after_cancel(timer)
        root.destroy()


@pytest.mark.skipif(__import__('os').environ.get('TM_NATIVE_UI_TEST') != '1', reason="Opt-in native real-owner temp smoke")
def test_native_real_owner_mapping_tamper_refresh_and_ack(tmp_path, session, messages_owner_backend, native_tk_app, monkeypatch):
    import time
    import tkinter as tk
    from ticket_master_runtime.results.gui import ResultsWindow
    from ticket_master_runtime.results.messages import binding_for, event_from_message
    backend = messages_owner_backend
    message = event_from_message(backend.observe(sample_notice()))
    session["events"].append(message)
    path = tmp_path / "native-owner-session.json"
    write(path, session)
    root = tk.Toplevel(native_tk_app)
    errors = []
    monkeypatch.setattr("ticket_master_runtime.results.gui.messagebox.showerror", lambda *a, **kw: errors.append(a))
    try:
        window = ResultsWindow(root, path, backend, language="de")
        root.title("TEST · Kanonische Zuordnung · nur Tempstore")
        root.update()
        evidence = window.evidence()
        assert evidence["viewable"] and evidence["mapped"]
        if __import__('os').name == "nt":
            assert evidence["IsWindowVisible"] and evidence["process_session_id"] == evidence["active_console_session_id"]
        (tmp_path / "native-evidence.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
        root.withdraw()
        def settle():
            for _ in range(100):
                root.update()
                window.drain_message_jobs()
                if not window.message_status_busy and not window.message_busy:
                    return
                time.sleep(.01)
            pytest.fail("Native owner job did not settle")
        window.show_messages()
        settle()
        before = {p.name: p.read_bytes() for p in backend.messages_dir.glob("*.json")}
        current, sha = read(path)
        current["events"][-1]["notification"]["external_id"] = "external:forged-native"
        current["revision"] += 1
        write(path, current, 1, sha)
        # A current-file change blocks the stale visible button before owner I/O.
        window.read_notice(message["id"])
        assert "Meldungsbezug verändert" in window.status.get()
        assert not window.message_busy
        window.refresh()
        settle()
        assert window.message_states[message["id"]]["state"] == "unknown"
        assert window.cards[message["id"]].winfo_manager() == "pack"
        window.read_notice(message["id"])
        settle()
        assert errors and window.message_states[message["id"]]["state"] == "unknown"
        assert {p.name: p.read_bytes() for p in backend.messages_dir.glob("*.json")} == before
        current, sha = read(path)
        current["events"][-1] = message
        current["revision"] += 1
        write(path, current, 2, sha)
        window.refresh()
        settle()
        assert window.message_states[message["id"]]["state"] == "prepared"
        window.read_notice(message["id"])
        settle()
        assert window.message_states[message["id"]]["state"] == "delivered"
        assert backend.status(binding_for(message))["state"] == "delivered"
        assert not window.cards[message["id"]].winfo_manager()
        window.archive.set("Archiv")
        assert window.cards[message["id"]].winfo_manager() == "pack"
        assert "result_reads" not in read(path)[0]  # no local message-read truth
    finally:
        for timer in root.tk.call("after", "info"):
            root.after_cancel(timer)
        root.destroy()


def test_result_read_content_binding_cas_archive_and_no_message_ack(tmp_path, session):
    from ticket_master_runtime.results.model import event_digest, is_read, mark_result_read, visible_events, validate
    from ticket_master_runtime.results.messages import event_from_message
    import jsonschema
    path = tmp_path / "session.json"
    notice = event_from_message(canonical_message())
    session["events"].append(notice)
    sha = write(path, session)
    stamp = event_digest(session["events"][0])
    mark_result_read(path, "one", 1, sha, stamp)
    current, current_sha = read(path)
    assert is_read(current, current["events"][0])
    assert [e["id"] for e in visible_events(current)] == [notice["id"]]
    assert [e["id"] for e in visible_events(current, archive="Archiv")] == ["one"]
    assert len(visible_events(current, archive="Alle")) == 2
    assert len(visible_events(current, {notice["id"]: {"state": "unknown"}})) == 1
    assert visible_events(current, {notice["id"]: {"state": "delivered"}}) == []
    assert current["metrics"] == session["metrics"] and current["events"] == session["events"]
    schema = json.loads((Path(__file__).resolve().parents[1] / "contracts/session-results/session.schema.json").read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker()).validate(current)
    for eid in (notice["id"], "missing"):
        with pytest.raises(ResultsError):
            mark_result_read(path, eid, 2, current_sha, stamp)
    with pytest.raises(ResultsError):
        mark_result_read(path, "one", 1, sha, stamp)
    assert read(path)[1] == current_sha
    current["revision"] = 3
    current["events"][0]["details"] += " · neue Erkenntnis"
    write(path, current, 2, current_sha)
    changed, changed_sha = read(path)
    assert not is_read(changed, changed["events"][0])
    with pytest.raises(ResultsError):
        mark_result_read(path, "one", 3, changed_sha, stamp)
    malformed = copy.deepcopy(changed)
    malformed["result_reads"][notice["id"]] = malformed["result_reads"]["one"]
    with pytest.raises(ResultsError):
        validate(malformed)
    malformed = copy.deepcopy(changed)
    malformed["result_reads"]["one"]["read_at"] = "2026-10-28"
    with pytest.raises(ResultsError):
        validate(malformed)


def test_lock_profile_pins_provenance_fence_and_gui_without_scan(tmp_path, monkeypatch):
    from ticket_master_runtime.results.locks import LockView
    from ticket_master_runtime.results.model import digest
    import ticket_master_runtime.results.locks as adapter
    # A dependency-free copy of the installed canonical parser, not a fake
    # positive fence implementation. All mutable objects remain in tmp_path.
    source = Path.home() / "OneDrive/_scripts/lock_utils.py"
    if not source.is_file():
        pytest.skip("Installed Lock-Master optional")
    owner = tmp_path / "lock_utils.py"
    owner.write_bytes(source.read_bytes())
    lock = tmp_path / "LOCK.test.txt"
    lock.write_text("owner: test-owner\ncreated: 2026-10-28T00:00:00Z\nexpires_after: 876000h\nfence: 123\n", encoding="utf-8")
    proof = tmp_path / "author-proof.json"
    claim = {"session_id": "example-session", "agent": "example-agent", "owner": "test-owner", "fence": 123, "path": str(lock)}
    proof.write_text(json.dumps({**claim, "source_reference": "Synthetic author receipt, test only"}), encoding="utf-8")
    gui_source = tmp_path / "web_server.py"
    gui_source.write_text("# reviewed existing local dashboard entry", encoding="utf-8")
    profile = {"schema": "ellmos.ticket.lock-view-profile.v1", "session_id": "example-session",
               "owner_module": str(owner), "owner_module_sha256": digest(owner.read_bytes()),
               "claims": [{**{k: v for k, v in claim.items() if k != "session_id"}, "proof_path": str(proof), "proof_sha256": digest(proof.read_bytes())}],
               "gui": {"kind": "existing_local_dashboard", "url": "http://127.0.0.1:8095/", "source_path": str(gui_source), "source_sha256": digest(gui_source.read_bytes())}}
    path = tmp_path / "profile.json"
    def reader():
        path.write_text(json.dumps(profile), encoding="utf-8")
        return LockView(path, digest(path.read_bytes()))
    view = reader()
    calls = []
    monkeypatch.setattr(adapter.webbrowser, "open", lambda *a, **k: calls.append((a,k)) or True)
    assert view.snapshot("example-session")[0]["status"] == "held"
    assert not calls  # neither scan nor browser/daemon on refresh
    view.open_gui()
    assert calls == [(("http://127.0.0.1:8095/",), {"new": 2})]
    with pytest.raises(ResultsError):
        view.snapshot("another-session")
    lock.write_text(lock.read_text().replace("fence: 123", "fence: 124"), encoding="utf-8")
    assert view.snapshot("example-session")[0]["status"] == "lost"
    lock.write_text(lock.read_text().replace("test-owner", "foreign-owner"), encoding="utf-8")
    assert view.snapshot("example-session")[0]["status"] == "unknown"
    profile["claims"][0]["agent"] = "other-agent"
    with pytest.raises(ResultsError):
        reader().snapshot("example-session")
    profile["claims"][0]["agent"] = "example-agent"
    profile["gui"]["url"] = "http://evil.invalid/"
    with pytest.raises(ResultsError):
        reader().open_gui()
    profile["gui"] = None
    view = reader()
    owner.write_text("raise RuntimeError('must not execute')", encoding="utf-8")
    with pytest.raises(ResultsError):
        view.snapshot("example-session")


@pytest.mark.skipif(__import__('os').environ.get('TM_NATIVE_UI_TEST') != '1', reason="Own native state and archive regression")
def test_native_incremental_identity_scroll_focus_text_selection_and_archive(tmp_path, session, native_tk_app, monkeypatch):
    import tkinter as tk
    from ticket_master_runtime.results.gui import ResultsWindow
    from ticket_master_runtime.results.model import event_digest
    session["events"] += [event(str(i), "Weitere", f"Ergebnis {i}") for i in range(12)]
    session["events"][0]["details"] = "\n".join(f"Zeile {i} · Überprüfung" for i in range(100))
    path = tmp_path / "session.json"
    write(path, session)
    root = tk.Toplevel(native_tk_app)
    try:
        w = ResultsWindow(root, path, language="de")
        root.title("TEST · Inkrementelle Ergebnisanzeige")
        root.update()
        assert w.evidence()["viewable"]
        assert not w.expanded and not w.cards["one"].overview.winfo_manager()
        card = w.cards["one"]
        assert not card.actions.winfo_manager()
        assert w.session_nav.palette["background"] == "#c12d3d"
        assert card.window.session_nav.icon.database_image.width() >= 20
        w.toggle("one")
        card.toggle_overview()
        root.update()
        assert card.details_button.master is card.question_button.master
        assert card.details_button.winfo_y() == card.question_button.winfo_y()
        assert card.details_button.winfo_height() == card.question_button.winfo_height()
        assert card.details_button.button.winfo_height() == card.question_button.button.winfo_height()
        assert card.details_button.button.winfo_rooty() == card.question_button.button.winfo_rooty()
        assert card.question_button.winfo_x() > card.details_button.winfo_x()
        assert card.header.winfo_y() < card.overview.winfo_y() < card.actions.winfo_y() < card.panel.winfo_y()
        card.text.yview_moveto(.4)
        card.text.tag_add("sel", "40.0", "40.5")
        card.text.mark_set("insert", "40.2")
        card.text.focus_force()
        w.canvas.yview_moveto(.05)
        root.update()
        old_y, text_y, focus = w.canvas.canvasy(0), card.text.yview(), root.focus_get()
        current, sha = read(path)
        append(path, event("appended", "Neu", "Späteres Ergebnis"), current["revision"], sha)
        w.refresh()
        root.update()
        assert w.cards["one"] is card
        assert root.focus_get() is focus
        assert card.text.tag_ranges("sel") and str(card.text.index("insert")) == "40.2"
        assert card.text.yview() == text_y
        assert abs(w.canvas.canvasy(0) - old_y) <= 3
        assert card.overview_open and "one" in w.expanded
        w.search.insert(0, "Ergebnis")
        root.update()
        assert w.query.get() == "Ergebnis"
        w.query.set("")
        w.search.focus_force()
        w.search.insert(0, "Ärztliche")
        w.search.selection_range(0, 3)
        w.search.icursor(3)
        root.update()
        search = w.search
        current, sha = read(path)
        update_session_data(path, {"agent_coverage": "Beobachtete Teilmenge"}, current["revision"], sha)
        w.refresh()
        root.update()
        assert w.search is search and root.focus_get() is search
        assert w.query.get() == "Ärztliche" and search.selection_present() and search.index("insert") == 3
        w.query.set("")
        # A producer race cannot hide the currently displayed result.
        current, sha = read(path)
        append(path, event("concurrent", "Neu", "CAS-Konkurrenz"), current["revision"], sha)
        w.read_event("one")
        assert "Nicht als gelesen" in w.status.get()
        assert "one" not in read(path)[0].get("result_reads", {})
        w.refresh()
        w.read_event("one")
        root.update()
        assert not card.winfo_manager()
        assert "Forschung" not in w.topics.get(0,"end")
        assert read(path)[0]["result_reads"]["one"]["event_sha256"] == event_digest(session["events"][0])
        w.archive.set("Archiv")
        root.update()
        assert w.cards["one"] is card and card.winfo_manager()
        assert "Forschung" in w.topics.get(0,"end")
        assert card.overview_open and "one" in w.expanded
        w.show_session()
        root.update()
        assert w.lock_gui_button.instate(["disabled"])
    finally:
        for timer in root.tk.call("after", "info"):
            root.after_cancel(timer)
        root.destroy()


def test_comments_cas_validation_cli_and_read_reopen(tmp_path, session):
    import jsonschema
    import subprocess
    import sys
    from ticket_master_runtime.results.model import (
        add_comment, event_digest, is_read, mark_result_read, validate, visible_events,
    )
    path = tmp_path / "session.json"
    sha = write(path, session)
    sha = add_comment(path, "one", "user", "Bitte Prüfgrenze für Abschnitt 2 erläutern.", 1, sha)
    current, current_sha = read(path)
    assert sha == current_sha and current["revision"] == 2
    assert len(current["events"][0]["comments"]) == 1
    assert current["events"][0]["comments"][0]["author"] == "user"
    assert current["events"][0]["comments"][0]["text"] == "Bitte Prüfgrenze für Abschnitt 2 erläutern."
    assert current["events"][0]["comments"][0]["id"] == "c-1"

    # Mark result as read after user comment; then a model reply via CLI reopens it as unread.
    sha = mark_result_read(path, "one", 2, sha, event_digest(current["events"][0]))
    read_state, _ = read(path)
    assert is_read(read_state, read_state["events"][0])
    assert visible_events(read_state, archive="Ungelesen") == []

    proc = subprocess.run(
        [sys.executable, "-m", "ticket_master_runtime.results", "comment", str(path),
         "--event-id", "one", "--author", "model",
         "--text", "Abschnitt 2 wurde gegen das Schema geprüft; keine Abweichung.",
         "--comment-id", "c-model-1", "--revision", "3", "--sha256", sha],
        capture_output=True, text=True, encoding="utf-8",
    )
    assert proc.returncode == 0, proc.stderr
    after_reply, reply_sha = read(path)
    assert after_reply["revision"] == 4
    assert len(after_reply["events"][0]["comments"]) == 2
    assert after_reply["events"][0]["comments"][1]["id"] == "c-model-1"
    assert after_reply["events"][0]["comments"][1]["author"] == "model"
    assert not is_read(after_reply, after_reply["events"][0])
    assert [e["id"] for e in visible_events(after_reply, archive="Ungelesen")] == ["one"]

    schema = json.loads((Path(__file__).resolve().parents[1] / "contracts/session-results/session.schema.json").read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker()).validate(after_reply)

    # Reject duplicate comment IDs, stale CAS, missing events, or empty comment text.
    with pytest.raises(ResultsError):
        add_comment(path, "one", "model", "Doppelte ID", 4, reply_sha, comment_id="c-model-1")
    with pytest.raises(ResultsError):
        add_comment(path, "one", "user", "Veraltete Revision", 3, sha)
    with pytest.raises(ResultsError):
        add_comment(path, "missing", "user", "Unbekanntes Event", 4, reply_sha)
    with pytest.raises(ResultsError):
        add_comment(path, "one", "user", "   ", 4, reply_sha)
    broken = copy.deepcopy(after_reply)
    broken["events"][0]["comments"][0]["created_at"] = "ohne-zeitzone"
    with pytest.raises(ResultsError):
        validate(broken)


@pytest.mark.skipif(__import__('os').environ.get('TM_NATIVE_UI_TEST') != '1', reason="Opt-in native hierarchy, unclipped read icon, comments, and file tile smoke")
def test_native_read_icon_unclipped_hierarchy_comments_and_file_tiles(tmp_path, session, native_tk_app):
    import tkinter as tk
    from ticket_master_runtime.results.gui import ActionTile, ResultsWindow
    from ticket_master_runtime.results.model import add_comment
    doc = tmp_path / "Ärztlicher_Prüfbericht_Übersicht_2026.pdf"
    doc.write_bytes(b"%PDF-1.4")
    session["events"][0]["artifacts"] = [
        {"path": str(doc), "label": "Ärztlicher Prüfbericht\nÜbersicht 2026", "mime": "application/pdf"}
    ]
    path = tmp_path / "session.json"
    write(path, session)
    root = tk.Toplevel(native_tk_app)
    try:
        window = ResultsWindow(root, path, language="de")
        root.withdraw()
        root.update()
        card = window.cards["one"]

        # 1. Read icon on the right of the first header line is unclipped, visible, and clickable.
        assert card.read_button.button.winfo_width() >= 28
        assert card.read_button.button.winfo_height() >= 24
        assert card.read_button.icon.winfo_width() >= 24
        assert card.read_button.icon.winfo_height() >= 24
        assert card.read_button.tooltip.text == "Als gelesen markieren"
        assert card.read_button.icon_kind == "READ"
        assert card.title_label.winfo_rootx() < card.more_button.winfo_rootx() < card.read_button.button.winfo_rootx()

        # 2. Compact start: Body (overview), actions, and fulltext panel are hidden until "Mehr" is clicked.
        assert not card.overview.winfo_manager()
        assert not card.actions.winfo_manager()
        assert not card.panel.winfo_manager()

        # File tiles never shrink to wraplength=48 while overview is still closed.
        file_tiles = [w for w in card.artifacts.winfo_children() if isinstance(w, ActionTile)]
        assert len(file_tiles) == 1
        file_tile = file_tiles[0]
        assert int(file_tile.button.cget("wraplength")) >= 140
        assert "\n" not in file_tile.button.cget("text")
        assert "PDF · Ärztlicher Prüfbericht Übersicht 2026" in file_tile.button.cget("text")
        assert file_tile.button.cget("anchor") == "w"

        # 3. Clicking "Mehr" expands the Body inline directly below the header, and places actions BELOW the Body.
        card.more_button.invoke()
        root.update()
        assert card.overview_open
        assert card.more_button.cget("text") == "Weniger"
        assert card.overview.winfo_manager() == "pack"
        assert card.actions.winfo_manager() == "pack"
        assert not card.panel.winfo_manager()
        assert card.header.winfo_y() < card.overview.winfo_y() < card.actions.winfo_y()
        assert card.details_button.button.cget("text") == "Ergebnistext öffnen"
        assert card.question_button.button.cget("text") == "Nachfrage / Auftrag kopieren"
        assert int(file_tile.button.cget("wraplength")) >= 140
        assert file_tile.winfo_width() >= 200

        # 4. Inline comment thread: user adds a comment in the GUI, model answers via CAS, refresh updates thread.
        card.comment_var.set("Bitte kurze Rückmeldung zur Validierung.")
        card.comment_button.invoke()
        root.update()
        assert card.comment_var.get() == ""
        assert window.data["revision"] == 2
        assert window.data["events"][0]["comments"][0]["text"] == "Bitte kurze Rückmeldung zur Validierung."
        add_comment(path, "one", "model", "Validierung ist vollständig abgeschlossen.", 2, window.sha)
        window.refresh()
        root.update()
        assert window.data["revision"] == 3
        assert len(card.comments_list.winfo_children()) == 2

        # 5. Clicking "Ergebnistext öffnen" opens fulltext panel below the actions bar.
        card.details_button.button.invoke()
        root.update()
        assert card.panel.winfo_manager() == "pack"
        assert card.details_button.button.cget("text") == "Ergebnistext schließen"
        assert card.header.winfo_y() < card.overview.winfo_y() < card.actions.winfo_y() < card.panel.winfo_y()

        # 6. Clicking the read icon marks as read; in view "Alle" it shows the green checkmark (READ_CHECK).
        window.archive.set("Alle")
        card.read_button.invoke()
        root.update()
        assert card.read_button.icon_kind == "READ_CHECK"
        assert card.read_button.tooltip.text == "Gelesen"
    finally:
        for timer in root.tk.call("after", "info"):
            root.after_cancel(timer)
        root.destroy()

