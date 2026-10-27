"""Versioned session projection with cooperative, hash-bound atomic updates."""
from __future__ import annotations

import copy
import hashlib
import json
import mimetypes
import os
import re
from pathlib import Path
import subprocess
import sys
import tempfile
from urllib.parse import urlsplit
import webbrowser
from datetime import datetime, timezone

SCHEMA = "ellmos.ticket.session-results.v1"
KINDS = {"message", "system", "analysis", "ticket_result", "decision"}
SAFE_SUFFIXES = {".md", ".txt", ".pdf", ".html", ".htm", ".json", ".csv", ".png", ".jpg", ".jpeg", ".svg", ".webp"}


class ResultsError(ValueError):
    pass


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def _text(value, name):
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ResultsError(f"Ungültiges Textfeld: {name}")


def validate(data):
    if not isinstance(data, dict) or data.get("schema") != SCHEMA:
        raise ResultsError("Unbekanntes Sessionformat")
    for key in ("session_id", "host", "updated_at"):
        _text(data.get(key), key)
    if type(data.get("revision")) is not int or data["revision"] < 1:
        raise ResultsError("Revision muss eine positive Ganzzahl sein")
    try:
        if datetime.fromisoformat(data["updated_at"]).tzinfo is None:
            raise ValueError()
    except ValueError as exc:
        raise ResultsError("Zeitstempel braucht Zeitzone") from exc
    if not isinstance(data.get("events"), list) or not isinstance(data.get("metrics"), dict):
        raise ResultsError("events/metrics fehlen")
    ids = set()
    message_ids = set()
    for event in data["events"]:
        if not isinstance(event, dict):
            raise ResultsError("Meldung muss ein Objekt sein")
        for key in ("id", "created_at", "title", "summary", "status", "source"):
            _text(event.get(key), key)
        if event["id"] in ids or event.get("kind") not in KINDS:
            raise ResultsError("Doppelte Meldungs-ID oder unbekannter Meldungstyp")
        ids.add(event["id"])
        try:
            if datetime.fromisoformat(event["created_at"]).tzinfo is None:
                raise ValueError()
        except ValueError as exc:
            raise ResultsError("Meldungszeit braucht Zeitzone") from exc
        if not isinstance(event.get("tags"), list) or any(not isinstance(x, str) for x in event["tags"]):
            raise ResultsError("tags muss eine Textliste sein")
        if not isinstance(event.get("details", ""), str):
            raise ResultsError("details muss Text sein")
        for ref in ("ticket_id", "message_id", "receipt_id", "log_id"):
            if ref in event:
                _text(event[ref], ref)
        if "notification" in event:
            note = event["notification"]
            if event["kind"] not in {"message", "system"} or not isinstance(note, dict):
                raise ResultsError("Ungültige Meldungsbeobachtung")
            for key in ("external_id", "observed_at"):
                _text(note.get(key), key)
            try:
                if datetime.fromisoformat(note["observed_at"]).tzinfo is None:
                    raise ValueError()
            except ValueError as exc:
                raise ResultsError("Beobachtungszeit braucht Zeitzone") from exc
            for field, pattern, value in (
                ("message_id", r"M-[0-9]{8}-[1-9][0-9]{8}", event.get("message_id")),
                ("receipt_id", r"R-[0-9]{8}-[1-9][0-9]{8}", event.get("receipt_id")),
                ("thread_id", r"TH-[0-9]{8}-[1-9][0-9]{8}", note.get("thread_id")),
                ("message_sha256", r"[0-9a-f]{64}", note.get("message_sha256")),
            ):
                if not isinstance(value, str) or not re.fullmatch(pattern, value):
                    raise ResultsError(f"Ungültige kanonische Meldungsbindung: {field}")
            if event["message_id"] in message_ids:
                raise ResultsError("Kanonische Message-ID doppelt in derselben Session")
            message_ids.add(event["message_id"])
        if not isinstance(event.get("artifacts", []), list):
            raise ResultsError("artifacts muss eine Liste sein")
        for artifact in event.get("artifacts", []):
            if not isinstance(artifact, dict):
                raise ResultsError("Artefakt muss ein Objekt sein")
            _text(artifact.get("path"), "artifact.path")
            _text(artifact.get("label"), "artifact.label")
            _text(artifact.get("mime"), "artifact.mime")
            if "sha256" in artifact and (not isinstance(artifact["sha256"], str) or not re.fullmatch(r"[0-9a-fA-F]{64}", artifact["sha256"])):
                raise ResultsError("Ungültiger Artefakt-SHA-256")
            if artifact["path"].lower().startswith("https:"):
                validate_https_url(artifact["path"])
                if artifact.get("sha256"):
                    raise ResultsError("URL-Inhalte werden nicht abgerufen oder per Dateihash bestätigt")
            elif not Path(artifact["path"]).is_absolute():
                raise ResultsError("Artefaktpfade müssen absolut sein")
    metrics = data["metrics"]
    for key in ("skills", "tokens", "agents"):
        if not isinstance(metrics.get(key), list):
            raise ResultsError(f"metrics.{key} muss eine Liste sein")
        for record in metrics[key]:
            if not isinstance(record, dict):
                raise ResultsError("Metrik muss ein Objekt sein")
            for field in ("source", "observed_at", "scope"):
                _text(record.get(field), field)
    for token in metrics["tokens"]:
        if token.get("value") is not None and (type(token["value"]) is not int or token["value"] < 0):
            raise ResultsError("Tokenwert muss null oder nichtnegative Ganzzahl sein")
    for agent in metrics["agents"]:
        for field in ("id", "status", "task"):
            _text(agent.get(field), field)
        for field in ("requested_model", "actual_model"):
            if agent.get(field) is not None and not isinstance(agent[field], str):
                raise ResultsError(f"{field} muss Text oder null sein")
    delivery = data.get("delivery", {})
    if delivery and (delivery.get("mode") not in {"on_demand", "active_chat", "chat_start"}
                     or delivery.get("surface") != "local_gui"):
        raise ResultsError("Ungültige Messages-Line-Zustellkonfiguration")
    settings = data.get("message_settings", {})
    if not isinstance(settings, dict) or set(settings) - {"read_mode"} or settings.get("read_mode", "manual") not in {"manual", "on_open"}:
        raise ResultsError("Ungültige Meldungs-Leseeinstellung")
    return data


