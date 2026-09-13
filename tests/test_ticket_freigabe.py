"""T-20260913-744071825: hashgebundene Einmal-Freigabe fuer USER/freigabe.

Die vom Ticket verlangten vier Regressionsfaelle -- richtige ID, falsche ID,
fehlende ID, zweite Verwendung derselben ID -- plus die Faelle, die erst beim
Messen am Live-Bestand sichtbar wurden (Platzhalter, Altbestand).
"""

import sys
import tempfile
import unittest
from pathlib import Path

LIB = Path(__file__).resolve().parent.parent / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

import ticket_freigabe as tf  # noqa: E402
import ticket_mover  # noqa: E402


RULE = "-" * 62


def _ticket(path: Path, freigabe_body: str | None = None) -> Path:
    """Ein Ticket im echten Format; ohne Argument ohne Freigabe-Sektion."""
    parts = [
        "=" * 62,
        "TICKET",
        "=" * 62,
        "ID:            T-20260913-000000001",
        "TITEL:         Beispiel",
        "STATUS:        USER/freigabe (seit 2026-09-13)",
        "",
    ]
    if freigabe_body is not None:
        parts += [RULE, "ZUR FREIGABE", RULE, freigabe_body, ""]
    parts += [RULE, "VERLAUF / LOG", RULE, "2026-09-13  Aufgenommen.", ""]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(parts), encoding="utf-8")
    return path


class TestFreigabeId(unittest.TestCase):
    def test_id_shape_and_sensitivity_to_the_wording(self):
        first = tf.freigabe_id("Modul X oeffentlich schalten.")
        self.assertTrue(first.startswith("frg_"))
        self.assertEqual(len(first), len("frg_") + 20)
        self.assertNotEqual(first, tf.freigabe_id("Modul Y oeffentlich schalten."))

    def test_line_endings_and_trailing_blanks_do_not_break_an_approval(self):
        """Die Queue liegt in OneDrive und wird von Windows wie Linux bearbeitet.
        Eine Freigabe darf nicht an einem CRLF scheitern -- wohl aber an jeder
        Aenderung des Wortlauts."""
        self.assertEqual(
            tf.freigabe_id("Zeile eins\nZeile zwei"),
            tf.freigabe_id("Zeile eins   \r\nZeile zwei\r\n"),
        )


