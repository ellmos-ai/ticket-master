"""Aufloesung des `systems_registry`-Pfads: CLI > Env > Config > Fehler.

Der Defekt, den diese Tests festhalten (T-20260913-734536498), war kein Absturz
an einer falschen Stelle, sondern ein Abbruch trotz korrekter Konfiguration: Am
2026-09-13 scheiterten zweimal hintereinander Transfer-Tickets mit
"--systems-registry is required", obwohl `TICKET_MASTER_SYSTEMS_REGISTRY` auf
Benutzerebene auf die richtige Datei zeigte. Der laufende Prozess war vor dem
Setzen gestartet und hatte die Variable nie geerbt.

Die Beispielkonfiguration fuehrte den Schluessel `systems_registry` da bereits --
aber niemand las ihn; ihr eigener Kommentar bat den Menschen, Datei und Variable
von Hand gleichzuhalten. Eine Umgebungsvariable ist kein verlaesslicher Traeger
fuer einen Pfad, den jeder Schreibvorgang braucht.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

LIB = Path(__file__).resolve().parent.parent / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

import ticket_writer  # noqa: E402


@pytest.fixture
def ohne_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TICKET_MASTER_SYSTEMS_REGISTRY", raising=False)


def test_ohne_env_und_ohne_config_bleibt_es_beim_bisherigen_verhalten(
    ohne_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Kein stiller Hardcode-Fallback: Ist nichts gesetzt, bleibt es None.

    Der Aufrufer wirft daraufhin denselben Fehler wie bisher. Ein eingebauter
    Standardpfad waere hier gefaehrlich -- er wuerde auf eine Datei zeigen, die
    auf diesem Host nichts mit der echten Maschinenliste zu tun haben muss.
    """
    monkeypatch.setattr(ticket_writer, "_tm_config", dict)
    assert ticket_writer._default_systems_registry() is None


def test_config_traegt_den_pfad_wenn_die_env_fehlt(
    ohne_env: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    ziel = tmp_path / "systems-registry.json"
    monkeypatch.setattr(ticket_writer, "_tm_config",
                        lambda: {"systems_registry": str(ziel)})
    assert ticket_writer._default_systems_registry() == ziel


def test_env_schlaegt_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    aus_env = tmp_path / "aus-env.json"
    aus_config = tmp_path / "aus-config.json"
    monkeypatch.setenv("TICKET_MASTER_SYSTEMS_REGISTRY", str(aus_env))
    monkeypatch.setattr(ticket_writer, "_tm_config",
                        lambda: {"systems_registry": str(aus_config)})
    assert ticket_writer._default_systems_registry() == aus_env


def test_platzhalter_im_config_wert_werden_aufgeloest(
    ohne_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`<HOME>` muss zum echten Heimatverzeichnis werden.

    Ein unaufgeloester Platzhalter waere hier besonders tueckisch: Der Pfad
    existiert dann buchstaeblich nicht, und die Fehlermeldung zeigt auf ein
    Verzeichnis namens `<HOME>` -- das sieht nach Tippfehler des Nutzers aus,
    nicht nach unterlassener Aufloesung.
    """
    monkeypatch.setattr(
        ticket_writer, "_tm_config",
        lambda: {"systems_registry": "<HOME>/ticket-master-data/systems-registry.json"})
    aufgeloest = ticket_writer._default_systems_registry()
    assert aufgeloest is not None
    assert "<HOME>" not in str(aufgeloest)
    assert aufgeloest == Path.home() / "ticket-master-data" / "systems-registry.json"


@pytest.mark.parametrize("wert", [None, "", "   ", 42, [], {}])
def test_unbrauchbare_config_werte_ergeben_none(
    ohne_env: None, monkeypatch: pytest.MonkeyPatch, wert: object
) -> None:
    monkeypatch.setattr(ticket_writer, "_tm_config", lambda: {"systems_registry": wert})
    assert ticket_writer._default_systems_registry() is None


def test_kaputte_konfigurationsdatei_verhindert_keinen_schreibvorgang(
    ohne_env: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Eine unlesbare Config ist eine fehlende Bequemlichkeit, kein Abbruch.

    `_tm_config()` faengt OSError und ValueError ab. Ohne das wuerde ein einzelnes
    fehlerhaftes Komma in der Konfiguration jeden Ticketschreibvorgang stoppen --
    auch die, die den Registry-Pfad gar nicht brauchen.
    """
    kaputt = tmp_path / "config" / "ticket-master.config.json"
    kaputt.parent.mkdir(parents=True)
    kaputt.write_text("{ das ist kein JSON", encoding="utf-8")
    monkeypatch.setattr(ticket_writer, "__file__", str(tmp_path / "lib" / "ticket_writer.py"))
    assert ticket_writer._tm_config() == {}


def test_beispielconfig_fuehrt_den_schluessel_und_er_ist_aufloesbar() -> None:
    """Der Vertrag der Beispielkonfiguration muss halten.

    Der Schluessel stand dort schon, bevor ihn jemand las. Dieser Test bindet ihn
    an den Code, damit er nicht erneut zur blossen Dekoration wird.
    """
    beispiel = (Path(__file__).resolve().parent.parent
                / "config" / "ticket-master.config.example.json")
    daten = json.loads(beispiel.read_text(encoding="utf-8"))
    assert "systems_registry" in daten, "Beispielconfig muss den Schluessel fuehren"
    from config_paths import resolve_config_path
    assert resolve_config_path(daten["systems_registry"]) is not None
