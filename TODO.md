# TODO — ticket-master

## Security Audit 2026-07-16

- [x] `.gitignore` schützt jetzt zusätzliche lokale Credential-/Recovery- und
      SQLite-Artefakte (`.pypirc`, Passwort-JSON, Recovery-Code-Dateien,
      `*.sqlite`, `*.sqlite3`); Smoke-Test deckt die Muster ab.

## Review 2026-07-04 (Modul-Review-Loop, frischer Subagent — Funde gefixt in v1.4.1)

- [x] **(hoch)** `ticket_writer.create()` konnte Tickets still überschreiben
      (nicht-exklusives `write_text`) → exklusives Anlegen + Retry.
- [x] **(hoch)** ID-Vergabe scannte nur `QUEUED/` → doppelte IDs bei
      verschobenen Tickets; jetzt alle Lebenszyklus-Ordner.
- [x] **(hoch)** `doc_scanner.append_entry()` korrumpierte nicht-UTF-8-Bestand
      (errors="replace" + Rückschreiben) → strikt lesen, ValueError.
- [x] **(hoch)** `test_smoke.py` unter pytest wirkungslos (return statt assert)
      + verwaiste Pfade auf vor-1.3.0-Log-Speicherort (Test schlug bei
      `python tests/test_smoke.py` real fehl).
- [x] **(mittel/niedrig)** `prompts_dir` als reserved dokumentiert;
      SECURITY-Versionsmatrix, Badges, `.gitignore` (`_logs/`-Pfade, LOCK*.txt).
- [x] **(Folge, 2026-08-10)** `prompts_dir` in den `bin/`-Launchern tatsächlich
      auswerten: alle Starter delegieren an den gemeinsamen, repo-beschränkten
      Resolver `bin/ticket_master.py`.
