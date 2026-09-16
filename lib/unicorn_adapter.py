"""Small Web/Tray boundary for the public Unicorn intake.

The adapter is intentionally transport-neutral: an embedding web server or
tray client supplies a method, path, and already-decoded JSON object.  This
module renders a small accessible form and exposes only preview and intake
receipt responses.  All validation, ID allocation, and idempotency remain in
``unicorn_intake`` and ``ticket_writer``.

There is no server, thread, subprocess, worker, task, claim, model, lock, or
transport control in this module.  Importing it is inert.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from html import escape
from pathlib import Path
from typing import Any

try:  # package import (``from lib import unicorn_adapter``)
    from .unicorn_intake import (
        IntakeValidationError,
        preview_intake,
        submit_intake,
    )
except ImportError:  # direct module import with ``lib`` on sys.path
    from unicorn_intake import IntakeValidationError, preview_intake, submit_intake


FORM_PATH = "/unicorn"
PREVIEW_PATH = "/unicorn/preview"
SUBMIT_PATH = "/unicorn/submit"
ERROR_SCHEMA = "ellmos.unicorn.adapter-error.v1"

_FORM_FIELDS = ("title", "body", "project", "priority", "pipeline", "request_id")
_JSON_CONTENT_TYPE = "application/json; charset=utf-8"
_HTML_CONTENT_TYPE = "text/html; charset=utf-8"


@dataclass(frozen=True)
class AdapterResponse:
    """Framework-neutral response returned by :func:`web_dispatch`."""

    status_code: int
    content_type: str
    body: str


def _json_response(status_code: int, payload: Mapping[str, Any]) -> AdapterResponse:
    return AdapterResponse(
        status_code,
        _JSON_CONTENT_TYPE,
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
    )


def _error(status_code: int, message: str) -> AdapterResponse:
    # Do not reflect the submitted object.  The core's short validation reason
    # is useful to a form client, while the response shape stays bounded.
    return _json_response(
        status_code,
        {"schema": ERROR_SCHEMA, "error": "invalid_request", "message": message},
    )


def _public_payload(payload: Mapping[str, Any] | None) -> Mapping[str, Any]:
    if payload is None:
        raise IntakeValidationError("request body is required")
    if not isinstance(payload, Mapping):
        raise IntakeValidationError("request body must be a JSON object")
    return payload


def render_form(*, action: str = PREVIEW_PATH) -> str:
    """Return the accessible public Unicorn form without internal controls."""
    if action not in {PREVIEW_PATH, SUBMIT_PATH}:
        raise ValueError("form action must be a public Unicorn endpoint")
    return f'''<!doctype html>
<html lang="de">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Unicorn-Ticketaufnahme</title>
  <style>
    :root {{ color-scheme: light dark; }}
    body {{ font: 1rem/1.5 system-ui, sans-serif; margin: 2rem auto; max-width: 52rem; padding: 0 1rem; }}
    label, legend {{ font-weight: 650; }}
    input, textarea {{ box-sizing: border-box; display: block; font: inherit; margin-top: .35rem; max-width: 100%; padding: .55rem; width: 100%; }}
    textarea {{ min-height: 10rem; }}
    fieldset {{ border: 0; margin: 1rem 0; padding: 0; }}
    .hint {{ color: color-mix(in srgb, currentColor 72%, transparent); }}
    button {{ font: inherit; margin-top: 1rem; padding: .65rem 1rem; }}
  </style>
</head>
<body>
  <main id="unicorn-intake">
    <h1>Unicorn-Ticketaufnahme</h1>
    <p id="form-help" class="hint">Vorschau und Ticketaufnahme enthalten nur die öffentlichen Angaben dieser Anfrage.</p>
    <form method="post" action="{escape(action, quote=True)}" aria-describedby="form-help">
      <fieldset>
        <legend>Öffentliche Anfrage</legend>
        <p>
          <label for="title">Titel</label>
          <input id="title" name="title" required maxlength="120" autocomplete="off">
        </p>
        <p>
          <label for="body">Beschreibung</label>
          <textarea id="body" name="body" required maxlength="100000"></textarea>
        </p>
        <p>
          <label for="project">Projekt <span class="hint">(optional)</span></label>
          <input id="project" name="project" maxlength="120" autocomplete="off">
        </p>
        <p>
          <label for="priority">Priorität</label>
          <input id="priority" name="priority" value="mittel" required maxlength="120" autocomplete="off">
        </p>
        <p>
          <label for="pipeline">Pipeline</label>
          <input id="pipeline" name="pipeline" value="&lt;offen&gt;" required maxlength="120" autocomplete="off">
        </p>
        <p>
          <label for="request_id">Anfrage-ID <span class="hint">(optional; stabilisiert Wiederholungen)</span></label>
          <input id="request_id" name="request_id" maxlength="256" autocomplete="off">
        </p>
      </fieldset>
      <button type="submit">Vorschau anzeigen</button>
    </form>
  </main>
</body>
</html>
'''


def web_preview(payload: Mapping[str, Any]) -> AdapterResponse:
    """Return a JSON preview for the public web endpoint."""
    try:
        return _json_response(200, preview_intake(_public_payload(payload)))
    except IntakeValidationError as exc:
        return _error(422, str(exc))


def web_submit(
    payload: Mapping[str, Any],
    *,
    tickets_dir: Path | str,
    registry_snapshot: Mapping[str, Any],
    resolver: Callable[..., Any] | None = None,
    today: str | None = None,
    rng: Any = None,
    session: str | None = None,
    session_agent: str | None = None,
    session_host: str | None = None,
) -> AdapterResponse:
    """Submit once and return only the public intake receipt."""
    try:
        receipt = submit_intake(
            _public_payload(payload),
            tickets_dir=tickets_dir,
            registry_snapshot=registry_snapshot,
            resolver=resolver,
            today=today,
            rng=rng,
            session=session,
            session_agent=session_agent,
            session_host=session_host,
        )
    except IntakeValidationError as exc:
        return _error(422, str(exc))
    except (OSError, ValueError) as exc:
        # Storage/contract failures are not converted into a success receipt.
        return _error(409, str(exc))
    return _json_response(200, receipt)


def tray_preview(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Return the same public preview object used by a tray client."""
    return preview_intake(_public_payload(payload))


