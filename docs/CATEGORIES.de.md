# Ticket-Kategorien v1 — Cluster, Unterkategorien, Übergänge

Das Kategorien-System des Ticket-Lebenszyklus, ab v1 (2026-07-31). Ersetzt das
Flachmodell `ROOT | QUEUED | PENDING | .USER | SOLVED` durch acht Cluster mit
feineren Unterkategorien, klaren Ein-/Ausgangsregeln und einem Autonomie-Loop
für den Betrieb ohne ständige User-Rückfragen.

Verbindlich für: `prompts/TICKET-MASTER.de.md` / `TICKET-MASTER.en.md`
(Routing), `prompts/TICKET-WRITER.*.md` (Dedup-Scan — Rolle seit 2026-08-15
ausgekapselt nach `ellmos-ai/system-auditor`, Datei bleibt nur als
Herkunftsbeleg liegen), `lib/ticket_writer.py` (`_LIFECYCLE_SUBDIRS`,
weiterhin aktive ID-Vergabe-Bibliothek, nicht zu verwechseln mit der
ausgekapselten Rolle, sowie STATUS-Parser/-Validator),
`tickets/_templates/TICKET.txt` (STATUS-Feld).

---

## Cluster-Übersicht

| Cluster | Ordner | Bedeutung | Unterkategorien |
|---------|--------|-----------|-----------------|
| INBOX | `tickets/INBOX/` (Root = Alias) | neu eingegangen, noch nicht triagiert | — |
| ACTIONABLE | `tickets/ACTIONABLE/` | sofort umsetzbar: kein Blocker, keine User-Abhängigkeit | — |
| QUEUED | `tickets/QUEUED/` | an Provider/Agent übergeben, Ergebnis ausstehend | — |
| BLOCKED | `tickets/BLOCKED/` | externer Blocker (nicht der User) | `host-receipt`, `foreign-state`, `lock`, `quota`, `dependency` |
| WAITING | `tickets/WAITING/` | zeit- oder marker-gebunden | `scheduled`, `review-due`, `marker` |
| USER | `tickets/USER/` | hängt zwingend am User | `decision`, `data`, `freigabe`, `hardware`, `session`, `marker` |
| PARKED | `tickets/PARKED/` | bewusst zurückgestellt | `skip`, `backlog`, `until-trigger` |
| SOLVED | `tickets/SOLVED/` | gelöst und empirisch bestätigt | — |

**Legacy-Aliase (rückwärtskompatibel, lesbar, keine neuen Einträge):**
`PENDING/` → Bestand wird einmalig auf ACTIONABLE/USER/BLOCKED/WAITING/PARKED
verteilt; `.USER/` → `USER/`; das Root-Verzeichnis (`tickets/*.txt`) gilt als
INBOX.

---

## Unterkategorien

### BLOCKED (externer Blocker — nie der User)

- `host-receipt` — wartet auf Receipt/Rückmeldung eines anderen Hosts oder
  Agenten (Receipt-Pfad im Ticket benennen).
- `foreign-state` — Fremdstand: ein anderes Repo/eine andere Instanz hat einen
  ungeklärten Zustand (Dirty Tree, offener fremder Vorgang), der zuerst
  aufgeräumt werden muss.
- `lock` — aktiver Lock (z. B. `LOCK*.txt` / `LOCK.permissions.json` eines
  Rechte-/Sperrsystems); User-Locks sind absolut.
- `quota` — Nutzungslimit, Token- oder Kontingentgrenze eines Providers.
- `dependency` — fachliche/technische Abhängigkeit von einem anderen Ticket,
  Modul oder Release (Referenz im Ticket benennen).

### WAITING (zeit-/marker-gebunden)

- `scheduled` — festes Datum/Zeitplan; Bearbeitung startet am Termin.
- `review-due` — ein Review ist fällig (fachlich, nicht blockiert).
- `marker` — wartet auf eine autonom prüfbare Marker-Datei oder ein definiertes
  Event; ist die Feststellung oder Bestätigung nur durch den User möglich,
  stattdessen `USER/marker` verwenden.

### USER (nächster Schritt ist zwingend der User)

- `decision` — der User muss eine Entscheidung treffen (Optionen im Ticket).
- `data` — der User muss Daten, Informationen oder Zugänge liefern.
- `freigabe` — eine explizite Freigabe/Genehmigung des Users ist nötig.
- `hardware` — physischer Schritt/Gerät, den nur der User ausführen kann.
- `session` — nur-User-startbares Modell, Login-Session oder manueller Lauf.
- `marker` — ein Marker/Ereignis muss zwingend vom User geliefert oder
  bestätigt werden. Beispiel: Der User bestätigt, dass ein Wettbewerb beendet
  ist. Ein autonom prüfbarer Marker bleibt `WAITING/marker`.

