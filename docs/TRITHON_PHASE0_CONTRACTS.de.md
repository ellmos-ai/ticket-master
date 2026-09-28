# Trithon und Muschelgrund: Phase-0-Verträge

Stand: 2026-09-16
Ticket: `T-20260916-735219043`
Status: Vertragsbasis; keine Laufzeit, kein Hostdienst, kein Deployment

## Ergebnis

Phase 0 definiert vier geschlossene JSON-Schema-Verträge:

| Grenze | Vertragskennung | Autorität |
|---|---|---|
| ticket-master → Trithon | `ellmos.trithon.task-projection.v1` | ticket-master bleibt Ticket- und Lifecycle-Kanon; Trithon erhält nur eine abgeleitete Projektion. |
| Trithon → agents-heart | `ellmos.trithon.agents-heart-dispatch.v1` | agents-heart entscheidet fail-closed über Rolle, Rechte und Budget. |
| Trithon → ticket-master | `ellmos.trithon.outcome-receipt.v1` | Das Receipt ist nur ein Vorschlag; ausschließlich ticket-master darf Ticketstatus und Lifecycle ändern. |
| angenommener Abschluss → Muschelgrund | `ellmos.muschelgrund.fact-projection.v1` | Muschelgrund erhält nur kuratierte Fakten, Lessons oder Working Notes; es wird kein Taskkanon. |

Alle Objektformen sind mit `additionalProperties: false` geschlossen. Die
Fixtures sind vollständig synthetisch und enthalten weder produktive Tickets
noch lokale Pfade, Prompts, Tooltranskripte oder Zugangsdaten.

## Gemessene Ausgangsverträge

### `route-intent.v1`

`lib/routing_contract.py::build_route_intent()` erzeugt genau diese fünf
Top-Level-Felder:

- `route_intent`;
- `ticket_id`;
- `target_snapshot`;
- `receipt_to`;
- `idempotency_key`.

Die synthetische Fixture `fixtures/route-intent.source.json` wird im Test direkt
mit der aktuellen Funktion erzeugt und verglichen. Die maschinenlesbare Datei
`route-intent-to-task-projection.v1.mapping.json` weist jedes Quellblatt genau
einem Zielfeld zu. Damit bleibt kein Quellfeld stillschweigend unberücksichtigt.

`route-intent.v1` enthält absichtlich weder Ticketrevision noch Priorität oder
Fähigkeit. Diese Werte werden deshalb nicht vorgetäuscht:

- `ticket_revision` ist der SHA-256-Wert der exakt gelesenen Ticketbytes;
- `source_status` muss am selben Compare-and-swap-Checkpoint `ACTIONABLE` sein;
- `capability` kommt ausschließlich aus allowlist-basierten Metadaten;
- `priority` wird aus dem Ticket-Master-Vokabular normalisiert;
- der Ticketvolltext wird nicht übertragen.

### agents-heart

Der vermessene BACH-HERZ-01-Seam liegt im lokalen Quell-Snapshot `9d4df01` in
`system/hub/_services/agents_heart.py`. Er kennt bereits:

- `role_id`, Rollenrevision und die Rechte `task.claim`/`task.execute`;
- `mode`, `agent_instance_id`, `backend_id`, `model_id`, `slot_id`, `task_id`,
  `session_id` und `initiated_by`;
- korrelierte Start-/Endereignisse mit `assignment_id`.

Der Snapshot war am 2026-09-16 nicht Bestandteil von BACH `origin/main`. Das
ist ein Integrationsgate und kein Implementierungsnachweis. Die genaue
Zuordnung steht in `agents-heart-v1.field-map.json`.

Für den späteren Phase-3-Adapter sind drei Erweiterungen ausdrücklich nötig:

1. die vorab idempotent reservierte `assignment_id` übernehmen oder im
   Zulassungsreceipt exakt darauf abbilden;
2. Fähigkeit und Budget vor dem Start prüfen;
3. den Dispatch-Idempotenzschlüssel als Replay-Gate speichern.

Bis diese Punkte implementiert und getestet sind, ist der bestehende Seam nicht
als konform mit dem neuen Dispatchvertrag zu bezeichnen.

## Feldabbildung `route-intent.v1` → Taskprojektion

