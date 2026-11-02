"""Producer and viewer CLI: python -m ticket_master_runtime.results."""
import argparse
import json
import subprocess
import sys
from pathlib import Path
from .model import read, write, append, add_comment, update_session_data, update_message_settings, mark_result_read, ResultsError


def backend_arguments(parser):
    parser.add_argument("--messages-core-dir", type=Path, help="Explizit freigegebene lokale Owner-Paketkopie")
    parser.add_argument("--messages-core-manifest-sha256", help="Gepinnter SHA-256 des MANIFEST.json")
    parser.add_argument("--messages-dir", type=Path, help="Bestehender kanonischer lokaler MessageStore")
    parser.add_argument("--messages-config", type=Path, help="Bestehende Messages-Line-Ownerkonfiguration")
    parser.add_argument("--messages-target", help="Registriertes local_gui-Ziel")
    parser.add_argument("--messages-recipient", help="Expliziter kanonischer Empfänger")
    parser.add_argument("--messages-actor", help="Lokaler Nutzer-/Akteurverweis für Quittungen")


def make_backend(args, required=False):
    values = [getattr(args, "messages_" + name) for name in ("core_dir", "core_manifest_sha256", "dir", "config", "target", "recipient", "actor")]
    if not any(values) and not required:
        return None
    if not all(values):
        raise ResultsError("Messages-Line braucht alle sieben expliziten --messages-Optionen")
    from .messages import ApprovedMessages
    return ApprovedMessages(*values)


def main(argv=None):
    # Redirected Windows streams may otherwise inherit cp1252 and reject arrows
    # or other legitimate report text. JSON files and CLI bytes are UTF-8.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Lokales Sitzungs-Ergebnisprotokoll")
    sub = parser.add_subparsers(dest="command", required=True)
    observe = sub.add_parser("observe", help="Alle tatsächlich wahrgenommenen externen Meldungen kanonisch erfassen")
    observe.add_argument("file", type=Path)
    source = observe.add_mutually_exclusive_group(required=True)
    source.add_argument("--input", type=Path, help="Beobachtung mit source/external_id/title/text/observed_at")
    source.add_argument("--message-id", help="Bereits vorhandene kanonische Message-ID zuordnen")
    source.add_argument("--event-id", help="Vorhandene ungebundene Sessionmeldung unter ihrer stabilen Event-ID zuordnen")
    observe.add_argument("--revision", type=int, required=True)
    observe.add_argument("--sha256", required=True)
    backend_arguments(observe)
    settings = sub.add_parser("message-settings", help="manual/on_open im selben Sessionfile per CAS konfigurieren")
    settings.add_argument("file", type=Path)
    settings.add_argument("--read-mode", choices=["manual", "on_open"], required=True)
    settings.add_argument("--revision", type=int, required=True)
    settings.add_argument("--sha256", required=True)
    archive = sub.add_parser("result-read", help="Ergebnis in dieser Session per CAS als gelesen archivieren; keine Taskabnahme")
    archive.add_argument("file", type=Path)
    archive.add_argument("--event-id", required=True)
    archive.add_argument("--event-sha256", required=True)
    archive.add_argument("--revision", type=int, required=True)
    archive.add_argument("--sha256", required=True)
    comment_cmd = sub.add_parser("comment", help="Kommentar, Nachfrage oder Modellantwort per CAS zu einem Ergebnis hinzufügen")
    comment_cmd.add_argument("file", type=Path)
    comment_cmd.add_argument("--event-id", required=True)
    comment_cmd.add_argument("--author", default="model", help="Autor des Kommentars (z. B. user oder model)")
    comment_cmd.add_argument("--text", required=True, help="Kommentartext")
    comment_cmd.add_argument("--comment-id", help="Optionale stabile Kommentar-ID")
    comment_cmd.add_argument("--revision", type=int, required=True)
    comment_cmd.add_argument("--sha256", required=True)
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
            p.add_argument("--locks-profile", type=Path, help="Optionales privates Operatorprofil mit belegten eigenen Lockpfaden")
            p.add_argument("--locks-profile-sha256", help="Explizit freigegebener SHA-256 des Lockprofils")
            backend_arguments(p)
    for name in ("create", "replace", "append"):
        p = sub.add_parser(name)
        p.add_argument("file", type=Path)
        p.add_argument("--input", type=Path, required=True)
        if name != "create":
            p.add_argument("--revision", type=int, required=True)
            p.add_argument("--sha256", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "observe":
            from .messages import ingest
            notice = json.loads(args.input.read_text(encoding="utf-8")) if args.input else None
            print(json.dumps(ingest(args.file, make_backend(args, required=True), args.revision, args.sha256,
                                   notice=notice, message_id=args.message_id, event_id=args.event_id), ensure_ascii=False))
        elif args.command == "message-settings":
            print(json.dumps({"sha256": update_message_settings(args.file, args.read_mode, args.revision, args.sha256)}))
        elif args.command == "result-read":
            print(json.dumps({"sha256": mark_result_read(args.file, args.event_id, args.revision, args.sha256, args.event_sha256)}))
        elif args.command == "comment":
            print(json.dumps({"sha256": add_comment(args.file, args.event_id, args.author, args.text,
                                                    args.revision, args.sha256, comment_id=args.comment_id)}))
        elif args.command == "session-data":
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
            if bool(args.locks_profile) != bool(args.locks_profile_sha256):
                raise ResultsError("Lockansicht benötigt Profil und gepinnten Profilhash")
            from .locks import LockView
            locks = LockView(args.locks_profile, args.locks_profile_sha256) if args.locks_profile else None
            launch(args.file, args.evidence, make_backend(args), locks)
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
    except (ValueError, OSError) as exc:
        parser.exit(2, f"Fehler: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
