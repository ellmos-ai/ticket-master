# -*- coding: utf-8 -*-
"""Verifikation von lib/domains_generator.py (Phase 1, T-20260704-02): Parser
fuer BACH-Boss-Frontmatter + Abgleich gegen eine Skill-Registry components.json."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

LIB_DIR = Path(__file__).resolve().parent.parent / "lib"
sys.path.insert(0, str(LIB_DIR))

import domains_generator as dg  # noqa: E402


FIXTURE_BOSS_A = """---
name: fixture-boss-a
version: 1.0.0
type: boss-agent
status: active

orchestrates:
  experts: [fixture-expert-one, fixture-expert-two]
  services: []

description: >
  Fixture boss agent. Use this skill when: (1) doing thing one is needed,
  (2) doing thing two is needed, (3) doing thing three is needed.
---
# Fixture Boss A
"""

FIXTURE_BOSS_B_NO_LIST = """---
name: fixture-boss-b
version: 1.0.0
type: agent
status: active

orchestrates:
  experts: []
  services: []

description: >
  Fixture boss without a numbered usecase list, just prose.
---
"""


class TestParseFrontmatter(unittest.TestCase):
    def test_parses_experts_and_description(self):
        fm = dg.parse_frontmatter(FIXTURE_BOSS_A)
        self.assertEqual(fm["name"], "fixture-boss-a")
        self.assertEqual(fm["orchestrates"]["experts"], ["fixture-expert-one", "fixture-expert-two"])
        self.assertIn("doing thing one", fm["description"])

    def test_empty_experts_list(self):
        fm = dg.parse_frontmatter(FIXTURE_BOSS_B_NO_LIST)
        self.assertEqual(fm["orchestrates"]["experts"], [])

    def test_no_frontmatter_returns_empty(self):
        self.assertEqual(dg.parse_frontmatter("# just a heading\n"), {})


class TestExtractUsecases(unittest.TestCase):
    def test_splits_numbered_list(self):
        usecases = dg.extract_usecases(
            "Intro text: (1) doing thing one, (2) doing thing two, (3) doing thing three."
        )
        self.assertEqual(len(usecases), 3)
        self.assertIn("doing thing one", usecases[0])

    def test_falls_back_to_whole_description(self):
        usecases = dg.extract_usecases("Just a plain sentence without numbers.")
        self.assertEqual(usecases, ["Just a plain sentence without numbers."])

    def test_empty_description(self):
        self.assertEqual(dg.extract_usecases(""), [])


class TestMatchStandaloneSkill(unittest.TestCase):
    def _component(self, origin_path):
        return {"id": "skill:test:x", "provenance": {"origin": "bach", "origin_path": origin_path}}

    def test_matches_expert_agent_suffix_variant(self):
        # frontmatter lists "steuer-agent", the actual skill folder is "steuer"
        comps = [self._component("system/agents/_experts/steuer/CONCEPT.md")]
        match = dg.match_standalone_skill("steuer-agent", comps)
        self.assertIsNotNone(match)

    def test_matches_filename_stem(self):
        comps = [self._component("system/skills/workflows/foerderplaner.md")]
        match = dg.match_standalone_skill("foerderplaner", comps)
        self.assertIsNotNone(match)

    def test_no_match_returns_none(self):
        comps = [self._component("system/skills/therapie/psychoedukation.md")]
        match = dg.match_standalone_skill("gesundheitsverwalter", comps)
        self.assertIsNone(match)


class TestTokenize(unittest.TestCase):
    """Advisor-review regression (T-20260704-02): `[a-zA-Z0-9]+` silently
    split German umlauts/ß out of a word ("Fördermittelberater" ->
    {"f", "rdermittelberater"}), quietly losing token-overlap matches for
    any non-ASCII expert/skill name."""

    def test_umlaut_o_stays_one_token(self):
        self.assertEqual(dg._tokenize("Fördermittelberater"), {"fördermittelberater"})

    def test_umlaut_u_stays_one_token(self):
        self.assertEqual(dg._tokenize("Gesundheitsprüfung"), {"gesundheitsprüfung"})

    def test_eszett_stays_one_token(self):
        self.assertEqual(dg._tokenize("Straße"), {"straße"})

    def test_digits_still_tokenize(self):
        self.assertEqual(dg._tokenize("gpt4 test"), {"gpt", "4", "test"})


class TestFuzzyMatchSkills(unittest.TestCase):
    """Stage-2 (fuzzy) matching, T-20260704-02 follow-up: covers the
    empirical case that motivated it -- an expert like "psycho-berater"
    governing a whole "therapy" skill family in the registry, where none of
    the individual components carry a per-component provenance link back to
    that expert (stage 1 finds nothing) and the registry entries have no
    descriptive text to token-match on, only a shared `category`."""

    def test_umlaut_name_token_overlap_match(self):
        """Regression: before the Unicode-aware tokenizer fix, this match
        was silently lost because "Fördermittelberater" tokenized to
        {"f", "rdermittelberater"} instead of one token."""
        components = [{
            "id": "skill:funding:foerdermittelberater-tool",
            "name": "Fördermittelberater-Tool",
            "description": "",
            "category": None,
        }]
        matches = dg.fuzzy_match_skills("Fördermittelberater", "Handles funding.", components)
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["id"], "skill:funding:foerdermittelberater-tool")

    def _therapy_components(self):
        # Mirrors the real registry shape: category present, description
        # empty -- stage 1 (provenance) already ruled out for these, and
        # plain token overlap alone would not find them either.
        return [
            {"id": "skill:therapy:act-techniken", "name": "act-techniken", "description": "", "category": "therapy"},
            {"id": "skill:therapy:psychoedukation", "name": "psychoedukation", "description": "", "category": "therapy"},
        ]

    def test_category_hint_matches_whole_family(self):
        matches = dg.fuzzy_match_skills(
            "psycho-berater",
            "Coordinates health management and psychological counseling experts.",
            self._therapy_components(),
        )
        matched_ids = {m["id"] for m in matches}
        self.assertEqual(matched_ids, {"skill:therapy:act-techniken", "skill:therapy:psychoedukation"})

    def test_unrelated_expert_does_not_match_therapy_family(self):
        matches = dg.fuzzy_match_skills(
            "steuer-agent",
            "Handles tax filings and receipts.",
            self._therapy_components(),
        )
        self.assertEqual(matches, [])

    def test_token_overlap_on_shared_description_word(self):
        # No category hint here -- match must come from the shared,
        # sufficiently long token "counseling" between the boss description
        # and the component's own description (mirrors an extra-skills-dir
        # entry, which never carries a `category`).
        components = [{
            "id": "claude-skill:counseling-basics",
            "name": "counseling-basics",
            "description": "Fundamentals of therapeutic communication and counseling.",
            "category": None,
        }]
        matches = dg.fuzzy_match_skills(
            "psycho-berater",
            "Coordinates health management and psychological counseling experts.",
            components,
        )
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["id"], "claude-skill:counseling-basics")

    def test_generic_role_suffix_alone_does_not_cause_false_match(self):
        # "berater" alone (role suffix, stripped from name_tokens) must not
        # match a component just called "berater-tools" via name overlap.
        components = [{"id": "skill:other:berater-tools", "name": "berater-tools", "description": "", "category": "other"}]
        matches = dg.fuzzy_match_skills("foerderplaner", "Plans funding applications.", components)
        self.assertEqual(matches, [])

    # -- T-20260711-01: German compound words don't split on their own -----
    # ("haushaltsmanagement" is one token; the matching skill's name splits
    # on a hyphen into {"haushalt", "manager"}), so plain set-intersection
    # token overlap misses the match. `_compound_overlap()` bridges this via
    # length-guarded substring matching, scoped to the component's id/name
    # (not free-text description, to keep precision high).

    def test_compound_word_matches_hyphenated_skill_name(self):
        """Empirical case (T-20260711-01): expert "haushaltsmanagement" vs.
        real skill "haushalt-manager" -- exact token overlap finds nothing
        ({"haushaltsmanagement"} vs {"haushalt", "manager"}), the compound
        bridge must find it via the substantive "haushalt" fragment."""
        components = [{
            "id": "claude-skill:haushalt-manager",
            "name": "haushalt-manager",
            "description": "Unterstuetzt bei der Organisation von Haushaltsroutinen.",
            "category": None,
        }]
        matches = dg.fuzzy_match_skills("haushaltsmanagement", "Manages household tasks.", components)
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["id"], "claude-skill:haushalt-manager")

    def test_compound_word_matches_prefix_skill_name(self):
        """Empirical case (T-20260711-01): expert "gesundheitsverwalter" vs.
        real skill "gesundheit" -- the substantive "gesundheit" is a prefix
        of the compound, "verwalter" is the (generic, stripped) role suffix."""
        components = [{
            "id": "claude-skill:gesundheit",
            "name": "gesundheit",
            "description": "Unterstuetzt bei der Verwaltung von Medikamentenplaenen.",
            "category": None,
        }]
        matches = dg.fuzzy_match_skills("gesundheitsverwalter", "Manages health records.", components)
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["id"], "claude-skill:gesundheit")

    def test_compound_bridge_does_not_fire_on_unrelated_clean_token(self):
        """Negative case: "steuer-agent" and "foerderplaner" are already
        clean single tokens (no compound to split) with NO matching skill in
        the real inventory (verified 2026-07-11: absent from both the skill
        registry and the extra-skills-dir). The compound bridge must not
        manufacture a match against an unrelated skill just because it scans
        substrings -- "kein Overfitting, lieber kein Match als ein falscher
        Skill-Endpunkt" (T-20260711-01)."""
        components = [{
            "id": "claude-skill:buero",
            "name": "buero",
            "description": "Unterstuetzt bei Buero-Aufgaben: Bewerbungsmanagement, Berichtsgenerierung.",
            "category": None,
        }]
        matches = dg.fuzzy_match_skills("steuer-agent", "Handles tax filings and receipts.", components)
        self.assertEqual(matches, [])

    def test_compound_bridge_does_not_use_description_text(self):
        """The compound bridge is scoped to id/name only. A component whose
        FREE-TEXT DESCRIPTION happens to contain a compound-overlapping
        fragment, but whose id/name does not, must not match -- otherwise
        the bridge would degrade into the same noisy full-text search the
        existing docstring explicitly rejects for stage 2."""
        components = [{
            "id": "claude-skill:unrelated",
            "name": "unrelated",
            "description": "Verwaltet einen Haushalt nebenbei in der Beschreibung.",
            "category": None,
        }]
        matches = dg.fuzzy_match_skills("haushaltsmanagement", "Manages household tasks.", components)
        self.assertEqual(matches, [])

    def test_compound_bridge_rejects_short_generic_fragment(self):
        """Regression (T-20260711-04, real data): expert "worksheet_generator"
        vs. unrelated component "genogram-work" -- both happen to contain the
        4-char substring "work", but that is a coincidental fragment, not a
        semantic match. MIN_COMPOUND_TOKEN_LEN=6 must reject this; a lower
        threshold (originally 4) let it through, producing a wrong endpoint
        for a real BACH expert once orchestrates.experts was completed."""
        components = [{
            "id": "skill:therapy:genogram-work",
            "name": "genogram-work",
            "description": "",
            "category": "therapy",
        }]
        matches = dg.fuzzy_match_skills("worksheet_generator", "Generates worksheets.", components)
        self.assertEqual(matches, [])

    def test_psycho_berater_category_hint_still_wins_over_compound_bridge(self):
        """Regression: psycho-berater's existing KEYWORD_CATEGORY_HINTS match
        must not be lost or altered by the new compound-overlap path."""
        matches = dg.fuzzy_match_skills(
            "psycho-berater",
            "Coordinates health management and psychological counseling experts.",
            self._therapy_components(),
        )
        matched_ids = {m["id"] for m in matches}
        self.assertEqual(matched_ids, {"skill:therapy:act-techniken", "skill:therapy:psychoedukation"})

    # -- T-20260711-05: case (c) exact-overlap scoped to id/name only ------
    # (previously ran over the full id+name+description haystack, so an
    # expert whose own name happened to be a common English word matched any
    # component whose free-text description mentioned that word in an
    # unrelated context). Four real cases from T-20260711-04 regeneration.

    def test_case_c_ignores_description_only_report_token(self):
        """report_generator must NOT match a component whose description
        merely contains "report" in an unrelated phrase ("Bug-Report-
        Template"); id/name carry no overlap."""
        components = [{
            "id": "claude-skill:bugfix-protocol",
            "name": "bugfix-protocol",
            "description": "Systematisches Debugging-Protokoll mit Bug-Report-Template.",
            "category": None,
        }]
        matches = dg.fuzzy_match_skills("report_generator", "Foerderbericht-Generierung.", components)
        self.assertEqual(matches, [])

    def test_case_c_ignores_description_only_generator_token(self):
        """report_generator/worksheet_generator must NOT match a component
        whose description merely mentions "newspaper_generator.py"."""
        components = [{
            "id": "claude-skill:tageszeitung",
            "name": "tageszeitung",
            "description": "Portiert aus dem BACH-Newssystem (news.py + newspaper_generator.py).",
            "category": None,
        }]
        matches = dg.fuzzy_match_skills("worksheet_generator", "Generates worksheets.", components)
        self.assertEqual(matches, [])

    def test_case_c_ignores_description_only_generic_buero_bewerbung_words(self):
        """report_generator/worksheet_generator must NOT match "buero" or
        "bewerbungsexperte" purely because their descriptions happen to
        contain "generator"/"generiert"-like fragments; neither the expert
        name nor these components' own id/name overlap."""
        components = [
            {"id": "claude-skill:buero", "name": "buero",
             "description": "Bewerbungsmanagement, Berichtsgenerierung und Office-Verwaltung.",
             "category": None},
            {"id": "claude-skill:bewerbungsexperte", "name": "bewerbungsexperte",
             "description": "Generiert ASCII-Lebenslaeufe aus einer SQLite-Datenbank.",
             "category": None},
        ]
        matches = dg.fuzzy_match_skills("report_generator", "Foerderbericht-Generierung.", components)
        self.assertEqual(matches, [])

    def test_case_c_ignores_description_only_health_token(self):
        """health_import must NOT match a component whose description
        merely contains the word "health" in an unrelated context."""
        components = [{
            "id": "claude-skill:rotation-check",
            "name": "rotation-check",
            "description": "Standard-Geruest fuer rotierende Pipeline-Checks ueber Projekt-Health.",
            "category": None,
        }]
        matches = dg.fuzzy_match_skills("health_import", "Medizinische Dokumentenverarbeitung.", components)
        self.assertEqual(matches, [])

    def test_case_c_still_matches_on_real_id_name_overlap(self):
        """Sanity check: case (c) must still fire when the overlap is in
        id/name, not just description -- the scoping must not have gutted
        the mechanism entirely."""
        components = [{
            "id": "claude-skill:textproduction",
            "name": "textproduction",
            "description": "Unrelated free text that says nothing about the match.",
            "category": None,
        }]
        matches = dg.fuzzy_match_skills("textproduction", "Content creation agent.", components)
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["id"], "claude-skill:textproduction")