class TestFreigabeLifecycle(unittest.TestCase):
    def test_the_four_cases_the_ticket_asks_for(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _ticket(Path(tmp) / "T-20260913-000000001.txt",
                           "Repo dev-bricks/beispiel auf public schalten.")
            self.assertEqual(tf.freigabe_state(path)["state"], "unstamped")

            # (0) ID vergeben, damit der Nutzer sie zitieren kann.
            stamped = tf.stamp_freigabe_id(path)
            self.assertEqual(tf.freigabe_state(path)["state"], "pending")

            # (2) falsche ID -> abgelehnt, Datei unveraendert.
            before = path.read_bytes()
            with self.assertRaises(tf.FreigabeError) as refused:
                tf.mark_freigabe(path, freigabe_id_value="frg_0000000000000000dead",
                                 by="control1@ASUS-GEI")
            self.assertIn("does not match", str(refused.exception))
            self.assertEqual(path.read_bytes(), before)

            # (3) fehlende ID -> genauso abgelehnt (leerer String matcht nie).
            with self.assertRaises(tf.FreigabeError):
                tf.mark_freigabe(path, freigabe_id_value="", by="control1@ASUS-GEI")
            self.assertEqual(path.read_bytes(), before)

            # (1) richtige ID -> Freigabe wird vermerkt.
            granted = tf.mark_freigabe(path, freigabe_id_value=stamped,
                                       by="control1@ASUS-GEI")
            self.assertIn(stamped, granted)
            self.assertEqual(tf.freigabe_state(path)["state"], "granted")

            # (4) zweite Verwendung derselben ID -> abgelehnt, Datei unveraendert.
            after = path.read_bytes()
            with self.assertRaises(tf.FreigabeError) as again:
                tf.mark_freigabe(path, freigabe_id_value=stamped,
                                 by="control1@ASUS-GEI")
            self.assertIn("single-use", str(again.exception))
            self.assertEqual(path.read_bytes(), after)

    def test_a_text_changed_after_approval_no_longer_counts_as_approved(self):
        """Der eigentliche Zweck der Hashbindung: eine spaetere Textaenderung
        darf nicht stillschweigend mitfreigegeben sein."""
        with tempfile.TemporaryDirectory() as tmp:
            path = _ticket(Path(tmp) / "T-20260913-000000001.txt",
                           "Nur Modul A veroeffentlichen.")
            stamped = tf.stamp_freigabe_id(path)
            tf.mark_freigabe(path, freigabe_id_value=stamped, by="control1@ASUS-GEI")
            self.assertEqual(tf.freigabe_state(path)["state"], "granted")

            path.write_text(
                path.read_text(encoding="utf-8").replace(
                    "Nur Modul A veroeffentlichen.",
                    "Modul A und Modul B veroeffentlichen."),
                encoding="utf-8")
            state = tf.freigabe_state(path)
            self.assertEqual(state["state"], "stale")
            self.assertNotIn(state["current_id"], state["granted"])

    def test_stamping_is_idempotent_but_never_silent_under_a_standing_approval(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _ticket(Path(tmp) / "T-20260913-000000001.txt", "Freigabe X.")
            first = tf.stamp_freigabe_id(path)
            unchanged = path.read_bytes()
            self.assertEqual(tf.stamp_freigabe_id(path), first)
            self.assertEqual(path.read_bytes(), unchanged)

            tf.mark_freigabe(path, freigabe_id_value=first, by="control1@ASUS-GEI")
            with self.assertRaises(tf.FreigabeError) as refused:
                tf.stamp_freigabe_id(path)
            self.assertIn("already approved", str(refused.exception))


class TestLegacyAndPlaceholders(unittest.TestCase):
    def test_legacy_stock_is_not_retrofitted_and_says_so(self):
        """17 Tickets standen am 2026-09-13 formlos in USER/freigabe. Eine ID
        fuer sie zu berechnen wuerde eine Bindung vortaeuschen, die es nie gab."""
        with tempfile.TemporaryDirectory() as tmp:
            path = _ticket(Path(tmp) / "T-20260808-000000001.txt")  # ohne Sektion
            self.assertEqual(tf.freigabe_state(path)["state"], "legacy")
            before = path.read_bytes()
            with self.assertRaises(tf.FreigabeError) as refused:
                tf.mark_freigabe(path, freigabe_id_value="frg_whatever",
                                 by="control1@ASUS-GEI")
            self.assertIn("legacy stock", str(refused.exception))
            self.assertEqual(path.read_bytes(), before)

    def test_an_unfilled_placeholder_is_not_a_text_to_approve(self):
        """Sonst traegt jedes ungefuellte Ticket DIESELBE ID -- eine einzige
        geteilte 'Freigabe' fuer beliebige Inhalte. Gemessen: 9 von 17 Tickets
        in USER/freigabe tragen den AUFTRAG-Platzhalter woertlich."""
        with tempfile.TemporaryDirectory() as tmp:
            a = _ticket(Path(tmp) / "a" / "T-20260913-000000001.txt",
                        "<Der genaue Wortlaut, den der Nutzer freigeben soll.>")
            b = _ticket(Path(tmp) / "b" / "T-20260913-000000002.txt",
                        "<Der genaue Wortlaut, den der Nutzer freigeben soll.>")
            for path in (a, b):
                self.assertEqual(tf.freigabe_state(path)["state"], "legacy")
                with self.assertRaises(tf.FreigabeError):
                    tf.stamp_freigabe_id(path)

    def test_an_empty_section_states_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _ticket(Path(tmp) / "T-20260913-000000001.txt", "   ")
            self.assertEqual(tf.freigabe_state(path)["state"], "legacy")

    def test_the_section_ends_at_the_next_section_not_at_the_file_end(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _ticket(Path(tmp) / "T-20260913-000000001.txt", "Nur dies hier.")
            self.assertEqual(tf.freigabe_text(path.read_text(encoding="utf-8")),
                             "Nur dies hier.")


class TestCli(unittest.TestCase):
    def test_cli_round_trip_and_fail_closed_exit_codes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _ticket(Path(tmp) / "T-20260913-000000001.txt", "CLI-Freigabe.")
            self.assertEqual(
                ticket_mover._cli(["--freigabe-status", str(path)]), 0)
            self.assertEqual(
                ticket_mover._cli(["--stamp-freigabe-id", str(path)]), 0)
            stamped = tf.freigabe_state(path)["current_id"]
            self.assertEqual(ticket_mover._cli([
                "--mark-freigabe", str(path), "--freigabe-id", "frg_wrong",
                "--agent", "control1@ASUS-GEI"]), 1)
            self.assertEqual(ticket_mover._cli([
                "--mark-freigabe", str(path), "--freigabe-id", stamped,
                "--agent", "control1@ASUS-GEI"]), 0)
            self.assertEqual(ticket_mover._cli([
                "--mark-freigabe", str(path), "--freigabe-id", stamped,
                "--agent", "control1@ASUS-GEI"]), 1)

    def test_a_freigabe_operation_cannot_be_combined_with_a_move(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _ticket(Path(tmp) / "T-20260913-000000001.txt", "X.")
            with self.assertRaises(SystemExit):
                ticket_mover._cli([str(path), tmp, "--freigabe-status", str(path)])


if __name__ == "__main__":
    unittest.main()
