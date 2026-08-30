"""Tests for lib/boot_menu.py (Entscheid 5A, T-20260830-446089912).

Pure data functions only -- clutch is always mocked (monkeypatch on
boot_menu.shutil/subprocess), never a real subprocess. No process/window is
ever started by anything under test here.
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

import boot_menu  # noqa: E402


# --------------------------------------------------------------------------
# parse_mode: canonical notation + user-facing aliases, None on anything else
# --------------------------------------------------------------------------

def test_parse_mode_canonical_notation():
    assert boot_menu.parse_mode("3:1") == (3, 1)
    assert boot_menu.parse_mode("3:3") == (3, 3)
    assert boot_menu.parse_mode("1:1") == (1, 1)
    assert boot_menu.parse_mode("2:2") == (2, 2)


def test_parse_mode_aliases():
    assert boot_menu.parse_mode("3 in 1") == (3, 1)
    assert boot_menu.parse_mode("3in1") == (3, 1)
    assert boot_menu.parse_mode("3:3") == (3, 3)
    assert boot_menu.parse_mode("3x3") == (3, 3)
    assert boot_menu.parse_mode("only1") == (1, 1)
    assert boot_menu.parse_mode("only2") == (2, 2)


def test_parse_mode_is_case_and_whitespace_insensitive():
    assert boot_menu.parse_mode("  ONLY1  ") == (1, 1)
    assert boot_menu.parse_mode("3IN1") == (3, 1)


def test_parse_mode_returns_none_never_guesses():
    for bad in ("", None, "nonsense", "3:", ":1", "3-1", "only3"):
        assert boot_menu.parse_mode(bad) is None


# --------------------------------------------------------------------------
# list_models: clutch present/absent -> "source" reflects which was used
# --------------------------------------------------------------------------

class _FakeResult:
    def __init__(self, returncode, stdout):
        self.returncode = returncode
        self.stdout = stdout


def test_list_models_uses_clutch_when_on_path(monkeypatch):
    monkeypatch.setattr(boot_menu.shutil, "which", lambda name: "fake-clutch")
    payload = {"models": [{"name": "claude-sonnet", "provider": "anthropic",
                            "gang_stufe": 3, "efforts": ["low", "high"]}]}
    monkeypatch.setattr(
        boot_menu.subprocess, "run",
        lambda *a, **k: _FakeResult(0, json.dumps(payload)),
    )
    result = boot_menu.list_models({"providers": {}})
    assert result["source"] == "clutch"
    assert result["models"] == payload["models"]


def test_list_models_fallback_when_clutch_not_on_path(monkeypatch):
    monkeypatch.setattr(boot_menu.shutil, "which", lambda name: None)
    config = {"providers": {"claude": {"default_model": "sonnet"}, "codex": {"default_model": "auto"}}}
    result = boot_menu.list_models(config)
    assert result["source"] == "config-fallback"
    names = {m["name"] for m in result["models"]}
    assert names == {"claude", "codex"}


def test_list_models_fallback_when_clutch_call_fails(monkeypatch):
    monkeypatch.setattr(boot_menu.shutil, "which", lambda name: "fake-clutch")
    monkeypatch.setattr(
        boot_menu.subprocess, "run",
        lambda *a, **k: _FakeResult(1, ""),
    )
    result = boot_menu.list_models({"providers": {"agy": {"default_model": "gemini-2.0-flash"}}})
    assert result["source"] == "config-fallback"
    assert result["models"][0]["name"] == "agy"


# --------------------------------------------------------------------------
# self_model: harness self-declaration only, "unknown" if absent -- never guessed
# --------------------------------------------------------------------------

def test_self_model_unknown_without_any_declaration(monkeypatch):
    monkeypatch.delenv("TM_MODEL", raising=False)
    assert boot_menu.self_model({}) == "unknown"
    assert boot_menu.self_model(None) == "unknown"


def test_self_model_from_env(monkeypatch):
    monkeypatch.setenv("TM_MODEL", "claude-opus")
    assert boot_menu.self_model({}) == "claude-opus"


def test_self_model_from_config_when_env_absent(monkeypatch):
    monkeypatch.delenv("TM_MODEL", raising=False)
    assert boot_menu.self_model({"self_model": "codex-sol"}) == "codex-sol"


# --------------------------------------------------------------------------
# build_spawn_orders: 3:1 (unified) vs 3:3 (one-per-role) vs 1:1
# --------------------------------------------------------------------------

def test_build_spawn_orders_3_1_is_one_unified_instance():
    orders = boot_menu.build_spawn_orders((3, 1), None, "codex-sol-xhigh", True)
    assert len(orders) == 1
    assert orders[0]["roles"] == list(boot_menu.DEFAULT_ROLES)
    assert orders[0]["model"] == "codex-sol-xhigh"
    assert orders[0]["execution"] == "window"
    assert "launcher_hint" in orders[0]


def test_build_spawn_orders_3_3_is_one_instance_per_role():
    orders = boot_menu.build_spawn_orders(
        (3, 3), None, ["codex", "codex-luna-max", "claude-sonnet"], False)
    assert len(orders) == 3
    assert [o["roles"] for o in orders] == [[r] for r in boot_menu.DEFAULT_ROLES]
    assert [o["model"] for o in orders] == ["codex", "codex-luna-max", "claude-sonnet"]
    assert all(o["execution"] == "companion" for o in orders)
    assert all("windowless_hint" in o for o in orders)


def test_build_spawn_orders_1_1_only_one_role():
    orders = boot_menu.build_spawn_orders((1, 1), ["taskwriter"], "claude-sonnet", False)
    assert len(orders) == 1
    assert orders[0]["roles"] == ["taskwriter"]


def test_build_spawn_orders_rejects_role_count_mismatch():
    import pytest
    with pytest.raises(ValueError):
        boot_menu.build_spawn_orders((2, 2), ["taskwriter"], "x", False)


def test_build_spawn_orders_rejects_unimplemented_combination():
    import pytest
    with pytest.raises(ValueError):
        boot_menu.build_spawn_orders((3, 2), None, "x", False)


# --------------------------------------------------------------------------
# offer() / CLI --offer
# --------------------------------------------------------------------------

def test_offer_default_action_is_none_and_includes_codeword(monkeypatch):
    monkeypatch.setattr(boot_menu.shutil, "which", lambda name: None)
    monkeypatch.delenv("TM_MODEL", raising=False)
    result = boot_menu.offer({"providers": {"claude": {"default_model": "sonnet"}},
                               "auditor_bridge": {"codeword": "audit!"}})
    assert result["default"] == {"action": "none",
                                  "reason": "Enter ohne Eingabe -> POSITION 0, nichts starten"}
    assert result["auditor_codeword"] == "audit!"
    assert result["self_model"] == "unknown"
    assert set(result["roles"]) == set(boot_menu.DEFAULT_ROLES)
    assert "3:1" in result["modes"]


def test_cli_offer_prints_json(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(boot_menu.shutil, "which", lambda name: None)
    monkeypatch.delenv("TM_MODEL", raising=False)
    config_path = tmp_path / "ticket-master.config.json"
    config_path.write_text(json.dumps({"providers": {"claude": {"default_model": "sonnet"}}}),
                            encoding="utf-8")
    exit_code = boot_menu._cli(["--offer", "--config", str(config_path)])
    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["default"]["action"] == "none"
    assert payload["models"]["source"] == "config-fallback"
