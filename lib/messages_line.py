"""Local message-ticket contracts for the ticket-master Messages-Line.

This module stores message text, thread metadata, and one canonical ReceiptID
per message. It does not send messages, fetch logs, or run retries. Channel
observations bind to that ReceiptID and retain upstream evidence references;
the existing cross-system transport and reconciliation remain external.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

try:  # package import
    from .ticket_writer import ID_DIGITS, draw_id
    from .unicorn_intake import ROUTE_INTENT_SCHEMA
except ImportError:  # direct import from a checkout's lib directory
    from ticket_writer import ID_DIGITS, draw_id
    from unicorn_intake import ROUTE_INTENT_SCHEMA


MESSAGE_TICKET_SCHEMA = "ellmos.ticket.message-ticket.v1"
MESSAGE_RECEIPT_SCHEMA = "ellmos.ticket.message-receipt.v1"
MESSAGE_RECEIPT_LEDGER_SCHEMA = "ellmos.ticket.message-receipt-ledger.v1"
MESSAGE_THREAD_LEDGER_SCHEMA = "ellmos.ticket.message-thread-ledger.v1"
MESSAGES_CONFIG_SCHEMA = "ellmos.ticket.messages-line-config.v1"
LOG_REGISTRY_SCHEMA = "ellmos.ticket.log-registry.v1"
MESSAGE_ROUTE_SCHEMA = "ellmos.ticket.message-route-intent.v1"

DELIVERY_MODES = frozenset({"chat_start", "on_demand", "active_chat"})
DELIVERY_EVENTS = frozenset(
    {
        "prepared",
        "offered",
        "transported",
        "fetched",
        "listed",
        "previewed",
        "gui_opened",
        "explicit_ack",
        "on_demand_ack",
        "active_chat_ack",
    }
)
ACK_EVENTS = frozenset({"explicit_ack", "on_demand_ack", "active_chat_ack"})
SURFACES = frozenset({"ticket_master", "local_gui", "server_gui", "registered_server"})
LOG_WINDOWS = frozenset({"24h", "monthly", "other"})
MAX_MESSAGE_TEXT_CHARS = 1_048_576

_MESSAGE_ID_RE = re.compile(rf"^M-[0-9]{{8}}-[1-9][0-9]{{{ID_DIGITS - 1}}}$")
_THREAD_ID_RE = re.compile(rf"^TH-[0-9]{{8}}-[1-9][0-9]{{{ID_DIGITS - 1}}}$")
_RECEIPT_ID_RE = re.compile(rf"^R-[0-9]{{8}}-[1-9][0-9]{{{ID_DIGITS - 1}}}$")
_OBSERVATION_ID_RE = re.compile(r"^O-[0-9a-f]{64}$")
_SAFE_REF_RE = re.compile(r"^[^\x00-\x20\x7f]{1,256}$")
_MESSAGE_FIELDS = frozenset(
    {
        "schema",
        "message_id",
        "receipt_id",
        "thread_id",
        "created_at",
        "origin_ref",
        "recipient_ref",
        "content_text",
        "delivery_mode",
        "target_snapshot",
        "delivery_policy_snapshot",
    }
)
_RECEIPT_LEDGER_FIELDS = frozenset({"schema", "receipt_id", "message_id"})
_THREAD_LEDGER_FIELDS = frozenset({"schema", "thread_id", "created_at"})
_RECEIPT_FIELDS = frozenset(
    {
        "schema",
        "receipt_id",
        "observation_id",
        "message_id",
        "recipient_ref",
        "target_ref",
        "surface",
        "channel_ref",
        "event",
        "observed_at",
        "gui_open_counts_as_delivery",
        "actor_ref",
        "evidence_ref",
    }
)
_CONFIG_FIELDS = frozenset(
    {
        "schema",
        "enabled",
        "delivery_modes",
        "targets",
        "gui_open_counts_as_delivery",
        "updated_at",
        "message_content_logging",
        "audio_logging",
        "transport_contract_ref",
    }
)
_MODE_FIELDS = frozenset(DELIVERY_MODES)
_TARGET_FIELDS = frozenset({"target_ref", "surface"})
_POLICY_FIELDS = frozenset({"gui_open_counts_as_delivery", "config_updated_at"})
_LOG_REGISTRY_FIELDS = frozenset({"schema", "sources"})
_LOG_SOURCE_FIELDS = frozenset(
    {
        "source_ref",
        "system_ref",
        "owner_ref",
        "window",
        "observed_at",
        "access_policy_ref",
        "redaction_policy_ref",
        "provenance_ref",
        "retention_ref",
        "materialization",
    }
)


class MessageLineError(ValueError):
    """Raised when a message-line object or store is not contract-valid."""


class ReceiptConflictError(MessageLineError):
    """Raised when one stable receipt ID is reused for different evidence."""


def _require_exact_fields(
    value: Mapping[str, Any], expected: frozenset[str], label: str
) -> None:
    actual = frozenset(value)
    missing = expected - actual
    extra = actual - expected
    if missing or extra:
        details: list[str] = []
        if missing:
            details.append("missing=" + ",".join(sorted(missing)))
        if extra:
            details.append("extra=" + ",".join(sorted(extra)))
        raise MessageLineError(f"{label} has invalid fields ({'; '.join(details)})")


def _require_ref(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _SAFE_REF_RE.fullmatch(value):
        raise MessageLineError(f"{field} must be a non-empty opaque reference")
    # References may be registry IDs or URIs. Local paths are not a valid
    # cross-system reference because they leak host-specific filesystem state.
    if value.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:[\\/]", value):
        raise MessageLineError(f"{field} must not be an absolute local path")
    if "\\" in value:
        raise MessageLineError(f"{field} must not contain a Windows path")
    return value


def _require_timestamp(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise MessageLineError(f"{field} must be an ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise MessageLineError(f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise MessageLineError(f"{field} must include a timezone")
    return value


def _require_bool(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise MessageLineError(f"{field} must be a boolean")
    return value


def validate_messages_config(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the local Messages-Line routing and acknowledgement policy."""
    if not isinstance(value, Mapping):
        raise MessageLineError("Messages-Line config must be an object")
    _require_exact_fields(value, _CONFIG_FIELDS, "Messages-Line config")
    if value["schema"] != MESSAGES_CONFIG_SCHEMA:
        raise MessageLineError("unsupported Messages-Line config schema")
    _require_timestamp(value["updated_at"], "updated_at")
    _require_bool(value["enabled"], "enabled")
    _require_bool(value["gui_open_counts_as_delivery"], "gui_open_counts_as_delivery")
    if value["message_content_logging"] is not False:
        raise MessageLineError("message content logging must remain disabled")
    if value["audio_logging"] is not False:
        raise MessageLineError("audio logging must remain disabled")
    contract_ref = value["transport_contract_ref"]
    if not isinstance(contract_ref, str) or contract_ref not in {ROUTE_INTENT_SCHEMA, MESSAGE_ROUTE_SCHEMA}:
        raise MessageLineError("Messages-Line transport contract is unsupported")

    modes = value["delivery_modes"]
    if not isinstance(modes, Mapping):
        raise MessageLineError("delivery_modes must be an object")
    _require_exact_fields(modes, _MODE_FIELDS, "delivery_modes")
    for mode, enabled in modes.items():
        _require_bool(enabled, f"delivery_modes.{mode}")

    targets = value["targets"]
    if not isinstance(targets, list):
        raise MessageLineError("targets must be an array")
    seen: set[tuple[str, str]] = set()
    canonical_targets: list[dict[str, str]] = []
    for index, target in enumerate(targets):
        if not isinstance(target, Mapping):
            raise MessageLineError(f"targets[{index}] must be an object")
        _require_exact_fields(target, _TARGET_FIELDS, f"targets[{index}]")
        target_ref = _require_ref(target["target_ref"], f"targets[{index}].target_ref")
        surface = target["surface"]
        if not isinstance(surface, str) or surface not in SURFACES:
            raise MessageLineError(f"targets[{index}].surface is unsupported")
        key = (target_ref, surface)
        if key in seen:
            raise MessageLineError("target snapshot contains a duplicate target/surface")
        seen.add(key)
        canonical_targets.append({"target_ref": target_ref, "surface": surface})

    return {
        "schema": MESSAGES_CONFIG_SCHEMA,
        "enabled": value["enabled"],
        "delivery_modes": dict(modes),
        "targets": canonical_targets,
        "gui_open_counts_as_delivery": value["gui_open_counts_as_delivery"],
        "updated_at": value["updated_at"],
        "message_content_logging": False,
        "audio_logging": False,
        "transport_contract_ref": value["transport_contract_ref"],
    }


