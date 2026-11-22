r"""Optional coupling of ticket claims to a BACH task lease (T-20261115-427953604).

Problem: the filename suffix (`T-....<HOST>.txt`) is only a *display* of a claim. When two
ticket-master instances run on different hosts and one of them has no working sync, both
can claim the same ticket. The file system cannot decide that; a central authority can.

Solution: with `bach_lease.enabled = true` in `config/ticket-master.config.json`,
`ticket_mover --claim-current-host` takes a lease on a task of that authority (one task per
ticket, created idempotently) BEFORE it renames the file. 409 = somebody else holds it ->
the claim is refused and the holder is named. Moves into SOLVED/BLOCKED/USER/... and
`--release-session` release the lease with a matching outcome. `--renew-leases` (and every
VERLAUF note written through `ticket_note`) renews held leases, cleans stale cache entries
for solved/inactive tickets, and backfills missing leases; `--backfill-leases` acquires
leases for existing or fallback (`pending_lease`) claims at boot.

Design choices:

* **Off by default.** A public install never talks to anything (INV-LOCAL-01). Nothing in
  this module touches the network unless `enabled` is true AND a ticket is claimed, moved
  or noted that this host actually holds a lease for.
* **One authority.** The remote task DB is the truth. The only local file is a cache of
  `lease_id`/`fence` (or `pending_lease` fallback markers) per ticket, kept OUTSIDE any
  OneDrive tree (refused otherwise): it is a capability, not a record.
* **Fail-closed.** With `enabled = true`, an unreachable lead or a missing token refuses
  the claim. `fail_open = true` is an explicit fallback to file mode when the lead is
  unreachable or the token is missing (never on 409): `ticket_mover` writes a visible
  VERLAUF note and records `pending_lease` in the cache for later backfill.
* **Release never undoes a move.** If the release call fails, the ticket stays where it is
  and a warning names the problem; the lease then expires on its own TTL.
* **Stdlib only** (INV-PORTABLE-09). The token comes from an environment variable or, if
  the optional `keyring` package is installed and configured, from the OS keyring. It is
  never printed, logged or put into an exception message.
* **No private defaults.** Lead URL, keyring service and entry names are empty in the
  defaults and the example config; they belong in the local (gitignored) config.
"""

from __future__ import annotations

import contextlib
import ipaddress
import json
import os
import re
import socket
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

try:  # package import
    from .config_paths import resolve_config_path, tm_config
except ImportError:  # direct import from lib on sys.path
    from config_paths import resolve_config_path, tm_config

__all__ = [
    "BachLeaseClient",
    "LeaseConflictError",
    "LeaseError",
    "LeaseUnavailableError",
    "claim_lease",
    "claim_lease_status",
    "clean_inactive_lease",
    "clear_pending_lease",
    "has_lease_capability",
    "is_pending_lease_entry",
    "load_config",
    "outcome_for_cluster",
    "record_pending_lease",
    "release_lease",
    "renew_leases",
    "worker_id_for_host",
]

TICKET_ID_RE = re.compile(r"^(T-\d{8}-\d+)")

# Destination folder -> release outcome. A move that stays inside QUEUED keeps the lease.
_OUTCOME_BY_CLUSTER = {
    "SOLVED": "done",
    "BLOCKED": "blocked",
    "USER": "return",
    "WAITING": "return",
    "PARKED": "return",
    "ACTIONABLE": "return",
    "INBOX": "return",
}

DEFAULT_CONFIG: dict[str, Any] = {
    "enabled": False,
    "lead_url": "",
    "ttl_profile": "M",
    "fail_open": False,
    "timeout_s": 20,
    "token_env": "TICKET_MASTER_BACH_TOKEN",
    "keyring_service": "",
    "keyring_entries": [],
    "state_file": "<HOME>/.ticket-master/bach_leases.json",
    "assigned_to": "TICKET-MASTER",
    "category": "tickets",
}

Transport = Callable[[str, str, dict, "bytes | None", float], "tuple[int, dict]"]


class LeaseError(RuntimeError):
    """Base class; the CLI already reports RuntimeError as `REFUSED: ...`."""


class LeaseConflictError(LeaseError):
    """409: another worker holds the lease for this ticket."""

    def __init__(self, message: str, *, holder: str = "") -> None:
        super().__init__(message)
        self.holder = holder


