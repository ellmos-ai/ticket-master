"""Producer and viewer CLI: python -m ticket_master_runtime.results."""
import argparse
import json
import subprocess
import sys
from pathlib import Path
from .model import read, write, append, update_session_data, ResultsError


def main(argv=None):
    # Redirected Windows streams may otherwise inherit cp1252 and reject arrows
    # or other legitimate report text. JSON files and CLI bytes are UTF-8.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Lokales Sitzungs-Ergebnisprotokoll")
    sub = parser.add_subparsers(dest="command", required=True)
    metadata = sub.add_parser("session-data", help="Sessionmetriken schmal lesen oder per CAS aktualisieren")
    metadata.add_argument("file", type=Path)
    metadata.add_argument("--input", type=Path, help="Patch der Metrikgruppen; Listen werden vollständig ersetzt")
    metadata.add_argument("--revision", type=int)
    metadata.add_argument("--sha256")
    decision = sub.add_parser("decision-open", help="Optionalen installierten decision-draw-Viewer öffnen")
    decision.add_argument("file", type=Path)
    decision.add_argument("--skill-dir", type=Path, help="Vertrauenswürdiger installierter Skillordner")
    for name in ("validate", "read", "open"):
        p = sub.add_parser(name)
        p.add_argument("file", type=Path)
        if name == "open":
            p.add_argument("--evidence", type=Path)
    for name in ("create", "replace", "append"):
        p = sub.add_parser(name)
        p.add_argument("file", type=Path)
        p.add_argument("--input", type=Path, required=True)
        if name != "create":
            p.add_argument("--revision", type=int, required=True)
            p.add_argument("--sha256", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "session-data":
            if args.input:
                if args.revision is None or args.sha256 is None:
                    raise ResultsError("Sessiondaten-Update braucht --revision und --sha256")
                patch = json.loads(args.input.read_text(encoding="utf-8"))
                print(json.dumps({"sha256": update_session_data(args.file, patch, args.revision, args.sha256)}))
            else:
                data, sha = read(args.file)
                print(json.dumps({"session_id": data["session_id"], "revision": data["revision"], "sha256": sha, "metrics": data["metrics"]}, ensure_ascii=False, indent=2))
        elif args.command == "decision-open":
            candidates = [args.skill_dir] if args.skill_dir else [Path.home() / folder / "skills/decision-draw" for folder in (".codex", ".claude", ".agents")]
            script = next((folder / "scripts/decision_viewer.py" for folder in candidates if (folder / "SKILL.md").is_file() and (folder / "scripts/decision_viewer.py").is_file()), None)
            if script is None:
                raise ResultsError("Optionaler decision-draw-Viewer ist nicht installiert; --skill-dir kann eine geprüfte Skillinstallation angeben")
            return subprocess.call([sys.executable, str(script.resolve()), "open", str(args.file.absolute())])
        elif args.command == "open":
            from .gui import launch
            launch(args.file, args.evidence)
        elif args.command in {"read", "validate"}:
            data, sha = read(args.file)
            print(json.dumps({"session_id": data["session_id"], "revision": data["revision"], "sha256": sha, **({"data": data} if args.command == "read" else {})}, ensure_ascii=False, indent=2))
        else:
            value = json.loads(args.input.read_text(encoding="utf-8"))
            if args.command == "append":
                sha = append(args.file, value, args.revision, args.sha256)
            else:
                sha = write(args.file, value, getattr(args, "revision", None), getattr(args, "sha256", None))
            print(json.dumps({"sha256": sha}))
        return 0
    except (ResultsError, OSError, json.JSONDecodeError) as exc:
        parser.exit(2, f"Fehler: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