def tray_submit(
    payload: Mapping[str, Any],
    *,
    tickets_dir: Path | str,
    registry_snapshot: Mapping[str, Any],
    resolver: Callable[..., Any] | None = None,
    today: str | None = None,
    rng: Any = None,
    session: str | None = None,
    session_agent: str | None = None,
    session_host: str | None = None,
) -> dict[str, Any]:
    """Return the same receipt object used by a tray client."""
    return submit_intake(
        _public_payload(payload),
        tickets_dir=tickets_dir,
        registry_snapshot=registry_snapshot,
        resolver=resolver,
        today=today,
        rng=rng,
        session=session,
        session_agent=session_agent,
        session_host=session_host,
    )


def web_dispatch(
    method: str,
    path: str,
    payload: Mapping[str, Any] | None = None,
    *,
    tickets_dir: Path | str | None = None,
    registry_snapshot: Mapping[str, Any] | None = None,
    resolver: Callable[..., Any] | None = None,
    today: str | None = None,
    rng: Any = None,
    session: str | None = None,
    session_agent: str | None = None,
    session_host: str | None = None,
) -> AdapterResponse:
    """Dispatch the three inert public web endpoints.

    Embedders own the actual HTTP server.  ``tickets_dir`` and
    ``registry_snapshot`` are required only for ``POST /unicorn/submit``.
    """
    if not isinstance(method, str) or not isinstance(path, str):
        return _error(400, "method and path must be strings")
    method = method.upper()
    if path == FORM_PATH:
        if method != "GET":
            return _error(405, "method not allowed")
        return AdapterResponse(200, _HTML_CONTENT_TYPE, render_form())
    if path == PREVIEW_PATH:
        if method != "POST":
            return _error(405, "method not allowed")
        return web_preview(payload)
    if path == SUBMIT_PATH:
        if method != "POST":
            return _error(405, "method not allowed")
        if tickets_dir is None or registry_snapshot is None:
            return _error(503, "submit configuration is unavailable")
        return web_submit(
            payload,
            tickets_dir=tickets_dir,
            registry_snapshot=registry_snapshot,
            resolver=resolver,
            today=today,
            rng=rng,
            session=session,
            session_agent=session_agent,
            session_host=session_host,
        )
    return _error(404, "unknown Unicorn endpoint")


__all__ = [
    "AdapterResponse",
    "ERROR_SCHEMA",
    "FORM_PATH",
    "PREVIEW_PATH",
    "SUBMIT_PATH",
    "render_form",
    "tray_preview",
    "tray_submit",
    "web_dispatch",
    "web_preview",
    "web_submit",
]
