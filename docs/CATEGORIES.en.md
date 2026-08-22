# Ticket Categories v1 — Clusters, Subcategories, Transitions

The category system of the ticket lifecycle, effective v1 (2026-07-31).
Replaces the flat model `ROOT | QUEUED | PENDING | .USER | SOLVED` with eight
clusters, finer subcategories, explicit entry/exit rules, and an autonomy
loop for operation without constant user round-trips.

Binding for: `prompts/TICKET-MASTER.de.md` / `TICKET-MASTER.en.md` (routing),
`prompts/TICKET-WRITER.*.md` (dedup scan — role moved out to
`ellmos-ai/system-auditor` on 2026-08-15, file remains only as a record of
origin), `lib/ticket_writer.py` (`_LIFECYCLE_SUBDIRS`, still the active
ID-assignment library — not to be confused with the relocated role, plus the
STATUS parser/validator),
`tickets/_templates/TICKET.txt` (STATUS field).

German version: [CATEGORIES.de.md](CATEGORIES.de.md)

---

## Cluster Overview

| Cluster | Folder | Meaning | Subcategories |
|---------|--------|---------|---------------|
| INBOX | `tickets/INBOX/` (root = alias) | newly arrived, not yet triaged | — |
| ACTIONABLE | `tickets/ACTIONABLE/` | actionable now: no blocker, no user dependency | — |
| QUEUED | `tickets/QUEUED/` | handed to a provider/agent, result pending | — |
| BLOCKED | `tickets/BLOCKED/` | external blocker (not the user) | `host-receipt`, `foreign-state`, `lock`, `quota`, `dependency` |
| WAITING | `tickets/WAITING/` | time- or marker-bound | `scheduled`, `review-due`, `marker` |
| USER | `tickets/USER/` | strictly depends on the user | `decision`, `data`, `freigabe`, `hardware`, `session`, `marker` |
| PARKED | `tickets/PARKED/` | deliberately set aside | `skip`, `backlog`, `until-trigger` |
| SOLVED | `tickets/SOLVED/` | solved and empirically confirmed | — |

**Legacy aliases (backwards-compatible, readable, no new entries):**
`PENDING/` → existing content is distributed once across
ACTIONABLE/USER/BLOCKED/WAITING/PARKED; `.USER/` → `USER/`; the root
directory (`tickets/*.txt`) counts as INBOX.

---

## Subcategories

### BLOCKED (external blocker — never the user)

- `host-receipt` — waiting for a receipt/callback from another host or agent
  (name the receipt path in the ticket).
- `foreign-state` — foreign state: another repo/instance has an unresolved
  state (dirty tree, open foreign operation) that must be cleaned up first.
- `lock` — active lock (e.g. `LOCK*.txt` / `LOCK.permissions.json` from a
  permission/lock system); user locks are absolute.
- `quota` — usage limit, token or quota boundary of a provider.
- `dependency` — functional/technical dependency on another ticket, module,
  or release (name the reference in the ticket).

### WAITING (time-/marker-bound)

- `scheduled` — fixed date/schedule; work starts at the appointed time.
- `review-due` — a review is due (content-wise, not blocked).
- `marker` — waiting for an autonomously observable marker file or defined
  event; use `USER/marker` instead when only the user can establish or confirm
  that it occurred.

### USER (next step is strictly the user)

- `decision` — the user must make a decision (options inside the ticket).
- `data` — the user must supply data, information, or credentials.
- `freigabe` — an explicit user approval/release is required (German term
  kept for consistency with the source taxonomy).
- `hardware` — a physical step/device only the user can perform.
- `session` — a user-only launchable model, login session, or manual run.
- `marker` — a marker/event must be supplied or confirmed by the user. Example:
  the user confirms that a competition has ended. An autonomously observable
  marker remains `WAITING/marker`.

### PARKED (deliberately set aside)

- `skip` — deliberately skipped/discarded but not deleted (kept on record).
- `backlog` — sometime later, no defined trigger.
- `until-trigger` — set aside until a named event occurs.

---

## STATUS Mirroring

A ticket's `STATUS` field mirrors its folder and subcategory:

```
STATUS:        <CLUSTER>[/<subcategory>] (since YYYY-MM-DD)
```

Examples: `STATUS: ACTIONABLE (since 2026-07-31)`,
`STATUS: BLOCKED/host-receipt (since 2026-07-31)`,
`STATUS: USER/marker (since 2026-07-31)`.

- Folder and STATUS must be congruent.
- Every move between clusters updates STATUS and appends a `HISTORY`/`LOG`
  line with the reason.
- The folder layout stays flat: `USER/decision`, `BLOCKED/dependency` and
  similar subfolders are invalid. The subcategory exists only in the STATUS
  field; `ticket_mover.py` rejects such destinations fail-closed and
  `ticket_audit.py` reports existing nested tickets read-only.

---

## Entry/Exit Rules

- **INBOX** — Entry: every new ticket (intake). Exit: triage (GATE 1 +
  urgency gate) → ACTIONABLE (actionable/delegatable), QUEUED (handed over
  directly), BLOCKED/WAITING/USER/PARKED (with a stated reason), or SOLVED
  (fast-lane, solved and verified immediately).
