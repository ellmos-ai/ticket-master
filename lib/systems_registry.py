#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Builds the system-registry snapshot that routing schema v2 requires.

T-20260912-203012999: ``ticket_writer.py`` refuses every schema-v2 ticket with
"--systems-registry is required for routing schema v2", and nothing in the
system produced such a snapshot. The only reference shape lived in a test
fixture, so transfer/fork tickets were effectively unusable: the one place
that knew the format was a test.

The snapshot is DERIVED, never authored. Its source are the inventory seeds
each host already maintains (``.SYNC/_inventory/systems/<slot>.json``, written
by ``update-inventory.ps1`` / ``update-inventory-mac.py``). Writing a registry
by hand would fake exactly the evidence the routing contract asks for
("targets from a proven system-registry snapshot", P-009: reuse the existing
inventory, do not start a parallel register).

Output shape -- the contract ``routing_contract._registry_systems()`` enforces:

    {"source": "...", "checked_at": "...",
     "systems": {"<HOSTNAME>": {"slot": "...", "hostname": "..."}}}

``active`` is emitted only where a marker PROVES a host is paused
(T-20260912-311033160). The seeds themselves do not record it, so it is never
guessed: the state comes from the markers ``slot_state_check.py`` already
reads (T-20260830-351639684), and only the positive "paused" evidence
produces ``active: false``. Absent a marker the field stays out, and
``resolve_targets()`` applies its documented default of ``True``.

The distinction that matters: "externally maintained" is NOT inactive. A host
whose inventory seed carries ``_gepflegt_von`` simply has no sync actor of its
own -- mac-studio is a 24/7 server. Treating the two alike would drop the
strongest compute host out of routing, which is exactly the conflation
T-20260830-351639684 removed from the vacancy watch.

Usage:
    python lib/systems_registry.py --systems-dir <dir> --out <file>
    python lib/systems_registry.py --systems-dir <dir>        # prints JSON
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _read_seed(path: Path) -> dict:
    # utf-8-sig, not utf-8: surface.json carries a BOM (measured 2026-09-12),
    # and a plain utf-8 read raises there while the other seeds parse fine.
    return json.loads(path.read_text(encoding="utf-8-sig"))


_PAUSED_MARKER = "PAUSIERT.md"


def _is_paused(sync_root: Path | None, slot: str) -> bool:
    """True only when the slot carries the paused marker slot_state_check.py reads."""
    if sync_root is None:
        return False
    return (Path(sync_root) / slot / _PAUSED_MARKER).is_file()


def build_snapshot(
    systems_dir: Path, *, sync_root: Path | None = None, now: str | None = None
) -> dict:
    """Derive the routing snapshot from the inventory seeds in *systems_dir*.

    *sync_root* holds the per-slot folders carrying the state markers. It
    defaults to the seed directory's grandparent, matching the canonical
    ``<sync-root>/_inventory/systems`` layout; pass it explicitly when the
    seeds live elsewhere. Where no marker tree is found, no state is read
    and no ``active`` field is emitted.
    """
    systems_dir = Path(systems_dir)
    if not systems_dir.is_dir():
        raise FileNotFoundError(f"inventory seed directory not found: {systems_dir}")
    if sync_root is None:
        candidate = systems_dir.parent.parent
        sync_root = candidate if candidate.is_dir() else None

    systems: dict[str, dict[str, str]] = {}
    for seed in sorted(systems_dir.glob("*.json")):
        if ".bak" in seed.name:  # update-inventory keeps <slot>.json.bak siblings
            continue
        try:
            data = _read_seed(seed)
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"unreadable inventory seed {seed.name}: {exc}") from exc

        hostname = str(data.get("system", {}).get("hostname", "")).strip()
        if not hostname:
            # A seed without a hostname cannot identify a routing target.
            # Skipping is safer than inventing one from the file name.
            continue
        entry = {"slot": seed.stem, "hostname": hostname}
        role = str(data.get("system", {}).get("role", "") or "").strip()
        if role:
            entry["role"] = role
        if _is_paused(sync_root, seed.stem):
            entry["active"] = False
        systems[hostname] = entry

    if not systems:
        raise ValueError(f"no usable inventory seeds in {systems_dir}")

    lowered = [key.casefold() for key in systems]
    if len(lowered) != len(set(lowered)):
        raise ValueError("inventory seeds resolve to duplicate hostnames")

    return {
        "source": f"derived from inventory seeds: {systems_dir.as_posix()}",
        "checked_at": now or _utc_now(),
        "systems": systems,
    }


def write_snapshot(
    systems_dir: Path, out: Path, *, sync_root: Path | None = None,
    now: str | None = None,
) -> Path:
    snapshot = build_snapshot(systems_dir, sync_root=sync_root, now=now)
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Derive the routing-v2 system-registry snapshot from inventory seeds."
    )
    parser.add_argument("--systems-dir", required=True,
                        help="directory holding <slot>.json inventory seeds")
    parser.add_argument("--out", default=None,
                        help="write the snapshot here (default: print to stdout)")
    parser.add_argument("--sync-root", default=None,
                        help="root holding the per-slot state markers "
                             "(default: the seed directory's grandparent)")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    try:
        if args.out:
            path = write_snapshot(
                Path(args.systems_dir), Path(args.out),
                sync_root=Path(args.sync_root) if args.sync_root else None,
            )
            if not args.quiet:
                print(path)
        else:
            snapshot = build_snapshot(
                Path(args.systems_dir),
                sync_root=Path(args.sync_root) if args.sync_root else None,
            )
            print(json.dumps(snapshot, ensure_ascii=False, indent=2, sort_keys=True))
    except (FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