def selected_targets(config: Mapping[str, Any], mode: str) -> list[dict[str, str]]:
    """Return the configured targets for one enabled mode; never sends them."""
    checked = validate_messages_config(config)
    if not isinstance(mode, str) or mode not in DELIVERY_MODES:
        raise MessageLineError(f"unsupported delivery mode: {mode}")
    if not checked["enabled"] or not checked["delivery_modes"][mode]:
        return []
    return [dict(target) for target in checked["targets"]]


def effective_targets(
    message: Mapping[str, Any], current_config: Mapping[str, Any]
) -> list[dict[str, str]]:
    """Apply current global switches to a message's immutable target snapshot.

    A saved snapshot preserves what was authorized when the message was made;
    it never overrides a later global disable, mode disable, or target removal.
    Newly configured targets cannot be added to an already-created message.
    """
    checked_message = validate_message_ticket(message)
    active_targets = {
        (target["target_ref"], target["surface"])
        for target in selected_targets(current_config, checked_message["delivery_mode"])
    }
    return [
        dict(target)
        for target in checked_message["target_snapshot"]
        if (target["target_ref"], target["surface"]) in active_targets
    ]


def validate_message_ticket(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the pointer-only, immutable Message-Ticket record."""
    if not isinstance(value, Mapping):
        raise MessageLineError("message ticket must be an object")
    _require_exact_fields(value, _MESSAGE_FIELDS, "message ticket")
    if value["schema"] != MESSAGE_TICKET_SCHEMA:
        raise MessageLineError("unsupported message-ticket schema")
    if not isinstance(value["message_id"], str) or not _MESSAGE_ID_RE.fullmatch(value["message_id"]):
        raise MessageLineError("message_id must use M-YYYYMMDD-#########")
    if not isinstance(value["receipt_id"], str) or not _RECEIPT_ID_RE.fullmatch(value["receipt_id"]):
        raise MessageLineError("receipt_id must use R-YYYYMMDD-#########")
    if not isinstance(value["thread_id"], str) or not _THREAD_ID_RE.fullmatch(value["thread_id"]):
        raise MessageLineError("thread_id must use TH-YYYYMMDD-#########")
    _require_identifier_date(value["message_id"], "M-", "message_id")
    _require_identifier_date(value["receipt_id"], "R-", "receipt_id")
    _require_identifier_date(value["thread_id"], "TH-", "thread_id")
    _require_timestamp(value["created_at"], "created_at")
    for field in ("origin_ref", "recipient_ref"):
        _require_ref(value[field], field)
    content_text = value["content_text"]
    if not isinstance(content_text, str) or not content_text.strip():
        raise MessageLineError("content_text must contain message text")
    if len(content_text) > MAX_MESSAGE_TEXT_CHARS:
        raise MessageLineError("content_text exceeds the local message size limit")
    if "\x00" in content_text:
        raise MessageLineError("content_text must not contain NUL bytes")
    if not isinstance(value["delivery_mode"], str) or value["delivery_mode"] not in DELIVERY_MODES:
        raise MessageLineError("delivery_mode is unsupported")

    targets = value["target_snapshot"]
    if not isinstance(targets, list):
        raise MessageLineError("target_snapshot must be an array")
    seen: set[tuple[str, str]] = set()
    canonical_targets: list[dict[str, str]] = []
    for index, target in enumerate(targets):
        if not isinstance(target, Mapping):
            raise MessageLineError(f"target_snapshot[{index}] must be an object")
        _require_exact_fields(target, _TARGET_FIELDS, f"target_snapshot[{index}]")
        target_ref = _require_ref(target["target_ref"], f"target_snapshot[{index}].target_ref")
        surface = target["surface"]
        if not isinstance(surface, str) or surface not in SURFACES:
            raise MessageLineError(f"target_snapshot[{index}].surface is unsupported")
        key = (target_ref, surface)
        if key in seen:
            raise MessageLineError("target_snapshot contains a duplicate target/surface")
        seen.add(key)
        canonical_targets.append({"target_ref": target_ref, "surface": surface})

    policy = value["delivery_policy_snapshot"]
    if not isinstance(policy, Mapping):
        raise MessageLineError("delivery_policy_snapshot must be an object")
    _require_exact_fields(policy, _POLICY_FIELDS, "delivery_policy_snapshot")
    gui_open_rule = _require_bool(
        policy["gui_open_counts_as_delivery"],
        "delivery_policy_snapshot.gui_open_counts_as_delivery",
    )
    config_updated_at = _require_timestamp(
        policy["config_updated_at"], "delivery_policy_snapshot.config_updated_at"
    )
    return {
        "schema": MESSAGE_TICKET_SCHEMA,
        "message_id": value["message_id"],
        "receipt_id": value["receipt_id"],
        "thread_id": value["thread_id"],
        "created_at": value["created_at"],
        "origin_ref": value["origin_ref"],
        "recipient_ref": value["recipient_ref"],
        "content_text": content_text,
        "delivery_mode": value["delivery_mode"],
        "target_snapshot": canonical_targets,
        "delivery_policy_snapshot": {
            "gui_open_counts_as_delivery": gui_open_rule,
            "config_updated_at": config_updated_at,
        },
    }


def _valid_calendar_date(day: str) -> str:
    if not isinstance(day, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
        raise MessageLineError("date must use YYYY-MM-DD")
    try:
        date.fromisoformat(day)
    except ValueError as exc:
        raise MessageLineError("date must be a real calendar date") from exc
    return day.replace("-", "")


def _require_identifier_date(value: str, prefix: str, field: str) -> None:
    compact = value[len(prefix) : len(prefix) + 8]
    try:
        date.fromisoformat(f"{compact[:4]}-{compact[4:6]}-{compact[6:8]}")
    except ValueError as exc:
        raise MessageLineError(f"{field} must contain a real calendar date") from exc


class MessagesLine:
    """Append-only local message archive and canonical message receipt ledger.

    Pass an explicit storage directory. Message text lives only in its message
    ticket; receipt observations contain metadata and evidence references only.
    This class does not transport messages or run retries.
    """

    def __init__(self, messages_dir: Path | str):
        self.messages_dir = Path(messages_dir)

    @property
    def _receipts_root(self) -> Path:
        return self.messages_dir / "_receipts"

    @property
    def _threads_root(self) -> Path:
        return self.messages_dir / "_threads"

    def _receipt_dir(self, receipt_id: str) -> Path:
        if not isinstance(receipt_id, str) or not _RECEIPT_ID_RE.fullmatch(receipt_id):
            raise MessageLineError("receipt_id must use R-YYYYMMDD-#########")
        _require_identifier_date(receipt_id, "R-", "receipt_id")
        return self._receipts_root / receipt_id

    def _thread_path(self, thread_id: str) -> Path:
        if not isinstance(thread_id, str) or not _THREAD_ID_RE.fullmatch(thread_id):
            raise MessageLineError("thread_id must use TH-YYYYMMDD-#########")
        _require_identifier_date(thread_id, "TH-", "thread_id")
        return self._threads_root / f"{thread_id}.json"

    def _read_thread_ledger(self, thread_id: str) -> dict[str, Any]:
        path = self._thread_path(thread_id)
        try:
            ledger = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise MessageLineError("canonical thread ledger is missing or unreadable") from exc
        if not isinstance(ledger, Mapping):
            raise MessageLineError("canonical thread ledger must be an object")
        _require_exact_fields(ledger, _THREAD_LEDGER_FIELDS, "thread ledger")
        if ledger["schema"] != MESSAGE_THREAD_LEDGER_SCHEMA or ledger["thread_id"] != thread_id:
            raise MessageLineError("canonical thread ledger does not match its message ticket")
        _require_timestamp(ledger["created_at"], "thread ledger created_at")
        return dict(ledger)

    def _reserve_thread_id(
        self,
        thread_id: str | None,
        date_compact: str,
        created_at: str,
        rng: Any,
    ) -> str:
        if thread_id is None:
            self._threads_root.mkdir(parents=True, exist_ok=True)
            used: set[int] = set()
            for entry in self._threads_root.iterdir():
                if (
                    not entry.is_file()
                    or not entry.name.endswith(".json")
                    or not _THREAD_ID_RE.fullmatch(entry.stem)
                ):
                    raise MessageLineError(f"unexpected entry in thread ledger: {entry.name}")
                _require_identifier_date(entry.stem, "TH-", "thread_id")
                if entry.stem.split("-")[1] == date_compact:
                    used.add(int(entry.stem.rsplit("-", 1)[1]))
            for _ in range(100):
                candidate = draw_id("TH", date_compact, used, rng)
                ledger = {
                    "schema": MESSAGE_THREAD_LEDGER_SCHEMA,
                    "thread_id": candidate,
                    "created_at": created_at,
                }
                if self._write_exclusive(self._thread_path(candidate), ledger):
                    return candidate
                used.add(int(candidate.rsplit("-", 1)[1]))
            raise MessageLineError("no free ThreadID after 100 random draws")

        path = self._thread_path(thread_id)
        if not path.is_file():
            raise MessageLineError("thread_id must reference an existing canonical thread ledger")
        self._read_thread_ledger(thread_id)
        return thread_id

    @staticmethod
    def _write_exclusive(path: Path, value: Mapping[str, Any]) -> bool:
        try:
            with path.open("x", encoding="utf-8", newline="\n") as handle:
                json.dump(value, handle, ensure_ascii=False, sort_keys=True, indent=2)
                handle.write("\n")
        except FileExistsError:
            return False
        except OSError:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
            raise
        return True

    def _read_receipt_ledger(self, message: Mapping[str, Any]) -> dict[str, Any]:
        receipt_dir = self._receipt_dir(message["receipt_id"])
        ledger_path = receipt_dir / "ledger.json"
        try:
            ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise MessageLineError("canonical receipt ledger is missing or unreadable") from exc
        if not isinstance(ledger, Mapping):
            raise MessageLineError("canonical receipt ledger must be an object")
        _require_exact_fields(ledger, _RECEIPT_LEDGER_FIELDS, "receipt ledger")
        if (
            ledger["schema"] != MESSAGE_RECEIPT_LEDGER_SCHEMA
            or ledger["receipt_id"] != message["receipt_id"]
            or ledger["message_id"] != message["message_id"]
        ):
            raise MessageLineError("canonical receipt ledger does not match its message ticket")
        return dict(ledger)

    def _existing_receipt_numbers(self, date_compact: str) -> set[int]:
        if not self._receipts_root.exists():
            return set()
        if not self._receipts_root.is_dir():
            raise MessageLineError("receipt ledger root is not a directory")
        used: set[int] = set()
        for entry in self._receipts_root.iterdir():
            if not entry.is_dir() or not _RECEIPT_ID_RE.fullmatch(entry.name):
                raise MessageLineError(f"unexpected entry in receipt ledger: {entry.name}")
            _require_identifier_date(entry.name, "R-", "receipt_id")
            if entry.name.split("-")[1] == date_compact:
                used.add(int(entry.name.rsplit("-", 1)[1]))
        return used

    def _read_all(self) -> list[dict[str, Any]]:
        if not self.messages_dir.exists():
            return []
        if not self.messages_dir.is_dir():
            raise MessageLineError("Messages-Line path is not a directory")
        records: list[dict[str, Any]] = []
        for path in sorted(self.messages_dir.glob("M-*.json")):
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise MessageLineError(f"unreadable message ticket: {path.name}") from exc
            record = validate_message_ticket(raw)
            if path.name != f"{record['message_id']}.json":
                raise MessageLineError(f"message filename/id mismatch: {path.name}")
            self._read_receipt_ledger(record)
            self._read_thread_ledger(record["thread_id"])
            records.append(record)
        return records

    def create(
        self,
        *,
        config: Mapping[str, Any],
        origin_ref: str,
        recipient_ref: str,
        content_text: str,
        delivery_mode: str,
        thread_id: str | None = None,
        created_at: datetime | None = None,
        today: str | None = None,
        rng: Any = None,
    ) -> dict[str, Any]:
        """Create one text message and its stable cross-channel ReceiptID."""
        checked_config = validate_messages_config(config)
        if not isinstance(delivery_mode, str) or delivery_mode not in DELIVERY_MODES:
            raise MessageLineError(f"unsupported delivery mode: {delivery_mode}")
        timestamp = created_at or datetime.now(timezone.utc)
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise MessageLineError("created_at must include a timezone")
        timestamp_text = timestamp.isoformat().replace("+00:00", "Z")
        date_text = today or timestamp.date().isoformat()
        date_compact = _valid_calendar_date(date_text)
        if date_text != timestamp.date().isoformat():
            raise MessageLineError("message ID date must match created_at's local date")
        _require_ref(origin_ref, "origin_ref")
        _require_ref(recipient_ref, "recipient_ref")
        if not isinstance(content_text, str) or not content_text.strip():
            raise MessageLineError("content_text must contain message text")
        if len(content_text) > MAX_MESSAGE_TEXT_CHARS:
            raise MessageLineError("content_text exceeds the local message size limit")
        if "\x00" in content_text:
            raise MessageLineError("content_text must not contain NUL bytes")

        existing = self._read_all()
        used_message_numbers = {
            int(record["message_id"].rsplit("-", 1)[1])
            for record in existing
            if record["message_id"].split("-")[1] == date_compact
        }
        used_receipt_numbers = self._existing_receipt_numbers(date_compact)
        thread_id = self._reserve_thread_id(thread_id, date_compact, timestamp_text, rng)

        target_snapshot = selected_targets(checked_config, delivery_mode)
        record_base = {
            "schema": MESSAGE_TICKET_SCHEMA,
            "thread_id": thread_id,
            "created_at": timestamp_text,
            "origin_ref": origin_ref,
            "recipient_ref": recipient_ref,
            "content_text": content_text,
            "delivery_mode": delivery_mode,
            "target_snapshot": target_snapshot,
            "delivery_policy_snapshot": {
                "gui_open_counts_as_delivery": checked_config[
                    "gui_open_counts_as_delivery"
                ],
                "config_updated_at": checked_config["updated_at"],
            },
        }

        self.messages_dir.mkdir(parents=True, exist_ok=True)
        self._receipts_root.mkdir(parents=True, exist_ok=True)
        for _ in range(100):
            message_id = draw_id("M", date_compact, used_message_numbers, rng)
            receipt_id = draw_id("R", date_compact, used_receipt_numbers, rng)
            record = validate_message_ticket(
                {**record_base, "message_id": message_id, "receipt_id": receipt_id}
            )
            message_path = self.messages_dir / f"{message_id}.json"
            receipt_dir = self._receipt_dir(receipt_id)
            try:
                receipt_dir.mkdir()
            except FileExistsError:
                used_receipt_numbers.add(int(receipt_id.rsplit("-", 1)[1]))
                continue

            ledger = {
                "schema": MESSAGE_RECEIPT_LEDGER_SCHEMA,
                "receipt_id": receipt_id,
                "message_id": message_id,
            }
            ledger_written = self._write_exclusive(receipt_dir / "ledger.json", ledger)
            if not ledger_written:
                receipt_dir.rmdir()
                used_receipt_numbers.add(int(receipt_id.rsplit("-", 1)[1]))
                continue
            try:
                message_written = self._write_exclusive(message_path, record)
            except OSError:
                (receipt_dir / "ledger.json").unlink(missing_ok=True)
                try:
                    receipt_dir.rmdir()
                except OSError:
                    pass
                raise
            if not message_written:
                (receipt_dir / "ledger.json").unlink(missing_ok=True)
                try:
                    receipt_dir.rmdir()
                except OSError:
                    pass
                used_message_numbers.add(int(message_id.rsplit("-", 1)[1]))
                used_receipt_numbers.add(int(receipt_id.rsplit("-", 1)[1]))
                continue
            return record
        raise MessageLineError("no free MessageID/ReceiptID after 100 random draws")

    def get(self, message_id: str) -> dict[str, Any]:
        """Resolve one message ticket, including its text, by stable MessageID."""
        if not isinstance(message_id, str) or not _MESSAGE_ID_RE.fullmatch(message_id):
            raise MessageLineError("message_id must use M-YYYYMMDD-#########")
        _require_identifier_date(message_id, "M-", "message_id")
        path = self.messages_dir / f"{message_id}.json"
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise MessageLineError("message ticket does not exist") from exc
        except (OSError, json.JSONDecodeError) as exc:
            raise MessageLineError("message ticket is unreadable") from exc
        record = validate_message_ticket(raw)
        if record["message_id"] != message_id:
            raise MessageLineError("message filename/id mismatch")
        self._read_receipt_ledger(record)
        self._read_thread_ledger(record["thread_id"])
        return record

    def by_thread(self, thread_id: str) -> list[dict[str, Any]]:
        """Resolve messages in one thread; delivery remains per-message."""
        if not isinstance(thread_id, str) or not _THREAD_ID_RE.fullmatch(thread_id):
            raise MessageLineError("thread_id must use TH-YYYYMMDD-#########")
        _require_identifier_date(thread_id, "TH-", "thread_id")
        records = [record for record in self._read_all() if record["thread_id"] == thread_id]
        return sorted(
            records,
            key=lambda record: (
                datetime.fromisoformat(record["created_at"].replace("Z", "+00:00")),
                record["message_id"],
            ),
        )

    def list_messages(self) -> list[dict[str, Any]]:
        """List message tickets for administration; reading never acknowledges."""
        return sorted(
            self._read_all(),
            key=lambda record: (
                datetime.fromisoformat(record["created_at"].replace("Z", "+00:00")),
                record["message_id"],
            ),
        )

    def list_for_delivery(
        self,
        *,
        delivery_mode: str,
        target_ref: str,
        surface: str,
        current_config: Mapping[str, Any],
    ) -> list[dict[str, Any]]:
        """List only currently routable, unacknowledged tickets for a channel."""
        checked_config = validate_messages_config(current_config)
        _require_ref(target_ref, "target_ref")
        if not isinstance(surface, str) or surface not in SURFACES:
            raise MessageLineError("surface is unsupported")
        if not isinstance(delivery_mode, str) or delivery_mode not in DELIVERY_MODES:
            raise MessageLineError("delivery_mode is unsupported")
        enabled_targets = {
            (target["target_ref"], target["surface"])
            for target in selected_targets(checked_config, delivery_mode)
        }
        target = (target_ref, surface)
        if target not in enabled_targets:
            return []
        eligible: list[dict[str, Any]] = []
        for message in self._read_all():
            if message["delivery_mode"] != delivery_mode:
                continue
            if target not in {
                (item["target_ref"], item["surface"])
                for item in message["target_snapshot"]
            }:
                continue
            if message_delivery_state(message, self.receipts(message["message_id"])) == "delivered":
                continue
            eligible.append(message)
        return sorted(eligible, key=lambda record: (record["created_at"], record["message_id"]))

    def read_for_delivery(
        self,
        message_id: str,
        *,
        target_ref: str,
        surface: str,
        current_config: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Resolve content for a delivery route after applying the live config gate."""
        message = self.get(message_id)
        _require_ref(target_ref, "target_ref")
        if surface not in SURFACES:
            raise MessageLineError("surface is unsupported")
        active = {
            (target["target_ref"], target["surface"])
            for target in effective_targets(message, current_config)
        }
        if (target_ref, surface) not in active:
            raise MessageLineError("current config does not permit this delivery route")
        if self.delivery_state(message_id) == "delivered":
            raise MessageLineError("message is already acknowledged as delivered")
        return message

    def receipts(self, message_id: str) -> list[dict[str, Any]]:
        """Read channel observations from the message's canonical receipt ledger."""
        message = self.get(message_id)
        observations_dir = self._receipt_dir(message["receipt_id"]) / "observations"
        if not observations_dir.exists():
            return []
        if not observations_dir.is_dir():
            raise MessageLineError("receipt observations path is not a directory")
        receipts: list[dict[str, Any]] = []
        for path in sorted(observations_dir.glob("O-*.json")):
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise MessageLineError(f"unreadable receipt observation: {path.name}") from exc
            receipt = validate_message_receipt(raw)
            if path.name != f"{receipt['observation_id']}.json":
                raise MessageLineError("receipt observation filename/id mismatch")
            if receipt["message_id"] != message_id or receipt["receipt_id"] != message["receipt_id"]:
                raise MessageLineError("receipt observation points at a different message")
            receipts.append(receipt)
        return receipts

    def record_receipt(
        self,
        value: Mapping[str, Any],
        *,
        current_config: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Read back an identical stored receipt; new external imports need a verifier."""
        receipt = validate_message_receipt(value)
        path = self._receipt_dir(receipt["receipt_id"]) / "observations" / f"{receipt['observation_id']}.json"
        if not path.is_file():
            raise MessageLineError("external receipt import is inactive without verified provenance")
        return self._store_receipt(receipt, current_config=current_config)

    def _store_receipt(
        self,
        receipt: Mapping[str, Any],
        *,
        current_config: Mapping[str, Any],
        manual_explicit_ack: bool = False,
    ) -> dict[str, Any]:
        checked_receipt = validate_message_receipt(receipt)
        checked_config = validate_messages_config(current_config)
        message = self.get(checked_receipt["message_id"])
        if checked_receipt["receipt_id"] != message["receipt_id"]:
            raise MessageLineError("receipt_id is not the message's canonical ReceiptID")
        if checked_receipt["recipient_ref"] != message["recipient_ref"]:
            raise MessageLineError("receipt recipient does not match the message ticket")
        allowed_targets = {
            (target["target_ref"], target["surface"])
            for target in message["target_snapshot"]
        }
        if (checked_receipt["target_ref"], checked_receipt["surface"]) not in allowed_targets:
            raise MessageLineError("receipt target is outside the message target snapshot")
        created_at = datetime.fromisoformat(message["created_at"].replace("Z", "+00:00"))
        observed_at = datetime.fromisoformat(checked_receipt["observed_at"].replace("Z", "+00:00"))
        if observed_at < created_at:
            raise MessageLineError("receipt observation predates its message ticket")

        observations_dir = self._receipt_dir(checked_receipt["receipt_id"]) / "observations"
        path = observations_dir / f"{checked_receipt['observation_id']}.json"
        if path.exists():
            previous = self._read_existing_receipt(path)
            if previous != checked_receipt:
                raise ReceiptConflictError("observation_id reused for different receipt fields")
            return previous

        _validate_observation_policy(
            message,
            checked_receipt,
            checked_config,
            allow_manual_explicit_ack=manual_explicit_ack,
        )
        observations_dir.mkdir(parents=True, exist_ok=True)
        if self._write_exclusive(path, checked_receipt):
            return dict(checked_receipt)
        previous = self._read_existing_receipt(path)
        if previous != checked_receipt:
            raise ReceiptConflictError("observation_id reused for different receipt fields")
        return previous

    @staticmethod
    def _read_existing_receipt(path: Path) -> dict[str, Any]:
        try:
            previous = validate_message_receipt(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError) as exc:
            raise MessageLineError("existing receipt observation is unreadable") from exc
        return previous

    def record_observation(
        self,
        message_id: str,
        *,
        target_ref: str,
        surface: str,
        channel_ref: str,
        actor_ref: str,
        evidence_ref: str,
        event: str,
        current_config: Mapping[str, Any],
        observed_at: str | None = None,
    ) -> dict[str, Any]:
        """Record an automated or observed channel event under current config."""
        if event == "explicit_ack":
            raise MessageLineError("explicit user acknowledgements must use acknowledge()")
        return self._record_observation(
            message_id,
            target_ref=target_ref,
            surface=surface,
            channel_ref=channel_ref,
            actor_ref=actor_ref,
            evidence_ref=evidence_ref,
            event=event,
            current_config=current_config,
            observed_at=observed_at,
        )

    def _record_observation(
        self,
        message_id: str,
        *,
        target_ref: str,
        surface: str,
        channel_ref: str,
        actor_ref: str,
        evidence_ref: str,
        event: str,
        current_config: Mapping[str, Any],
        observed_at: str | None = None,
        manual_explicit_ack: bool = False,
    ) -> dict[str, Any]:
        if not isinstance(event, str) or event not in DELIVERY_EVENTS:
            raise MessageLineError("receipt event is unsupported")
        if manual_explicit_ack != (event == "explicit_ack"):
            raise MessageLineError("only acknowledge() may create an explicit user acknowledgement")
        message = self.get(message_id)
        checked_config = validate_messages_config(current_config)
        receipt_base = {
            "schema": MESSAGE_RECEIPT_SCHEMA,
            "receipt_id": message["receipt_id"],
            "message_id": message_id,
            "recipient_ref": message["recipient_ref"],
            "target_ref": target_ref,
            "surface": surface,
            "channel_ref": channel_ref,
            "event": event,
            "observed_at": observed_at or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "gui_open_counts_as_delivery": bool(
                checked_config["enabled"]
                and checked_config["gui_open_counts_as_delivery"]
            ),
            "actor_ref": actor_ref,
            "evidence_ref": evidence_ref,
        }
        observation_id = _make_observation_id(receipt_base)
        return self._store_receipt(
            {**receipt_base, "observation_id": observation_id},
            current_config=checked_config,
            manual_explicit_ack=manual_explicit_ack,
        )

    def acknowledge(
        self,
        message_id: str,
        *,
        target_ref: str,
        surface: str,
        channel_ref: str,
        actor_ref: str,
        evidence_ref: str,
        current_config: Mapping[str, Any],
        observed_at: str | None = None,
    ) -> dict[str, Any]:
        """Record a deliberate user acknowledgement; it never triggers routing."""
        return self._record_observation(
            message_id,
            target_ref=target_ref,
            surface=surface,
            channel_ref=channel_ref,
            actor_ref=actor_ref,
            evidence_ref=evidence_ref,
            event="explicit_ack",
            current_config=current_config,
            observed_at=observed_at,
            manual_explicit_ack=True,
        )

    def delivery_state(self, message_id: str) -> str:
        message = self.get(message_id)
        return message_delivery_state(message, self.receipts(message_id))

    def delivery_targets(
        self, message_id: str, current_config: Mapping[str, Any]
    ) -> list[dict[str, str]]:
        """Return targets still permitted by the current global config."""
        return effective_targets(self.get(message_id), current_config)


def validate_message_receipt(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate an externally produced receipt; never creates or persists it."""
    if not isinstance(value, Mapping):
        raise MessageLineError("message receipt must be an object")
    _require_exact_fields(value, _RECEIPT_FIELDS, "message receipt")
    if value["schema"] != MESSAGE_RECEIPT_SCHEMA:
        raise MessageLineError("unsupported message-receipt schema")
    if not isinstance(value["receipt_id"], str) or not _RECEIPT_ID_RE.fullmatch(value["receipt_id"]):
        raise MessageLineError("receipt_id must use R-YYYYMMDD-#########")
    _require_identifier_date(value["receipt_id"], "R-", "receipt_id")
    if not isinstance(value["observation_id"], str) or not _OBSERVATION_ID_RE.fullmatch(value["observation_id"]):
        raise MessageLineError("observation_id must use O- followed by 64 lowercase hex digits")
    if not isinstance(value["message_id"], str) or not _MESSAGE_ID_RE.fullmatch(value["message_id"]):
        raise MessageLineError("receipt message_id must use M-YYYYMMDD-#########")
    _require_identifier_date(value["message_id"], "M-", "receipt message_id")
    for field in ("recipient_ref", "target_ref", "channel_ref", "actor_ref", "evidence_ref"):
        _require_ref(value[field], field)
    if not isinstance(value["surface"], str) or value["surface"] not in SURFACES:
        raise MessageLineError("receipt surface is unsupported")
    if not isinstance(value["event"], str) or value["event"] not in DELIVERY_EVENTS:
        raise MessageLineError("receipt event is unsupported")
    _require_timestamp(value["observed_at"], "observed_at")
    _require_bool(value["gui_open_counts_as_delivery"], "gui_open_counts_as_delivery")
    if value["observation_id"] != _make_observation_id(value):
        raise ReceiptConflictError(
            "observation_id reused or does not match the canonical receipt fields"
        )
    return dict(value)


def _make_observation_id(value: Mapping[str, Any]) -> str:
    receipt_base = {key: item for key, item in value.items() if key != "observation_id"}
    digest = hashlib.sha256(
        json.dumps(
            receipt_base,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return "O-" + digest


def _validate_observation_policy(
    message: Mapping[str, Any],
    receipt: Mapping[str, Any],
    current_config: Mapping[str, Any],
    *,
    allow_manual_explicit_ack: bool,
) -> None:
    """Gate all new channel evidence against the effective current config."""
    event = receipt["event"]
    if event == "on_demand_ack" and message["delivery_mode"] != "on_demand":
        raise MessageLineError("on-demand acknowledgement does not match the message delivery mode")
    if event == "active_chat_ack" and message["delivery_mode"] != "active_chat":
        raise MessageLineError("active-chat acknowledgement does not match the message delivery mode")
    if event == "explicit_ack":
        if not allow_manual_explicit_ack:
            raise MessageLineError("explicit acknowledgements require the manual acknowledge API")
        # A person can attest to already-received content while automatic
        # routing is disabled; this never re-enables any route.
        return

    active_targets = {
        (target["target_ref"], target["surface"])
        for target in effective_targets(message, current_config)
    }
    if (receipt["target_ref"], receipt["surface"]) not in active_targets:
        raise MessageLineError("current config disables automatic delivery for this message")
    if event == "on_demand_ack":
        if not current_config["delivery_modes"]["on_demand"]:
            raise MessageLineError("current config disables on-demand delivery")
    if event == "active_chat_ack":
        if not current_config["delivery_modes"]["active_chat"]:
            raise MessageLineError("current config disables active-chat delivery")
    if event == "gui_opened":
        if not current_config["gui_open_counts_as_delivery"]:
            raise MessageLineError("current config disables GUI-open acknowledgement")
        if not message["delivery_policy_snapshot"]["gui_open_counts_as_delivery"]:
            raise MessageLineError("message policy disables GUI-open acknowledgement")
        if not receipt["gui_open_counts_as_delivery"]:
            raise MessageLineError("GUI-open receipt does not capture the active acknowledgement rule")
    elif receipt["gui_open_counts_as_delivery"] != bool(
        current_config["enabled"]
        and current_config["gui_open_counts_as_delivery"]
    ):
        raise MessageLineError("receipt does not capture the current GUI-open acknowledgement rule")


def message_delivery_state(
    message: Mapping[str, Any], receipts: Iterable[Mapping[str, Any]]
) -> str:
    """Project one message's canonical receipt observations to a user-visible state.

    The message-level ReceiptID is shared across channels; each observation has
    its own stable ID. Identical replay is harmless, conflicting reuse fails
    closed, and offered/transported/fetched/listed/previewed are never delivery.
    """
    checked_message = validate_message_ticket(message)
    seen_receipts: dict[str, dict[str, Any]] = {}
    target_snapshot = {
        (target["target_ref"], target["surface"])
        for target in checked_message["target_snapshot"]
    }
    event_rank = {
        "prepared": 0,
        "offered": 1,
        "transported": 2,
        "fetched": 3,
        "listed": 4,
        "previewed": 5,
        "gui_opened": 6,
    }
    observed_events: list[tuple[int, str, str]] = []
    delivered = False

    for receipt_value in receipts:
        receipt = validate_message_receipt(receipt_value)
        observation_id = receipt["observation_id"]
        previous = seen_receipts.get(observation_id)
        if previous is not None:
            if previous != receipt:
                raise ReceiptConflictError("observation_id reused for different receipt fields")
            continue
        seen_receipts[observation_id] = receipt

        if receipt["message_id"] != checked_message["message_id"]:
            continue
        if receipt["receipt_id"] != checked_message["receipt_id"]:
            continue
        if receipt["recipient_ref"] != checked_message["recipient_ref"]:
            continue
        if (receipt["target_ref"], receipt["surface"]) not in target_snapshot:
            continue
        message_created = datetime.fromisoformat(checked_message["created_at"].replace("Z", "+00:00"))
        receipt_observed = datetime.fromisoformat(receipt["observed_at"].replace("Z", "+00:00"))
        if receipt_observed < message_created:
            raise MessageLineError("receipt observation predates its message ticket")

        event = receipt["event"]
        if event == "on_demand_ack" and checked_message["delivery_mode"] != "on_demand":
            raise MessageLineError("on-demand acknowledgement does not match the message delivery mode")
        if event == "active_chat_ack" and checked_message["delivery_mode"] != "active_chat":
            raise MessageLineError("active-chat acknowledgement does not match the message delivery mode")
        if event in ACK_EVENTS:
            delivered = True
        elif (
            event == "gui_opened"
            and checked_message["delivery_policy_snapshot"]["gui_open_counts_as_delivery"]
            and receipt["gui_open_counts_as_delivery"]
        ):
            delivered = True
        observed_events.append((event_rank.get(event, -1), receipt["observed_at"], event))

    if delivered:
        return "delivered"
    if not observed_events:
        return "prepared"
    return max(observed_events)[2]


def validate_log_registry(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate provenance pointers to logs without ingesting their contents."""
    if not isinstance(value, Mapping):
        raise MessageLineError("log registry must be an object")
    _require_exact_fields(value, _LOG_REGISTRY_FIELDS, "log registry")
    if value["schema"] != LOG_REGISTRY_SCHEMA:
        raise MessageLineError("unsupported log-registry schema")
    sources = value["sources"]
    if not isinstance(sources, list):
        raise MessageLineError("log registry sources must be an array")
    canonical_sources: list[dict[str, str]] = []
    seen: set[str] = set()
    for index, source in enumerate(sources):
        if not isinstance(source, Mapping):
            raise MessageLineError(f"sources[{index}] must be an object")
        _require_exact_fields(source, _LOG_SOURCE_FIELDS, f"sources[{index}]")
        source_ref = _require_ref(source["source_ref"], f"sources[{index}].source_ref")
        if source_ref in seen:
            raise MessageLineError("log registry contains a duplicate source_ref")
        seen.add(source_ref)
        for field in (
            "system_ref",
            "owner_ref",
            "access_policy_ref",
            "redaction_policy_ref",
            "provenance_ref",
            "retention_ref",
        ):
            _require_ref(source[field], f"sources[{index}].{field}")
        if not isinstance(source["window"], str) or source["window"] not in LOG_WINDOWS:
            raise MessageLineError(f"sources[{index}].window is unsupported")
        _require_timestamp(source["observed_at"], f"sources[{index}].observed_at")
        if source["materialization"] != "reference_only":
            raise MessageLineError("log registry must remain pointer-only")
        canonical_sources.append(dict(source))
    return {"schema": LOG_REGISTRY_SCHEMA, "sources": canonical_sources}
