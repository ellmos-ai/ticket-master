# -*- coding: utf-8 -*-
"""Tests for lib/ticket_note.py.

Ticket T-20260906-543388409: Tests verifying atomic STATUS updates, VERLAUF/LOG
appending before the LOESUNG block, CRLF/LF and UTF-8 preservation, dry-run
behavior, and CLI ergonomics.
"""

import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import date
from io import StringIO
from pathlib import Path

LIB_DIR = Path(__file__).resolve().parent.parent / "lib"
sys.path.insert(0, str(LIB_DIR))

import ticket_note  # noqa: E402
from routing_contract import StaleContentError, content_hash  # noqa: E402

SAMPLE_TICKET_LF = """==============================================================
TICKET
==============================================================
ID:            T-20260906-123456789
TITEL:         Sample ticket
ERSTELLT:      2026-09-06
STATUS:        BLOCKED/dependency (seit 2026-09-06) — Blocker T-20260902-111111111
PRIORITAET:    mittel

--------------------------------------------------------------
VERLAUF / LOG
--------------------------------------------------------------
2026-09-06  Aufgenommen (asynchron via Lock-Watcher-GUI / ticket_writer).
2026-09-06  Triage: BLOCKED/dependency

--------------------------------------------------------------
LOESUNG / ERGEBNIS
--------------------------------------------------------------
<Vor Verschieben nach SOLVED ausfuellen.>
==============================================================
"""

SAMPLE_TICKET_CRLF = SAMPLE_TICKET_LF.replace("\n", "\r\n")


