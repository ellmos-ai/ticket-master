"""Aufloesung der Queue-Wurzel: Aufrufer > Umgebung > Konfiguration > Fehler.

Der Defekt, den diese Tests festhalten (T-20260913-156957497), ist die
Zwillingsschwester von `systems_registry` (T-20260913-734536498, PR #30) -- mit
einem Unterschied, der ihn schlimmer machte: Er endete nicht in einer lesbaren
Fehlermeldung, sondern in einem nackten ``TypeError`` aus ``pathlib``.

``ticket_writer --split-from`` ohne ``--tickets-dir`` lief am 2026-09-13 so:
``_default_tickets_dir()`` las nur die Umgebungsvariable, fand nichts, gab
``None`` zurueck -- und ``split_ticket()`` hatte als einzige der vier
Schreibfunktionen **gar keine** Pruefung, reichte ``None`` an ``Path()`` weiter
und brach mit "argument should be a str or an os.PathLike object ... not
'NoneType'" ab. Die Beispielkonfiguration fuehrte den Schluessel ``tickets_dir``
da bereits; niemand las ihn.

Vier Module stellten dieselbe Frage und waren sich nicht einig: drei lasen nur
die Umgebung, ``auditor_bridge`` las Konfiguration VOR Umgebung. Waren beide
gesetzt und liefen auseinander, schrieb derselbe Aufruf je nach Einstiegspunkt
in eine andere Queue. Deshalb liegt die Aufloesung jetzt in ``config_paths``.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

LIB = Path(__file__).resolve().parent.parent / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

import config_paths  # noqa: E402
import ticket_writer  # noqa: E402

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture
def ohne_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(config_paths.TICKETS_DIR_ENV, raising=False)


@pytest.fixture
def ohne_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """Konfiguration leer -- ohne die echte Datei des Hosts anzufassen."""
    monkeypatch.setattr(config_paths, "tm_config", lambda path=None: {})


# --- Praezedenz -------------------------------------------------------------

def test_ohne_alles_bleibt_es_none(ohne_env: None, ohne_config: None) -> None:
    """Kein eingebauter Standardpfad.

    Ein stiller Fallback auf etwas wie ``./tickets`` waere hier der schlimmste
    Ausgang: Der Schreiber legte eine ueberzeugend aussehende Parallel-Queue an,
    statt zu sagen, dass er die echte nicht kennt.
    """
    assert config_paths.resolve_tickets_dir() is None


def test_aufrufer_schlaegt_umgebung_und_konfiguration(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(config_paths.TICKETS_DIR_ENV, str(tmp_path / "aus-env"))
    monkeypatch.setattr(config_paths, "tm_config",
                        lambda path=None: {"tickets_dir": str(tmp_path / "aus-config")})
    assert config_paths.resolve_tickets_dir(tmp_path / "vom-aufrufer") == tmp_path / "vom-aufrufer"


def test_umgebung_schlaegt_konfiguration(
    ohne_env: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(config_paths.TICKETS_DIR_ENV, str(tmp_path / "aus-env"))
    monkeypatch.setattr(config_paths, "tm_config",
                        lambda path=None: {"tickets_dir": str(tmp_path / "aus-config")})
    assert config_paths.resolve_tickets_dir() == tmp_path / "aus-env"


def test_konfiguration_traegt_wenn_die_umgebung_schweigt(
    ohne_env: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Der eigentliche Fix: Die Datei allein genuegt jetzt.

    Genau hier lag der Fehler -- der Wert stand in der Konfiguration und wurde
    nicht gelesen, weil nur die Umgebung befragt wurde.
    """
    monkeypatch.setattr(config_paths, "tm_config",
                        lambda path=None: {"tickets_dir": str(tmp_path / "aus-config")})
    assert config_paths.resolve_tickets_dir() == tmp_path / "aus-config"


# --- Unbrauchbare Werte fallen durch, statt zu gewinnen ----------------------

@pytest.mark.parametrize("wert", ["", "   ", None, 42, [], {}])
def test_unbrauchbarer_konfigurationswert_gilt_als_nicht_gesetzt(
    ohne_env: None, monkeypatch: pytest.MonkeyPatch, wert: object
) -> None:
    monkeypatch.setattr(config_paths, "tm_config", lambda path=None: {"tickets_dir": wert})
    assert config_paths.resolve_tickets_dir() is None


def test_leere_umgebungsvariable_faellt_auf_die_konfiguration_durch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Eine auf "" gesetzte Variable ist gesetzt und trotzdem wertlos."""
    monkeypatch.setenv(config_paths.TICKETS_DIR_ENV, "")
    monkeypatch.setattr(config_paths, "tm_config",
                        lambda path=None: {"tickets_dir": str(tmp_path / "aus-config")})
    assert config_paths.resolve_tickets_dir() == tmp_path / "aus-config"


def test_kaputte_konfigurationsdatei_verhindert_keinen_schreibvorgang(
    ohne_env: None, tmp_path: Path
) -> None:
    """Eine unlesbare Konfiguration ist kein Grund, gar nichts mehr zu tun."""
    kaputt = tmp_path / "kaputt.json"
    kaputt.write_text("{ das ist kein json", encoding="utf-8")
    assert config_paths.tm_config(kaputt) == {}
    fehlt = tmp_path / "gibt-es-nicht.json"
    assert config_paths.tm_config(fehlt) == {}


# --- Platzhalter gelten auf allen drei Wegen --------------------------------

def test_platzhalter_werden_auch_aus_der_umgebung_aufgeloest(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sonst entsteht ein Verzeichnis namens buchstaeblich "<HOME>/..."

    Dieselbe Klasse wie T-20260912-206012253, wo ein unaufgeloester Platzhalter
    die Dedup-Pruefung ins Leere laufen liess und jedes Finding als neu meldete.
    """
    monkeypatch.setenv(config_paths.TICKETS_DIR_ENV, "<HOME>/queue")
    aufgeloest = config_paths.resolve_tickets_dir()
    assert aufgeloest == Path.home() / "queue"
    assert "<HOME>" not in str(aufgeloest)