- [x] **(Folge, Design — entschieden [U 2026-07-04 „immer verbesserungen
      rückangleichen"], umgesetzt in v1.5.0)** Angleichung an die private
      `_TICKETS`-Instanz: Sammel-Logdatei deprecated, Audit-Trail PRO Ticket
      (Prompts, READMEs, SKILL, llms.txt, Config, Log-Stub). Im Gegenzug die
      v1.4.1-Lib-Fixes (exklusives Anlegen, Lifecycle-IDs, striktes UTF-8)
      in die private laufende Instanz `_scripts/ticket_writer.py` +
      `_scripts/doc_scanner.py` gespiegelt (Lock-Watcher-Tests 8/8 grün).

## Personal-Assistant-Ausbau (entschieden [U 2026-07-04, Decision-Briefing E02/A])

> Ticket-Master wird zum persönlichen Live-Assistenten ausgebaut: er erkennt
> Nutzer-Usecases, bewertet Dringlichkeit (sofort vs. später) und delegiert an
> Skills/Module/Modelle. Umsetzungs-Ticket (privat, mit Systemdetails):
> `_control-center/_TICKETS/T-20260704-02.txt`.

- [x] **Phase 1 — Domänen-Map (generiert):** `lib/domains_generator.py` liest
      die Boss-Agent-Frontmatter (`orchestrates.experts`) und gleicht sie
      gegen eine Skill-Registry-`components.json` (`provenance.origin: bach`,
      `origin_path`) ab → `config/domains.json` (gitignored, generiert;
      Schema/Beispiel in `config/domains.example.json`). Nicht portierte
      Experten werden als `"status": "nicht-portiert"` markiert. Die
      5. Domäne (Versicherung/Finanzen) wird per Namens-/Beschreibungssuche
      gefunden, da ihr Ordnername variieren kann. Zur Laufzeit ist
      `domains.json` BACH-frei — BACH ist nur Generator-Input, kein
      Runtime-Dependency. Tests: `tests/test_domains_generator.py` (12 Fälle).
- [x] **Phase 2 — Dringlichkeitsachse:** `config/urgency.json` (Schema:
      `config/urgency.example.json`) — Domäne→Frist-Default-Matrix
      (`sofort|heute|woche|backlog`), entkoppelt vom 5-Dim-Komplexitäts-Score.
      Neues URGENCY-GATE in beiden Prompts (EN/DE) direkt nach GATE 1:
      Domain-Default lesen, Eskalationsregeln prüfen (veröffentlichte Software
      + schwerer Bug → sofort, ggf. nur Diagnose-Subagent zuerst;
      Trigger-Keywords → sofort), optionaler `preference_model_hint.command`
      bei echten Grenzfällen, niedrige Konfidenz → User fragen
      (`low_confidence_policy`). `woche`/`backlog` → optionale
      `task_db_command`-Senke statt Subagent-Spawn.
- [x] **Phase 3 — Delegations-Verdrahtung:** Intake-GATE (GATE 1) um
      DOMAIN/ENDPOINT erweitert (Lookup: `domains.json` →
      `controlcenter_find_skill`/skill-finder → generischer Fallback).
      Modellwahl bevorzugt optionalen `router_command` vor der
      Score→Tier-Formel, die zum expliziten Fallback degradiert (Duplikat
      bewusst NICHT entfernt — bleibt Fallback-Pfad). Neuer
      Rechteprüfungs-Schritt vor jedem Worker-Spawn in Abschnitt (B):
      `LOCK*.txt`/`LOCK.permissions.json`-Konventionen (deny>ask>allow,
      User-Locks absolut). Neue Template-Felder `DOMAIN`/`ENDPOINT`/`URGENCY`,
      neue Config-Felder `router_command`/`task_db_command`. Details:
      CHANGELOG 1.7.0. Rückangleichung an die private `_TICKETS`-Instanz
      (Prompt `_control-center/_prompts/TICKET-MASTER.txt`, Template
      `_control-center/_TICKETS/_templates/TICKET.txt`) erledigt.
- [x] **Phase 3 Follow-up (1.8.0) — Stage-2-Fuzzy-Matching:** Empirischer
      Befund (psycho-berater governs 19x `skill:therapy:*`, ohne
      Provenance-Link; lokale Skills wie `counseling-basics` fehlen in der
      Registry) zeigte: strikte 1:1-Provenance (Stage 1) reicht nicht, ein
      Experte kann eine ganze Skill-FAMILIE regieren. `fuzzy_match_skills()`
      + `KEYWORD_CATEGORY_HINTS` (nur gegen den Expertennamen, NICHT die
      geteilte Boss-Beschreibung — sonst Bleed-Over auf Geschwister-Experten,
      empirisch beobachtet) + optionaler zweiter Skill-Bestand
      (`load_extra_skills()` / `--extra-skills-dir`). Ergebnis:
      `"status": "teilportiert"` + `"match": "fuzzy"` +
      `"matched_skills"`-Liste (Stage-1-Treffer bleiben `"portiert"`/
      `"exact"`). Bereits stage-1-vergebene Skills werden für Geschwister-
      Experten aus dem Fuzzy-Pool ausgeschlossen. Beide Prompts (EN/DE) +
      private Instanz präzisiert: keine Experten-Ebene, GATEs lesen
      `standalone_skill`/`matched_skills` direkt, Worker bekommt bei
      `teilportiert` ALLE gelisteten Skills; optionale (harness-abhängige)
      Worker-Rollen-Wahl generisch im Modul, konkret (Claude-Code-
      Subagenten) in der privaten Instanz. Tests: 9 neue (32/32 gesamt).
- [ ] **Phase 1b — Usecase-Level-Matching (Folgepunkt, 2026-07-04):** Der
      Generator matcht nur die EXPERTEN-Ebene gegen Skills. Boss-EIGENE
      Usecases (im BACH-Bestand z.B. Dossier/Location/Route beim
      persönlichen Assistenten) haben aber teils bereits extrahierte
      Standalone-Skills (Kategorie `assist`, untracked: dossier-briefing,
      location-suche, reiseroute, …) und erscheinen trotzdem nicht als
      Endpunkte (empirisch verifiziert mit `--extra-skills-dir`).
      Erweiterung: `usecases[]` der Domäne ebenfalls fuzzy matchen →
      neues Feld `usecase_skills`, damit domains.json die volle
      Endpunkt-Landkarte trägt.
- [x] **Phase 4 (1.9.0) — Wissens-Schicht:** User-Leitsatz: Was den
      Ticket-Master zum persönlichen Assistenten macht, ist WISSEN über das
      System (wo was ist, Routing, MCP-Server, Subsysteme) — nicht nur
      Routing-Logik. `config/knowledge.json` (Schema:
      `config/knowledge.example.json`) mit `knowledge_sources` in 4
      Kategorien (`maps`/`state`/`capabilities`/`user_model`, je
      `{id, kind: file|command|mcp_tool, target, when_to_read}`). Neuer
      optionaler Boot-Schritt „(c3) SYSTEM-WISSEN laden" in beiden Prompts
      (EN/DE) direkt vor Position 0: `maps` beim Boot laden, `state` vor
      JEDER Routing-Entscheidung neu prüfen (nicht nur beim Boot),
      `capabilities` bei Endpunkt-/Modell-Lookup, `user_model` nur bei
      echten Grenzfällen. Grundregel: generierten Karten vertrauen, nicht
      dem Gedächtnis — bei Widerspruch Karte neu generieren lassen.
      Feldnamen bewusst englisch (`when_to_read`), konsistent mit den
      anderen Config-Beispielen des Moduls. Rückangleichung: private
      Instanz bekam eine konkrete SYSTEM-WISSEN-Sektion im Prompt (statt
      einer separaten JSON — passend zum bestehenden Muster dieser
      prosa-basierten Instanz) mit realen Pfaden/Kommandos (MANIFEST.md,
      domains.json, releases.json+MASTER-REGISTRY.md, repos.json+
      REPOS-INDEX.md, lock_watcher, _TICKETS, Rinnsal, controlcenter_*,
      clutch, tom-lm).
- [x] **Advisor-Review + Abschluss-Retest-Fixes (weiterhin 1.9.0):**
      Advisor-Auflagen (PFLICHT): `_tokenize()` auf Unicode-fähige Regex
      umgestellt (Umlaute/ß wurden vorher still zerschnitten); Exact-Match-
      Exklusion Stage1→Stage2 GLOBAL statt nur pro Boss geführt (Wahl,
      dokumentiert). Retest-Befunde B2–B6 (frischer Agent, beide
      User-Beispiele "bestanden mit Befunden"): B2 GATE1-Projektanker über
      Repo-/System-Inventar-`maps`-Quelle für Projekte außerhalb
      `project_roots[]`; B3 projekteigene Pflicht-Lektüre-Ketten liest der
      WORKER, nicht der Master (Lean-Router); B4 GATE3-Nutzungslimit auf
      Best-Effort-Selbsteinschätzung abgeschwächt (keine verlässliche
      Quelle); B5 Präzedenzregel für Dringlichkeits-Kollision (Keyword=WANN,
      Diagnose-zuerst=WAS → zusammen: sofort Diagnose-Subagent); B6
      Werkzeug-Hinweis gegen Glob-Timeouts über große/cloud-synchronisierte
      Ordner. B1 kein Fix nötig (erwartetes GAP-Design). Tests: 7 neu
      (39/39 gesamt).

## Roadmap

- [ ] **i18n:** Standardsprachen über DE/EN hinaus erweitern — dem Muster der
      ellmos-MCP-Server folgen (`set_language`-Mechanik, `README_de` + weitere
      Sprachdateien). Geplant: weitere `prompts/TICKET-MASTER.<lang>.md`
      (z.B. `es`, `fr`) + Sprachauswahl bereits vorbereitet
      (`TM_LANG`/`default_language`).

## Near-term

- [x] Python helper script (`bin/ticket_master.py`) as a thin wrapper that reads
      `config/ticket-master.config.json` and dispatches to the correct provider
      without shell-specific scripts — easier cross-platform maintenance.
- [x] `--list` mode: print deterministic, non-secret open-ticket metadata from
      `tickets/` to stdout (including v1 clusters and legacy aliases).
- [x] `--intake "description"` flag: validate and exclusively pre-create an
      unclaimed `INBOX/` ticket file from the command line without the shared
      intake log; `QUEUED/` starts only after a real handover.
- [ ] Config validation on startup: warn if `project_roots` is empty or provider
      commands are not found in PATH.

## Medium-term

- [ ] Optional TUI dashboard (curses or textual) showing ticket counts per
      lifecycle state.
- [ ] GitHub Issues bridge: pull open issues from a repo into `tickets/` as `.txt`
      files automatically.
- [ ] Webhook receiver: accept tickets via HTTP POST (e.g. from n8n or a CI system).
- [ ] pytest integration: convert `tests/test_smoke.py` to proper pytest suite.

## Long-term / Ideas

- [ ] Multi-repo support: manage tickets across several Git repositories from one
      ticket-master instance.
- [ ] Automatic companion rotation based on context-token watermarks.
- [ ] Web UI for ticket overview and manual routing overrides.

## Offen — Architektur-Vorbehalt SIG-TU (2026-07-31)

- [x] **ERLEDIGT 2026-08-15 — Verlegung vollzogen nach `ellmos-ai/system-auditor`.**
      Gewählt wurde die dritte der damals genannten Optionen (eigenständiges
      Integrity-Modul). Die Rolle ist dort als `prompts/AUDITOR.de.md` kanonisch;
      ticket-master ist nur noch Konsument über eine Maßnahmen-Senke und behält die
      Hoheit über Ticketformat, Kategorien, IDs und Routing. Die hiesigen
      `prompts/TICKET-WRITER.*.md` sind als abgelöst markiert.
      Ursprünglicher Vorbehalt:
      SIG-TU/TICKET-WRITER greift querschnittlich auf fremde Policy-,
      Entscheidungs- und Gedächtnis-Speicher zu und kann der Kapselung
      von ticket-master widersprechen. Zum Einführungszeitpunkt kein
      besserer Ort bekannt; Rolle bleibt vorerst hier, entkoppelt über
      `config/ticket-writer.config.json`. Kandidaten später: controlroom-
      Stack, policy-registry/lock-master oder eigenes Integrity-Modul.
      Vermerk in `prompts/TICKET-WRITER.de.md` + `.en.md` (Relocation
      note [K 2026-07-31]). [K 2026-07-31]

## Offen — SIG-TU-Rollentest 2026-08-01 (5 Reibungspunkte) — Rolle inzwischen ausgekapselt

> **2026-08-16:** Die geprüfte Rolle lebt seit 2026-08-15 als eigenes Modul in
> [`ellmos-ai/system-auditor`](https://github.com/ellmos-ai/system-auditor) (siehe
> Abschnitt „Architektur-Vorbehalt SIG-TU" oben). Diese Sektion bleibt als
> Herkunftsbeleg stehen; verbleibende offene Punkte sind nicht mehr Sache von
> ticket-master.
>
> Quelle: TICKET-WRITER/SIG-TU-Rollentest 2026-08-01 (Auftrag OP-TW-TEST),
> Bereich `control-center-manifest-vs-reality`. Reibungspunkte am
> Rollen-Prompt/Config selbst, nicht am geprüften Zielsystem — siehe
> Sessionbericht (USMC-Note ID 102) und `_control-center/_TICKETS/
> T-20260801-18.ASUS-GEI.txt` (das einzige produzierte Ticket des Laufs).

- [x] **Fail-Safe "kein Bereich zuteilbar" zu hart für Ersteinrichtung/
      Testbetrieb — behoben durch TASKSOLVER-Rollentest 2026-08-01
      (task-master Task-ID 2, taskplan.db).** `prompts/TICKET-WRITER.de.md`
      + `.en.md` (Abschnitt FAIL-SAFES) sahen bei fehlender Config nur
      "USER FRAGEN; kein Selbstwahl-Sweep" vor, ohne Ausnahme für den in
      einem frischen Deployment sehr wahrscheinlichen Fall "nur
      `*.example.json` vorhanden". Beide Sprachfassungen um eine explizite
      Ausnahme ergänzt (Selbstwahl NUR als gekennzeichneter Trockenlauf,
      mit Begründung im Sessionbericht; produktiver Einsatz ohne echte
      Config bleibt bei USER FRAGEN). Verifiziert: `python
      tests/test_smoke.py` weiterhin 4/4 grün.
- [x] **`<HOME>/SYSTEM-MANIFEST.md` in `ticket-writer.config.example.json`
      falsch — direkt gefixt (dieser Commit/Edit).** Die kanonische Datei
      liegt unter `<HOME>/OneDrive/SYSTEM-MANIFEST.md` (bestätigt durch
      `~/CLAUDE.md`/`~/OneDrive/CLAUDE.md`: "Kanonischer Ort:
      `~/OneDrive/SYSTEM-MANIFEST.md`"). Genau die Art Pfad-Drift, die
      SIG-TU selbst aufspüren soll.
- [x] **Beleg-C-Recherche hat keine Tiefen-/Umfangs-Leitplanke — im Nachfolger
      adressiert.** Der Prompt erlaubte für Beleg C ausdrücklich
      bereichsübergreifendes Lesen ("Bereichsdisziplin" im LOOP-CONTRACT),
      aber nicht, wie tief/wie viele Dateien das sein dürfen, bevor es
      faktisch zu einem zweiten, unkontrollierten Sweep wird. Im Testlauf
      brauchte die Auflösung von zwei Anfangsverdachten mehrere Leseschritte
      in `.AI/.MODULES/.CONTROL/controlroom/` (außerhalb des Zielbereichs
      `_control-center/`). `ellmos-ai/system-auditor` (`prompts/AUDITOR.en.md`,
      Abschnitt „Resolve rule sources") führt für Beleg B/C jetzt eine
      dritte Auflösungsstufe „convention — a bounded name list, bounded
      depth, only inside the domain" statt eines freien Verzeichnis-Crawls;
      kein separater Fix hier nötig, da die Rolle ausgekapselt ist.
- [x] **Datei-/Claim-Konvention beim Selbst-Anlegen
      eines SIG-TU-Tickets.** Der Prompt sagt nur "Ticket-Dateien im
      `tickets_dir`, nicht ob neu erzeugte SIG-TU-Tickets sofort mit
      Host-Suffix oder unclaimed in Root/INBOX abgelegt werden. Gelöst mit
      T-20260812-06: `TICKET-AUSGABEFORMAT` legt neue Tickets ohne Host-Suffix
      unter `INBOX/` mit `STATUS: INBOX` an; der Claim folgt bei Bearbeitung,
      `QUEUED` erst bei tatsächlicher Übergabe.
## TASKWRITER-Recheck 2026-08-02

Live-Baseline des kanonischen Clones: `main`, HEAD/origin
`51f370f`; Arbeitsbaum vor diesem Writeback sauber und synchron; keine
`LOCK.user*.txt`- oder echten `*WORKSTATION-LG*`-Treffer. TASKPLAN enthielt vor
dem Lauf keine Aufgaben für diesen Projektpfad.

Persistierte TASKPLAN-Aufgaben:

- `1914` — `prompts_dir` in allen Startern wirksam machen oder konsistent entfernen (high/medium/local) — erledigt 2026-08-10.
- `1915` — Usecase-Level-Matching im Domains-Generator ergänzen (high/large/local).
- `1916` — Plattformneutrale CLI-Schicht mit Konfigurationsvalidierung bauen (high/large/local).
- `1917` — `--list` und `--intake` als auditable CLI-Funktionen ergänzen (medium/medium/local) — erledigt 2026-08-10.
- `1918` — Prompt- und Dokumentationssprache über DE/EN hinaus erweitern (low/large/local).
- `1919` — `llms.txt`, Testbadge und Release-/Unreleased-Nachweis synchronisieren (high/medium/local) — erledigt 2026-08-10.

Die vollständigen Quellen, Soll/Ist-Ableitungen, Definition-of-Done, Prüfwege
und Blocker liegen im TASKPLAN-Register. Zum damaligen TASKWRITER-Lauf wurde
keine dieser Aufgaben ausgeführt; insbesondere kein Test, Build, CLI-Start,
Ticket-Write, Commit oder Push.

## TASKSOLVER-Readback 2026-08-10

Die Aufgaben 1914, 1917 und 1919 wurden im kanonischen Clone bearbeitet. Der
Der aktuelle Lauf hatte 95 Pytest-Erfolge; Prompt-Resolver, CLI-Ausgabe und
Intake-Kollisionen wurden gezielt geprüft. Release- und Tag-Aktionen blieben
unverändert ausstehend.