class TestLoadExtraSkills(unittest.TestCase):
    def test_loads_frontmatter_from_extra_skills_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            skill_dir = Path(tmp) / "bewerbungsexperte"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text("""---
name: bewerbungsexperte
description: >
  Specialist for the whole job-application process.
---
""", encoding="utf-8")
            found = dg.load_extra_skills(Path(tmp))
            self.assertEqual(len(found), 1)
            self.assertEqual(found[0]["id"], "claude-skill:bewerbungsexperte")
            self.assertEqual(found[0]["name"], "bewerbungsexperte")
            self.assertIn("job-application", found[0]["description"])
            self.assertIsNone(found[0]["category"])

    def test_skips_entries_without_skill_md(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "not-a-skill").mkdir()
            self.assertEqual(dg.load_extra_skills(Path(tmp)), [])

    def test_missing_dir_returns_empty_list(self):
        self.assertEqual(dg.load_extra_skills(Path("/nonexistent/extra/skills/dir")), [])


class TestBuildDomains(unittest.TestCase):
    def test_build_domains_end_to_end(self):
        with tempfile.TemporaryDirectory() as tmp:
            agents_dir = Path(tmp) / "agents"
            (agents_dir / "fixture-boss-a").mkdir(parents=True)
            (agents_dir / "fixture-boss-a" / "SKILL.md").write_text(FIXTURE_BOSS_A, encoding="utf-8")

            registry_path = Path(tmp) / "components.json"
            registry_path.write_text(json.dumps({
                "components": [
                    {
                        "id": "skill:test:fixture-expert-one",
                        "provenance": {"origin": "bach", "origin_path": "system/agents/_experts/fixture-expert-one/CONCEPT.md"},
                    }
                ]
            }), encoding="utf-8")

            result = dg.build_domains(agents_dir, registry_path, extra_boss_dirs=["fixture-boss-a"])
            self.assertEqual(result["schema"], "ticket-master-domains-v1")
            self.assertEqual(len(result["domains"]), 1)
            domain = result["domains"][0]
            self.assertEqual(domain["source_boss"], "fixture-boss-a")
            experts_by_name = {e["name"]: e for e in domain["experts"]}
            self.assertEqual(experts_by_name["fixture-expert-one"]["status"], "portiert")
            self.assertEqual(experts_by_name["fixture-expert-one"]["match"], "exact")
            self.assertEqual(experts_by_name["fixture-expert-one"]["matched_skills"], ["skill:test:fixture-expert-one"])
            self.assertEqual(experts_by_name["fixture-expert-two"]["status"], "nicht-portiert")
            self.assertIsNone(experts_by_name["fixture-expert-two"]["standalone_skill"])
            self.assertIsNone(experts_by_name["fixture-expert-two"]["match"])
            self.assertEqual(experts_by_name["fixture-expert-two"]["matched_skills"], [])
            self.assertFalse(result["source"]["extra_skills_dir_provided"])
            self.assertEqual(result["source"]["extra_skills_scanned"], 0)

    def test_stage_2_fuzzy_match_via_category_hint(self):
        """End-to-end version of the empirical psycho-berater/therapy case:
        stage 1 finds nothing (no provenance link at all), stage 2 finds the
        whole category via KEYWORD_CATEGORY_HINTS."""
        with tempfile.TemporaryDirectory() as tmp:
            agents_dir = Path(tmp) / "agents"
            boss_dir = agents_dir / "fixture-boss-c"
            boss_dir.mkdir(parents=True)
            boss_dir.joinpath("SKILL.md").write_text("""---
name: fixture-boss-c
orchestrates:
  experts: [psycho-berater]
  services: []
description: >
  Coordinates health management and psychological counseling experts.
---
""", encoding="utf-8")

            registry_path = Path(tmp) / "components.json"
            registry_path.write_text(json.dumps({
                "components": [
                    {"id": "skill:therapy:psychoedukation", "name": "psychoedukation", "category": "therapy",
                     "provenance": {"origin": "bach", "origin_path": "system/skills/therapie/psychoedukation.md"}},
                ]
            }), encoding="utf-8")

            result = dg.build_domains(agents_dir, registry_path, extra_boss_dirs=["fixture-boss-c"])
            expert = result["domains"][0]["experts"][0]
            self.assertEqual(expert["name"], "psycho-berater")
            self.assertEqual(expert["status"], "teilportiert")
            self.assertEqual(expert["match"], "fuzzy")
            self.assertEqual(expert["matched_skills"], ["skill:therapy:psychoedukation"])
            self.assertIsNone(expert["standalone_skill"])

    def test_extra_skills_dir_feeds_stage_2(self):
        with tempfile.TemporaryDirectory() as tmp:
            agents_dir = Path(tmp) / "agents"
            boss_dir = agents_dir / "fixture-boss-d"
            boss_dir.mkdir(parents=True)
            boss_dir.joinpath("SKILL.md").write_text("""---
name: fixture-boss-d
orchestrates:
  experts: [psycho-berater]
  services: []
description: >
  Coordinates health management and psychological counseling experts.
---
""", encoding="utf-8")

            extra_skills_dir = Path(tmp) / "extra-skills"
            skill_dir = extra_skills_dir / "counseling-basics"
            skill_dir.mkdir(parents=True)
            skill_dir.joinpath("SKILL.md").write_text("""---
name: counseling-basics
description: >
  Fundamentals of therapeutic communication and counseling.
---
""", encoding="utf-8")

            result = dg.build_domains(
                agents_dir, None, extra_boss_dirs=["fixture-boss-d"], extra_skills_dir=extra_skills_dir,
            )
            expert = result["domains"][0]["experts"][0]
            self.assertEqual(expert["status"], "teilportiert")
            self.assertEqual(expert["matched_skills"], ["claude-skill:counseling-basics"])
            self.assertTrue(result["source"]["extra_skills_dir_provided"])
            self.assertEqual(result["source"]["extra_skills_scanned"], 1)

    def test_exact_match_excluded_from_sibling_experts_fuzzy_pool(self):
        """Advisor-review regression test: a skill exact-matched (stage 1) to
        one expert must not ALSO be fuzzy-matched (stage 2) to a sibling
        expert of the same boss, even if a KEYWORD_CATEGORY_HINTS stem would
        otherwise match it."""
        with tempfile.TemporaryDirectory() as tmp:
            agents_dir = Path(tmp) / "agents"
            boss_dir = agents_dir / "fixture-boss-e"
            boss_dir.mkdir(parents=True)
            boss_dir.joinpath("SKILL.md").write_text("""---
name: fixture-boss-e
orchestrates:
  experts: [foerderplaner, psycho-berater]
  services: []
description: >
  Coordinates funding planning and psychological counseling experts.
---
""", encoding="utf-8")

            registry_path = Path(tmp) / "components.json"
            registry_path.write_text(json.dumps({
                "components": [
                    {"id": "skill:test:shared-skill", "name": "shared-skill", "category": "therapy",
                     "provenance": {"origin": "bach", "origin_path": "system/agents/_experts/foerderplaner/CONCEPT.md"}},
                ]
            }), encoding="utf-8")

            result = dg.build_domains(agents_dir, registry_path, extra_boss_dirs=["fixture-boss-e"])
            experts_by_name = {e["name"]: e for e in result["domains"][0]["experts"]}
            self.assertEqual(experts_by_name["foerderplaner"]["status"], "portiert")
            self.assertEqual(experts_by_name["foerderplaner"]["standalone_skill"], "skill:test:shared-skill")
            # psycho-berater's KEYWORD_CATEGORY_HINTS stem would match
            # category "therapy", but the skill is already claimed exactly
            # by its sibling foerderplaner -- must not show up here too.
            self.assertEqual(experts_by_name["psycho-berater"]["status"], "nicht-portiert")
            self.assertNotIn("skill:test:shared-skill", experts_by_name["psycho-berater"]["matched_skills"])

    def test_exact_match_exclusion_is_global_across_bosses(self):
        """The exclusion above is deliberately GLOBAL, not just per-boss: a
        skill exact-matched to an expert in one boss must not be
        fuzzy-matched to an unrelated expert in a DIFFERENT boss either."""
        with tempfile.TemporaryDirectory() as tmp:
            agents_dir = Path(tmp) / "agents"
            boss_buero = agents_dir / "fixture-boss-buero"
            boss_buero.mkdir(parents=True)
            boss_buero.joinpath("SKILL.md").write_text("""---
name: fixture-boss-buero
orchestrates:
  experts: [foerderplaner]
  services: []
description: >
  Coordinates funding planning.
---
""", encoding="utf-8")
            boss_gesundheit = agents_dir / "fixture-boss-gesundheit"
            boss_gesundheit.mkdir(parents=True)
            boss_gesundheit.joinpath("SKILL.md").write_text("""---
name: fixture-boss-gesundheit
orchestrates:
  experts: [psycho-berater]
  services: []
description: >
  Coordinates psychological counseling.
---
""", encoding="utf-8")

            registry_path = Path(tmp) / "components.json"
            registry_path.write_text(json.dumps({
                "components": [
                    {"id": "skill:test:shared-skill", "name": "shared-skill", "category": "therapy",
                     "provenance": {"origin": "bach", "origin_path": "system/agents/_experts/foerderplaner/CONCEPT.md"}},
                ]
            }), encoding="utf-8")

            result = dg.build_domains(
                agents_dir, registry_path,
                extra_boss_dirs=["fixture-boss-buero", "fixture-boss-gesundheit"],
            )
            all_experts = [e for dom in result["domains"] for e in dom["experts"]]
            experts_by_name = {e["name"]: e for e in all_experts}
            self.assertEqual(experts_by_name["foerderplaner"]["status"], "portiert")
            self.assertEqual(experts_by_name["psycho-berater"]["status"], "nicht-portiert")
            self.assertNotIn("skill:test:shared-skill", experts_by_name["psycho-berater"]["matched_skills"])

    def test_missing_agents_dir_raises(self):
        with self.assertRaises(FileNotFoundError):
            dg.build_domains(Path("/nonexistent/agents/dir/xyz"), None)

    def test_discovers_variable_named_insurance_dir(self):
        """5. Boss-Domaene kann einen abweichenden Ordnernamen haben (z.B.
        'versicherungs-agent' statt 'versicherungen') — wird per Namens-/
        Beschreibungssuche gefunden, nicht per fixem Pfad."""
        with tempfile.TemporaryDirectory() as tmp:
            agents_dir = Path(tmp) / "agents"
            odd_dir = agents_dir / "versicherungs-kram"
            odd_dir.mkdir(parents=True)
            (odd_dir / "SKILL.md").write_text("""---
name: versicherungs-agent
type: agent
orchestrates:
  experts: []
  services: []
description: >
  Dedicated agent for insurance and financial planning.
---
""", encoding="utf-8")
            found = dg.discover_boss_dirs(agents_dir)
            self.assertIn("versicherungs-kram", found)

    # -- T-20260711-06: stage-2-only "custom"-origin pool + dedup ----------
    # Intent check (verified against real data before implementing, see
    # ticket VERLAUF): the origin=="bach" filter is deliberate for stage 1
    # (match_standalone_skill, "was this ported from BACH") but was
    # unreflectively inherited by stage 2 (fuzzy) when it was added later.
    # All affected "custom"-origin skills were individually verified
    # git-tracked and free of any privacy/maturity marker.

    def _registry_with_custom_component(self, tmp, comp_id="skill:dev:example-tool",
                                         name="example-tool", origin_path=None):
        registry_path = Path(tmp) / "components.json"
        comp = {"id": comp_id, "name": name, "description": "", "category": None,
                "provenance": {"origin": "custom"}}
        if origin_path:
            comp["provenance"]["origin_path"] = origin_path
        registry_path.write_text(json.dumps({"components": [comp]}), encoding="utf-8")
        return registry_path

    def test_custom_origin_component_reaches_stage_2_fuzzy_pool(self):
        """Core T-20260711-06 case: a registered "custom"-origin skill, not
        mirrored anywhere else, must now be reachable via stage 2 (it was
        previously invisible to the whole matching pipeline)."""
        with tempfile.TemporaryDirectory() as tmp:
            agents_dir = Path(tmp) / "agents"
            boss_dir = agents_dir / "fixture-boss-custom"
            boss_dir.mkdir(parents=True)
            boss_dir.joinpath("SKILL.md").write_text("""---
name: fixture-boss-custom
orchestrates:
  experts: [rotation-check]
  services: []
description: >
  Coordinates rotation-based pipeline checks.
---
""", encoding="utf-8")
            registry_path = self._registry_with_custom_component(
                tmp, comp_id="skill:dev:rotation-check", name="rotation-check")

            result = dg.build_domains(agents_dir, registry_path, extra_boss_dirs=["fixture-boss-custom"])
            expert = result["domains"][0]["experts"][0]
            self.assertEqual(expert["status"], "teilportiert")
            self.assertEqual(expert["match"], "fuzzy")
            self.assertEqual(expert["matched_skills"], ["skill:dev:rotation-check"])
            self.assertEqual(result["source"]["custom_components_scanned"], 1)

    def test_custom_origin_component_never_reaches_stage_1_exact(self):
        """Grenze 1 (Team-Lead): stage 1 (match_standalone_skill) must stay
        bach-only. Even if a "custom"-origin component's origin_path
        happens to reference the expert's own name/folder, it must NOT
        produce an exact/"portiert" match -- only fuzzy/"teilportiert"."""
        with tempfile.TemporaryDirectory() as tmp:
            agents_dir = Path(tmp) / "agents"
            boss_dir = agents_dir / "fixture-boss-custom2"
            boss_dir.mkdir(parents=True)
            boss_dir.joinpath("SKILL.md").write_text("""---
name: fixture-boss-custom2
orchestrates:
  experts: [rotation-check]
  services: []
description: >
  Coordinates rotation-based pipeline checks.
---
""", encoding="utf-8")
            # origin_path deliberately references the expert's own folder
            # segment, the way a real BACH-extracted component would -- but
            # this component is origin=="custom", so it must still not
            # trigger stage 1.
            registry_path = self._registry_with_custom_component(
                tmp, comp_id="skill:dev:rotation-check", name="rotation-check",
                origin_path="system/agents/_experts/rotation-check/CONCEPT.md")

            result = dg.build_domains(agents_dir, registry_path, extra_boss_dirs=["fixture-boss-custom2"])
            expert = result["domains"][0]["experts"][0]
            self.assertNotEqual(expert["status"], "portiert")
            self.assertNotEqual(expert["match"], "exact")
            self.assertIsNone(expert["standalone_skill"])

    def test_custom_component_deduped_against_extra_skills_by_name(self):
        """Grenze 2 (Team-Lead): dedup by name BEFORE merging into the fuzzy
        pool. A "custom"-origin registry component and an extra-skills-dir
        entry that share the same declared name are the SAME real skill
        mirrored twice under two different ids -- the registry copy must be
        dropped so the expert's matched_skills lists the skill exactly
        ONCE, not twice under two different ids."""
        with tempfile.TemporaryDirectory() as tmp:
            agents_dir = Path(tmp) / "agents"
            boss_dir = agents_dir / "fixture-boss-dedup"
            boss_dir.mkdir(parents=True)
            boss_dir.joinpath("SKILL.md").write_text("""---
name: fixture-boss-dedup
orchestrates:
  experts: [rotation-check]
  services: []
description: >
  Coordinates rotation-based pipeline checks.
---
""", encoding="utf-8")
            registry_path = self._registry_with_custom_component(
                tmp, comp_id="skill:dev:rotation-check", name="rotation-check")

            extra_skills_dir = Path(tmp) / "extra-skills"
            skill_dir = extra_skills_dir / "rotation-check"
            skill_dir.mkdir(parents=True)
            skill_dir.joinpath("SKILL.md").write_text("""---
name: rotation-check
description: >
  Standard rig for rotating pipeline checks.
---
""", encoding="utf-8")

            result = dg.build_domains(
                agents_dir, registry_path, extra_boss_dirs=["fixture-boss-dedup"],
                extra_skills_dir=extra_skills_dir,
            )
            expert = result["domains"][0]["experts"][0]
            self.assertEqual(expert["status"], "teilportiert")
            # Exactly ONE id for "rotation-check", the extra_skills_dir one --
            # the registry duplicate was deduped away before merging.
            self.assertEqual(expert["matched_skills"], ["claude-skill:rotation-check"])
            self.assertEqual(result["source"]["custom_components_scanned"], 0)

    def test_bach_origin_matching_unaffected_by_custom_pool(self):
        """Regression: adding the custom-origin pool must not change
        existing bach-origin exact-match behaviour (Grenze 1)."""
        with tempfile.TemporaryDirectory() as tmp:
            agents_dir = Path(tmp) / "agents"
            (agents_dir / "fixture-boss-a").mkdir(parents=True)
            (agents_dir / "fixture-boss-a" / "SKILL.md").write_text(FIXTURE_BOSS_A, encoding="utf-8")

            registry_path = Path(tmp) / "components.json"
            registry_path.write_text(json.dumps({
                "components": [
                    {
                        "id": "skill:test:fixture-expert-one",
                        "provenance": {"origin": "bach", "origin_path": "system/agents/_experts/fixture-expert-one/CONCEPT.md"},
                    },
                    {
                        "id": "skill:test:unrelated-custom",
                        "name": "unrelated-custom",
                        "description": "",
                        "provenance": {"origin": "custom"},
                    },
                ]
            }), encoding="utf-8")

            result = dg.build_domains(agents_dir, registry_path, extra_boss_dirs=["fixture-boss-a"])
            experts_by_name = {e["name"]: e for e in result["domains"][0]["experts"]}
            self.assertEqual(experts_by_name["fixture-expert-one"]["status"], "portiert")
            self.assertEqual(experts_by_name["fixture-expert-one"]["match"], "exact")
            self.assertEqual(experts_by_name["fixture-expert-one"]["matched_skills"], ["skill:test:fixture-expert-one"])
            self.assertEqual(result["source"]["custom_components_scanned"], 1)


