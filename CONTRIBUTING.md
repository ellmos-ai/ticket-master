# Contributing to ticket-master

[English](#english) | [Deutsch](#deutsch)

---

<a id="english"></a>
## English

Thank you for your interest in contributing to **ticket-master**!

### Architectural Principles & Governance Invariants

`ticket-master` is a prompt-driven workflow and operating mode that turns an active AI coding agent session into a structured, reliable **Position 0** triage console across multi-provider environments (Claude Code, OpenAI Codex, agy/Gemini). All contributions must respect our foundational invariants:

1. **100% Local-First & Zero-Egress (`INV-LOCAL-01`)**: All tickets, queues, lifecycles, and logs remain strictly on local disk; zero cloud telemetry or outbound network calls.
2. **Agent-Native Workflow (`INV-WORKFLOW-02`)**: Transparent prompt-driven Position 0 triage mode; zero opaque background daemons.
3. **5-Dimension Routing (`INV-ROUTING-03`)**: Deterministic scoring across Clarity, Complexity, Creativity, Context, and Criticality with a graceful fallback chain.
4. **Multi-System Claims (`INV-CLAIM-04`)**: Atomic filesystem renames (`.claim-<host>-<ts>`) without external database servers or distributed locking daemons.
5. **Companion Pattern (`INV-COMPANION-05`)**: Domain-grouped tasks reuse companion sub-agents, amortizing orientation cost.
6. **Informal Intake (`INV-INTAKE-06`)**: Raw text notes in `INBOX/` are formalized with byte-identical `ORIGINALTEXT` preservation.
7. **Auditor Bridge (`INV-AUDITOR-07`)**: Seamless `system-auditor` bridge with fail-closed sparmodus token-budget gating.
8. **Unprivileged Execution (`INV-UNPRIV-08`)**: Standard user-mode execution (`RunAsInvoker`); zero UAC or root privileges required.
9. **Zero Dependencies Core (`INV-PORTABLE-09`)**: Pure Python standard library (`dependencies = []`); optional extras cleanly isolated.
10. **Transparent SLA (`INV-SLA-10`)**: Permissive MIT license, automated multi-OS CI matrix, and committed 48h security SLA.

### Development Guidelines & Quality Gates

- **Plan D Architecture**: Development, git operations, and tests occur strictly in the canonical local git repository clone (`C:\_Local_DEV\repos\ticket-master`). OneDrive mirrors are derived read-only projections.
- **Python Compatibility**: Python 3.10, 3.11, 3.12, and 3.13 support.
- **Zero-Dependency Core**: The core package relies strictly on the official Python Standard Library; external runtime dependencies are strictly prohibited in the core package.
- **Version Freeze Policy**: Versions are strictly frozen per `T-20260920-167562623` unless explicitly authorized by maintainers. All ongoing improvements land under `## [Unreleased]` in `CHANGELOG.md`.
- **Canonical Lock System**: Respect all active repository and filesystem locks (`LOCK.user.*`, `LOCK.until.*`, `LOCK.condition.*`).
- **Pre-commit Quality Gates**:
  - Bytecode compilation: `python -m compileall -q lib tests`
  - Code formatting & linting: `ruff check .`
  - Automated test suite: `pytest` (100% green required)
  - Git whitespace hygiene: `git diff --check`
- **Security Vulnerabilities**: Never report security vulnerabilities publicly. Please follow our [SECURITY.md](SECURITY.md) guidelines for responsible disclosure (48h response SLA).

---

<a id="deutsch"></a>
## Deutsch

Vielen Dank für Ihr Interesse an einer Mitarbeit an **ticket-master**!

### Architektur-Prinzipien & Governance-Invarianten

`ticket-master` ist ein prompt-gesteuerter Workflow und Betriebsmodus, der eine aktive KI-Coding-Agenten-Sitzung in eine strukturierte, zuverlässige **Position 0** Triage-Konsole für Multi-Provider-Umgebungen (Claude Code, OpenAI Codex, agy/Gemini) verwandelt. Alle Beiträge müssen unsere grundlegenden Invarianten einhalten:

1. **100% Local-First & Zero-Egress (`INV-LOCAL-01`)**: Sämtliche Tickets, Warteschlangen, Lebenszyklen und Protokolle verbleiben strikt auf der lokalen Festplatte; keinerlei Telemetrie oder externe Netzwerkaufrufe.
2. **Agent-Nativer Workflow (`INV-WORKFLOW-02`)**: Transparenter prompt-gesteuerter Position 0 Triage-Modus; keine undurchsichtigen Hintergrund-Daemons.
3. **5-Dimensionen-Routing (`INV-ROUTING-03`)**: Deterministische Bewertung über Klarheit, Komplexität, Kreativität, Kontext und Kritikalität mit ausfallsicherer Fallback-Kette.
4. **Multi-System-Claims (`INV-CLAIM-04`)**: Atomare Dateisystem-Umbenennungen (`.claim-<host>-<ts>`) ohne externe Datenbankserver oder verteilte Sperrdienste.
5. **Companion-Muster (`INV-COMPANION-05`)**: Domänen-gruppierte Aufgaben nutzen langlebige Begleit-Subagenten wieder und amortisieren Orientierungskosten.
6. **Formlose Einreichung (`INV-INTAKE-06`)**: Rohe Notizen in `INBOX/` werden unter byte-identischer Erhaltung des `ORIGINALTEXT`-Blocks formalisiert.
7. **Auditor-Brücke (`INV-AUDITOR-07`)**: Nahtlose `system-auditor`-Brücke mit fail-closed Sparmodus-Tokenbudget-Prüfung.
8. **Unprivilegierter Benutzermodus (`INV-UNPRIV-08`)**: Ausführung strikt im unprivilegierten Anwenderbereich (`RunAsInvoker`); keine Administrator- oder Root-Rechte erforderlich.
9. **Null externe Laufzeitabhängigkeiten (`INV-PORTABLE-09`)**: Reine Python-Standardbibliothek im Kern (`dependencies = []`); optionale Extras sauber isoliert.
10. **Transparente SLA (`INV-SLA-10`)**: Freie MIT-Lizenz, automatisierte Multi-OS CI-Matrix und verbindliche 48-Stunden-Sicherheits-SLA.

### Richtlinien für Entwickler & Qualitäts-Tore

- **Plan D Architektur**: Entwicklung und Tests erfolgen ausschließlich im lokalen kanonischen Git-Repository (`C:\_Local_DEV\repos\ticket-master`). OneDrive-Spiegel sind abgeleitete Projektionen.
- **Python-Unterstützung**: Python 3.10 bis 3.13.
- **Null externe Laufzeitabhängigkeiten**: Der Kern basiert rein auf der Python-Standardbibliothek.
- **Versions-Einfrier-Richtlinie**: Versionen bleiben eingefroren per `T-20260920-167562623`; Neuerungen werden unter `## [Unreleased]` in `CHANGELOG.md` erfasst.
- **Kanonisches Lock-System**: Alle aktiven Sperren (`LOCK.user.*`, `LOCK.until.*`, `LOCK.condition.*`) sind verbindlich zu respektieren.
- **Qualitäts-Tore vor Commits**:
  - Bytecode-Prüfung: `python -m compileall -q lib tests`
  - Linter & Code-Prüfung: `ruff check .`
  - Testsuite: `pytest` (100% grün erforderlich)
  - Git-Whitespace-Prüfung: `git diff --check`
- **Sicherheitsmeldungen**: Sicherheitslücken bitte niemals öffentlich melden, sondern gemäß [SECURITY.md](SECURITY.md) vertraulich einreichen (48h Reaktions-SLA).
