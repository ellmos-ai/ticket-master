# Changelog

All notable changes to ticket-master are documented here.

## [Unreleased]

### Maintainer-Verifikation & Audit-Fix (2026-08-10)
- 86 Pytest-Tests, Smoke-Checks 4/4, Ruff, `compileall` und die Writer-/Mover-
  CLI-Hilfen lokal verifiziert. Der read-only Ticket-Audit meldete danach keine
  Befunde mehr: versionierte `.gitkeep`-Platzhalter in leeren Lebenszyklus-
  Ordnern gelten nicht als Ticket-Clutter.
- Die Audit-Ausnahme ist durch einen Regressionstest abgesichert; Tickets,
  Statusordner und Remote-Zustände wurden nicht verändert.

### Added

- **Fail-closed ticket move + collision audit (T-20260808-03).** No code path
  existed for moving a ticket between lifecycle folders; moves happened by
  hand, and on 2026-08-08 that silently overwrote a ticket that had lived in
  `SOLVED/` since 2026-08-01 — the write looked normal on readback, the loss
  was only caught because a folder's file count didn't grow. `lib/ticket_mover.py`
  adds `move_ticket()`: atomic exclusive-create (`O_EXCL`) of the destination,
  so a same-named file there is refused rather than overwritten, with a
  compare-before-delete readback of both source and destination before the
  source is removed. Verified empirically (unit test + a live CLI run): a
  move onto an occupied target fails and leaves both files byte-identical to
  before. `lib/ticket_writer.py` gained a matching CLI (`--title/--body/...`)
  so a fresh ID no longer has to be picked by eyeballing the directory — the
  same manual picking, bypassing this module's existing atomic
  exclusive-create, produced the same-minute ID collision between two agents
  on one host that led to this ticket in the first place. `lib/ticket_audit.py`
  is a new read-only health check reproducing the ticket's own 231-ID sweep:
  live ID collisions, claimed tickets sitting in the root/INBOX alias
  (invisible to every status-folder-based triage — one such ticket carried
  same-day urgency and sat unworked for seven days), and non-ticket files in
  the tree. Tuned against the real bestand: an initial version flagged ~100
  legitimate legacy tickets (`T-YYYYMMDD-NN_slug.txt`, pre-dating the current
  bare-ID convention) as false-positive clutter, which would have buried the
  one real find; a broader "looks like a ticket" pattern for the clutter scan
  (kept separate from the strict ID-extraction pattern used for collision
  detection) cut that to 18 genuine anomalies. 26 new tests, including the
  two known production collisions (`T-20260731-02`, `T-20260731-03`)
  reproduced as fixtures. The two existing collisions are NOT renumbered by
  this change — both filenames are externally referenced from other tickets,
  and choosing which reference chain to preserve is a user decision, not an
  automated one.
- **Stage-0 domain-level skill matching in `lib/domains_generator.py`
  (T-20260808-02).** Stages 1 (exact provenance) and 2 (fuzzy keyword/token)
  both compare a component against an EXPERT's own name/description, so
  neither can see a standalone skill that supersedes a WHOLE boss agent
  instead of any one of its named experts (empirically observed: a `buero`
  skill covering all four experts of the `bueroassistent` boss, and a
  `finanz-versicherung` skill covering the `versicherungen` boss, which
  orchestrates zero named experts at all). `match_domain_skill()` adds a
  third pass, restricted to whole-token equality against the domain's own
  `id`/`label` (never free-text description, never substring bridging — see
  the T-20260711-04/-05 regressions this deliberately avoids repeating),
  merged into every expert that lacks a stronger match as `"match":
  "domain"`; a boss with zero orchestrated experts gets a synthetic
  `"__domain__:<boss>"` pseudo-expert instead. Verified against the real
  2026-08-08 corpus: `buero`'s four experts and `versicherung`'s pseudo-expert
  now correctly carry their standalone skills, `gesundheit`'s `health_import`
  gains `claude-skill:gesundheit` as a side effect, and a full diff across
  all five live domains shows zero regressions (no expert lost a match, no
  `"status": "portiert"` expert was touched). 11 new tests in
  `tests/test_domains_generator.py`; `config/domains.example.json`
  documents the new shape. Root-cause note: this closes Befund B of
  T-20260808-02; Befund A (which of two colliding skill-registry schemas is
  canonical) remains an open decision, not implemented here.
