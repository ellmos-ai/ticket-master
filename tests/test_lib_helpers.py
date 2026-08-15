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
            # Nummer ist seit 2026-08-15 zufaellig (9-stellig), deshalb wird
            # die Form geprueft, nicht der Wert.
            m = ticket_writer.TICKET_FILENAME_RE.match(p.name)
            self.assertIsNotNone(m)
            self.assertEqual(m.group("date"), "20260627")
            self.assertEqual(len(m.group("number")), ticket_writer.ID_DIGITS)
            self.assertIsNone(m.group("suffix"))
            self.assertEqual(p.parent.name, "INBOX")
            text = p.read_text(encoding="utf-8")
            self.assertIn("STATUS:        INBOX", text)
            self.assertIn("Titel", text)
            self.assertIn(p.stem, text)  # ID im Dateinamen == ID im Ticket

    def test_id_avoids_numbers_taken_anywhere_in_lifecycle(self):
        """Der lokale Abgleich zaehlt Tickets in ALLEN Lebenszyklus-Ordnern —
        ein nach SOLVED verschobenes Ticket darf seine Nummer nicht freigeben.
        Erzwungen wird das hier ueber einen RNG, der zuerst genau die schon
        belegten Zahlen liefert."""
        import random
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "SOLVED").mkdir()
            (base / "SOLVED" / "T-20260627-111111111.HOSTX.txt").write_text(
                "alt", encoding="utf-8")
            (base / "T-20260627-222222222.txt").write_text("intake", encoding="utf-8")

            class ScriptedRandom(random.Random):
                def __init__(self, values):
                    super().__init__()
                    self._values = list(values)

                def randrange(self, *_args, **_kwargs):
                    return self._values.pop(0)

            path = ticket_writer.create(
                "Neu", "Body", tickets_dir=base, today="2026-06-27",
                rng=ScriptedRandom([111111111, 222222222, 333333333]))
            self.assertEqual(Path(path).name, "T-20260627-333333333.txt")

    def test_id_unique_across_v1_category_dirs(self):
        """Kategorien v1: der Abgleich sieht auch die neuen Cluster-Ordner
        (INBOX/ACTIONABLE/BLOCKED/WAITING/USER/PARKED) und die Legacy-Aliase
        PENDING/.USER (docs/CATEGORIES.*.md)."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            for sub, name in (("ACTIONABLE", "T-20260627-100000001.txt"),
                              ("BLOCKED", "T-20260627-100000002.HOSTX.txt"),
                              ("USER", "T-20260627-100000003.txt"),
                              ("PARKED", "T-20260627-100000004.txt"),
                              ("PENDING", "T-20260627-100000005.txt")):
                d = base / sub
                d.mkdir()
                (d / name).write_text("alt", encoding="utf-8")
            self.assertEqual(
                ticket_writer.used_numbers(base, "20260627"),
                {100000001, 100000002, 100000003, 100000004, 100000005})

    def test_create_never_overwrites_existing(self):
        """Zieht der RNG eine Zahl, deren Datei schon existiert (Race auf
        demselben Host), wird neu gewuerfelt statt ueberschrieben."""
        import random
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            inbox = base / "INBOX"
            inbox.mkdir()
            first = inbox / "T-20260627-444444444.txt"
            first.write_text("ORIGINAL", encoding="utf-8")

            class ScriptedRandom(random.Random):
                def __init__(self, values):
                    super().__init__()
                    self._values = list(values)

                def randrange(self, *_args, **_kwargs):
                    return self._values.pop(0)

            # Erste Zahl ist belegt, taucht aber NICHT in used_numbers auf,
            # weil die Datei erst nach dem Einlesen entstanden waere.
            rng = ScriptedRandom([444444444, 555555555])
            with patch.object(ticket_writer, "used_numbers", return_value=set()):
                path = ticket_writer.create("Neu", "Body", tickets_dir=base,
                                            today="2026-06-27", rng=rng)
            self.assertEqual(Path(path).name, "T-20260627-555555555.txt")
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
