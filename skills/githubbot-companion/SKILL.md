---
name: githubbot-companion
description: Route authorized GitHub repository maintenance to one native companion using an existing githubbot-core installation and its typed actions, without starting a second bot or service.
---

# GitHubBot-Core als Companion

Diese nutzerneutrale Andockstelle ergänzt den TICKET-MASTER. Sie installiert
keinen Bot und kopiert keine privaten Betriebsprofile in öffentliche Registries.
Ein vorhandener privater Betriebs-Skill kann Root, Interpreter, Konfigurations-
schichten und Paketprompt auflösen; diese Instanzdaten bleiben außerhalb dieses Repos.

## Konkreter Dispatch

Der Root beauftragt **einen nativen Companion** im bereits laufenden Harness.
Der Auftrag enthält Ticket-ID, angefragten Repo-/PR-Scope, erlaubte Tätigkeiten,
belegte Konfigurationsreferenzen und das gewünschte Ergebnisformat. Modell und
Provider stammen aus dem aktuellen Laufvertrag. Der Companion bleibt der
ausführende Agent; er ruft getypte Core-Aktionen inline auf. Dafür keinen weiteren
LLM-Launcher, zweiten Bot, Scheduler oder Dienst starten.

Vor Ausführung den vorhandenen Installations-/Betriebs-Skill und den installierten
Paketprompt `githubbot_core/prompts/MULTI-BOT.de.md` lesen; englisch entsprechend
`MULTI-BOT.en.md`, sofern installiert. Fehlende lokale Bindung oder Capability
sichtbar als nicht verfügbar melden. Ein Rollenname ist kein Startbeleg.

Der geprüfte Interpreter der bestehenden Installation ist hier als `<core-python>`
bezeichnet; nicht ungeprüft den gleichnamigen Befehl auf PATH verwenden.

```text
<core-python> -X utf8 -m githubbot_core --help
<core-python> -X utf8 -m githubbot_core --json multi-bot-role --mode operator
<core-python> -X utf8 -m githubbot_core [--config <layer> ...] act <kind> --params <json>
```

Die ersten beiden Aufrufe beschreiben die verfügbare Schnittstelle/Rolle und
starten keinen weiteren Provider. `act` erzeugt zunächst eine Vorschau. Nur bei
vorliegender Berechtigung wird derselbe Plan über `--apply --expect-plan <sha>`
mit dem bereits konfigurierten kanonischen Receiptweg ausgeführt. Konfigurations-
schichten müssen in derselben Reihenfolge bleiben. Der optionale vorhandene
`multi-bot`-Fensterstarter ist ein separater Benutzerstartweg; er ist für den
hier beschriebenen nativen Inline-Companion nicht zusätzlich erforderlich.

## Ausführungs- und Ergebnisvertrag

- Der Nutzerauftrag begrenzt Repositories und Tätigkeiten. Konto, Credentials,
  Rechte, Locks, Reviews und CI werden frisch über die bestehenden zuständigen
  Verträge geprüft; Rollenwahl und Modellname erweitern keine Rechte.
- Reviews und Mutationen binden an den tatsächlichen aktuellen Head. Ein eigener
  Codeautor ersetzt keine unabhängige Freigabe. Fehlende Fähigkeiten, unbekannte
  Identität, Schutzkonflikte oder abweichende Pläne bleiben HOLD im betroffenen Scope.
- Ergebnisübergabe nennt Ticket/Scope, tatsächlich verwendeten Runner/Modellweg
  soweit belegt, betroffene URLs und SHAs, ausgeführte Core-Aktionsarten sowie
  bestehende Plan-/Receipt-/Readbackreferenzen. Keine neue Receipt-Registry anlegen.
- Vorbereitung, Workerstart, Vorschau, Ausführungsversuch und unabhängig
  bestätigtes Remoteergebnis getrennt berichten. Unbekannte Zähler bleiben unbekannt.
  Wiederholung nach Timeout erst nach Prüfung bereits vorhandener Wirkung.
- Der Root nimmt das konkrete Ergebnis ab und pflegt Ticket/Session weiter.
  Dieser Skill bucht weder automatisch Merge, Versand noch Ticketabschluss.

## Wiederfinden

Der mitgelieferte TICKET-MASTER-Prompt verweist direkt auf diesen Skill; Wheels
liefern ihn unter `share/ticket-master/skills/githubbot-companion/`. Betreiber
können ihn zusätzlich als `file`-Quelle in den vorhandenen `capabilities`-Teil
ihrer privaten Knowledge-Konfiguration aufnehmen. Die öffentliche Source
behauptet keine Aktivierung einer privaten Instanz oder eines anderen Hosts.

Quelle dieses Integrationsauftrags: T-20261027-816938660. Konkrete Instanzdaten
und tatsächliche Repositoryaktionen gehören in deren bestehende private Verträge.
