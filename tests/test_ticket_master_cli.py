import contextlib
import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


def _load_cli():
    path = ROOT / "bin" / "ticket_master.py"
    spec = importlib.util.spec_from_file_location("ticket_master_cli", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"unable to load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TicketMasterCliTests(unittest.TestCase):
    @staticmethod
    def _config(root: Path, **values) -> Path:
        config = {
            "prompts_dir": "./prompts",
            "default_language": "en",
            "tickets_dir": "./tickets",
        }
        config.update(values)
        path = root / "config.json"
        path.write_text(json.dumps(config), encoding="utf-8")
        return path

    def test_custom_prompt_dir_is_used_and_language_falls_back(self):
        cli = _load_cli()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prompt_dir = root / "custom-prompts"
            prompt_dir.mkdir()
            (prompt_dir / "TICKET-MASTER.en.md").write_text("en", encoding="utf-8")
            (prompt_dir / "TICKET-MASTER.de.md").write_text("de", encoding="utf-8")
            config = self._config(root, prompts_dir="./custom-prompts", default_language="de")
            with patch.object(cli, "REPO_ROOT", root):
                lang, path, warnings = cli.resolve_prompt(config)
                self.assertEqual((lang, path.read_text(encoding="utf-8")), ("de", "de"))
                self.assertEqual(warnings, [])
                lang, path, warnings = cli.resolve_prompt(config, language="fr")
                self.assertEqual((lang, path.name), ("en", "TICKET-MASTER.en.md"))
                self.assertTrue(any("falling back" in warning for warning in warnings))

    def test_prompt_dir_cannot_escape_repository_root(self):
        cli = _load_cli()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = self._config(root, prompts_dir="../outside")
            with patch.object(cli, "REPO_ROOT", root):
                with self.assertRaises(cli.ConfigError):
                    cli.resolve_prompt(config)

    def test_list_is_deterministic_and_excludes_solved_bodies(self):
        cli = _load_cli()
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "QUEUED").mkdir()
            (base / "BLOCKED").mkdir()
            (base / "SOLVED").mkdir()
            (base / "QUEUED" / "T-20260810-02.txt").write_text(
                "ID: T-20260810-02\nTITEL: queued title\nSTATUS: QUEUED\nSECRET body\n",
                encoding="utf-8",
            )
            (base / "BLOCKED" / "T-20260810-01.HOST.txt").write_text(
                "ID: T-20260810-01\nTITEL: blocked title\nSTATUS: BLOCKED/lock\n",
                encoding="utf-8",
            )
            (base / "SOLVED" / "T-20260810-03.txt").write_text(
                "ID: T-20260810-03\nTITEL: solved secret\n", encoding="utf-8"
            )
            rows = cli.list_open_tickets(base)
            self.assertEqual([row["id"] for row in rows], ["T-20260810-01", "T-20260810-02"])
            self.assertEqual(rows[0]["status"], "BLOCKED/lock")
            self.assertNotIn("SECRET", repr(rows))

    def test_list_covers_all_v1_clusters_and_legacy_aliases(self):
        cli = _load_cli()
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            folders = ("", "INBOX", "ACTIONABLE", "QUEUED", "BLOCKED",
                       "WAITING", "USER", "PARKED", "PENDING", ".USER")
            for number, folder in enumerate(folders, start=1):
                directory = base / folder if folder else base
                directory.mkdir(parents=True, exist_ok=True)
                (directory / f"T-20260810-{number:02d}.txt").write_text(
                    f"ID: T-20260810-{number:02d}\nTITEL: {folder or 'root'}\n",
                    encoding="utf-8",
                )
            rows = cli.list_open_tickets(base)
            self.assertEqual(len(rows), len(folders))
            self.assertEqual({row["path"].split("/")[0] for row in rows},
                             {"T-20260810-01.txt", "INBOX", "ACTIONABLE", "QUEUED",
                              "BLOCKED", "WAITING", "USER", "PARKED", "PENDING", ".USER"})

    def test_intake_accepts_multiline_text_escapes_template_separator_and_is_exclusive(self):
        cli = _load_cli()
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            path = cli.intake_ticket(
                "first line\nsecond line\n" + cli._SEPARATOR,
                tickets_dir=base,
                today="2026-08-10",
            )
            self.assertEqual(path.parent.name, "INBOX")
            text = path.read_text(encoding="utf-8")
            self.assertIn("STATUS:        INBOX", text)
            self.assertIn("second line", text)
            self.assertIn("[ticket separator escaped]", text)
            self.assertFalse((base / "_logs" / "INTAKE-TRIAGE-LOG.txt").exists())

    def test_intake_rejects_empty_and_nul_descriptions(self):
        cli = _load_cli()
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                cli.intake_ticket("\n  ", tickets_dir=Path(tmp))
            with self.assertRaises(ValueError):
                cli.intake_ticket("safe\x00unsafe", tickets_dir=Path(tmp))

    def test_intake_does_not_overwrite_an_existing_ticket(self):
        """Seit 2026-08-15 wird die Nummer zufaellig gezogen (9-stellig) statt
        hochgezaehlt. Geprueft wird deshalb die Garantie, nicht der Wert: das
        vorhandene Ticket bleibt unangetastet, und das neue traegt eine eigene,
        formgueltige ID am selben Datum."""
        cli = _load_cli()
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            inbox = base / "INBOX"
            inbox.mkdir()
            existing = inbox / "T-20260810-01.txt"
            existing.write_text("ORIGINAL", encoding="utf-8")
            path = cli.intake_ticket("new", tickets_dir=base, today="2026-08-10")
            self.assertNotEqual(path.name, existing.name)
            self.assertTrue(path.name.startswith("T-20260810-"))
            self.assertEqual(existing.read_text(encoding="utf-8"), "ORIGINAL")

    def test_cli_list_json_and_explicit_missing_config_are_controlled(self):
        cli = _load_cli()
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = cli.main(["--list", "--json", "--tickets-dir", str(base)])
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(output.getvalue()), {"count": 0, "tickets": []})
            self.assertEqual(cli.main(["--list", "--config", str(base / "missing.json")]), 2)

    def test_all_starters_delegate_to_shared_resolver(self):
        for name in ("ticket-master.sh", "ticket-master.bat", "ticket-master.ps1"):
            text = (ROOT / "bin" / name).read_text(encoding="utf-8")
            self.assertIn("ticket_master.py", text, name)


if __name__ == "__main__":
    unittest.main()
