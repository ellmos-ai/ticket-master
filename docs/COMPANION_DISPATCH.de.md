# Begleitrolle: gebundener SDK-Start

Dieser optionale Bedienpfad startet **ein** inneres TASKPLAN-Child einer bereits
laufenden TICKET-MASTER-Sitzung. Der äußere GUI-/TASKPLAN-Rollenstart bleibt
unverändert. `lib/session_boot.py` verwendet die vorhandene Claim-Rotation und
den vorhandenen nativen Registry-/PID-/Geburtszeitprüfer. Der neue Adapter
erkennt freie native Chat-Slots **nicht selbst**: Der steuernde Agent erhebt und
bezeugt den aktuellen Management-, Aufgaben-, Lock- und Quotenstand.

## Voraussetzungen

- Persönliches Profil `ticket-master.session-profile.v1` ist ausdrücklich
  `runtime_compatibility: active` und `companion.enabled: true`. Bei `pending`
  meldet der Befehl `disabled` und startet nichts.
- Im installierten TASKPLAN ist für die beanspruchte Rolle unter
  `[execution] provider` und `[providers.<provider>.models]` ein konkretes
  Modell konfiguriert; `[providers.<provider>.reasoning_effort]` liefert den
  Aufwand. Leere Codex-Defaults werden nicht geraten. Der Rollenprompt kommt
  ausschließlich über `taskplan.workflows.get_workflow_prompt_path` aus dem
  installierten Paket.
- AgentLauncher samt seinen Pflichtabhängigkeiten ist in **demselben**
  Interpreter installiert. In `ticket-master.config.json` zeigt
  `agent_launcher_work_root` vor dem Claim auf ein vorhandenes privates,
  absolut adressiertes Arbeitsverzeichnis;
  `agent_launcher_registry_dir` ist optional (sonst Nutzerstandard). Die
  Paketinstallation und Profilaktivierung erfolgen getrennt nach Review.
- Der Steueragent hat einen konkreten TASKPLAN-Auftrag mit gültigen Task-,
  Lock- und Scope-Gates sowie einen tatsächlich freien niedrigen/mittleren
  Slot und positives Providerbudget frisch geprüft. `max_luna` ist nur eine
  persönliche Obergrenze, keine Slotmessung.

## Einmaliger Ablauf

1. `python -X utf8 lib/session_boot.py --claim-companion <SESSION_ID>` liefert
   `claim_id`, `role` und `dispatch_name`. Ein bereits offener Claim wird
   wiedergegeben. Vor einem weiteren Versuch ist dessen Zustand zu klären.
   Bei konfiguriertem SDK-Workroot liegen Claim und Attempt gemeinsam unter
   dessen kanonisch aufgelöstem `.ticket-master-companion/`: verschiedene
   Profilordner und zulässige Pfad-Aliasse desselben Workroots teilen damit
   Rotation, persistente Sicht und Reservation. Ein vor der Konfiguration
   angelegtes profilgebundenes Ledger muss zuerst geklärt werden; es wird
   nicht still in den gemeinsamen Bereich übernommen.
2. Der Steueragent schreibt einen **privaten** Admission-Beleg. Schema:
   `ticket-master.companion-admission.v1`; Felder `session_id`, `claim_id`,
   `role`, `provider`, `model`, `model_class: low-medium`, `issued_at`,
   `expires_at`, `management`, `agent_classes`, `slot`, `quota`, `task_gate`.
   Zeitstempel sind UTC und höchstens zwei Minuten gültig.
3. `management` enthält `source: collaboration.list_agents`, `checked_at`,
   das gekürzte tatsächliche Readback
   `{"agents":[{"agent_name":"...","agent_status":"running"}]}` und
   `sha256`. `agent_classes` klassifiziert **jeden** gelesenen Agenten als
   `low-medium`, `high` oder `other`; unbekannte Status werden abgelehnt.
   `slot.limit` ist eine positive Ganzzahl bis zur persönlichen Obergrenze.
   Eine Klassifikation ist eine ausdrückliche Steueragenten-Attestation, keine
   aus Namen erratene Modellklasse.
4. `quota` enthält den geprüften `provider`, `checked_at`, ein natives
   `readback` mit `status: OK`, eigenem `checked_at` und positiv gemessenem
   `remaining_percent` oder `windows.<fenster>.remaining_percent` sowie
   `sha256`. **Alle** gelieferten Werte desselben Providerbelegs sind
   wirksame Grenzen: der kleinste gemessene Rest ist maßgeblich; null,
   fehlende oder ungültige Fenster sperren den Start. `status: OK` allein
   belegt kein Budget. Ist ein solcher Readback für den Anbieter nicht erhältlich,
   bleibt der Start offen. Beide SHA-256-Werte sind die Hex-Digests von
   `json.dumps(readback, sort_keys=True, separators=(",", ":"),
   ensure_ascii=True).encode("utf-8")`.
