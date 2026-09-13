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


class TestUnknownSubcategory(unittest.TestCase):
    """T-20260913-204557243: melden, niemals reparieren.

    Die drei bekannten Live-Abweichungen plus ein kanonisches Gegenbeispiel,
    wie im Ticket verlangt -- und zusaetzlich die Freitext-Faelle, an denen ein
    naiver Melder scheitern wuerde.
    """

    def test_the_three_live_deviations_are_reported_canonical_values_are_not(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            # Kanonisch -> kein Befund.
            _ticket(base / "USER" / "T-20260913-000000001.txt",
                    "USER/decision (seit 2026-09-13)")
            _ticket(base / "BLOCKED" / "T-20260913-000000002.txt",
                    "BLOCKED/foreign-state (seit 2026-09-13)")
            # Die drei real vorgefundenen Abweichungen.
            _ticket(base / "USER" / "T-20260913-000000003.txt",
                    "USER/decision-partial (seit 2026-09-13) — Rest offen")
            _ticket(base / "USER" / "T-20260913-000000004.txt",
                    "USER / uac-live-abnahme (seit 2026-09-13)")
            _ticket(base / "USER" / "T-20260913-000000005.txt",
                    "USER/manual-hardware-restart")
            findings = ticket_audit.status_drift(base)
            self.assertEqual([f["kind"] for f in findings],
                             ["unknown-subcategory"] * 3)
            self.assertEqual(
                sorted(Path(f["path"]).name for f in findings),
                ["T-20260913-000000003.txt", "T-20260913-000000004.txt",
                 "T-20260913-000000005.txt"],
            )

    def test_free_text_after_the_cluster_is_not_mistaken_for_a_subcategory(self):
        """Sonst floodet der Melder die Queue -- die Fehlerklasse aus
        T-20260808-03 (114 gemeldete "Nicht-Ticket-Dateien", ~100 davon echt)."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            _ticket(base / "SOLVED" / "T-20260913-000000001.txt",
                    "SOLVED / Option A vollständig belegt")
            _ticket(base / "SOLVED" / "T-20260913-000000002.txt",
                    "SOLVED / Decision-Routing und Wiederaufnahme geklärt")
            _ticket(base / "USER" / "T-20260913-000000003.txt",
                    "USER/freigabe — Fix lokal fertig und verifiziert")
            _ticket(base / "WAITING" / "T-20260913-000000004.txt",
                    "WAITING/review-due - Fix als Draft-PR offen")
            self.assertEqual(ticket_audit.status_drift(base), [])

    def test_a_cluster_documented_without_subcategories_accepts_none(self):
        """SOLVED traegt in der Tabelle einen Gedankenstrich. Das ist eine
        Aussage (keine Unterkategorien), kein Datenloch -- deshalb ist jeder
        Wert dort ein Befund."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            _ticket(base / "SOLVED" / "T-20260913-000000001.txt",
                    "SOLVED/local-only (seit 2026-09-13)")
            findings = ticket_audit.status_drift(base)
            self.assertEqual([f["kind"] for f in findings], ["unknown-subcategory"])

    def test_a_cluster_absent_from_the_table_stays_silent(self):
        """PENDING/.USER sind Legacy und stehen in keiner Tabellenzeile.
        'nicht dokumentiert' heisst hier 'nicht bewertbar', nicht 'alles falsch'."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            _ticket(base / "PENDING" / "T-20260801-01.txt", "PENDING/irgendwas")
            self.assertEqual(ticket_audit.status_drift(base), [])

    def test_cluster_level_drift_wins_so_a_ticket_is_reported_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            _ticket(base / "PARKED" / "T-20260913-000000001.txt",
                    "USER/decision-partial (seit 2026-09-13)")
            findings = ticket_audit.status_drift(base)
            self.assertEqual([f["kind"] for f in findings], ["folder-mismatch"])

    def test_vocabulary_comes_from_the_docs_and_both_languages_agree(self):
        """Die Doku ist die einzige Quelle -- also muss sie parsebar bleiben.
        Bricht das Tabellenlayout, faellt dieser Test, nicht erst ein Lauf."""
        vocabulary = ticket_audit.subcategory_vocabulary()
        self.assertEqual(vocabulary["USER"], frozenset(
            {"decision", "data", "freigabe", "hardware", "session", "marker"}))
        self.assertEqual(vocabulary["SOLVED"], frozenset())
        self.assertNotIn("PENDING", vocabulary)
        english = ticket_audit.subcategory_vocabulary(
            Path(ticket_audit._CATEGORIES_DOC).with_name("CATEGORIES.en.md"))
        self.assertEqual(vocabulary, english)

    def test_an_unreadable_vocabulary_is_said_out_loud_not_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            _ticket(base / "USER" / "T-20260913-000000001.txt", "USER/decision")
            original = ticket_audit._CATEGORIES_DOC
            ticket_audit._CATEGORIES_DOC = Path(tmp) / "missing" / "CATEGORIES.de.md"
            try:
                findings = ticket_audit.status_drift(base)
            finally:
                ticket_audit._CATEGORIES_DOC = original
            self.assertEqual([f["kind"] for f in findings],
                             ["subcategory-vocabulary-unavailable"])
