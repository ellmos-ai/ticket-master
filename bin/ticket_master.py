#!/usr/bin/env python3
"""Cross-platform ticket-master launcher and auditable ticket CLI.

The shell starters delegate to this module so prompt resolution, ``--list`` and
``--intake`` have one implementation on Windows, macOS, and Linux.  The module
uses only the Python standard library and never invokes a shell for provider
commands or ticket files.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterable


REPO_ROOT = Path(__file__).resolve().parents[1]
LIB_ROOT = REPO_ROOT / "lib"
if str(LIB_ROOT) not in sys.path:
    sys.path.insert(0, str(LIB_ROOT))

from ticket_writer import create as create_ticket  # noqa: E402
from routing_contract import RoutingContractError, load_contract, parse_ticket_name  # noqa: E402


class ConfigError(ValueError):
    """Raised when a local configuration cannot be used safely."""


_LANG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$")
_TICKET_NAME_RE = re.compile(
    r"^T-(\d{8})-(\d+)(?:_[A-Za-z0-9_-]+)?"
    r"(?:\.[A-Za-z0-9_-]+)?\.txt$"
)
_ID_LINE_RE = re.compile(r"^ID:\s*(T-\d{8}-\d+)", re.IGNORECASE)
_TITLE_LINE_RE = re.compile(r"^(?:TITEL|TITLE):\s*(.*)$", re.IGNORECASE)
_STATUS_LINE_RE = re.compile(r"^STATUS:\s*(.*)$", re.IGNORECASE)
_SEPARATOR = "=============================================================="

# Root/INBOX is the canonical alias for new intake. PENDING and .USER remain
# readable legacy aliases. SOLVED is deliberately excluded from --list.
OPEN_LIFECYCLE_DIRS = (
    "",
    "INBOX",
    "ACTIONABLE",
    "QUEUED",
    "BLOCKED",
    "WAITING",
    "USER",
    "PARKED",
    "PENDING",
    ".USER",
)


def _config_path(config: str | Path | None) -> tuple[Path, bool]:
    """Return the resolved config path and whether it was explicitly chosen."""

    raw = config if config is not None else os.environ.get("TM_CONFIG")
    explicit = raw is not None
    if raw is None:
        return REPO_ROOT / "config" / "ticket-master.config.json", False
    path = Path(str(raw)).expanduser()
    if not path.is_absolute():
        path = REPO_ROOT / path
    return path.resolve(), explicit


def load_config(config: str | Path | None = None) -> dict[str, Any]:
    """Load local config, using safe built-in defaults when it is absent.

    The real config is intentionally gitignored. A missing default config is
    therefore normal in a fresh clone; an explicitly requested missing config
    is an actionable, controlled error.
    """

    path, explicit = _config_path(config)
    if not path.is_file():
        if explicit:
            raise ConfigError(f"config file not found: {path}")
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ConfigError(f"cannot read config {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ConfigError(f"config root must be a JSON object: {path}")
    return value


def _expand_placeholders(value: str) -> str:
    return (
        value.replace("<HOME>", str(Path.home()))
        .replace("<USER>", getpass.getuser())
    )


def _resolve_path(raw: Any, *, root: Path, field: str, allow_outside: bool) -> Path:
    if isinstance(raw, os.PathLike):
        raw = os.fspath(raw)
    if not isinstance(raw, str) or not raw.strip():
        raise ConfigError(f"config field {field!r} must be a non-empty string")
    value = _expand_placeholders(raw.strip())
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = root / path
    path = path.resolve()
    if not allow_outside:
        try:
            path.relative_to(root.resolve())
        except ValueError as exc:
            raise ConfigError(
                f"config field {field!r} must stay within the repository root"
            ) from exc
    return path


def resolve_tickets_dir(
    config: str | Path | None = None,
    override: str | Path | None = None,
) -> Path:
    cfg = load_config(config)
    raw = override if override is not None else cfg.get("tickets_dir", "tickets")
    return _resolve_path(raw, root=REPO_ROOT, field="tickets_dir", allow_outside=True)


def _normalise_language(raw: Any, *, source: str) -> tuple[str, str | None]:
    value = str(raw or "en").strip()
    if _LANG_RE.fullmatch(value):
        return value, None
    return "en", f"WARNING: invalid language {value!r} from {source}; falling back to 'en'."


def resolve_prompt(
    config: str | Path | None = None,
    language: str | None = None,
) -> tuple[str, Path, list[str]]:
    """Resolve a prompt from configured ``prompts_dir`` with safe fallbacks.

    ``prompts_dir`` is deliberately bounded to the repository root so a public
    starter cannot be redirected through ``..`` or an unsafe system path. A
    user may still place custom prompt files in any repository-local directory.
    """

    cfg = load_config(config)
    raw_dir = cfg.get("prompts_dir", "prompts")
    prompt_dir = _resolve_path(
        raw_dir, root=REPO_ROOT, field="prompts_dir", allow_outside=False
    )
    requested = language if language is not None else os.environ.get("TM_LANG")
    if requested is None:
        requested = cfg.get("default_language", "en")
        source = "default_language"
    else:
        source = "TM_LANG"
    lang, warning = _normalise_language(requested, source=source)
    warnings = [warning] if warning else []
    candidate = (prompt_dir / f"TICKET-MASTER.{lang}.md").resolve()
    try:
        candidate.relative_to(prompt_dir)
    except ValueError as exc:  # defensive; language validation should prevent it
        raise ConfigError("resolved prompt path escaped prompts_dir") from exc
    if not candidate.is_file():
        warnings.append(
            f"WARNING: prompt file for language {lang!r} not found; falling back to 'en'."
        )
        lang = "en"
        candidate = (prompt_dir / "TICKET-MASTER.en.md").resolve()
    if not candidate.is_file():
        raise ConfigError(f"prompt file not found: {candidate}")
    return lang, candidate, warnings


def _ticket_id(path: Path, text: str) -> str:
    for line in text.splitlines():
        match = _ID_LINE_RE.match(line.strip())
        if match:
            return match.group(1)
    match = _TICKET_NAME_RE.match(path.name)
    if match:
        return f"T-{match.group(1)}-{int(match.group(2)):02d}"
    try:
        parsed = parse_ticket_name(path.name)
        return f"T-{parsed.date}-{parsed.number}"
    except RoutingContractError:
        pass
    return path.stem


def _line_value(pattern: re.Pattern[str], text: str) -> str:
    for line in text.splitlines():
        match = pattern.match(line.strip())
        if match:
            return match.group(1).strip()
    return ""


def _safe_single_line(value: str, fallback: str) -> str:
    value = re.sub(r"\s+", " ", value).replace("\t", " ").strip()
    return value[:200] or fallback


def _iter_ticket_files(base: Path) -> Iterable[tuple[Path, str]]:
    for subdir in OPEN_LIFECYCLE_DIRS:
        directory = base / subdir if subdir else base
        if not directory.is_dir():
            continue
        for path in sorted(directory.iterdir(), key=lambda item: item.name):
            if not path.is_file():
                continue
            if _TICKET_NAME_RE.match(path.name):
                yield path, subdir or "INBOX"
                continue
            try:
                parse_ticket_name(path.name)
                yield path, subdir or "INBOX"
            except RoutingContractError:
                continue


def list_open_tickets(tickets_dir: Path | str) -> list[dict[str, str]]:
    """Return deterministic, non-secret metadata for every non-SOLVED ticket."""

    base = Path(tickets_dir).expanduser().resolve()
    rows: list[dict[str, str]] = []
    for path, folder_status in _iter_ticket_files(base):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        raw_status = _safe_single_line(_line_value(_STATUS_LINE_RE, text), folder_status)
        title = _safe_single_line(_line_value(_TITLE_LINE_RE, text), "<ohne Titel>")
        try:
            relative = path.relative_to(base).as_posix()
        except ValueError:
            relative = path.name
        row = {
            "status": raw_status,
            "id": _ticket_id(path, text),
            "title": title,
            "path": relative,
        }
        rows.append(row)
        if "ROUTING_SCHEMA: 2" in text:
            try:
                contract = load_contract(path)
                compact_ledger = ",".join(
                    f"{item.get('system')}={item.get('status')}" for item in contract.ledger
                )
                row.update(
                    primary=contract.fields.get("PRIMARY_TICKET", ""),
                    owner=contract.fields.get("ORIGINAL_OWNER", ""),
                    target=f"{contract.target_kind}:{','.join(contract.target_systems)}",
                    ledger=compact_ledger,
                )
            except (OSError, RoutingContractError):
                row.update(primary="<invalid>", owner="<invalid>", target="<invalid>", ledger="<invalid>")
    return sorted(rows, key=lambda row: (row["status"], row["id"], row["path"]))


def print_open_tickets(rows: list[dict[str, str]], *, as_json: bool = False) -> None:
    if as_json:
        print(json.dumps({"count": len(rows), "tickets": rows}, ensure_ascii=False, indent=2))
        return
    print("STATUS\tID\tTITLE\tPATH\tPRIMARY\tOWNER\tTARGET\tSYSTEM_LEDGER")
    for row in rows:
        print("\t".join(row.get(key, "") for key in (
            "status", "id", "title", "path", "primary", "owner", "target", "ledger"
        )))
    print(f"TOTAL\t{len(rows)}")


def _safe_intake_body(description: str) -> str:
    if "\x00" in description:
        raise ValueError("intake description contains NUL")
    body = description.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not body:
        raise ValueError("intake description must not be empty")
    # Keep multiline input useful while preventing a caller from injecting the
    # ticket template's structural separator into the body.
    body = body.replace(_SEPARATOR, "[ticket separator escaped]")
    return body


def intake_ticket(
    description: str,
    *,
    tickets_dir: Path | str,
    title: str | None = None,
    project: str | None = None,
    priority: str = "mittel",
    pipeline: str = "<offen>",
    today: str | None = None,
) -> Path:
    body = _safe_intake_body(description)
    if title is None or not title.strip():
        title = body.splitlines()[0][:120]
    title = _safe_single_line(title, "Command-line intake")
    for field_name, field_value in (
        ("project", project),
        ("priority", priority),
        ("pipeline", pipeline),
    ):
        if field_value is not None and "\x00" in str(field_value):
            raise ValueError(f"intake {field_name} contains NUL")
    project = _safe_single_line(str(project), "") if project is not None else None
    priority = _safe_single_line(str(priority), "mittel")
    pipeline = _safe_single_line(str(pipeline), "<offen>")
    path = create_ticket(
        title,
        body,
        project=project,
        priority=priority,
        pipeline=pipeline,
        tickets_dir=Path(tickets_dir),
        today=today,
    )
    return Path(path)


def _provider_command(cfg: dict[str, Any], provider: str) -> list[str]:
    providers = cfg.get("providers", {})
    configured = providers.get(provider, {}) if isinstance(providers, dict) else {}
    command = configured.get("command", provider) if isinstance(configured, dict) else provider
    if not isinstance(command, str) or not command.strip():
        raise ConfigError(f"provider command for {provider!r} is invalid")
    return shlex.split(command, posix=os.name != "nt")


def launch_provider(
    provider: str | None = None,
    *,
    config: str | Path | None = None,
    language: str | None = None,
    skip_permissions: bool = False,
) -> int:
    cfg = load_config(config)
    selected = provider or os.environ.get("TM_PROVIDER") or cfg.get("default_provider") or "claude"
    if selected not in {"claude", "codex", "agy"}:
        raise ConfigError(f"unknown provider {selected!r}; use claude, codex, or agy")
    lang, prompt_file, warnings = resolve_prompt(config=config, language=language)
    for warning in warnings:
        print(warning, file=sys.stderr)
    command = _provider_command(cfg, selected)
    if not shutil.which(command[0]):
        raise ConfigError(f"{command[0]!r} not found in PATH")
    bootstrap = (
        f"Read and follow the instructions in {prompt_file} - start at step (a) "
        "and work down to Position 0."
    )
    if selected == "claude" and (skip_permissions or os.environ.get("TM_SKIP_PERMISSIONS") == "1"):
        command.append("--dangerously-skip-permissions")
    command.append(bootstrap)
    print(f"[ticket-master] Starting provider: {selected}")
    print(f"[ticket-master] Language: {lang}")
    print(f"[ticket-master] Repo root: {REPO_ROOT}")
    return subprocess.run(command, cwd=REPO_ROOT, check=False).returncode


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ticket_master",
        description="Cross-platform ticket-master launcher and auditable ticket CLI.",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--list", action="store_true", help="list open ticket metadata")
    mode.add_argument("--intake", metavar="DESCRIPTION", help="create one exclusive INBOX intake ticket")
    mode.add_argument("--print-prompt", action="store_true", help="resolve and print the prompt path")
    parser.add_argument("--json", action="store_true", help="emit --list output as JSON")
    parser.add_argument("--tickets-dir", help="override configured ticket directory")
    parser.add_argument("--config", help="path to local JSON config")
    parser.add_argument("--title", help="optional intake title (otherwise first description line)")
    parser.add_argument("--project", default=None, help="optional intake project")
    parser.add_argument("--priority", default="mittel", help="optional intake priority")
    parser.add_argument("--pipeline", default="<offen>", help="optional intake pipeline")
    parser.add_argument("--provider", choices=("claude", "codex", "agy"), help="provider to launch")
    parser.add_argument("--lang", help="prompt language (overrides TM_LANG/config default)")
    parser.add_argument("--skip-permissions", action="store_true", help="pass Claude skip-permissions flag")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.json and not args.list:
            raise ConfigError("--json is only valid with --list")
        if args.print_prompt:
            _lang, path, warnings = resolve_prompt(config=args.config, language=args.lang)
            for warning in warnings:
                print(warning, file=sys.stderr)
            print(path)
            return 0
        if args.list:
            rows = list_open_tickets(resolve_tickets_dir(args.config, args.tickets_dir))
            print_open_tickets(rows, as_json=args.json)
            return 0
        if args.intake is not None:
            path = intake_ticket(
                args.intake,
                tickets_dir=resolve_tickets_dir(args.config, args.tickets_dir),
                title=args.title,
                project=args.project,
                priority=args.priority,
                pipeline=args.pipeline,
            )
            print(f"CREATED: {path}")
            return 0
        return launch_provider(
            args.provider,
            config=args.config,
            language=args.lang,
            skip_permissions=args.skip_permissions,
        )
    except (ConfigError, ValueError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
