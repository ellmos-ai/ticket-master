# -*- coding: utf-8 -*-
"""Verifikation der zurueckgespiegelten Python-Helfer im ticket-master-Modul:
ticket_writer (asynchrone Ticket-Erzeugung) + doc_scanner (TODO/AUFGABEN/DONE/DECISIONS)."""
import sys
import tempfile
import unittest
from pathlib import Path

LIB_DIR = Path(__file__).resolve().parent.parent / "lib"
sys.path.insert(0, str(LIB_DIR))

import ticket_writer  # noqa: E402
import doc_scanner  # noqa: E402


class TestTicketWriter(unittest.TestCase):
    def test_requires_tickets_dir(self):
        with self.assertRaises(ValueError):
            ticket_writer.create("t", "b", today="2026-06-27")  # kein tickets_dir/env

    def test_creates_unclaimed_ticket(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = ticket_writer.create("Titel", "Body", project="proj",
                                        tickets_dir=Path(tmp), today="2026-06-27")
            p = Path(path)
            self.assertEqual(p.name, "T-20260627-01.txt")
            self.assertEqual(p.parent.name, "INBOX")
            text = p.read_text(encoding="utf-8")
            self.assertIn("STATUS:        INBOX", text)
            self.assertIn("Titel", text)

    def test_id_unique_across_lifecycle_dirs(self):
        """Nummernvergabe zaehlt Tickets in ALLEN Lebenszyklus-Ordnern —
        ein nach SOLVED verschobenes Ticket darf seine ID nicht freigeben."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            solved = base / "SOLVED"
            solved.mkdir()
            (solved / "T-20260627-01.HOSTX.txt").write_text("alt", encoding="utf-8")
            (base / "T-20260627-02.txt").write_text("intake", encoding="utf-8")
            path = ticket_writer.create("Neu", "Body", tickets_dir=base,
                                        today="2026-06-27")
            self.assertEqual(Path(path).name, "T-20260627-03.txt")

    def test_id_unique_across_v1_category_dirs(self):
        """Kategorien v1: Nummernvergabe zaehlt auch die neuen Cluster-Ordner
        (INBOX/ACTIONABLE/BLOCKED/WAITING/USER/PARKED) und die Legacy-Aliase
        PENDING/.USER weiter mit (docs/CATEGORIES.*.md)."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            for sub, name in (("ACTIONABLE", "T-20260627-01.txt"),
                              ("BLOCKED", "T-20260627-02.HOSTX.txt"),
                              ("USER", "T-20260627-03.txt"),
                              ("PARKED", "T-20260627-04.txt"),
                              ("PENDING", "T-20260627-05.txt")):
                d = base / sub
                d.mkdir()
                (d / name).write_text("alt", encoding="utf-8")
            path = ticket_writer.create("Neu", "Body", tickets_dir=base,
                                        today="2026-06-27")
            self.assertEqual(Path(path).name, "T-20260627-06.txt")

    def test_create_never_overwrites_existing(self):
        """Kollidiert die berechnete Nummer (Race/Cloud-Sync), wird hochgezaehlt
        statt ueberschrieben (exklusives Anlegen)."""
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            inbox = base / "INBOX"
            inbox.mkdir()
            first = inbox / "T-20260627-01.txt"
            first.write_text("ORIGINAL", encoding="utf-8")
            with patch.object(ticket_writer, "_next_number", return_value=1):
                path = ticket_writer.create("Neu", "Body", tickets_dir=base,
                                            today="2026-06-27")
            self.assertEqual(Path(path).name, "T-20260627-02.txt")
            self.assertEqual(first.read_text(encoding="utf-8"), "ORIGINAL")

    def test_cli_creates_ticket(self):
        """T-20260808-03 Punkt 2: die Vergabesperre nuetzt nur, wenn sie
        bequemer ist als von Hand zaehlen. Ein Shell-Einzeiler ist das."""
        with tempfile.TemporaryDirectory() as tmp:
            exit_code = ticket_writer._cli([
                "--title", "CLI-Titel", "--body", "CLI-Body",
                "--tickets-dir", tmp,
            ])
            self.assertEqual(exit_code, 0)
            created = list((Path(tmp) / "INBOX").glob("T-*.txt"))
            self.assertEqual(len(created), 1)
            self.assertIn("CLI-Titel", created[0].read_text(encoding="utf-8"))

    def test_cli_requires_tickets_dir(self):
        import os
        from unittest.mock import patch

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TICKET_MASTER_TICKETS_DIR", None)
            exit_code = ticket_writer._cli(["--title", "x"])
        self.assertEqual(exit_code, 1)


class TestDocScanner(unittest.TestCase):
    def test_scan_and_ensure(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "TODO.md").write_text("# TODO\n", encoding="utf-8")
            res = doc_scanner.scan_docs(d)
            self.assertTrue(res["todo"]["exists"])
            self.assertFalse(res["decisions"]["exists"])
            path = doc_scanner.ensure_doc(d, "decisions")
            self.assertIn("ADR", Path(path).read_text(encoding="utf-8"))

    def test_append_entry_rejects_non_utf8(self):
        """Nicht-UTF-8-Bestand (z. B. cp1252) fuehrt zu ValueError statt
        stiller U+FFFD-Korruption; die Datei bleibt unveraendert."""
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "TODO.md"
            raw = "# TODO\nCafé äöü\n".encode("cp1252")
            target.write_bytes(raw)
            with self.assertRaises(ValueError):
                doc_scanner.append_entry(target, "- neuer Eintrag")
            self.assertEqual(target.read_bytes(), raw)

    def test_append_entry_appends_utf8(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "TODO.md"
            target.write_text("# TODO\nCafé äöü", encoding="utf-8")
            doc_scanner.append_entry(target, "- neuer Eintrag")
            text = target.read_text(encoding="utf-8")
            self.assertIn("Café äöü\n- neuer Eintrag\n", text)


if __name__ == "__main__":
    unittest.main()
