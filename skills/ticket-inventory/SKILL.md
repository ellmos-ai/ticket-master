---
name: ticket-inventory
description: "Use for Ticketinventur: fully reconcile a local ticket queue, preserve every remaining task, and prepare reversible, evidence-bound archives."
---

# Ticketinventur / Ticket inventory

This is a bounded workflow, not a background service or permission to execute the
projects described by tickets. Reuse the installed skill-finder, docs-analysis
(plan consolidation/document synopsis), and folder-organization (Cut-and-Clue)
workflows. If a required skill is unavailable, report that specific gap; do not
silently claim its review. Read the applicable project rules and original request.

## 1. Capture the complete source set

From the module root, run:

~~~sh
python bin/ticket_master.py --ticket-inventory --tickets-dir /path/to/queue
~~~

The German alias is --ticketinventur. This command is read-only, emits JSON and
starts no provider. --inventory-content includes private UTF-8 source text.
Save the JSON in a private report directory OUTSIDE the watched queue; never put
reports/sidecars in INBOX or the intake root. Do not publish private source text.
Use actual environment paths, not the example path above.

Capture includes every regular file under active folders, root, legacy folders,
attachments and other ancillary folders. It lists excluded evidence stores
(SOLVED, DONE, _archive) and operational caches. Inspect relevant SOLVED/archive
files separately as evidence. Unreadable files, changed files/directories and
links/reparse points produce INCOMPLETE_CAPTURE_HOLD and a nonzero exit. Opaque
files require the appropriate reader; a hash is not content review. Directory
names that merely resemble an exclusion do not exempt active sources.

## 2. Produce a complete semantic matrix

The capture is NOT a completed inventory. Read every active ticket and relevant
ancillary source, including all user additions, latest decisions, original scope,
acceptance criteria and conflicting chronology. Index every section with its
source path, raw SHA-256 and byte range. For each source/section record:

- Current authoritative directive and dated evidence, distinguishing historical
  claims, actual current readbacks and unknowns.
- Fulfilled, superseded, open, partial, unclear or paused status; preserve the
  full acceptance criteria and every remaining action.
- Canonical active successor/rest ticket and exact section mapping.
- Intended disposition and remaining acceptance/authority gates.

Never turn 75% completion, a PR, passing tests, a source review or a plan into
full completion. Superseded is not SOLVED. Preserve active/foreign claims,
per-host ledgers, user locks and specific pauses. A pause is not supersession.
Do not start previously paused work or infer new publication authority.

## 3. Consolidate and prepare a source-to-target plan

Group compatible remaining actions into useful, executable collection tickets;
retain distinct owners, host duties, pauses, dependencies and acceptance gates.
Use lib/ticket_writer.py for IDs and intake. Do not invent IDs or clone tickets.
Every open/partial/unclear requirement must have an active home BEFORE archiving
its source. Keep an original active when full preservation is not established.
A link alone is insufficient if archiving would conceal the only actionable rest.

Write a reviewable plan BEFORE actions: source path/raw hash, reason, destination,
canonical successor/section, preserved remainder, authority/locks, target absence,
collision/reparse checks and rollback destination. Record unresolved items as HOLD.
Use existing authorization for explicitly requested reversible archiving; do not
invent an additional approval step. Independent code/release gates still apply.

## 4. Apply only the proven safe plan

Recheck live source bytes, actual claims/locks/host identity and destination before
each operation. Use the existing ticket_mover.move_ticket API with expected_hash
for moves into _archive/<run>/originals; never a second move engine. Its CAS hash
is a normalized text hash: ALSO compare/store the raw byte SHA-256 independently.
Preserve original bytes and names. Keep annotations, source-to-target mappings,
reason and rollback receipts as separate private sidecars. Do not mark an
archived superseded ticket SOLVED. Do not overwrite a destination or mutate an
original to make its status match an archive folder. Preserve original IDs in
manifests and check archives when allocating/reconciling successors.

On uncertain/partial results, read actual source and destination state before
retrying. Rollback uses the same mover only when the original path is absent and
the archived raw hash still matches; it must refuse collisions. Unmodified
foreign work and evidence stay untouched.

## 5. Verify and report

Read every moved destination back byte-for-byte, verify source absence, active
rest/successor presence, full matrix coverage, no lost IDs/tasks and no INBOX
sidecars. Record completed and held actions separately, with exact rollback paths.
Refresh sources changed by other workers during the inventory. Deliver a concise
report plus private JSON manifest. New module code needs independent review;
author tests are not self-approval. Keep runtime/installation/publication and
whole-ticket acceptance separate from the source change.