class TestLoadCustomComponents(unittest.TestCase):
    def test_filters_to_custom_origin_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry_path = Path(tmp) / "components.json"
            registry_path.write_text(json.dumps({
                "components": [
                    {"id": "skill:a:bach-one", "provenance": {"origin": "bach"}},
                    {"id": "skill:b:custom-one", "provenance": {"origin": "custom"}},
                    {"id": "skill:c:custom-two", "provenance": {"origin": "custom"}},
                ]
            }), encoding="utf-8")
            found = dg.load_custom_components(registry_path)
            self.assertEqual({c["id"] for c in found}, {"skill:b:custom-one", "skill:c:custom-two"})


class TestMatchDomainSkill(unittest.TestCase):
    """T-20260808-02: a standalone skill can cover a whole boss agent rather
    than any single named expert -- verified against the real 2026-08-08
    corpus (buero/versicherung) before being reduced to these fixtures."""

    def test_exact_name_equals_domain_id(self):
        components = [{"id": "claude-skill:buero", "name": "buero"}]
        matches = dg.match_domain_skill("buero", "Büro, Steuer & Förderung", components)
        self.assertEqual([c["id"] for c in matches], ["claude-skill:buero"])

    def test_whole_token_overlap_with_domain_label(self):
        components = [{"id": "claude-skill:finanz-versicherung", "name": "finanz-versicherung"}]
        matches = dg.match_domain_skill("versicherung", "Versicherung & Finanzen", components)
        self.assertEqual([c["id"] for c in matches], ["claude-skill:finanz-versicherung"])

    def test_no_match_for_unrelated_component(self):
        components = [{"id": "claude-skill:textproduction", "name": "textproduction"}]
        matches = dg.match_domain_skill("buero", "Büro, Steuer & Förderung", components)
        self.assertEqual(matches, [])

    def test_hyphenated_whole_token_does_match(self):
        """A hyphen is a token boundary, so "content" inside a hyphenated
        compound name IS a whole token -- this is the intended, safe case,
        not the substring-bridging failure mode below."""
        components = [{"id": "skill:test:content-strategy-toolkit", "name": "content-strategy-toolkit"}]
        matches = dg.match_domain_skill("content", "Content & Produktion", components)
        self.assertEqual([c["id"] for c in matches], ["skill:test:content-strategy-toolkit"])

    def test_substring_inside_a_single_word_does_not_false_positive(self):
        """Regression guard for the T-20260711-04 failure class (a short
        fragment bridging two unrelated names via substring containment):
        "content" is a substring of "contentment", but as a WHOLE token
        "contentment" != "content" -- must not match, unlike the hyphenated
        case above."""
        components = [{"id": "skill:test:contentment-tracker", "name": "contentment-tracker"}]
        matches = dg.match_domain_skill("content", "Content & Produktion", components)
        self.assertEqual(matches, [], "substring containment must not match, only whole-token equality")

    def test_description_is_never_consulted(self):
        """T-20260711-05 regression class: matching must stay scoped to a
        component's own id/name, never its free-text description."""
        components = [{
            "id": "skill:test:unrelated", "name": "unrelated-thing",
            "description": "This tool has nothing to do with buero directly but mentions buero in passing.",
        }]
        matches = dg.match_domain_skill("buero", "Büro, Steuer & Förderung", components)
        self.assertEqual(matches, [])


