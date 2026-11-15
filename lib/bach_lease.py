r"""Optional coupling of ticket claims to a BACH task lease (T-20261115-427953604).

Problem: the filename suffix (`T-....<HOST>.txt`) is only a *display* of a claim. When two
ticket-master instances run on different hosts and one of them has no working sync, both
can claim the same ticket. The file system cannot decide that; a central authority can.

Solution: with `bach_lease.enabled = true` in `config/ticket-master.config.json`,
`ticket_mover --claim-current-host` takes a lease on a task of that authority (one task per
ticket, created idempotently) BEFORE it renames the file. 409 = somebody else holds it ->
the claim is refused and the holder is named. Moves into SOLVED/BLOCKED/USER/... and
`--release-session` release the lease with a matching outcome. `--renew-leases` (and every
VERLAUF note written through `ticket_note`) renews held leases.

Design choices:

* **Off by default.** A public install never talks to anything (INV-LOCAL-01). Nothing in
  this module touches the network unless `enabled` is true AND a ticket is claimed, moved
  or noted that this host actually holds a lease for.
* **One authority.** The remote task DB is the truth. The only local file is a cache of
  `lease_id`/`fence` per ticket, kept OUTSIDE any OneDrive tree (refused otherwise): it is
  a capability, not a record.
* **Fail-closed.** With `enabled = true`, an unreachable lead or a missing token refuses
  the claim. `fail_open = true` is an explicit opt-out that only warns.
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
import json
import os
import re
import socket
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
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
    "load_config",
    "outcome_for_cluster",
    "release_lease",
    "renew_leases",
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


class LeaseUnavailableError(LeaseError):
    """The lead is unreachable, answered unusably, or no token/config is available."""


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


def save_state(cfg: dict, state: dict[str, dict]) -> None:
    path = state_path(cfg)
    payload = json.dumps(state, indent=1, ensure_ascii=False)  # encode first, then replace
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
        os.replace(tmp, path)
    finally:
        with contextlib.suppress(OSError):
            os.unlink(tmp)


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


def _holder_text(body: dict) -> str:
    """Human-readable holder from a 409 body (never lease_id or fence)."""
    holder = body.get("holder") or body.get("held_by") or body.get("worker_id")
    parts = [str(holder)] if holder else []
    for key in ("host", "expires_at", "detail", "error"):
        if body.get(key) and str(body[key]) not in parts:
            parts.append(f"{key}={body[key]}")
    return ", ".join(parts) or "unknown holder"


class BachLeaseClient:
    """Task + lease calls against the lead. All network access goes through `transport`."""

    def __init__(self, cfg: dict, *, transport: Transport | None = None,
                 token_provider: Callable[[str], str | None] | None = None,
                 host: str | None = None) -> None:
        self.cfg = cfg
        self.host = (host or socket.gethostname()).strip().upper()
        self._transport = transport or _urllib_transport
        self._token_provider = token_provider or (lambda h: _default_token(cfg, h))
        lead = str(cfg.get("lead_url") or "").rstrip("/")
        if not re.match(r"^https?://", lead):
            raise LeaseUnavailableError("bach_lease.lead_url is missing or not an http(s) URL")
        self.lead = lead

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
        if known and self._renew_entry(known, state):
            return known
        status, body = self._call("POST", "/api/tasks", {
            "title": title,
            "source": f"ticket:{ticket_id}:ticket-master",
            "assigned_to": self.cfg.get("assigned_to") or "TICKET-MASTER",
            "created_by": self.worker_id,
            "category": self.cfg.get("category") or "tickets",
        })
        task = body.get("task") if isinstance(body.get("task"), dict) else {}
        task_id = body.get("id") or task.get("id")
        if not 200 <= status < 300 or not task_id:
            raise LeaseUnavailableError(f"task create/lookup failed (HTTP {status})")
        status, body = self._call("POST", f"/api/tasks/{task_id}/lease", {
            "worker_id": self.worker_id,
            "host": self.host,
            "ttl_profile": self.cfg.get("ttl_profile") or "M",
            "intent": ticket_id,
            "request_id": str(uuid.uuid4()),
        })
        if status == 409:
            raise LeaseConflictError(
                f"{ticket_id} is leased by another worker: {_holder_text(body)}")
        if not 200 <= status < 300 or not body.get("lease_id"):
            raise LeaseUnavailableError(f"lease request failed (HTTP {status})")
        entry = {"task_id": task_id, "lease_id": body["lease_id"], "fence": body.get("fence"),
                 "worker_id": self.worker_id, "acquired": int(time.time())}
        state[ticket_id] = entry
        save_state(self.cfg, state)
        return entry

    def _renew_entry(self, entry: dict, state: dict) -> bool:
        status, body = self._call("POST", f"/api/tasks/{entry['task_id']}/lease/renew",
                                  {"lease_id": entry["lease_id"], "fence": entry["fence"]})
        if 200 <= status < 300:
            if body.get("fence") is not None:
                entry["fence"] = body["fence"]
                save_state(self.cfg, state)
            return True
        return False

    def renew(self, ticket_id: str) -> bool:
        state = load_state(self.cfg)
        entry = state.get(ticket_id)
        return bool(entry) and self._renew_entry(entry, state)

    def release(self, ticket_id: str, outcome: str, result_ref: str = "") -> bool:
        state = load_state(self.cfg)
        entry = state.get(ticket_id)
        if not entry:
            return False
        status, _body = self._call("POST", f"/api/tasks/{entry['task_id']}/lease/release", {
            "lease_id": entry["lease_id"], "fence": entry["fence"],
            "outcome": outcome, "result_ref": result_ref, "note": self.worker_id,
        })
        # 404/409/410: the lease is already gone on the lead's side; forget the cache either way.
        if 200 <= status < 300 or status in (404, 409, 410):
            state.pop(ticket_id, None)
            save_state(self.cfg, state)
            return 200 <= status < 300
        raise LeaseUnavailableError(f"lease release failed (HTTP {status})")


# --- high-level hooks used by ticket_mover / ticket_note ----------------------


def _client(cfg: dict, client: BachLeaseClient | None, host: str | None) -> BachLeaseClient:
    return client or BachLeaseClient(cfg, host=host)


def claim_lease(ticket: Path | str, *, host: str, config: dict | None = None,
                client: BachLeaseClient | None = None) -> bool:
    """Take the lease for a ticket about to be claimed by `host`.

    Returns True when a lease is held, False when the feature is off (or a fail-open skip).
    Raises LeaseConflictError on 409 and LeaseUnavailableError when enabled and unusable.
    """
    cfg = load_config(config)
    if not cfg["enabled"]:
        return False
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
            print(f"WARNING: lease not taken (fail_open): {exc}", file=sys.stderr)
            return False
        raise
    return True


def release_lease(ticket_name: str, outcome: str, *, result_ref: str = "",
                  config: dict | None = None, client: BachLeaseClient | None = None) -> str | None:
    """Best-effort release. Never raises; returns a warning text or None.

    No local cache entry for the ticket -> nothing happens, and in particular no network call.
    """
    ticket_id = ticket_id_of(ticket_name)
    try:
        cfg = load_config(config)
        if not cfg["enabled"] or ticket_id is None or ticket_id not in load_state(cfg):
            return None
        _client(cfg, client, None).release(ticket_id, outcome, result_ref)
    except LeaseError as exc:
        return f"WARNING: lease for {ticket_id} not released ({exc}); it expires on its TTL"
    return None


def renew_leases(*, config: dict | None = None, client: BachLeaseClient | None = None,
                 only: str | None = None) -> list[tuple[str, bool, str]]:
    """Renew every cached lease (or only `only`). Returns (ticket_id, ok, message)."""
    cfg = load_config(config)
    if not cfg["enabled"]:
        return []
    results: list[tuple[str, bool, str]] = []
    try:
        state = load_state(cfg)
        ids = ([only] if only in state else []) if only else sorted(state)
        for ticket_id in ids:
            try:
                ok = _client(cfg, client, None).renew(ticket_id)
                results.append((ticket_id, ok, "renewed" if ok else "not renewed (no cache entry or lead refused)"))
            except LeaseError as exc:
                results.append((ticket_id, False, str(exc)))
    except LeaseError as exc:
        results.append(("-", False, str(exc)))
    return results
