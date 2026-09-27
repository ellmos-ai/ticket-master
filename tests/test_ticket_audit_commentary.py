# -*- coding: utf-8 -*-
"""Verifikation der datierten, zugeschriebenen VERLAUF-Belege."""
import sys
import tempfile
import unittest
from pathlib import Path

LIB_DIR = Path(__file__).resolve().parent.parent / "lib"
sys.path.insert(0, str(LIB_DIR))

import ticket_audit  # noqa: E402


def _write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


_HEADER = (
    "ID:            T-20260920-100000001\n"
    "TITEL:         Kommentarprüfung\n"
    "ERSTELLT:      2026-09-20\n"
    "STATUS:        ACTIONABLE\n"
    "\nVERLAUF / LOG\n"
    "--------------------------------------------------------------\n"
)


class TestCommentaryLint(unittest.TestCase):
    def test_canonical_line_is_clean(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            _write(
                base / "ACTIONABLE" / "T-20260920-100000001.txt",
                _HEADER
                + "2026-09-20 | ticket-master@WORKSTATION-LG | Status geprüft — "
                  "Beleg: git status und Readback gemessen\n"
                + "\nLOESUNG / ERGEBNIS\n",
            )
            self.assertEqual(ticket_audit.commentary_lint(base), [])
            self.assertEqual(ticket_audit.lint(base), [])

    def test_missing_date_and_actor_are_reported_without_rewriting(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            original = _HEADER + "Aufgenommen.\n\nLOESUNG / ERGEBNIS\n"
            path = _write(base / "ACTIONABLE" / "T-20260920-100000001.txt", original)
            findings = ticket_audit.commentary_lint(base)
            kinds = {finding["kind"] for finding in findings}
            self.assertIn("commentary-missing-date", kinds)
            self.assertIn("commentary-missing-actor", kinds)
            self.assertEqual(path.read_text(encoding="utf-8"), original)

    def test_state_without_evidence_is_reported_and_readback_clears_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            path = base / "ACTIONABLE" / "T-20260920-100000001.txt"
            _write(
                path,
                _HEADER
                + "2026-09-20 | ticket-master@WORKSTATION-LG | BACH-Sperre bleibt "
                  "aktiv\n"
                + "\nLOESUNG / ERGEBNIS\n",
            )
            findings = ticket_audit.commentary_lint(base)
            self.assertTrue(any(
                finding["kind"] == "commentary-state-without-evidence"
                for finding in findings
            ))

            path.write_text(
                _HEADER
                + "2026-09-20 | ticket-master@WORKSTATION-LG | BACH-Sperre ist frei "
                  "— Beleg: Lock-Status gemessen und Readback bestätigt\n"
                + "\nLOESUNG / ERGEBNIS\n",
                encoding="utf-8",
            )
            self.assertEqual(ticket_audit.commentary_lint(base), [])

    def test_audit_exposes_commentary_findings_as_warnings(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            _write(
                base / "ACTIONABLE" / "T-20260920-100000001.txt",
                _HEADER + "2026-09-20  Hold bleibt.\n",
            )
            report = ticket_audit.audit(base)
            self.assertTrue(report["commentary_lint"])
            self.assertEqual(report["collisions"], {})


if __name__ == "__main__":
    unittest.main()