class TestBuildDomainsStage0(unittest.TestCase):
    """End-to-end coverage for the Stage-0 domain-level wiring inside
    `build_domains()` (T-20260808-02)."""

    def _boss_with_experts(self, tmp, dirname, expert_names):
        boss_dir = Path(tmp) / "agents" / dirname
        boss_dir.mkdir(parents=True)
        experts_literal = "[" + ", ".join(expert_names) + "]"
        boss_dir.joinpath("SKILL.md").write_text(f"""---
name: {dirname}
orchestrates:
  experts: {experts_literal}
  services: []
description: >
  Fixture boss agent for domain-level matching tests.
---
""", encoding="utf-8")
        return Path(tmp) / "agents"

    def test_domain_level_skill_covers_all_experts_without_own_match(self):
        """The buero case: none of the four experts individually names or
        describes the covering skill, so only Stage 0 can find it."""
        with tempfile.TemporaryDirectory() as tmp:
            agents_dir = self._boss_with_experts(
                tmp, "bueroassistent",
                ["steuer-agent", "foerderplaner", "report_generator", "worksheet_generator"],
            )
            extra_skills_dir = Path(tmp) / "extra-skills"
            skill_dir = extra_skills_dir / "buero"
            skill_dir.mkdir(parents=True)
            skill_dir.joinpath("SKILL.md").write_text("""---
name: buero
description: >
  Ordnet Bueroaufgaben, Korrespondenz und Fristen.
---
""", encoding="utf-8")

            result = dg.build_domains(
                agents_dir, None, extra_boss_dirs=["bueroassistent"],
                extra_skills_dir=extra_skills_dir,
            )
            domain = result["domains"][0]
            self.assertEqual(domain["id"], "buero")
            for expert in domain["experts"]:
                self.assertEqual(expert["status"], "teilportiert", expert["name"])
                self.assertEqual(expert["match"], "domain", expert["name"])
                self.assertEqual(expert["matched_skills"], ["claude-skill:buero"], expert["name"])

    def test_domain_with_zero_experts_gets_synthetic_pseudo_expert(self):
        """The versicherung case: the boss frontmatter lists zero experts, so
        there is nowhere to attach a per-expert match -- a synthetic
        `__domain__:<boss>` entry must carry it instead."""
        with tempfile.TemporaryDirectory() as tmp:
            agents_dir = self._boss_with_experts(tmp, "versicherungen", [])
            extra_skills_dir = Path(tmp) / "extra-skills"
            skill_dir = extra_skills_dir / "finanz-versicherung"
            skill_dir.mkdir(parents=True)
            skill_dir.joinpath("SKILL.md").write_text("""---
name: finanz-versicherung
description: >
  Strukturiert Finanz- und Versicherungsunterlagen.
---
""", encoding="utf-8")

            result = dg.build_domains(
                agents_dir, None, extra_boss_dirs=["versicherungen"],
                extra_skills_dir=extra_skills_dir,
            )
            domain = result["domains"][0]
            self.assertEqual(len(domain["experts"]), 1)
            pseudo = domain["experts"][0]
            self.assertEqual(pseudo["name"], "__domain__:versicherungen")
            self.assertEqual(pseudo["status"], "teilportiert")
            self.assertEqual(pseudo["match"], "domain")
            self.assertEqual(pseudo["standalone_skill"], "claude-skill:finanz-versicherung")
            self.assertEqual(pseudo["matched_skills"], ["claude-skill:finanz-versicherung"])

    def test_stage_1_exact_match_is_never_diluted_by_stage_0(self):
        """An expert with a verified 1:1 provenance link (stage 1, status
        "portiert") must be left untouched even if a domain-level skill also
        exists -- stage 0 may only ever add to weaker (or absent) matches."""
        with tempfile.TemporaryDirectory() as tmp:
            agents_dir = self._boss_with_experts(tmp, "bueroassistent", ["steuer-agent"])
            registry_path = Path(tmp) / "components.json"
            registry_path.write_text(json.dumps({
                "components": [
                    {"id": "skill:test:steuer-agent", "name": "steuer-agent",
                     "provenance": {"origin": "bach",
                                    "origin_path": "system/agents/_experts/steuer-agent/CONCEPT.md"}},
                ]
            }), encoding="utf-8")
            extra_skills_dir = Path(tmp) / "extra-skills"
            skill_dir = extra_skills_dir / "buero"
            skill_dir.mkdir(parents=True)
            skill_dir.joinpath("SKILL.md").write_text("""---
name: buero
description: >
  Ordnet Bueroaufgaben, Korrespondenz und Fristen.
---
""", encoding="utf-8")

            result = dg.build_domains(
                agents_dir, registry_path, extra_boss_dirs=["bueroassistent"],
                extra_skills_dir=extra_skills_dir,
            )
            expert = result["domains"][0]["experts"][0]
            self.assertEqual(expert["status"], "portiert")
            self.assertEqual(expert["match"], "exact")
            self.assertEqual(expert["matched_skills"], ["skill:test:steuer-agent"])

    def test_no_domain_level_skill_leaves_experts_unchanged(self):
        """Regression safety: a domain with no whole-domain skill (like the
        real 'alltag'/'content' domains) must come out byte-for-byte the same
        as without Stage 0."""
        with tempfile.TemporaryDirectory() as tmp:
            agents_dir = self._boss_with_experts(tmp, "fixture-boss-f", ["some-expert"])
            result = dg.build_domains(agents_dir, None, extra_boss_dirs=["fixture-boss-f"])
            expert = result["domains"][0]["experts"][0]
            self.assertEqual(expert["status"], "nicht-portiert")
            self.assertIsNone(expert["match"])
            self.assertEqual(expert["matched_skills"], [])

    def test_existing_expert_level_fuzzy_match_is_not_duplicated(self):
        """The gesundheit/health_import case, generalised: if an expert
        ALREADY carries the domain-level skill via its own stage-2 fuzzy
        match, Stage 0 must be a dedup no-op (no relabel to "domain", no
        duplicate list entry) -- only an expert that does NOT yet have it
        should be upgraded."""
        with tempfile.TemporaryDirectory() as tmp:
            agents_dir = self._boss_with_experts(
                tmp, "gesundheitsassistent", ["gesundheitsverwalter", "health_import"],
            )
            extra_skills_dir = Path(tmp) / "extra-skills"
            skill_dir = extra_skills_dir / "gesundheit"
            skill_dir.mkdir(parents=True)
            skill_dir.joinpath("SKILL.md").write_text("""---
name: gesundheit
description: >
  Verwaltet Medikamentenplaene und Arztberichte.
---
""", encoding="utf-8")

            result = dg.build_domains(
                agents_dir, None, extra_boss_dirs=["gesundheitsassistent"],
                extra_skills_dir=extra_skills_dir,
            )
            experts_by_name = {e["name"]: e for e in result["domains"][0]["experts"]}
            # "gesundheitsverwalter" already whole-token-overlaps "gesundheit"
            # via its own stage-2 fuzzy match -- Stage 0 must not relabel it.
            self.assertEqual(experts_by_name["gesundheitsverwalter"]["match"], "fuzzy")
            self.assertEqual(experts_by_name["gesundheitsverwalter"]["matched_skills"], ["claude-skill:gesundheit"])
            # "health_import" has no expert-level match at all -- Stage 0 is
            # the only thing that can cover it.
            self.assertEqual(experts_by_name["health_import"]["match"], "domain")
            self.assertEqual(experts_by_name["health_import"]["matched_skills"], ["claude-skill:gesundheit"])


