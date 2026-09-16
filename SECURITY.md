# Security Policy

## Supported Versions

| Version | Supported |
|---------|-----------|
| 1.12.x | Yes |
| < 1.12 | No |

## Reporting a Vulnerability

If you discover a security vulnerability, please **do not** open a public GitHub
issue. Instead, report it privately:

1. Open a [GitHub Security Advisory](https://github.com/ellmos-ai/ticket-master/security/advisories/new)
   in this repository, or
2. Send a brief description to the maintainer via the contact on
   [github.com/lukisch](https://github.com/lukisch).

Please include:
- A description of the vulnerability
- Steps to reproduce or a proof-of-concept
- The potential impact

### Response Times & SLA

You can expect:
- An initial acknowledgement within **48 hours**.
- A preliminary assessment and triage classification within **5 business days**.
- A fix, mitigation plan, or security advisory within **30 days** where feasible.

## Scope & Security Architecture

`ticket-master` is engineered as a local-first task router and AI coding agent triage engine. It enforces strict local boundaries:

- **Zero Network Egress:** `ticket-master` does not make network requests, transmit telemetry, or send user data to remote endpoints.
- **Provider CLI Invocation:** Starters pass bootstrap prompts to locally installed LLM CLIs. Ensure your provider CLI executables are authentic and secured.
- **Configuration Hygiene:** `config/ticket-master.config.json` and real site configurations are gitignored. Do not commit credentials, tokens, or private endpoints.
- **Ticket Privacy:** Runtime ticket files in lifecycle directories (`INBOX`, `ACTIONABLE`, `QUEUED`, `BLOCKED`, `WAITING`, `USER`, `PARKED`, `PENDING`, `SOLVED`) are gitignored by default to prevent leaking internal project context.
- **Unprivileged Execution:** Runs strictly in user mode (`RunAsInvoker`) without administrative elevation.

### Governance & Runtime Invariants

| Invariant | Focus | Enforcement Mechanism |
|---|---|---|
| `INV-LOCAL-01` | Zero Egress | Pure local disk / user cloud sync; zero external telemetry. |
| `INV-WORKFLOW-02` | Agent Native | Transparent prompt-driven Position 0 triage; no background daemons. |
| `INV-ROUTING-03` | 5D Scoring | 5-dimension deterministic scoring with provider fallback chains. |
| `INV-CLAIM-04` | Multi-System | Atomic filesystem renames (`.claim-<host>-<ts>`); no database locks. |
| `INV-COMPANION-05` | Companion Pattern | Reusable companion sub-agents for batch orientation savings. |
| `INV-INTAKE-06` | Informal Intake | Byte-identical formalization via `ticket_writer.py --from-file`. |
| `INV-AUDITOR-07` | Auditor Bridge | Fail-closed sparmodus token budget gate for `system-auditor`. |
| `INV-UNPRIV-08` | Non-Elevation | Strict unprivileged `RunAsInvoker` user-mode execution. |
| `INV-PORTABLE-09` | 0 Dependencies | Pure Python standard library (`dependencies = []`). |
| `INV-SLA-10` | 48h SLA | Committed 48-hour response SLA and multi-OS CI testing matrix. |

---

## Sicherheitsrichtlinie (Deutsch)

### Unterstützte Versionen

| Version | Unterstützt |
|---------|-------------|
| 1.12.x | Ja |
| < 1.12 | Nein |

### Meldung von Sicherheitslücken

Wenn Sie eine Sicherheitslücke entdecken, eröffnen Sie bitte **kein** öffentliches GitHub-Issue. Melden Sie diese stattdessen vertraulich:

1. Über einen privaten [GitHub Security Advisory](https://github.com/ellmos-ai/ticket-master/security/advisories/new) in diesem Repository, oder
2. Über die Kontaktmöglichkeiten des Maintainers auf [github.com/lukisch](https://github.com/lukisch).

Bitte fügen Sie der Meldung Folgendes bei:
- Eine Beschreibung der Schwachstelle
- Schritte zur Reproduktion oder ein Proof-of-Concept
- Eine Einschätzung der potenziellen Auswirkungen

### Reaktionszeiten & SLA

- **48 Stunden:** Erste Bestätigung des Eingangs der Meldung.
- **5 Werktage:** Vorläufige Sicherheitsbewertung, Schweregrad-Einstufung und Triage.
- **30 Tage:** Bereitstellung eines Patches, Workarounds oder Sicherheitshinweises (wo machbar).

### Sicherheitsarchitektur & Schutzgrenzen

`ticket-master` ist als rein lokales Werkzeug für Aufgabenrouting und Agenten-Triage konzipiert:
- **Kein Netzwerk-Egress:** Das Modul führt standardmäßig keine Netzwerkaufrufe durch und überträgt keinerlei Telemetrie- oder Benutzerdaten.
- **Sichere Subprozesse:** Aufrufe lokaler Provider-CLIs erfolgen mit expliziten Argumenten ohne implizite Shell-Ausführung.
- **Lokale Datenhaltung:** Laufzeit-Tickets und reale Konfigurationsdateien sind per `.gitignore` geschützt und verbleiben ausschließlich auf dem lokalen Rechner.
- **Unprivilegierte Ausführung:** Standardmäßige Ausführung im Benutzermodus (`RunAsInvoker`) ohne administrative Rechte.

#### Governance- & Laufzeit-Invarianten

| Invariante | Bereich | Durchsetzungsmechanismus |
|---|---|---|
| `INV-LOCAL-01` | Zero Egress | Rein lokale Datenhaltung / Nutzer-Cloud-Sync; keine Telemetrie. |
| `INV-WORKFLOW-02` | Agent Native | Transparenter, prompt-gesteuerter Position-0-Triage-Modus; keine Daemons. |
| `INV-ROUTING-03` | 5D Scoring | 5-Dimensionen-Scoring mit deterministischer Fallback-Kette. |
| `INV-CLAIM-04` | Multi-System | Atomares Filesystem-Rename (`.claim-<host>-<ts>`); keine DB-Locks. |
| `INV-COMPANION-05` | Companion Pattern | Wiederverwendbare Companion-Sub-Agenten zur Orientierungs-Ersparnis. |
| `INV-INTAKE-06` | Formloser Intake | Zeichengetreue Formalisierung via `ticket_writer.py --from-file`. |
| `INV-AUDITOR-07` | Auditor-Brücke | Fail-Closed Token-Sparmodus-Gate für `system-auditor`. |
| `INV-UNPRIV-08` | Keine Elevation | Strikte unprivilegierte `RunAsInvoker`-Benutzermodus-Ausführung. |
| `INV-PORTABLE-09` | 0 Abhängigkeiten | Reine Python-Standardbibliothek (`dependencies = []`). |
| `INV-SLA-10` | 48h SLA | Verbindliche 48h-Sicherheits-Reaktionszeit und Multi-OS-CI-Matrix. |
