# -*- coding: utf-8 -*-
"""Verifikation von ticket_audit: Kollisionserkennung + die zwei
Zweitbefunde aus T-20260808-03 (geclaimte Tickets in der Wurzel,
Nicht-Ticket-Dateien im Ticketbaum)."""
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


class TestCollectIds(unittest.TestCase):
    def test_no_collisions_on_clean_bestand(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            _write(base / "QUEUED" / "T-20260808-01.txt")
            _write(base / "SOLVED" / "T-20260808-02.WORKSTATION-LG.txt")
            report = ticket_audit.audit(base)
            self.assertEqual(report["collisions"], {})

    def test_reproduces_the_two_known_production_collisions(self):
        """Nachbau der beiden scharfen Kollisionen aus dem Ticket
        (T-20260731-02 in SOLVED+BLOCKED, T-20260731-03 in SOLVED+ACTIONABLE) —
        als synthetisches Fixture, nicht am echten Bestand."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            _write(base / "SOLVED" / "T-20260731-02.WORKSTATION-LG.txt", "memoryhooker-retrieval")
            _write(base / "BLOCKED" / "T-20260731-02.WORKSTATION-LG.txt", "foreign-state-vorgang")
            _write(base / "SOLVED" / "T-20260731-03.WORKSTATION-LG.txt", "bach-scoring-evaluation")
            _write(base / "ACTIONABLE" / "T-20260731-03.WORKSTATION-LG.txt", "trusted-peer-setup")
            _write(base / "QUEUED" / "T-20260808-01.txt", "unrelated, clean")

            report = ticket_audit.audit(base)
            self.assertEqual(set(report["collisions"]), {"T-20260731-02", "T-20260731-03"})
            self.assertEqual(len(report["collisions"]["T-20260731-02"]), 2)
            self.assertEqual(len(report["collisions"]["T-20260731-03"]), 2)

    def test_collision_across_padded_and_unpadded_number(self):
        """T-...-2.txt und T-...-02.txt sind dieselbe logische ID."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            _write(base / "QUEUED" / "T-20260808-2.txt")
            _write(base / "SOLVED" / "T-20260808-02.HOSTX.txt")
            report = ticket_audit.audit(base)
            self.assertIn("T-20260808-02", report["collisions"])


class TestClaimedInRoot(unittest.TestCase):
    def test_claimed_ticket_in_root_is_flagged(self):
        """Nachbau des Zweitbefunds: T-20260801-07.WORKSTATION-LG.txt lag
        sieben Tage unsichtbar in der Wurzel statt in einem Statusordner."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            _write(base / "T-20260801-07.WORKSTATION-LG.txt", "urgent user order")
            _write(base / "T-20260808-09.txt")  # unclaimed in root: das ist der INBOX-Regelfall
            report = ticket_audit.audit(base)
            self.assertEqual(
                report["claimed_in_root"],
                [str(base / "T-20260801-07.WORKSTATION-LG.txt")],
            )

    def test_unclaimed_ticket_in_root_is_not_flagged(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            _write(base / "T-20260808-09.txt")
            report = ticket_audit.audit(base)
            self.assertEqual(report["claimed_in_root"], [])

    def test_claimed_ticket_in_status_folder_is_not_flagged(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            _write(base / "ACTIONABLE" / "T-20260808-09.WORKSTATION-LG.txt")
            report = ticket_audit.audit(base)
            self.assertEqual(report["claimed_in_root"], [])


class TestNonTicketFiles(unittest.TestCase):
    def test_gitkeep_placeholder_is_not_flagged(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            _write(base / "QUEUED" / ".gitkeep")
            _write(base / "T-20260808-09.txt")
            report = ticket_audit.audit(base)
            self.assertEqual(report["non_ticket_files"], [])

    def test_stray_file_is_flagged(self):
        """Nachbau: COMIC-REPORT_2026-07-31.txt lag lose in der Wurzel."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            _write(base / "COMIC-REPORT_2026-07-31.txt")
            _write(base / "T-20260808-09.txt")
            report = ticket_audit.audit(base)
            self.assertEqual(
                report["non_ticket_files"],
                [str(base / "COMIC-REPORT_2026-07-31.txt")],
            )

    def test_readme_and_template_files_are_not_flagged_by_subfolder_scan(self):
        """_templates/ und _logs/ sind eigene Unterordner -- nur ihre TOP-LEVEL
        Geschwister werden gescannt, nicht rekursiv in sie hinein."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            _write(base / "README.md")
            _write(base / "_templates" / "TICKET.txt")
            _write(base / "_logs" / "INTAKE-TRIAGE-LOG.txt")
            _write(base / "T-20260808-09.txt")
            report = ticket_audit.audit(base)
            self.assertIn(str(base / "README.md"), report["non_ticket_files"])
            self.assertNotIn(
                str(base / "_templates" / "TICKET.txt"), report["non_ticket_files"]
            )
            self.assertNotIn(
                str(base / "_logs" / "INTAKE-TRIAGE-LOG.txt"), report["non_ticket_files"]
            )

    def test_non_ticket_file_in_status_folder_is_flagged(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            _write(base / "SOLVED" / "notes.txt")
            report = ticket_audit.audit(base)
            self.assertIn(str(base / "SOLVED" / "notes.txt"), report["non_ticket_files"])

    def test_legacy_slugged_tickets_are_not_false_positives(self):
        """Regression: gegen den echten Bestand geprueft, tauchten ~100
        legitime Alt-Tickets im Format T-YYYYMMDD-NN_slug[.HOST].txt als
        vermeintliche Nicht-Ticket-Dateien auf und haetten den einen echten
        Fund (ein loser Comic-Report) unter False Positives begraben."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            _write(base / "SOLVED" / "T-20260614-20_ticket-master-modul-repo.txt")
            _write(base / "SOLVED" / "T-20260621-43_roblox-malware-scan.LAPTOP.txt")
            _write(base / "COMIC-REPORT_2026-07-31.txt")
            report = ticket_audit.audit(base)
            self.assertEqual(
                report["non_ticket_files"],
                [str(base / "COMIC-REPORT_2026-07-31.txt")],
            )

    def test_nested_lifecycle_ticket_is_reported_without_mutation(self):
        """T-20260822-116395676: audit finds but never migrates USER/decision/."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            nested = _write(
                base / "USER" / "decision" / "T-20260822-123456789.ASUS-GEI.txt",
                "STATUS: USER/decision\n",
            )
            before = nested.read_bytes()

            report = ticket_audit.audit(base)

            self.assertEqual(report["nested_lifecycle_tickets"], [str(nested)])
            self.assertEqual(nested.read_bytes(), before)


class TestCleanBestand(unittest.TestCase):
    def test_fully_clean_bestand_reports_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            _write(base / "T-20260808-01.txt")
            _write(base / "ACTIONABLE" / "T-20260808-02.WORKSTATION-LG.txt")
            _write(base / "SOLVED" / "T-20260808-03.WORKSTATION-LG.txt")
            report = ticket_audit.audit(base)
            self.assertEqual(report["collisions"], {})
            self.assertEqual(report["claimed_in_root"], [])
            self.assertEqual(report["non_ticket_files"], [])


if __name__ == "__main__":
    unittest.main()
