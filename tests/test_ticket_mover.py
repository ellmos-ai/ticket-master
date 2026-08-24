# -*- coding: utf-8 -*-
"""Verifikation von ticket_mover: T-20260808-03 verlangt einen EMPIRISCHEN
Beleg, dass ein Verschieben auf ein belegtes Ziel scheitert, nicht nur eine
Codeaenderung. test_move_onto_occupied_target_is_refused_and_survives ist
dieser Beleg."""
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

LIB_DIR = Path(__file__).resolve().parent.parent / "lib"
sys.path.insert(0, str(LIB_DIR))

import ticket_mover  # noqa: E402


class TestMoveTicket(unittest.TestCase):
    def test_cli_single_move_dry_run_reports_without_mutation(self):
        """T-20260822-797239249: --dry-run muss auch beim Einzelticket
        eine echte Vorschau sein und darf nicht einmal den Zielordner anlegen."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            source = base / "ACTIONABLE" / "T-20260822-797239249.ASUS-GEI.txt"
            source.parent.mkdir()
            original = b"DRY-RUN-SOURCE\n"
            source.write_bytes(original)
            dest_dir = base / "USER"
            output = StringIO()

            with redirect_stdout(output):
                result = ticket_mover._cli(
                    [str(source), str(dest_dir), "--dry-run"])

            target = dest_dir / source.name
            self.assertEqual(result, 0)
            self.assertEqual(output.getvalue(), f"WOULD MOVE: {target}\n")
            self.assertTrue(source.is_file())
            self.assertEqual(source.read_bytes(), original)
            self.assertFalse(dest_dir.exists())
            self.assertFalse(target.exists())

    def test_cli_single_move_dry_run_refuses_collision_without_mutation(self):
        """Die Vorschau behält die fail-closed Kollisionsprüfung bei und
        verändert weder die Quelle noch ein bereits vorhandenes Ziel."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            source = base / "ACTIONABLE" / "T-20260822-797239249.ASUS-GEI.txt"
            source.parent.mkdir()
            source_before = b"SOURCE-BEFORE\n"
            source.write_bytes(source_before)
            dest_dir = base / "USER"
            dest_dir.mkdir()
            target = dest_dir / source.name
            target_before = b"TARGET-BEFORE\n"
            target.write_bytes(target_before)
            output = StringIO()

            with redirect_stdout(output):
                result = ticket_mover._cli(
                    [str(source), str(dest_dir), "--dry-run"])

            self.assertEqual(result, 1)
            self.assertIn("REFUSED:", output.getvalue())
            self.assertTrue(source.is_file())
            self.assertEqual(source.read_bytes(), source_before)
            self.assertTrue(target.is_file())
            self.assertEqual(target.read_bytes(), target_before)

    def test_move_relocates_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            source = base / "T-20260808-01.txt"
            source.write_text("CONTENT", encoding="utf-8")
            dest_dir = base / "SOLVED"
            target = ticket_mover.move_ticket(source, dest_dir)
            self.assertEqual(target, dest_dir / "T-20260808-01.txt")
            self.assertTrue(target.is_file())
            self.assertFalse(source.exists())
            self.assertEqual(target.read_text(encoding="utf-8"), "CONTENT")

    def test_move_onto_occupied_target_is_refused_and_survives(self):
        """Der Pflichtnachweis aus dem Ticket: ein Verschiebeversuch auf ein
        belegtes Ziel MUSS scheitern -- und zwar so, dass BEIDE Dateien
        unveraendert erhalten bleiben (kein Verlust, keine Vermischung)."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            source = base / "T-20260808-02.txt"
            source.write_text("NEUER VORGANG - darf nicht verloren gehen", encoding="utf-8")
            dest_dir = base / "SOLVED"
            dest_dir.mkdir()
            occupied = dest_dir / "T-20260808-02.txt"
            occupied.write_text("ALTER VORGANG - seit Tagen hier, darf nicht ueberschrieben werden",
                                 encoding="utf-8")

            with self.assertRaises(ticket_mover.TicketCollisionError):
                ticket_mover.move_ticket(source, dest_dir)

            # Exakt der Schaden aus dem Ticket waere hier: occupied veraendert
            # oder source verschwunden. Beides muss ausbleiben.
            self.assertEqual(
                occupied.read_text(encoding="utf-8"),
                "ALTER VORGANG - seit Tagen hier, darf nicht ueberschrieben werden",
            )
            self.assertTrue(source.is_file())
            self.assertEqual(
                source.read_text(encoding="utf-8"),
                "NEUER VORGANG - darf nicht verloren gehen",
            )

    def test_move_to_queued_refuses_same_id_in_another_lifecycle_folder(self):
        """A different suffix must not hide an already visible physical copy."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            source = base / "ACTIONABLE" / "T-20260824-422995631.ASUS-GEI.txt"
            source.parent.mkdir()
            source.write_text("SOURCE", encoding="utf-8")
            duplicate = base / "SOLVED" / "T-20260824-422995631.WORKSTATION-LG.txt"
            duplicate.parent.mkdir()
            duplicate.write_text("OTHER HOST", encoding="utf-8")

            with self.assertRaises(ticket_mover.DuplicateTicketIdError) as caught:
                ticket_mover.move_ticket(source, base / "QUEUED")

            message = str(caught.exception)
            self.assertIn(str(source), message)
            self.assertIn(str(duplicate), message)
            self.assertEqual(source.read_text(encoding="utf-8"), "SOURCE")
            self.assertEqual(duplicate.read_text(encoding="utf-8"), "OTHER HOST")
            self.assertFalse((base / "QUEUED").exists())

    def test_move_to_queued_matches_legacy_and_v2_names_by_canonical_id(self):
        """Slug, host claim and routing-v2 axes are not part of the ticket ID."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            source = (
                base / "INBOX" /
                "T-20260824-422995631_legacy.ASUS-GEI.txt"
            )
            source.parent.mkdir()
            source.write_text("LEGACY", encoding="utf-8")
            duplicate = (
                base / "USER" /
                "T-20260824-422995631_routed.to-WORKSTATION-LG."
                "via-claude.claim-WORKSTATION-LG.txt"
            )
            duplicate.parent.mkdir()
            duplicate.write_text("ROUTING V2", encoding="utf-8")

            with self.assertRaises(ticket_mover.DuplicateTicketIdError):
                ticket_mover.move_ticket(source, base / "QUEUED")

            self.assertTrue(source.is_file())
            self.assertTrue(duplicate.is_file())
            self.assertFalse((base / "QUEUED").exists())

    def test_move_to_queued_succeeds_when_source_is_the_only_id_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            source = base / "ACTIONABLE" / "T-20260824-422995631.ASUS-GEI.txt"
            source.parent.mkdir()
            source.write_text("ONLY COPY", encoding="utf-8")

            target = ticket_mover.move_ticket(source, base / "QUEUED")

            self.assertEqual(target, base / "QUEUED" / source.name)
            self.assertFalse(source.exists())
            self.assertEqual(target.read_text(encoding="utf-8"), "ONLY COPY")

    def test_duplicate_id_does_not_change_non_queued_moves(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            source = base / "ACTIONABLE" / "T-20260824-422995631.ASUS-GEI.txt"
            source.parent.mkdir()
            source.write_text("SOURCE", encoding="utf-8")
            duplicate = base / "WAITING" / "T-20260824-422995631.WORKSTATION-LG.txt"
            duplicate.parent.mkdir()
            duplicate.write_text("WAITING COPY", encoding="utf-8")

            target = ticket_mover.move_ticket(source, base / "SOLVED")

            self.assertEqual(target, base / "SOLVED" / source.name)
            self.assertTrue(target.is_file())
            self.assertTrue(duplicate.is_file())

    def test_duplicate_arriving_during_queued_move_rolls_back_new_target(self):
        """A post-write scan narrows the scan/open race without deleting either copy."""
        import os

        real_open = os.open
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            source = base / "ACTIONABLE" / "T-20260824-422995631.ASUS-GEI.txt"
            source.parent.mkdir()
            source.write_text("SOURCE", encoding="utf-8")
            target = base / "QUEUED" / source.name
            duplicate = base / "SOLVED" / "T-20260824-422995631.WORKSTATION-LG.txt"
            injected = False

            def racing_open(path, flags, *args, **kwargs):
                nonlocal injected
                if Path(path) == target and not injected:
                    duplicate.parent.mkdir()
                    duplicate.write_text("SYNC ARRIVAL", encoding="utf-8")
                    injected = True
                return real_open(path, flags, *args, **kwargs)

            try:
                os.open = racing_open
                with self.assertRaises(ticket_mover.DuplicateTicketIdError):
                    ticket_mover.move_ticket(source, base / "QUEUED")
            finally:
                os.open = real_open

            self.assertTrue(source.is_file())
            self.assertEqual(source.read_text(encoding="utf-8"), "SOURCE")
            self.assertTrue(duplicate.is_file())
            self.assertEqual(duplicate.read_text(encoding="utf-8"), "SYNC ARRIVAL")
            self.assertFalse(target.exists())

    def test_missing_source_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            with self.assertRaises(FileNotFoundError):
                ticket_mover.move_ticket(base / "does-not-exist.txt", base / "SOLVED")

    def test_dest_dir_created_if_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            source = base / "T-20260808-03.txt"
            source.write_text("x", encoding="utf-8")
            dest_dir = base / "BRAND_NEW_STATUS_DIR"
            self.assertFalse(dest_dir.exists())
            target = ticket_mover.move_ticket(source, dest_dir)
            self.assertTrue(target.is_file())

    def test_dest_dir_as_full_ticket_file_path_is_refused(self):
        """T-20260818-427750316: ein Aufrufer, der dest_dir versehentlich als
        volle Zieldatei statt als Zielordner uebergibt (".../SOLVED/T-....txt"
        statt ".../SOLVED"), erzeugte bisher lautlos einen verschachtelten
        Ordner in Ticket-Dateiform (SOLVED/T-....txt/T-....txt) -- real live
        beobachtet bei einem USER->SOLVED-Move. move_ticket() muss das jetzt
        VOR jeder Schreibaktion erkennen und fail-closed ablehnen: kein
        Zielordner/-datei entsteht, die Quelle bleibt unangetastet."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            source = base / "T-20260818-01.ASUS-GEI.txt"
            source.write_text("CONTENT", encoding="utf-8")
            dest_dir = base / "SOLVED"
            # Der Bedienfehler: volle Zieldatei statt Zielordner.
            bogus_dest = dest_dir / source.name

            with self.assertRaises(ticket_mover.DestinationLooksLikeFileError):
                ticket_mover.move_ticket(source, bogus_dest)

            # Der historische Schaden: dest_dir.name ("T-....txt") wurde als
            # ECHTER ORDNER angelegt und enthielt die Datei ein Level zu tief.
            self.assertFalse(dest_dir.exists(), "SOLVED/ darf gar nicht erst entstehen")
            self.assertTrue(source.is_file())
            self.assertEqual(source.read_text(encoding="utf-8"), "CONTENT")

        # Der korrekte Aufruf (dest_dir = Ordner) muss weiterhin funktionieren --
        # der Guard darf legitime Ziele nicht mit-blockieren.
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            source = base / "T-20260818-02.ASUS-GEI.txt"
            source.write_text("CONTENT", encoding="utf-8")
            dest_dir = base / "SOLVED"
            target = ticket_mover.move_ticket(source, dest_dir)
            self.assertEqual(target, dest_dir / source.name)
            self.assertTrue(target.is_file())
            self.assertFalse(source.exists())

    def test_nested_lifecycle_subcategory_destination_is_refused(self):
        """T-20260822-116395676: USER/decision is STATUS metadata, not a folder."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            source_dir = base / "ACTIONABLE"
            source_dir.mkdir()
            source = source_dir / "T-20260822-123456789.txt"
            source.write_text("STATUS: ACTIONABLE\n", encoding="utf-8")

            with self.assertRaises(ticket_mover.NestedLifecycleDestinationError):
                ticket_mover.move_ticket(source, base / "USER" / "decision")

            self.assertTrue(source.is_file())
            self.assertFalse((base / "USER").exists())

    def test_source_changed_during_move_aborts_without_deleting(self):
        """Schuetzt gegen einen fremden Schreiber, der die Quelle waehrend des
        Verschiebens noch aendert: Move bricht ab, Quelle bleibt (im neuen,
        geaenderten Zustand) erhalten, KEIN Ziel wird angelegt."""
        import contextlib
        import os

        real_open = os.open

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            source = base / "T-20260808-04.txt"
            source.write_text("ORIGINAL", encoding="utf-8")
            dest_dir = base / "SOLVED"

            def flaky_open(path, flags, *args, **kwargs):
                # Simuliert einen fremden Schreiber, der zwischen dem Lesen
                # der Quelle und dem Anlegen des Ziels zuschlaegt.
                if str(path) == str(source):
                    pass
                elif "SOLVED" in str(path):
                    source.write_text("VERAENDERT WAEHREND DES MOVES", encoding="utf-8")
                return real_open(path, flags, *args, **kwargs)

            with contextlib.suppress(Exception):
                os.open = flaky_open
                with self.assertRaises(RuntimeError):
                    ticket_mover.move_ticket(source, dest_dir)
            os.open = real_open

            self.assertTrue(source.is_file())
            self.assertEqual(source.read_text(encoding="utf-8"), "VERAENDERT WAEHREND DES MOVES")
            self.assertFalse((dest_dir / source.name).exists())


if __name__ == "__main__":
    unittest.main()
