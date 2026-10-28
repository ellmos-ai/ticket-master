"""Optional projection adapter. The existing Messages-Line remains the only owner."""
from __future__ import annotations

import copy
import importlib
import importlib.util
import json
import os
from pathlib import Path
import re
import sys
import threading
import uuid

from .model import ResultsError, digest, now, read, write

ID_RE = re.compile(r"^M-[0-9]{8}-[1-9][0-9]{8}$")
RECEIPT_RE = re.compile(r"^R-[0-9]{8}-[1-9][0-9]{8}$")
THREAD_RE = re.compile(r"^TH-[0-9]{8}-[1-9][0-9]{8}$")
ENVELOPE_SCHEMA = "ellmos.ticket.observed-notification.v1"
_OWNER_IMPORT_MUTEX = threading.RLock()


def message_digest(message):
    return digest(json.dumps(message, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def projection_digest(event):
    # This is compared with a NEW projection from line.get(), never with a
    # second session-derived checksum. Include every canonical display mapping,
    # including time, external identity and thread. Event-ID, status, artifacts
    # and ticket annotations remain session-owned (legacy adoption preserves them).
    fields = ("kind", "created_at", "title", "summary", "details", "source", "tags",
              "message_id", "receipt_id", "notification")
    return digest(json.dumps({key: event.get(key, "") for key in fields},
                             sort_keys=True, ensure_ascii=False).encode("utf-8"))


def validate_notice(notice):
    if not isinstance(notice, dict) or set(notice) - {"source", "external_id", "title", "text", "summary", "observed_at", "kind", "tags", "thread_id"}:
        raise ResultsError("Unbekannte Felder in der externen Meldung")
    for field in ("source", "external_id", "title", "text", "observed_at"):
        value = notice.get(field)
        if not isinstance(value, str) or not value.strip() or "\x00" in value:
            raise ResultsError(f"Externe Meldung: {field} fehlt oder ist ungültig")
    if notice.get("kind", "message") not in {"message", "system"}:
        raise ResultsError("Externe Meldung muss message oder system sein")
    if "summary" in notice and (not isinstance(notice["summary"], str) or not notice["summary"].strip() or "\x00" in notice["summary"]):
        raise ResultsError("Ungültige Meldungszusammenfassung")
    if not isinstance(notice.get("tags", []), list) or any(not isinstance(x, str) for x in notice.get("tags", [])):
        raise ResultsError("Meldungstags müssen eine Textliste sein")
    if "thread_id" in notice and not THREAD_RE.fullmatch(str(notice["thread_id"])):
        raise ResultsError("Ungültige kanonische Thread-ID")
    from datetime import datetime
    try:
        if datetime.fromisoformat(notice["observed_at"]).tzinfo is None:
            raise ValueError()
    except ValueError as exc:
        raise ResultsError("Beobachtungszeit braucht eine Zeitzone") from exc
    return notice


class ApprovedMessages:
    """Operator-chosen, hash-pinned local owner; never selected by feed contents.

    All imported package files are verified against the pinned manifest. No
    remote transport, services, source copying or provider calls happen here.
    """
    def __init__(self, core_dir, manifest_sha256, messages_dir, config, target, recipient, actor):
        self.core_dir, self.messages_dir, self.config_path = Path(core_dir).absolute(), Path(messages_dir).absolute(), Path(config).absolute()
        self.manifest_sha256 = manifest_sha256
        self.target, self.recipient, self.actor = target, recipient, actor
        self._mutex = threading.Lock()
        self._loaded_owner = None

    def _import_owner(self, files):
        """Only reuse objects loaded by THIS adapter, never a hash-name cache.

        A fresh private namespace also separates approved copies at different
        paths. All its imported modules must originate in manifest-listed files;
        replacement of a previously loaded module fails closed. Configuration
        is deliberately read outside this cache on every owner operation.
        """
        identity = (self.core_dir.resolve(), self.manifest_sha256)
        with _OWNER_IMPORT_MUTEX:
            if self._loaded_owner is not None and self._loaded_owner[0] == identity:
                _, name, modules, owner = self._loaded_owner
                current = {key: value for key, value in list(sys.modules.items()) if key == name or key.startswith(name + ".")}
                if current.keys() != modules.keys() or any(current[key] is not value for key, value in modules.items()):
                    raise ResultsError("Privater Messages-Core-Namensraum wurde ersetzt")
                self._check_import_origins(name, modules, files)
                return owner
            # Never replace or remove a foreign preloaded module, even if it
            # uses the old public hash-qualified name.
            name = "_approved_message_owner_" + self.manifest_sha256 + "_" + uuid.uuid4().hex
            if any(key == name or key.startswith(name + ".") for key in list(sys.modules)):
                raise ResultsError("Privater Messages-Core-Namensraum ist bereits belegt")
            previous_bytecode = sys.dont_write_bytecode
            loaded = {}
            module = None
            try:
                sys.dont_write_bytecode = True
                spec = importlib.util.spec_from_file_location(name, self.core_dir / "lib/__init__.py", submodule_search_locations=[str(self.core_dir / "lib")])
                if spec is None or spec.loader is None:
                    raise ResultsError("Messages-Core ist nicht importierbar")
                module = importlib.util.module_from_spec(spec)
                sys.modules[name] = module
                spec.loader.exec_module(module)
                owner = importlib.import_module(name + ".messages_line")
                loaded = {key: value for key, value in list(sys.modules.items()) if key == name or key.startswith(name + ".")}
                self._check_import_origins(name, loaded, files)
            except BaseException:
                # Remove only the exact new objects from this failed import.
                for key, value in loaded.items():
                    if sys.modules.get(key) is value:
                        del sys.modules[key]
                if module is not None and sys.modules.get(name) is module:
                    del sys.modules[name]
                raise
            finally:
                sys.dont_write_bytecode = previous_bytecode
            self._loaded_owner = identity, name, loaded, owner
            return owner

    def _check_import_origins(self, name, modules, files):
        for key, module in modules.items():
            source = "lib/__init__.py" if key == name else "lib/" + key[len(name) + 1:].replace(".", "/") + ".py"
            expected = (self.core_dir / source).resolve()
            spec = getattr(module, "__spec__", None)
            if (source not in files or spec is None or spec.name != key
                    or not isinstance(spec.loader, importlib.machinery.SourceFileLoader)
                    or Path(spec.origin or "").resolve() != expected
                    or Path(getattr(module, "__file__", "")).resolve() != expected):
                raise ResultsError("Geladener Messages-Core stammt nicht aus der geprüften Quelle")
            if key == name and list(getattr(module, "__path__", [])) != [str(self.core_dir / "lib")]:
                raise ResultsError("Messages-Core-Paketpfad wurde verändert")

    def _owner(self):
        manifest_path = self.core_dir / "MANIFEST.json"
        raw = manifest_path.read_bytes()
        if digest(raw) != self.manifest_sha256:
            raise ResultsError("Messages-Core-Manifest weicht vom freigegebenen Hash ab")
        manifest = json.loads(raw)
        files = manifest.get("files")
        if not isinstance(files, dict) or "lib/messages_line.py" not in files or "lib/__init__.py" not in files:
            raise ResultsError("Unvollständiges Messages-Core-Manifest")
        for relative, expected in files.items():
            part = Path(relative)
            path = self.core_dir / part
            if part.is_absolute() or ".." in part.parts or path.is_symlink() or not path.resolve().is_relative_to(self.core_dir.resolve()):
                raise ResultsError("Unsicherer Messages-Core-Pfad")
            if digest(path.read_bytes()) != expected:
                raise ResultsError("Messages-Core-Datei weicht vom freigegebenen Stand ab")
        # Reject unmanifested Python dependencies too, including injected packages.
        actual = {p.relative_to(self.core_dir).as_posix() for p in (self.core_dir / "lib").rglob("*") if p.is_file()}
        if actual != set(files):
            raise ResultsError("Unmanifestierte Messages-Core-Datei (auch Bytecode wird abgelehnt)")
        owner = self._import_owner(files)
        config = owner.validate_messages_config(json.loads(self.config_path.read_text(encoding="utf-8")))
        return owner, owner.MessagesLine(self.messages_dir), config

    def _bound(self, line, binding):
        mid = binding["message_id"]
        if not ID_RE.fullmatch(mid):
            raise ResultsError("Kein kanonischer Message-Identifier")
        message = line.get(mid)
        if message["receipt_id"] != binding["receipt_id"] or message_digest(message) != binding["message_sha256"]:
            raise ResultsError("Message-ID, Receipt-ID oder Inhalt hat sich verändert")
        if message["recipient_ref"] != self.recipient:
            raise ResultsError("Nachricht gehört zu einem anderen Empfänger")
        if projection_digest(event_from_message(message)) != binding["projection_sha256"]:
            raise ResultsError("Angezeigte Meldung oder Zuordnung stimmt nicht mit dem kanonischen Ownerumschlag überein")
        return message

    def resolve(self, message_id):
        with self._mutex:
            _, line, _ = self._owner()
            message = line.get(message_id)
            if message["recipient_ref"] != self.recipient:
                raise ResultsError("Nachricht gehört zu einem anderen Empfänger")
            return message

    def observe(self, notice):
        """Idempotent canonical creation, using the owner's archive, not a registry.

        A crash after owner.create and before session CAS is recoverable by replay.
        Cooperating ingestion adapters serialize this lookup/create critical section.
        """
        validate_notice(notice)
        identity = digest((notice["source"] + "\0" + notice["external_id"]).encode("utf-8"))
        origin = "observed:" + identity
        content = {"schema": ENVELOPE_SCHEMA, **notice}
        with self._mutex:
            owner, line, config = self._owner()
            targets = owner.selected_targets(config, "on_demand")
            if {"target_ref": self.target, "surface": "local_gui"} not in targets:
                raise ResultsError("Owner-Config erlaubt das lokale Meldungsziel nicht")
            self.messages_dir.mkdir(parents=True, exist_ok=True)
            lock = self.messages_dir / ".observed-notice-write-lock"
            try:
                fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError as exc:
                raise ResultsError("Meldungserfassung ist gesperrt; Schreibprozess zuerst prüfen") from exc
            try:
                os.close(fd)
                existing = [m for m in line.list_messages() if m["origin_ref"] == origin and m["recipient_ref"] == self.recipient]
                if len(existing) > 1:
                    raise ResultsError("Mehrdeutige kanonische Meldungszuordnung")
                if existing:
                    previous = json.loads(existing[0]["content_text"])
                    # The first observation time is retained across sessions/replays.
                    def comparable(value):
                        return {k: v for k, v in value.items() if k != "observed_at"}
                    if comparable(previous) != comparable(content):
                        raise ResultsError("Externe ID wurde für einen anderen Meldungsinhalt wiederverwendet")
                    return existing[0]
                return line.create(config=config, origin_ref=origin, recipient_ref=self.recipient,
                                   content_text=json.dumps(content, ensure_ascii=False, sort_keys=True),
                                   delivery_mode="on_demand", thread_id=notice.get("thread_id"))
            finally:
                lock.unlink(missing_ok=True)

    def status(self, binding):
        with self._mutex:
            _, line, _ = self._owner()
            message = self._bound(line, binding)
            receipts = line.receipts(message["message_id"])
            state = line.delivery_state(message["message_id"])
            return {"state": state, "observed_at": now(), "receipt_id": message["receipt_id"],
                    "observations": [r["observation_id"] for r in receipts],
                    "can_ack": {"target_ref": self.target, "surface": "local_gui"} in message["target_snapshot"]}

    def acknowledge(self, binding, event="explicit_ack"):
        if event not in {"explicit_ack", "gui_opened"}:
            raise ResultsError("Nicht unterstützte Leseaktion")
        with self._mutex:
            _, line, config = self._owner()  # fresh config and message before every write
            message = self._bound(line, binding)
            if line.delivery_state(message["message_id"]) == "delivered":
                return {"state": "delivered", "already_acknowledged": True, "observed_at": now(), "receipt_id": message["receipt_id"]}
            kwargs = dict(target_ref=self.target, surface="local_gui", channel_ref="session-results",
                          actor_ref=self.actor, evidence_ref="session-results:" + binding["message_sha256"], current_config=config)
            if event == "explicit_ack":
                receipt = line.acknowledge(message["message_id"], **kwargs)
            else:
                receipt = line.record_observation(message["message_id"], event="gui_opened", **kwargs)
            # Do not report a successful read merely because a write was attempted.
            stored = line.receipts(message["message_id"])
            if receipt not in stored or line.delivery_state(message["message_id"]) != "delivered":
                raise ResultsError("Kanonischer Quittungs-Readback fehlgeschlagen")
            return {"state": "delivered", "receipt_id": message["receipt_id"], "observation_id": receipt["observation_id"], "event": receipt["event"], "observed_at": now()}


def event_from_message(message):
    try:
        envelope = json.loads(message["content_text"])
    except json.JSONDecodeError:
        envelope = None
    if isinstance(envelope, dict) and envelope.get("schema") == ENVELOPE_SCHEMA:
        notice = {k: v for k, v in envelope.items() if k != "schema"}
        validate_notice(notice)
    else:
        notice = {"source": message["origin_ref"], "external_id": message["message_id"],
                  "title": "Externe Nachricht · " + message["message_id"], "text": message["content_text"],
                  "observed_at": message["created_at"], "kind": "message", "tags": ["Meldungen"]}
    return {"id": "message:" + message["message_id"], "kind": notice.get("kind", "message"),
            "created_at": notice["observed_at"], "title": notice["title"], "summary": notice.get("summary", notice["text"][:240]),
            "details": notice["text"], "source": notice["source"], "status": "Wahrgenommen · Lesen ist keine Freigabe",
            "tags": list(dict.fromkeys(["Meldungen", *notice.get("tags", [])])),
            "message_id": message["message_id"], "receipt_id": message["receipt_id"],
            "notification": {"external_id": notice["external_id"], "observed_at": notice["observed_at"],
                             "thread_id": message["thread_id"], "message_sha256": message_digest(message)}}


def ingest(path, backend, revision, sha256, *, notice=None, message_id=None, event_id=None):
    data, current_sha = read(path)
    if data["revision"] != revision or current_sha != sha256:
        raise ResultsError("Veraltete Sessionrevision; keine Nachricht angelegt")
    legacy = None
    if event_id is not None:
        legacy = next((e for e in data["events"] if e["id"] == event_id), None)
        if legacy is None or legacy["kind"] not in {"message", "system"}:
            raise ResultsError("Nur vorhandene Nachrichten/Systemmeldungen können zugeordnet werden")
        if legacy.get("notification"):
            message_id = legacy["message_id"]
        else:
            notice = {"source": legacy["source"], "external_id": legacy.get("log_id") or legacy["id"],
                      "title": legacy["title"], "summary": legacy["summary"], "text": legacy.get("details") or legacy["summary"],
                      "observed_at": legacy["created_at"], "kind": legacy["kind"], "tags": legacy["tags"]}
    message = backend.observe(notice) if notice is not None else backend.resolve(message_id)
    event = event_from_message(message)
    previous = next((e for e in data["events"] if e.get("notification") and e.get("message_id") == message["message_id"]), None)
    if previous:
        if legacy is not None and previous["id"] != legacy["id"]:
            raise ResultsError("Dieselbe kanonische Meldung ist bereits unter einer anderen Event-ID zugeordnet")
        if previous["notification"]["message_sha256"] != event["notification"]["message_sha256"]:
            raise ResultsError("Meldungszuordnung widerspricht dem vorhandenen Eintrag")
        return {"revision": revision, "sha256": current_sha, "event_id": previous["id"], "message_id": message["message_id"], "deduplicated": True}
    data = copy.deepcopy(data)
    if legacy is not None:
        event = {**legacy, **event, "id": legacy["id"], "status": legacy["status"]}
        data["events"] = [event if e["id"] == legacy["id"] else e for e in data["events"]]
    else:
        data["events"].append(event)
    data["revision"], data["updated_at"] = revision + 1, now()
    sha = write(path, data, revision, sha256)
    return {"revision": revision + 1, "sha256": sha, "event_id": event["id"], "message_id": message["message_id"], "deduplicated": False}


def binding_for(event):
    if not event.get("notification") or not event.get("message_id"):
        return None
    return {"message_id": event["message_id"], "receipt_id": event["receipt_id"], "message_sha256": event["notification"]["message_sha256"],
            "projection_sha256": projection_digest(event)}


def message_counts(events, states):
    notices = [e for e in events if e["kind"] in {"message", "system"}]
    confirmed = sum(states.get(e["id"], {}).get("state") == "delivered" for e in notices)
    unknown = sum(states.get(e["id"], {}).get("state", "unknown") == "unknown" for e in notices)
    return len(notices) - confirmed - unknown, unknown