5. `task_gate` enthält `status: ready`, die Claim-`role`, `locks: clear`,
   `scope: authorized`, eine konkrete `task_ref` und eine prüfbare `source`
   des aktuellen Selektor-/Aufgabenbelegs. Der Adapter wertet keine freien
   Befehle oder Promptpfade aus diesem Feld aus.
6. `python -X utf8 lib/session_boot.py --dispatch-companion <SESSION_ID>
   --admission-file <PRIVATE_JSON>` führt nach erneuter Prüfung genau einen
   SDK-Startversuch aus. Vor dem Aufruf wird ein exklusiver Attempt-Beleg
   im gemeinsamen Workroot-Ledger angelegt; dessen Dateilock serialisiert
   auch Starts aus unterschiedlichen Sitzungsprofilordnern. Unter demselben
   Lock werden vorhandene Claims und Attempts mit der AgentLauncher-Registry
   abgeglichen.
   Ein laufender eigener SDK-Child zählt zusätzlich zur nativen Chat-Belegung;
   ein unlesbarer Registryeintrag oder ungewisser Start lässt keinen freien
   Slot entstehen. Die tatsächlichen Management- und Quotenmesszeiten müssen
   nach früheren Startversuchen liegen; ein neues `issued_at` verjüngt keine
   alte Messung. Der SDK-Name ist ausschließlich
   der Claim-`dispatch_name`; die drei `ticket_master_*`-Metadaten werden
   unverändert übergeben. Erst `StartResult.ok` plus der **bestehende**
   `companion_ack`-Registryprüfer ergibt `accepted`.

`pending-reconcile` bedeutet: Ein Start könnte stattgefunden haben. Den
eigenen Attempt, Claim und AgentLauncher-Registry-/Prozessbeleg prüfen; **kein
zweiter Start desselben Claims**, kein fremder Prozesskill und kein blindes
Löschen. Bei eindeutig fehlgeschlagenem Vorstart kann die zuständige
Steuerung einen neuen, dokumentierten Wiederanlauf planen; die Software
entfernt einen unklaren Attempt nicht automatisch. Ein späterer Admission-
Beleg muss nach dem vorherigen Startversuch neu gemessen werden. Die
AgentLauncher-Registry wird separat im selben Reservationsbereich gelesen,
weil native Chat-Verwaltung SDK-Prozesse nicht zuverlässig enthält. Nur ein
identitätsgebunden laufender Child wird gezählt; eine nachweislich beendete
PID/Geburtszeitbindung gibt den Platz frei. Unklare Prozesszustände bleiben
gesperrt. Native Chat-Spawns ohne AgentLauncher-Registry werden hier nicht
als Annahme verbucht.

## Späterer isolierter Runtime-Kandidat

Nach unabhängigem Source-GO kann ein ausdrücklich beaufsichtigter Test ein
separates privates Profil mit `runtime_compatibility: active` nutzen. Bei
demselben kanonischen SDK-Workroot teilt es das Claim-/Attemptledger mit dem
persönlichen Profil; ein anderer Workroot belegt keinen gemeinsamen
Reservationsbereich und darf nicht als isolierender Freislot missverstanden
werden. `TM_SESSION_PROFILE` hat Vorrang vor
`session_profile_path` in der Konfiguration; der steuernde Agent muss diesen
Wert für den konkreten Prozess nachweisen. `TASKPLAN_CONFIG` wählt eine
isolierte TASKPLAN-Konfiguration mit explizitem Rollenmodell. Das persönliche
Profil bleibt währenddessen `pending`. Ein isoliertes SDK-Registryverzeichnis
darf keine vorhandene Produktivbelegung verbergen: die Nutzerstandard-Registry
wird zusätzlich geprüft, wenn sie vorhanden ist; bei anderen bekannten
Produktiv-Registryorten oder unbekannter Belegung hält die Steuerung den
Test an. Der aktuelle Code behauptet weder vollständige OS-Belegungssicht
noch Runtime-Wirksamkeit ohne echten Child-, Claim- und PID-/Geburtszeitbeleg.

Die Hashes binden die vom **berechtigten Steueragenten** gelieferten Readbacks
an den Beleg. Sie sind keine Signatur und verleihen einer erfundenen Datei
keine Autorität. Tests mit Fake-SDK und Fake-Readbacks belegen ausschließlich
Codeverhalten; ein Runtime-GO erfordert einen späteren echten, beaufsichtigten
Start in der installierten Zielumgebung.