class TestTicketNote(unittest.TestCase):
    def test_update_status_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "T-test.txt"
            p.write_text(SAMPLE_TICKET_LF, encoding="utf-8")

            res = ticket_note.update_ticket_note(
                p,
                status="ACTIONABLE (seit 2026-09-17) — Blocker geloest",
            )
            self.assertTrue(res["status_updated"])
            self.assertFalse(res["verlauf_appended"])
            self.assertEqual(res["new_status"], "ACTIONABLE (seit 2026-09-17) — Blocker geloest")

            content = p.read_text(encoding="utf-8")
            self.assertIn("STATUS:        ACTIONABLE (seit 2026-09-17) — Blocker geloest", content)
            self.assertNotIn("STATUS:        BLOCKED/dependency", content)
            # VERLAUF section should not be altered
            self.assertIn("2026-09-06  Triage: BLOCKED/dependency\n\n--------------------------------------------------------------\nLOESUNG", content)

    def test_append_verlauf_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "T-test.txt"
            p.write_text(SAMPLE_TICKET_LF, encoding="utf-8")

            res = ticket_note.update_ticket_note(
                p,
                verlauf="TICKET-MASTER: Reaktivierung nach ACTIONABLE.",
                entry_date="2026-09-17",
            )
            self.assertFalse(res["status_updated"])
            self.assertTrue(res["verlauf_appended"])

            content = p.read_text(encoding="utf-8")
            expected_entry = "2026-09-17  TICKET-MASTER: Reaktivierung nach ACTIONABLE."
            self.assertIn(expected_entry, content)
            # Entry must be placed BEFORE LOESUNG delimiter
            pos_entry = content.index(expected_entry)
            pos_loesung = content.index("LOESUNG / ERGEBNIS")
            self.assertLess(pos_entry, pos_loesung)

    def test_verlauf_with_explicit_date_in_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "T-test.txt"
            p.write_text(SAMPLE_TICKET_LF, encoding="utf-8")

            # Entry already has a date prefix
            res = ticket_note.update_ticket_note(
                p,
                verlauf="2026-09-15  Manual verification passed.",
            )
            self.assertTrue(res["verlauf_appended"])
            content = p.read_text(encoding="utf-8")
            # Should not duplicate the date
            self.assertIn("2026-09-15  Manual verification passed.", content)
            self.assertNotIn(f"{date.today().isoformat()}  2026-09-15", content)

    def test_update_status_and_verlauf_together(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "T-test.txt"
            p.write_text(SAMPLE_TICKET_LF, encoding="utf-8")

            res = ticket_note.update_ticket_note(
                p,
                status="ACTIONABLE (seit 2026-09-17)",
                verlauf="Entblockt wegen Beseitigung der Abhaengigkeit.",
                entry_date="2026-09-17",
            )
            self.assertTrue(res["status_updated"])
            self.assertTrue(res["verlauf_appended"])

            content = p.read_text(encoding="utf-8")
            self.assertIn("STATUS:        ACTIONABLE (seit 2026-09-17)", content)
            self.assertIn("2026-09-17  Entblockt wegen Beseitigung der Abhaengigkeit.", content)

    def test_preserves_crlf_line_endings(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "T-test.txt"
            p.write_bytes(SAMPLE_TICKET_CRLF.encode("utf-8"))

            ticket_note.update_ticket_note(
                p,
                status="ACTIONABLE (seit 2026-09-17)",
                verlauf="CRLF preservation check.",
                entry_date="2026-09-17",
            )

            raw = p.read_bytes()
            self.assertIn(b"\r\n", raw)
            # Ensure no lone LF exists
            normalized = raw.replace(b"\r\n", b"")
            self.assertNotIn(b"\n", normalized)

    def test_preserves_lf_line_endings(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "T-test.txt"
            p.write_bytes(SAMPLE_TICKET_LF.encode("utf-8"))

            ticket_note.update_ticket_note(
                p,
                status="ACTIONABLE (seit 2026-09-17)",
                verlauf="LF preservation check.",
            )

            raw = p.read_bytes()
            self.assertNotIn(b"\r\n", raw)
            self.assertIn(b"\n", raw)

    def test_preserves_utf8_and_umlauts(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "T-test.txt"
            ticket_text = SAMPLE_TICKET_LF.replace("Sample ticket", "Änderungswunsch Überprüfung Straße")
            p.write_text(ticket_text, encoding="utf-8")

            ticket_note.update_ticket_note(
                p,
                status="ACTIONABLE (seit 2026-09-17) — Prüfung nötig",
                verlauf="Rücksprache mit Entwicklerteam — Freigabe erteilt.",
                entry_date="2026-09-17",
            )

            readback = p.read_text(encoding="utf-8")
            self.assertIn("Änderungswunsch Überprüfung Straße", readback)
            self.assertIn("Prüfung nötig", readback)
            self.assertIn("Rücksprache mit Entwicklerteam — Freigabe erteilt.", readback)

    def test_dry_run_does_not_mutate(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "T-test.txt"
            original_bytes = SAMPLE_TICKET_LF.encode("utf-8")
            p.write_bytes(original_bytes)

            res = ticket_note.update_ticket_note(
                p,
                status="SOLVED",
                verlauf="Closing note.",
                dry_run=True,
            )
            self.assertTrue(res["dry_run"])
            self.assertEqual(p.read_bytes(), original_bytes)

    def test_stale_content_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "T-test.txt"
            p.write_text(SAMPLE_TICKET_LF, encoding="utf-8")

            good_hash = content_hash(SAMPLE_TICKET_LF)
            bad_hash = "sha256:0000000000000000000000000000000000000000000000000000000000000000"

            # Should succeed with correct expected_hash
            ticket_note.update_ticket_note(
                p, status="ACTIONABLE", expected_hash=good_hash
            )

            # Should fail closed with stale hash
            with self.assertRaises(StaleContentError):
                ticket_note.update_ticket_note(
                    p, status="SOLVED", expected_hash=bad_hash
                )

    def test_fallback_when_no_loesung_block(self):
        ticket_without_loesung = """==============================================================
TICKET
==============================================================
ID:            T-20260906-999999999
STATUS:        ACTIONABLE
--------------------------------------------------------------
VERLAUF / LOG
--------------------------------------------------------------
2026-09-06  Init.
==============================================================
"""
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "T-test.txt"
            p.write_text(ticket_without_loesung, encoding="utf-8")

            ticket_note.update_ticket_note(
                p, verlauf="Fallback note.", entry_date="2026-09-17"
            )
            content = p.read_text(encoding="utf-8")
            self.assertIn("2026-09-17  Fallback note.", content)
            pos_entry = content.index("2026-09-17  Fallback note.")
            pos_end = content.rindex("==============================================================")
            self.assertLess(pos_entry, pos_end)

    def test_cli_dry_run_and_execution(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "T-test.txt"
            p.write_text(SAMPLE_TICKET_LF, encoding="utf-8")

            # 1. CLI dry-run
            out = StringIO()
            with redirect_stdout(out):
                code = ticket_note._cli([
                    str(p),
                    "--status", "QUEUED (seit 2026-09-17)",
                    "--verlauf", "Delegiert an Agent.",
                    "--date", "2026-09-17",
                    "--dry-run",
                ])
            self.assertEqual(code, 0)
            self.assertIn("WOULD UPDATE", out.getvalue())
            self.assertIn("QUEUED (seit 2026-09-17)", out.getvalue())
            self.assertIn("2026-09-17  Delegiert an Agent.", out.getvalue())

            # File should not be modified yet
            self.assertIn("BLOCKED/dependency", p.read_text(encoding="utf-8"))

            # 2. CLI actual run with JSON output
            out = StringIO()
            with redirect_stdout(out):
                code = ticket_note._cli([
                    str(p),
                    "--status", "QUEUED (seit 2026-09-17)",
                    "--verlauf", "Delegiert an Agent.",
                    "--date", "2026-09-17",
                    "--json",
                ])
            self.assertEqual(code, 0)
            data = json.loads(out.getvalue())
            self.assertTrue(data["status_updated"])
            self.assertTrue(data["verlauf_appended"])

            # Verify on disk
            final_content = p.read_text(encoding="utf-8")
            self.assertIn("STATUS:        QUEUED (seit 2026-09-17)", final_content)
            self.assertIn("2026-09-17  Delegiert an Agent.", final_content)

    def test_cli_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "T-test.txt"
            p.write_text(SAMPLE_TICKET_LF, encoding="utf-8")

            # Missing arguments
            err = StringIO()
            with redirect_stderr(err), self.assertRaises(SystemExit):
                ticket_note._cli([str(p)])

            # Non-existent file
            err = StringIO()
            with redirect_stderr(err):
                code = ticket_note._cli([str(Path(tmp) / "missing.txt"), "--status", "SOLVED"])
            self.assertEqual(code, 1)
            self.assertIn("ERROR:", err.getvalue())


if __name__ == "__main__":
    unittest.main()