class TestLoadModulesCatalog(unittest.TestCase):
    """T-20260818-410274502: third matching source, the ellmos module
    catalog."""

    def _write_catalog(self, tmp, modules):
        path = Path(tmp) / "modules.catalog.json"
        path.write_text(json.dumps({"modules": modules}), encoding="utf-8")
        return path

    def test_normalizes_module_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_catalog(tmp, [
                {"id": "foerderplaner", "display_name": "foerderplaner",
                 "description": "Foerderplanung und ICF."},
            ])
            found = dg.load_modules_catalog(path)
            self.assertEqual(len(found), 1)
            self.assertEqual(found[0]["id"], "module:foerderplaner")
            self.assertEqual(found[0]["name"], "foerderplaner")
            self.assertIn("Foerderplanung", found[0]["description"])
            self.assertIsNone(found[0]["category"])

    def test_falls_back_to_bare_id_when_display_name_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_catalog(tmp, [{"id": "report-forge"}])
            found = dg.load_modules_catalog(path)
            self.assertEqual(found[0]["name"], "report-forge")

    def test_missing_file_returns_empty_list(self):
        self.assertEqual(dg.load_modules_catalog(Path("/nonexistent/modules.catalog.json")), [])

    def test_malformed_json_returns_empty_list_not_a_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "modules.catalog.json"
            path.write_text("{not valid json", encoding="utf-8")
            self.assertEqual(dg.load_modules_catalog(path), [])