class LeaseUnavailableError(LeaseError):
    """The lead is unreachable, answered unusably, or no token/config is available."""


def _normalize_host(host: str | None = None) -> str:
    """Normalize a host token: strip whitespace, a trailing `.local`, and uppercase."""
    raw = (host or socket.gethostname()).strip()
    if raw.casefold().endswith(".local"):
        raw = raw[:-6].strip()
    return raw.upper()


def worker_id_for_host(host: str | None = None) -> str:
    """Canonical worker identifier `<role>@<HOST>` for lease and fallback notes."""
    resolved = _normalize_host(host)
    return f"ticket-master@{resolved}"


def is_pending_lease_entry(entry: object) -> bool:
    """True when a cache entry marks a fail-open fallback claim awaiting backfill."""
    return isinstance(entry, dict) and (
        entry.get("pending_lease") is True or entry.get("status") == "pending_lease"
    )


def has_lease_capability(entry: object) -> bool:
    """True when a cache entry holds a syntactically usable task/lease capability."""
    return (
        isinstance(entry, dict)
        and not is_pending_lease_entry(entry)
        and _valid_task_id(entry.get("task_id")) is not None
        and isinstance(entry.get("lease_id"), str)
        and bool(entry.get("lease_id"))
    )


_BEARER_RE = re.compile(r"(?i)bearer\s+\S+")
_URL_RE = re.compile(r"https?://\S+")


def _safe_fallback_reason(exc: BaseException, cfg: dict | None = None) -> str:
    """Sanitize a LeaseUnavailableError reason so tokens, URLs or pipes never enter VERLAUF."""
    raw = str(exc).strip() or type(exc).__name__
    raw = _BEARER_RE.sub("Bearer [REDACTED]", raw)
    raw = _URL_RE.sub("[REDACTED-URL]", raw)
    if isinstance(cfg, dict):
        env_name = cfg.get("token_env")
        env_secret = os.environ.get(str(env_name)) if env_name else None
        if env_secret:
            raw = raw.replace(env_secret, "[REDACTED]")
    cleaned = " ".join(raw.replace("|", "/").split())
    return cleaned[:200] or type(exc).__name__


# --- configuration ----------------------------------------------------------


def load_config(config: dict | None = None) -> dict[str, Any]:
    """The `bach_lease` section merged over the defaults (disabled when absent)."""
    raw = (tm_config() if config is None else config).get("bach_lease")
    merged = dict(DEFAULT_CONFIG)
    if isinstance(raw, dict):
        merged.update({k: v for k, v in raw.items() if not str(k).startswith("_")})
    merged["enabled"] = merged.get("enabled") is True
    merged["fail_open"] = merged.get("fail_open") is True
    return merged


def state_path(cfg: dict) -> Path:
    path = resolve_config_path(cfg.get("state_file"))
    if path is None:
        raise LeaseUnavailableError("bach_lease.state_file is not a usable path")
    if any(part.lower().startswith("onedrive") for part in path.parts):
        raise LeaseUnavailableError(
            "bach_lease.state_file must be outside OneDrive (it holds lease capabilities)"
        )
    return path


def ticket_id_of(name: str) -> str | None:
    match = TICKET_ID_RE.match(Path(name).name)
    return match.group(1) if match else None


def outcome_for_cluster(cluster: str) -> str | None:
    """Release outcome for a destination folder, or None when the lease must stay."""
    return _OUTCOME_BY_CLUSTER.get(cluster.upper())


def _title_of(ticket: Path, ticket_id: str) -> str:
    try:
        for line in ticket.read_text(encoding="utf-8-sig", errors="replace").splitlines()[:40]:
            if line.startswith("TITEL:"):
                title = line.split(":", 1)[1].strip()
                if title:
                    return f"{ticket_id} {title}"[:160]
    except OSError:
        pass
    return ticket_id


# --- local lease cache (lease_id/fence only) ---------------------------------


