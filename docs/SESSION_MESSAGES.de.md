# Lokale Ergebnis- und Meldeanzeige

Die Anzeige ist eine Sessionprojektion. Der bestehende `lib/messages_line.py`-Owner besitzt weiter Nachrichtentext, M-/TH-/R-IDs und Quittungen. Die Anbindung startet keine Remotezustellung und benötigt weder BACH/Ocean noch einen Server. Remote-Empfänger-Ack und verifizierter Payloadresolver bleiben Aufgaben des jeweiligen Transportowners.

## Explizite Ownerinstallation

Ein Betreiber wählt einen unabhängig geprüften, unveränderten Commit der vorhandenen Messages-Line-Quelle. Aus dessen Gitobjekten den vollständigen getrackten `lib/`-Ordner in einen eigenen freigegebenen lokalen Ordner kopieren; keinen Arbeitsbaum oder fremde Änderungen kopieren. `MANIFEST.json` enthält `source_commit` und ein `files`-Objekt aus relativem POSIX-Pfad zu SHA-256 jeder Datei, beispielsweise `{"source_commit":"<approved-head>","files":{"lib/__init__.py":"<sha256>","lib/messages_line.py":"<sha256>","…":"…"}}`. Das Beispiel ist nur eine Formskizze; **alle** getrackten Paketdateien müssen enthalten sein. Den SHA-256 der exakten UTF-8-Manifestbytes unabhängig festhalten.

Der Viewer prüft Manifesthash, alle Datei-Hashes und das Fehlen unmanifestierter Paketdateien vor jedem Ownerzugriff. Er lädt das verifizierte Paket unter einem isolierten Modulnamen und schreibt keinen Bytecode in die Kopie. Der Manifesthash ist ein expliziter Vertrauensanker aus dem Startaufruf, keine Selbstauskunft aus dem Feed. Die geprüfte Kopie enthält Code und ist deshalb wie eine lokale Softwareinstallation zu behandeln. Ownerupdates benötigen eine neue geprüfte Kopie und einen neuen Startaufruf.

Den **bestehenden** Messages-Line-Store und dessen Config angeben. Für einen neuen eigenständigen lokalen Store die gültige `ellmos.ticket.messages-line-config.v1` aus der Ownerquelle verwenden: nur `on_demand` aktivieren, genau das gewünschte `local_gui`-Ziel registrieren; keine Server-/Mail-/Chatroute hinzufügen. Die unten genannten Namen sind frei wählbare Beispiele. `gui_open_counts_as_delivery` kann true sein; die Anzeige selbst bleibt standardmäßig manuell.

```text
ticket-master-results open /absolute/session.json
  --messages-core-dir /approved/messages-owner
  --messages-core-manifest-sha256 <approved-manifest-sha256>
  --messages-dir /canonical/local-messages
  --messages-config /canonical/messages-config.json
  --messages-target local-results
  --messages-recipient local-user
  --messages-actor local-user
```

Das ist ein einzelner CLI-Aufruf; Zeilenumbrüche dienen hier nur der Lesbarkeit. Dieselben sieben Optionen gelten für `observe`. Fehlt eine Teilangabe, wird die Anbindung abgelehnt. Ohne jegliche Owneroption ist die Ergebnisanzeige weiterhin nutzbar; Lesestatus bleibt ausdrücklich unbekannt. Die `.bat`-/`.sh`-Starter geben zusätzliche explizite Optionen an die CLI weiter. Keine dieser Optionen darf aus einer Meldung oder deren Artefakten stammen.

## Erfassung und Quittung

`observe --input observed-notice.json --revision R --sha256 H` erfasst tatsächlich wahrgenommene Meldungen. Pflichtfelder: `source`, `external_id`, `title`, `text`, `observed_at` mit Zeitzone; optional `kind=message|system`, Tags und bestehende `thread_id`. Quelle und externe ID bilden einen stabilen `observed:<sha256>`-Origin im vorhandenen Ownerarchiv. Der Owner speichert den Beobachtungsumschlag als normalen Messageinhalt. Ein temporärer exklusiver `.observed-notice-write-lock` serialisiert kooperierende Lookup/Create-Aufrufe; liegengebliebene Sperren nie ungeprüft löschen. Andere Schreiber dürfen diese Idempotenz nicht durch eigene Nachbauten umgehen. Das ist kein zusätzlicher dauerhafter Index oder Quittungsspeicher.

