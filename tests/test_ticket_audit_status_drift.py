"""T-20260830-517795746 Befund 3: STATUS-Feld vs. Lebenszyklus-Ordner --
Drift wird gemeldet, nie repariert."""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

import ticket_audit  # noqa: E402


def _ticket(path: Path, status: str | None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["ID:            T-20260830-000000001", "TITEL:         x"]
    if status is not None:
        lines.append(f"STATUS:        {status}")
    lines.append("PRIORITAET:    mittel")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


class TestStatusDrift(unittest.TestCase):
    def test_clean_bestand_has_no_drift(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            _ticket(base / "ACTIONABLE" / "T-20260830-000000001.txt",
                    "ACTIONABLE (seit 2026-08-30) — Freitext dahinter ist erlaubt")
            _ticket(base / "USER" / "T-20260830-000000002.ASUS-GEI.txt",
                    "USER/decision (seit 2026-08-30)")
            _ticket(base / "T-20260830-000000003.txt", "INBOX")
            _ticket(base / "T-20260830-000000004.txt", "OPEN")  # Legacy-Alias fuer INBOX
            _ticket(base / "PENDING" / "T-20260801-01.txt", "PENDING")
            self.assertEqual(ticket_audit.status_drift(base), [])

    def test_folder_mismatch_unknown_and_missing_are_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            _ticket(base / "WAITING" / "T-20260830-000000001.txt",
                    "QUEUED (in Arbeit durch Session X)")          # Ordner WAITING, STATUS QUEUED
            _ticket(base / "WAITING" / "T-20260830-000000002.txt", "/REVIEW — Fix gepusht")
            _ticket(base / "SOLVED" / "T-20260830-000000003.txt", "GELOEST")
            _ticket(base / "BLOCKED" / "T-20260830-000000004.txt", None)
            findings = ticket_audit.status_drift(base)
            kinds = {Path(f["path"]).name: f["kind"] for f in findings}
            self.assertEqual(kinds, {
                "T-20260830-000000001.txt": "folder-mismatch",
                "T-20260830-000000002.txt": "unknown-status",
                "T-20260830-000000003.txt": "unknown-status",
                "T-20260830-000000004.txt": "missing-status",
            })
            self.assertEqual(findings[0]["folder"], "BLOCKED")  # sortiert nach Pfad

    def test_audit_report_carries_status_drift_and_ignores_non_tickets(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            _ticket(base / "QUEUED" / "T-20260830-000000001.txt", "ACTIONABLE (seit 2026-08-30)")
            (base / "QUEUED" / "notizen.md").write_text("STATUS: SOLVED\n", encoding="utf-8")
            report = ticket_audit.audit(base)
            self.assertEqual(len(report["status_drift"]), 1)
            self.assertEqual(report["status_drift"][0]["kind"], "folder-mismatch")


if __name__ == "__main__":
    unittest.main()


class TestLegacyMarkdownStatus(unittest.TestCase):
    """T-20260901-916096823: '**Status:**'-Legacy-Feld gilt als STATUS-Zeile.

    T-20260902-792359826: folder-kongruente Legacy-Dateien fielen dadurch
    komplett aus dem Audit (weder STATUS-DRIFT noch NON-TICKET-FILES) --
    genau die faile-silent-Luecke, die dieses Ticket fand. Sie muessen als
    eigener Fund ('legacy-header') erscheinen, ohne erneut als
    missing-status/folder-mismatch falsch zu alarmieren.
    """

    def test_legacy_markdown_status_matching_folder_is_reported_as_legacy_header(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            p = base / "BLOCKED" / "T-20260802-01.WORKSTATION-LG.txt"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(
                "# T-20260802-01.WORKSTATION-LG\n\n"
                "**Status:** BLOCKED/foreign-state (seit 2026-08-08) - Details\n"
                "**Typ:** Automation\n",
                encoding="utf-8",
            )
            findings = ticket_audit.status_drift(base)
            self.assertEqual(len(findings), 1)
            self.assertEqual(findings[0]["kind"], "legacy-header")
            self.assertEqual(findings[0]["folder"], "BLOCKED")

    def test_legacy_markdown_status_mismatch_is_still_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            p = base / "USER" / "T-20260802-02.txt"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("**Status:** QUEUED (seit 2026-08-08)\n", encoding="utf-8")
            findings = ticket_audit.status_drift(base)
            self.assertEqual(len(findings), 1)
            self.assertEqual(findings[0]["kind"], "folder-mismatch")
