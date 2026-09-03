"""T-20260903-778818739: fix the SAFE subset of status_drift findings only."""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

import status_drift_fixer  # noqa: E402


def _ticket(path: Path, status: str, loesung: str, verlauf_extra: str = "") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    verlauf = "2026-08-16  Aufgenommen (asynchron via Lock-Watcher-GUI / ticket_writer)."
    if verlauf_extra:
        verlauf += f"\n{verlauf_extra}"
    path.write_text(
        "==============================================================\n"
        "TICKET\n"
        "==============================================================\n"
        "ID:            T-20260816-000000001\n"
        "TITEL:         x\n"
        f"STATUS:        {status}\n"
        "PRIORITAET:    mittel\n"
        "--------------------------------------------------------------\n"
        "VERLAUF / LOG\n"
        "--------------------------------------------------------------\n"
        f"{verlauf}\n"
        "--------------------------------------------------------------\n"
        "LOESUNG / ERGEBNIS\n"
        "--------------------------------------------------------------\n"
        f"{loesung}\n"
        "==============================================================\n",
        encoding="utf-8",
    )
    return path


_FILLED = "Problem X behoben, Verifikation Y durchgefuehrt."
_PLACEHOLDER = "<Vor Verschieben nach SOLVED ausfüllen.>"
_DONE_ENTRY = "2026-08-17  Bearbeitet und verifiziert."