def load_state(cfg: dict) -> dict[str, dict]:
    try:
        data = json.loads(state_path(cfg).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise LeaseUnavailableError(f"lease cache unreadable: {type(exc).__name__}") from exc
    return data if isinstance(data, dict) else {}


@contextlib.contextmanager
def _state_write_lock(cfg: dict):
    """Coordinate short cache transactions across processes; never hold across HTTP."""
    path = state_path(cfg)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(path) + ".lock", os.O_RDWR | os.O_CREAT, 0o600)
        with os.fdopen(fd, "a+b") as lock:
            if os.name == "nt":
                import msvcrt
                if os.fstat(lock.fileno()).st_size == 0:
                    lock.write(b"\0")
                    lock.flush()

                def take():
                    lock.seek(0)
                    msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)

                def drop():
                    lock.seek(0)
                    msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                def take():
                    fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

                def drop():
                    fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

            deadline = time.monotonic() + 5
            while True:
                try:
                    take()
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise LeaseUnavailableError("lease cache busy; capability retained") from None
                    time.sleep(0.01)
            try:
                yield
            finally:
                drop()
    except OSError as exc:
        raise LeaseUnavailableError(f"lease cache write failed: {type(exc).__name__}") from exc


def _write_state(cfg: dict, state: dict[str, dict]) -> None:
    """Atomic replace; caller must hold _state_write_lock."""
    path = state_path(cfg)
    payload = json.dumps(state, indent=1, ensure_ascii=False)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
        os.replace(tmp, path)
    finally:
        with contextlib.suppress(OSError):
            os.unlink(tmp)


def save_state(cfg: dict, state: dict[str, dict]) -> None:
    """Explicit whole-cache replacement, serialized with production transactions."""
    with _state_write_lock(cfg):
        _write_state(cfg, state)


def _same_capability(current: object, expected: dict) -> bool:
    if not isinstance(current, dict):
        return False
    return all(
        type(current.get(key)) is type(expected.get(key)) and current.get(key) == expected.get(key)
        for key in ("task_id", "lease_id", "fence", "worker_id", "pending_lease", "status")
    )


def _update_cache_entry(cfg: dict, ticket_id: str, expected: dict | None,
                        replacement: dict | None) -> bool:
    """Compare/update one exact capability in the current cache under a shared lock."""
    with _state_write_lock(cfg):
        current = load_state(cfg)
        if expected is None:
            if ticket_id in current:
                return False
        elif not _same_capability(current.get(ticket_id), expected):
            return False
        if replacement is None:
            current.pop(ticket_id, None)
        else:
            current[ticket_id] = replacement
        _write_state(cfg, current)
        return True


# --- HTTP client -------------------------------------------------------------