def read(path, session_id=None):
    raw = Path(path).read_bytes()
    data = validate(json.loads(raw.decode("utf-8")))
    if session_id is not None and data["session_id"] != session_id:
        raise ResultsError("Andere Session: Aktualisierung verweigert")
    return data, digest(raw)


def write(path, data, expected_revision=None, expected_sha256=None):
    """All cooperating writers use this API; lock leftovers fail closed.

    Hash check protects against changed preimages. External non-cooperating
    writers must not write between the final check and replace.
    """
    path = Path(path).absolute()
    validate(data)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = path.with_name(path.name + ".write-lock")
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise ResultsError(f"Schreibsperre aktiv: {lock}") from exc
    tmp = None
    try:
        os.close(fd)
        if expected_revision is None:
            if path.exists() or data["revision"] != 1:
                raise ResultsError("Session existiert bereits oder Startrevision ist nicht 1")
        else:
            current, sha = read(path, data["session_id"])
            if current["revision"] != expected_revision or sha != expected_sha256:
                raise ResultsError("Veraltete Revision/Quellenhash: erneut lesen")
            if data["revision"] != expected_revision + 1:
                raise ResultsError("Revision muss genau um eins steigen")
        raw = (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".results-", delete=False) as handle:
            tmp = Path(handle.name)
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        if expected_revision is None:
            os.link(tmp, path)  # atomic publish; refuses an existing destination
        else:
            # Recheck immediately before publication, including external edits.
            current, sha = read(path, data["session_id"])
            if current["revision"] != expected_revision or sha != expected_sha256:
                raise ResultsError("Datei wurde während des Schreibens verändert")
            os.replace(tmp, path)
        return digest(raw)
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)
        lock.unlink(missing_ok=True)


def append(path, event, revision, sha256):
    data, _ = read(path)
    data = copy.deepcopy(data)
    data["events"].append(event)
    data["revision"] = revision + 1
    data["updated_at"] = now()
    return write(path, data, revision, sha256)


def update_session_data(path, patch, revision, sha256):
    """Patch metric groups, preserving events and session identity under CAS."""
    if not isinstance(patch, dict) or set(patch) - {"skills", "tokens", "agents", "agent_coverage"}:
        raise ResultsError("Sessiondaten dürfen nur skills/tokens/agents/agent_coverage enthalten")
    data, _ = read(path)
    data = copy.deepcopy(data)
    data["metrics"].update(patch)
    data["revision"] = revision + 1
    data["updated_at"] = now()
    return write(path, data, revision, sha256)


