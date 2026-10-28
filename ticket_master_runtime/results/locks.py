"""Optional, explicitly pinned read-only Lock-Master adapter. Never scans roots.

The operator profile is a set of evidence pointers, not another lock registry.
Only the existing owner's fence_status decides whether a listed grant is held.
The session projection cannot supply profiles, executable paths or ownership.
"""
from __future__ import annotations

import json
from pathlib import Path
import types
import webbrowser

from .model import ResultsError, digest, now


def verified_file(value, sha256):
    if not isinstance(value, (str, Path)) or not isinstance(sha256, str):
        raise ResultsError("Lock-Master-Pfad und Quellenhash fehlen")
    path = Path(value)
    if not path.is_absolute() or path.is_symlink() or not path.is_file() or path.resolve() != path.absolute():
        raise ResultsError("Lock-Master-Dateipfad ist nicht physisch eindeutig")
    raw = path.read_bytes()
    if digest(raw) != sha256:
        raise ResultsError("Lock-Master-Quelle oder Beleg wurde verändert")
    return path, raw


class LockView:
    def __init__(self, profile_path, profile_sha256):
        self.profile_path, self.profile_sha256 = profile_path, profile_sha256
        self.has_gui = False

    def profile(self):
        _, raw = verified_file(self.profile_path, self.profile_sha256)
        profile = json.loads(raw)
        if not isinstance(profile, dict) or profile.get("schema") != "ellmos.ticket.lock-view-profile.v1" or not isinstance(profile.get("claims"), list):
            raise ResultsError("Unbekanntes Lock-Master-Profil")
        if not all(isinstance(profile.get(k), str) and profile[k] for k in ("session_id", "owner_module", "owner_module_sha256")):
            raise ResultsError("Lock-Master-Profil unvollständig")
        for claim in profile["claims"]:
            if (not isinstance(claim, dict) or type(claim.get("fence")) is not int or claim["fence"] <= 0
                    or not all(isinstance(claim.get(k), str) and claim[k] for k in ("agent", "owner", "path", "proof_path", "proof_sha256"))):
                raise ResultsError("Ungültiger belegter Lockverweis")
        if profile.get("gui") is not None and (not isinstance(profile["gui"], dict) or not all(isinstance(profile["gui"].get(k), str) for k in ("kind", "url", "source_path", "source_sha256"))):
            raise ResultsError("Ungültiger Oberflächenverweis")
        return profile

    def snapshot(self, session_id):
        profile = self.profile()
        if profile.get("session_id") != session_id:
            raise ResultsError("Lock-Master-Profil gehört zu einer anderen Session")
        source, raw = verified_file(profile["owner_module"], profile["owner_module_sha256"])
        # No sys.modules cache or ambient path imports. Approved lock_utils uses
        # only stdlib imports; executing its verified bytes writes no pycache.
        owner = types.ModuleType("_session_lock_owner")
        owner.__file__ = str(source)
        exec(compile(raw, str(source), "exec"), owner.__dict__)
        rows = []
        for claim in profile["claims"]:
            proof_path, proof_raw = verified_file(claim["proof_path"], claim["proof_sha256"])
            proof = json.loads(proof_raw)
            expected = {"session_id": session_id, "agent": claim["agent"], "owner": claim["owner"],
                        "fence": claim["fence"], "path": claim["path"]}
            # The pinned, operator-reviewed author proof must name this exact
            # grant and agent; prefixes and owner names alone prove nothing.
            if not isinstance(proof, dict) or any(proof.get(k) != v for k, v in expected.items()) or not proof.get("source_reference"):
                raise ResultsError("Session-/Agentzugehörigkeit des Locks nicht belegt")
            path = Path(claim["path"])
            if not path.is_absolute() or path.is_symlink() or path.resolve() != path.absolute():
                raise ResultsError("Lockpfad ist nicht physisch eindeutig")
            try:
                before = path.read_bytes()
                parsed = owner.parse_lock_file(path)
                status, _ = owner.fence_status(path, claim["fence"])
                after = path.read_bytes()
                if before != after or parsed.get("owner") != claim["owner"]:
                    status = "unknown"
            except OSError:
                status = "unknown"
            rows.append({**expected, "status": status, "observed_at": now(), "proof": str(proof_path)})
        self.has_gui = self.gui_target(profile) is not None
        return rows

    @staticmethod
    def gui_target(profile):
        gui = profile.get("gui")
        if gui is None:
            return None
        # This is only a user-initiated link to an already running, pinned local
        # dashboard. START.bat, scanners, daemons and shell commands are rejected.
        if gui.get("kind") != "existing_local_dashboard" or gui.get("url") != "http://127.0.0.1:8095/":
            raise ResultsError("Kein erlaubter Lock-Master-Oberflächeneinstieg")
        verified_file(gui["source_path"], gui["source_sha256"])
        return gui["url"]

    def open_gui(self):
        url = self.gui_target(self.profile())
        if url is None:
            raise ResultsError("Keine geprüfte vorhandene Lock-Master-Oberfläche konfiguriert")
        if not webbrowser.open(url, new=2):
            raise ResultsError("Lokale Oberfläche konnte nicht geöffnet werden; kein Dienst wurde gestartet")
