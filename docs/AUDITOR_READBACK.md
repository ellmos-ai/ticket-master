# Finding links and dated readbacks / Finding-Verknüpfung und datierter Readback

## English

`auditor_bridge.py --findings-to-tickets` treats an exact finding-ID link as
duplicate protection in every lifecycle folder. It never creates a second ticket
because a receipt is missing. A linked finding without a recognized current
readback is reported as `needs_readback`, with all existing ticket paths and a
reason. `skipped_existing` means a dated readback was recognized, **not** that the
finding is resolved or independently verified.

The existing log envelope remains `YYYY-MM-DD | actor@host | assertion — evidence`.
The date must be on/after the date in `M-YYYYMMDD-slug` and not in the future. There
is no new TTL. The assertion must bind that exact ID, a concrete location and an
observed value together. For example, these existing field and prose forms work:

```text
2026-10-25 | reviewer@host | Finding M-20261025-example location=lib/probe.py value=locked — lock_scan --check-dir /repo exit_code=1 status=locked
2026-10-25 | reviewer@host | Finding M-20261025-example at lib/probe.py currently reports 0 linked tickets — pytest tests/test_probe.py -q: 35 passed; SHA256 deadbeef
2026-10-25 | reviewer@host | Finding M-20261025-example location=lib/probe.py value=missing — command=stat exit_code=1 result=missing; log=stat.log
```

Recognition consumes **complete clauses**, rather than searching independently
for positive words, paths and hashes. Every semicolon-separated evidence clause
must be a recognized artifact reference (path, URL, hash, commit), test summary,
command result or compatible observed absence (`no lock found` with `clear`).
Unknown qualifications before or after a marker keep the entry observable. Thus
`measurement never performed; SHA256 deadbeef` cannot become a measured receipt.
Nor can `measurement failed` accompanying `value=clear`.

A `commit <7-40 hex>` clause is provenance, not a measurement by itself. It is
accepted only when the same receipt also carries an independent numeric
value/count, test summary, command output, or artifact path that exists. A bare
commit therefore remains `needs_readback`.
A complete `sha256: <64 hex>` or `sha256=<64 hex>` clause, optionally prefixed by `receipt` or `Beleg`, is independent evidence; shorter hashes are not.

This is deliberately a bounded text recognizer, **not natural-language truth
verification**. It accepts the illustrated prose, English/German observation
verbs, existing location/value aliases, numeric/Boolean/null values and common
state words. Other wordings, complex values, multiple assertions or ambiguous
context remain `needs_readback`; that is a request for review, not a declaration
that the author's measurement is false. No new mandatory field protocol is
introduced. Concrete evidence is recognized as a reference; the bridge does not
read referenced file contents, fetch URLs, execute commands or attest their
contents. For receipts produced on the local host, however, `location=` is
resolved against `auditor_bridge.location_base`
when configured, otherwise against the invoking process's working directory. A
conclusively missing location yields `needs_readback` with reason
`location not found`. Foreign-host locations and portable placeholders such as
`<OneDrive>` are not locally resolvable and retain the bounded syntactic check.
UNC/network paths and absolute paths written in a foreign operating-system
syntax are also left unresolved, so recognition never initiates a network
filesystem access or reinterprets a foreign path locally. Filesystem errors other than proven non-existence
likewise do not become a negative claim. Artifact paths used to make a commit
clause independent must exist under the same resolution rules.

`unknown`, `locked` and `clear` are legitimate measured results. Error and absence
values (`failed`, `error`, `missing`, `unavailable`, `fehlt`) require an execution
result, not just an adjacent hash. A nonzero exit code is allowed. Explicit
state/value/result outputs must agree with the asserted value; exit codes and
HTTP codes are separate measurements, not synonyms for the observed state.

## Deutsch

