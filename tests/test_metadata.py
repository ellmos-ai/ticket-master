# -*- coding: utf-8 -*-
"""test_metadata.py - Metadata, badge, manifest, and documentation parity tests for ticket-master."""

import json
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
EXPECTED_TEST_COUNT = 508
LAST_CHECKED = "2026-09-16"


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
    for contract_token in (
        "ROUTING_SCHEMA", ".to-", ".via-", ".claim-", "SYSTEM_LEDGER",
        "route_intent", "create_routed_ticket", "Clutch", ".SYNC",
    ):
        assert contract_token in readme_en, contract_token
        assert contract_token in readme_de, contract_token
    assert "Ausführungsauflösung" in readme_de


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
    """Verify current PEP 621/639 metadata, classifiers, runtimes, and URLs."""
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

    assert 'requires = ["setuptools>=77.0.3"]' in content
    assert 'license = "MIT"' in content
    assert 'license-files = ["LICENSE", "THIRD_PARTY_LICENSES.md"]' in content
    assert 'dependencies = []' in content
    assert "license = {" not in content
    assert '"License ::' not in content
    assert (REPO_ROOT / "LICENSE").read_text(encoding="utf-8").startswith(
        "MIT License\n"
    )

    assert 'Homepage = "https://github.com/ellmos-ai/ticket-master"' in content
    assert 'Repository = "https://github.com/ellmos-ai/ticket-master.git"' in content
    config = json.loads(
        (REPO_ROOT / "config" / "ticket-master.config.example.json").read_text(
            encoding="utf-8"
        )
    )
    assert config["execution_binding"]["default_ttl_days"] == 7


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
    """Verify SECURITY.md exists and contains reporting guidelines, SLA, and security scope."""
    sec_path = REPO_ROOT / "SECURITY.md"
    assert sec_path.is_file()
    content = sec_path.read_text(encoding="utf-8")
    assert "Security Policy" in content
    assert "Reporting a Vulnerability" in content
    assert "Scope" in content
    assert "48 hours" in content
    assert "5 business days" in content
    assert "1.12.x" in content
    assert "Sicherheitsrichtlinie" in content


def test_third_party_licenses_inventory_and_zero_dependencies():
    """Verify THIRD_PARTY_LICENSES.md exists, documents zero runtime dependencies and dev tooling."""
    tpl_path = REPO_ROOT / "THIRD_PARTY_LICENSES.md"
    assert tpl_path.is_file(), "THIRD_PARTY_LICENSES.md missing"
    content = tpl_path.read_text(encoding="utf-8")
    assert "Zero-Runtime-Dependency Guarantee" in content
    assert "Python Standard Library" in content
    assert "PSF License" in content
    assert "pytest" in content
    assert "ruff" in content
    assert "MIT License" in content
    assert "Apache License 2.0" in content


def test_gitignore_secret_and_credential_patterns():
    """Verify .gitignore contains patterns for credentials, private keys, queue markers, and multi-host sync conflicts."""
    gitignore_path = REPO_ROOT / ".gitignore"
    assert gitignore_path.is_file(), ".gitignore missing"
    content = gitignore_path.read_text(encoding="utf-8")
    for pattern in [
        ".env",
        "*.pem",
        "*.key",
        "id_rsa",
        "id_ed25519",
        "credentials*.json",
        "token*.json",
        "secrets*.json",
        ".npmrc",
        ".pypirc",
        "tickets/.ticket-master-queue",
        "*-WORKSTATION-LG.*",
        "*-ASUS-GEI.*",
        "*.sync-conflict-*",
        "*.conflict",
    ]:
        assert pattern in content, f"Pattern '{pattern}' missing in .gitignore"


def test_security_policy_bilingual_sla_and_version():
    """Verify SECURITY.md contains bilingual German policy, 48h/5d SLA, and version 1.12.x support."""
    sec_path = REPO_ROOT / "SECURITY.md"
    assert sec_path.is_file()
    content = sec_path.read_text(encoding="utf-8")
    assert "48 hours" in content
    assert "5 business days" in content
    assert "1.12.x" in content
    assert "Sicherheitsrichtlinie" in content
    assert "48 Stunden" in content
    assert "5 Werktage" in content


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