class TestMatchStandaloneModule(unittest.TestCase):
    """Exact-tier module matching (T-20260818-410274502): raw full-token-set
    equality, no `_GENERIC_EXPERT_NAME_TOKENS` stripping."""

    def test_exact_name_identity_matches(self):
        modules = [{"id": "module:foerderplaner", "name": "foerderplaner"}]
        match = dg.match_standalone_module("foerderplaner", modules)
        self.assertIsNotNone(match)
        self.assertEqual(match["id"], "module:foerderplaner")

    def test_hyphen_underscore_insensitive(self):
        modules = [{"id": "module:worksheet-generator", "name": "worksheet-generator"}]
        match = dg.match_standalone_module("worksheet_generator", modules)
        self.assertIsNotNone(match)

    def test_partial_token_overlap_is_not_an_exact_match(self):
        """report_generator/report-forge share only the token "report" --
        this must stay fuzzy (T-20260818-410274502), not be promoted to
        exact just because one token overlaps."""
        modules = [{"id": "module:report-forge", "name": "report-forge"}]
        self.assertIsNone(dg.match_standalone_module("report_generator", modules))

    def test_generic_suffix_stripping_would_have_caused_a_false_positive(self):
        """Regression guard for the design this function deliberately avoids:
        "steuer-agent" and "steuer-assistent" reduce to the same token set
        ({"steuer"}) if generic suffixes are stripped first -- match_
        standalone_module() must NOT do that, or it would silently claim an
        exact 1:1 identity between two different names and drop the sibling
        candidate "steuer-suite" that stage-2 fuzzy should still surface."""
        modules = [{"id": "module:steuer-assistent", "name": "steuer-assistent"}]
        self.assertIsNone(dg.match_standalone_module("steuer-agent", modules))

    def test_no_match_returns_none(self):
        modules = [{"id": "module:unrelated-thing", "name": "unrelated-thing"}]
        self.assertIsNone(dg.match_standalone_module("foerderplaner", modules))


