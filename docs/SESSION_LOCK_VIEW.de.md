# Optionale Lock-Master-Ansicht

Die native Ergebnisanzeige kann eine explizit konfigurierte Teilmenge eigener
Locks darstellen. Sie erfindet keine Lockregistry und durchsucht keine Roots.
Die bestehende Ownerfunktion `lock_utils.fence_status(path, fence)` entscheidet
über `held`/`lost`. Fehlende Dateien, widersprüchliche Eigentümer oder Änderungen
während des Reads bleiben `unknown`. Der angezeigte Zeitstempel ist eine
Beobachtung, keine dauerhafte Sperrgarantie und keine Schreibberechtigung.

## Vertrauensgrenze

Der Betreiber prüft das private Profil und dessen Autorenbelege unabhängig und
übergibt seinen SHA-256 als Startargument. Es ist ein Start-Vertrauensanker,
keine vom Feed selbst behauptete Autorität. Der Profilhash muss aus der Freigabe
stammen, nicht aus ungeprüften Sessiondaten. Pfade sind physisch absolut und
ohne Symlink-/Junctionumleitung. Das Profil enthält:

```json
{
  "schema": "ellmos.ticket.lock-view-profile.v1",
  "session_id": "synthetische-session",
  "owner_module": "/approved/lock_utils.py",
  "owner_module_sha256": "<geprüfter SHA-256>",
  "claims": [
    {
      "agent": "belegter-agent",
      "owner": "belegter-owner",
      "fence": 123,
      "path": "/own-scope/LOCK.example.txt",
      "proof_path": "/private-evidence/author-proof.json",
      "proof_sha256": "<geprüfter SHA-256>"
    }
  ],
  "gui": null
}
```

Das ist eine Formskizze, keine funktionsfähige reale Konfiguration. Der
geprüfte Autorenbeleg enthält exakt `session_id`, `agent`, `owner`, `fence`,
`path` sowie `source_reference` auf den ursprünglichen Grant-/Toolbeleg.
Ein Besitzerpräfix oder die bloße Verfügbarkeit eines Agenten beweist keine
Sessionzugehörigkeit. Der Betreiber bestätigt die Belegkette vor der Freigabe;
die Anzeige prüft Profil-/Beleghashes und exakte Grantzuordnung erneut.
Es ist keine automatische Agentenentdeckung oder vollständige Lockinventur.

Der gepinnte, dependencyfreie kanonische `lock_utils.py` wird aus frisch
geprüften Bytes in einen privaten Modulkontext geladen. Kein fremder
`sys.modules`-Cache, keine Feed-Codepfade und keine Bytecodewrites. Der Betreiber
muss die vorhandene Ownerquelle als lokalen Code geprüft haben. Eine andere
Installation mit zusätzlichen Importabhängigkeiten benötigt neue Prüfung.

## Vorhandene Oberfläche

Optional kann `gui` statt `null` dieses Objekt enthalten:

```json
{
  "kind": "existing_local_dashboard",
  "url": "http://127.0.0.1:8095/",
  "source_path": "/installed/lock-master/watcher/web_server.py",
  "source_sha256": "<geprüfter SHA-256>"
}
```

Nur der bewusste Nutzerklick öffnet diese feste lokale URL mit der zugeordneten
Anwendung. Die Quelle wird vor jedem Klick erneut geprüft. Das ist ein Einstieg
in die vorhandene Oberfläche, kein Serverstart, Gesundheitscheck oder Beleg der
Identität des an diesem Port laufenden Prozesses. Die Anzeige startet und pollt
keinen Watcher, Scanner oder Webserver. `START.bat` ist hierfür ungeeignet, da
es Dienste starten kann. Ohne separat bereitgestellte Oberfläche bleibt dieser
optionale Einstieg unkonfiguriert; die native Ergebnisanzeige funktioniert allein.

## Betrieb und Aktualisierung

Beim ersten Öffnen der Sessiondaten und über „Lockstatus gezielt prüfen“ werden
nur Profil, Autorenbelege, Ownerquelle und die einzeln genannten Lockdateien
gelesen. Reguläre Feedupdates lösen keine Lockscans aus. Die Sessiondatei enthält
keine Kopie dieser Locks. Keine Freigabe, Erneuerung, Reclaim- oder Installaktion
ist implementiert. Nach Änderungen an Profil, Ownerquelle oder Belegkette ist
ein neuer explizit geprüfter Start-Vertrauensanker nötig.
