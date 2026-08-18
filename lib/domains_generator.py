r"""
domains_generator.py — Generates the ticket-master domains registry.

Phase 1 of the personal-assistant expansion (T-20260704-02): reads the boss-
agent SKILL.md frontmatter of a BACH installation (`orchestrates.experts`,
`description`) and cross-references each expert against a skill registry's
`components.json` (`provenance.origin: bach`, `provenance.origin_path`) to
mark it "portiert" (a standalone skill already exists) or "nicht-portiert"
(still only lives inside BACH).

Stage-2 fuzzy matching (T-20260704-02 follow-up): stage-1 provenance matching
is a strict, exact 1:1 link and misses experts that govern a whole SKILL
FAMILY rather than a single ported skill (e.g. a counseling-style expert
whose skill family has no per-component `provenance` link back to it, only a
shared registry `category`), and skills that were extracted as standalone
but never registered in the main skill registry. `fuzzy_match_skills()` adds
a second pass (keyword/category hints + token overlap, see
`KEYWORD_CATEGORY_HINTS`) that only runs when stage 1 found nothing, marking
the result `"status": "teilportiert"` / `"match": "fuzzy"` with a
`"matched_skills"` list (as opposed to `"portiert"` / `"match": "exact"` /
a single `"standalone_skill"`). `load_extra_skills()` optionally folds a
second skill directory (e.g. a Claude Code `~/.claude/skills/` tree) into
that fuzzy pass via `--extra-skills-dir`. `domains.json`'s per-expert
`experts[]` entries remain provenance/grouping metadata only — the
ticket-master prompt routes directly to the resolved skill(s), it does not
introduce experts as a separate routing hop.

Stage-0 domain-level matching (T-20260808-02): stages 1 and 2 both compare a
component against an EXPERT's own name/description, so neither can see a
skill that supersedes a whole BOSS AGENT instead of any one of its named
experts (e.g. a "buero" skill covering all four experts of the
"bueroassistent" boss, none of which is itself named or described as
"buero"). `match_domain_skill()` runs once per domain, after the per-expert
loop, using only whole-token equality against the domain's own `id`/`label`
(never free-text description, never `_compound_overlap()`'s substring
bridging — see that function's docstring for why both were previously
walked back). A hit is merged into every expert that does not already carry
it (never into one with `"status": "portiert"`) as `"match": "domain"`; a
boss with zero orchestrated experts at all (e.g. `versicherungen`) gets a
synthetic `"__domain__:<boss>"` pseudo-expert instead, since there is
otherwise nowhere to attach the match.

Third matching source: the ellmos module catalog (T-20260818, ticket
T-20260818-410274502). Some experts have since been extracted as standalone
MODULES rather than standalone skills (e.g. `foerderplaner`, `mediaproduction`
-> module `ai-media-editor`), invisible to the two skill-registry-only
sources above. `load_modules_catalog()` normalizes a `modules.catalog.json`
into the same component shape and feeds it into both an exact tier
(`match_standalone_module()` -- name-identity, since a module carries no
BACH provenance link to check) and the existing stage-2 fuzzy pool, plus a
module-only compound-bridge pass (`fuzzy_match_modules_compound()`) for
cases token overlap alone can't reach. A resolved module is referenced as
`"module:<module-id>"` in `standalone_skill`/`matched_skills`, reusing the
existing status/match vocabulary rather than introducing a new one. Enabled
via `--modules-catalog`; default is unset, in which case the generator
behaves exactly as it did before this source existed.

Skill-library provenance becomes the PRIMARY stage-1 source (T-20260818-
137943175, Nachanalyse 2026-08-18): `load_skill_library()` reads
`bach_origin`/`provenance.origin_path` directly from each skill's OWN
SKILL.md frontmatter under an ellmos skill library, instead of through the
(now schema-drifted, provenance-less) skill registry `components.json`.
Per expert, `match_standalone_skill()` is tried against this pool FIRST;
`load_bach_components()`'s registry pool is now a LEGACY FALLBACK, tried
only when the library found nothing (e.g. a system with only a registry
checked out, no full library) -- `load_modules_catalog()`'s module pool
stays the third source, unchanged, tried only after both skill tiers.
`match_tool_by_stem()` additionally covers experts' sibling boss-level
`dependencies.tools` entries (evidenced: `dossier-briefing`,
`location-suche` -- extractions of a TOOL file, not of a named expert) via
the same two-tier precedence, surfaced as synthetic `"__tool__:<stem>"`
pseudo-experts (mirrors `match_domain_skill()`'s `"__domain__:<boss>"`
convention). A `bach_origin: true` library skill that resolves to neither an
expert nor a tool anywhere (evidenced: `assist/wetter`, `assist/
tageszeitung` -- provably BACH-extracted, but not referenced by any boss's
`orchestrates.experts` or `dependencies.tools`, in body text or otherwise)
is not force-attached to an unrelated expert via fuzzy proximity; it is
reported under `source.skill_library_bach_origin_unattached` instead, so
the gap stays visible rather than silently dropped. Enabled via
`--skill-library-dir`; default is unset, in which case the generator
behaves exactly as it did before this source existed.

`load_skill_library_fuzzy()` (same-day follow-up) is the STAGE-2-ONLY
complement: every library skill NOT already covered by the exact pool
above -- chiefly `bach_origin: false` skills, mirroring the existing
`load_bach_components()`/`load_custom_components()` split on the same
registry file, generalized to this second source. Real-corpus finding
this closes: several experts (`haushaltsmanagement`, `gesundheitsverwalter`,
`health_import`, the domain-level `__domain__:versicherungen`) used to
fuzzy/domain-match against `~/.claude/skills/` entries that no longer
exist there; topically equivalent library skills DO exist under
`.TOPICS/.AI/.SKILLS/skills/assist/`, correctly excluded from stage-1
exact matching (their own frontmatter denies BACH lineage) but legitimate
stage-2 fuzzy/domain candidates -- never promoted past "teilportiert".

This script is a GENERATOR that runs once on the "origin system" (the machine
that has BACH installed). Its output, `config/domains.json`, is consumed at
ticket-master runtime and is itself BACH-free — no BACH path or BACH code is
read at runtime, only this generated file. If the BACH agents directory is
not available (e.g. a different system, or a fresh checkout), the generator
aborts cleanly without touching any existing `config/domains.json`.

User-neutral module: no hardcoded local paths. Both source directories are
CLI arguments (or environment variables); there is no default that assumes a
particular filesystem layout. `config/domains.json` is itself a generated,
site-specific artifact (like `config/ticket-master.config.json`) and is not
meant to be committed — see `config/domains.example.json` for the schema.

Zero-dep: stdlib only (argparse, json, re, pathlib). Not a general YAML
parser — `parse_frontmatter()` targets the specific frontmatter shape used by
BACH boss-agent SKILL.md files (scalar `key: value`, folded `key: >` block
scalars, and one level of nesting for `orchestrates:`).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

# Known boss-agent directory names -> (domain id, domain label). Four of the
# five personal-assistant domains have stable folder names; the fifth
# (insurance/finance) is discovered by pattern since its folder name varies
# across BACH installations (e.g. "versicherungen" vs. "versicherungs-agent").
BOSS_DIR_DEFAULTS: dict[str, tuple[str, str]] = {
    "persoenlicher-assistent": ("alltag", "Alltag & Termine"),
    "gesundheitsassistent": ("gesundheit", "Gesundheit"),
    "bueroassistent": ("buero", "Büro, Steuer & Förderung"),
    "production": ("content", "Content & Produktion"),
}
_VERSICHERUNG_PATTERN = re.compile(r"versicher", re.IGNORECASE)

# Stage-2 (fuzzy) matching, T-20260704-02 follow-up: generic role-suffix
# tokens stripped from an expert's own name before token-overlap matching, so
# e.g. "psycho-berater" contributes the meaningful token "psycho" rather than
# the near-universal "berater".
_GENERIC_EXPERT_NAME_TOKENS: set[str] = {
    "agent", "berater", "beraterin", "verwalter", "verwalterin", "planer",
    "planerin", "management", "manager", "assistent", "assistentin",
    "experte", "expertin",
}

# Stage-2 compound-word bridge (T-20260711-01): `_tokenize()` only splits on
# non-letter boundaries, so a German compound written as ONE word (e.g. the
# expert name "haushaltsmanagement") never breaks into {"haushalt",
# "management"} the way a hyphenated skill name ("haushalt-manager") does.
# Plain set-intersection token overlap then finds nothing even though the
# expert and skill clearly refer to the same thing. `_compound_overlap()`
# below bridges this with a length-guarded substring test instead of a real
# compound splitter (stdlib-only, no German morphology dependency available).
# Length threshold avoids short/generic fragments ("in", "der", "test")
# matching almost anything. 4 was tried first and empirically proved too low
# (T-20260711-04 regression, real data): "work" (4 chars) bridged the expert
# "worksheet_generator" to the unrelated therapy skill "genogram-work" purely
# because both contain the substring "work" -- a coincidental fragment, not a
# semantic match. 6 keeps every verified real compound case comfortably clear
# (haushalt=8, gesundheit=10, transkription=13) while excluding short/generic
# English fragments like "work", "team", "plan", "data", "file".
_MIN_COMPOUND_TOKEN_LEN = 6


def _compound_overlap(name_tokens: set[str], comp_tokens: set[str]) -> bool:
    """True if some sufficiently long component token is a substring of some
    expert-name token, or vice versa. Both token sets are expected to already
    have `_GENERIC_EXPERT_NAME_TOKENS` removed by the caller, so a purely
    generic fragment (e.g. "manager") can't bridge two otherwise-unrelated
    compounds on its own."""
    for nt in name_tokens:
        if len(nt) < _MIN_COMPOUND_TOKEN_LEN:
            continue
        for ct in comp_tokens:
            if len(ct) < _MIN_COMPOUND_TOKEN_LEN:
                continue
            if ct in nt or nt in ct:
                return True
    return False


# Optional keyword-stem hints for stage-2 fuzzy matching. Deliberately keyed
# off the EXPERT'S OWN NAME ONLY, never the boss-level description: that
# description is shared verbatim across every expert of the same boss, so
# matching against it would leak one expert's hits onto all of its siblings
# (empirically observed: a "psychological ... counseling" phrase in a shared
# boss description would otherwise also credit a purely medical/
# administrative sibling expert with the whole therapy skill family).
# Each hint maps a stem to (a) the registry `category` this expert's skill
# family likely lives under, and (b) a small set of related term-stems to
# substring-match against a component's own id/name/description — needed for
# sources like an extra skills dir that have no `category` concept at all.
# Generic domain vocabulary, not project-specific; extend for your own
# taxonomy, but keep entries narrow (a few related stems), not broad topic
# words, to avoid turning stage 2 into a noisy full-text search.
KEYWORD_CATEGORY_HINTS: dict[str, dict[str, object]] = {
    "psycho": {"category": "therapy", "terms": {"therap", "counsel", "psycho"}},
    "therap": {"category": "therapy", "terms": {"therap", "counsel"}},
    "berat": {"category": "therapy", "terms": {"therap", "counsel", "berat"}},
    "counsel": {"category": "therapy", "terms": {"therap", "counsel"}},
}


def _tokenize(text: str) -> set[str]:
    """Unicode-aware tokenizer. `[a-zA-Z0-9]+` would silently split German
    umlauts/ß out of a word (verified: "Fördermittelberater" ->
    {"f", "rdermittelberater"}), quietly losing token-overlap matches for
    non-ASCII expert/skill names. `[^\\W\\d_]+` matches Unicode letters
    (Python's `\\w` is Unicode-aware by default), `\\d+` matches digit runs,
    so "Fördermittelberater" stays one token and "gpt4" still splits into
    letters+digits like before."""
    return set(re.findall(r"[^\W\d_]+|\d+", text.lower()))


def _collect_indented(body: list[str], start: int) -> tuple[list[str], int]:
    """Collects consecutive non-blank, indented lines starting at `start`.
    Stops at the first blank line or first line without leading whitespace."""
    j = start
    collected: list[str] = []
    while j < len(body) and body[j].startswith((" ", "\t")) and body[j].strip():
        collected.append(body[j].strip())
        j += 1
    return collected, j


def _parse_bracket_list(value: str) -> list[str]:
    value = value.strip()
    if value.startswith("[") and value.endswith("]"):
        inner = value[1:-1].strip()
        return [item.strip() for item in inner.split(",") if item.strip()]
    return []


def parse_frontmatter(text: str) -> dict:
    """Targeted extractor for BACH boss-agent SKILL.md frontmatter (between
    the first pair of `---` lines). Returns a dict with whatever top-level
    keys were present; `orchestrates` (if present) is itself a dict whose
    `experts`/`services` values are parsed as lists."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    try:
        end = lines[1:].index("---") + 1
    except ValueError:
        return {}
    body = lines[1:end]

    result: dict = {}
    i = 0
    while i < len(body):
        raw = body[i]
        if not raw.strip() or raw.startswith((" ", "\t")):
            i += 1
            continue
        if ":" not in raw:
            i += 1
            continue
        key, _, rest = raw.partition(":")
        key = key.strip()
        rest = rest.strip()
        if rest == ">":
            collected, i = _collect_indented(body, i + 1)
            result[key] = " ".join(collected)
            continue
        if rest == "":
            collected, i = _collect_indented(body, i + 1)
            nested: dict = {}
            for sub in collected:
                if ":" in sub:
                    sub_key, _, sub_val = sub.partition(":")
                    nested[sub_key.strip()] = _parse_bracket_list(sub_val.strip())
            result[key] = nested
            continue
        result[key] = rest
        i += 1
    return result


def discover_boss_dirs(agents_dir: Path, extra_dirs: list[str] | None = None) -> dict[str, Path]:
    """Finds the boss-agent directories under `agents_dir`. Known directory
    names are used directly; the insurance/finance domain is additionally
    searched for by name/description pattern, since its folder name is not
    guaranteed across BACH installations."""
    found: dict[str, Path] = {}
    for dirname in list(BOSS_DIR_DEFAULTS) + list(extra_dirs or []):
        candidate = agents_dir / dirname
        if (candidate / "SKILL.md").is_file():
            found[dirname] = candidate

    if not any(_VERSICHERUNG_PATTERN.search(name) for name in found):
        for entry in sorted(agents_dir.iterdir()):
            if entry.name in found or not entry.is_dir():
                continue
            skill_file = entry / "SKILL.md"
            if not skill_file.is_file():
                continue
            if _VERSICHERUNG_PATTERN.search(entry.name):
                found[entry.name] = entry
                break
            try:
                text = skill_file.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if _VERSICHERUNG_PATTERN.search(text[:2000]):
                found[entry.name] = entry
                break
    return found


def _domain_id_label(dirname: str, frontmatter: dict) -> tuple[str, str]:
    if dirname in BOSS_DIR_DEFAULTS:
        return BOSS_DIR_DEFAULTS[dirname]
    if _VERSICHERUNG_PATTERN.search(dirname) or _VERSICHERUNG_PATTERN.search(str(frontmatter.get("name", ""))):
        return ("versicherung", "Versicherung & Finanzen")
    slug = re.sub(r"[^a-z0-9]+", "-", dirname.lower()).strip("-")
    return (slug or dirname, str(frontmatter.get("name", dirname)))


_USECASE_RE = re.compile(r"\(\d+\)\s*")


def extract_usecases(description: str) -> list[str]:
    """Splits a `(1) ... (2) ...`-style description into individual usecase
    strings. Falls back to the whole description as a single entry when the
    numbered-list pattern is not present."""
    description = description.strip()
    if not description:
        return []
    parts = _USECASE_RE.split(description)
    if len(parts) <= 1:
        return [description]
    return [p.strip().rstrip(",.") for p in parts[1:] if p.strip()]


def load_bach_components(registry_components_path: Path) -> list[dict]:
    """Loads a skill-registry `components.json` and returns only the entries
    with `provenance.origin == "bach"` (candidates for expert-to-skill
    matching). Used for BOTH stage 1 (exact provenance) and stage 2 (fuzzy).
    Stage 1's use of this bach-only scope is deliberate and documented
    (T-20260704-02 Phase 1, commit a8cbf1b): it answers "has this BACH
    expert already been extracted as its own standalone skill" -- a
    "custom"-origin skill was never a BACH expert, so it cannot answer that
    question and MUST stay out of stage 1 (T-20260711-06 decision). See
    `load_custom_components()` for the separate, stage-2-only pool."""
    data = json.loads(Path(registry_components_path).read_text(encoding="utf-8"))
    components = data.get("components", [])
    return [c for c in components if (c.get("provenance") or {}).get("origin") == "bach"]


def load_custom_components(registry_components_path: Path) -> list[dict]:
    """Loads registry entries with `provenance.origin == "custom"` (locally
    authored skills, never extracted from BACH) -- T-20260711-06. Unlike
    `load_bach_components()`, this pool is STAGE-2-ONLY (fuzzy matching):
    `match_standalone_skill()` (stage 1) is never called with it, deliberately
    -- stage 1's "was this ported from BACH" question does not apply to
    skills that never had a BACH origin in the first place. Verified before
    introducing this pool (T-20260711-06 intent check): every "custom"-origin
    component that reaches this pool is git-tracked and carries no privacy/
    maturity marker in its own frontmatter (unlike the separate, unrelated
    `.gitignore` privacy block covering skills like `foerderplaner` and
    `swarm-operations`, which never appear in the registry at all -- see
    T-20260711-03). `build_domains()` still deduplicates the result against
    `load_extra_skills()` entries by name before merging into the fuzzy pool,
    since the same real skill can otherwise appear twice under two different
    ids (a registry id and a `claude-skill:` id)."""
    data = json.loads(Path(registry_components_path).read_text(encoding="utf-8"))
    components = data.get("components", [])
    return [c for c in components if (c.get("provenance") or {}).get("origin") == "custom"]


def _parse_provenance_value(raw: str) -> dict:
    """Parses a skill SKILL.md's single-line `provenance: {...}` frontmatter
    value (T-20260818-137943175). Written by the extraction tooling as a
    Python dict repr (single-quoted strings, `None`/`True`/`False` literals),
    e.g. `{'origin': 'bach', 'origin_path': 'system/hub/_services/weather/
    weather_service.py', 'origin_version': '1.0.0', ...}` -- NOT valid YAML
    or JSON, so `parse_frontmatter()`'s top-level scalar branch only ever
    captures it as one raw string. `ast.literal_eval` is the correct,
    side-effect-free stdlib tool for exactly this shape (unlike `eval()`, it
    only ever produces literals/containers, never executes arbitrary code).
    Malformed/partial values must not abort a library-wide scan of ~400+
    files (observed: a skill's embedded `notes` string containing a literal
    unescaped newline) -- returns `{}` on any parse failure, the same
    best-effort contract every other loader in this module already has."""
    raw = raw.strip()
    if not raw.startswith("{"):
        return {}
    try:
        import ast
        value = ast.literal_eval(raw)
    except (ValueError, SyntaxError):
        return {}
    return value if isinstance(value, dict) else {}


def load_skill_library(skills_dir: Path) -> list[dict]:
    """PRIMARY stage-1 source (T-20260818-137943175 + T-20260818-410274502
    follow-up, "Nachanalyse" 2026-08-18): scans every `SKILL.md` under an
    ellmos skill library (`.TOPICS/.AI/.SKILLS/skills/<category>/<name>/
    SKILL.md`) directly, not through the skill registry. Root cause this
    heals: each skill's OWN frontmatter carries `bach_origin` (bool) and,
    when true, `provenance.origin_path` -- the registry `components.json`
    this generator previously relied on exclusively for stage 1
    (`load_bach_components()`) does not carry these fields at all (measured
    against the live 2026-08-18 registry: 0/136 entries have a `provenance`
    key -- it migrated to a "public-catalog-v1" schema without them). Every
    expert whose lineage now only lives in the library frontmatter silently
    regressed to stage-2 fuzzy or "nicht-portiert" once that drift happened.
    Reading the library directly is upstream of the drift and does not
    depend on the registry being in sync with it -- see `load_bach_components()`,
    which stays in place as the LEGACY FALLBACK for systems that only have a
    (possibly stale) registry checked out and no full library.

    Only `bach_origin: true` skills are returned (case-insensitive string
    compare -- like the rest of this module, `parse_frontmatter()` never
    bool-coerces, so the YAML scalar `true`/`false` survives as a raw
    string). A skill with `bach_origin: false` (e.g. `assist/transkription`,
    whose own frontmatter explicitly documents "kein direkter BACH-Origin
    ... neu konzipiert") is deliberately EXCLUDED here, even if some other
    signal (name, topic) makes it look related to a BACH expert -- a skill
    that denies its own BACH lineage must never be offered to stage-1
    exact matching as if it were a provenance-backed link. This is the
    concrete implementation of the Nachanalyse's methodology note: a
    frontmatter denial beats a topical/fuzzy resemblance, not the other way
    round. (It does not need to additionally suppress that skill from
    whatever fuzzy result it may already have via the pre-existing registry/
    extra-skills pools -- those pools and this one are disjoint by
    construction, since only `bach_origin: true` skills ever reach this
    pool.) A skill with `bach_origin: true` but no resolvable
    `provenance.origin_path` is also skipped -- useless for an exact-match
    source, and this loader's contract is stage-1 exact matches only, not a
    general-purpose fuzzy pool.

    `id` reuses the registry's own `skill:<category>:<name>` scheme exactly
    (verified against the live registry, T-20260818) so a resolved reference
    from this source is indistinguishable downstream from one the registry
    would have produced when intact -- no new id shape for `standalone_skill`/
    `matched_skills` consumers to special-case. `provenance.origin_path` is
    carried through unchanged so `match_standalone_skill()` (unmodified) can
    consume this pool exactly as it already consumes
    `load_bach_components()`'s. Missing/unreadable directory or file is
    skipped silently -- same best-effort contract as every other loader
    here."""
    skills_dir = Path(skills_dir)
    found: list[dict] = []
    if not skills_dir.is_dir():
        return found
    for category_dir in sorted(skills_dir.iterdir()):
        if not category_dir.is_dir():
            continue
        for skill_dir in sorted(category_dir.iterdir()):
            skill_file = skill_dir / "SKILL.md"
            if not skill_file.is_file():
                continue
            try:
                text = skill_file.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            frontmatter = parse_frontmatter(text)
            bach_origin_raw = str(frontmatter.get("bach_origin", "")).strip().lower()
            if bach_origin_raw != "true":
                continue
            provenance = _parse_provenance_value(str(frontmatter.get("provenance", "")))
            origin_path = str(provenance.get("origin_path") or "")
            if not origin_path:
                continue
            name = str(frontmatter.get("name", skill_dir.name))
            found.append({
                "id": f"skill:{category_dir.name}:{name}",
                "name": name,
                "description": str(frontmatter.get("description", "")),
                "category": category_dir.name,
                "provenance": {"origin_path": origin_path},
            })
    return found


def load_skill_library_fuzzy(skills_dir: Path) -> list[dict]:
    """STAGE-2-ONLY complement of `load_skill_library()` (follow-up to
    T-20260818-137943175, real-corpus finding 2026-08-18): mirrors exactly
    the `load_bach_components()`/`load_custom_components()` split on the
    SAME registry file -- `load_skill_library()` already answers "was this
    library skill a confirmed BACH extraction" (stage-1 exact, bach_origin:
    true + resolvable origin_path only); this function answers the
    different question "does some OTHER library skill happen to cover the
    same ground topically" (stage-2 fuzzy only), returning every skill NOT
    already covered by that pool -- `bach_origin: false` skills (deliberate
    non-BACH-origin, e.g. `assist/haushalt-manager`, `assist/gesundheit`,
    `assist/finanz-versicherung` -- all explicitly `provenance.origin:
    public-neutral`, a 2026-07-30 rewrite generation, not a BACH port) AND
    `bach_origin: true` skills without a resolvable `origin_path`.

    Concrete real-corpus case this closes: `haushaltsmanagement`,
    `gesundheitsverwalter`, `health_import` and the domain-level
    `__domain__:versicherungen` used to fuzzy/domain-match against
    `~/.claude/skills/` entries (`claude-skill:haushalt-manager` etc.) that
    no longer exist there -- verified equivalent-named library skills DO
    exist under `.TOPICS/.AI/.SKILLS/skills/assist/`, just correctly
    excluded from stage-1 exact matching (their own frontmatter denies
    BACH lineage). Never promoting a `bach_origin: false` skill past
    "teilportiert"/fuzzy is the whole point -- exactly the same guarantee
    `load_custom_components()` already gives the registry-origin ("custom")
    pool, generalized to this second source. Same id scheme
    (`skill:<category>:<name>`) as `load_skill_library()`, so a skill that
    happens to satisfy BOTH functions' criteria (impossible under the
    current mutually-exclusive filter, but kept id-consistent regardless)
    would dedupe cleanly rather than appear as two different-looking
    entries. Missing/unreadable directory or file is skipped silently --
    same best-effort contract as every other loader here."""
    skills_dir = Path(skills_dir)
    found: list[dict] = []
    if not skills_dir.is_dir():
        return found
    for category_dir in sorted(skills_dir.iterdir()):
        if not category_dir.is_dir():
            continue
        for skill_dir in sorted(category_dir.iterdir()):
            skill_file = skill_dir / "SKILL.md"
            if not skill_file.is_file():
                continue
            try:
                text = skill_file.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            frontmatter = parse_frontmatter(text)
            bach_origin_raw = str(frontmatter.get("bach_origin", "")).strip().lower()
            if bach_origin_raw == "true":
                provenance = _parse_provenance_value(str(frontmatter.get("provenance", "")))
                if str(provenance.get("origin_path") or ""):
                    continue  # already covered by load_skill_library()'s exact pool
            name = str(frontmatter.get("name", skill_dir.name))
            found.append({
                "id": f"skill:{category_dir.name}:{name}",
                "name": name,
                "description": str(frontmatter.get("description", "")),
                "category": category_dir.name,
            })
    return found


def _expert_name_variants(name: str) -> set[str]:
    base = name.strip().lower().replace("_", "-")
    variants = {base}
    if base.endswith("-agent"):
        variants.add(base[: -len("-agent")])
    else:
        variants.add(f"{base}-agent")
    return variants


def match_standalone_skill(expert_name: str, bach_components: list[dict]) -> dict | None:
    """Finds a standalone skill in `bach_components` whose `origin_path`
    references this expert (by directory segment or filename stem). Tries
    both `<name>` and `<name>-agent` variants, since BACH expert folder names
    and their `orchestrates.experts` entries do not always match exactly
    (e.g. folder `steuer` vs. frontmatter entry `steuer-agent`)."""
    variants = _expert_name_variants(expert_name)
    for comp in bach_components:
        origin_path = str((comp.get("provenance") or {}).get("origin_path") or "")
        origin_path = origin_path.lower().replace("\\", "/")
        if not origin_path:
            continue
        segments = origin_path.split("/")
        stem = Path(origin_path).stem
        if any(v in segments for v in variants) or stem in variants:
            return comp
    return None


def match_tool_by_stem(tool_filename: str, components: list[dict]) -> dict | None:
    """Boss-level `dependencies.tools` match (T-20260818-137943175
    Nachanalyse Teil 3): a handful of skills (evidenced: `dossier-briefing`,
    `location-suche`) are lineage-backed extractions of a boss agent's own
    TOOL file, not of any of its named `orchestrates.experts` -- there is no
    per-expert entry to attach them to. `persoenlicher-assistent`'s
    `dependencies.tools: [dossier_generator.py, location_search.py,
    route_planner.py]` sits ALONGSIDE, not inside, its four orchestrated
    experts (`haushaltsmanagement`, `decision-briefing`, `literaturverwalter`,
    `transkriptions-service`) -- `build_domains()`'s existing per-expert loop
    has no entry point for a tool at all before this function.

    Deliberately a SEPARATE, simpler function rather than reusing
    `match_standalone_skill()`'s expert-name-variant machinery: that would
    silently fail here. `_expert_name_variants()` folds underscores to
    hyphens (built for hyphenated BACH expert-folder names), so passing a
    raw Python filename stem like "dossier_generator" through it produces
    variants `{"dossier-generator", "dossier-generator-agent"}` that can
    never equal an origin_path's own un-folded stem "dossier_generator" --
    verified empirically against the live corpus before choosing this
    separate function instead of extending that one.

    Matches on EXACT stem equality only (case-insensitive, both sides via
    `Path(...).stem`, no fuzzy/substring fallback) -- deliberately the
    strictest tier available, since a tool filename is unambiguous where it
    matches at all. Verified against the three real tool filenames of
    `persoenlicher-assistent`: `dossier_generator` <-> a skill whose
    `provenance.origin_path` stem is `dossier_generator` (match);
    `location_search` <-> a skill whose origin_path stem is
    `location_search` (match); `route_planner` <-> no skill with that exact
    stem exists in the library (correctly NO match -- deliberately not the
    unrelated `reiseroute` skill, which would require substring/fuzzy
    bridging this function does not attempt)."""
    tool_stem = Path(tool_filename).stem.strip().lower()
    if not tool_stem:
        return None
    for comp in components:
        origin_path = str((comp.get("provenance") or {}).get("origin_path") or "")
        if not origin_path:
            continue
        comp_stem = Path(origin_path.replace("\\", "/")).stem.strip().lower()
        if comp_stem == tool_stem:
            return comp
    return None


def fuzzy_match_skills(expert_name: str, boss_description: str, components: list[dict]) -> list[dict]:
    """Stage-2 (fuzzy) matching, T-20260704-02 follow-up: an expert governs a
    whole SKILL FAMILY, not necessarily a single 1:1 standalone skill (e.g.
    "psycho-berater" governs an entire "therapy" category of skills). Called
    only when stage-1 exact provenance matching (`match_standalone_skill`)
    finds nothing. `boss_description` is accepted for signature stability
    and possible future per-expert context, but is deliberately NOT used as a
    matching signal here — see the note on `KEYWORD_CATEGORY_HINTS` above for
    why matching against the (boss-shared) description leaks matches across
    sibling experts. Matches a component if either:
      (a) a `KEYWORD_CATEGORY_HINTS` stem is present in the expert's OWN name,
          and the component's `category` equals that stem's hinted category
          (works even when the component has no descriptive text, which is
          common in this registry); or
      (b) same hint, but the component has no `category` (e.g. an
          `load_extra_skills()` entry) — matched instead via a substring hit
          from the hint's `terms` against the component's own id/name/
          description; or
      (c) the expert's name tokens (role-suffix stripped) exactly overlap
          with the component's own id/name tokens (role-suffix stripped);
          or
      (d) a component's own id/name token (role-suffix stripped, length-
          guarded) is a substring of an expert-name token or vice versa —
          bridges German compounds written as one word on the expert side
          against a hyphenated/split skill name (T-20260711-01, see
          `_compound_overlap()`).
    Cases (c) and (d) are deliberately scoped to the component's id/name
    only, NOT its free-text description (T-20260711-05 -- case (c) used to
    run over the full id+name+description haystack, which produced false
    positives whenever an expert's own name happened to be a common English
    word appearing incidentally somewhere in an unrelated component's prose,
    e.g. expert "report_generator" against a component whose description
    merely mentions a "Bug-Report-Template". Empirically verified against
    the real BACH+skill-registry corpus (T-20260711-04/-05 diagnostics):
    no currently-legitimate match relies on a description-only token in
    case (c) -- every real hit already goes through id/name, a category
    hint (a), or a `hinted_terms` substring (b), all of which still
    intentionally read the description). Case (b) is a narrow exception
    that KEEPS reading the description on purpose (see
    `test_token_overlap_on_shared_description_word`): it is gated by a
    `KEYWORD_CATEGORY_HINTS` stem match on the expert's own name first, so
    it cannot fire on an arbitrary shared word the way unguarded case (c)
    could.
    `components` may mix registry entries (with `category`) and entries from
    `load_extra_skills()` (no `category` — matched via (b)/(c)/(d) only).
    Returns every match, since an expert can legitimately govern several
    skills. Deliberately conservative: on a real corpus of 100+ candidate
    skills, a broader token-overlap-on-shared-description heuristic was found
    to match almost anything (verified empirically) — precision over recall
    here."""
    name_tokens = _tokenize(expert_name) - _GENERIC_EXPERT_NAME_TOKENS
    expert_name_lower = expert_name.lower()

    hinted_categories: set[str] = set()
    hinted_terms: set[str] = set()
    for stem, hint in KEYWORD_CATEGORY_HINTS.items():
        if stem in expert_name_lower:
            hinted_categories.add(str(hint["category"]))
            hinted_terms.update(hint["terms"])  # type: ignore[arg-type]

    matches: list[dict] = []
    seen_ids: set[str] = set()
    for comp in components:
        comp_id = comp.get("id")
        if not comp_id or comp_id in seen_ids:
            continue
        category = str(comp.get("category") or "").strip().lower()
        haystack = " ".join([
            str(comp.get("id", "")), str(comp.get("name", "")), str(comp.get("description", "")),
        ]).lower()

        if category and category in hinted_categories:
            matches.append(comp)
            seen_ids.add(comp_id)
            continue
        if not category and hinted_terms and any(term in haystack for term in hinted_terms):
            matches.append(comp)
            seen_ids.add(comp_id)
            continue
        id_name_tokens = _tokenize(
            " ".join([str(comp.get("id", "")), str(comp.get("name", ""))])
        ) - _GENERIC_EXPERT_NAME_TOKENS
        if name_tokens and (name_tokens & id_name_tokens):
            matches.append(comp)
            seen_ids.add(comp_id)
            continue
        if name_tokens and id_name_tokens and _compound_overlap(name_tokens, id_name_tokens):
            matches.append(comp)
            seen_ids.add(comp_id)
    return matches


def match_domain_skill(domain_id: str, domain_label: str, components: list[dict]) -> list[dict]:
    """Stage-0 (domain-level) matching, T-20260808-02: covers the case where a
    standalone skill supersedes an ENTIRE boss agent rather than one of its
    named sub-experts (e.g. a "buero" skill covering all four experts of the
    "bueroassistent" boss -- `steuer-agent`, `foerderplaner`,
    `report_generator`, `worksheet_generator` -- none of which is itself
    named or described as "buero"). Stage 1 (`match_standalone_skill`) and
    stage 2 (`fuzzy_match_skills`) both compare a component against an
    EXPERT's own name/description, so they structurally cannot see this case
    no matter how complete the registry provenance is -- verified empirically
    against both the public-catalog and skill-v1 registries (T-20260808-02).

    Deliberately narrower than `fuzzy_match_skills()`: two rules only, both
    scoped to the component's own `id`/`name` (never free-text description --
    see T-20260711-05 on why description-scoped token overlap produces false
    positives), and NEITHER rule uses `_compound_overlap()`'s substring
    bridging (see T-20260711-04: a length-4 substring bridged the unrelated
    pair "worksheet_generator"/"genogram-work"). A domain id/label is a much
    shorter, more generic-looking string than a full expert name, so this
    stage restricts itself to whole-token equality, the strictest available
    signal:
      (a) exact equality: a component's own `name` equals the domain `id`
          (case-insensitive) -- e.g. skill name "buero" == domain id "buero".
      (b) whole-token overlap: a token of `domain_id`/`domain_label` (role-
          suffix stripped via `_GENERIC_EXPERT_NAME_TOKENS`) exactly equals a
          token of the component's own id/name (same stripping) -- e.g.
          domain label "Versicherung & Finanzen" contributes the token
          "versicherung", which equals a token of skill name
          "finanz-versicherung". No length threshold is applied because this
          is whole-token equality, not a substring test -- empirically
          verified against the live 2026-08-08 corpus (122 components) to
          produce zero incidental hits for the three domains without a
          whole-domain skill (`alltag`, `content`, `versicherung`'s sibling
          `gesundheit` produced one additional, correct hit -- see caller).
    Returns every match (a domain can, in principle, be covered by more than
    one skill); empty list if none. Callers are responsible for excluding
    IDs already claimed by stage-1 exact matches (pass the same
    `fuzzy_pool_available` used for stage 2) so a component is never both
    a per-expert exact match and a domain-level match at once.
    """
    domain_tokens = _tokenize(f"{domain_id} {domain_label}") - _GENERIC_EXPERT_NAME_TOKENS
    domain_id_lower = domain_id.strip().lower()

    matches: list[dict] = []
    seen_ids: set[str] = set()
    for comp in components:
        comp_id = comp.get("id")
        if not comp_id or comp_id in seen_ids:
            continue
        comp_name = str(comp.get("name", "")).strip().lower()
        if comp_name and comp_name == domain_id_lower:
            matches.append(comp)
            seen_ids.add(comp_id)
            continue
        id_name_tokens = _tokenize(
            " ".join([str(comp.get("id", "")), str(comp.get("name", ""))])
        ) - _GENERIC_EXPERT_NAME_TOKENS
        if domain_tokens and id_name_tokens and (domain_tokens & id_name_tokens):
            matches.append(comp)
            seen_ids.add(comp_id)
    return matches


def load_extra_skills(extra_skills_dir: Path) -> list[dict]:
    """Loads a second, independent skill inventory (e.g. a Claude Code
    `~/.claude/skills/` tree) for stage-2 fuzzy matching — useful when a
    skill has been extracted as standalone but was never (or not yet)
    registered in the main skill registry (observed empirically: skills like
    a job-application helper or a self-management skill existed locally but
    were absent from `components.json`). Reads each
    `<extra_skills_dir>/<name>/SKILL.md` frontmatter (`name`, `description`)
    via `parse_frontmatter()`. These entries never carry a `category`, so in
    `fuzzy_match_skills()` they can only match via token overlap, never via
    `KEYWORD_CATEGORY_HINTS`. Missing directory / unreadable files are
    skipped silently — this is a best-effort secondary source, not a
    required one."""
    extra_skills_dir = Path(extra_skills_dir)
    found: list[dict] = []
    if not extra_skills_dir.is_dir():
        return found
    for entry in sorted(extra_skills_dir.iterdir()):
        if not entry.is_dir():
            continue
        skill_file = entry / "SKILL.md"
        if not skill_file.is_file():
            continue
        try:
            text = skill_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        frontmatter = parse_frontmatter(text)
        found.append({
            "id": f"claude-skill:{entry.name}",
            "name": str(frontmatter.get("name", entry.name)),
            "description": str(frontmatter.get("description", "")),
            "category": None,
        })
    return found


def load_modules_catalog(modules_catalog_path: Path) -> list[dict]:
    """Third matching source (T-20260818, ticket T-20260818-410274502): the
    ellmos module catalog (`.MODULES/modules.catalog.json`, ~58 entries at
    the time this was written). Some BACH experts have since been extracted
    as standalone MODULES rather than standalone SKILLS (evidenced:
    `foerderplaner` -> module `foerderplaner`, `report_generator` -> module
    `report-forge`, `mediaproduction` -> module `ai-media-editor`), which
    `load_bach_components()`/`load_custom_components()` cannot see -- those
    only read a skill registry's `components.json`. Unlike the skill
    registry, a module manifest carries no BACH `provenance.origin_path`
    back-link, so there is no stage-1-equivalent provenance cross-reference
    available here; `match_standalone_module()` below establishes an exact
    tier a different way (name-identity, not provenance).

    Each module is normalized into the same id/name/description/category
    shape the skill pools already use, so it can be dropped straight into
    `fuzzy_match_skills()`/`match_domain_skill()` unchanged. `id` is prefixed
    `module:<module-id>` (mirrors `load_extra_skills()`'s `claude-skill:`
    prefix exactly -- same rationale: keeps a resolved reference
    self-describing and collision-free against skill ids, at the cost of
    injecting the token "module" into that entry's own tokenization, which
    `load_extra_skills()` already accepts for "claude"/"skill"). `category`
    is deliberately left `None`: the module catalog's own `category` field
    (`memory`/`control`/`domains`/...) is a taxonomy unrelated to
    `KEYWORD_CATEGORY_HINTS`'s skill-registry categories (`therapy`/...);
    reusing it would risk an accidental cross-taxonomy hit. Missing/
    unreadable file is skipped silently -- best-effort secondary source,
    same contract as `load_extra_skills()`."""
    modules_catalog_path = Path(modules_catalog_path)
    if not modules_catalog_path.is_file():
        return []
    try:
        data = json.loads(modules_catalog_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    found: list[dict] = []
    for module in data.get("modules", []):
        module_id = module.get("id")
        if not module_id:
            continue
        found.append({
            "id": f"module:{module_id}",
            "name": str(module.get("display_name") or module_id),
            "description": str(module.get("description", "")),
            "category": None,
        })
    return found


def match_standalone_module(expert_name: str, modules: list[dict]) -> dict | None:
    """Exact-tier module match (T-20260818-410274502) -- the module-catalog
    analogue of `match_standalone_skill()`, but since a module carries no
    `provenance.origin_path` to check, this instead requires the RAW,
    UN-stripped token set of the expert's own name to be exactly equal to
    the RAW token set of the module's bare id (hyphen/underscore-insensitive,
    since `_tokenize()` splits on both as non-letter boundaries -- same
    effect as `_expert_name_variants()`'s hyphen/underscore folding for
    skills, generalized to full-name equality instead of a single "-agent"
    suffix).

    Deliberately full-SET equality of the RAW tokens, NOT overlap and NOT
    `_GENERIC_EXPERT_NAME_TOKENS`-stripped: overlap alone would wrongly
    promote `report_generator`/`report-forge` (share only the token
    "report", second token differs: "generator" vs "forge" -- a real match,
    but not a NAME-identity one, so it must fall through to stage-2 fuzzy
    token-overlap instead, which already finds it once modules are in the
    fuzzy pool). Generic-stripping first would be worse, not just
    unnecessary: `steuer-agent` minus `_GENERIC_EXPERT_NAME_TOKENS` is just
    `{"steuer"}`, and so is `steuer-assistent` minus generics (`"assistent"`
    is itself a generic token) -- stripped-set equality would wrongly call
    that an exact 1:1 identity match and silently drop the second, equally
    valid candidate `steuer-suite` that stage-2 fuzzy would otherwise still
    surface for the same expert (stage 1 hits skip stage 2 entirely, see
    `build_domains()`). Verified empirically against the real 2026-08-18
    corpus (14 orchestrated experts x 58 catalogued modules): raw full-set
    equality fires for exactly two pairs, both genuine identity matches
    (`foerderplaner`<->module `foerderplaner`; `worksheet_generator`<->
    module `worksheet-generator`), zero false positives, and correctly
    leaves `steuer-agent` for stage-2 fuzzy to find both `steuer-assistent`
    and `steuer-suite`."""
    expert_tokens = _tokenize(expert_name)
    if not expert_tokens:
        return None
    for module in modules:
        module_id = module["id"].split(":", 1)[-1]
        module_tokens = _tokenize(module_id)
        if module_tokens and module_tokens == expert_tokens:
            return module
    return None


# Module-pool-only compound bridge (T-20260818-410274502). A SEPARATE
# threshold from `_MIN_COMPOUND_TOKEN_LEN`/`_compound_overlap()` (which stay
# untouched, still governing skill-pool matching only) -- lowering the
# shared skill threshold below 6 would resurrect the exact false positive it
# was raised to fix (T-20260711-04: "work", 4 chars, bridging
# "worksheet_generator" to the unrelated skill "genogram-work"). The module
# catalog is a much smaller (~58 entries), curated, human-authored pool with
# a materially lower noise floor, so a lower threshold there does not carry
# the same risk -- but it still needs its own empirical check, not an
# assumption. Verified against the real 2026-08-18 corpus (14 experts x 58
# modules) at threshold 5: exactly three pairs bridge, two of them the
# targeted ticket cases (`mediaproduction`<->module `ai-media-editor`,
# `mediaproduction`<->module `media-editor-core` -- both genuinely
# media-production-related, a legitimate double match, not noise) and one
# plausible bonus (`transkriptions-service`<->module `doc-services`, bridged
# on "service"/"services", a singular/plural variant of the same word). Zero
# incidental noise at threshold 5 on the real corpus.
_MIN_MODULE_COMPOUND_TOKEN_LEN = 5


def _module_compound_overlap(expert_tokens: set[str], module_tokens: set[str]) -> bool:
    """Module-pool compound bridge, see `_MIN_MODULE_COMPOUND_TOKEN_LEN`
    above for the threshold rationale. Same substring-either-way shape as
    `_compound_overlap()`, intentionally NOT shared code with it: the two
    thresholds must be free to diverge without one accidentally dragging the
    other along in a future edit."""
    for et in expert_tokens:
        if len(et) < _MIN_MODULE_COMPOUND_TOKEN_LEN:
            continue
        for mt in module_tokens:
            if len(mt) < _MIN_MODULE_COMPOUND_TOKEN_LEN:
                continue
            if mt in et or et in mt:
                return True
    return False


def fuzzy_match_modules_compound(expert_name: str, modules: list[dict]) -> list[dict]:
    """Module-pool-only compound-bridge pass (T-20260818-410274502), run IN
    ADDITION to `fuzzy_match_skills()` -- that function already covers
    modules for ordinary whole-token overlap once they are in the shared
    fuzzy pool (e.g. `report_generator`<->module `report-forge` via the
    shared token "report"), but it only ever calls `_compound_overlap()` at
    the shared skill threshold of 6, which excludes the 5-character token
    "media" the ticket's targeted `mediaproduction`<->`ai-media-editor` case
    needs. This function closes exactly that gap using the module-only
    threshold (`_MIN_MODULE_COMPOUND_TOKEN_LEN`) without touching the shared
    skill one. Caller merges the result into whatever `fuzzy_match_skills()`
    already found for the same expert (see `build_domains()`)."""
    expert_tokens = _tokenize(expert_name) - _GENERIC_EXPERT_NAME_TOKENS
    if not expert_tokens:
        return []
    matches: list[dict] = []
    seen_ids: set[str] = set()
    for module in modules:
        module_id_bare = module["id"].split(":", 1)[-1]
        module_tokens = _tokenize(module_id_bare) - _GENERIC_EXPERT_NAME_TOKENS
        if not module_tokens or module["id"] in seen_ids:
            continue
        if _module_compound_overlap(expert_tokens, module_tokens):
            matches.append(module)
            seen_ids.add(module["id"])
    return matches


def build_domains(agents_dir: Path, registry_components_path: Path | None,
                   extra_boss_dirs: list[str] | None = None,
                   extra_skills_dir: Path | None = None,
                   modules_catalog_path: Path | None = None,
                   skill_library_dir: Path | None = None) -> dict:
    agents_dir = Path(agents_dir)
    if not agents_dir.is_dir():
        raise FileNotFoundError(f"BACH agents dir not found: {agents_dir}")

    bach_components: list[dict] = []
    custom_components: list[dict] = []
    if registry_components_path is not None and Path(registry_components_path).is_file():
        bach_components = load_bach_components(Path(registry_components_path))
        custom_components = load_custom_components(Path(registry_components_path))

    skill_library_components: list[dict] = []
    skill_library_fuzzy_components: list[dict] = []
    if skill_library_dir is not None:
        skill_library_components = load_skill_library(Path(skill_library_dir))
        skill_library_fuzzy_components = load_skill_library_fuzzy(Path(skill_library_dir))

    extra_skills: list[dict] = []
    if extra_skills_dir is not None:
        extra_skills = load_extra_skills(Path(extra_skills_dir))

    modules_components: list[dict] = []
    if modules_catalog_path is not None:
        modules_components = load_modules_catalog(Path(modules_catalog_path))

    # Dedup BEFORE merging (T-20260711-06, team-lead condition: solve dedup
    # first or don't expand the pool at all). A "custom"-origin registry
    # component can be the SAME real skill as an `extra_skills_dir` entry,
    # just mirrored to a second location (e.g. a registry skill also
    # deployed to a Claude Code `~/.claude/skills/` tree) -- under a
    # DIFFERENT id in each source (a registry id like "skill:dev:rotation-
    # check" vs. `load_extra_skills()`'s "claude-skill:rotation-check").
    # `seen_ids` in `fuzzy_match_skills()` dedupes by id, so two different
    # ids for the same real skill would both survive and produce a doubled
    # `matched_skills` entry for one expert. Dedup by the skill's own
    # declared `name` (case-insensitive) instead of id, since `name` is the
    # semantic identity that stays consistent across both sources while the
    # id's source-specific prefix does not. If a custom-origin skill's name
    # is already present via `extra_skills`, the extra_skills copy wins and
    # the registry duplicate is dropped before it ever reaches the pool.
    extra_skill_names = {str(e.get("name", "")).strip().lower() for e in extra_skills}
    custom_components = [
        c for c in custom_components
        if str(c.get("name", "")).strip().lower() not in extra_skill_names
    ]

    # Same dedup, extended to the skill-library fuzzy pool (T-20260818-
    # 137943175 follow-up): a bach_origin:false library skill can be the
    # SAME real skill as an extra_skills_dir entry under a different id
    # (e.g. a skill mirrored both to the library and to a Claude Code
    # skills tree) -- the extra_skills copy wins, same precedent as the
    # custom_components dedup immediately above. `load_skill_library()`'s
    # own bach_origin:true+origin_path stage-1 pool is structurally
    # disjoint already (the loader itself excludes anything that pool
    # would claim), so no separate dedup against it is needed here.
    skill_library_fuzzy_components = [
        c for c in skill_library_fuzzy_components
        if str(c.get("name", "")).strip().lower() not in extra_skill_names
    ]

    # Same dedup, extended to the module pool (T-20260818-410274502): if a
    # capability was already registered as a STAGE-1-CAPABLE skill (bach
    # origin, skill-library exact, or an extra_skills_dir entry) under the
    # same declared name, the skill copy wins and the module duplicate is
    # dropped -- an established skill match should not be silently
    # displaced by a same-named module entry. Deliberately does NOT include
    # `custom_components` or `skill_library_fuzzy_components` here (both
    # fuzzy-only, non-lineage pools) -- verified regression (T-20260818-
    # 137943175 follow-up, real corpus): `.SKILLS/skills/education/
    # foerderplaner` (bach_origin:false, ~50-entry fuzzy pool) shares its
    # declared name with the `foerderplaner` MODULE this generator already
    # correctly resolves as an exact stage-1 match (T-20260818-410274502) --
    # including the fuzzy-only skill pools here silently dropped that
    # module from `modules_components` entirely, regressing a
    # previously-fixed case. A fuzzy-only skill is a weaker signal than an
    # exact module-name identity, so it must never suppress the module's
    # own stage-1 eligibility; the original, narrower dedup (T-20260711-06)
    # only ever needed to protect CONFIRMED-lineage/registered pools, not a
    # broad "anything topically similar exists somewhere" pool.
    skill_names = extra_skill_names | {
        str(c.get("name", "")).strip().lower()
        for c in bach_components + custom_components + skill_library_components
    }
    modules_components = [
        m for m in modules_components
        if str(m.get("name", "")).strip().lower() not in skill_names
    ]

    # Stage 2 (fuzzy) only: `custom_components` is deliberately NOT added to
    # `bach_components` and never passed to `match_standalone_skill()` (see
    # `load_custom_components()` docstring) -- it only feeds the fuzzy pool.
    # `skill_library_fuzzy_components` is the same shape of exclusion,
    # generalized to the skill-library source (T-20260818-137943175
    # follow-up -- see `load_skill_library_fuzzy()` docstring). `modules_
    # components` DOES also feed stage 1 (via `match_standalone_module()`,
    # called separately below, T-20260818-410274502) since a module --
    # unlike a "custom"-origin skill -- can be a genuine 1:1 name identity
    # for an expert (see that function's docstring).
    fuzzy_pool = (
        bach_components + custom_components + extra_skills
        + modules_components + skill_library_fuzzy_components
    )

    boss_dirs = discover_boss_dirs(agents_dir, extra_boss_dirs)

    # Read every boss's frontmatter once, up front, so the exact-match
    # exclusion below can be computed GLOBALLY across all bosses/experts
    # before any fuzzy matching happens -- not just within one boss.
    boss_data: list[tuple[str, str, str, str, list[str], list[str], list[str]]] = []
    for dirname, path in sorted(boss_dirs.items()):
        skill_file = path / "SKILL.md"
        text = skill_file.read_text(encoding="utf-8", errors="replace")
        frontmatter = parse_frontmatter(text)
        domain_id, label = _domain_id_label(dirname, frontmatter)
        description = str(frontmatter.get("description", ""))
        orchestrates = frontmatter.get("orchestrates", {})
        expert_names = orchestrates.get("experts", []) if isinstance(orchestrates, dict) else []
        services = orchestrates.get("services", []) if isinstance(orchestrates, dict) else []
        # T-20260818-137943175: boss-level tools, matched separately via
        # `match_tool_by_stem()` below -- see that function's docstring for
        # why these sit alongside `orchestrates.experts`, not inside it.
        dependencies = frontmatter.get("dependencies", {})
        tool_files = dependencies.get("tools", []) if isinstance(dependencies, dict) else []
        boss_data.append((dirname, domain_id, label, description, expert_names, services, tool_files))

    # Stage 1 (exact) runs for EVERY expert of EVERY boss first. The
    # resulting matched skill IDs are excluded from the stage-2 fuzzy pool
    # GLOBALLY (across all bosses, not just siblings within the same boss) --
    # otherwise a component could end up "portiert" for one expert here and,
    # via a coincidental keyword/token overlap, "teilportiert" for an
    # unrelated expert in a completely different domain.
    global_exact_matches: dict[tuple[str, str], dict] = {}
    for dirname, _domain_id, _label, _description, expert_names, _services, tool_files in boss_data:
        for expert_name in expert_names:
            # T-20260818-137943175: skill-library frontmatter provenance is
            # now the PRIMARY stage-1 source; the registry pool is a LEGACY
            # FALLBACK, tried only when the library found nothing (see
            # `load_skill_library()` docstring for why the registry alone
            # can no longer be relied on). Module-catalog identity stays the
            # third, unchanged tier.
            match = match_standalone_skill(expert_name, skill_library_components) if skill_library_components else None
            if match is None and bach_components:
                match = match_standalone_skill(expert_name, bach_components)
            if match is None and modules_components:
                # T-20260818-410274502: module-catalog exact tier, tried
                # only when neither skill tier found anything -- a skill
                # provenance link is a stronger signal (an actual BACH
                # extraction record) than a bare name-identity match, so it
                # takes precedence whenever either exists.
                match = match_standalone_module(expert_name, modules_components)
            if match:
                global_exact_matches[(dirname, expert_name)] = match
        for tool_file in tool_files:
            # T-20260818-137943175: boss-level tool match, same two-tier
            # precedence (library primary, registry fallback), synthetic
            # `__tool__:<stem>` key -- see `match_tool_by_stem()` docstring.
            tool_match = match_tool_by_stem(tool_file, skill_library_components) if skill_library_components else None
            if tool_match is None and bach_components:
                tool_match = match_tool_by_stem(tool_file, bach_components)
            if tool_match:
                tool_key = f"__tool__:{Path(tool_file).stem}"
                global_exact_matches[(dirname, tool_key)] = tool_match
    global_exact_matched_ids = {m["id"] for m in global_exact_matches.values()}
    fuzzy_pool_available = [c for c in fuzzy_pool if c.get("id") not in global_exact_matched_ids]
    # Module-only subset of the same exact-excluded pool, for the module
    # compound-bridge pass below (T-20260818-410274502) -- reuses the exact-
    # match exclusion `fuzzy_pool_available` already computed rather than
    # filtering `modules_components` a second, independent way.
    modules_pool_available = [c for c in fuzzy_pool_available if c["id"].startswith("module:")]

    domains = []
    for dirname, domain_id, label, description, expert_names, services, tool_files in boss_data:
        experts = []
        for expert_name in expert_names:
            match = global_exact_matches.get((dirname, expert_name))
            if match:
                experts.append({
                    "name": expert_name,
                    "standalone_skill": match["id"],
                    "status": "portiert",
                    "match": "exact",
                    "matched_skills": [match["id"]],
                })
                continue

            # Stage 2 (T-20260704-02 follow-up): keyword/category fuzzy
            # matching against the registry and/or an extra skills dir. An
            # expert governs a skill FAMILY, so this can yield several
            # matches, not just one.
            fuzzy_matches = fuzzy_match_skills(expert_name, description, fuzzy_pool_available) if fuzzy_pool_available else []
            # Module compound-bridge pass (T-20260818-410274502), merged
            # additively into the same fuzzy result -- see
            # `fuzzy_match_modules_compound()` for why this needs its own
            # pass instead of being folded into `fuzzy_match_skills()`.
            # Dedup by id: a module can already be present in `fuzzy_matches`
            # via ordinary token overlap (e.g. `report-forge`), in which case
            # the bridge pass would just rediscover the same entry.
            if modules_pool_available:
                already_matched_ids = {c["id"] for c in fuzzy_matches}
                bridge_matches = [
                    m for m in fuzzy_match_modules_compound(expert_name, modules_pool_available)
                    if m["id"] not in already_matched_ids
                ]
                fuzzy_matches = fuzzy_matches + bridge_matches
            if fuzzy_matches:
                experts.append({
                    "name": expert_name,
                    "standalone_skill": None,
                    "status": "teilportiert",
                    "match": "fuzzy",
                    "matched_skills": sorted(c["id"] for c in fuzzy_matches),
                })
                continue

            experts.append({
                "name": expert_name,
                "standalone_skill": None,
                "status": "nicht-portiert",
                "match": None,
                "matched_skills": [],
            })

        # Boss-level tools (T-20260818-137943175): exact-only, no fuzzy/
        # "nicht-portiert" fallback -- unlike a named expert (which is
        # always listed, matched or not, since it's part of the boss's own
        # declared structure), a tool only produces an entry when it
        # actually resolved to something; a tool with no match is simply
        # not mentioned, same "add nothing unless there's something to say"
        # contract `match_domain_skill()`'s `__domain__:` entries already
        # follow.
        for tool_file in tool_files:
            tool_key = f"__tool__:{Path(tool_file).stem}"
            tool_match = global_exact_matches.get((dirname, tool_key))
            if tool_match:
                experts.append({
                    "name": tool_key,
                    "standalone_skill": tool_match["id"],
                    "status": "portiert",
                    "match": "exact",
                    "matched_skills": [tool_match["id"]],
                })

        # Stage 0 (domain-level, T-20260808-02): a standalone skill can cover
        # the WHOLE boss agent instead of one of its named experts (see
        # `match_domain_skill()` docstring). Applied AFTER the per-expert
        # loop so it only ever adds to an expert, never replaces a stage-1
        # exact match -- `fuzzy_pool_available` already excludes every ID
        # claimed by `global_exact_matches`.
        domain_matches = match_domain_skill(domain_id, label, fuzzy_pool_available)
        if domain_matches:
            domain_match_ids = sorted(c["id"] for c in domain_matches)
            if experts:
                for expert in experts:
                    if expert["status"] == "portiert":
                        continue  # never dilute a verified 1:1 provenance link
                    merged = sorted(set(expert["matched_skills"]) | set(domain_match_ids))
                    if merged == expert["matched_skills"]:
                        continue  # already covers every domain-level skill, nothing to add
                    expert["matched_skills"] = merged
                    if expert["status"] == "nicht-portiert":
                        expert["status"] = "teilportiert"
                        expert["match"] = "domain"
            else:
                # No orchestrated experts at all (e.g. `versicherung`'s boss
                # lists none) -- there is nowhere to attach the match, so add
                # a synthetic entry. The `__domain__:` prefix is not a valid
                # BACH expert-name shape (those come from `orchestrates.
                # experts` frontmatter, never contain "::" or this prefix),
                # so it can never collide with or be mistaken for a real
                # orchestrated expert.
                experts.append({
                    "name": f"__domain__:{dirname}",
                    "standalone_skill": domain_match_ids[0] if len(domain_match_ids) == 1 else None,
                    "status": "teilportiert",
                    "match": "domain",
                    "matched_skills": domain_match_ids,
                })

        domains.append({
            "id": domain_id,
            "label": label,
            "source_boss": dirname,
            "description": description,
            "usecases": extract_usecases(description),
            "services": services,
            "experts": experts,
        })

    domains.sort(key=lambda d: d["id"])

    # T-20260818-137943175: bach_origin:true library skills that never ended
    # up referenced anywhere in the output above (neither as an expert's
    # `standalone_skill`/`matched_skills`, nor as a tool's) -- provably
    # BACH-extracted, but structurally unattached to any boss's
    # `orchestrates.experts`/`dependencies.tools` (evidenced: `assist/
    # wetter`, `assist/tageszeitung`; verified none of the 8 BACH boss
    # agents' SKILL.md reference either by name, in frontmatter or body
    # text). Reported rather than force-attached via fuzzy proximity to
    # some unrelated expert -- see `load_skill_library()` docstring.
    referenced_skill_ids: set[str] = set()
    for domain in domains:
        for expert in domain["experts"]:
            if expert.get("standalone_skill"):
                referenced_skill_ids.add(expert["standalone_skill"])
            referenced_skill_ids.update(expert.get("matched_skills") or [])
    skill_library_unattached = sorted(
        c["id"] for c in skill_library_components if c["id"] not in referenced_skill_ids
    )

    return {
        "schema": "ticket-master-domains-v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": {
            "generator": "lib/domains_generator.py",
            "registry_provided": registry_components_path is not None,
            "bach_components_scanned": len(bach_components),
            "custom_components_scanned": len(custom_components),
            "extra_skills_dir_provided": extra_skills_dir is not None,
            "extra_skills_scanned": len(extra_skills),
            "modules_catalog_provided": modules_catalog_path is not None,
            "modules_scanned": len(modules_components),
            "skill_library_provided": skill_library_dir is not None,
            "skill_library_scanned": len(skill_library_components),
            "skill_library_fuzzy_scanned": len(skill_library_fuzzy_components),
            "skill_library_bach_origin_unattached": skill_library_unattached,
        },
        "domains": domains,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--bach-agents-dir",
        default=os.environ.get("TICKET_MASTER_BACH_AGENTS_DIR"),
        help="Path to the BACH system/agents/ directory (generation-time only, not read at runtime).",
    )
    parser.add_argument(
        "--skills-registry-components",
        default=os.environ.get("TICKET_MASTER_SKILLS_REGISTRY_COMPONENTS"),
        help="Path to a skill registry's components.json (for provenance cross-reference).",
    )
    parser.add_argument(
        "--extra-boss-dir", action="append", default=[],
        help="Additional boss-agent directory name to check (repeatable).",
    )
    parser.add_argument(
        "--extra-skills-dir",
        default=os.environ.get("TICKET_MASTER_EXTRA_SKILLS_DIR"),
        help=(
            "Optional second skill inventory (e.g. a Claude Code ~/.claude/skills/ "
            "tree) for stage-2 fuzzy matching, in case a skill was extracted as "
            "standalone but never registered in the main skill registry. Default: "
            "none (stage 2 then only uses the registry components)."
        ),
    )
    parser.add_argument(
        "--modules-catalog",
        default=os.environ.get("TICKET_MASTER_MODULES_CATALOG"),
        help=(
            "Optional path to an ellmos modules.catalog.json (T-20260818-410274502) "
            "-- a third matching source for experts that were extracted as a "
            "standalone MODULE rather than a standalone skill. Default: none "
            "(behaves exactly as before this option existed)."
        ),
    )
    parser.add_argument(
        "--skill-library-dir",
        default=os.environ.get("TICKET_MASTER_SKILL_LIBRARY_DIR"),
        help=(
            "Optional path to an ellmos skill library's skills/ root "
            "(T-20260818-137943175) -- PRIMARY stage-1 exact-match source, "
            "reading bach_origin/provenance.origin_path directly from each "
            "skill's own SKILL.md frontmatter. --skills-registry-components "
            "becomes a legacy fallback when this is set. Default: none "
            "(behaves exactly as before this option existed)."
        ),
    )
    parser.add_argument(
        "--output",
        default=str(Path(__file__).resolve().parent.parent / "config" / "domains.json"),
        help="Output path for the generated domains.json.",
    )
    args = parser.parse_args(argv)

    if not args.bach_agents_dir:
        print(
            "No --bach-agents-dir / TICKET_MASTER_BACH_AGENTS_DIR set — "
            "aborting cleanly, existing domains.json (if any) is left untouched.",
            file=sys.stderr,
        )
        return 2

    agents_dir = Path(args.bach_agents_dir)
    if not agents_dir.is_dir():
        print(f"BACH agents dir not found: {agents_dir} — aborting without changes.", file=sys.stderr)
        return 2

    registry_path = Path(args.skills_registry_components) if args.skills_registry_components else None
    if registry_path is not None and not registry_path.is_file():
        print(f"Skills registry components file not found: {registry_path} — continuing without it.", file=sys.stderr)
        registry_path = None

    extra_skills_dir = Path(args.extra_skills_dir) if args.extra_skills_dir else None
    if extra_skills_dir is not None and not extra_skills_dir.is_dir():
        print(f"Extra skills dir not found: {extra_skills_dir} — continuing without it.", file=sys.stderr)
        extra_skills_dir = None

    modules_catalog_path = Path(args.modules_catalog) if args.modules_catalog else None
    if modules_catalog_path is not None and not modules_catalog_path.is_file():
        print(f"Modules catalog not found: {modules_catalog_path} — continuing without it.", file=sys.stderr)
        modules_catalog_path = None

    skill_library_dir = Path(args.skill_library_dir) if args.skill_library_dir else None
    if skill_library_dir is not None and not skill_library_dir.is_dir():
        print(f"Skill library dir not found: {skill_library_dir} — continuing without it.", file=sys.stderr)
        skill_library_dir = None

    result = build_domains(
        agents_dir, registry_path, args.extra_boss_dir, extra_skills_dir,
        modules_catalog_path, skill_library_dir,
    )

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"domains.json written: {output_path} ({len(result['domains'])} domains)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
