# Messages-Line: Message-Tickets und Log-Quellen

Stand: lokaler Kernvertrag. Produktive GUI-, BACH-/Ocean- und Transport-
Abnahmen stehen noch aus.

## Message-Tickets und IDs

`lib/messages_line.py` verwaltet eine append-only Messages-Line mit einem
JSON-Datensatz je Nachricht. Die Datensätze enthalten eine stabile
`MessageID` (`M-YYYYMMDD-#########`), eine `ThreadID`
(`TH-YYYYMMDD-#########`), eine kanonische `ReceiptID`
(`R-YYYYMMDD-#########`), Herkunfts-, Empfänger- und Zielreferenzen sowie den
Text der Nachricht. So kann eine lokale Oberfläche den Inhalt über die
`MessageID` tatsächlich auflösen und anzeigen. Der Text ist Nachrichteninhalt,
kein Protokolleintrag; Audio und Chattranskripte werden nicht gespeichert oder
mitgeschrieben. Ein Thread gruppiert über die gemeinsame `ThreadID`; Quittungen
gelten trotzdem jeweils nur für eine einzelne `MessageID`.

Die neunstellige Zufallskomponente wird über `ticket_writer.draw_id()` geprägt
und verwendet damit denselben ID-Vertrag wie Tickets. Exklusive lokale
Dateianlage, `_threads/`-Reservierung und ein exklusiv angelegter
ReceiptID-Ordner erkennen Kollisionen am selben Speicherort und ziehen eine
andere Nummer. Die MessageID, ReceiptID und ThreadID bleiben bei Kanal- und
Hostwechseln gleich, weil sie im Message-Ticket stehen. Getrennte Hosts ohne
aktuellen Sync-Abgleich erhalten keine mathematische globale
Eindeutigkeitsgarantie; Exactly-Once-Transport wird nicht behauptet.

`MessagesLine` benötigt einen expliziten lokalen Speicherpfad. Es gibt keine
zweite `INBOX`-/`OUTBOX`-Queue, keine Zustell-Retry-Schleife und keine eigene
Transportdatenbank. Die Message-Line enthält den Text für die spätere
Darstellung. `_receipts/<ReceiptID>/ledger.json` bindet die ReceiptID dauerhaft
an genau eine MessageID; `observations/` hält belegte Ereignisse einzelner
Kanäle. Das ist die kanonische Zustellquittung für eine Message-Line-Nachricht,
kein zweites Transport-Receipt-Ledger.

## Zustellung und Quittungen

`MessageLine.get()`, `by_thread()` und `list_messages()` sind administrative
Lesezugriffe; sie erzeugen keine Zustellquittung. `list_for_delivery()` und
`read_for_delivery()` liefern Text nur, wenn die aktuelle Konfiguration,
der Zustellmodus und der gespeicherte Ziel-Snapshot die Route weiterhin
erlauben. `acknowledge()` legt eine Quittungsbeobachtung im Receipt-Ledger ab;
`delivery_state()` liest sie nach einem Neustart wieder ein.
`message_delivery_state()` unterscheidet `prepared`, `offered`, `transported`,
`fetched`, `listed`, `previewed`, `gui_opened` und `delivered`. Angebot,
Transport, Abruf, Listenansicht und Vorschau zählen niemals als Zustellung.
`explicit_ack`, `on_demand_ack` und `active_chat_ack` zählen mit passendem
Empfänger, Ziel und Evidenzreferenz als Zustellung. On-demand- und
Active-Chat-Quittungen benötigen außerdem den passenden Nachrichtentyp und
aktuell aktivierten Modus. Ein tatsächliches GUI-Öffnen zählt nur, wenn die
Regel beim Erstellen, zum Ereigniszeitpunkt und in der aktuellen Konfiguration
aktiv ist.

Die Message-Line prägt pro Nachricht genau eine kanonische ReceiptID. Alle
Kanäle quittieren dieselbe ReceiptID; jede einzelne Ereignisbeobachtung erhält
eine deterministische `observation_id`. Das Wiederholen derselben Beobachtung
ist idempotent; dieselbe ObservationID mit abweichenden Feldern wird
fail-closed abgelehnt. Jeder neue automatisierte Receipt-Einstieg benötigt
die aktuelle Konfiguration; ein globaler Aus-Schalter, deaktivierter Modus
oder entferntes Ziel sperrt weitere automatische Zustellung auch für ältere
Nachrichten, auch wenn ein Aufrufer das Ereignis zurückdatiert. Bereits lokal
gespeicherte historische Quittungen bleiben auditierbar. Ein neuer historischer
Import ist bis zu einem vertrauenswürdigen Herkunfts- und Evidenzprüfer
gesperrt. Nur `acknowledge()` darf eine bewusste manuelle `explicit_ack`
erfassen, auch wenn die Automatik ausgeschaltet ist; dadurch wird keine Route
reaktiviert. `.SYNC` bleibt
Transportfläche und system-gap-master bleibt zuständig für die
systemübergreifende Reconciliation. Der Adapter, der die Message-ReceiptID an
diesen vorhandenen Cross-System-Vertrag bindet, ist noch offene
Integrationsarbeit.

## Konfiguration und Protokollregister