### PARKED (bewusst zurückgestellt)

- `skip` — bewusst übersprungen/verworfen, aber nicht gelöscht (Aktenkraft).
- `backlog` — irgendwann später, ohne definierten Trigger.
- `until-trigger` — zurückgestellt bis ein benanntes Ereignis eintritt.

---

## STATUS-Spiegelung

Das `STATUS`-Feld eines Tickets spiegelt Ordnerlage und Unterkategorie:

```
STATUS:        <CLUSTER>[/<unterkategorie>] (seit YYYY-MM-DD)
```

Beispiele: `STATUS: ACTIONABLE (seit 2026-07-31)`,
`STATUS: BLOCKED/host-receipt (seit 2026-07-31)`,
`STATUS: USER/marker (seit 2026-07-31)`.

- Ordner und STATUS müssen kongruent sein.
- Jedes Verschieben zwischen Clustern aktualisiert STATUS und fügt eine
  `VERLAUF`/`LOG`-Zeile mit Grund hinzu.
- Die Ordnerstruktur bleibt flach: `USER/decision`, `BLOCKED/dependency` und
  ähnliche Unterordner sind ungültig. Die Unterkategorie steht ausschließlich
  im `STATUS`-Feld; `ticket_mover.py` weist solche Ziele fail-closed ab und
  `ticket_audit.py` meldet vorhandene verschachtelte Tickets read-only. Der
  JSON-Bericht behält die rückwärtskompatible Pfadliste
  `nested_lifecycle_tickets` und ergänzt `nested_lifecycle_details`: pro Fund
  `source`, das durch Entfernen der Unterordner abgeleitete flache
  `expected_target` und `target_collision`. Der Audit ändert dabei weder
  STATUS noch Claim oder Datei.

---

## Ein-/Ausgangsregeln

- **INBOX** — Eingang: jedes neue Ticket (Intake). Ausgang: Triage
  (GATE 1 + Dringlichkeits-Gate) → ACTIONABLE (sofort/delegierbar), QUEUED
  (direkt übergeben), BLOCKED/WAITING/USER/PARKED (mit Begründung) oder
  SOLVED (Fast-Lane, sofort verifiziert gelöst).
- **ACTIONABLE** — Eingang: Triage „jetzt umsetzbar" oder Entblockung aus
  BLOCKED/WAITING/USER/PARKED. Ausgang: QUEUED (Delegation läuft), SOLVED
  (direkt gelöst) oder Rückstufung mit neuem Grund.
- **QUEUED** — Eingang: Übergabe an Provider/Subagent. Ausgang: SOLVED
  (GATE 4 Erfolg), ACTIONABLE (Fehlschlag → Fallback-Kette), BLOCKED/quota
  (Limit erreicht) oder USER (nur-User-Schritt nötig).
- **BLOCKED** — Eingang nur mit benanntem, belegtem Blocker + Unterkategorie.
  Ausgang: ACTIONABLE, sobald der Blocker empirisch entfallen ist; alternativ
  PARKED/until-trigger, wenn der Blocker dauerhaft ist.
- **WAITING** — Eingang nur mit Datum oder Marker. Ausgang: ACTIONABLE bei
  Termin-/Marker-Eintritt; PARKED, wenn der Termin entfällt.
- **USER** — Eingang nur, wenn der nächste Schritt zwingend der User ist
  (Unterkategorie Pflicht). Ausgang: ACTIONABLE nach Entscheidung/Lieferung
  des Users; PARKED/skip, wenn der User verwirft.
- **PARKED** — Eingang nur auf ausdrücklichen Auftrag oder definierten
  Trigger. Kein automatisches Wiederaufnehmen. Ausgang: ACTIONABLE bei
  Trigger/Auftrag; SOLVED nur nach echter Erledigung.
- **SOLVED** — Endzustand; nur mit empirischer Bestätigung im
  LOESUNG/SOLUTION-Feld.

Grundregel: Ein Ticket verlässt BLOCKED/WAITING/USER/PARKED nur mit Beleg
(Receipt, Datum, User-Antwort, Trigger) — nie durch Vermutung.

---

## Autonomie-Loop

Betrieb ohne ständige User-Rückfragen:

- **BLOCKED → periodischer Re-Check.** Bei Session-Start und in Intervallen
  prüfen, ob der Blocker entfallen ist (Receipt da? Lock weg? Kontingent
  zurück? Fremdstand bereinigt? Abhängigkeit gelöst?). Blocker empirisch weg
  → nach ACTIONABLE ziehen und bearbeiten, nicht liegen lassen.
