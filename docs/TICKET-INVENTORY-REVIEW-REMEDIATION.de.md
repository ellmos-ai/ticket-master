# Ticketinventur: Korrekturen nach unabhängigem Review

Stand: 26.10.2026. Auftrag: T-20261025-252629410.
Geprüfter Kandidat: `d00aec786a46f4c89a708b8cf5dbbbce7148500e`.
Status: **CHANGES_REQUIRED; Source, Integration und Installation offen.**
Dieser Plan dokumentiert Restarbeit und behauptet keine bereits erfolgte Korrektur.

## 1. Quellgrenze während des Lesens halten (P1)

`lib/ticket_inventory.py` prüft Pfade per `lstat`, verwendet danach jedoch
`os.listdir` und `Path.open`. Ein vorübergehend ausgetauschter Verzeichnislink
kann vor dem abschließenden Vergleich zurückgesetzt werden. Die Nachprüfung
beweist deshalb nicht, dass ausschließlich innerhalb der Queue gelesen wurde.

Die Erfassung muss Verzeichnisse und Dateien über stabil gehaltene Handles mit
No-follow-Semantik öffnen und die Traversierung an diese binden. Unter POSIX
eignen sich relative Zugriffe mit `dir_fd` und `O_NOFOLLOW`; Windows benötigt
eine gleichwertige Behandlung von Reparse Points und Verzeichnishandles.
Ein bloßer zusätzlicher `resolve`-/`lstat`-Vergleich oder eine Prüfung nach dem
Lesen beseitigt das Zeitfenster nicht. Ohne nachweisbar sicheren Mechanismus
muss die betroffene Plattform vor dem Inhaltszugriff kontrolliert abbrechen.
Die dokumentierte Unterstützung für Windows, macOS und Linux ist zu erfüllen.

Abnahme: adversarielle Fälle für einen Austausch vor Verzeichnisaufzählung,
vor Dateiöffnung und während der Traversierung, jeweils mit Wiederherstellung
vor der Nachprüfung. Externe Dateinamen und Inhalte dürfen nicht in den Bericht
gelangen; der Zugriff darf keine Bytes außerhalb der Queue lesen. Echte
Windows-Junctions und POSIX-Symlinks getrennt nachweisen; übersprungene Fälle
sind fehlende Plattformabnahme. Unveränderter normaler Bestand muss weiter
vollständig und bytegenau erfasst werden.

## 2. Queue-Alias-Konflikt kontrolliert melden (P2)

Der Inventurzweig in `bin/ticket_master.py` muss `QueueAliasError` aus
`lib.config_paths` ausdrücklich behandeln. Beide vorhandenen Queue-Aliase
dürfen weder willkürlich ausgewählt noch mit Traceback quittiert werden.
Abnahme: kontrollierte Fehlermeldung, Exitcode 2, keine Provider-Ausführung,
keine Änderung an einer Queue; bisherige Aliasauflösung bleibt erhalten.

## 3. Den dokumentierten CLI-Einstieg aus dem Wheel liefern (P2)

Das Wheel liefert derzeit `lib*` und den Skill als Daten aus, aber nicht
`bin/ticket_master.py`. Einen gemeinsamen paketierten CLI-Einstieg bereitstellen,
den Quellstarter und installierte CLI verwenden. Prompt-/Skilldaten auch aus
der installierten Distribution auflösen. Die Dokumentation muss einen aus
einem fremden Arbeitsverzeichnis tatsächlich nutzbaren Aufruf nennen.

Abnahme: Wheel bauen, Inhalt prüfen und in eine frische isolierte Umgebung
installieren. Ohne Quellcheckout auf Importpfad oder Arbeitsverzeichnis müssen
Hilfe und Inventur einer temporären Queue funktionieren. Provider-Aufrufe und
Schreibaktionen bleiben im Inventurmodus ausgeschlossen. Keine globale
Installation aus diesem Paketnachweis ableiten.

## 4. BACH und Ocean tatsächlich anbinden (P2)

Vor Umsetzung die aktuelle Ocean-Zusammensetzung, Modulzuordnung und vorhandenen
Adapterverträge lesen. Die gemeinsame Inventurlogik bleibt im Modul. Dünne
Adapter müssen sie in BACH und Ocean erreichen; Manifest und kanonische
CLI-Zuordnung müssen auf tatsächlich vorhandene Einstiege verweisen.
Ein gefülltes `adapters`-Feld allein ist kein Integrationsnachweis.

Abnahme: beide Hosteinstiege mit derselben temporären Queue aufrufen und ihre
Ergebnisse mit dem Moduleinstieg vergleichen; Fehler und No-follow-Verhalten
müssen durchgereicht werden. Bestehende Aufrufer und Modul-Pins prüfen.
Keine eigene SQL-Anbindung und keine zweite Inventurimplementierung bauen.

## 5. Historische `_logs` als Belege erfassen (P2)

`_logs` aus dem pauschalen Cacheausschluss entfernen. Historische Dateien als
ergänzende Quellen mit Hash, Abschnittsbezug und offenem Prüfstatus erfassen;
sie werden dadurch nicht zu aktiven Tickets. Die gezielte Behandlung von
SOLVED/DONE/Archiv und echten Betriebscaches bleibt ausdrücklich dokumentiert.
Abnahme: `_logs` auf oberster und verschachtelter Ebene mit vollständiger
Quellenabdeckung; keine neuen Tickets aus Logdateien und keine Logänderungen.

## Abschlussfolge

Die fünf Punkte einzeln umsetzen und angemessen prüfen; danach den gesamten
geänderten Kandidaten unabhängig reviewen. Source-GO, Wheel-GO, beide
Integrationsnachweise und die autorisierte lokale Installation sind getrennt
zu belegen. Erst danach Bericht und Ursprungsticket mit dem tatsächlichen
Ergebnis abschließen. Die bereits belegten Inventurmaßnahmen nicht erneut
ausführen. Die gewünschte Task-Leasing-Umstellung folgt anschließend und
benötigt zusätzlich den aktuellen Taskplan/BACH-API- und Datenzuordnungsvertrag.