- **New role TICKET-WRITER (SIG-TU — System Integrity Guardian with Ticket and
  USMC adapter), T-20260731-20.** Loop-based, read-only integrity sweeps: one
  externally assigned area per run (loop contract with explicit assignment,
  optional external selector, or rotation over `areas[]`), mandatory ABC
  evidence schema per finding (A problem/location, B violated rule, C
  recommendation basis), explicit counterargument + drift verdict
  (unwanted/wanted/no drift), thematic bundling within a run only, dedup duty,
  USMC run logging with local file fallback, and a null-finding protocol (no
  ticket when nothing is found — session report only). Ships as
  `prompts/TICKET-WRITER.de.md` + `prompts/TICKET-WRITER.en.md` with
  `config/ticket-writer.config.example.json`.
- **`<HOME>`/`<USER>` placeholder convention for `ticket-master.config.json`
  (T-20260808-01).** `tickets_dir` and `project_roots[].path` may now use
  `<HOME>` (current user's home directory) and `<USER>` (OS user name)
  instead of a literal, host-specific path — the convention already
  established in `config/ticket-writer.config.example.json`. Needed once a
  config lives in a folder synced across several machines, where a literal
  path only resolves on the host it was written on. Documented in
  `prompts/TICKET-MASTER.de.md` / `.en.md` (agent substitutes the placeholder
  before any file access), `config/ticket-master.config.example.json` (new
  worked example) and both READMEs.
- **Category system v1 (T-20260731-22).** The flat lifecycle
  `ROOT | QUEUED | PENDING | .USER | SOLVED` is superseded by eight clusters
  with subcategories, explicit entry/exit rules and an autonomy loop —
  specified in the new `docs/CATEGORIES.de.md` / `docs/CATEGORIES.en.md`.
  New cluster folders ship in `tickets/` (INBOX, ACTIONABLE, BLOCKED,
  WAITING, USER, PARKED); `PENDING/` and `.USER/` stay readable as legacy
  aliases (no new entries).

### Changed

- **Prompts, template, lib and tests moved to categories v1
  (T-20260731-22).** `prompts/TICKET-MASTER.de.md` / `.en.md` route to
  `USER/` / `BLOCKED/` / `PARKED/` instead of `.USER/` / `PENDING/`; the
  TICKET-WRITER dedup scan covers the new cluster folders;
  `tickets/_templates/TICKET.txt` documents the `CLUSTER[/subcategory]`
  STATUS format; `lib/ticket_writer.py` `_LIFECYCLE_SUBDIRS` counts all v1
  clusters plus the legacy aliases (backwards-compatible; QUEUED/SOLVED
  unchanged); smoke/lib tests cover the new layout. `README.md` /
  `README_de.md` describe the new directory layout and link the spec.

### Documentation & Maintenance

- Replaced the legacy `dev-bricks ticket-master` search phrase with the
  canonical `ellmos-ai ticket-master` identifier in both READMEs and
  `llms.txt`; refreshed the LLM index check date.
- Corrected repository URLs and canonical search identifiers from the former
  `dev-bricks/ticket-master` location to `ellmos-ai/ticket-master`; updated
  `llms.txt` Last-checked to 2026-08-03.
- Synchronized `README_de.md` with the canonical English README: restored all
  sections and configuration fields, aligned heading levels and links, and made
  all code and Mermaid blocks byte-identical across both language versions.
- Updated `llms.txt` Last-checked timestamp to 2026-07-27 and verified Pytest test suite contract (55 passed tests).
- Updated `llms.txt` Last-checked timestamp to 2026-07-26.
- Added standard `pyproject.toml` with PEP 621 metadata and Pytest configuration (`[tool.pytest.ini_options]`).
- Added GitHub Actions test CI workflow (`.github/workflows/tests.yml`).
- Added `> [!NOTE]` callouts referencing `llms.txt` in `README.md` and `README_de.md`.
- Enhanced Shields.io badges in `README.md` & `README_de.md` (Pytest status 55 passed, Python 3.10+, LLM-Ready, Multi-Provider).
- Completed Discoverability & Marketing Audit (Pfad B).

### Security

- Hardened local repository hygiene for additional credential and runtime data
  names: `.pypirc`, password JSON files, recovery-code files and SQLite
  sidecars are now ignored and covered by the smoke-test privacy defaults.

## [1.9.0] — 2026-07-04

### Added

- **System-knowledge layer (Phase 4 of the personal-assistant expansion,
  T-20260704-02).** User framing: what makes the ticket-master a personal
  assistant isn't just routing logic, it's *knowledge about the system it
  routes for* — where things are, what state they're in, what it's capable
  of, and how its user tends to decide. `config/knowledge.json` (schema:
  `config/knowledge.example.json`) lists `knowledge_sources` in four
  categories, each source carrying `kind` (`file` | `command` | `mcp_tool`)
  and `target`:
  - **`maps`** (structural, relatively stable — a control-plane manifest,
    `domains.json`, a project/repo registry, a system inventory): loaded/
    skimmed once at session start.
  - **`state`** (changes during the session — a lock overview, open
    tickets, a task queue): re-checked before EVERY routing decision, not
    just at boot.
  - **`capabilities`** (what the system can do and how to reach it — a
    skill-catalog command/MCP tool, an MCP server inventory, a model
    router): consulted as needed, above all at the ENDPOINT lookup and at
    model selection.
  - **`user_model`** (a preference/decision hint, e.g. a theory-of-mind
    tool): consulted only on genuine borderline calls, never routinely.
  Both prompts (EN/DE) gained a new optional startup step, **(c3) Load
  SYSTEM KNOWLEDGE**, right before going to Position 0, plus the ground
  rule: **trust generated maps over your own memory** — on a conflict
  between what a map says and what you recall from earlier in the session,
  the map wins; if you suspect the map itself is stale, have it regenerated
  rather than trusting memory. `config/knowledge.json` is gitignored and
  site-specific, same pattern as `domains.json`/`urgency.json`.
- Field-naming note: the config uses `when_to_read` (English) rather than a
  German field name, for consistency with every other config file in this
  module (`domains.example.json`, `urgency.example.json`,
  `ticket-master.config.example.json`), all of which use English field
  names regardless of prompt language.

### Fixed (advisor review, T-20260704-02)

- **`_tokenize()` silently split German umlauts/ß out of a word.**
  `[a-zA-Z0-9]+` is ASCII-only, so e.g. `"Fördermittelberater"` tokenized to
  `{"f", "rdermittelberater"}` instead of one token — a stage-2 token-overlap
  match against any non-ASCII expert/skill name was silently lost, no error
  or warning. Fixed to a Unicode-aware `[^\W\d_]+|\d+` (Python's `\w` is
  Unicode-aware by default), verified against umlauts (ö/ü) and ß.
  Re-verified against the real installation: no real BACH expert name in
  this system's boss-agent frontmatter actually contains an umlaut, so
  regenerating `config/domains.json` with the fix produced byte-identical
  output (aside from the `generated_at` timestamp) to the pre-fix run — the
  bug existed but had not yet silently dropped a real match here.
- **Exact-match exclusion (stage 1 → stage 2 pool) is now GLOBAL, not just
  per-boss.** Previously, a skill exact-matched to an expert in one boss
  could still be fuzzy-matched to an unrelated expert in a *different* boss,
  since the exclusion set was recomputed fresh per boss. `build_domains()`
  now reads all bosses' frontmatter up front and computes one global
  exact-match set before any stage-2 matching happens, so a skill claimed
  exactly anywhere is excluded from fuzzy matching everywhere.
- Tests: `TestTokenize` (4 cases: two umlaut variants, ß, digit tokenization
  unaffected), a fuzzy-match regression case with an umlaut expert name, and
  two new `TestBuildDomains` end-to-end cases (same-boss exclusion — the
  explicitly requested regression test — and cross-boss exclusion,
  demonstrating the global-vs-per-boss fix). Full suite: 39/39 green
  (32 → 39).

### Fixed (fresh-agent retest findings B2–B6, T-20260704-02)

A fresh sub-agent ran both user example tickets end to end ("passed with
findings"). Prompt-only fixes (both languages where applicable):

- **B2 — GATE 1 couldn't resolve a project outside `project_roots[]`.**
  Intake and GATE 1 now note that an optional repo/system-inventory `maps`
  source (see the SYSTEM KNOWLEDGE step) can serve as an additional project
  anchor before treating GATE 1 as unconfirmed.
- **B3 — mandatory-read chains weren't assigned to a role.** New rule: a
  project's own mandatory-read chain (e.g. `CLAUDE.md` pointing further) is
  read by the WORKER, not the Master — the Master only passes the
  entry-point pointer in the task (Lean Router principle).
- **B4 — GATE 3's ">10% usage limit" isn't actually queryable.** Weakened
  to an explicit best-effort self-assessment (throttling signals, session
  context) rather than an exact percentage check; harnesses with a real
  queryable source should reference it instead.
- **B5 — the keyword-trigger rule and the "diagnose first" rule looked like
  they could collide.** Made explicit: the keyword rule decides WHEN (now),
  the severity-unclear rule decides WHAT (a diagnosis sub-agent) — together
  they are one instruction (dispatch a diagnosis sub-agent immediately), not
  a conflict.
- **B6 — tooling note for `maps` lookups.** Broad directory scans over
  large/cloud-synced folders can be timeout-prone; use targeted read/grep
  access or a dedicated file tool instead (both prompts, generic; concrete
  tool name in the private instance).
- **B1 (no fix needed):** all real experts currently show `"nicht-portiert"`
  — expected GAP-by-design behaviour until skills are ported, not a bug.

## [1.8.0] — 2026-07-04

### Added

- **Stage-2 (fuzzy) skill matching in `lib/domains_generator.py`
  (T-20260704-02 follow-up).** Stage 1 (`match_standalone_skill`) is a
  strict, exact 1:1 provenance link and misses two real cases: an expert
  that governs a whole skill FAMILY sharing a registry `category` rather
  than a single ported skill, and skills extracted as standalone but never
  registered in the main skill registry. `fuzzy_match_skills()` adds a
  second pass, run only when stage 1 found nothing:
  - `KEYWORD_CATEGORY_HINTS`: a small, curated keyword-stem table, matched
    **only against the expert's own name** (never the boss-level
    description, which is shared verbatim across every expert of the same
    boss and was found, empirically, to leak matches onto unrelated sibling
    experts) — mapping to a registry `category` and/or a set of term-stems
    to substring-match against a component's own id/name/description.
  - Plain token overlap between the expert's name (role-suffix stripped)
    and a component's own id/name/description.
  - Components already claimed by stage 1 for a sibling expert in the same
    boss are excluded from the fuzzy pool, so a skill cannot end up both
    exactly matched to one expert and coincidentally fuzzy-matched to
    another.
  Results are marked `"status": "teilportiert"` / `"match": "fuzzy"` with a
  `"matched_skills"` list (as opposed to `"status": "portiert"` /
  `"match": "exact"` / a single `"standalone_skill"` for stage 1). Verified
  empirically against a real installation: an expert whose own name
  contains a hinted keyword stem correctly resolves to its entire matching
  skill category (double digits of skills), while unrelated experts (no
  matching stem, no token overlap) correctly stay `"nicht-portiert"` — an
  earlier version of this heuristic that also matched on the shared
  boss-level description was found to over-match broadly across a
  100+-skill corpus and was tightened before release.
- **`load_extra_skills()` + `--extra-skills-dir` / `TICKET_MASTER_EXTRA_SKILLS_DIR`:**
  optionally folds a second, independent skill inventory (e.g. a Claude Code
  `~/.claude/skills/` tree) into the stage-2 fuzzy pool — useful when a
  skill was extracted as standalone but is absent from the main registry.
  These entries have no `category`, so they can only match via the
  `KEYWORD_CATEGORY_HINTS` term-substring path or token overlap, never via
  category equality. New `source.extra_skills_dir_provided` /
  `source.extra_skills_scanned` fields in the generated `domains.json`.
- `config/domains.example.json`: schema extended with `match` /
  `matched_skills` per expert, and three illustrative entries (exact /
  fuzzy family / gap).
- **Wording pass across both prompts (EN/DE) and the private instance
  clarifying the routing model (user architecture note, T-20260704-02):**
  `domains.json`'s `experts[]` is provenance/grouping metadata only — the
  gates read the skill field directly (`standalone_skill` /
  `matched_skills`), never the expert name as a routing target; there is
  nothing to "activate". A `"teilportiert"` match equips the worker with
  **all** skills in `matched_skills`, not just the first. Both prompts also
  gained an optional, harness-agnostic note that a domain-appropriate worker
  role/agent type may be selected in addition to the resolved skills, when
  the harness supports predefined roles.
- Tests: `tests/test_domains_generator.py` gained `TestFuzzyMatchSkills` (4
  cases, including the negative case that a generic role suffix alone must
  not cause a false match), `TestLoadExtraSkills` (3 cases), and two new
  `TestBuildDomains` end-to-end cases (category-hint stage-2 match; extra
  skills dir feeding stage 2). Full suite: 32/32 green.

## [1.7.0] — 2026-07-04

### Added

- **Urgency axis (Phase 2 of the personal-assistant expansion,
  T-20260704-02).** `config/urgency.json` (schema:
  `config/urgency.example.json`) maps each domain (from `config/domains.json`)
  to a default deadline — `sofort` / `heute` / `woche` / `backlog` — plus
  escalation rules. This axis is deliberately **decoupled** from the
  5-dimension complexity score in the main prompt (Clarity/Complexity/
  Creativity/Context/Criticality): a ticket can be low-complexity and urgent,
  or high-complexity and not urgent. Both agent prompts (EN/DE) gained a new
  **URGENCY GATE** right after GATE 1: read the domain default, check
  escalation rules (published/production software + a severe bug → `sofort`,
  dispatching a lean diagnosis-only sub-agent first if severity is unclear;
  trigger keywords → `sofort`), optionally consult a configured
  `preference_model_hint.command` on genuine borderline cases, and always ask
  the user instead of guessing on low confidence
  (`low_confidence_policy`). `woche`/`backlog` tickets go to an optional
  `task_db_command` "later" sink instead of spawning a sub-agent.
- **Delegation wiring (Phase 3 of the personal-assistant expansion,
  T-20260704-02).** GATE 1 (intake) now also resolves a `DOMAIN`/`ENDPOINT`
  when `config/domains.json` matches the ticket, in this lookup order: (1)
  `domains.json` itself (`experts[].standalone_skill` when already
  `"portiert"`), (2) skill-registry tools if available
  (`controlcenter_find_skill` MCP tool / a local `skill-finder`-style skill),
  (3) if neither yields a skill despite a domain/usecase match, flag it as a
  **GAP** (`ENDPOINT: GAP — no standalone skill yet (<expert>)`) instead of
  silently falling back — `domains.json`'s `experts[]` is provenance metadata
  only, not a routing hop; the ticket routes directly to the resolved skill.
  Model selection now prefers an
  optional external `router_command` (config field) over the built-in
  score→tier formula, which is downgraded to an explicit **fallback** (used
  only when `router_command` is unset or unreachable) — the duplicated
  scoring logic in the prompt is kept, not removed, since it remains the
  fallback path. A new permission-check step runs before every worker spawn
  in section (B): check the target project for `LOCK*.txt` /
  `LOCK.permissions.json`-style conventions (precedence
  `deny > ask > allow`; user locks are absolute) if such a system is in use.
- New template fields `DOMAIN` / `ENDPOINT` / `URGENCY` in
  `tickets/_templates/TICKET.txt`.
- New config fields `router_command` / `task_db_command` in
  `config/ticket-master.config.example.json` (both optional, default `null`).
- Tests: existing suite re-verified green (23/23) after the prompt/template/
  config additions; no new BACH-specific or absolute-path strings introduced
  (checked against `tests/test_smoke.py`'s anonymisation check).

## [1.6.0] — 2026-07-04

### Added

- **`lib/domains_generator.py` (Phase 1 of the personal-assistant expansion,
  T-20260704-02).** Generates `config/domains.json`, a domains registry that
  maps boss-agent domains to their experts and, where one already exists, the
  matching standalone skill. Reads a boss-agent `SKILL.md` frontmatter
  (`orchestrates.experts`, `description`) and cross-references each expert
  against a skill registry's `components.json`
  (`provenance.origin: bach` / `origin_path`). Experts without a standalone
  counterpart are marked `"status": "nicht-portiert"` (to be closed later via
  a skill-extractor pass). `config/domains.json` is itself BACH-free at
  runtime — the generator only needs BACH access once, at generation time, and
  aborts cleanly (leaving any existing file untouched) if the BACH agents
  directory is not available. Site-specific and gitignored, like
  `config/ticket-master.config.json`; see `config/domains.example.json` for
  the schema. Tests: `tests/test_domains_generator.py`.

## [1.5.0] — 2026-07-04

### Changed

- **Audit trail is now PER TICKET; the shared intake log is deprecated.**
  Back-alignment from the battle-tested private instance of this workflow:
  with several machines appending to one cloud-synced
  `tickets/_logs/INTAKE-TRIAGE-LOG.txt`, sync conflict copies ate log lines.
  The audit/triage trail now lives inside each ticket's own
  `T-….<HOST>.txt` (`STATUS` / `LOG` / `SOLUTION` fields). Trivial,
  immediately verified one-liners get a **minimal** ticket file dropped
  directly into `tickets/SOLVED/` instead of a shared log line.
  Updated: both prompts (decision ladder 3c + LOGGING section), both READMEs,
  `SKILL.md`, `llms.txt`; `logging.intake_log` removed from the config
  example; the `_logs/` file itself is kept as a deprecation stub so legacy
  checkouts and old references do not break.

## [1.4.1] — 2026-07-04

### Fixed

- **`lib/ticket_writer.py`: ticket loss and duplicate IDs prevented.**
  `create()` now opens the target exclusively (`"x"`) and retries with the
  next number on collision — a concurrent creator (second machine, cloud
  sync) can no longer silently overwrite an existing ticket. Ticket numbering
  now scans **all** lifecycle folders (root intake, `QUEUED/`, `PENDING/`,
  `SOLVED/`, `.USER/`) instead of only `QUEUED/`, so a ticket that was moved
  on no longer frees up its ID for reuse.
- **`lib/doc_scanner.py`: `append_entry()` no longer corrupts non-UTF-8
  documents.** Previously a cp1252-encoded `TODO.md` was read with
  `errors="replace"` and written back with U+FFFD replacement characters —
  permanently damaging curated content. Now the read is strict and raises
  `ValueError` with a clear message, leaving the file untouched.
- **`tests/test_smoke.py` had no effect under pytest:** the four checks
  returned booleans, which pytest counts as PASSED regardless of outcome
  (only a `PytestReturnNotNoneWarning`). They are now `check_*` helpers with
  real `test_*` assert wrappers; `python tests/test_smoke.py` still works.
- **Stale references to the pre-1.3.0 log location:** `tests/test_smoke.py`
  (`REQUIRED_PATHS`, gitignore check) and two `.gitignore` lines still
  pointed at `tickets/INTAKE-TRIAGE-LOG.txt`; the file moved to
  `tickets/_logs/` in 1.3.0 — the documented `python tests/test_smoke.py`
  call failed on a correct checkout. Prompt short references (decision
  ladder 3c, EN+DE) now use the full `tickets/_logs/…` path too.
- Version badge (READMEs) and `SECURITY.md` supported-versions table were
  stuck at 1.3.0/1.0.x; `prompts_dir` in the config example is documented
  as reserved (the `bin/` launchers do not read it yet).
- `.gitignore`: ignore local `LOCK*.txt` coordination files.

### Tests

- 7 → 11 green (`py -m pytest tests/`): ID uniqueness across lifecycle
  folders, no-overwrite collision retry, strict-UTF-8 append (reject + happy
  path).

## [1.4.0] — 2026-06-27

### Added

- **`lib/ticket_writer.py`:** user-neutral helper for asynchronous ticket creation — drop an
  unclaimed `T-YYYYMMDD-NN.txt` into `<tickets_dir>/QUEUED/` even when no TICKET-MASTER session
  is running (e.g. from a lock-watcher GUI). `tickets_dir` is required or read from
  `TICKET_MASTER_TICKETS_DIR`; the date is injectable for deterministic tests.
- **`lib/doc_scanner.py`:** scan / create / append the four project control documents
  (`TODO.md`, `AUFGABEN.txt`, `DONE.md`, `DECISIONS.md`) without overwriting curated content;
  `DECISIONS.md` is created in ADR format.
- **`tests/test_lib_helpers.py`** covering both helpers.

### Notes

- Mirrored from the running `_scripts/` instance used by the lock-watcher; this module is the
  user-neutral publishable copy.

## [1.3.0] — 2026-06-19

### Added

- **Cloud-Ready / Multi-System Claim Convention:** When the `tickets/` directory is
  shared across multiple machines via a cloud-synced folder (OneDrive, Dropbox, Google
  Drive), claims are signalled via filename rename — `T-YYYYMMDD-NN.txt` (unclaimed)
  → `T-YYYYMMDD-NN.<HOST>.txt` (claimed). Atomic on NTFS; no lock files needed.
  Documented in both prompts (new `MULTI-SYSTEM CLAIM CONVENTION` section), both
  READMEs (new `Cloud-Ready` sections), `SKILL.md`, and `llms.txt`.
- **`tickets/_logs/` sub-directory:** Audit trail (`INTAKE-TRIAGE-LOG.txt`) moved from
  `tickets/` root into `tickets/_logs/` to keep the ticket queue clean.
  Existing `INTAKE-TRIAGE-LOG.txt` migrated; all references updated (prompts, config
  example, `llms.txt`, READMEs).
- Added README/README_de discovery context and `llms.txt` search notes so the
  project is easier to distinguish from Ticketmaster event APIs, support-ticket
  SaaS, ticket bots, and resale marketplaces.

### Changed

- Both agent prompts: log path updated from `tickets/INTAKE-TRIAGE-LOG.txt` to
  `tickets/_logs/INTAKE-TRIAGE-LOG.txt`.
- `config/ticket-master.config.example.json`: `logging.intake_log` updated to
  `_logs/INTAKE-TRIAGE-LOG.txt`.
- Both READMEs: Ticket Lifecycle section replaced by expanded Directory Layout +
  Cloud-Ready section; version badges bumped to 1.3.0.
- `SKILL.md`: description and body updated to mention Cloud-Ready and `_logs/` path.
- `llms.txt`: description updated; audit trail path corrected; last-checked updated
  to 2026-06-19.

## [1.2.1] — 2026-06-14

### Changed

- README banner: replaced the small centered icon with a full-width banner
  (`assets/banner.svg`) — icon motif plus wordmark and tagline, edge-to-edge.

## [1.2.0] — 2026-06-14

### Changed

- Reframed ticket-master as a **workflow / operating mode** for an AI coding agent
  rather than an autonomous tool that acts on its own. Sharpened the framing in both
  READMEs and `llms.txt` using a canonical description; reworded passages that
  presented the program as the acting subject so that the *agent* performs each step
  by following the prompt.
- Version badges in both READMEs bumped to 1.2.0.

### Added

- `SKILL.md` — Claude Code skill manifest. Instructs the agent to read
  `prompts/TICKET-MASTER.${TM_LANG:-en}.md`, load `config/ticket-master.config.json`,
  and follow the workflow through to Position 0.

## [1.1.1] — 2026-06-14

### Changed

- Logo replaced with a refined version genuinely authored by agy (Gemini 3.5 Pro)
  via the Antigravity CLI (workspace granted with the `--add-dir` flag) — ticket
  with perforation and stub detail plus a masked routing hub branching to three
  nodes (amber accent). Works on light and dark backgrounds.

## [1.1.0] — 2026-06-14

### Added

- Bilingual agent prompts: `prompts/TICKET-MASTER.en.md` (English) and
  `prompts/TICKET-MASTER.de.md` (German) — fully equivalent in content.
- `TM_LANG` environment variable for prompt-language selection in all starters
  (`.sh`, `.bat`, `.ps1`); loads `prompts/TICKET-MASTER.${TM_LANG}.md` and falls
  back to English with a stderr warning if the requested file is missing.
- `default_language` field in `config/ticket-master.config.example.json`.
- Logo (`assets/logo.svg`, agy-designed) embedded at the top of both READMEs.
- i18n roadmap entry in `TODO.md`.

### Changed

- Renamed `prompts/TICKET-MASTER.md` → `prompts/TICKET-MASTER.en.md`.
- Smoke test now checks both prompt languages and an extended anonymisation
  pattern list.
- Version badges in both READMEs bumped to 1.1.0.

## [1.0.0] — 2026-06-14

### Initial Release

- Cross-platform starters: Unix shell (`.sh`), Windows CMD (`.bat`), PowerShell (`.ps1`)
- Provider support: Claude CLI, Codex CLI, agy (Gemini CLI)
- `TM_PROVIDER` and `TM_SKIP_PERMISSIONS` environment variables
- `prompts/TICKET-MASTER.md` — fully anonymised, provider-agnostic agent prompt
  - Lean Router principle and three-bucket context model
  - Companion Pattern for ticket series
  - Decision Ladder (feature/user-only/actionable/bulk)
  - Score formula: `(10 - CLARITY) + COMPLEXITY + CREATIVITY + CONTEXT + CRITICALITY`
  - Processing chain: Intake → GATE1 → Characterise → Score → Candidates (GATE2/3) → Delegate (GATE4 + fallback) → Position 0
  - CHECKPOINT ALPHA (async / project task / user handoff)
- `config/ticket-master.config.example.json` — all fields documented
- `tickets/` — lifecycle directories: `QUEUED/`, `PENDING/`, `SOLVED/`, `.USER/`
- `tickets/_templates/TICKET.txt` — structured ticket template
- `tickets/INTAKE-TRIAGE-LOG.txt` — one-line-per-ticket audit trail
- `tests/test_smoke.py` — structure, JSON validity, anonymisation checks
- English and German documentation (`README.md`, `README_de.md`)