class TestClassify(unittest.TestCase):
    def test_solved_with_filled_sections_is_fixable(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            _ticket(base / "SOLVED" / "T-20260816-000000001.txt", "INBOX", _FILLED, _DONE_ENTRY)
            report = status_drift_fixer.classify(base)
            self.assertEqual(len(report["fixable"]), 1)
            self.assertEqual(report["needs_review"], [])

    def test_solved_with_placeholder_loesung_needs_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            _ticket(base / "SOLVED" / "T-20260816-000000001.txt", "INBOX", _PLACEHOLDER, _DONE_ENTRY)
            report = status_drift_fixer.classify(base)
            self.assertEqual(report["fixable"], [])
            self.assertEqual(len(report["needs_review"]), 1)

    def test_solved_with_only_bootstrap_verlauf_needs_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            _ticket(base / "SOLVED" / "T-20260816-000000001.txt", "INBOX", _FILLED)  # no verlauf_extra
            report = status_drift_fixer.classify(base)
            self.assertEqual(report["fixable"], [])
            self.assertEqual(len(report["needs_review"]), 1)

    def test_user_decision_stays_needs_review_even_when_filled(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            _ticket(base / "SOLVED" / "T-20260816-000000001.txt",
                    "USER/decision (seit 2026-08-16)", _FILLED, _DONE_ENTRY)
            report = status_drift_fixer.classify(base)
            self.assertEqual(report["fixable"], [])
            self.assertEqual(len(report["needs_review"]), 1)

    def test_non_solved_folder_is_never_fixable(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            _ticket(base / "BLOCKED" / "T-20260816-000000001.txt", "ACTIONABLE", _FILLED, _DONE_ENTRY)
            report = status_drift_fixer.classify(base)
            self.assertEqual(report["fixable"], [])
            self.assertEqual(len(report["needs_review"]), 1)

    def test_wont_fix_stays_needs_review_even_when_filled(self):
        """T-20260903-778818739 review: WONT-FIX is its own closure type,
        not stale vocabulary for 'done' -- never auto-fold into SOLVED."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            _ticket(base / "SOLVED" / "T-20260816-000000001.txt",
                    "WONT-FIX (privacy-begruendet)", _FILLED, _DONE_ENTRY)
            report = status_drift_fixer.classify(base)
            self.assertEqual(report["fixable"], [])
            self.assertEqual(len(report["needs_review"]), 1)


class TestApplyFixErrors(unittest.TestCase):
    def test_file_removed_since_scan_is_reported_not_raised(self):
        """A mass rewrite must not crash on the first surprise; a file that
        vanished (or a STATUS line that changed) between scan and apply
        belongs in errors, not a traceback (T-20260903-778818739 review)."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            path = _ticket(base / "SOLVED" / "T-20260816-000000001.txt", "INBOX", _FILLED, _DONE_ENTRY)
            report = status_drift_fixer.classify(base)
            self.assertEqual(len(report["fixable"]), 1)
            path.unlink()
            new_value, error = status_drift_fixer.apply_fix(report["fixable"][0])
            self.assertIsNone(new_value)
            self.assertIsNotNone(error)

    def test_foreign_write_between_scan_and_apply_is_reported_not_clobbered(self):
        """T-20260903-965930417: a shared, lock-free ticket tree means the
        classify() scan and apply_fix() write can straddle a foreign edit.
        The compare-and-swap must refuse instead of overwriting it."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            path = _ticket(base / "SOLVED" / "T-20260816-000000001.txt", "INBOX", _FILLED, _DONE_ENTRY)
            report = status_drift_fixer.classify(base)
            self.assertEqual(len(report["fixable"]), 1)
            path.write_text(path.read_text(encoding="utf-8") + "\nFOREIGN EDIT\n", encoding="utf-8")

            new_value, error = status_drift_fixer.apply_fix(report["fixable"][0])
            self.assertIsNone(new_value)
            self.assertIn("changed since scan", error)
            self.assertIn("FOREIGN EDIT", path.read_text(encoding="utf-8"))  # not clobbered


class TestRun(unittest.TestCase):
    def test_dry_run_does_not_write_and_previews_the_real_value(self):
        """T-20260903-778818739 review: the dry-run preview must show what
        would ACTUALLY be written, not just echo the old status -- and
        'applied' (files really touched) must be empty."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            path = _ticket(base / "SOLVED" / "T-20260816-000000001.txt", "INBOX", _FILLED, _DONE_ENTRY)
            before = path.read_text(encoding="utf-8")
            report = status_drift_fixer.run(base, dry_run=True)
            self.assertEqual(path.read_text(encoding="utf-8"), before)
            self.assertEqual(report["applied"], [])
            self.assertEqual(len(report["preview"]), 1)
            self.assertTrue(report["preview"][0]["new_status"].startswith("SOLVED (seit "))

    def test_apply_rewrites_status_line_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            path = _ticket(base / "SOLVED" / "T-20260816-000000001.txt", "INBOX", _FILLED, _DONE_ENTRY)
            before = path.read_text(encoding="utf-8")
            report = status_drift_fixer.run(base, dry_run=False)
            after = path.read_text(encoding="utf-8")
            self.assertTrue(after.splitlines()[5].startswith("STATUS:        SOLVED"))
            # Everything except the STATUS line is untouched.
            self.assertEqual(
                [l for i, l in enumerate(before.splitlines()) if i != 5],
                [l for i, l in enumerate(after.splitlines()) if i != 5],
            )
            self.assertEqual(status_drift_fixer.status_drift(base), [])
            self.assertEqual(report["preview"], [])
            self.assertTrue(report["applied"][0]["new_status"].startswith("SOLVED (seit "))

    def test_fallback_preserves_free_text_for_unparsable_cluster(self):
        """T-20260903-778818739 review: 'done (Grund X, siehe Folgeticket
        T-99)' must not collapse to a bare 'SOLVED (seit ...)' -- that
        silently drops the closure reason and the follow-up reference."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            _ticket(base / "SOLVED" / "T-20260816-000000001.txt",
                    "done (Grund X, siehe Folgeticket T-99)", _FILLED, _DONE_ENTRY)
            report = status_drift_fixer.run(base, dry_run=True)
            self.assertEqual(len(report["preview"]), 1)
            new_status = report["preview"][0]["new_status"]
            self.assertTrue(new_status.startswith("SOLVED (seit "))
            self.assertIn("Grund X, siehe Folgeticket T-99", new_status)

    def test_subcategory_is_dropped_not_carried_over(self):
        """T-20260903-778818739 review: a source subcategory ('dependency',
        'cleanup', ...) describes the SOURCE cluster, not SOLVED."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            _ticket(base / "SOLVED" / "T-20260816-000000001.txt",
                    "BLOCKED/dependency (seit 2026-08-17)", _FILLED, _DONE_ENTRY)
            report = status_drift_fixer.run(base, dry_run=True)
            new_status = report["preview"][0]["new_status"]
            self.assertTrue(new_status.startswith("SOLVED (seit "))
            self.assertNotIn("dependency", new_status)


if __name__ == "__main__":
    unittest.main()