`config/messages-line.example.json` startet mit deaktivierter Funktion, drei
deaktivierten Zustellmodi (`chat_start`, `on_demand`, `active_chat`) und ohne
Ziele. GUI-Öffnen zählt standardmäßig als Zustellung, wenn die Funktion, der
Zustellmodus, das Ziel und die beim Erstellen gespeicherte Regel aktiv sind;
`gui_open_counts_as_delivery` lässt sich dafür abschalten. `updated_at` muss
bei jeder Konfigurationsänderung auf den Änderungszeitpunkt gesetzt werden.
Nachrichtentext wird in der
Message-Ticket-Datei gespeichert, aber
`message_content_logging` und `audio_logging` bleiben fest ausgeschaltet.
Vorhandene Registry-Referenzen können mit einer Oberfläche
(`ticket_master`, `local_gui`, `server_gui` oder `registered_server`)
konfiguriert werden. Das Feld erfindet weder Systemrollen noch Endpunkte.
`transport_contract_ref` zeigt auf das bestehende
`ellmos.ticket.route-intent.v1`.

Die Inhalts- und Audio-Protokollierung ist fest ausgeschaltet; der Validator
lehnt ein Einschalten ab. `config/log-registry.example.json` ist ein leeres
Quellenregister. Ein Eintrag referenziert Quelle, Eigentümer, Zeitfenster
(`24h`, `monthly` oder `other`), Zugriffs-/Redaktionsregeln, Provenienz und
Aufbewahrung. `materialization` ist auf `reference_only` festgelegt. Rohlogs
werden nicht kopiert. Eigenständige Policy-, Plans- oder Decisions-Register
werden nicht erzeugt, weil sie bereits einer anderen kanonischen Quelle
gehören.

## Lokale CLI

Der vorhandene `ticket-master`-Dispatcher macht den lokalen Kern über
`--messages-line {create,list,read,ack,config,registry}` zugänglich. Für
`create`, `list`, `read` und `ack` muss mit `--messages-dir` ein lokaler
Speicherpfad ausdrücklich gewählt werden. `--messages-config` und
`--log-registry` lesen jeweils eine JSON-Datei; ohne Pfad wird das paketierte
Beispiel verwendet. Konfiguration und Register werden vor der Ausgabe mit
denselben Python-Validatoren wie die Bibliotheks-API geprüft.

In den folgenden Beispielen ist `M-YYYYMMDD-#########` ein Platzhalter für
eine zuvor von `create` ausgegebene MessageID. Beim manuellen Ack müssen Ziel
und Oberfläche zum gespeicherten Ziel-Snapshot der Nachricht passen.

```text
ticket-master --messages-line config
ticket-master --messages-line registry --log-registry ./log-registry.json
ticket-master --messages-line create --messages-dir ./private/messages --origin-ref automation:build --recipient-ref principal:self --content-text "Künstliche Beispielnachricht" --delivery-mode on_demand --messages-config ./messages-line.json
ticket-master --messages-line list --messages-dir ./private/messages --json
ticket-master --messages-line read --messages-dir ./private/messages --message-id "M-YYYYMMDD-#########"
ticket-master --messages-line ack --messages-dir ./private/messages --message-id "M-YYYYMMDD-#########" --target-ref gui:local --surface local_gui --channel-ref client:local --actor-ref principal:self --evidence-ref ui:explicit-action
```

`create` speichert nur ein Message-Ticket. `list` und `read` sind administrative
Lesezugriffe und quittieren nichts. Nur der ausdrücklich aufgerufene `ack`-
Befehl ruft `MessagesLine.acknowledge()` für eine manuelle Nutzerbestätigung
auf. Der CLI-Zugang sendet keine Nachricht, startet keine Transportqueue und
lädt keine Logs. `registry` validiert und zeigt nur Quellenreferenzen an.

## Integrations- und Abnahmegrenzen

Dieses Repository liefert ID-Vergabe über `ticket_writer.draw_id()`,
Thread-Gruppierung, abrufbaren Nachrichtentext, Live-Konfigurations-Gates an
den lokalen Zustell-Lese- und Quittungseinstiegen, ein persistentes lokales
Receipt-Ledger, Quittungsprojektion und geschlossene JSON-Verträge. Es sendet
keine Nachricht und fragt weder eine GUI noch BACH/Ocean ab. Für einen
produktiven Anschluss müssen Integrationen den Message-Ticket-Ziel-Snapshot an
den bestehenden `route_intent`-/`.SYNC`-Transport binden und überall dieselbe
ReceiptID verwenden. GUI-Clients müssen Öffnen und explizite Bestätigung
getrennt quittieren. BACH/Ocean-Zuordnung, Serveranzeige, Authentifizierung,
Live-Logs und geräteübergreifende Laufzeitabnahme bleiben separate
Folgearbeiten. Ein Import historischer Receipts benötigt eine geprüfte
unveränderliche Quittung mit belegter Herkunft und MessageID/ReceiptID-Bindung;
bis dieser Reconciliation-Verifier existiert, bleibt der Importpfad gesperrt.
Polarstern, Trithon und Muschelgrund erhalten hier keine voreilige Rollen-
oder Endpunktzuweisung.
