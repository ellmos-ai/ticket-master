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
                   modules_catalog_path: Path | None = None) -> dict:
    agents_dir = Path(agents_dir)
    if not agents_dir.is_dir():
        raise FileNotFoundError(f"BACH agents dir not found: {agents_dir}")

    bach_components: list[dict] = []
    custom_components: list[dict] = []
    if registry_components_path is not None and Path(registry_components_path).is_file():
        bach_components = load_bach_components(Path(registry_components_path))
        custom_components = load_custom_components(Path(registry_components_path))

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

    # Same dedup, extended to the module pool (T-20260818-410274502): if a
    # capability was already registered as a skill (bach or custom origin)
    # or an extra_skills_dir entry under the same declared name, the skill
    # copy wins and the module duplicate is dropped -- an established skill
    # match should not be silently displaced by a same-named module entry.
    # No collision exists in the corpus this was built against (verified:
    # none of the three targeted modules -- foerderplaner, report-forge,
    # ai-media-editor -- nor worksheet-generator share a name with any
    # current skill/extra_skills entry), but the guard costs nothing and
    # protects against future registry drift going the other way.
    skill_names = extra_skill_names | {
        str(c.get("name", "")).strip().lower() for c in bach_components + custom_components
    }
    modules_components = [
        m for m in modules_components
        if str(m.get("name", "")).strip().lower() not in skill_names
    ]

    # Stage 2 (fuzzy) only: `custom_components` is deliberately NOT added to
    # `bach_components` and never passed to `match_standalone_skill()` (see
    # `load_custom_components()` docstring) -- it only feeds the fuzzy pool.
    # `modules_components` DOES also feed stage 1 (via `match_standalone_
    # module()`, called separately below, T-20260818-410274502) since a
    # module -- unlike a "custom"-origin skill -- can be a genuine 1:1 name
    # identity for an expert (see that function's docstring).
    fuzzy_pool = bach_components + custom_components + extra_skills + modules_components

    boss_dirs = discover_boss_dirs(agents_dir, extra_boss_dirs)

    # Read every boss's frontmatter once, up front, so the exact-match
    # exclusion below can be computed GLOBALLY across all bosses/experts
    # before any fuzzy matching happens -- not just within one boss.
    boss_data: list[tuple[str, str, str, str, list[str], list[str]]] = []
    for dirname, path in sorted(boss_dirs.items()):
        skill_file = path / "SKILL.md"
        text = skill_file.read_text(encoding="utf-8", errors="replace")
        frontmatter = parse_frontmatter(text)
        domain_id, label = _domain_id_label(dirname, frontmatter)
        description = str(frontmatter.get("description", ""))
        orchestrates = frontmatter.get("orchestrates", {})
        expert_names = orchestrates.get("experts", []) if isinstance(orchestrates, dict) else []
        services = orchestrates.get("services", []) if isinstance(orchestrates, dict) else []
        boss_data.append((dirname, domain_id, label, description, expert_names, services))

    # Stage 1 (exact) runs for EVERY expert of EVERY boss first. The
    # resulting matched skill IDs are excluded from the stage-2 fuzzy pool
    # GLOBALLY (across all bosses, not just siblings within the same boss) --
    # otherwise a component could end up "portiert" for one expert here and,
    # via a coincidental keyword/token overlap, "teilportiert" for an
    # unrelated expert in a completely different domain.
    global_exact_matches: dict[tuple[str, str], dict] = {}
    for dirname, _domain_id, _label, _description, expert_names, _services in boss_data:
        for expert_name in expert_names:
            match = match_standalone_skill(expert_name, bach_components)
            if match is None and modules_components:
                # T-20260818-410274502: module-catalog exact tier, tried
                # only when the skill registry found nothing -- a skill-
                # registry provenance link is a stronger signal (an actual
                # BACH extraction record) than a bare name-identity match,
                # so it takes precedence whenever both happen to exist.
                match = match_standalone_module(expert_name, modules_components)
            if match:
                global_exact_matches[(dirname, expert_name)] = match
    global_exact_matched_ids = {m["id"] for m in global_exact_matches.values()}
    fuzzy_pool_available = [c for c in fuzzy_pool if c.get("id") not in global_exact_matched_ids]
    # Module-only subset of the same exact-excluded pool, for the module
    # compound-bridge pass below (T-20260818-410274502) -- reuses the exact-
    # match exclusion `fuzzy_pool_available` already computed rather than
    # filtering `modules_components` a second, independent way.
    modules_pool_available = [c for c in fuzzy_pool_available if c["id"].startswith("module:")]

    domains = []
    for dirname, domain_id, label, description, expert_names, services in boss_data:
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

    result = build_domains(agents_dir, registry_path, args.extra_boss_dir, extra_skills_dir, modules_catalog_path)

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"domains.json written: {output_path} ({len(result['domains'])} domains)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