`observe --message-id M-…` ordnet eine bereits existente Nachricht zu. Pro Session gibt es höchstens eine Meldungsprojektion derselben kanonischen M-ID. Andere Sessions dürfen dieselbe M-ID referenzieren; ihr Readstatus stammt dann weiterhin aus derselben kanonischen R-ID. Bei Fehler nach Messageanlage und vor Session-CAS kann ein kanonisches, noch nicht projiziertes Objekt bestehen bleiben: dieselbe Beobachtung nach frischem Sessionread wiederholen. Die erste Beobachtungszeit bleibt erhalten. Keine Neuanlage unter zufällig geänderter externer ID.

`observe --event-id <stabile-Sessionevent-ID>` bindet eine bereits angezeigte ungebundene Nachricht/Systemmeldung an denselben Owner. Die bisherige Log-ID, sonst Event-ID dient als externe ID. Titel, Zusammenfassung, Volltext, fachlicher Status und Artefakte bleiben erhalten; die Karte erhält M-/TH-/R-Bindung unter ihrer bisherigen Event-ID. Analyse-/Ergebniskarten werden nicht zu Nachrichten umgewandelt. Bereits gebundene Meldungen werden idempotent wiedergefunden. Diese Migration ermöglicht das kanonische Gelesenmarkieren von Altbeobachtungen ohne Kartenverdoppelung.

Die GUI liest periodisch ausschließlich die im Sessionfeed referenzierten M-IDs. Sie zeigt Quelle, Zeit, ursprüngliche externe ID, Thread und kanonische Zustellung an. Eine unbekannte oder veränderte Inhaltsbindung sperrt Quittierungen. Vor jeder Aktion werden die aktuelle Sessionbindung, kanonischer Nachrichtentext/Hash, Empfänger, ursprüngliches Ziel und aktuelle Ownerconfig erneut geprüft. Die sichtbare Textprojektion muss zum Ownerinhalt passen. `acknowledge()` ist ausschließlich der bewusste Nutzerklick; Erfolg erfordert die exakte gespeicherte Observation und `delivery_state=delivered` im Readback. Asynchrone alte Statusantworten dürfen eine neu bestätigte Quittung nicht zurücksetzen.

`on_open` wird im Zahnrad oder mit `message-settings --read-mode on_open` aktiviert. Erst bewusstes Öffnen des Meldungsvolltexts ruft `record_observation(event=gui_opened)` auf. Ownerconfig und Nachrichtensnapshot müssen es beide erlauben. Ein abgelehntes Öffnungs-Ack bleibt unbekannt/unquittiert; kein automatischer Ersatz durch `explicit_ack`. Eine Listenansicht, ein Refresh, Ergebnisakzeptanz oder eine Freigabe wird daraus nie abgeleitet. Der Ownervertrag bezeichnet die kanonische Quittung als `delivered`; das ist keine Garantie, dass ein Mensch jedes Zeichen gelesen hat.

## Dokumentlinks

Lokale erlaubte Dokumente werden bewusst geöffnet, andere Dateitypen ausschließlich als Pfad kopiert. Echte externe Artefakte sind ausschließlich HTTPS mit Host, Standardport und ohne Zugangsdaten/Steuerzeichen/Backslash. Nur der Nutzerklick übergibt einen validierten Link an den normalen Browser; keine Browserautomation, Inhaltsabfrage, Shellbefehle oder Remotehashbestätigung. Frühere Versionen vor dieser Meldeerweiterung besaßen lediglich ein URL-Typsymbol; sie unterstützten noch keine externen URL-Artefakte.

## Prüfung

`TM_NATIVE_UI_TEST=1` aktiviert die nativen Tempfenster-Smokes. `TM_MESSAGES_CORE_TEST_DIR` und `TM_MESSAGES_CORE_TEST_SHA256` aktivieren zusätzlich die unveränderte echte Ownerintegration; Tests schreiben ausschließlich in temporäre Stores/Configs. Ohne diese bewusst gewählten Parameter sind Ownerprüfungen ausdrücklich übersprungen, nicht bestanden. Der echte Nutzerfeed und seine Zwischenablage sind keine Testfixtures.
