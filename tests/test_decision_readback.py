"""T-20260913-867541218: Readback-Gate vor der Eskalation nach USER/.

Der Lehrfall aus dem Ticket, nachgebaut: T-20260824-857836410 wurde am 25.08.
als offen gemeldet, obwohl D-20260731-010 den Entscheid seit dem 31.07. trug.
Ein Gate, das den Index liest, haette den Move verweigert.
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

LIB = Path(__file__).resolve().parent.parent / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

import decision_readback as dr  # noqa: E402
import ticket_mover  # noqa: E402
from queue_helpers import verified_queue  # noqa: E402


def _index(path: Path, entries: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"schema": "decisions.index/1", "entries": entries}),
                    encoding="utf-8")
    return path


def _queue(tmp: Path) -> Path:
    """Eine Queue mit dem echten Nachbarschaftsverhaeltnis zum Register:
    <control-center>/_TICKETS neben <control-center>/_CONTROL/_DECISIONS."""
    queue = tmp / "_control-center" / "_TICKETS"
    queue.mkdir(parents=True, exist_ok=True)
    verified_queue(queue)
    return queue


def _ticket(queue: Path, folder: str, name: str, body: str = "") -> Path:
    path = queue / folder / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "=" * 62 + "\nTICKET\n" + "=" * 62 + "\n"
        f"ID:            {name.split('.')[0]}\n"
        f"TITEL:         Beispiel\n"
        f"STATUS:        {folder} (seit 2026-09-13)\n\n"
        f"{body}\n\n"
        + "-" * 62 + "\nVERLAUF / LOG\n" + "-" * 62 + "\n2026-09-13  Aufgenommen.\n",
        encoding="utf-8")
    return path


class TestReadback(unittest.TestCase):
    def test_the_teaching_case_is_caught(self):
        """Ticket nennt D-20260731-010, das Register fuehrt es als DONE ->
        die Eskalation nach USER/ wird verweigert, das Ticket bleibt liegen."""
        with tempfile.TemporaryDirectory() as tmp:
            queue = _queue(Path(tmp))
            _index(queue.parent / dr.INDEX_RELATIVE_PATH, [{
                "key": "D-20260731-010", "id": "D-20260731-010",
                "status_class": "DONE", "title": "E8/F17 entschieden",
                "source_file": "DECIDED-AND-DONE.md",
            }])
            ticket = _ticket(queue, "ACTIONABLE", "T-20260824-857836410.txt",
                             "Offen laut Analyse: E8/F17, siehe D-20260731-010.")
            before = ticket.read_bytes()
            with self.assertRaises(ticket_mover.DecisionReadbackError) as refused:
                ticket_mover.move_ticket(ticket, queue / "USER")
            self.assertIn("D-20260731-010", str(refused.exception))
            self.assertTrue(ticket.exists())
            self.assertEqual(ticket.read_bytes(), before)
            self.assertFalse((queue / "USER" / ticket.name).exists())

    def test_lowercase_user_target_triggers_readback_gate(self):
        """T-20261107-711141176: Ziel 'user' oder path / 'user' muss das Gate ebenso triggern."""
        with tempfile.TemporaryDirectory() as tmp:
            queue = _queue(Path(tmp))
            _index(queue.parent / dr.INDEX_RELATIVE_PATH, [{
                "key": "D-20260731-010", "id": "D-20260731-010",
                "status_class": "DONE", "title": "Entschieden",
                "source_file": "DECIDED-AND-DONE.md",
            }])
            ticket = _ticket(queue, "ACTIONABLE", "T-20260824-857836410.txt",
                             "Offen laut Analyse: siehe D-20260731-010.")
            before = ticket.read_bytes()
            with self.assertRaises(ticket_mover.DecisionReadbackError):
                ticket_mover.move_ticket(ticket, queue / "user")
            self.assertEqual(ticket.read_bytes(), before)

            with self.assertRaises(ticket_mover.DecisionReadbackError):
                ticket_mover.move_ticket(ticket, "user")
            self.assertEqual(ticket.read_bytes(), before)

    def test_junction_queue_finds_index_via_resolve_fallback(self):
        """T-20261107-711141176: Queue hinter Junction/Symlink findet den Index."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            cc = tmp_path / "control-center"
            queue = cc / "_TICKETS"
            queue.mkdir(parents=True, exist_ok=True)
            verified_queue(queue)
            _index(cc / dr.INDEX_RELATIVE_PATH, [{
                "key": "D-20260731-010", "id": "D-20260731-010",
                "status_class": "DONE", "title": "Entschieden",
                "source_file": "DECIDED-AND-DONE.md",
            }])
            junction = tmp_path / "junction_queue"
            try:
                import _winapi
                _winapi.CreateJunction(str(queue), str(junction))
            except (ImportError, OSError):
                try:
                    import os
                    os.symlink(queue, junction, target_is_directory=True)
                except OSError:
                    self.skipTest("weder Junction noch Symlink verfuegbar")

            path, expected = dr.find_decisions_index(junction)
            self.assertIsNotNone(path, "Index muss ueber aufgeloeste Junction gefunden werden")
            self.assertTrue(expected)
            self.assertEqual(path.resolve(), (cc / dr.INDEX_RELATIVE_PATH).resolve())

    def test_an_acknowledged_hit_lets_the_move_through_and_is_recorded(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue = _queue(Path(tmp))
            _index(queue.parent / dr.INDEX_RELATIVE_PATH, [{
                "key": "D-20260731-010", "id": "D-20260731-010",
                "status_class": "DONE", "title": "Etwas anderes",
                "source_file": "DECIDED-AND-DONE.md",
            }])
            ticket = _ticket(queue, "ACTIONABLE", "T-20260824-857836410.txt",
                             "Bezug am Rande: D-20260731-010.")
            moved = ticket_mover.move_ticket(
                ticket, queue / "USER",
                acknowledged_decisions=["D-20260731-010"])
            text = moved.read_text(encoding="utf-8")
            self.assertIn("Entscheidungs-Readback: TREFFER", text)
            self.assertIn("nicht einschlaegig quittiert: D-20260731-010", text)

    def test_no_hit_is_recorded_as_not_found_never_as_open(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue = _queue(Path(tmp))
            _index(queue.parent / dr.INDEX_RELATIVE_PATH, [{
                "key": "D-20260101-001", "id": "D-20260101-001",
                "status_class": "DONE", "title": "Unbeteiligt",
                "source_file": "DECIDED-AND-DONE.md",
            }])
            ticket = _ticket(queue, "ACTIONABLE", "T-20260913-000000001.txt")
            moved = ticket_mover.move_ticket(ticket, queue / "USER")
            self.assertIn("Entscheidungs-Readback: NICHT GEFUNDEN",
                          moved.read_text(encoding="utf-8"))

    def test_a_named_but_unreadable_index_refuses_the_move(self):
        """Wer den Index benennt, meint ihn. Dann ist Nichtlesbarkeit ein
        Blocker, keine Achselzuckerei."""
        with tempfile.TemporaryDirectory() as tmp:
            queue = _queue(Path(tmp))
            ticket = _ticket(queue, "ACTIONABLE", "T-20260913-000000001.txt")
            with self.assertRaises(ticket_mover.DecisionReadbackError) as refused:
                ticket_mover.move_ticket(ticket, queue / "USER",
                                         decisions_index=Path(tmp) / "weg.json")
            self.assertIn("unreadable", str(refused.exception))
            self.assertTrue(ticket.exists())

    def test_without_any_index_the_move_passes_but_says_it_is_unverified(self):
        """Das Werkzeug ist oeffentlich; ein fremder Nutzer hat kein Register.
        Jede Eskalation zu blockieren waere falsch -- stillschweigend 'offen'
        zu behaupten aber genau der Mangel dieses Tickets."""
        with tempfile.TemporaryDirectory() as tmp:
            queue = _queue(Path(tmp))
            ticket = _ticket(queue, "ACTIONABLE", "T-20260913-000000001.txt")
            moved = ticket_mover.move_ticket(ticket, queue / "USER")
            text = moved.read_text(encoding="utf-8")
            self.assertIn("Entscheidungs-Readback UNGEPRUEFT", text)
            self.assertIn("unbelegte Annahme", text)

    def test_an_open_register_entry_does_not_block(self):
        """Ein noch unbeantworteter Eintrag ist kein erledigter Entscheid.
        Er wird protokolliert, blockiert aber nicht -- sonst koennte eine
        Nachfrage zum selben Thema nie mehr gestellt werden."""
        with tempfile.TemporaryDirectory() as tmp:
            queue = _queue(Path(tmp))
            _index(queue.parent / dr.INDEX_RELATIVE_PATH, [{
                "key": "D-20260912-001", "id": "D-20260912-001",
                "status_class": "OFFEN", "title": "Noch offen",
                "source_file": "TO-DECIDE-USER.txt",
            }])
            ticket = _ticket(queue, "ACTIONABLE", "T-20260913-000000001.txt",
                             "Siehe D-20260912-001.")
            moved = ticket_mover.move_ticket(ticket, queue / "USER")
            self.assertIn("TREFFER", moved.read_text(encoding="utf-8"))

    def test_the_ticket_id_itself_is_a_search_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue = _queue(Path(tmp))
            _index(queue.parent / dr.INDEX_RELATIVE_PATH, [{
                "key": "D-20260901-004", "id": "D-20260901-004",
                "status_class": "DONE", "title": "Antwort",
                "source_excerpt": "Quelle: Ticket T-20260913-000000001",
                "source_file": "DECIDED-AND-DONE.md",
            }])
            ticket = _ticket(queue, "ACTIONABLE", "T-20260913-000000001.txt")
            with self.assertRaises(ticket_mover.DecisionReadbackError):
                ticket_mover.move_ticket(ticket, queue / "USER")

    def test_moves_that_are_not_escalations_are_untouched(self):
        """Das Gate gilt der Eskalation NACH USER/, nicht jedem Move -- und
        nicht dem Rename innerhalb von USER/ (claim_contract tut genau das)."""
        with tempfile.TemporaryDirectory() as tmp:
            queue = _queue(Path(tmp))
            _index(queue.parent / dr.INDEX_RELATIVE_PATH, [{
                "key": "D-20260731-010", "id": "D-20260731-010",
                "status_class": "DONE", "title": "Entschieden",
                "source_file": "DECIDED-AND-DONE.md",
            }])
            # Rueckweg USER -> ACTIONABLE: kein Gate.
            back = _ticket(queue, "USER", "T-20260913-000000002.txt",
                           "Bezug D-20260731-010.")
            self.assertTrue(ticket_mover.move_ticket(back, queue / "ACTIONABLE").exists())
            # Rename innerhalb von USER/: kein Gate.
            inside = _ticket(queue, "USER", "T-20260913-000000003.txt",
                             "Bezug D-20260731-010.")
            renamed = ticket_mover.move_ticket(
                inside, queue / "USER", new_name="T-20260913-000000003.ASUS-GEI.txt")
            self.assertEqual(renamed.name, "T-20260913-000000003.ASUS-GEI.txt")

    def test_dry_run_never_writes_the_protocol_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue = _queue(Path(tmp))
            ticket = _ticket(queue, "ACTIONABLE", "T-20260913-000000001.txt")
            before = ticket.read_bytes()
            ticket_mover.move_ticket(ticket, queue / "USER", dry_run=True)
            self.assertEqual(ticket.read_bytes(), before)
            self.assertFalse((queue / "USER").exists())


class TestReadbackCli(unittest.TestCase):
    def test_reporting_mode_writes_nothing_and_signals_unavailable(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue = _queue(Path(tmp))
            ticket = _ticket(queue, "ACTIONABLE", "T-20260913-000000001.txt")
            before = ticket.read_bytes()
            self.assertEqual(ticket_mover._cli([
                "--decision-readback", str(ticket), "--tickets-dir", str(queue)]), 0)
            self.assertEqual(ticket_mover._cli([
                "--decision-readback", str(ticket), "--tickets-dir", str(queue),
                "--decisions-index", str(Path(tmp) / "weg.json")]), 1)
            self.assertEqual(ticket.read_bytes(), before)

    def test_cli_refuses_the_escalation_and_reports_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue = _queue(Path(tmp))
            _index(queue.parent / dr.INDEX_RELATIVE_PATH, [{
                "key": "D-20260731-010", "id": "D-20260731-010",
                "status_class": "DONE", "title": "Entschieden",
                "source_file": "DECIDED-AND-DONE.md",
            }])
            ticket = _ticket(queue, "ACTIONABLE", "T-20260913-000000001.txt",
                             "Bezug D-20260731-010.")
            self.assertEqual(
                ticket_mover._cli([str(ticket), str(queue / "USER")]), 1)
            self.assertTrue(ticket.exists())
            self.assertEqual(ticket_mover._cli([
                str(ticket), str(queue / "USER"),
                "--acknowledge-decision", "D-20260731-010"]), 0)


class TestReadbackRelativePaths(unittest.TestCase):
    """T-20261107-456970984: Mit RELATIVEM Ticketpfad meldete --decision-readback
    'ABSENT, index: -', obwohl der Index neben der Queue lag. Path('ACTIONABLE/T.txt')
    .parent.parent ist '.', und '.'.parent bleibt '.' -- gesucht wurde dann unter
    <Queue>/_CONTROL statt <control-center>/_CONTROL. Ein falsches 'absent' ist
    genau das 'nicht gefunden', das als 'offen' gelesen wird."""

    def _run_cli(self, cwd: Path, ticket_arg: str) -> tuple[int, str]:
        import contextlib
        import io
        import os
        old = os.getcwd()
        out = io.StringIO()
        try:
            os.chdir(cwd)
            with contextlib.redirect_stdout(out):
                code = ticket_mover._cli(["--decision-readback", ticket_arg])
        finally:
            os.chdir(old)
        return code, out.getvalue()

    def _setup(self, tmp: Path) -> tuple[Path, Path]:
        queue = _queue(tmp)
        _index(queue.parent / dr.INDEX_RELATIVE_PATH, [{
            "key": "D-20260731-010", "id": "D-20260731-010",
            "status_class": "DONE", "title": "Entschieden",
            "source_file": "DECIDED-AND-DONE.md",
        }])
        ticket = _ticket(queue, "ACTIONABLE", "T-20260913-000000001.txt",
                         "Bezug D-20260731-010.")
        return queue, ticket

    def test_relative_ticket_path_from_the_queue_finds_the_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue, ticket = self._setup(Path(tmp))
            code, out = self._run_cli(queue, "ACTIONABLE/" + ticket.name)
            self.assertEqual(code, 0)
            self.assertIn("DECISION READBACK CHECKED", out)
            self.assertIn("[BLOCKING] D-20260731-010", out)

    def test_relative_ticket_path_from_the_lifecycle_folder_finds_the_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue, ticket = self._setup(Path(tmp))
            code, out = self._run_cli(queue / "ACTIONABLE", ticket.name)
            self.assertEqual(code, 0)
            self.assertIn("DECISION READBACK CHECKED", out)
            self.assertIn("[BLOCKING] D-20260731-010", out)

    def test_a_ticket_in_the_queue_root_finds_the_index_too(self):
        """INBOX-Alias: Tickets direkt in der Queue-Wurzel. parent.parent zeigte
        dort schon mit absolutem Pfad ueber das Control-Center hinaus."""
        with tempfile.TemporaryDirectory() as tmp:
            queue, _ = self._setup(Path(tmp))
            root_ticket = _ticket(queue, ".", "T-20260913-000000002.txt", "Bezug D-20260731-010.")
            code, out = self._run_cli(queue, root_ticket.name)
            self.assertEqual(code, 0)
            self.assertIn("[BLOCKING] D-20260731-010", out)

    def test_relative_queue_dir_is_resolved_before_deriving_the_index(self):
        import os
        with tempfile.TemporaryDirectory() as tmp:
            queue, _ = self._setup(Path(tmp))
            old = os.getcwd()
            try:
                os.chdir(queue)
                path, _named = dr.find_decisions_index(".")
            finally:
                os.chdir(old)
            self.assertIsNotNone(path)
            self.assertTrue(path.is_file())

    def test_negative_control_no_register_stays_absent(self):
        """Ohne Register neben der Queue bleibt es 'absent' -- das oeffentliche
        Werkzeug darf Nutzer ohne Register nicht blockieren."""
        with tempfile.TemporaryDirectory() as tmp:
            queue = _queue(Path(tmp))
            ticket = _ticket(queue, "ACTIONABLE", "T-20260913-000000001.txt")
            code, out = self._run_cli(queue, "ACTIONABLE/" + ticket.name)
            self.assertEqual(code, 0)
            self.assertIn("DECISION READBACK ABSENT", out)

    def test_register_present_but_index_missing_is_unavailable_not_absent(self):
        """Haertung: Liegt neben der Queue ein Register (_CONTROL/_DECISIONS),
        fehlt aber dessen Index, ist das ein kaputtes Register, kein fehlendes.
        Dann 'unavailable' (Exit 1, Eskalation verweigert), nie still 'absent'."""
        with tempfile.TemporaryDirectory() as tmp:
            queue = _queue(Path(tmp))
            (queue.parent / dr.INDEX_RELATIVE_PATH.parent.parent).mkdir(parents=True)
            ticket = _ticket(queue, "ACTIONABLE", "T-20260913-000000001.txt")
            code, out = self._run_cli(queue, "ACTIONABLE/" + ticket.name)
            self.assertEqual(code, 1)
            self.assertIn("DECISION READBACK UNAVAILABLE", out)
            with self.assertRaises(ticket_mover.DecisionReadbackError):
                ticket_mover.move_ticket(ticket, queue / "USER")
            self.assertTrue(ticket.exists())

    def test_move_gate_with_relative_destination_finds_the_index(self):
        import os
        with tempfile.TemporaryDirectory() as tmp:
            queue, ticket = self._setup(Path(tmp))
            old = os.getcwd()
            try:
                os.chdir(queue / "ACTIONABLE")
                with self.assertRaises(ticket_mover.DecisionReadbackError):
                    ticket_mover.move_ticket(Path(ticket.name), Path("..") / "USER")
            finally:
                os.chdir(old)
            self.assertTrue(ticket.exists())


if __name__ == "__main__":
    unittest.main()


class TestKeyMatchingIsWholeKey(unittest.TestCase):
    def test_a_short_id_does_not_match_inside_a_longer_one(self):
        """Die alte zweistellige Form ist ein Praefix der neunstelligen:
        T-20260808-03 steckt buchstaeblich in T-20260808-031234567. Ein
        Teilstring-Test blockiert damit eine berechtigte Eskalation -- und
        bringt dem Naechsten bei, --acknowledge-decision ungelesen zu tippen."""
        with tempfile.TemporaryDirectory() as tmp:
            queue = _queue(Path(tmp))
            _index(queue.parent / dr.INDEX_RELATIVE_PATH, [{
                "key": "D-20260901-004", "id": "D-20260901-004",
                "status_class": "DONE", "title": "Fremder Vorgang",
                "source_excerpt": "Quelle: Ticket T-20260808-031234567",
                "source_file": "DECIDED-AND-DONE.md",
            }])
            ticket = _ticket(queue, "ACTIONABLE", "T-20260808-03.txt")
            moved = ticket_mover.move_ticket(ticket, queue / "USER")
            self.assertIn("NICHT GEFUNDEN", moved.read_text(encoding="utf-8"))

    def test_the_same_id_at_the_end_of_a_sentence_still_matches(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue = _queue(Path(tmp))
            _index(queue.parent / dr.INDEX_RELATIVE_PATH, [{
                "key": "D-20260901-004", "id": "D-20260901-004",
                "status_class": "DONE", "title": "Derselbe Vorgang",
                "source_excerpt": "Beschlossen zu T-20260808-03.",
                "source_file": "DECIDED-AND-DONE.md",
            }])
            ticket = _ticket(queue, "ACTIONABLE", "T-20260808-03.txt")
            with self.assertRaises(ticket_mover.DecisionReadbackError):
                ticket_mover.move_ticket(ticket, queue / "USER")


class TestSubDecisionIds(unittest.TestCase):
    def test_a_sub_decision_id_in_the_ticket_finds_the_parent_in_the_register(self):
        """Tickets schreiben "D-20260906-008/E01", das Register indiziert die
        Eltern-ID "D-20260906-008". Ohne die Basis-Kennung findet das Gate
        nichts -- ein falsches NEGATIV, und damit genau das "nicht gefunden",
        das hier nie als "offen" durchgehen soll. Am 2026-09-13 gegen das echte
        Register gemessen: die Entscheide des Tages stehen in genau dieser
        Notation."""
        with tempfile.TemporaryDirectory() as tmp:
            queue = _queue(Path(tmp))
            _index(queue.parent / dr.INDEX_RELATIVE_PATH, [{
                "key": "D-20260906-008@DECIDED-AND-DONE", "id": "D-20260906-008",
                "status_class": "DONE", "title": "Fünf Governance-Entscheidungen",
                "source_file": "DECIDED-AND-DONE.md",
            }])
            ticket = _ticket(queue, "ACTIONABLE", "T-20260913-000000001.txt",
                             "Entscheid D-20260906-008/E01 (2026-09-11).")
            with self.assertRaises(ticket_mover.DecisionReadbackError) as refused:
                ticket_mover.move_ticket(ticket, queue / "USER")
            self.assertIn("D-20260906-008", str(refused.exception))

    def test_both_forms_are_searched_and_the_written_one_comes_first(self):
        keys = dr._keys_from_ticket("Siehe D-20260906-008/E01 und D-20260731-010.",
                                    "T-20260913-000000001.txt")
        self.assertEqual(keys, ["T-20260913-000000001", "D-20260906-008/E01",
                                "D-20260906-008", "D-20260731-010"])


class TestAcknowledgeMessageNamesTheQuotableId(unittest.TestCase):
    """T-20260920-716303992: das echte Register vergibt fuer jeden Eintrag ein
    qualifiziertes "key" (D-ID@Quelldatei), nicht die nackte D-ID -- belegt an
    D-20260916-004@DECIDED-AND-DONE, D-20260827-003@DECIDED-AND-DONE u.a. Die
    alte Fehlermeldung nannte nur den Platzhalter "--acknowledge-decision <ID>"
    und liess offen, ob damit die nackte oder die qualifizierte Form gemeint
    ist -- drei Fehlversuche am 2026-09-20 waren die Folge. Die Meldung muss
    die tatsaechlich quittierbare, kopierbare Kennung selbst nennen."""

    def test_the_refusal_quotes_the_exact_qualified_id_to_acknowledge(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue = _queue(Path(tmp))
            _index(queue.parent / dr.INDEX_RELATIVE_PATH, [{
                "key": "D-20260916-004@DECIDED-AND-DONE", "id": "D-20260916-004",
                "status_class": "DONE", "title": "Cluster-Planungsrahmen",
                "source_file": "DECIDED-AND-DONE.md",
            }])
            ticket = _ticket(queue, "WAITING", "T-20260920-000000001.txt",
                             "Bezug D-20260916-004.")
            with self.assertRaises(ticket_mover.DecisionReadbackError) as refused:
                ticket_mover.move_ticket(ticket, queue / "USER")
            message = str(refused.exception)
            # Die Meldung muss die volle, quittierbare Kennung als fertigen,
            # kopierbaren Flag enthalten -- nicht nur einen "<ID>"-Platzhalter
            # und nicht nur die nackte D-ID ohne Quelle.
            self.assertIn(
                "--acknowledge-decision 'D-20260916-004@DECIDED-AND-DONE'",
                message)
            self.assertNotIn("<ID>", message)
            # Die nackte D-ID allein (ohne @Quelle) darf weiterhin NICHT
            # quittieren -- sonst waere die Meldung nur kosmetisch, das Gate
            # aber weiter unbenutzbar mit dem, was es selbst empfiehlt.
            with self.assertRaises(ticket_mover.DecisionReadbackError):
                ticket_mover.move_ticket(
                    ticket, queue / "USER",
                    acknowledged_decisions=["D-20260916-004"])
            # Die aus der Meldung kopierte, qualifizierte Kennung muss
            # tatsaechlich funktionieren.
            moved = ticket_mover.move_ticket(
                ticket, queue / "USER",
                acknowledged_decisions=["D-20260916-004@DECIDED-AND-DONE"])
            self.assertTrue(moved.exists())

    def test_cluster_planning_decision_kind_is_logged_as_hit_but_does_not_block(self):
        """T-20260920-716303992 / D-20261030-003 (Richtung A): Cluster-Rahmenentscheide
        tragen im Index decision_kind='cluster-planning'. Sie werden beim Readback
        weiterhin als TREFFER protokolliert, blockieren aber keine spaetere
        Einzel-Eskalation nach USER/."""
        with tempfile.TemporaryDirectory() as tmp:
            queue = _queue(Path(tmp))
            _index(queue.parent / dr.INDEX_RELATIVE_PATH, [{
                "key": "D-20260916-004@DECIDED-AND-DONE", "id": "D-20260916-004",
                "status_class": "DONE", "decision_kind": "cluster-planning",
                "title": "Cluster-Planungsrahmen",
                "source_file": "DECIDED-AND-DONE.md",
            }])
            ticket = _ticket(queue, "WAITING", "T-20260920-000000002.txt",
                             "Bezug D-20260916-004.")
            moved = ticket_mover.move_ticket(ticket, queue / "USER")
            self.assertTrue(moved.exists())
            text = moved.read_text(encoding="utf-8")
            self.assertIn("Entscheidungs-Readback: TREFFER", text)
            self.assertIn("D-20260916-004@DECIDED-AND-DONE", text)


class TestUnreadableIndexNextToRegister(unittest.TestCase):
    """T-20261107-456970984, Review r1: Liegt neben der Queue ein Register und ist dessen Index
    VORHANDEN, aber UNLESBAR (halb geschrieben, weil decisions_index.py nicht atomar schreibt;
    OneDrive-Platzhalter offline), blieb das Ergebnis still 'absent'. Dann lief die Eskalation
    nach USER/ ungehindert durch -- das 'nicht gefunden', das als 'offen' gelesen wird.
    Erwartet: das Register macht den Index zum erwarteten, also 'unavailable'."""

    BROKEN = {
        "abgeschnitten": '{"schema": "decisions.index/1", "entries": [{"key": "D-2026',
        "leer": "",
        "ohne entries": "{}",
        "entries ist kein Array": '{"entries": "x"}',
    }

    def _setup(self, tmp: Path, text: str) -> tuple[Path, Path]:
        queue = _queue(tmp)
        index = queue.parent / dr.INDEX_RELATIVE_PATH
        index.parent.mkdir(parents=True, exist_ok=True)
        index.write_text(text, encoding="utf-8")
        ticket = _ticket(queue, "ACTIONABLE", "T-20260913-000000001.txt", "Bezug D-20260731-010.")
        return queue, ticket

    def _cli(self, ticket: Path) -> tuple[int, str]:
        import contextlib
        import io
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = ticket_mover._cli(["--decision-readback", str(ticket)])
        return code, out.getvalue()

    def test_a_present_but_broken_index_is_unavailable_not_absent(self):
        for label, text in self.BROKEN.items():
            with self.subTest(label), tempfile.TemporaryDirectory() as tmp:
                queue, ticket = self._setup(Path(tmp), text)
                path, expected = dr.find_decisions_index(queue)
                self.assertTrue(expected, "ein Register neben der Queue macht den Index zum erwarteten")
                self.assertEqual(dr.decision_readback(ticket, queue_dir=queue)["state"], "unavailable")
                code, out = self._cli(ticket)
                self.assertEqual(code, 1)
                self.assertIn("DECISION READBACK UNAVAILABLE", out)
                with self.assertRaises(ticket_mover.DecisionReadbackError):
                    ticket_mover.move_ticket(ticket, queue / "USER")
                self.assertTrue(ticket.exists(), "das Ticket bleibt liegen")

    def test_an_index_the_os_cannot_read_is_unavailable_too(self):
        from unittest import mock

        with tempfile.TemporaryDirectory() as tmp:
            queue, ticket = self._setup(Path(tmp), '{"entries": []}')
            real = Path.read_text

            def flaky(self, *args, **kwargs):
                if self.name == "decisions.index.json":
                    raise PermissionError("OneDrive-Platzhalter offline")
                return real(self, *args, **kwargs)

            with mock.patch.object(Path, "read_text", flaky):
                self.assertEqual(dr.decision_readback(ticket, queue_dir=queue)["state"], "unavailable")
                with self.assertRaises(ticket_mover.DecisionReadbackError):
                    ticket_mover.move_ticket(ticket, queue / "USER")

    def test_a_readable_index_still_passes_and_no_register_stays_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue, ticket = self._setup(Path(tmp), '{"entries": []}')
            self.assertEqual(dr.decision_readback(ticket, queue_dir=queue)["state"], "checked")
            self.assertTrue(ticket_mover.move_ticket(ticket, queue / "USER").exists())
        with tempfile.TemporaryDirectory() as tmp:
            queue = _queue(Path(tmp))
            ticket = _ticket(queue, "ACTIONABLE", "T-20260913-000000001.txt")
            self.assertEqual(dr.decision_readback(ticket, queue_dir=queue)["state"], "absent")

    def test_the_refusal_names_the_rebuild_command_not_the_word_named(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue, ticket = self._setup(Path(tmp), self.BROKEN["abgeschnitten"])
            tool = queue.parent / dr.INDEX_RELATIVE_PATH.parent / "decisions_index.py"
            tool.write_text("# Platzhalter fuer den Test\n", encoding="utf-8")
            with self.assertRaises(ticket_mover.DecisionReadbackError) as caught:
                ticket_mover.move_ticket(ticket, queue / "USER")
            message = str(caught.exception)
            self.assertIn(str(tool), message, "der echte Pfad des Werkzeugs steht in der Meldung")
            self.assertIn("python", message)
            self.assertNotIn("named", message)
            code, out = self._cli(ticket)
            self.assertIn(str(tool), out)
            result = dr.decision_readback(ticket, queue_dir=queue)
            line = dr.readback_log_line(result, stamp="2026-11-07T21:00:00Z")
            self.assertIn("decisions_index.py", line)
            self.assertNotIn("benannter", line)

    def test_without_the_tool_beside_the_index_the_hint_still_names_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue, ticket = self._setup(Path(tmp), "")
            with self.assertRaises(ticket_mover.DecisionReadbackError) as caught:
                ticket_mover.move_ticket(ticket, queue / "USER")
            self.assertIn("decisions_index.py", str(caught.exception))