Eine exakte Finding-ID im Ticket verhindert weiterhin neue Duplikate. Fehlt ein
erkennbarer aktueller Readback, liefert die Brücke `needs_readback` samt allen
bestehenden Ticketpfaden und Grund. `skipped_existing` bedeutet ausschließlich,
dass ein datierter Readback erkannt wurde, nicht dass das Finding erledigt oder
unabhängig geprüft ist.

Das vorhandene Format bleibt `YYYY-MM-DD | akteur@host | Aussage — Beleg`. Datum,
exakte Finding-ID, konkreter Ort, Messwert und Beleg gehören zusammen. Das Datum
darf weder vor dem Finding-Datum noch in der Zukunft liegen; eine zusätzliche
Altersgrenze gibt es nicht. Feldformen wie `Ort=lib/probe.py Wert=0` und freie
Sätze wie `bei lib/probe.py zeigt 0 verknüpfte Tickets` bleiben möglich.

Die Erkennung prüft vollständige Aussage- und Belegklauseln. Unbekannte Zusätze
werden nicht übergangen; ein beigefügter Hash oder Pfad macht aus einer fehlenden,
unbestätigten oder nicht ausgeführten Messung keinen positiven Readback. Das gilt
auch für Zusätze vor oder hinter dem Belegmarker. Mehrdeutige Texte bleiben zur
Beobachtung sichtbar, ohne eine neue Ticketkopie auszulösen.

Eine Klausel `commit <7-40 hex>` belegt allein nur Herkunft, keine Messung. Sie
zählt erst zusammen mit einem unabhängigen numerischen Wert/einer Zählung, einer
Testzusammenfassung, einer Kommandoausgabe oder einem existierenden Belegpfad.
Ein alleiniger Commit bleibt daher `needs_readback`.
Eine vollständige Klausel `sha256: <64 Hex-Zeichen>` oder `sha256=<64 Hex-Zeichen>`, optional mit Präfix `receipt` oder `Beleg`, ist ein unabhängiger Beleg; kürzere Hashes sind es nicht.

Es handelt sich um begrenzte Texterkennung, keine vollständige semantische oder
mehrsprachige Wahrheitsprüfung. Nicht erkannte Formulierungen sind kein Beweis
gegen die Messung. Es gibt kein exklusives neues Pflichtfeldformat. Referenzierte
Dateiinhalte werden nicht gelesen, URLs nicht abgerufen, Kommandos nicht
ausgeführt und Inhalte nicht beglaubigt. Bei auf dem lokalen Host erzeugten
Receipts wird `location=` jedoch gegen
`auditor_bridge.location_base` aufgelöst, falls konfiguriert, sonst gegen das
Arbeitsverzeichnis des Aufrufs. Ein sicher fehlender Ort ergibt
`needs_readback` mit dem Grund `location not found`. Orte fremder Hosts und
portable Platzhalter wie `<OneDrive>` bleiben lokal unauflösbar und werden wie
bisher nur syntaktisch geprüft. Auch UNC-/Netzwerkpfade und absolute Pfade in
einer fremden Betriebssystem-Syntax bleiben unaufgelöst, damit die Erkennung
weder einen Netzwerk-Dateizugriff auslöst noch einen fremden Pfad lokal
umdeutet. Andere
Dateisystemfehler werden nicht als Nichtexistenz ausgelegt. Ein Belegpfad, der
einen Commit unabhängig machen soll, muss nach denselben Regeln existieren.

Ein gemessenes `unknown`, `locked` oder `clear` ist zulässig. Tatsächlich
beobachtete Fehler und fehlende Zustände zählen mit konkretem Ausführungsbeleg
ebenfalls; Exitcode 0 ist keine Voraussetzung. Ein alleiniger Hash neben
`value=missing` oder `value=failed` genügt hingegen nicht. Explizite Zustandswerte
im Kommandoergebnis müssen zur Aussage passen. Die Beispiele oben und die
Regressionstests in `tests/test_auditor_bridge.py` zeigen die erkannten Formen.
