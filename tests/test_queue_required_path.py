"""`ticket_master.py` hat keinen stillen Fallback auf `<repo>/tickets` mehr (T-20261107-847936256).

Ohne `tickets_dir` (weder `--tickets-dir` noch Konfiguration) fiel `resolve_tickets_dir` auf
`REPO_ROOT/tickets` zurueck -- im OneDrive-Spiegel des Moduls ist das die Fixture, in der im
September 27 Live-Tickets landeten. Jetzt ist der Pfad Pflicht.
"""
import contextlib
import io
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from queue_helpers import load_cli, verified_queue, write_file  # noqa: E402


def test_cli_has_no_silent_fallback_to_the_repo_fixture(tmp_path):
    cli = load_cli()
    config = write_file(tmp_path / "config.json", json.dumps({}))

    with pytest.raises(cli.ConfigError, match="tickets_dir"):
        cli.resolve_tickets_dir(config)
    with pytest.raises(cli.ConfigError, match="tickets_dir"):
        cli.resolve_tickets_dir(config, preserve_links=True)

    for argv in (["--list"], ["--ticket-inventory"], ["--intake", "Ohne Ziel"]):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), contextlib.redirect_stdout(io.StringIO()):
            assert cli.main([*argv, "--config", str(config)]) == 2
        assert "tickets_dir" in stderr.getvalue()


def test_the_error_names_the_ways_to_provide_the_path(tmp_path):
    cli = load_cli()
    config = write_file(tmp_path / "config.json", "{}")

    with pytest.raises(cli.ConfigError) as caught:
        cli.resolve_tickets_dir(config)

    assert "--tickets-dir" in str(caught.value)
    assert "config" in str(caught.value)


def test_cli_still_honours_an_explicit_or_configured_queue(tmp_path):
    cli = load_cli()
    queue = verified_queue(tmp_path / "queue")
    with_dir = write_file(tmp_path / "with.json", json.dumps({"tickets_dir": str(queue)}))

    assert cli.resolve_tickets_dir(with_dir) == queue.resolve()
    assert cli.resolve_tickets_dir(write_file(tmp_path / "empty.json", "{}"), override=queue) == queue.resolve()
    with contextlib.redirect_stdout(io.StringIO()):
        assert cli.main(["--list", "--config", str(with_dir)]) == 0