def _json_or_empty(raw: bytes) -> dict:
    try:
        data = json.loads(raw or b"{}")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _urllib_transport(method: str, url: str, headers: dict, body: bytes | None,
                      timeout: float) -> tuple[int, dict]:
    req = urllib.request.Request(url, method=method, data=body, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - scheme checked
            return resp.status, _json_or_empty(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, _json_or_empty(exc.read())
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise LeaseUnavailableError(f"lead not reachable ({type(exc).__name__})") from exc


def _default_token(cfg: dict, host: str) -> str | None:
    env_name = cfg.get("token_env")
    if env_name and os.environ.get(str(env_name)):
        return os.environ[str(env_name)]
    service = cfg.get("keyring_service")
    entries = cfg.get("keyring_entries")
    if not service or not isinstance(entries, list):
        return None
    try:
        import keyring  # optional extra, deliberately not a dependency
    except ImportError:
        return None
    for template in entries:
        for variant in (host, host.lower()):
            with contextlib.suppress(Exception):
                value = keyring.get_password(str(service), str(template).format(host=variant))
                if value:
                    return value
    return None


_CGNAT = ipaddress.ip_network("100.64.0.0/10")  # Tailscale and other carrier-grade NAT overlays


def _plaintext_lead_warning(lead: str) -> str | None:
    """A warning when the bearer token would travel in clear text over an untrusted network.

    http:// is accepted without a word for loopback and for the 100.64.0.0/10 overlay range
    (Tailscale); any other http:// host gets a warning that recommends https. Never refuses:
    the operator decides, the warning only makes the choice visible.
    """
    parsed = urllib.parse.urlparse(lead)
    if parsed.scheme != "http":
        return None
    host = (parsed.hostname or "").lower()
    if host == "localhost":
        return None
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and (address.is_loopback or address in _CGNAT):
        return None
    return (f"WARNING: bach_lease.lead_url uses http:// to {host or '?'}; the bearer token is sent in "
            "clear text. Use https:// or a loopback/overlay (e.g. Tailscale) address.")


def _valid_task_id(value: object) -> int | None:
    """The task id from the lead's answer, or None. It ends up in a URL path, so only a
    positive integer (or its decimal string) is accepted."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, str) and value.isascii() and value.isdigit() and int(value) > 0:
        return int(value)
    return None


def _holder_text(body: dict) -> str:
    """Human-readable holder from a 409 body (never lease_id or fence)."""
    holder = body.get("holder") or body.get("held_by") or body.get("worker_id")
    parts = [str(holder)] if holder else []
    for key in ("host", "expires_at", "detail", "error"):
        if body.get(key) and str(body[key]) not in parts:
            parts.append(f"{key}={body[key]}")
    return ", ".join(parts) or "unknown holder"


_RELEASE_REASONS = frozenset({
    "reason_not_provided", "ticket_claim_rollback", "ticket_claim_released", "ticket_moved",
})
_RELEASE_STATUSES = {"return": "pending", "blocked": "blocked", "done": "done", "review": "review"}
_INVALID_LEASE_REASONS = frozenset({"expired", "stale_fence"})
_SAFE_RELEASE_DENIALS = _INVALID_LEASE_REASONS | {"completion_guard", "stale_task_version", "conflict"}


def _release_note(reason_code: str | None, reason_detail: str | None) -> str:
    """Closed caller context only; no actor, exception text, path or token."""
    code = reason_code if reason_code not in (None, "") else "reason_not_provided"
    if type(code) is not str or code not in _RELEASE_REASONS:
        raise LeaseUnavailableError("unsupported release reason code")
    if reason_detail not in (None, ""):
        if type(reason_detail) is not str or code != "ticket_moved" or reason_detail not in _OUTCOME_BY_CLUSTER:
            raise LeaseUnavailableError("unsupported release reason detail")
        return f"{code}:{reason_detail.lower()}"
    return code


def _native_response_matches(body: object, task_id: int) -> bool:
    """Correlate the lead's finite ACK/denial shape without trusting error text."""
    if not isinstance(body, dict) or type(body.get("task_id")) is not int or body["task_id"] != task_id:
        return False
    stamp = body.get("server_now")
    if type(stamp) is not str or len(stamp) > 40:
        return False
    try:
        return datetime.fromisoformat(stamp.replace("Z", "+00:00")).utcoffset() == timedelta(0)
    except ValueError:
        return False


class BachLeaseClient:
    """Task + lease calls against the lead. All network access goes through `transport`."""

    def __init__(self, cfg: dict, *, transport: Transport | None = None,
                 token_provider: Callable[[str], str | None] | None = None,
                 host: str | None = None) -> None:
        self.cfg = cfg
        self.host = _normalize_host(host)
        self._transport = transport or _urllib_transport
        self._token_provider = token_provider or (lambda h: _default_token(cfg, h))
        lead = str(cfg.get("lead_url") or "").rstrip("/")
        if not re.match(r"^https?://", lead):
            raise LeaseUnavailableError("bach_lease.lead_url is missing or not an http(s) URL")
        self.lead = lead
        warning = _plaintext_lead_warning(lead)
        if warning:
            print(warning, file=sys.stderr)

    @property
    def worker_id(self) -> str:
        return f"ticket-master@{self.host}"

    def _call(self, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
        token = self._token_provider(self.host)
        if not token:
            raise LeaseUnavailableError("no lease token available (env var or keyring entry)")
        headers = {"Authorization": "Bearer " + token, "Content-Type": "application/json"}
        data = json.dumps(body).encode("utf-8") if body is not None else None
        return self._transport(method, self.lead + path, headers, data,
                               float(self.cfg.get("timeout_s") or 20))

    def acquire(self, ticket_id: str, title: str) -> dict:
        """Idempotent task + lease. Returns the cache entry; raises on 409 / unusable answer."""
        state = load_state(self.cfg)
        known = state.get(ticket_id)
        if known and _valid_task_id(known.get("task_id")) is not None and self._renew_entry(ticket_id, known):
            return known
        status, body = self._call("POST", "/api/tasks", {
            "title": title,
            "source": f"ticket:{ticket_id}:ticket-master",
            "assigned_to": self.cfg.get("assigned_to") or "TICKET-MASTER",
            "created_by": self.worker_id,
            "category": self.cfg.get("category") or "tickets",
        })
        if status == 409:
            # A conflict on the task itself is an answer, not an outage: never let fail_open skip it.
            holder_detail = _holder_text(body)
            holder_name = str(
                body.get("holder") or body.get("held_by") or body.get("worker_id") or holder_detail
            )
            raise LeaseConflictError(
                f"{ticket_id}: task create refused by the lead: {holder_detail}", holder=holder_name
            )
        task = body.get("task") if isinstance(body.get("task"), dict) else {}
        task_id = _valid_task_id(body.get("id") or task.get("id"))
        if not 200 <= status < 300 or task_id is None:
            raise LeaseUnavailableError(f"task create/lookup failed (HTTP {status} or unusable task id)")
        status, body = self._call("POST", f"/api/tasks/{task_id}/lease", {
            "worker_id": self.worker_id,
            "host": self.host,
            "ttl_profile": self.cfg.get("ttl_profile") or "M",
            "intent": ticket_id,
            "request_id": str(uuid.uuid4()),
        })
        if status == 409:
            holder_detail = _holder_text(body)
            holder_name = str(
                body.get("holder") or body.get("held_by") or body.get("worker_id") or holder_detail
            )
            raise LeaseConflictError(
                f"{ticket_id} is leased by another worker: {holder_detail}", holder=holder_name
            )
        if not 200 <= status < 300 or not body.get("lease_id"):
            raise LeaseUnavailableError(f"lease request failed (HTTP {status})")
        entry = {"task_id": task_id, "lease_id": body["lease_id"], "fence": body.get("fence"),
                 "worker_id": self.worker_id, "acquired": int(time.time())}
        if not _update_cache_entry(self.cfg, ticket_id, known, entry):
            raise LeaseUnavailableError("lease cache changed during acquire; current capability retained")
        return entry

    def _renew_entry(self, ticket_id: str, entry: dict) -> bool:
        if _valid_task_id(entry.get("task_id")) is None:
            return False
        status, body = self._call("POST", f"/api/tasks/{entry['task_id']}/lease/renew",
                                  {"lease_id": entry["lease_id"], "fence": entry["fence"]})
        if 200 <= status < 300:
            renewed = dict(entry)
            if body.get("fence") is not None:
                renewed["fence"] = body["fence"]
            if not _update_cache_entry(self.cfg, ticket_id, entry, renewed):
                raise LeaseUnavailableError("lease cache changed during renew; current capability retained")
            entry.update(renewed)
            return True
        return False

    def renew(self, ticket_id: str) -> bool:
        state = load_state(self.cfg)
        entry = state.get(ticket_id)
        return bool(entry) and self._renew_entry(ticket_id, entry)

    def release(self, ticket_id: str, outcome: str, result_ref: str = "", *,
                reason_code: str | None = None, reason_detail: str | None = None) -> bool:
        note = _release_note(reason_code, reason_detail)
        if type(outcome) is not str or outcome not in _RELEASE_STATUSES:
            raise LeaseUnavailableError("unsupported lease release outcome")
        state = load_state(self.cfg)
        entry = state.get(ticket_id)
        if not entry:
            return False
        if _valid_task_id(entry.get("task_id")) is None:
            _update_cache_entry(self.cfg, ticket_id, entry, None)  # never use a corrupt task id in a URL
            raise LeaseUnavailableError("lease cache entry has an unusable task id; dropped")
        status, body = self._call("POST", f"/api/tasks/{entry['task_id']}/lease/release", {
            "lease_id": entry["lease_id"], "fence": entry["fence"],
            "outcome": outcome, "result_ref": result_ref, "note": note,
        })
        if type(status) is not int:
            raise LeaseUnavailableError("lease release response is invalid")
        correlated = _native_response_matches(body, _valid_task_id(entry["task_id"]))
        released = (200 <= status < 300 and correlated and body.get("released") is True
                    and type(body.get("fence")) is int and body["fence"] == entry["fence"]
                    and body.get("outcome") == outcome and body.get("status") == _RELEASE_STATUSES[outcome])
        invalidated = (status == 409 and correlated and body.get("granted") is False
                       and type(body.get("reason")) is str and body["reason"] in _INVALID_LEASE_REASONS)
        if released or invalidated:
            _update_cache_entry(self.cfg, ticket_id, entry, None)
            return released
        reason = body.get("reason") if isinstance(body, dict) else None
        safe_reason = reason if type(reason) is str and reason in _SAFE_RELEASE_DENIALS else "reason_unavailable"
        raise LeaseUnavailableError(f"lease release not confirmed (HTTP {status}; {safe_reason}); capability retained")


# --- high-level hooks used by ticket_mover / ticket_note ----------------------


def _client(cfg: dict, client: BachLeaseClient | None, host: str | None) -> BachLeaseClient:
    return client or BachLeaseClient(cfg, host=host)


def record_pending_lease(ticket_id: str, *, host: str, reason: str = "",
                         config: dict | None = None) -> bool:
    """Record a fail-open fallback claim as `pending_lease` in the local cache.

    Returns True only when `pending_lease` was newly recorded; never overwrites a
    live lease capability or an already-pending marker.
    """
    cfg = load_config(config)
    resolved_host = _normalize_host(host)
    pending_entry: dict[str, Any] = {
        "status": "pending_lease",
        "pending_lease": True,
        "worker_id": worker_id_for_host(resolved_host),
        "host": resolved_host,
        "recorded_at": int(time.time()),
        "reason": reason,
    }
    with _state_write_lock(cfg):
        current = load_state(cfg)
        existing = current.get(ticket_id)
        if has_lease_capability(existing):
            return False
        if is_pending_lease_entry(existing):
            return False
        current[ticket_id] = pending_entry
        _write_state(cfg, current)
        return True


def clear_pending_lease(ticket_name: str, *, config: dict | None = None) -> None:
    """Best-effort rollback of a `pending_lease` marker when the file rename fails."""
    ticket_id = ticket_id_of(ticket_name)
    if ticket_id is None:
        return
    with contextlib.suppress(LeaseError, OSError):
        cfg = load_config(config)
        with _state_write_lock(cfg):
            current = load_state(cfg)
            if is_pending_lease_entry(current.get(ticket_id)):
                current.pop(ticket_id, None)
                _write_state(cfg, current)


def claim_lease_status(ticket: Path | str, *, host: str, config: dict | None = None,
                       client: BachLeaseClient | None = None) -> tuple[bool, str | None]:
    """Take the lease for a ticket about to be claimed by `host`.

    Returns `(True, None)` when a lease is held, `(False, None)` when `bach_lease` is disabled
    or when `fail_open` skipped without newly setting `pending_lease` (already holding a cached
    lease capability or already `pending_lease`), and `(False, safe_reason)` when `fail_open`
    newly recorded `pending_lease` for this outage phase.
    Raises `LeaseConflictError` on 409 (never a fallback) and `LeaseUnavailableError` when
    enabled with `fail_open = false`.
    """
    cfg = load_config(config)
    if not cfg["enabled"]:
        return False, None
    ticket = Path(ticket)
    ticket_id = ticket_id_of(ticket.name)
    if ticket_id is None:
        raise LeaseUnavailableError(f"cannot derive a ticket id from {ticket.name!r}")
    try:
        _client(cfg, client, host).acquire(ticket_id, _title_of(ticket, ticket_id))
    except LeaseConflictError:
        raise  # never fail-open on a real conflict
    except LeaseUnavailableError as exc:
        if cfg["fail_open"]:
            reason = _safe_fallback_reason(exc, cfg)
            print(f"WARNING: lease not taken (fail_open): {reason}", file=sys.stderr)
            recorded = record_pending_lease(ticket_id, host=host, reason=reason, config=cfg)
            return False, (reason if recorded else None)
        raise
    return True, None


def claim_lease(ticket: Path | str, *, host: str, config: dict | None = None,
                client: BachLeaseClient | None = None) -> bool:
    """Take the lease for a ticket about to be claimed by `host`.

    Returns True when a lease is held, False when the feature is off (or a fail-open skip).
    Raises LeaseConflictError on 409 and LeaseUnavailableError when enabled and unusable.
    """
    leased, _fallback_reason = claim_lease_status(ticket, host=host, config=config, client=client)
    return leased


def release_lease(ticket_name: str, outcome: str, *, result_ref: str = "",
                  reason_code: str | None = None, reason_detail: str | None = None,
                  config: dict | None = None, client: BachLeaseClient | None = None) -> str | None:
    """Best-effort release. Never raises; returns a warning text or None.

    No local cache entry for the ticket -> nothing happens, and in particular no network call.
    A `pending_lease` marker is dropped locally without touching the network.
    """
    ticket_id = ticket_id_of(ticket_name)
    try:
        cfg = load_config(config)
        if not cfg["enabled"] or ticket_id is None:
            return None
        state = load_state(cfg)
        entry = state.get(ticket_id)
        if entry is None:
            return None
        if is_pending_lease_entry(entry):
            _update_cache_entry(cfg, ticket_id, entry, None)
            return None
        released = _client(cfg, client, None).release(
            ticket_id, outcome, result_ref, reason_code=reason_code, reason_detail=reason_detail)
        if not released:
            return f"WARNING: lease for {ticket_id} is no longer valid; requested release outcome was not applied"
    except LeaseError as exc:
        return f"WARNING: lease for {ticket_id} not released ({exc}); it expires on its TTL"
    return None


def clean_inactive_lease(ticket_id: str, outcome: str, *, result_ref: str = "",
                         reason_code: str | None = None, reason_detail: str | None = None,
                         config: dict | None = None,
                         client: BachLeaseClient | None = None) -> tuple[str, str]:
    """Cache hygiene for a ticket in SOLVED or no longer in an active folder.

    Valid leases are released with `outcome`; invalid or pending entries are removed from
    the cache; transient lead outages retain the capability and report `("error", reason)`.
    """
    cfg = load_config(config)
    if not cfg["enabled"]:
        return "absent", "disabled"
    try:
        note = _release_note(reason_code, reason_detail)
        if type(outcome) is not str or outcome not in _RELEASE_STATUSES:
            raise LeaseUnavailableError("unsupported lease release outcome")
        state = load_state(cfg)
        entry = state.get(ticket_id)
        if entry is None:
            return "absent", "not in cache"
        task_id = _valid_task_id(entry.get("task_id"))
        if is_pending_lease_entry(entry) or task_id is None or not entry.get("lease_id"):
            _update_cache_entry(cfg, ticket_id, entry, None)
            return "removed", "invalid or pending cache entry removed"
        lease_client = _client(cfg, client, None)
        status, body = lease_client._call("POST", f"/api/tasks/{task_id}/lease/release", {
            "lease_id": entry["lease_id"], "fence": entry["fence"],
            "outcome": outcome, "result_ref": result_ref, "note": note,
        })
        if type(status) is not int:
            raise LeaseUnavailableError("lease release response is invalid")
        correlated = _native_response_matches(body, task_id)
        released = (
            200 <= status < 300
            and correlated
            and body.get("released") is True
            and type(body.get("fence")) is int
            and body["fence"] == entry["fence"]
            and body.get("outcome") == outcome
            and body.get("status") == _RELEASE_STATUSES[outcome]
        )
        if released:
            _update_cache_entry(cfg, ticket_id, entry, None)
            return "released", f"released ({outcome})"
        denial_reason = body.get("reason") if isinstance(body, dict) else None
        if status in (404, 410) or (
            status == 409 and denial_reason not in ("completion_guard", "stale_task_version")
        ):
            _update_cache_entry(cfg, ticket_id, entry, None)
            return "removed", "invalid lease removed from cache"
        safe_reason = (
            denial_reason
            if type(denial_reason) is str and denial_reason in _SAFE_RELEASE_DENIALS
            else "reason_unavailable"
        )
        return "error", f"lease release not confirmed (HTTP {status}; {safe_reason}); capability retained"
    except LeaseError as exc:
        return "error", _safe_fallback_reason(exc, cfg)


def renew_leases(*, config: dict | None = None, client: BachLeaseClient | None = None,
                 only: str | None = None,
                 skip: set[str] | frozenset[str] | None = None,
                 host: str | None = None) -> list[tuple[str, bool, str]]:
    """Renew every cached lease capability (or only `only`), excluding `skip`.
    Returns (ticket_id, ok, message).

    `pending_lease` markers have no remote lease yet and are skipped so backfill can acquire them.
    """
    cfg = load_config(config)
    if not cfg["enabled"]:
        return []
    skipped = skip or ()
    results: list[tuple[str, bool, str]] = []
    try:
        state = load_state(cfg)
        ids = ([only] if only in state else []) if only else sorted(state)
        for ticket_id in ids:
            if ticket_id in skipped or is_pending_lease_entry(state.get(ticket_id)):
                continue
            try:
                ok = _client(cfg, client, host).renew(ticket_id)
                results.append((ticket_id, ok, "renewed" if ok else "not renewed (no cache entry or lead refused)"))
            except LeaseError as exc:
                results.append((ticket_id, False, str(exc)))
    except LeaseError as exc:
        results.append(("-", False, str(exc)))
    return results
