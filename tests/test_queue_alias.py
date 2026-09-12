"""Rename-Toleranz `_TICKETS` <-> `TICKETS` (T-20260906-387521104).

Der Ordner und die Konfiguration, die auf ihn zeigt, liegen beide in OneDrive
und replizieren mit eigener Latenz. Zwischen Rename und angekommener Config
kann ein Host den einen Namen in der Config und den anderen auf der Platte
sehen. Ohne Toleranz legt ein Schreiber in diesem Fenster den alten Ordner
neu an -- eine Split-Queue, die niemand bemerkt, weil beide Haelften
plausibel aussehen.

Die Toleranz darf aber NICHT raten: existieren beide Namen, ist die Spaltung
bereits eingetreten, und Weiterschreiben wuerde sie vertiefen.
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

import config_paths  # noqa: E402
import ticket_writer  # noqa: E402
from queue_helpers import verified_queue  # noqa: E402


@pytest.fixture(autouse=True)
def _forget_warnings():
    """Die Einmal-Warnung ist Modulzustand -- sonst faerbt Test A auf B ab."""
    config_paths._warned_redirects.clear()


# --- Fall 1: nur der neue Name existiert (der Normalfall nach dem Rename) ---

def test_old_name_redirects_to_new_directory(tmp_path, capsys):
    new = tmp_path / "TICKETS"
    new.mkdir()

    out = config_paths.resolve_queue_alias(tmp_path / "_TICKETS")

    assert out == new
    assert "T-20260906-387521104" in capsys.readouterr().err


# --- Fall 2: nur der alte Name existiert (vor dem Rename, oder Rollback) ---

def test_new_name_redirects_back_to_old_directory(tmp_path, capsys):
    old = tmp_path / "_TICKETS"
    old.mkdir()

    out = config_paths.resolve_queue_alias(tmp_path / "TICKETS")

    assert out == old
    assert "_TICKETS" in capsys.readouterr().err


def test_existing_directory_wins_without_warning(tmp_path, capsys):
    """Steht der angefragte Ordner da, ist der Guard stumm und aendert nichts."""
    old = tmp_path / "_TICKETS"
    old.mkdir()

    assert config_paths.resolve_queue_alias(old) == old
    assert capsys.readouterr().err == ""


# --- Fall 3: beide existieren -> fail-closed, nicht waehlen ---

def test_both_names_present_is_refused(tmp_path):
    (tmp_path / "_TICKETS").mkdir()
    (tmp_path / "TICKETS").mkdir()

    with pytest.raises(config_paths.QueueAliasError) as exc:
        config_paths.resolve_queue_alias(tmp_path / "TICKETS")

    assert "split queue" in str(exc.value)


# --- Fall 4: keiner existiert -> unveraendert, Aufrufer meldet selbst ---

def test_neither_name_present_returns_input_unchanged(tmp_path, capsys):
    asked = tmp_path / "_TICKETS"

    assert config_paths.resolve_queue_alias(asked) == asked
    assert capsys.readouterr().err == ""


# --- Fremde Verzeichnisnamen bleiben unberuehrt (Aufruf ist ueberall sicher) ---

def test_unrelated_directory_name_is_untouched(tmp_path):
    other = tmp_path / "tickets"

    assert config_paths.resolve_queue_alias(other) == other


def test_warning_is_printed_only_once(tmp_path, capsys):
    (tmp_path / "TICKETS").mkdir()
    asked = tmp_path / "_TICKETS"

    config_paths.resolve_queue_alias(asked)
    capsys.readouterr()
    config_paths.resolve_queue_alias(asked)

    assert capsys.readouterr().err == ""


# --- Durchgriff: der Writer verifiziert gegen den vorhandenen Ordner ---

def test_writer_verifies_queue_under_renamed_directory(tmp_path):
    """require_verified_queue_root ist der Punkt, durch den jeder Writer geht."""
    new = verified_queue(tmp_path / "TICKETS")

    assert ticket_writer.require_verified_queue_root(tmp_path / "_TICKETS") == new


def test_writer_refuses_when_queue_is_split(tmp_path):
    verified_queue(tmp_path / "TICKETS")
    verified_queue(tmp_path / "_TICKETS")

    with pytest.raises(config_paths.QueueAliasError):
        ticket_writer.require_verified_queue_root(tmp_path / "_TICKETS")