class TestFuzzyMatchModulesCompound(unittest.TestCase):
    """Module-only compound bridge (T-20260818-410274502): the targeted
    `mediaproduction`/`ai-media-editor` case, which needs a 5-character
    bridge ("media") the shared skill threshold of 6 deliberately excludes."""

    def test_media_bridges_mediaproduction_to_ai_media_editor(self):
        modules = [{"id": "module:ai-media-editor", "name": "ai-media-editor"}]
        matches = dg.fuzzy_match_modules_compound("mediaproduction", modules)
        self.assertEqual([m["id"] for m in matches], ["module:ai-media-editor"])

    def test_matches_multiple_modules_for_one_expert(self):
        """Both ai-media-editor and media-editor-core are legitimately
        media-production-related (T-20260813 core/app split) -- a double
        match here is correct, not noise."""
        modules = [
            {"id": "module:ai-media-editor", "name": "ai-media-editor"},
            {"id": "module:media-editor-core", "name": "media-editor-core"},
        ]
        matches = dg.fuzzy_match_modules_compound("mediaproduction", modules)
        self.assertEqual(sorted(m["id"] for m in matches), ["module:ai-media-editor", "module:media-editor-core"])

    def test_four_character_fragment_does_not_bridge(self):
        """"work" (4 chars) must stay below the module threshold too -- same
        false-positive class as T-20260711-04's skill-pool regression, this
        time verified against the module-only threshold of 5."""
        modules = [{"id": "module:network-tool", "name": "network-tool"}]
        self.assertEqual(dg.fuzzy_match_modules_compound("worksheet-generator", modules), [])

    def test_no_expert_tokens_returns_empty(self):
        modules = [{"id": "module:ai-media-editor", "name": "ai-media-editor"}]
        self.assertEqual(dg.fuzzy_match_modules_compound("", modules), [])


