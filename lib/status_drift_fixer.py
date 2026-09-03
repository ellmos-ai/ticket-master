r"""
status_drift_fixer.py — repairs the SAFE subset of ticket_audit.status_drift()
findings (T-20260903-778818739): a ticket sitting in SOLVED whose STATUS line
still names an earlier lifecycle stage, but whose LOESUNG/ERGEBNIS section is
filled and whose VERLAUF/LOG carries a real closing entry beyond the
ticket_writer bootstrap line -- i.e. the move happened, only the STATUS line
was never pulled forward (Fall a).

Deliberately narrow, everything else is left for individual review (Fall b,
or genuinely ambiguous):
  - any finding outside the SOLVED folder
  - any STATUS naming a USER or WAITING cluster, even in SOLVED -- a filled
    LOESUNG doesn't prove the user was really asked or the review really
    happened; blindly pulling STATUS forward there would hide exactly the
    misfilings this audit exists to surface
  - a SOLVED-folder ticket whose LOESUNG/VERLAUF aren't actually filled

Rewrites only the STATUS line's value, byte-identical otherwise. --dry-run
by default (see _cli).
"""
from __future__ import annotations

import re
from datetime import date
from pathlib import Path

try:  # package import
    from .ticket_audit import status_drift, _STATUS_LINE_RE
    from .ticket_mover import suggested_status_line
except ImportError:  # direct import from lib on sys.path
    from ticket_audit import status_drift, _STATUS_LINE_RE
    from ticket_mover import suggested_status_line


# Header lines sometimes carry trailing free text on the same line (e.g.
# "LOESUNG / ERGEBNIS  (Schwarm 3x Sonnet, ...)") -- match up to end of that
# line, not just immediate whitespace, so those aren't missed as unfilled.
_LOESUNG_RE = re.compile(r"LOESUNG / ERGEBNIS[^\n]*\n-+\n(?P<body>.*?)\n=+", re.DOTALL)
_VERLAUF_RE = re.compile(
    r"VERLAUF / LOG[^\n]*\n-+\n(?P<body>.*?)\n-+\nLOESUNG", re.DOTALL,
)
_PLACEHOLDER_LOESUNG = "Vor Verschieben nach SOLVED ausf"  # umlaut-safe prefix
_BOOTSTRAP_VERLAUF_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}\s+Aufgenommen \(asynchron via Lock-Watcher-GUI */ *ticket_writer\)\.\s*$",
    re.MULTILINE,
)
# Judgment clusters that always need a human, even with LOESUNG+VERLAUF
# filled: see module docstring.
_JUDGMENT_CLUSTERS = {"USER", "WAITING"}
# WONT-FIX is its own closure type, not stale vocabulary for "done"
# (T-20260903-778818739 review): "we decided not to do this" is a different
# statement than "solved", and this exact case is a privacy decision
# (foerderplaner) independently flagged as worth protecting -- leave it for
# a human, don't fold it into SOLVED.
_NEVER_FIXABLE_PREFIXES = ("WONT-FIX", "WONTFIX")


def _is_filled_loesung(text: str) -> bool:
    m = _LOESUNG_RE.search(text)
    if not m:
        return False
    body = m.group("body").strip()
    return bool(body) and _PLACEHOLDER_LOESUNG not in body


def _has_closing_verlauf_entry(text: str) -> bool:
    m = _VERLAUF_RE.search(text)
    if not m:
        return False
    body = _BOOTSTRAP_VERLAUF_RE.sub("", m.group("body")).strip()
    return bool(body)


def classify(base: Path | str) -> dict:
    """Splits status_drift() findings into fixable / needs_review.

    "needs_review" also carries findings this fixer isn't scoped for at all
    (kind not in folder-mismatch/unknown-status) so a caller can see the
    full audit picture, not just its own slice.
    """
    fixable, needs_review = [], []
    for finding in status_drift(base):
        if finding["kind"] not in ("folder-mismatch", "unknown-status"):
            needs_review.append(finding)
            continue
        if finding["folder"] != "SOLVED":
            needs_review.append(finding)
            continue
        status = finding["status"] or ""
        cluster_match = re.match(r"^(\.USER|[A-Za-z]+)", status)
        cluster = cluster_match.group(1).upper() if cluster_match else ""
        if cluster in _JUDGMENT_CLUSTERS or status.upper().startswith(_NEVER_FIXABLE_PREFIXES):
            needs_review.append(finding)
            continue
        text = Path(finding["path"]).read_text(encoding="utf-8", errors="replace")
        if _is_filled_loesung(text) and _has_closing_verlauf_entry(text):
            fixable.append(finding)
        else:
            needs_review.append(finding)
    return {"fixable": fixable, "needs_review": needs_review}


_LEADING_TOKEN_RE = re.compile(r"^\S+")