def test_readme_18_point_quick_navigation_and_anchor_parity():
    """Verify README.md and README_de.md have identical 18-point numbered navigation anchors."""
    readme_en = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    readme_de = (REPO_ROOT / "README_de.md").read_text(encoding="utf-8")

    expected_anchors = [
        "1-overview",
        "2-key-capabilities",
        "3-target-personas--discoverability",
        "4-comparative-matrix-vs-alternatives",
        "5-governance--runtime-invariants",
        "6-architecture--routing-flow",
        "7-roles--auditor-bridge",
        "8-informal-intake--boot-menu",
        "9-quick-start--starters",
        "10-configuration--multi-host",
        "11-routing-contract-v2--score-formula",
        "12-cloud-ready-multi-system-claim-convention",
        "13-personal-assistant-expansion--delegation",
        "14-test-suite--verification-gates",
        "15-ecosystem--sibling-tools",
        "16-third-party-licenses--transparency",
        "17-security-policy--sla",
        "18-license--maintainers",
    ]

    for anchor in expected_anchors:
        assert f'href="#{anchor}"' in readme_en or f'(#{anchor})' in readme_en, f"Anchor #{anchor} missing in README.md nav"
        assert f'href="#{anchor}"' in readme_de or f'(#{anchor})' in readme_de, f"Anchor #{anchor} missing in README_de.md nav"
        assert f'id="{anchor}"' in readme_en, f"Anchor id={anchor} missing in README.md body"
        assert f'id="{anchor}"' in readme_de, f"Anchor id={anchor} missing in README_de.md body"


def test_readme_target_personas_and_discoverability():
    """Verify README.md and README_de.md define all 4 personas."""
    readme_en = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    readme_de = (REPO_ROOT / "README_de.md").read_text(encoding="utf-8")

    for persona in ["[PERSONA-01]", "[PERSONA-02]", "[PERSONA-03]", "[PERSONA-04]"]:
        assert persona in readme_en, f"Persona {persona} missing in README.md"
        assert persona in readme_de, f"Persona {persona} missing in README_de.md"


def test_readme_comparative_matrix_and_governance_invariants():
    """Verify README.md and README_de.md contain the 10-dimension comparative matrix and invariants."""
    readme_en = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    readme_de = (REPO_ROOT / "README_de.md").read_text(encoding="utf-8")

    invariants = [
        "INV-LOCAL-01", "INV-WORKFLOW-02", "INV-ROUTING-03", "INV-CLAIM-04", "INV-COMPANION-05",
        "INV-INTAKE-06", "INV-AUDITOR-07", "INV-UNPRIV-08", "INV-PORTABLE-09", "INV-SLA-10",
    ]
    for inv in invariants:
        assert inv in readme_en, f"Invariant {inv} missing in README.md"
        assert inv in readme_de, f"Invariant {inv} missing in README_de.md"


def test_pyproject_pep621_extended_urls():
    """Verify pyproject.toml contains Third-Party Licenses, Marketing Log, and LLM Ready in project.urls."""
    pyproject_text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert '"Third-Party Licenses"' in pyproject_text
    assert '"Marketing Log"' in pyproject_text
    assert '"LLM Ready"' in pyproject_text


def test_marketing_log_structure_and_completeness():
    """Verify MARKETING-LOG.txt exists, contains required sections, personas, and invariants."""
    mkt_file = REPO_ROOT / "MARKETING-LOG.txt"
    assert mkt_file.is_file(), "MARKETING-LOG.txt missing"
    content = mkt_file.read_text(encoding="utf-8")

    assert "2026-09-16" in content
    assert "Pfad B" in content
    for persona in ["[PERSONA-01]", "[PERSONA-02]", "[PERSONA-03]", "[PERSONA-04]"]:
        assert persona in content
    for inv in [
        "INV-LOCAL-01", "INV-WORKFLOW-02", "INV-ROUTING-03", "INV-CLAIM-04", "INV-COMPANION-05",
        "INV-INTAKE-06", "INV-AUDITOR-07", "INV-UNPRIV-08", "INV-PORTABLE-09", "INV-SLA-10",
    ]:
        assert inv in content


def test_third_party_licenses_zero_copyleft_and_invariants():
    """Verify THIRD_PARTY_LICENSES.md contains 2026-09-16 audit date, zero-copyleft guarantee, and invariants."""
    lic_file = REPO_ROOT / "THIRD_PARTY_LICENSES.md"
    assert lic_file.is_file()
    content = lic_file.read_text(encoding="utf-8")
    assert "2026-09-16" in content
    assert "Zero-Copyleft Guarantee" in content
    assert "RunAsInvoker" in content
    assert "INV-LOCAL-01" in content
    assert "INV-SLA-10" in content
