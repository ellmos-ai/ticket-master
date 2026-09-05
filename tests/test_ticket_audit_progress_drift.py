"""T-20260904-141396683: teilerledigte Tickets in offenen Ordnern erkennen.

Fehlerklasse: ein Stand war einmal wahr und wird spaeter als aktuell gelesen.
Jeder Test hier faellt aus, sobald die zugehoerige Pruefung nicht mehr greift --
ein Audit ohne solchen Test ist dieselbe Attrappe wie ein Guard ohne Selbsttest.
"""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

import ticket_audit  # noqa: E402

NL = chr(10)
_HEAD = "ID:            {tid}\nTITEL:         x\nSTATUS:        {folder}\n"


def _ticket(base: Path, folder: str, tid: str, body: str = "") -> Path:
    directory = base / folder if folder else base
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{tid}.txt"
    path.write_text(_HEAD.format(tid=tid, folder=folder or "INBOX") + body, encoding="utf-8")
    return path


class TestProgressDrift(unittest.TestCase):
    def test_fresh_ticket_is_not_flagged(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            _ticket(base, "ACTIONABLE", "T-20260904-000000001",
                    "\nVERLAUF / LOG\n2026-09-04  Aufgenommen.\n")
            self.assertEqual(ticket_audit.progress_drift(base), [])

    def test_gate4_acceptance_in_verlauf_is_flagged(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            _ticket(base, "ACTIONABLE", "T-20260904-000000002",
                    "\nVERLAUF / LOG\n2026-09-03  WELLE 1 ABGENOMMEN (GATE 4) durch lock-worker.\n")
            findings = ticket_audit.progress_drift(base)
            self.assertEqual([f["kind"] for f in findings], ["gate4-accepted"])
            self.assertIn("GATE 4", str(findings[0]["evidence"]))

    def test_acceptance_quoted_in_the_description_is_not_flagged(self):
        # Erster Treffer am echten Bestand war genau das: ein Ticket, das eine
        # FREMDE Abnahme in seiner Problembeschreibung zitiert. Deshalb wird
        # nur ab VERLAUF gesucht.
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            body = NL.join([
                "", "PROBLEM",
                "T-x trug 'WELLE 1 ABGENOMMEN (GATE 4)' im VERLAUF.",
                "", "VERLAUF / LOG", "2026-09-04  Aufgenommen.", "",
            ])
            _ticket(base, "INBOX", "T-20260904-000000010", body)
            self.assertEqual(ticket_audit.progress_drift(base), [])

    def test_delegiert_an_with_completion_note_is_flagged(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            _ticket(base, "INBOX", "T-20260904-000000003",
                    "\nDELEGIERT_AN: lock-worker@ASUS-GEI, Welle 1 fertig, Wellen 2-4 offen\n")
            findings = ticket_audit.progress_drift(base)
            self.assertEqual([f["kind"] for f in findings], ["delegated-done"])

    def test_plain_delegation_without_completion_note_is_not_flagged(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            _ticket(base, "ACTIONABLE", "T-20260904-000000004",
                    "\nDELEGIERT_AN: bach-worker@ASUS-GEI\n")
            self.assertEqual(ticket_audit.progress_drift(base), [])

    def test_closed_folder_is_out_of_scope(self):
        # SOLVED darf einen Abnahmevermerk tragen -- das ist sein Normalzustand.
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            _ticket(base, "SOLVED", "T-20260904-000000005",
                    "\nVERLAUF / LOG\n2026-09-03  WELLE 1 ABGENOMMEN (GATE 4).\n")
            self.assertEqual(ticket_audit.progress_drift(base), [])

    def test_audit_report_carries_the_finding(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            _ticket(base, "ACTIONABLE", "T-20260904-000000006",
                    "\nVERLAUF / LOG\n2026-09-03  ABGENOMMEN (GATE-4).\n")
            # source_roots=[] haelt den Test von einem echten Repo-Scan fern.
            report = ticket_audit.audit(base)
            self.assertEqual(len(report["progress_drift"]), 1)


class TestSourceReferenceDrift(unittest.TestCase):
    """T-20260903-370384774: Erledigungsstand stand NUR im Quelltext."""

    def _repo(self, root: Path, name: str, content: str) -> Path:
        repo = root / name
        repo.mkdir(parents=True)
        (repo / "sync_skills.sh").write_text(content, encoding="utf-8")
        for cmd in (["init", "-q"], ["add", "-A"]):
            subprocess.run(["git", "-C", str(repo), *cmd], check=True,
                           capture_output=True)
        return repo

    def test_open_ticket_id_found_in_source_is_flagged(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repos"
            base = Path(tmp) / "tickets"
            tid = "T-20260903-370384774"
            _ticket(base, "ACTIONABLE", tid)
            self._repo(root, "skill-mirror",
                       f"# Entscheidung {tid}: lokale Aenderungen nicht ueberschreiben\n")
            findings = ticket_audit.source_reference_drift(base, roots=[root])
            self.assertEqual([f["ticket"] for f in findings], [tid])
            self.assertIn("skill-mirror", str(findings[0]["location"]))

    def test_unrelated_ticket_id_in_source_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repos"
            base = Path(tmp) / "tickets"
            _ticket(base, "ACTIONABLE", "T-20260904-000000007")
            self._repo(root, "other", "# siehe T-20260101-999999999\n")
            self.assertEqual(ticket_audit.source_reference_drift(base, roots=[root]), [])

    def test_audit_scans_sources_only_on_request(self):
        # Der Scan ist opt-in, weil er je Repo ein git grep kostet. Ein Opt-in,
        # das der Aufrufer nie einschaltet, faengt nichts -- also beide Seiten
        # festnageln: aus = leer, an = Treffer.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repos"
            base = Path(tmp) / "tickets"
            tid = "T-20260904-000000009"
            _ticket(base, "ACTIONABLE", tid)
            self._repo(root, "some-repo", "# " + tid + chr(10))
            off = ticket_audit.audit(base)
            self.assertEqual(off["source_reference_drift"], [])
            on = ticket_audit.audit(base, scan_sources=True, source_roots=[root])
            self.assertEqual([f["ticket"] for f in on["source_reference_drift"]], [tid])

    def test_cli_enables_the_source_scan_by_default(self):
        import inspect
        src = inspect.getsource(ticket_audit._cli)
        self.assertIn("scan_sources=args.scan_sources", src)
        self.assertIn("--no-source-scan", src)

    def test_solved_ticket_id_in_source_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repos"
            base = Path(tmp) / "tickets"
            tid = "T-20260904-000000008"
            _ticket(base, "SOLVED", tid)
            self._repo(root, "done-repo", f"# {tid} erledigt\n")
            self.assertEqual(ticket_audit.source_reference_drift(base, roots=[root]), [])


if __name__ == "__main__":
    unittest.main()
