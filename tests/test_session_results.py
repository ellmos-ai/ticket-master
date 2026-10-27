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


def test_action_palette_contrast_and_requested_file_icons():
    from ticket_master_runtime.results.icons import PALETTES, contrast_ratio
    assert len({p["background"] for p in PALETTES.values()}) == 3
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
        window = ResultsWindow(root, path)
        root.title("TEST · Aktionsdesign")
        root.update()
        assert window.evidence()["viewable"]
        root.withdraw()
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
def test_native_ui_filters_details_and_refresh(tmp_path, session, native_tk_app):
    import tkinter as tk
    from ticket_master_runtime.results.gui import ResultsWindow
    path = tmp_path / "session.json"
    sha = write(path, session)
    root = tk.Toplevel(native_tk_app)
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
        # Preserve unknown/nontext clipboard formats: exercise the OS clipboard
        # only when its prior text can actually be restored.
        if previous_clipboard is not None:
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
        window = ResultsWindow(root, path, backend)
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