| Quelle | Ziel | Regel |
|---|---|---|
| `/route_intent` | `/source/route_intent` | unverändert |
| `/ticket_id` | `/source/ticket_id` | unverändert |
| `/target_snapshot/kind` | `/target_snapshot/kind` | unverändert |
| `/target_snapshot/systems` | `/target_snapshot/systems` | unverändert, eindeutig |
| `/target_snapshot/at` | `/target_snapshot/at` | unverändert |
| `/target_snapshot/source` | `/target_snapshot/source_ref` | unverändert, aber nur als bereits pfadfreie Registry-Referenz ohne `/`, `\` oder `..` zulässig; sonst fail-closed |
| `/target_snapshot/fingerprint` | `/target_snapshot/fingerprint` | unverändert |
| `/receipt_to` | `/receipt_to` | unverändert |
| `/idempotency_key` | `/source/route_intent_idempotency_key` | unverändert |

Die Taskprojektion akzeptiert nur `ACTIONABLE` und startet im abgeleiteten
Zustand `ready`. Dieser Zustand ändert keinen Ticketstatus. Der Consumer führt
dieselbe maschinenlesbare Mappingtabelle dynamisch aus: Route-Intent-Schlüssel,
Ziel-Snapshot und alle übrigen Quellfelder müssen mit der Taskprojektion
übereinstimmen; gleiche Ticket-IDs allein genügen nicht.

## Idempotenz- und Replayregeln

Alle Hashes verwenden kanonisches JSON: UTF-8, sortierte Schlüssel und keine
Leerzeichen zwischen Trennzeichen. Bei jedem der vier Verträge wird der
Idempotenzschlüssel aus sämtlichen stabilen Top-Level-Feldern gebildet. Nur
`delivery` und `idempotency_key` selbst sind ausgeschlossen. Damit verändern
auch Rechte, Budget, Zielbindung, Outcome-Belege oder redaktionelle Metadaten
den Schlüssel und können nicht unbemerkt unter derselben Identität wechseln.

Ein Retry behält Projektions-/Receipt-ID und Idempotenzschlüssel. Nur
`delivery.event_id`, `attempt`, `sequence` und `emitted_at` ändern sich. Ein
Consumer berechnet vor jeder Deduplizierung den Schlüssel erneut und führt eine
Zuordnung `idempotency_key → stable_digest`. Er nimmt den ersten gültigen
Schlüssel an und behandelt nur inhaltsgleiche Wiederholungen als Duplikat. Ein
alter Schlüssel mit verändertem stabilem Inhalt ist ein Konflikt und muss
fail-closed enden.

Publisher-Epochen werden nicht allein durch syntaktische Gültigkeit
vertrauenswürdig. Der Consumer führt für jeden registrierten `publisher_id`
einen Checkpoint aus erwarteter Epoche und letzter Sequenz. Fremder Publisher,
abweichende Epoche und nicht monoton steigende Sequenz werden abgewiesen;
Zeitstempel ersetzen diese Prüfung nicht.

Ticket-ID, Ticketrevision, Receipt-Ziel, Projektions-ID und Task-ID bleiben von
der Taskprojektion bis zum Outcome gebunden. Dispatch-ID und Assignment-ID
werden vom Dispatch in das Outcome übernommen. Das Outcome-Receipt wird
schließlich zusammen mit allen vorigen IDs in der Muschelgrund-Projektion
referenziert. Gleiche Digests allein reichen nicht als Kettennachweis.

## Datenschutz-Allowlist

Die Verträge erlauben nur:

- opaque Ticket-, Task-, Assignment-, Receipt- und Evidenzreferenzen;
- SHA-256-Digests;
- kontrollierte Status-, Rollen-, Rechte-, Fähigkeits- und Budgetfelder;
- pfadfreie Publisher-/Epoch-/Sequenzprovenienz;
- für Muschelgrund genau eine redaktionell angenommene, geprüfte Aussage mit
  Betreff, Sprache und Datenschutzklasse.

Ausdrücklich unzulässig sind Ticketvolltext, Rohprompt, Tooltranskript,
Credential, Secret, Passwort, Bearer-Token, private Schlüssel und lokale
Dateipfade mit Windows- oder POSIX-Schreibweise. Der Datenschutztest durchsucht alle Fixtures nach verbotenen
Feldnamen und typischen Geheimnis-/Pfadmustern. Die geschlossenen Schemas weisen
zusätzliche Felder ebenfalls ab.

Die Muschelgrund-Projektion trägt außerdem ein geschlossenes
`privacy_gate`-Receipt mit Policy, Scannerrevision, Digest des gesamten
`entry`-Objekts, Entscheidung und Findings-Anzahl. Zusätzlich referenziert sie
ein kanonisches Kuratierungsreceipt. Der Consumer löst dieses aus einem
vertrauenswürdigen Kuratorenbestand auf, hasht es neu und prüft registrierten
Kurator, Quellart `accepted_outcome_evidence`, Transformation
`curated_summary`, Ticket-Master-Annahmedigest und Entry-Digest. Er berechnet
außerdem Statement-Digest und Allowlist-Prüfung selbst neu. Nur
`decision: allow`, null Findings und vollständige Übereinstimmung lassen die
Projektion passieren; frei eingesetzter Rohtext kann sich nicht selbst als
kuratierte Aussage deklarieren.

Die Ticket-Master-Annahme wird ebenfalls nicht von Trithon selbst beglaubigt.
Der Consumer löst `ticket_master_acceptance_ref` gegen den kanonischen
Ticket-Master-Receiptbestand auf, berechnet dessen Digest neu, prüft den
registrierten Publisher und vergleicht Ticket, Revision, Receipt-Ziel, Task,
Projektion, Dispatch, Assignment, Outcome und angewandten Status Feld für Feld.

## Gefahrenmodell und negative Vertragsfälle

| Gefahr | Fail-closed-Regel | Testnachweis |
|---|---|---|
| Rechteausweitung | Der Dispatch erlaubt genau `task.claim` und `task.execute`; zusätzliche Rechte sind ungültig. | zusätzliche `filesystem.write`-Berechtigung wird abgewiesen |
| falsche Rollenrevision | Nur der vermessene Vertrag `hintergrund_worker/v1` ist zulässig. | Revision `v2` wird abgewiesen |
| Budgetausweitung | Harte Obergrenzen für Laufzeit, Versuche und Ausgabemenge. | Überschreitung wird abgewiesen |
| Replay/Doppelreceipt | Schlüssel vor Deduplizierung neu berechnen und stabile Inhalte hinterlegen. | erster Eingang `accepted`, identischer Retry `duplicate`, veränderter Payload `conflict` |
| veraltete oder fremde Kette | Ticket, Revision, Receipt-Ziel, Task, Projektion, Dispatch, Assignment und Outcome-Receipt bleiben gebunden. | jede einzelne abweichende Referenz wird abgewiesen |
| Publisher-Replay | Checkpoint je Publisher aus Epoche und monotoner Sequenz | fremder Publisher, fremde Epoche und alte Sequenz werden abgewiesen |
| Lifecycle-Übergriff | Outcome enthält nur `proposed_ticket_status`; kein Schreibpfad oder angewandter Status. | injizierter Ticketpfad wird abgewiesen |
| unzulässige Memory-Projektion | nur kanonisch aufgelöster, von einem registrierten Ticket-Master angenommener Abschluss mit `completed`/`SOLVED` | gefälschter Publisher, Digest oder Bindungswert wird abgewiesen |
| Datenabfluss | geschlossene Allowlist, kanonisch aufgelöstes Kuratierungsreceipt und erneute Consumer-Prüfung | Volltext-, Prompt-, Transcript-, Secret- und Windows-/POSIX-Pfadfelder werden abgewiesen; Rohtext ohne vertrauenswürdige Kuratierungsbindung scheitert |
| Wissens-Retry blockiert Taskabschluss | eigener Delivery-Versuch bei stabiler Abschlussreferenz | Muschelgrund-Retry bleibt vom Outcome getrennt |

## Integrationsgates für Phase 1 bis 4

- Phase 1 darf die Schemas nur konsumieren; sie darf keinen neuen Ticketkanon
  und keinen direkten Statusschreiber einführen.
- Phase 3 muss den agents-heart-Adapter gegen den tatsächlich integrierten
  BACH-Stand testen. Der lokale Snapshot allein genügt nicht.
- Vor jedem Start bleiben lock-master und lokale User-Locks absolut. Kein
  Dispatch, Budget, Zeitstempel oder Epochensignal überstimmt einen Lock.
- Erst ein von ticket-master angenommener Abschluss darf die
  Muschelgrund-Projektion erzeugen. Die Projektion muss sowohl das vorgeschlagene
  Outcome-Receipt als auch Referenz und Digest des getrennten
  Ticket-Master-Annahmereceipts tragen. Der Consumer löst diese Referenz aus dem
  Ticket-Master-Kanon auf und prüft Digest, Publisher und sämtliche
  Kettenbindungen erneut; eine bloße Trithon-Behauptung genügt nicht.
- Ausfall oder Retry von Muschelgrund rollt weder Task noch Ticketabschluss
  zurück.
- Föderation, Lead-Wahl, Leases, Fencing und Salt sind nicht Teil dieser Phase.

Damit sind die Nutzer- und Policy-Entscheidungen für Phase 0 geschlossen. Offen
bleiben ausschließlich technische Integrationsreceipts der späteren Phasen.
