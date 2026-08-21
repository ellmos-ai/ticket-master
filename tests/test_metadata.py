# -*- coding: utf-8 -*-
"""test_metadata.py - Metadata, badge, manifest, and documentation parity tests for ticket-master."""

import json
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
EXPECTED_TEST_COUNT = 201
LAST_CHECKED = "2026-08-21"


def test_version_consistency():
    """Verify version parity across VERSION, pyproject.toml, README.md, README_de.md, CHANGELOG.md, and llms.txt."""
    version_file = (REPO_ROOT / "VERSION").read_text(encoding="utf-8").strip()
    assert version_file, "VERSION file is empty"

    pyproject_text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    version_match = re.search(r'version\s*=\s*"([^"]+)"', pyproject_text)
    assert version_match, "Version not found in pyproject.toml"
    version = version_match.group(1)
    assert version == version_file, f"Version mismatch: VERSION ({version_file}) != pyproject.toml ({version})"

    readme_en = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert f"version-{version}" in readme_en or f"Version: {version}" in readme_en or f"`{version}`" in readme_en

    readme_de = (REPO_ROOT / "README_de.md").read_text(encoding="utf-8")
    assert f"version-{version}" in readme_de or f"Version: {version}" in readme_de or f"`{version}`" in readme_de

    changelog = (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert f"## [{version}]" in changelog or "## [Unreleased]" in changelog

    llms_text = (REPO_ROOT / "llms.txt").read_text(encoding="utf-8")
    assert f"Version: `{version}`" in llms_text or f"Version: {version}" in llms_text


def test_badge_parity_and_links():
    """Verify README.md and README_de.md contain matching status badges and ecosystem links."""
    readme_en = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    readme_de = (REPO_ROOT / "README_de.md").read_text(encoding="utf-8")

    for keyword in [
        "License-MIT",
        "actions/workflows/tests.yml",
        "python-3.10",
        "Ecosystem-ellmos--ai",
        "Umbrella-open--bricks",
        "llms.txt",
    ]:
        assert keyword in readme_en, f"Badge keyword '{keyword}' missing in README.md"
        assert keyword in readme_de, f"Badge keyword '{keyword}' missing in README_de.md"

    test_badge = f"pytest-{EXPECTED_TEST_COUNT}%20passed"
    assert test_badge in readme_en
    assert test_badge in readme_de


def test_ci_workflow_integrity():
    """Verify GitHub Actions CI workflow exists, tests across Python 3.10-3.13 on ubuntu and windows, and includes ruff."""
    ci_path = REPO_ROOT / ".github" / "workflows" / "tests.yml"
    assert ci_path.is_file(), "CI workflow .github/workflows/tests.yml missing"
    content = ci_path.read_text(encoding="utf-8")

    assert "actions/checkout@v4" in content
    assert "actions/setup-python@v5" in content
    assert "3.10" in content and "3.11" in content and "3.12" in content and "3.13" in content
    assert "ubuntu-latest" in content and "windows-latest" in content
    assert "ruff check ." in content
    assert "pytest" in content


def test_pyproject_pep621_metadata():
    """Verify pyproject.toml PEP 621 compliance, standard classifiers, Python 3.13 support, and URLs."""
    pyproject_path = REPO_ROOT / "pyproject.toml"
    assert pyproject_path.is_file()
    content = pyproject_path.read_text(encoding="utf-8")

    for classifier in [
        "Operating System :: OS Independent",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
        "Programming Language :: Python :: 3.12",
        "Programming Language :: Python :: 3.13",
    ]:
        assert classifier in content, f"Classifier '{classifier}' missing in pyproject.toml"

    assert 'Homepage = "https://github.com/ellmos-ai/ticket-master"' in content
    assert 'Repository = "https://github.com/ellmos-ai/ticket-master.git"' in content


def test_llms_txt_integrity():
    """Verify llms.txt exists, contains required context, entry points, and has up-to-date timestamp."""
    llms_path = REPO_ROOT / "llms.txt"
    assert llms_path.is_file()
    content = llms_path.read_text(encoding="utf-8")

    assert f"Last-checked: {LAST_CHECKED}" in content
    assert f"`{EXPECTED_TEST_COUNT} passed`" in content
    assert "https://github.com/ellmos-ai/ticket-master" in content
    assert "prompts/TICKET-MASTER.en.md" in content or "prompts_dir/TICKET-MASTER.en.md" in content
    assert "config/ticket-master.config.example.json" in content
    assert "bin/ticket_master.py" in content
    assert "tests/test_smoke.py" in content


def test_security_policy_exists():
    """Verify SECURITY.md exists and contains reporting guidelines and security scope."""
    sec_path = REPO_ROOT / "SECURITY.md"
    assert sec_path.is_file()
    content = sec_path.read_text(encoding="utf-8")
    assert "Security Policy" in content
    assert "Reporting a Vulnerability" in content
    assert "Scope" in content


def test_ellmos_module_manifest_validity():
    """Verify ellmos-module.v2.json exists, adheres to schema, and specifies correct boundaries."""
    manifest_path = REPO_ROOT / "ellmos-module.v2.json"
    assert manifest_path.is_file()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    assert manifest.get("schema") == "ellmos.module.v2"
    assert manifest.get("id") == "ticket-master"
    assert manifest.get("visibility") == "public"
    assert "control.tickets" in manifest.get("provides", [])
    assert manifest.get("boundaries", {}).get("network") == "none"
    assert manifest.get("boundaries", {}).get("data") == "user-local"


def test_utf8_encoding_cleanliness():
    """Verify all text files in repository are valid UTF-8 without double-encoded mojibake or replacement chars."""
    mojibake_sequences = [b"\xc3\x83\xc2\xa4", b"\xc3\x83\xc2\xb6", b"\xc3\x83\xc2\xbc", b"\xc3\x83\xc2\x9f"]
    for pattern in ["*.md", "*.toml", "bin/*.py", "lib/*.py", "tests/*.py", "llms.txt", "*.json"]:
        for file_path in REPO_ROOT.glob(pattern):
            if file_path.is_file() and file_path.name != "test_metadata.py":
                raw = file_path.read_bytes()
                decoded = raw.decode("utf-8")
                assert "\ufffd" not in decoded, f"Unicode replacement character found in {file_path.name}"
                for seq in mojibake_sequences:
                    assert seq not in raw, f"Double-encoded sequence found in {file_path.name}"