def _legacy_fallback_status_line(value: str, dest_cluster: str) -> str:
    """Fallback for values with no parsable cluster token at all
    (done/GELOEST/ERLEDIGT/...): strips only the leading token, keeps
    everything after it as free text (T-20260903-778818739 review -- the
    previous fallback discarded it wholesale, losing e.g. a follow-up
    ticket reference)."""
    remainder = _LEADING_TOKEN_RE.sub("", value, count=1)
    return f"{dest_cluster} (seit {date.today().isoformat()}){remainder}"


def _compute_new_status(text: str, dest_cluster: str) -> tuple[str | None, str | None]:
    """Pure STATUS-line computation, shared by apply_fix (writes) and
    preview_fix (dry-run, never writes) so they can't drift apart."""
    match = _STATUS_LINE_RE.search(text)
    if match is None:
        return None, "no STATUS line found (file changed since scan?)"
    value = match.group("value").strip()
    new_value = suggested_status_line(value, dest_cluster)
    if new_value is None:
        new_value = _legacy_fallback_status_line(value, dest_cluster)
    return new_value, None


def preview_fix(finding: dict) -> tuple[str | None, str | None]:
    """Like apply_fix, but never writes -- what --dry-run reports."""
    path = finding.get("path")
    if not path:
        return None, "finding has no path"
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return None, f"read failed: {exc}"
    return _compute_new_status(text, finding["folder"])


def apply_fix(finding: dict) -> tuple[str | None, str | None]:
    """Rewrites the STATUS line in place. Returns (new_value, error) -- one
    of the two is always None.

    Never raises: a rewrite over ~100 OneDrive ticket files must not abort
    on the first file whose STATUS line changed or vanished between scan
    and apply -- that belongs in the report as a skip, not a traceback
    that leaves the run half-done (T-20260903-778818739 review).
    """
    path = finding.get("path")
    if not path:
        return None, "finding has no path"
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return None, f"read failed: {exc}"
    new_value, error = _compute_new_status(text, finding["folder"])
    if error:
        return None, error
    match = _STATUS_LINE_RE.search(text)
    new_text = text[: match.start("value")] + new_value + text[match.end("value") :]
    try:
        path.write_text(new_text, encoding="utf-8", newline="")
    except OSError as exc:
        return None, f"write failed: {exc}"
    return new_value, None


def run(base: Path | str, dry_run: bool = True) -> dict:
    report = classify(base)
    applied, preview, errors = [], [], []
    for finding in report["fixable"]:
        new_value, error = (preview_fix if dry_run else apply_fix)(finding)
        if error:
            errors.append({**finding, "error": error})
            continue
        entry = {**finding, "new_status": new_value}
        (preview if dry_run else applied).append(entry)
    report["applied"] = applied  # what was actually written -- always [] on dry-run
    report["preview"] = preview  # what WOULD be written -- only populated on dry-run
    report["errors"] = errors
    report["dry_run"] = dry_run
    return report


def _print_human(report: dict) -> None:
    entries = report["preview"] if report["dry_run"] else report["applied"]
    verb = "WOULD FIX" if report["dry_run"] else "FIXED"
    print(f"{verb} ({len(entries)}):")
    for f in entries:
        print(f"  {f['path']}: {f['status']!r} -> {f['new_status']!r}")
    print(f"NEEDS REVIEW ({len(report['needs_review'])}):")
    for f in report["needs_review"]:
        print(f"  [{f['kind']}] {f['folder']}: {f['path']} (STATUS={f['status']!r})")
    if report["errors"]:
        print(f"ERRORS/SKIPPED ({len(report['errors'])}):")
        for f in report["errors"]:
            print(f"  {f['path']}: {f['error']}")


def _cli(argv: list[str] | None = None) -> int:
    import argparse
    import json
    import os

    parser = argparse.ArgumentParser(
        prog="status_drift_fixer",
        description="Pulls STATUS forward for SOLVED tickets with filled LOESUNG+VERLAUF only.",
    )
    default_dir = os.environ.get("TICKET_MASTER_TICKETS_DIR")
    parser.add_argument(
        "tickets_dir", nargs="?" if default_dir else None, default=default_dir,
        help="Ticket bestand root (default: $TICKET_MASTER_TICKETS_DIR).",
    )
    parser.add_argument("--apply", action="store_true", help="write changes (default: dry-run)")
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args(argv)
    if not args.tickets_dir:
        parser.error("tickets_dir required (pass it or set TICKET_MASTER_TICKETS_DIR).")

    report = run(args.tickets_dir, dry_run=not args.apply)
    if args.as_json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        _print_human(report)
    return 0


if __name__ == "__main__":
    import sys

    raise SystemExit(_cli(sys.argv[1:]))