- **ACTIONABLE** — Entry: triage "actionable now" or unblocking from
  BLOCKED/WAITING/USER/PARKED. Exit: QUEUED (delegation running), SOLVED
  (solved directly), or demotion with a new reason.
- **QUEUED** — Entry: handover to provider/sub-agent. Exit: SOLVED (GATE 4
  success), ACTIONABLE (failure → fallback chain), BLOCKED/quota (limit
  hit), or USER (user-only step required).
- **BLOCKED** — Entry only with a named, evidenced blocker + subcategory.
  Exit: ACTIONABLE once the blocker is empirically gone; alternatively
  PARKED/until-trigger if the blocker is permanent.
- **WAITING** — Entry only with a date or marker. Exit: ACTIONABLE when the
  date/marker arrives; PARKED if the appointment is cancelled.
- **USER** — Entry only if the next step is strictly the user's
  (subcategory mandatory). Exit: ACTIONABLE after the user's
  decision/delivery; PARKED/skip if the user discards it.
- **PARKED** — Entry only on explicit order or a defined trigger. No
  automatic resumption. Exit: ACTIONABLE on trigger/order; SOLVED only
  after genuine completion.
- **SOLVED** — Terminal state; only with empirical confirmation in the
  SOLUTION/RESULT field.

Ground rule: a ticket leaves BLOCKED/WAITING/USER/PARKED only with evidence
(receipt, date, user answer, trigger) — never by assumption.

---

## Autonomy Loop

Operation without constant user round-trips:

- **BLOCKED → periodic re-check.** At session start and at intervals, check
  whether the blocker is gone (receipt arrived? lock released? quota back?
  foreign state cleaned? dependency solved?). Blocker empirically gone →
  pull to ACTIONABLE and work it — do not leave it lying around.
- **USER → present in batches.** Collect USER tickets and present them as
  ONE batched brief (no individual pings). After the user's answer, re-file
  each ticket immediately (usually ACTIONABLE or PARKED/skip). This includes
  `USER/marker`; do not silently reinterpret that marker as autonomous
  `WAITING/marker`.
- **WAITING → pull on date/marker.** Check `scheduled`/`review-due` on day
  change, `marker` on every run; once reached → ACTIONABLE.
- **PARKED → no auto re-check.** Only on explicit order or when the named
  until-trigger event fires.

---

## Migration from the Flat Model (Transitional Rules)

- **SOLVED, QUEUED:** unchanged.
- **PENDING/ → one-time distribution of existing content.** Content decides:
  blocker merely named but outdated/gone → ACTIONABLE; user dependency →
  USER (with subcategory); external blocker → BLOCKED (with subcategory);
  date/marker → WAITING; deliberately later → PARKED.
- **.USER/ → USER/:** carry content over and assign subcategories.
- **Root (`tickets/*.txt`):** = INBOX (unclaimed/intake) — unchanged.
- **No new entries** into `PENDING/` or `.USER/`. Both folders remain
  readable as legacy aliases; ID assignment (`lib/ticket_writer.py`,
  `_LIFECYCLE_SUBDIRS`) keeps counting them so no ticket ID is issued twice.

---

## Multi-Host Note

In cloud-synced multi-host setups (OneDrive, Dropbox, Google Drive) the
claim convention via filename (`T-YYYYMMDD-#########.<HOST>.txt`) applies
unchanged. The 9-digit random component is minted exclusively by
`lib/ticket_writer.py`; it must never be chosen or incremented manually. The
cluster folders are shared across hosts.

- Hosts on the old layout keep reading `PENDING/` and `.USER/` as legacy
  aliases — old content is not an error. A one-time migration run per
  instance performs the moves.
- `BLOCKED/host-receipt` is the canonical place for tickets waiting on
  another host; name the receipt path in the ticket so the autonomy loop's
  re-check can work on evidence.

### Routing schema v2 and circulating contracts

Schema v2 separates target, execution and ownership in the filename:
`T-ID[.to-<target>][.via-<Clutch selector>][.claim-<HOST>].txt`. Only tickets
with `ROUTING_SCHEMA: 2` may use these reserved segments. An old
`T-ID.<HOST>.txt` is always a legacy claim and is never reinterpreted as a
target or a host set.

Transfer and fork tickets contain an evidence-bearing `TARGET_SYSTEMS`
snapshot fixed at creation time and exactly one `SYSTEM_LEDGER` row per target.
An eligible system changes only its own row from `pending` to `claimed`, then
to `done` or `blocked` after an empirical receipt. It then releases `.claim-…`;
`.to-…` and an unexpired `.via-…` binding remain. `SOLVED` is forbidden while
any required row is not `done`. A transport state such as `delivered` is not a
ledger state and can never prove domain completion.

An expired execution binding is recorded as `expired-unbound` before the next
successful claim. This removes only `.via-…`; it never changes the target
snapshot, ledger or an active claim. A blocked system share can remain visible
in the circulating contract or move to `BLOCKED/host-receipt` with an evidenced
reason when no executable share remains.

`.SYNC` remains only the transport and receipt surface. ticket-master owns the
contract, claim, ledger and completion predicate; Clutch owns execution
resolution; system-gap-master owns the cross-system protocol. ticket-master
adds no second lifecycle, retry loop or transport inbox.