- **USER → gebündelt vorlegen.** USER-Tickets sammeln und als EINE gebündelte
  Vorlage präsentieren (keine Einzel-Pings). Nach der User-Antwort jedes
  Ticket sofort umhängen (meist ACTIONABLE oder PARKED/skip). Das gilt auch
  für `USER/marker`; der Marker wird nicht still als autonomes
  `WAITING/marker` umgedeutet.
- **WAITING → am Datum/Marker ziehen.** `scheduled`/`review-due` beim
  Tageswechsel, `marker` bei jedem Lauf prüfen; eingetreten → ACTIONABLE.
- **PARKED → kein Auto-Re-Check.** Nur auf ausdrücklichen Auftrag oder beim
  benannten until-trigger-Ereignis.

---

## Migration vom Flachmodell (Übergangsregeln)

- **SOLVED, QUEUED:** unverändert.
- **PENDING/ → einmalige Bestandsverteilung.** Der Inhalt entscheidet:
  Blocker nur benannt, aber überholt/entfallen → ACTIONABLE; User-Abhängigkeit
  → USER (mit Unterkategorie); externer Blocker → BLOCKED (mit Unterkategorie);
  Termin/Marker → WAITING; bewusst später → PARKED.
- **.USER/ → USER/:** Inhalte übernehmen und mit Unterkategorie versehen.
- **Root (`tickets/*.txt`):** = INBOX (unclaimed/Intake) — unverändert.
- **Keine neuen Einträge** nach `PENDING/` oder `.USER/`. Beide Ordner bleiben
  als Legacy-Aliase lesbar; die ID-Vergabe (`lib/ticket_writer.py`,
  `_LIFECYCLE_SUBDIRS`) zählt sie mit, damit keine Ticket-ID doppelt vergeben
  wird.

---

## Multi-Host-Hinweis

In cloud-synchronisierten Multi-Host-Setups (OneDrive, Dropbox, Google Drive)
gilt die Claim-Konvention per Dateiname
(`T-YYYYMMDD-#########.<HOST>.txt`) unverändert; die neunstellige
Zufallskomponente wird ausschließlich durch `lib/ticket_writer.py` erzeugt,
nie manuell gewählt oder hochgezählt. Die Cluster-Ordner werden
host-übergreifend gemeinsam genutzt.

- Hosts mit altem Stand lesen `PENDING/` und `.USER/` als Legacy-Aliase
  weiter — alte Inhalte sind keine Fehler. Das Verschieben übernimmt ein
  einmaliger Migrationslauf je Instanz.
- `BLOCKED/host-receipt` ist der kanonische Ort für Tickets, die auf einen
  anderen Host warten; Receipt-Pfad im Ticket benennen, damit der Re-Check
  des Autonomie-Loops belegt arbeiten kann.

### Routing-Schema v2 und umlaufende Vertragsakten

Schema v2 trennt Ziel-, Ausführungs- und Besitzachse im Dateinamen:
`T-ID[.to-<ziel>][.via-<Clutch-Selektor>][.claim-<HOST>].txt`. Nur Tickets mit
`ROUTING_SCHEMA: 2` dürfen diese reservierten Segmente tragen. Ein altes
`T-ID.<HOST>.txt` bleibt immer ein Legacy-Claim und wird niemals als Ziel oder
Hostmenge umgedeutet.

Transfer- und Forktickets enthalten einen beim Erstellen belegten
`TARGET_SYSTEMS`-Snapshot und genau eine `SYSTEM_LEDGER`-Zeile je Ziel. Ein
berechtigtes System setzt nur seine Zeile von `pending` auf `claimed` und nach
einem empirischen Receipt auf `done` oder `blocked`. Danach gibt es
`.claim-…` frei; `.to-…` und eine noch aktive `.via-…`-Bindung bleiben stehen.
Solange mindestens eine erforderliche Zeile nicht `done` ist, ist `SOLVED`
unzulässig. Ein Transportzustand wie `delivered` ist kein Ledger-Zustand und
kann niemals fachlichen Abschluss belegen.

Eine abgelaufene Ausführungsbindung wird vor dem nächsten erfolgreichen Claim
als `expired-unbound` protokolliert. Das entfernt ausschließlich `.via-…` und
verändert weder Ziel-Snapshot noch Ledger oder aktiven Claim. Ein blockierter
Systemanteil kann je nach Gesamtzustand weiter als umlaufende Akte sichtbar
bleiben oder mit belegtem Grund nach `BLOCKED/host-receipt` wechseln.

`.SYNC` ist weiterhin nur Transport- und Receipt-Fläche. ticket-master besitzt
Vertrag, Claim, Ledger und Abschlussprädikat; Clutch besitzt die
Ausführungsauflösung; system-gap-master besitzt das Cross-System-Protokoll. Es
gibt keinen zweiten Ticket-Lebenszyklus, keine Retry-Schleife und keine
Transport-Inbox im ticket-master.
