"""Verifikation der Ticket-Rueckgabe (Claim-Freigabe) und der gehaerteten
Dateinamen-Grammatik.

Hintergrund (Messung am Live-Bestand 2026-08-15, 277 Ticketdateien):
110 Dateien wurden von TICKET_FILENAME_RE NICHT erkannt, weil die im Bestand
verbreitete Slug-Form "T-YYYYMMDD-NN_beschreibung.txt" einen UNTERSTRICH
zwischen Nummer und Slug traegt, das Muster aber einen Punkt verlangte.
Folge: 101 belegte Datum/Nummer-Paare waren fuer _next_number unsichtbar und
konnten ein zweites Mal vergeben werden -- genau die ID-Kollision aus
T-20260808-03, nur an einer anderen Stelle.

Die Tests unten halten beide Haelften fest: die Nummer gilt als belegt
(Slug hin oder her), aber der Slug ist NICHT Teil der kanonischen ID.
"""
import sys
import tempfile
import unittest
from pathlib import Path

LIB_DIR = Path(__file__).resolve().parent.parent / "lib"
sys.path.insert(0, str(LIB_DIR))

import ticket_mover  # noqa: E402
import ticket_writer  # noqa: E402


class TestFilenameGrammar(unittest.TestCase):
    """Die Grammatik entscheidet, welche Nummern als vergeben gelten."""

    def test_slug_form_is_recognised(self):
        m = ticket_writer.TICKET_FILENAME_RE.match(
            "T-20260612-01_promptboard-readme.txt")
        self.assertIsNotNone(m)
        self.assertEqual(m.group("date"), "20260612")
        # Rohtext, nicht int: die fuehrende Null bleibt erhalten, damit ein
        # freigegebener Name exakt so heisst wie vorher (T-20260612-01, nicht
        # T-20260612-1).
        self.assertEqual(m.group("number"), "01")
        self.assertEqual(m.group("slug"), "promptboard-readme")
        self.assertIsNone(m.group("suffix"))

    def test_slug_and_claim_suffix_together(self):
        m = ticket_writer.TICKET_FILENAME_RE.match(
            "T-20260620-29_taa-zenodo-erstupload.WORKSTATION-LG.txt")
        self.assertIsNotNone(m)
        self.assertEqual(m.group("slug"), "taa-zenodo-erstupload")
        self.assertEqual(m.group("suffix"), "WORKSTATION-LG")

    def test_plain_and_claimed_forms_still_match(self):
        plain = ticket_writer.TICKET_FILENAME_RE.match("T-20260808-03.txt")
        claimed = ticket_writer.TICKET_FILENAME_RE.match(
            "T-20260808-03.ASUS-GEI.txt")
        self.assertIsNotNone(plain)
        self.assertIsNotNone(claimed)
        self.assertIsNone(plain.group("suffix"))
        self.assertEqual(claimed.group("suffix"), "ASUS-GEI")

    def test_non_tickets_are_still_rejected(self):
        """Die Erweiterung darf den Clutter nicht mitschlucken: im Bestand
        liegen unter PENDING/ Reports und Skripte mit T-Praefix, die keine
        Tickets sind (kein 8-stelliges Datum bzw. falsche Endung)."""
        for name in (
            "T-41_LOESCH-REPORT.txt",       # keine 8-stellige Datumsgruppe
            "T-41_cleanup.ps1",
            "T-41_gnomad_transfer.sh",
            "T-20260612-01_notiz.md",       # kein .txt
            "TICKET.txt",
            "T-20260612.txt",               # keine laufende Nummer
        ):
            self.assertIsNone(
                ticket_writer.TICKET_FILENAME_RE.match(name),
                f"{name} darf NICHT als Ticket gelten")

    def test_slug_ticket_blocks_its_number(self):
        """Kern des Defekts: eine per Slug benannte Nummer muss als vergeben
        gelten, sonst kann sie ein zweites Mal gezogen werden. Der RNG liefert
        hier absichtlich zuerst genau die belegte Zahl."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "SOLVED").mkdir()
            (base / "SOLVED" / "T-20260612-123456789_promptboard-readme.txt").write_text(
                "alt", encoding="utf-8")

            class ScriptedRandom:
                def __init__(self, values):
                    self._values = list(values)

                def randrange(self, *_args, **_kwargs):
                    return self._values.pop(0)

            path = Path(ticket_writer.create(
                "Neu", "Body", tickets_dir=base, today="2026-06-12",
                rng=ScriptedRandom([123456789, 987654321])))
            self.assertEqual(path.name, "T-20260612-987654321.txt")

    def test_slug_is_not_part_of_the_id(self):
        """Der Slug belegt die Nummer, gehoert aber nicht zur ID -- sonst
        waeren zwei Dateien mit gleicher Nummer und verschiedenem Slug
        faelschlich zwei verschiedene Vorgaenge."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "SOLVED").mkdir()
            (base / "SOLVED" / "T-20260612-07_alpha.txt").write_text("a", encoding="utf-8")
            found = {
                f"T-{d}-{n:02d}"
                for _p, d, n, _s in ticket_writer.iter_lifecycle_files(base)
            }
            self.assertEqual(found, {"T-20260612-07"})


