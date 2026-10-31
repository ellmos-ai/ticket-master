import contextlib
import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from queue_helpers import verified_queue


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

    def test_default_prompt_resolves_from_installed_data_without_checkout(self):
        cli = _load_cli()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_root = root / "python-data"
            prompt_dir = data_root / "share" / "ticket-master" / "prompts"
            prompt_dir.mkdir(parents=True)
            (prompt_dir / "TICKET-MASTER.en.md").write_text("installed prompt", encoding="utf-8")
            config = root / "empty-config.json"
            config.write_text("{}", encoding="utf-8")
            with patch.object(cli, "REPO_ROOT", root), patch.object(
                cli.sysconfig, "get_path", return_value=str(data_root)
            ):
                lang, path, warnings = cli.resolve_prompt(config)
            self.assertEqual((lang, path.read_text(encoding="utf-8")), ("en", "installed prompt"))
            self.assertEqual(warnings, [])

    def test_packaged_entrypoints_share_inventory_implementation(self):
        import bin.ticket_master as source_cli
        import ticket_master_runtime.cli as packaged_cli
        from lib.ticket_inventory import capture_inventory as canonical_inventory
        from ticket_master_runtime.inventory import capture_inventory as public_inventory
        from ticket_master_runtime.adapters.bach import capture_inventory as bach_inventory
        from ticket_master_runtime.adapters.ocean import capture_inventory as ocean_inventory

        self.assertIs(packaged_cli.main, source_cli.main)
        self.assertIs(public_inventory, canonical_inventory)
        with tempfile.TemporaryDirectory() as tmp:
            queue = Path(tmp)
            (queue / "source.txt").write_text("inventory", encoding="utf-8")
            canonical = canonical_inventory(queue)
            canonical.pop("captured_at")
            for adapter in (bach_inventory, ocean_inventory):
                report = adapter(queue)
                report.pop("captured_at")
                self.assertEqual(report, canonical)

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
            base = verified_queue(tmp)
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

    def test_intake_accepts_the_public_producer_contract_title_and_body(self):
        """Der oeffentliche Uebergabevertrag, von der Konsumentenseite geprueft.

        Eine Befund-Senke (system-auditor `sinks.emit_command`) haengt an das
        konfigurierte Kommando genau `--title <titel> --body <text>` an; sie kennt
        weder Ticketformate noch diese CLI. Bis 2026-09-12 nahm `--intake` nur ein
        Positionsargument und kein `--body` entgegen -- der oeffentliche Aufruf lief
        damit ins Leere, und nur die interne Verdrahtung ueber `lib/ticket_writer.py`
        funktionierte (M-20260820-auditor-ticket-sink).

        Geprueft wird die Zusage vollstaendig: genau EIN Ticket entsteht, und Titel
        wie mehrzeiliger Body kommen unveraendert zurueck.
        """
        cli = _load_cli()
        title = "Registry-Quellattestierung aktualisieren"
        body = "Zeile eins\nZeile zwei mit Umlauten: \u00e4\u00f6\u00fc\nZeile drei"
        with tempfile.TemporaryDirectory() as tmp:
            # .resolve(): auf Windows-CI-Runnern liefert der Temp-Ordner den
            # 8.3-Kurznamen (C:\Users\RUNNER~1\...), waehrend die CLI intern
            # aufloest (C:\Users\runneradmin\...). Ohne das vergleicht der
            # assertIn unten zwei Schreibweisen desselben Pfades. Lokal faellt
            # das nicht auf, weil dort keine Kurznamen erzeugt werden.
            base = verified_queue(Path(tmp).resolve())
            argv = [
                "--intake",
                "--title",
                title,
                "--body",
                body,
                "--tickets-dir",
                str(base),
            ]
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = cli.main(argv)
            self.assertEqual(code, 0, out.getvalue())

            created = sorted(base.rglob("T-*.txt"))
            self.assertEqual(len(created), 1, created)
            self.assertEqual(created[0].parent.name, "INBOX")
            self.assertIn(str(created[0]), out.getvalue())

            text = created[0].read_text(encoding="utf-8")
            self.assertIn(title, text)
            for line in body.split("\n"):
                self.assertIn(line, text)

    def test_intake_rejects_a_description_given_twice_or_not_at_all(self):
        """Zwei Beschreibungen sind kein Vorrang, sondern ein Aufruffehler."""
        cli = _load_cli()
        with self.assertRaises(ValueError):
            cli.resolve_intake_description("positional", "body")
        with self.assertRaises(ValueError):
            cli.resolve_intake_description(None, None)
        with self.assertRaises(ValueError):
            cli.resolve_intake_description("", "   ")
        self.assertEqual(cli.resolve_intake_description("", "body"), "body")
        self.assertEqual(cli.resolve_intake_description("positional", None), "positional")

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
            base = verified_queue(tmp)
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

    def test_empty_project_roots_warns_on_startup_but_does_not_block(self):
        cli = _load_cli()
        self.assertEqual(cli.config_warnings({"project_roots": [{"path": "x"}]}), [])
        for cfg in ({}, {"project_roots": []}, {"project_roots": "C:/repos"}):
            warnings = cli.config_warnings(cfg)
            self.assertEqual(len(warnings), 1, cfg)
            self.assertIn("project_roots", warnings[0])

    def test_all_starters_delegate_to_shared_resolver(self):
        for name in ("ticket-master.sh", "ticket-master.bat", "ticket-master.ps1"):
            text = (ROOT / "bin" / name).read_text(encoding="utf-8")
            self.assertIn("ticket_master.py", text, name)

    def test_messages_line_public_cli_exposes_local_core_without_transport(self):
        import ticket_master_runtime.cli as packaged_cli
        from lib.messages_line import MessagesLine

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            messages_dir = root / "private-messages"
            active_config = {
                "schema": "ellmos.ticket.messages-line-config.v1",
                "updated_at": "2026-10-27T12:00:00Z",
                "enabled": True,
                "delivery_modes": {
                    "chat_start": True,
                    "on_demand": True,
                    "active_chat": True,
                },
                "targets": [{"target_ref": "fixture:local-gui", "surface": "local_gui"}],
                "gui_open_counts_as_delivery": True,
                "message_content_logging": False,
                "audio_logging": False,
                "transport_contract_ref": "ellmos.ticket.route-intent.v1",
            }
            disabled_config = {
                **active_config,
                "updated_at": "2026-10-27T12:02:00Z",
                "enabled": False,
            }
            active_path = root / "active-messages-config.json"
            active_path.write_text(json.dumps(active_config), encoding="utf-8")
            disabled_path = root / "disabled-messages-config.json"
            disabled_path.write_text(json.dumps(disabled_config), encoding="utf-8")
            registry_path = root / "log-registry.json"
            registry_path.write_text(
                json.dumps({"schema": "ellmos.ticket.log-registry.v1", "sources": []}),
                encoding="utf-8",
            )

            def invoke(argv):
                stdout = io.StringIO()
                stderr = io.StringIO()
                with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                    code = packaged_cli.main(argv)
                return code, stdout.getvalue(), stderr.getvalue()

            common = ["--messages-dir", str(messages_dir)]
            code, output, _error = invoke(["--messages-line", "config"])
            self.assertEqual(code, 0)
            self.assertFalse(json.loads(output)["enabled"])
            code, output, _error = invoke(["--messages-line", "registry"])
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(output)["sources"], [])

            code, output, _error = invoke(
                [
                    "--messages-line",
                    "create",
                    *common,
                    "--json",
                    "--messages-config",
                    str(active_path),
                    "--origin-ref",
                    "fixture:automation",
                    "--recipient-ref",
                    "fixture:user",
                    "--content-text",
                    "CLI fixture message.",
                    "--delivery-mode",
                    "on_demand",
                ]
            )
            self.assertEqual(code, 0)
            first = json.loads(output)
            self.assertEqual(first["content_text"], "CLI fixture message.")
            self.assertEqual(first["target_snapshot"], active_config["targets"])

            core = MessagesLine(messages_dir)
            self.assertEqual(core.delivery_state(first["message_id"]), "prepared")

            code, output, _error = invoke(
                [
                    "--messages-line",
                    "create",
                    *common,
                    "--messages-config",
                    str(active_path),
                    "--origin-ref",
                    "fixture:automation",
                    "--recipient-ref",
                    "fixture:user",
                    "--content-text",
                    "Second CLI fixture message.",
                    "--delivery-mode",
                    "active_chat",
                    "--thread-id",
                    first["thread_id"],
                ]
            )
            self.assertEqual(code, 0)
            second = json.loads(output)
            self.assertEqual(second["thread_id"], first["thread_id"])

            code, output, _error = invoke(
                ["--messages-line", "read", *common, "--message-id", first["message_id"]]
            )
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(output)["content_text"], "CLI fixture message.")
            self.assertEqual(core.delivery_state(first["message_id"]), "prepared")

            code, output, _error = invoke(
                [
                    "--messages-line",
                    "list",
                    *common,
                    "--json",
                    "--thread-id",
                    first["thread_id"],
                ]
            )
            self.assertEqual(code, 0)
            self.assertEqual([item["message_id"] for item in json.loads(output)],
                             [first["message_id"], second["message_id"]])
            self.assertEqual(core.delivery_state(first["message_id"]), "prepared")

            code, output, error = invoke(
                [
                    "--messages-line",
                    "create",
                    *common,
                    "--messages-config",
                    str(active_path),
                    "--origin-ref",
                    "fixture:automation",
                    "--recipient-ref",
                    "fixture:user",
                    "--content-text",
                    "Rejected unknown thread.",
                    "--delivery-mode",
                    "on_demand",
                    "--thread-id",
                    "TH-20261027-111111111",
                ]
            )
            self.assertEqual(code, 2)
            self.assertIn("existing canonical thread ledger", error)
            self.assertEqual(len(core.list_messages()), 2)

            code, output, _error = invoke(
                [
                    "--messages-line",
                    "config",
                    "--messages-config",
                    str(active_path),
                ]
            )
            self.assertEqual(code, 0)
            self.assertTrue(json.loads(output)["enabled"])

            code, output, _error = invoke(
                ["--messages-line", "registry", "--log-registry", str(registry_path)]
            )
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(output)["sources"], [])

            code, output, _error = invoke(
                [
                    "--messages-line",
                    "ack",
                    *common,
                    "--messages-config",
                    str(disabled_path),
                    "--message-id",
                    first["message_id"],
                    "--target-ref",
                    "fixture:local-gui",
                    "--surface",
                    "local_gui",
                    "--channel-ref",
                    "fixture:manual-cli",
                    "--actor-ref",
                    "fixture:user",
                    "--evidence-ref",
                    "fixture:explicit-user-action",
                ]
            )
            self.assertEqual(code, 0)
            ack = json.loads(output)
            self.assertEqual(ack["receipt"]["event"], "explicit_ack")
            self.assertEqual(ack["delivery_state"], "delivered")
            self.assertEqual(core.delivery_state(second["message_id"]), "prepared")


if __name__ == "__main__":
    unittest.main()