class TestBuildDomainsModulesCatalog(unittest.TestCase):
    """End-to-end coverage for the module-catalog third source inside
    `build_domains()` (T-20260818-410274502). Regression fixtures shaped
    after the ticket's three evidenced cases, kept synthetic/portable --
    the live corpus itself is verified separately as part of the actual
    domains.json regeneration, not committed as a unit test."""

    def _boss(self, tmp, dirname, expert_names):
        boss_dir = Path(tmp) / "agents" / dirname
        boss_dir.mkdir(parents=True)
        experts_literal = "[" + ", ".join(expert_names) + "]"
        boss_dir.joinpath("SKILL.md").write_text(f"""---
name: {dirname}
orchestrates:
  experts: {experts_literal}
  services: []
description: >
  Fixture boss agent for module-catalog matching tests.
---
""", encoding="utf-8")
        return Path(tmp) / "agents"

    def _modules_catalog(self, tmp, modules):
        path = Path(tmp) / "modules.catalog.json"
        path.write_text(json.dumps({"modules": modules}), encoding="utf-8")
        return path

    def test_default_behaviour_unchanged_when_modules_catalog_not_provided(self):
        """Regression guard: build_domains() without modules_catalog_path
        (the default) must behave identically to before this ticket."""
        with tempfile.TemporaryDirectory() as tmp:
            agents_dir = self._boss(tmp, "fixture-boss-m0", ["foerderplaner"])
            result = dg.build_domains(agents_dir, None, extra_boss_dirs=["fixture-boss-m0"])
            expert = result["domains"][0]["experts"][0]
            self.assertEqual(expert["status"], "nicht-portiert")
            self.assertFalse(result["source"]["modules_catalog_provided"])
            self.assertEqual(result["source"]["modules_scanned"], 0)

    def test_exact_name_match_reports_portiert_with_module_reference(self):
        """The foerderplaner case: exact name identity -> "portiert" tier,
        target is "module:<id>"."""
        with tempfile.TemporaryDirectory() as tmp:
            agents_dir = self._boss(tmp, "fixture-boss-m1", ["foerderplaner"])
            catalog = self._modules_catalog(tmp, [
                {"id": "foerderplaner", "display_name": "foerderplaner",
                 "description": "Foerderplanung."},
            ])
            result = dg.build_domains(
                agents_dir, None, extra_boss_dirs=["fixture-boss-m1"],
                modules_catalog_path=catalog,
            )
            expert = result["domains"][0]["experts"][0]
            self.assertEqual(expert["status"], "portiert")
            self.assertEqual(expert["match"], "exact")
            self.assertEqual(expert["standalone_skill"], "module:foerderplaner")
            self.assertEqual(expert["matched_skills"], ["module:foerderplaner"])
            self.assertTrue(result["source"]["modules_catalog_provided"])
            self.assertEqual(result["source"]["modules_scanned"], 1)

    def test_token_overlap_reports_teilportiert(self):
        """The report_generator/report-forge case: shared token "report",
        differing second token -> stays fuzzy, found via the existing
        fuzzy_match_skills() token-overlap case once modules are pooled."""
        with tempfile.TemporaryDirectory() as tmp:
            agents_dir = self._boss(tmp, "fixture-boss-m2", ["report_generator"])
            catalog = self._modules_catalog(tmp, [
                {"id": "report-forge", "display_name": "report-forge"},
            ])
            result = dg.build_domains(
                agents_dir, None, extra_boss_dirs=["fixture-boss-m2"],
                modules_catalog_path=catalog,
            )
            expert = result["domains"][0]["experts"][0]
            self.assertEqual(expert["status"], "teilportiert")
            self.assertEqual(expert["match"], "fuzzy")
            self.assertEqual(expert["matched_skills"], ["module:report-forge"])
            self.assertIsNone(expert["standalone_skill"])

    def test_compound_bridge_reports_teilportiert(self):
        """The mediaproduction/ai-media-editor case: no token overlap at
        all, found only via the module-only compound bridge."""
        with tempfile.TemporaryDirectory() as tmp:
            agents_dir = self._boss(tmp, "fixture-boss-m3", ["mediaproduction"])
            catalog = self._modules_catalog(tmp, [
                {"id": "ai-media-editor", "display_name": "ai-media-editor"},
            ])
            result = dg.build_domains(
                agents_dir, None, extra_boss_dirs=["fixture-boss-m3"],
                modules_catalog_path=catalog,
            )
            expert = result["domains"][0]["experts"][0]
            self.assertEqual(expert["status"], "teilportiert")
            self.assertEqual(expert["matched_skills"], ["module:ai-media-editor"])

    def test_skill_registry_exact_match_takes_precedence_over_module(self):
        """If both a skill-registry provenance link and a module name-
        identity match exist for the same expert, the skill (a real BACH
        extraction record) wins -- module exact matching is only tried when
        the skill registry found nothing."""
        with tempfile.TemporaryDirectory() as tmp:
            agents_dir = self._boss(tmp, "fixture-boss-m4", ["foerderplaner"])
            registry_path = Path(tmp) / "components.json"
            registry_path.write_text(json.dumps({
                "components": [{
                    "id": "skill:buero:foerderplaner",
                    "provenance": {"origin": "bach", "origin_path": "system/agents/_experts/foerderplaner/CONCEPT.md"},
                }],
            }), encoding="utf-8")
            catalog = self._modules_catalog(tmp, [
                {"id": "foerderplaner", "display_name": "foerderplaner"},
            ])
            result = dg.build_domains(
                agents_dir, registry_path, extra_boss_dirs=["fixture-boss-m4"],
                modules_catalog_path=catalog,
            )
            expert = result["domains"][0]["experts"][0]
            self.assertEqual(expert["standalone_skill"], "skill:buero:foerderplaner")

    def test_module_dropped_when_name_collides_with_existing_skill(self):
        """Dedup guard: a module sharing a declared name with an existing
        skill/extra_skills entry is dropped from the module pool -- the
        skill copy wins, matching the same-shaped custom-component dedup
        this ticket extended (T-20260711-06 precedent)."""
        with tempfile.TemporaryDirectory() as tmp:
            agents_dir = self._boss(tmp, "fixture-boss-m5", ["some-expert"])
            registry_path = Path(tmp) / "components.json"
            registry_path.write_text(json.dumps({
                "components": [{
                    "id": "skill:custom:shared-name", "name": "shared-name",
                    "provenance": {"origin": "custom"},
                }],
            }), encoding="utf-8")
            catalog = self._modules_catalog(tmp, [
                {"id": "shared-name", "display_name": "shared-name"},
            ])
            result = dg.build_domains(
                agents_dir, registry_path, extra_boss_dirs=["fixture-boss-m5"],
                modules_catalog_path=catalog,
            )
            self.assertEqual(result["source"]["modules_scanned"], 0)

    def test_no_module_match_still_reports_nicht_portiert(self):
        with tempfile.TemporaryDirectory() as tmp:
            agents_dir = self._boss(tmp, "fixture-boss-m6", ["totally-unrelated-expert"])
            catalog = self._modules_catalog(tmp, [
                {"id": "ai-media-editor", "display_name": "ai-media-editor"},
            ])
            result = dg.build_domains(
                agents_dir, None, extra_boss_dirs=["fixture-boss-m6"],
                modules_catalog_path=catalog,
            )
            expert = result["domains"][0]["experts"][0]
            self.assertEqual(expert["status"], "nicht-portiert")


if __name__ == "__main__":
    unittest.main()
