# -*- coding: utf-8 -*-
"""Nutzerentscheid 3A (T-20260830-145228426): audit() zaehlt formlose
INBOX-Dateien (ohne "T-"-Praefix) als eigenen Report-Schluessel
`informal_entries`, nicht als `non_ticket_files`. Dazu `lint()`/--lint:
Pflichtfelder, STATUS-Vokabular, doppelte Block-Ueberschriften -- nur
melden, nie reparieren."""
import sys
import tempfile
import unittest
from pathlib import Path

LIB_DIR = Path(__file__).resolve().parent.parent / "lib"
sys.path.insert(0, str(LIB_DIR))

import ticket_audit  # noqa: E402


def _write(path: Path, content: str = "x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


class TestInformalEntries(unittest.TestCase):
    def test_formless_inbox_file_is_informal_not_non_ticket(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            _write(base / "INBOX" / "formlos-codex-20260830.txt", "Freitext")
            report = ticket_audit.audit(base)
            self.assertEqual(len(report["informal_entries"]), 1)
            self.assertIn("formlos-codex-20260830.txt", report["informal_entries"][0])
            self.assertEqual(report["non_ticket_files"], [])

    def test_gitkeep_and_ticket_files_are_not_informal(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            _write(base / "INBOX" / ".gitkeep", "")
            _write(base / "INBOX" / "T-20260830-100000001.txt", "ticket")
            report = ticket_audit.audit(base)
            self.assertEqual(report["informal_entries"], [])

    def test_non_inbox_formless_files_stay_non_ticket_files(self):
        """Die Lockerung gilt nur fuer INBOX/ -- ein anderswo abgelegter
        Freitext bleibt Clutter."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            _write(base / "ACTIONABLE" / "notiz.txt", "kein Ticket")
            report = ticket_audit.audit(base)
            self.assertEqual(report["informal_entries"], [])
            self.assertEqual(len(report["non_ticket_files"]), 1)


class TestLint(unittest.TestCase):
    def test_clean_ticket_has_no_findings(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            _write(base / "ACTIONABLE" / "T-20260830-100000001.txt", (
                "ID:            T-20260830-100000001\n"
                "TITEL:         x\n"
                "ERSTELLT:      2026-08-30\n"
                "STATUS:        ACTIONABLE\n"
            ))
            self.assertEqual(ticket_audit.lint(base), [])

    def test_missing_required_fields_are_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            _write(base / "T-20260830-100000002.txt", "PRIORITAET:    mittel\n")
            findings = ticket_audit.lint(base)
            kinds_fields = {(f["kind"], f.get("field")) for f in findings}
            self.assertIn(("missing-field", "ID"), kinds_fields)
            self.assertIn(("missing-field", "TITLE"), kinds_fields)
            self.assertIn(("missing-field", "CREATED"), kinds_fields)
            self.assertIn(("missing-field", "STATUS"), kinds_fields)

    def test_title_and_created_aliases_are_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            _write(base / "T-20260830-100000003.txt", (
                "ID:            T-20260830-100000003\n"
                "TITLE:         english alias\n"
                "CREATED:       2026-08-30\n"
                "STATUS:        INBOX\n"
            ))
            self.assertEqual(ticket_audit.lint(base), [])

    def test_invalid_status_vocabulary_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            _write(base / "SOLVED" / "T-20260830-100000004.txt", (
                "ID:            T-20260830-100000004\n"
                "TITEL:         x\n"
                "ERSTELLT:      2026-08-30\n"
                "STATUS:        GELOEST\n"
            ))
            findings = ticket_audit.lint(base)
            self.assertTrue(any(f["kind"] == "invalid-status" for f in findings))

    def test_duplicate_block_headings_are_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            _write(base / "T-20260830-100000005.txt", (
                "ID:            T-20260830-100000005\n"
                "TITEL:         x\n"
                "ERSTELLT:      2026-08-30\n"
                "STATUS:        INBOX\n"
                "PROJEKT-ZUORDNUNG\n"
                "a\n"
                "PROJEKT-ZUORDNUNG\n"
                "b\n"
            ))
            findings = ticket_audit.lint(base)
            duplicate = [f for f in findings if f["kind"] == "duplicate-block"]
            self.assertEqual(len(duplicate), 1)
            self.assertEqual(duplicate[0]["heading"], "PROJEKT-ZUORDNUNG")
            self.assertEqual(duplicate[0]["count"], 2)

    def test_cli_lint_flag_reports_json(self):
        import json
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            _write(base / "T-20260830-100000006.txt", "PRIORITAET: mittel\n")
            import io
            from contextlib import redirect_stdout

            buf = io.StringIO()
            with redirect_stdout(buf):
                exit_code = ticket_audit._cli([str(base), "--lint", "--json"])
            self.assertEqual(exit_code, 1)
            findings = json.loads(buf.getvalue())
            self.assertTrue(len(findings) >= 1)

    def test_cli_lint_clean_bestand_exits_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            _write(base / "T-20260830-100000007.txt", (
                "ID:            T-20260830-100000007\n"
                "TITEL:         x\n"
                "ERSTELLT:      2026-08-30\n"
                "STATUS:        INBOX\n"
            ))
            exit_code = ticket_audit._cli([str(base), "--lint"])
            self.assertEqual(exit_code, 0)


if __name__ == "__main__":
    unittest.main()
