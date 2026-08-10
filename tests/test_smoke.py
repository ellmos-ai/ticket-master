"""
Smoke tests for ticket-master.

Checks:
1. Required directory structure exists.
2. config example JSON is valid.
3. Prompt file contains no forbidden terms (absolute Windows paths, system-specific
   infra names that should have been anonymised).
"""
import json
import pathlib
import shutil
import subprocess
import sys

REPO_ROOT = pathlib.Path(__file__).parent.parent

REQUIRED_PATHS = [
    "prompts/TICKET-MASTER.en.md",
    "prompts/TICKET-MASTER.de.md",
    "config/ticket-master.config.example.json",
    "tickets/_templates/TICKET.txt",
    "tickets/_logs/INTAKE-TRIAGE-LOG.txt",
    "tickets/QUEUED/.gitkeep",
    "tickets/SOLVED/.gitkeep",
    "tickets/INBOX/.gitkeep",
    "tickets/ACTIONABLE/.gitkeep",
    "tickets/BLOCKED/.gitkeep",
    "tickets/WAITING/.gitkeep",
    "tickets/USER/.gitkeep",
    "tickets/PARKED/.gitkeep",
    "tickets/PENDING/.gitkeep",
    "tickets/.USER/.gitkeep",
    "bin/ticket-master.sh",
    "bin/ticket-master.bat",
    "bin/ticket-master.ps1",
    "bin/ticket_master.py",
    "bin/start-claude.sh",
    "bin/start-claude.bat",
    "bin/start-codex.sh",
    "bin/start-codex.bat",
    "bin/start-agy.sh",
    "bin/start-agy.bat",
    "LICENSE",
    "README.md",
    "README_de.md",
    "CHANGELOG.md",
    "VERSION",
    "llms.txt",
    "SECURITY.md",
    ".gitignore",
    ".gitattributes",
]

FORBIDDEN_PATTERNS = [
    r"C:\\Users",
    r"C:/Users",
    "/Users/User/",
    "BACH",
    "Mac Studio",
    "Hetzner",
    ".SYNC",
]

PROMPT_FILES = [
    REPO_ROOT / "prompts" / "TICKET-MASTER.en.md",
    REPO_ROOT / "prompts" / "TICKET-MASTER.de.md",
]


def check_structure():
    missing = []
    for rel in REQUIRED_PATHS:
        p = REPO_ROOT / rel
        if not p.exists():
            missing.append(rel)
    if missing:
        print("FAIL structure — missing files:")
        for m in missing:
            print(f"  {m}")
        return False
    print("OK   structure — all required paths present")
    return True


def check_config_json():
    cfg = REPO_ROOT / "config" / "ticket-master.config.example.json"
    try:
        with open(cfg, encoding="utf-8") as f:
            json.load(f)
        print("OK   config JSON — valid")
        return True
    except json.JSONDecodeError as e:
        print(f"FAIL config JSON — {e}")
        return False


def check_prompt_clean():
    ok = True
    for prompt_file in PROMPT_FILES:
        text = prompt_file.read_text(encoding="utf-8")
        hits = [p for p in FORBIDDEN_PATTERNS if p in text]
        if hits:
            print(f"FAIL anonymisation — forbidden terms in {prompt_file.name}: {hits}")
            ok = False
    if ok:
        print("OK   anonymisation — no forbidden terms in prompt files")
    return ok


def check_gitignore_privacy_defaults():
    if not shutil.which("git"):
        print("WARN gitignore — git not found; skipping ignore-rule check")
        return True

    should_ignore = [
        "config/ticket-master.config.json",
        "tickets/BUG-123.txt",
        "tickets/QUEUED/work.txt",
        "tickets/.USER/manual.txt",
        ".env",
        ".env.local",
        "credentials.json",
        "token.json",
        "passwords.json",
        "id_ed25519",
        ".pypirc",
        "npm_recovery_codes.txt",
        "local.db",
        "local.sqlite",
        "local.sqlite3",
        "data/runtime.json",
        ".vscode/settings.json",
        ".idea/workspace.xml",
    ]
    should_track = [
        "config/ticket-master.config.example.json",
        "tickets/_logs/INTAKE-TRIAGE-LOG.txt",
        "tickets/_templates/TICKET.txt",
    ]

    ok = True
    for rel in should_ignore:
        result = subprocess.run(
            ["git", "check-ignore", "-q", "--", rel],
            cwd=REPO_ROOT,
            check=False,
        )
        if result.returncode != 0:
            print(f"FAIL gitignore — expected ignored: {rel}")
            ok = False

    for rel in should_track:
        result = subprocess.run(
            ["git", "check-ignore", "-q", "--", rel],
            cwd=REPO_ROOT,
            check=False,
        )
        if result.returncode == 0:
            print(f"FAIL gitignore — expected trackable: {rel}")
            ok = False

    if ok:
        print("OK   gitignore — privacy defaults protect local tickets and secrets")
    return ok


# pytest-Wrapper: die check_*-Funktionen geben bool zurueck (fuer main()/CLI);
# unter pytest zaehlte ein return-Wert frueher IMMER als bestanden
# (PytestReturnNotNoneWarning) — deshalb hier echte Asserts.

def test_structure():
    assert check_structure()


def test_config_json():
    assert check_config_json()


def test_prompt_clean():
    assert check_prompt_clean()


def test_gitignore_privacy_defaults():
    assert check_gitignore_privacy_defaults()


def main():
    results = [
        check_structure(),
        check_config_json(),
        check_prompt_clean(),
        check_gitignore_privacy_defaults(),
    ]
    passed = sum(results)
    total = len(results)
    print(f"\n{'PASSED' if passed == total else 'FAILED'}: {passed}/{total} checks")
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
