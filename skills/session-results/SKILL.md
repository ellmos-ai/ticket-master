---
name: session-results
description: Create, update, search or open the native ticket-master result display for one current console session, including analyses, document links and source-bound session metrics.
---

# Sitzungsergebnisse anzeigen

Nutze `ticket-master-results` aus der Installation oder `python -m ticket_master_runtime.results` im Checkout. Tk/ttk gehört zur Python-Standardbibliothek; die Python-Installation braucht Tk-Unterstützung und für `open` eine grafische Benutzersitzung. Kein BACH/Ocean, Browser oder Server ist erforderlich.

Eine Konsolensitzung besitzt eine Datei außerhalb der Ticketqueue, empfohlen `~/.config/ticket-master/result-sessions/<session-id>.json`. Die Sitzungs-ID kommt aus der autoritativen aktuellen Sitzung. Niemals fremde Sitzungen zusammenführen. Private Inhalte bleiben außerhalb des Repos; öffentliche Vorlage und JSON-Schema liegen unter `contracts/session-results/`.

```text
ticket-master-results create session.json --input seed.json
ticket-master-results validate session.json
ticket-master-results read session.json
ticket-master-results session-data session.json
ticket-master-results session-data session.json --input metrics-patch.json --revision 1 --sha256 <hash-aus-read>
ticket-master-results append session.json --input event.json --revision 1 --sha256 <hash-aus-read>
ticket-master-results replace session.json --input next.json --revision 2 --sha256 <hash-aus-read>
ticket-master-results open session.json
ticket-master-results decision-open draw.json --skill-dir /trusted/decision-draw
```

`decision-open` ist eine optionale Brücke zum vertrauenswürdigen installierten
`decision-draw`-Skill. Ohne `--skill-dir` werden die üblichen lokalen Skillordner
der drei Agenten geprüft; fehlt der Viewer, meldet die CLI das sichtbar und
installiert nichts. Dateiinhalte dürfen keinen ausführbaren Skillpfad bestimmen.

`read` liefert Revision und SHA-256. `replace` erwartet eine vollständige nächste Revision, exakt alte Revision + 1; `append` vergibt diese nächste Revision selbst. Nach Konflikt erneut lesen und den eigenen Eintrag auf den frischen Stand anwenden. Kooperierende Producer müssen ausschließlich diese CLI/API benutzen. Eine übrig gebliebene `.write-lock` sperrt fail-closed; vor manueller Bereinigung erst den schreibenden Prozess prüfen. Nicht kooperierende Editoren können die letzte Prüfung und das atomare Ersetzen unterlaufen; sie dürfen die Sessiondatei nicht parallel ändern.

`session-data` liest nur Identität, Revision, Hash und Metriken. Mit `--input`
aktualisiert es angegebene Metrikgruppen in derselben Datei, erhält alle Events
und nutzt denselben CAS-Weg. Erlaubte Patchfelder: `skills`, `tokens`, `agents`,
`agent_coverage`. Angegebene Listen werden vollständig ersetzt, ausgelassene
Gruppen bleiben erhalten. Vorher den schmalen Stand lesen und frühere belegte
Agenten/Skillbeobachtungen bei der Zusammenstellung bewahren.

Meldungen brauchen stabile eigene Event-IDs; vorhandene Ticket-, Message-, Receipt- und Log-IDs nur referenzieren. Ergebnis, Status, nächster Schritt und Quelle unterscheiden. `created_at` ist die Eintragszeit, kein nachträglich erfundener Ereigniszeitpunkt. Volltexte können in `details` stehen. Artefakte führen absolute Dateipfade, verständliche Labels, MIME-Typen und optional SHA-256. Die Anzeige öffnet nur erlaubte lokale Dokumenttypen auf einen bewussten Nutzerklick; keine ausführbaren Dateien oder Shellbefehle aus JSON.

Jede Ergebniskarte bietet „Nachfrage / Auftrag kopieren“. Die Vorlage enthält
nur Session-ID, stabile Ergebnis-ID, als Daten quotierten Titel, optional die
Ticket-ID, absoluten Sessiondateipfad und angezeigte Revision. Der Nutzer fügt
sie in die Konsole ein und ergänzt „Meine Frage / Anweisung“. Keine Vollberichte,
Artefaktinhalte, automatische Zustellung oder Modellaufrufe werden kopiert oder
ausgeführt. Nach späteren Sessionupdates die stabile Ergebnis-ID auflösen.

`metrics.skills/tokens/agents` enthalten ausschließlich tatsächlich gemeldete Nutzung. Jede Beobachtung braucht `source`, `observed_at` und `scope`. Fehlende Tokens bleiben `null` und werden als unbekannt angezeigt. Goal-Zähler sind keine verifizierten Provider-/Billable-Tokens. Angeforderte und tatsächlich belegte Modelle getrennt führen; fehlendes tatsächliches Modell bleibt `null`. Beobachtete Subagenten, aktuelle Aktivität und unbekannte Lifetime-Gesamtzahl ausdrücklich unterscheiden.

Das Fenster liest dieselbe Datei regelmäßig neu. Thema, Suche, geöffnete Details und äußere Scrollposition bleiben bei einem gültigen Revisionswechsel erhalten. Zurückgehende Revisionen oder andere Sitzungs-IDs werden abgelehnt. Es ist eine Anzeigeprojektion, keine Ticketqueue und keine Receipt-Registry. Öffnen bucht weder SOLVED/accepted noch eine Nachrichtenquittung. Optionales `delivery` verwendet die bestehenden Messages-Line-Modi `on_demand/active_chat/chat_start`, `surface=local_gui` und `ellmos.ticket.message-route-intent.v1` als Referenz. Den konfigurierten Messages-Line-Owner für echte Zustellung/Beobachtungsbuchung verwenden; der Viewer emittiert selbst keine Receipts.

Prüfen: `ticket-master-results validate` ist headless. Der opt-in native Smoke läuft mit `TM_NATIVE_UI_TEST=1 python -m pytest tests/test_session_results.py`. `open --evidence <lokale-datei.json>` protokolliert native Fensterflags, Geometrie und unter Windows Prozess-/aktive Konsolensitzung; das belegt technische Sichtbarkeit, keine menschliche Wahrnehmung.