# --- Der Fehlerweg ----------------------------------------------------------

def test_fehlermeldung_nennt_alle_drei_wege(ohne_env: None, ohne_config: None) -> None:
    with pytest.raises(ValueError) as fehler:
        config_paths.require_tickets_dir()
    text = str(fehler.value)
    assert "--tickets-dir" in text
    assert config_paths.TICKETS_DIR_ENV in text
    assert "tickets_dir" in text
    assert "ticket-master.config.json" in text


def test_split_ticket_meldet_klartext_statt_typeerror(
    ohne_env: None, ohne_config: None, tmp_path: Path
) -> None:
    """Der Regressionstest zum Ausloeser.

    Vorher: ``TypeError: argument should be a str or an os.PathLike object
    where __fspath__ returns a str, not 'NoneType'`` -- eine Meldung, die den
    Anwender nichts ueber die Ursache lehrt.
    """
    quelle = tmp_path / "T-20260913-000000001.txt"
    quelle.write_text("ID:            T-20260913-000000001\n", encoding="utf-8")
    with pytest.raises(ValueError, match="tickets_dir is not configured"):
        ticket_writer.split_ticket(quelle, tickets_dir=None, title="egal")


@pytest.mark.parametrize("funktion,kwargs", [
    ("create", {"title": "t", "body": "b"}),
    ("formalize_informal_entry", {}),
    ("create_routed_ticket", {"title": "t", "body": "b", "registry_snapshot": {}}),
])
def test_jede_schreibfunktion_meldet_denselben_klartext(
    ohne_env: None, ohne_config: None, tmp_path: Path, funktion: str, kwargs: dict
) -> None:
    """Eine Meldung fuer alle vier Einstiege -- nicht vier verschiedene."""
    ziel = getattr(ticket_writer, funktion)
    if funktion == "formalize_informal_entry":
        quelle = tmp_path / "formlos.txt"
        quelle.write_text("irgendein Text\n", encoding="utf-8")
        args = (quelle,)
    else:
        args = ()
    with pytest.raises(ValueError, match="tickets_dir is not configured"):
        ziel(*args, tickets_dir=None, **kwargs)


# --- Vertragstest: der Schluessel und der Code haengen zusammen -------------

def test_beispielconfig_fuehrt_genau_den_schluessel_den_der_code_liest() -> None:
    """Bindet Dokumentation an Verhalten.

    Ohne diesen Test kann der Schluessel in der Beispielconfig umbenannt werden,
    ohne dass ein Test rot wird -- und damit entsteht genau der Zustand, der
    dieses Ticket ausgeloest hat: ein Eintrag, den die Leute pflegen und den
    niemand liest.
    """
    beispiel = REPO / "config" / "ticket-master.config.example.json"
    daten = json.loads(beispiel.read_text(encoding="utf-8-sig"))
    assert "tickets_dir" in daten, "Beispielconfig fuehrt den Schluessel nicht mehr"

    # Und der Code liest genau diesen Schluessel -- nachgewiesen, nicht behauptet.
    class NurDieserSchluessel(dict):
        def __init__(self) -> None:
            super().__init__({"tickets_dir": "/erwartet"})
            self.gelesen: list = []

        def get(self, schluessel, default=None):  # type: ignore[override]
            self.gelesen.append(schluessel)
            return super().get(schluessel, default)

    spion = NurDieserSchluessel()
    assert config_paths.resolve_tickets_dir(config=spion) == Path("/erwartet")
    assert spion.gelesen == ["tickets_dir"]


def test_kein_modul_im_paket_liest_die_variable_noch_selbst() -> None:
    """Die vier privaten Varianten sind wirklich weg, nicht nur eine davon.

    Der Punkt dieses Tickets war nicht, EINE Stelle zu reparieren, sondern die
    vier uneinigen Stellen auf eine Quelle zu ziehen. Ein Test, der nur
    ticket_writer prueft, laesst die naechste Variante entstehen.
    """
    lib = REPO / "lib"
    schuldige = []
    for datei in sorted(lib.glob("*.py")):
        if datei.name == "config_paths.py":  # dort gehoert sie hin
            continue
        text = datei.read_text(encoding="utf-8")
        for nummer, zeile in enumerate(text.splitlines(), 1):
            if config_paths.TICKETS_DIR_ENV in zeile and (
                "os.environ" in zeile or "os.getenv" in zeile
            ):
                schuldige.append(f"{datei.name}:{nummer}")
    assert not schuldige, (
        "Diese Stellen lesen die Umgebungsvariable wieder selbst, statt "
        f"config_paths.resolve_tickets_dir() zu nutzen: {schuldige}"
    )