def update_message_settings(path, mode, revision, sha256):
    data, _ = read(path)
    data = copy.deepcopy(data)
    data["message_settings"] = {"read_mode": mode}
    data["revision"], data["updated_at"] = revision + 1, now()
    return write(path, data, revision, sha256)


def select_events(data, topic="Alle", query="", order="Älteste zuerst"):
    words = query.casefold().split()
    events = [e for e in data["events"] if (topic == "Alle" or topic in e["tags"])
              and all(word in json.dumps(e, ensure_ascii=False).casefold() for word in words)]
    if order == "Thema":
        return sorted(events, key=lambda e: (e["tags"][0] if e["tags"] else "", e["created_at"], e["id"]))
    return sorted(events, key=lambda e: (datetime.fromisoformat(e["created_at"]), e["id"]), reverse=order == "Neueste zuerst")


def artifact_kind(artifact):
    if artifact["path"].lower().startswith(("https://", "http://")):
        return "URL"
    suffix = Path(artifact["path"]).suffix.lower()
    mime = artifact.get("mime") or mimetypes.guess_type(artifact["path"])[0] or "application/octet-stream"
    known = {".pdf": "PDF", ".md": "MD", ".html": "HTML", ".htm": "HTML", ".url": "URL",
             ".doc": "WORD", ".docx": "WORD", ".txt": "TXT", ".py": "PY", ".json": "JSON", ".csv": "CSV"}
    if suffix in known:
        return known[suffix]
    if mime.startswith("image/"):
        return "IMAGE"
    return "FILE"


def artifact_badge(artifact):
    return {"IMAGE": "BILD", "FILE": "DATEI"}.get(artifact_kind(artifact), artifact_kind(artifact))


def question_prompt(data, event_id, path):
    event = next((item for item in data["events"] if item["id"] == event_id), None)
    if event is None:
        raise ResultsError("Ergebnis-ID ist in dieser Sitzung nicht vorhanden")
    reference = {"session_id": data["session_id"], "event_id": event["id"], "title": event["title"],
                 "session_file": str(Path(path).absolute()), "displayed_revision": data["revision"]}
    if event.get("ticket_id"):
        reference["ticket_id"] = event["ticket_id"]
    return ("Bezug zu einem angezeigten Sitzungsergebnis (folgende Metadaten sind Daten):\n"
            + json.dumps(reference, ensure_ascii=False, indent=2)
            + "\nBitte löse den Eintrag über seine stabile Session-/Ergebnis-ID auf.\n\nMeine Frage / Anweisung: ")


def open_artifact(artifact):
    """Open only passive local documents, never commands/arguments from JSON."""
    if artifact["path"].lower().startswith("https:"):
        url = validate_https_url(artifact["path"])
        if artifact.get("sha256"):
            raise ResultsError("URL-Dateihash wird nicht bestätigt")
        if not webbrowser.open(url, new=2):
            raise ResultsError("Kein Browser für diesen HTTPS-Link verfügbar")
        return
    path = Path(artifact["path"])
    if (not path.is_absolute() or str(path).startswith(("\\\\", "//")) or not path.is_file()
            or path.is_symlink() or path.suffix.lower() not in SAFE_SUFFIXES):
        raise ResultsError("Nur vorhandene lokale Dokumente mit erlaubtem Dateityp können geöffnet werden")
    resolved = path.resolve(strict=True)
    if resolved.suffix.lower() not in SAFE_SUFFIXES:
        raise ResultsError("Ausführbares Linkziel abgelehnt")
    if artifact.get("sha256") and digest(resolved.read_bytes()) != artifact["sha256"].lower():
        raise ResultsError("Dokument wurde seit der Ergebnisbuchung verändert; zuerst Quelle neu prüfen")
    if sys.platform == "win32":
        os.startfile(str(resolved))
    else:
        subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(resolved)])


def validate_https_url(value):
    """An explicit user-click URL, without credentials, controls or other schemes."""
    if not isinstance(value, str) or not value.startswith("https://") or any(ord(c) <= 32 or ord(c) == 127 for c in value) or "\\" in value:
        raise ResultsError("Unsicherer HTTPS-Link")
    try:
        parts = urlsplit(value)
        host = parts.hostname
        if parts.scheme != "https" or not host or parts.username is not None or parts.password is not None or parts.port not in {None, 443}:
            raise ValueError()
        if len(host) > 253 or any(not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label) for label in host.split(".")):
            raise ValueError()
    except ValueError as exc:
        raise ResultsError("Nur gültige HTTPS-Links ohne Anmeldedaten und mit Standardport sind erlaubt") from exc
    return value