class TestReleaseSingleClaim(unittest.TestCase):
    def test_release_drops_host_suffix(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            claimed = base / "T-20260815-01.ASUS-GEI.txt"
            claimed.write_text("INHALT", encoding="utf-8")
            freed = ticket_mover.release_claim(claimed)
            self.assertEqual(freed.name, "T-20260815-01.txt")
            self.assertFalse(claimed.exists())
            self.assertEqual(freed.read_text(encoding="utf-8"), "INHALT")

    def test_release_keeps_the_slug(self):
        """Nur der Claim faellt, die beschreibende Benennung bleibt."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            claimed = base / "T-20260620-29_taa-zenodo.ASUS-GEI.txt"
            claimed.write_text("x", encoding="utf-8")
            freed = ticket_mover.release_claim(claimed)
            self.assertEqual(freed.name, "T-20260620-29_taa-zenodo.txt")

    def test_release_of_unclaimed_ticket_is_a_noop(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            plain = base / "T-20260815-02.txt"
            plain.write_text("x", encoding="utf-8")
            self.assertEqual(ticket_mover.release_claim(plain), plain)
            self.assertTrue(plain.is_file())

    def test_release_onto_existing_unclaimed_is_refused(self):
        """Auch die Rueckgabe ist fail-closed: liegt die unclaimed Fassung
        schon da (anderer Vorgang, gleiche Nummer), darf nichts ueberschrieben
        werden."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            claimed = base / "T-20260815-03.ASUS-GEI.txt"
            claimed.write_text("MEINER", encoding="utf-8")
            blocker = base / "T-20260815-03.txt"
            blocker.write_text("FREMDER VORGANG", encoding="utf-8")

            with self.assertRaises(ticket_mover.TicketCollisionError):
                ticket_mover.release_claim(claimed)

            self.assertEqual(claimed.read_text(encoding="utf-8"), "MEINER")
            self.assertEqual(blocker.read_text(encoding="utf-8"), "FREMDER VORGANG")


class TestReleaseSessionClaims(unittest.TestCase):
    def _queue(self, base: Path) -> None:
        for folder, name in (
            ("QUEUED", "T-20260815-01.ASUS-GEI.txt"),
            ("ACTIONABLE", "T-20260815-02.ASUS-GEI.txt"),
            ("ACTIONABLE", "T-20260815-03.WORKSTATION-LG.txt"),
            ("ACTIONABLE", "T-20260815-04.LAPTOP.txt"),
            ("SOLVED", "T-20260815-05.ASUS-GEI.txt"),
            ("USER", "T-20260815-06.ASUS-GEI.txt"),
            ("BLOCKED", "T-20260815-07.ASUS-GEI.txt"),
            ("WAITING", "T-20260815-08.ASUS-GEI.txt"),
            ("PARKED", "T-20260815-09.ASUS-GEI.txt"),
            ("QUEUED", "T-20260815-10.txt"),
        ):
            d = base / folder
            d.mkdir(parents=True, exist_ok=True)
            (d / name).write_text(name, encoding="utf-8")

    def test_releases_only_own_host_in_working_folders(self):
        """T-20260815-205002196: QUEUED ist standardmaessig NICHT dabei --
        nur ACTIONABLE wird bedingungslos freigegeben. QUEUED bleibt
        geclaimed und wird nur gemeldet (siehe test_queued_is_held_by_default
        weiter unten), sonst wuerde eine aktive Delegation blind freigegeben."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            self._queue(base)
            freed = ticket_mover.release_claims(base, host="ASUS-GEI")

            self.assertEqual(
                sorted(p.name for p in freed),
                ["T-20260815-02.txt"])
            self.assertTrue(
                (base / "QUEUED" / "T-20260815-01.ASUS-GEI.txt").is_file(),
                "QUEUED darf standardmaessig NICHT freigegeben werden")
            self.assertTrue((base / "ACTIONABLE" / "T-20260815-02.txt").is_file())

    def test_foreign_claims_are_never_touched(self):
        """Der Beleg gegen den gefaehrlichsten Fehlgriff: von hier aus darf
        kein Claim eines anderen Hosts fallen. LAPTOP ist bewusst dabei --
        es ist zwar historisch dieselbe Maschine wie ASUS-GEI, aber eine
        Normalisierung veralteter Identitaeten ist ein separater, benannter
        Vorgang und darf nicht still in der Rueckgabe passieren."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            self._queue(base)
            ticket_mover.release_claims(base, host="ASUS-GEI")
            self.assertTrue(
                (base / "ACTIONABLE" / "T-20260815-03.WORKSTATION-LG.txt").is_file())
            self.assertTrue(
                (base / "ACTIONABLE" / "T-20260815-04.LAPTOP.txt").is_file())

    def test_waiting_clusters_keep_their_claim_as_provenance(self):
        """In SOLVED/USER/BLOCKED/WAITING/PARKED ist der Host-Suffix kein
        'in Arbeit', sondern Herkunft: wer hat geloest, wer wartet auf wessen
        Receipt. Eine pauschale Rueckgabe wuerde diese Information loeschen."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            self._queue(base)
            ticket_mover.release_claims(base, host="ASUS-GEI")
            for folder, name in (
                ("SOLVED", "T-20260815-05.ASUS-GEI.txt"),
                ("USER", "T-20260815-06.ASUS-GEI.txt"),
                ("BLOCKED", "T-20260815-07.ASUS-GEI.txt"),
                ("WAITING", "T-20260815-08.ASUS-GEI.txt"),
                ("PARKED", "T-20260815-09.ASUS-GEI.txt"),
            ):
                self.assertTrue((base / folder / name).is_file(),
                                f"{folder}/{name} haette stehen bleiben muessen")

    def test_host_argument_is_required(self):
        """Kein stiller COMPUTERNAME-Default: wer freigibt, benennt den Host.
        Sonst gibt eine falsch konfigurierte Umgebung fremde Claims frei."""
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(TypeError):
            ticket_mover.release_claims(Path(tmp))  # type: ignore[call-arg]

    def test_dry_run_reports_without_changing_anything(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            self._queue(base)
            planned = ticket_mover.release_claims(base, host="ASUS-GEI", dry_run=True)
            self.assertEqual([p.name for p in planned], ["T-20260815-02.txt"])
            self.assertTrue((base / "QUEUED" / "T-20260815-01.ASUS-GEI.txt").is_file())
            self.assertTrue((base / "ACTIONABLE" / "T-20260815-02.ASUS-GEI.txt").is_file())

    def test_collision_does_not_abort_the_whole_release(self):
        """Ein blockiertes Ticket darf die Rueckgabe der uebrigen nicht
        verhindern -- sonst bleibt eine ganze Session geclaimed, weil ein
        einziger Name belegt war. include_queued=True, damit das blockierte
        QUEUED-Ticket ueberhaupt einen Freigabeversuch erlebt."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            self._queue(base)
            (base / "QUEUED" / "T-20260815-01.txt").write_text("blockiert", encoding="utf-8")

            freed, refused, held = ticket_mover.release_claims(
                base, host="ASUS-GEI", include_queued=True, report_refused=True)

            self.assertEqual(sorted(p.name for p in freed), ["T-20260815-02.txt"])
            self.assertEqual(len(refused), 1)
            self.assertEqual(held, [])
            self.assertTrue((base / "QUEUED" / "T-20260815-01.ASUS-GEI.txt").is_file())


class TestQueuedIsHeldNotBlindlyReleased(unittest.TestCase):
    """T-20260815-205002196: QUEUED = 'an einen Agenten uebergeben, Ergebnis
    aussteht' -- da arbeitet moeglicherweise noch jemand, auch wenn DIESE
    Rueckgabe von einem anderen (z. B. gerade beendeten) Prozess desselben
    Hosts aufgerufen wird. Belegter Fall: der Dry-Run vom 2026-08-15 haette
    ein Ticket freigegeben, an dem ein Subagent aktiv arbeitete."""

    def _one_queued(self, base: Path, host: str = "ASUS-GEI") -> Path:
        d = base / "QUEUED"
        d.mkdir(parents=True, exist_ok=True)
        ticket = d / f"T-20260815-01.{host}.txt"
        ticket.write_text("INHALT", encoding="utf-8")
        return ticket

    def test_queued_is_held_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            ticket = self._one_queued(base)
            freed, refused, held = ticket_mover.release_claims(
                base, host="ASUS-GEI", report_refused=True)
            self.assertEqual(freed, [])
            self.assertEqual(refused, [])
            self.assertEqual(len(held), 1)
            self.assertEqual(held[0][0], ticket)
            self.assertIn("not included", held[0][1])
            self.assertTrue(ticket.is_file(), "darf nicht freigegeben worden sein")

    def test_include_queued_releases_orphaned_queued_ticket(self):
        """Kein DELEGIERT_AN-Vermerk -> gilt als verwaist (Worker/Session ist
        weg) -> darf mit --include-queued freigegeben werden."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            self._one_queued(base)
            freed, refused, held = ticket_mover.release_claims(
                base, host="ASUS-GEI", include_queued=True, report_refused=True)
            self.assertEqual([p.name for p in freed], ["T-20260815-01.txt"])
            self.assertEqual(held, [])

    def test_actively_delegated_queued_ticket_is_never_released(self):
        """Der eigentliche Kernfall aus dem Ticket: ein frischer
        DELEGIERT_AN-Vermerk schuetzt das Ticket, SELBST mit
        include_queued=True."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            ticket = self._one_queued(base)
            ticket_mover.mark_delegated(ticket, "claude-code@ASUS-GEI")

            freed, refused, held = ticket_mover.release_claims(
                base, host="ASUS-GEI", include_queued=True, report_refused=True)

            self.assertEqual(freed, [])
            self.assertEqual(len(held), 1)
            self.assertIn("active delegation", held[0][1])
            self.assertTrue(ticket.is_file())
            self.assertIn("DELEGIERT_AN: claude-code@ASUS-GEI",
                           ticket.read_text(encoding="utf-8"))

    def test_stale_delegation_marker_does_not_block_release(self):
        """Sicherheitsnetz: ein Vermerk ohne Frische (Worker abgestuerzt, nie
        aktualisiert) darf einen Claim nicht fuer immer schuetzen."""
        import os

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            ticket = self._one_queued(base)
            ticket_mover.mark_delegated(ticket, "claude-code@ASUS-GEI")
            # mtime kuenstlich auf "vor 7 Stunden" setzen (Default-Schwelle: 6h).
            old = ticket.stat().st_mtime - 7 * 3600
            os.utime(ticket, (old, old))

            freed, refused, held = ticket_mover.release_claims(
                base, host="ASUS-GEI", include_queued=True, report_refused=True)

            self.assertEqual([p.name for p in freed], ["T-20260815-01.txt"])
            self.assertEqual(held, [])

    def test_dry_run_reports_queued_candidate_without_touching_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            ticket = self._one_queued(base)
            freed, refused, held = ticket_mover.release_claims(
                base, host="ASUS-GEI", dry_run=True, report_refused=True)
            self.assertEqual(freed, [])
            self.assertEqual(len(held), 1)
            self.assertTrue(ticket.is_file())

    def test_foreign_host_queued_ticket_is_never_touched(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            self._one_queued(base, host="WORKSTATION-LG")
            freed, refused, held = ticket_mover.release_claims(
                base, host="ASUS-GEI", include_queued=True, report_refused=True)
            self.assertEqual(freed, [])
            self.assertEqual(held, [])
            self.assertTrue(
                (base / "QUEUED" / "T-20260815-01.WORKSTATION-LG.txt").is_file())


class TestDelegationMarker(unittest.TestCase):
    """is_actively_delegated() / mark_delegated() als eigenstaendige
    Bausteine, unabhaengig von release_claims()."""

    def test_mark_then_check_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            ticket = Path(tmp) / "T-20260815-01.ASUS-GEI.txt"
            ticket.write_text("VORGANG\n", encoding="utf-8")
            self.assertFalse(ticket_mover.is_actively_delegated(ticket))
            ticket_mover.mark_delegated(ticket, "claude-code@ASUS-GEI")
            self.assertTrue(ticket_mover.is_actively_delegated(ticket))

    def test_repeated_marking_does_not_duplicate_the_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            ticket = Path(tmp) / "T-20260815-01.ASUS-GEI.txt"
            ticket.write_text("VORGANG\n", encoding="utf-8")
            ticket_mover.mark_delegated(ticket, "claude-code@ASUS-GEI")
            ticket_mover.mark_delegated(ticket, "claude-code@ASUS-GEI")
            text = ticket.read_text(encoding="utf-8")
            self.assertEqual(text.count("DELEGIERT_AN:"), 1)

    def test_marking_missing_ticket_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(FileNotFoundError):
                ticket_mover.mark_delegated(Path(tmp) / "nope.txt", "x")

    def test_missing_ticket_is_not_actively_delegated(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertFalse(
                ticket_mover.is_actively_delegated(Path(tmp) / "nope.txt"))


class TestRenameForRenumbering(unittest.TestCase):
    """Umnummerierung bei einer ID-Kollision braucht einen Rename, der
    dieselbe fail-closed-Garantie hat wie das Verschieben."""

    def test_rename_within_same_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            source = base / "T-20260731-02.WORKSTATION-LG.txt"
            source.write_text("VORGANG", encoding="utf-8")
            target = ticket_mover.move_ticket(
                source, base, new_name="T-20260731-24.WORKSTATION-LG.txt")
            self.assertEqual(target.name, "T-20260731-24.WORKSTATION-LG.txt")
            self.assertFalse(source.exists())
            self.assertEqual(target.read_text(encoding="utf-8"), "VORGANG")

    def test_rename_onto_occupied_name_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            source = base / "T-20260731-02.WORKSTATION-LG.txt"
            source.write_text("MEINER", encoding="utf-8")
            (base / "T-20260731-24.WORKSTATION-LG.txt").write_text(
                "FREMDER", encoding="utf-8")
            with self.assertRaises(ticket_mover.TicketCollisionError):
                ticket_mover.move_ticket(
                    source, base, new_name="T-20260731-24.WORKSTATION-LG.txt")
            self.assertEqual(source.read_text(encoding="utf-8"), "MEINER")

    def test_new_name_wins_over_reactivation_release(self):
        """Sonst wuerde ein Umnummerieren aus einem Wartezustand heraus den
        uebergebenen Namen still verkuerzen."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            src_dir = base / "BLOCKED"
            src_dir.mkdir()
            source = src_dir / "T-20260731-02.WORKSTATION-LG.txt"
            source.write_text("x", encoding="utf-8")
            target = ticket_mover.move_ticket(
                source, base / "ACTIONABLE",
                new_name="T-20260731-24.WORKSTATION-LG.txt")
            self.assertEqual(target.name, "T-20260731-24.WORKSTATION-LG.txt")


class TestBareClusterNameAsDestination(unittest.TestCase):
    """Belegt am 2026-08-15: ein Worker rief move_ticket(src, "SOLVED") mit
    dem blossen Clusternamen auf. Das legte still ein Verzeichnis "SOLVED"
    im Arbeitsverzeichnis an (dort: ticket-master/lib/SOLVED/) und das Ticket
    verschwand aus der Queue -- ohne Fehlermeldung. Genau die Sorte stiller
    Fehlablage, die dieses Modul verhindern soll."""

    def test_bare_cluster_name_resolves_against_the_queue_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "QUEUED").mkdir()
            source = base / "QUEUED" / "T-20260815-111111111.ASUS-GEI.txt"
            source.write_text("x", encoding="utf-8")

            target = ticket_mover.move_ticket(source, "SOLVED")

            self.assertEqual(target.parent, base / "SOLVED")
            self.assertTrue(target.is_file())
            self.assertFalse((Path.cwd() / "SOLVED").exists(),
                             "darf keinen Ordner im Arbeitsverzeichnis anlegen")

    def test_bare_cluster_name_works_from_the_queue_root_too(self):
        """Liegt die Quelle direkt in der Wurzel (INBOX-Alias), ist die Wurzel
        ihr eigener Elternordner."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            source = base / "T-20260815-222222222.txt"
            source.write_text("x", encoding="utf-8")
            target = ticket_mover.move_ticket(source, "ACTIONABLE")
            self.assertEqual(target.parent, base / "ACTIONABLE")

    def test_unknown_relative_name_is_left_alone(self):
        """Nur BEKANNTE Clusternamen werden aufgeloest. Ein beliebiger
        relativer Pfad bleibt relativ -- sonst wuerde die Bequemlichkeit zur
        Magie und ueberraeschte an anderer Stelle."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "QUEUED").mkdir()
            source = base / "QUEUED" / "T-20260815-333333333.txt"
            source.write_text("x", encoding="utf-8")
            target = ticket_mover.move_ticket(source, base / "irgendwas-eigenes")
            self.assertEqual(target.parent, base / "irgendwas-eigenes")

    def test_absolute_paths_are_unaffected(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "QUEUED").mkdir()
            source = base / "QUEUED" / "T-20260815-444444444.txt"
            source.write_text("x", encoding="utf-8")
            target = ticket_mover.move_ticket(source, base / "SOLVED")
            self.assertEqual(target.parent, base / "SOLVED")


class TestReleaseOnReactivation(unittest.TestCase):
    """Wunsch des Nutzers (2026-08-15): wartende Tickets sollen uebernehmbar
    werden, SOBALD sie wieder aktuell sind -- nicht schon im Wartezustand."""

    def test_blocked_to_actionable_drops_the_claim(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            src_dir = base / "BLOCKED"
            src_dir.mkdir()
            source = src_dir / "T-20260815-01.WORKSTATION-LG.txt"
            source.write_text("entblockt", encoding="utf-8")

            target = ticket_mover.move_ticket(source, base / "ACTIONABLE")

            self.assertEqual(target.name, "T-20260815-01.txt")
            self.assertEqual(target.parent.name, "ACTIONABLE")
            self.assertFalse(source.exists())

    def test_waiting_user_parked_to_actionable_also_release(self):
        for cluster in ("WAITING", "USER", "PARKED"):
            with tempfile.TemporaryDirectory() as tmp:
                base = Path(tmp)
                src_dir = base / cluster
                src_dir.mkdir()
                source = src_dir / "T-20260815-02.WORKSTATION-LG.txt"
                source.write_text("x", encoding="utf-8")
                target = ticket_mover.move_ticket(source, base / "ACTIONABLE")
                self.assertEqual(target.name, "T-20260815-02.txt", cluster)

    def test_other_transitions_keep_the_claim(self):
        """Nur die Reaktivierung gibt frei. ACTIONABLE->SOLVED behaelt den
        Claim (Herkunft), QUEUED->ACTIONABLE ebenfalls (derselbe Host faellt
        auf seine eigene Fallback-Kette zurueck, das ist keine Freigabe)."""
        for src_cluster, dest_cluster in (
            ("ACTIONABLE", "SOLVED"),
            ("QUEUED", "ACTIONABLE"),
            ("ACTIONABLE", "BLOCKED"),
            ("INBOX", "ACTIONABLE"),
        ):
            with tempfile.TemporaryDirectory() as tmp:
                base = Path(tmp)
                src_dir = base / src_cluster
                src_dir.mkdir()
                source = src_dir / "T-20260815-03.ASUS-GEI.txt"
                source.write_text("x", encoding="utf-8")
                target = ticket_mover.move_ticket(source, base / dest_cluster)
                self.assertEqual(target.name, "T-20260815-03.ASUS-GEI.txt",
                                 f"{src_cluster}->{dest_cluster}")

    def test_reactivation_release_can_be_overridden(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            src_dir = base / "BLOCKED"
            src_dir.mkdir()
            source = src_dir / "T-20260815-04.ASUS-GEI.txt"
            source.write_text("x", encoding="utf-8")
            target = ticket_mover.move_ticket(
                source, base / "ACTIONABLE", release_claim=False)
            self.assertEqual(target.name, "T-20260815-04.ASUS-GEI.txt")

    def test_reactivation_falls_back_to_claimed_name_on_collision(self):
        """Liegt die unclaimed Fassung im Ziel schon, darf die Reaktivierung
        nicht scheitern und auch nichts ueberschreiben: das Ticket wandert
        dann unter seinem geclaimten Namen und bleibt sichtbar."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            src_dir = base / "BLOCKED"
            src_dir.mkdir()
            source = src_dir / "T-20260815-05.WORKSTATION-LG.txt"
            source.write_text("meiner", encoding="utf-8")
            dest = base / "ACTIONABLE"
            dest.mkdir()
            (dest / "T-20260815-05.txt").write_text("fremder", encoding="utf-8")

            target = ticket_mover.move_ticket(source, dest)

            self.assertEqual(target.name, "T-20260815-05.WORKSTATION-LG.txt")
            self.assertEqual((dest / "T-20260815-05.txt").read_text(encoding="utf-8"),
                             "fremder")


if __name__ == "__main__":
    unittest.main()
