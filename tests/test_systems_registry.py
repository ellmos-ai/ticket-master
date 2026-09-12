# -*- coding: utf-8 -*-
"""T-20260912-203012999: routing schema v2 demanded a system-registry
snapshot that nothing produced -- the only description of the format was a
test fixture. These tests pin the generator against the REAL consumer
(routing_contract), not against a copy of the expected shape, so generator
and contract cannot drift apart."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

LIB_DIR = Path(__file__).resolve().parent.parent / "lib"
sys.path.insert(0, str(LIB_DIR))

import routing_contract as rc  # noqa: E402
import systems_registry  # noqa: E402


def _seed(path: Path, hostname: str, role: str = "mobile", bom: bool = False) -> None:
    payload = {
        "_comment": "inventory seed",
        "_stand": "2026-09-12",
        "system": {"name": hostname, "hostname": hostname, "os": "Windows", "role": role},
        "software": [],
        "skills": [],
    }
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    path.write_text(text, encoding="utf-8-sig" if bom else "utf-8")


class TestSystemsRegistry(unittest.TestCase):
    def test_snapshot_satisfies_the_real_routing_contract(self):
        """Der eigentliche Zweck: die Ausgabe muss durch denselben Parser
        gehen, der sie spaeter ablehnt -- nicht durch eine Testkopie davon."""
        with tempfile.TemporaryDirectory() as tmp:
            seeds = Path(tmp)
            _seed(seeds / "laptop.json", "ASUS-GEI")
            _seed(seeds / "workstation.json", "WORKSTATION-LG", role="primary-dev")

            snapshot = systems_registry.build_snapshot(seeds, now="2026-09-12T08:00:00Z")

            resolved = rc.resolve_targets("exact", target="WORKSTATION-LG",
                                          registry_snapshot=snapshot)
            self.assertEqual(resolved["systems"], ["WORKSTATION-LG"])
            self.assertEqual(resolved["details"]["WORKSTATION-LG"]["slot"], "workstation")
            self.assertTrue(resolved["fingerprint"].startswith("sha256:"))

            every = rc.resolve_targets("all", registry_snapshot=snapshot)
            self.assertEqual(every["systems"], ["ASUS-GEI", "WORKSTATION-LG"])

    def test_seed_with_utf8_bom_is_readable(self):
        """surface.json traegt ein BOM (gemessen 2026-09-12); ein plain-utf-8
        Read wirft dort, waehrend die uebrigen Seeds durchgehen."""
        with tempfile.TemporaryDirectory() as tmp:
            seeds = Path(tmp)
            _seed(seeds / "surface.json", "SURFACE-LAPTOP", role="", bom=True)

            snapshot = systems_registry.build_snapshot(seeds)

            self.assertIn("SURFACE-LAPTOP", snapshot["systems"])

    def test_active_is_not_invented(self):
        """Die Seeds fuehren kein active-Feld. resolve_targets() liest es als
        value.get('active', True) -- optional mit Default. Ein erfundenes
        Feld wuerde eine fehlende Tatsache zu einer behaupteten machen."""
        with tempfile.TemporaryDirectory() as tmp:
            seeds = Path(tmp)
            _seed(seeds / "laptop.json", "ASUS-GEI")

            snapshot = systems_registry.build_snapshot(seeds)

            self.assertNotIn("active", snapshot["systems"]["ASUS-GEI"])

    def test_backup_seeds_are_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            seeds = Path(tmp)
            _seed(seeds / "laptop.json", "ASUS-GEI")
            _seed(seeds / "laptop.json.bak.json", "ASUS-GEI-STALE")

            snapshot = systems_registry.build_snapshot(seeds)

            self.assertEqual(list(snapshot["systems"]), ["ASUS-GEI"])

    def test_seed_without_hostname_is_skipped_not_guessed(self):
        with tempfile.TemporaryDirectory() as tmp:
            seeds = Path(tmp)
            _seed(seeds / "laptop.json", "ASUS-GEI")
            (seeds / "broken.json").write_text(
                json.dumps({"system": {"name": "x"}}), encoding="utf-8"
            )

            snapshot = systems_registry.build_snapshot(seeds)

            self.assertEqual(list(snapshot["systems"]), ["ASUS-GEI"])

    def test_empty_or_missing_directory_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                systems_registry.build_snapshot(Path(tmp))
            with self.assertRaises(FileNotFoundError):
                systems_registry.build_snapshot(Path(tmp) / "does-not-exist")

    def test_write_snapshot_roundtrips_through_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            seeds = Path(tmp) / "systems"
            seeds.mkdir()
            _seed(seeds / "laptop.json", "ASUS-GEI")
            out = Path(tmp) / "nested" / "systems-registry.json"

            systems_registry.write_snapshot(seeds, out, now="2026-09-12T08:00:00Z")

            written = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(written["checked_at"], "2026-09-12T08:00:00Z")
            rc.resolve_targets("all", registry_snapshot=written)


class TestWriterReadsSnapshot(unittest.TestCase):
    """Der Erzeuger schreibt sauberes UTF-8, aber ein von einem
    Windows-Werkzeug angefasster Snapshot kann ein BOM tragen -- genau wie
    einer der Inventar-Seeds, aus denen er abgeleitet wird."""

    def test_snapshot_with_bom_is_still_readable(self):
        import subprocess

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "INBOX").mkdir()
            (base / ".ticket-master-queue").write_text(
                "ticket-master-queue-v1", encoding="utf-8"
            )
            seeds = base / "systems"
            seeds.mkdir()
            _seed(seeds / "workstation.json", "WORKSTATION-LG", role="primary-dev")

            snapshot = base / "systems-registry.json"
            systems_registry.write_snapshot(seeds, snapshot)
            # Mit BOM neu schreiben, wie ein Windows-Editor es tun wuerde
            snapshot.write_text(
                snapshot.read_text(encoding="utf-8"), encoding="utf-8-sig"
            )

            result = subprocess.run(
                [sys.executable, str(LIB_DIR / "ticket_writer.py"),
                 "--tickets-dir", str(base), "--title", "BOM-Probe",
                 "--body", "x", "--ticket-kind", "transfer",
                 "--target-kind", "exact", "--target", "WORKSTATION-LG",
                 "--primary-ticket", "T-20260912-000000001",
                 "--original-owner", "a@h", "--receipt-to", "a@h",
                 "--systems-registry", str(snapshot)],
                capture_output=True, text=True,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(list((base / "INBOX").glob("*.txt")))


if __name__ == "__main__":
    unittest.main()
