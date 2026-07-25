# Schnittstelle des Voice-Aktuators

*Was der Assistent von der ausführenden Seite erwartet — und warum sie so
aussieht, wie sie aussieht.*

Der Voice-Aktuator (`voice_assistant/services/actuator.py`) fängt Schaltbefehle
direkt nach der Spracherkennung ab und führt sie lokal aus, statt den langsamen
Agenten zu bemühen:

```
Wakeword → Aufnahme → STT → Aktuator ─── Schaltbefehl? ──┬─ ja  → ausführen, Antwort vorlesen  (~0,5 s)
                                                          └─ nein → weiter wie immer zum Agenten
```

In dieser Installation liegt die ausführende Seite auf **Node-RED**. Das ist
aber keine Voraussetzung: der Aktuator spricht mit **zwei HTTP-Endpunkten**, und
wer die bereitstellt, ist ihm gleich. Dieses Dokument beschreibt den Vertrag
vollständig genug, um ihn gegen Home Assistant, openHAB, ein eigenes Skript oder
was auch immer neu zu implementieren.

Wer nur die Endpunkte braucht: **Teil 1**. Wer verstehen will, warum sie so
geschnitten sind — und das lohnt sich, bevor man sie „verbessert" —: **Teil 2**.

---

## Teil 1: Der Vertrag

### Konfiguration auf der Assistenten-Seite

Im Profil in `config.yaml`:

```yaml
    actuator:
      enabled: true
      base_url: "http://<host>:<port>/voiceact"   # ohne Schrägstrich am Ende
      token_file: ""                              # leer → <repo>/voiceact-token.txt
      llm_url: "http://localhost:8090/v1/chat/completions"
      llm_timeout: 5.0
      intent_timeout: 1.5
      mqtt_host: "<broker>"                       # optional, siehe unten
      mqtt_port: 1883
      refresh_poll_sec: 600
```

Ohne diesen Block ist der Aktuator aus und der Assistent verhält sich wie
zuvor. Fällt er zur Laufzeit aus, ebenfalls — siehe „Graceful Degradation".

**Authentifizierung:** beide Endpunkte erwarten den Header
`X-Actuator-Token: <secret>`. Der Wert steht in `voiceact-token.txt` im
Repo-Wurzelverzeichnis (gitignored). Ein gemeinsames Shared Secret, kein
OAuth — die Endpunkte sind rein im lokalen Netz erreichbar.

### `GET {base_url}/capabilities`

Liefert die **geschlossene** Liste dessen, was gesteuert werden darf. Aus ihr
baut der Assistent zur Laufzeit das JSON-Schema und den Prompt für sein kleines
Sprachmodell. Es gibt kein handgepflegtes Gerätevokabular im Assistenten.

```json
{
  "version": "e3d6af78",
  "ziele": [
    { "id": "kuechenlicht",
      "namen": ["Küchenlicht", "Licht in der Küche"],
      "typ": "licht",
      "aktionen": ["ein", "aus"],
      "reversibel": true,
      "kosten": "niedrig" },

    { "id": "wohnzimmerrollo",
      "namen": ["Wohnzimmerrollo", "Rollo im Wohnzimmer"],
      "typ": "rollo",
      "aktionen": ["auf", "zu", "setzen"],
      "wert": { "einheit": "prozent", "min": 0, "max": 100 },
      "reversibel": true,
      "kosten": "mittel" }
  ]
}
```

| Feld | Pflicht | Wer liest es |
|---|---|---|
| `version` | ja | Assistent — Änderungserkennung. Beliebiger String, muss sich nur ändern, wenn sich die Liste ändert (Hash, ISO-Zeit, Zähler). |
| `ziele[].id` | ja | Assistent — **das ist der Wert, den das Sprachmodell ausgibt.** Stabiler ASCII-Key. Siehe die Namensregel in Teil 2, ids sind hier keine freien Labels. |
| `ziele[].namen` | ja | Assistent — Aliase für den Prompt. Umlaute erwünscht, das ist die natürliche Sprechweise. |
| `ziele[].aktionen` | ja | Assistent — erlaubte Verben **dieses** Ziels. Treibt Schema und Plausibilitätsprüfung. |
| `ziele[].wert` | nur bei `setzen` | Assistent — `{einheit, min, max}`, landet als Bereichsangabe im Prompt. |
| `ziele[].typ` | optional | nur informativ. Der Assistent kodiert **keine** Typen hart. |
| `ziele[].kosten`, `reversibel` | optional | **nur die ausführende Seite.** Der Assistent liest sie nie. |

Verben, die diese Installation benutzt: `ein`, `aus`, `auf`, `zu`, `setzen`,
`aktivieren` (Szene), `starten` (Routine). Die Liste ist nicht fest verdrahtet —
der Assistent sammelt ein, was in den `aktionen[]` vorkommt. Eigene Verben
funktionieren, solange der Prompt sie erklärt
(`_build_system_prompt` in `actuator.py`).

### `POST {base_url}/intent`

Nimmt **einen** Intent, prüft ihn inhaltlich, führt ihn aus.

```json
{ "ist_kommando": true,
  "ziel": "wohnzimmerrollo",
  "aktion": "setzen",
  "wert": 40,
  "einheit": "prozent",
  "quelle": "aktuator",
  "request_id": "b3f1…",
  "bestaetigt": true }
```

`ist_kommando` ist immer `true` — negative Klassifikationen werden gar nicht
erst gepostet. `wert`/`einheit` sind `null` außer bei `setzen`. `bestaetigt`
fehlt normalerweise und steht nur beim zweiten Anlauf eines Handshakes.
`konfidenz` schickt dieser Client **bewusst nicht** (Teil 2), `sprecher` in v1
ebenfalls nicht.

Antwort — **immer HTTP 200**, wenn die Anfrage inhaltlich verarbeitet wurde:

```json
{ "status": "ausgefuehrt",
  "request_id": "b3f1…",
  "ausgefuehrt": { "ziel": "wohnzimmerrollo", "aktion": "setzen", "wert": 40, "einheit": "prozent" },
  "grund": null,
  "gesprochen": "Wohnzimmerrollo auf vierzig Prozent." }
```

**Der Assistent wertet genau zwei Dinge aus:**

1. **`gesprochen`** — wird **wörtlich vorgelesen**, bei *jedem* Status. Bestätigung,
   Ablehnungsgrund, Rückfrage: immer dieses Feld. Der Assistent formuliert nichts
   selbst. Dieses Feld ist damit das wichtigste der ganzen Schnittstelle.
2. **`status == "zurueckgestellt"`** — löst den Bestätigungs-Handshake aus.

Jeder andere `status`-Wert wird gleich behandelt: `gesprochen` vorlesen, fertig.
Der Assistent kennt `ausgefuehrt`, `abgelehnt` und `unbekanntes_ziel` aus dieser
Installation, verzweigt aber nicht danach — er schreibt sie nur mit. Eine
Portierung braucht also **eine** exakte Statuszeichenkette: `zurueckgestellt`.
Der Rest darf heißen, wie er will.

`ausgefuehrt` und `grund` sind optional und landen nur im Mitschnitt.

### Der Bestätigungs-Handshake

Für Ziele, bei denen ein Irrtum teuer wäre („alle Rollos", „Nachtruhe"):

1. Der Assistent postet ohne `bestaetigt`.
2. Die ausführende Seite antwortet `status: "zurueckgestellt"` und stellt in
   `gesprochen` eine **Rückfrage** („Wirklich alle Rollos öffnen?").
3. Der Assistent spricht sie, piept und hört zu.
4. Bei „ja" postet er **denselben Intent mit derselben `request_id`** plus
   `"bestaetigt": true`. Bei „nein" oder Unverständlichem bricht er ab und
   postet gar nichts mehr.
5. Die ausführende Seite überspringt bei `bestaetigt: true` ihr Gate und führt aus.

Es gibt in v1 **keinen** zweiten Rückfrage-Versuch.

### Fehler, Timeouts, Wiederholungen

| Fall | HTTP | Verhalten des Assistenten |
|---|---|---|
| Fachliches Ergebnis (auch Ablehnung) | **200** + `status` | `gesprochen` vorlesen. Kein Retry — das ist eine endgültige Antwort. |
| Kaputtes JSON, Pflichtfeld fehlt | 400 | Loggen, „Die Haussteuerung antwortet nicht.", kein Retry |
| Token falsch/fehlt | 401 | dito |
| Interner Fehler | 500 | dito |
| Verbindung/Timeout (1,5 s) | — | **Genau ein** Wiederholungsversuch mit derselben `request_id`, dann aufgeben |

Daraus folgt eine harte Anforderung: **die ausführende Seite muss über
`request_id` deduplizieren**, mindestens 60 Sekunden lang. Eine wiederholte
`request_id` darf dieselbe Antwort liefern, aber **nicht** ein zweites Mal
schalten. Ohne das ist der Retry gefährlich.

Zweite Anforderung: **sofort antworten, nicht auf das Gerät warten.** Ein Rollo
braucht 30 Sekunden; der Sprachpfad darf so lange nicht stehen. `ausgefuehrt`
heißt „angenommen und abgesetzt", nicht „Zielzustand erreicht".

### Optional: Änderungsbenachrichtigung

Ändert sich die Zielliste, soll der Assistent das möglichst sofort merken. Zwei
Wege, beide eingebaut:

- **MQTT** (falls konfiguriert): retained Message auf `voiceact/capabilities_changed`
  mit `{"version": "<neu>"}`. Der Assistent holt dann sofort neu.
- **Poll** als Fallback: alle `refresh_poll_sec` Sekunden `GET /capabilities`,
  Neuaufbau nur bei abweichender `version`.

MQTT ist reiner Komfort. Wer keinen Broker hat, setzt `mqtt_host: ""` —
dann läuft nur der Poll, mit bis zu zehn Minuten Verzug. (Ein *fehlender*
`mqtt_host` fällt auf den Default dieser Installation zurück; zum Abschalten
braucht es den leeren String.)

### Was der Assistent selbst prüft, bevor er postet

- Das Sprachmodell ist per `response_format: json_schema` (strict) auf die
  bekannten ids, Verben und Einheiten eingesperrt. Ein unbekanntes Ziel ist
  formal nicht ausdrückbar.
- `is_actionable()` prüft zusätzlich gegen die zwischengespeicherte Zielliste:
  `ziel` bekannt **und** `aktion` in `ziel.aktionen`. Sonst wird nicht gepostet,
  sondern an den Agenten durchgereicht.

Das ersetzt die Prüfung auf der ausführenden Seite **nicht**. Ein zweiter
Konsument (Agent, Skript, Fehler) kann jederzeit Unsinn posten.

### Graceful Degradation

Jeder Fehler im Aktuator führt zurück auf den bisherigen Weg, keiner bricht den
Assistenten ab:

| Ausfall | Folge |
|---|---|
| `/capabilities` beim Start nicht erreichbar | Aktuator bleibt untätig, alles geht an den Agenten. Poll/MQTT versuchen es weiter. |
| Sprachmodell tot oder zu langsam | `classify()` liefert `None` → Agenten-Pfad, exakt wie vor dem Einbau |
| Intent unschlüssig | Agenten-Pfad |
| `/intent` nicht erreichbar | Gesprochener Fehler, **kein** Agenten-Fallback (es wurde nichts geschaltet) |
| Token-Datei fehlt | Warnung beim Start, Aufrufe scheitern dann mit 401 |

### Mindest-Implementierung

Kleinster funktionierender Aufbau, ohne jede Automatisierungsplattform:

```python
from flask import Flask, request, jsonify
app = Flask(__name__)
TOKEN = open("voiceact-token.txt").read().strip()
ZIELE = {"kuechenlicht": {"namen": ["Küchenlicht", "Licht in der Küche"],
                          "typ": "licht", "aktionen": ["ein", "aus"],
                          "reversibel": True, "kosten": "niedrig"}}
GESEHEN = {}   # request_id -> Antwort (mind. 60 s aufheben!)

def auth_ok():
    return request.headers.get("X-Actuator-Token") == TOKEN

@app.get("/voiceact/capabilities")
def capabilities():
    if not auth_ok(): return "", 401
    return jsonify(version="v1",
                   ziele=[{"id": k, **v} for k, v in ZIELE.items()])

@app.post("/voiceact/intent")
def intent():
    if not auth_ok(): return "", 401
    b = request.get_json(silent=True) or {}
    ziel, aktion, rid = b.get("ziel"), b.get("aktion"), b.get("request_id")
    if not (ziel and aktion and rid):
        return jsonify(error="ziel, aktion und request_id sind erforderlich"), 400
    if rid in GESEHEN:                       # Idempotenz — Pflicht wegen Retry
        return jsonify(GESEHEN[rid])
    z = ZIELE.get(ziel)
    if z is None:
        antwort = {"status": "unbekanntes_ziel", "request_id": rid,
                   "gesprochen": f"Ich kenne kein Ziel namens {ziel}."}
    elif aktion not in z["aktionen"]:
        antwort = {"status": "abgelehnt", "request_id": rid,
                   "gesprochen": "Das geht bei diesem Gerät nicht."}
    else:
        schalte(ziel, aktion, b.get("wert"), b.get("einheit"))   # <- deine Welt
        antwort = {"status": "ausgefuehrt", "request_id": rid,
                   "ausgefuehrt": {"ziel": ziel, "aktion": aktion,
                                   "wert": b.get("wert"), "einheit": b.get("einheit")},
                   "gesprochen": f"{z['namen'][0]} {'eingeschaltet' if aktion == 'ein' else 'ausgeschaltet'}."}
    GESEHEN[rid] = antwort
    return jsonify(antwort)
```

Das reicht für einen vollständig funktionierenden Sprach-Schaltpfad. Wert-Bereiche,
das Kosten-Gate mit `zurueckgestellt` und die Änderungsbenachrichtigung kommen
danach.

### Abnahme-Prüfung

```bash
T=$(cat voiceact-token.txt); B=http://<host>:<port>/voiceact

curl -s $B/capabilities                       # → 401
curl -s -H "X-Actuator-Token: $T" $B/capabilities   # → version + ziele
curl -s -H "X-Actuator-Token: $T" -H 'Content-Type: application/json' \
     -d '{"ziel":"kuechenlicht","aktion":"ein"}' $B/intent          # → 400
curl -s -H "X-Actuator-Token: $T" -H 'Content-Type: application/json' \
     -d '{"ist_kommando":true,"ziel":"gibtsnicht","aktion":"ein","quelle":"aktuator","request_id":"t1"}' \
     $B/intent                                                      # → 200 unbekanntes_ziel
# zweimal mit derselben request_id: identische Antwort, aber nur EIN Schaltvorgang
```

Auf der Assistenten-Seite prüft man mit einem gefahrlosen Ziel:

```bash
ow-venv/bin/python -c "
from voice_assistant.config import load_profile
from voice_assistant.services.actuator import Actuator
a = Actuator(load_profile().actuator); a.refresh()
print(len(a.digest), 'Ziele, Version', a.version)
print(a.classify('Mach das Küchenlicht an'))"
```

---

## Teil 2: Warum die Schnittstelle so aussieht

Jeder Punkt hier ist bezahlt worden — durch eine Messung oder durch einen Fehler
im Betrieb. Wer portiert, sollte sie kennen, bevor er etwas vereinfacht.

**Das Vokabular kommt von der ausführenden Seite, nicht aus dem Assistenten.**
Eine im Sprachcode gepflegte Geräteliste wäre eine zweite Wahrheit neben der
Hausautomation — und damit früher oder später falsch. Stattdessen erzeugt
`/capabilities` das JSON-Schema und den Prompt zur Laufzeit: neues Gerät anlegen
→ Version ändert sich → der Assistent baut sein Vokabular neu → sofort
sprechbar. Kein Deploy, kein Neustart, kein Prompt-Edit. Beim Sprung von 60 auf
62 Ziele wurde das überprüft.

**Ein Ziel pro Intent — Gruppen und Abläufe entfaltet die ausführende Seite.**
Die naheliegende Alternative wäre, das Sprachmodell mehrere Intents ausgeben zu
lassen. Bewusst verworfen: dann wächst seine kognitive Last mit der Komplexität
der Hausautomation. So bleibt eine Zwanzig-Schritt-Routine für das Modell genau
so einfach wie „Licht an" — beides ist *ein benanntes Ziel*. Die ganze
Ablauf-, Timing- und Logikkomplexität bleibt dort, wo sie hingehört. Das Modell
weiß nicht einmal, dass hinter „Guten Morgen" zwanzig Schaltvorgänge stecken.
Der Preis steht unten unter „Grenzen".

**Die Sicherheit liegt im Rahmen, nicht im Modell.** Zwei unabhängige
Schranken: das Schema kann Unbekanntes nicht *ausdrücken*, die ausführende Seite
akzeptiert Unsinniges nicht. Man verlässt sich nie darauf, dass das Modell sich
benimmt. Ein kleines quantisiertes Modell tut das nämlich nicht zuverlässig —
und das ist in Ordnung, solange es nicht muss.

**`aktionen[]` steht pro Ziel, nicht pro Typ.** Verlockend wäre, die erlaubten
Verben aus dem `typ` abzuleiten — „Licht kann ein/aus". Beim Abgleich mit der
laufenden Installation tauchten aber sieben Typen auf, darunter `schalter`, den
niemand eingeplant hatte. Eine typ-basierte Ableitung wäre daran zerbrochen.
Der Generator sammelt deshalb ein, was tatsächlich in den `aktionen[]` steht;
`typ` ist rein informativ.

**`gesprochen` kommt von der ausführenden Seite, nicht vom Assistenten.** Nur
sie weiß, was wirklich passiert ist — inklusive der Eigenheiten („das
Spiegellicht schaltet die Spiegelheizung mit"). Der Assistent liest wörtlich
vor. Das hält deutsche Formulierung und Sachwissen auf derselben Seite und
erspart dem Sprachpfad jede Textbausteinlogik. Ursprünglich war das Feld nur für
den Erfolgsfall gedacht; dass es auch Ablehnungen und Rückfragen trägt, hat sich
als die bessere Lösung erwiesen — der Assistent muss dadurch überhaupt keinen
Satz selbst bilden.

**Fachliche Ergebnisse sind HTTP 200, nur Transportfehler sind 4xx/5xx.** Eine
Ablehnung ist kein Serverfehler; sie ist eine gültige, endgültige Antwort.
Diese Trennung entscheidet, ob der Client wiederholen darf. Ohne sie retriet man
Ablehnungen — oder man retriet nie und verliert Antworten an einem Paketverlust.

**`request_id` + Idempotenz.** Sie machen den einen erlaubten Retry ungefährlich.
Ein Timeout bedeutet nicht, dass nichts geschaltet wurde — die Antwort kann
unterwegs verloren gegangen sein. Ohne Deduplizierung fährt das Rollo zweimal.
Dieselbe `request_id` trägt außerdem den Handshake über zwei Anfragen hinweg.

**Kein `konfidenz`-Feld.** Der Vertrag erlaubt es, dieser Client schickt es
absichtlich nicht. Das Gate lautet: teures Ziel ohne belastbare Konfidenz →
Rückfrage. Indem der Assistent gar keine schickt, fragen teure Ziele
*immer* nach. Eine erfundene Zahl wäre der schlechtere sichere Zustand.

**Sofort antworten, physische Vollendung ist asynchron.** Ein Rollo braucht 30
Sekunden. Würde die ausführende Seite darauf warten, stünde der Sprachpfad
still. `ausgefuehrt` heißt „angenommen und abgesetzt". Wer Vollzug wissen will,
hört den Gerätezustand ab, nicht diese Antwort.

**`ist_kommando: false` wird nie gepostet.** Bei „wie spät ist es" spart das den
Netzweg; die Frage geht direkt an den Agenten. Die ausführende Seite sollte den
Fall trotzdem defensiv abweisen, falls ihn je jemand schickt.

**Die ids sind Teil der Spracherkennung, keine freien Labels.** Der teuerste
Befund. Das Modell gibt die `id` aus und hängt stark an ihrer Schreibweise —
nicht nur an den Aliassen. Daraus folgt eine Regel für Gruppen:

> **Eine Gruppe darf keine morphologische Variante ihrer Mitglieder sein.
> Sie braucht ein eigenes Wort.**

Gemessen am echten Modell, je zehn Sätze bei `temperature 0`:

| Gruppe für die drei Küchenlichter | Ergebnis |
|---|---|
| `kuechenlichter` | „Mach das Küchenlicht an" landet auf der **Gruppe** — schlimmer als vorher |
| `kuechenlichter` + disziplinierte Aliase + Beispielpaar | besser, aber instabil: kippt bei kleinen Prompt-Änderungen |
| `kueche_alle_lichter` (künstlich konstruiert) | Gruppe wird **nie** gewählt, `kuechenarbeitsplattenlicht` gewinnt |
| **`kuechenbeleuchtung`** | **10/10** |

„Küchenlicht" und „Küchenlichter" trennt eine Silbe — ein kleines quantisiertes
Modell verwechselt sie in beide Richtungen. „Küchenlicht" und
„Küchenbeleuchtung" sind trennscharf. Die Gegenprobe zeigt die andere Klippe:
eine zu weit weg konstruierte id wird gar nicht mehr gefunden. Gesucht ist ein
*natürliches eigenes Wort*, keine Kunst-id. Dasselbe gilt für die Aliase — ein
Alias, der einen Gerätenamen fast wiederholt („Lichter in der Küche" neben
„Licht in der Küche"), zieht denselben Fehler an.

**Der Assistent verzweigt nur auf `zurueckgestellt`.** Der Vertrag nennt zwar
vier Statuswerte, aber weil für alles außer der Rückfrage ohnehin nur
`gesprochen` vorgelesen wird, ist die Kopplung zwischen beiden Seiten auf ein
einziges Wort geschrumpft. Wer portiert, muss sich nur an dieses halten — die
übrigen Namen darf er frei wählen.

---

## Grenzen von v1

Ehrlich gesagt, damit niemand sie erst im Betrieb findet:

- **Mehrere Ziele auf einmal gibt es nicht.** „Mach im Erdgeschoss alles aus"
  oder „Licht in Küche und Wohnzimmer" ist nicht ausdrückbar. Vorgesehene
  Antwort: als Gruppe oder Szene anlegen, dann ist es *ein* Ziel.
- **Plural läuft still ins Leere.** Gibt es keine passende Gruppe, wählt das
  Modell das nächstliegende Einzelziel und meldet Erfolg. „Mach alle Lichter in
  der Küche an" schaltete eines von dreien und sagte „Küchenlicht
  eingeschaltet." Genau dieselbe Eigenschaft fängt allerdings auch
  Hörfehler der Spracherkennung ab („Küchenarbeitsblattchenlicht" →
  `kuechenarbeitsplattenlicht`). Es ist eine Eigenschaft, nicht zwei.
- **Deutsche Zahlwörter sind fragil.** Die Spracherkennung macht aus „23"
  gelegentlich „3 und 20". Deshalb sollte die ausführende Seite Werte gegen
  `min`/`max` plausibilisieren — das fängt den Fall, weil 3 Grad außerhalb des
  erlaubten Bereichs liegen.
- **Kein Sprecher-Gate.** `sprecher` steht im Vertrag, wird in v1 nicht
  geschickt. Wer spricht, wird nur mitgeschrieben.
- **Der Aktuator kann sich nicht korrigieren.** Er sieht keinen Gerätezustand
  und weiß nicht, ob er zu wenig getan hat. Dafür ist ein separater Überwacher
  vorgesehen, der Transkript und tatsächlich Ausgeführtes vergleicht — er liest
  `actuator_turns.log` (siehe `_log_actuator_turn` in `voice_assistant/assistant.py`).
  Gebaut ist er noch nicht.

## Weiterlesen

- `voice_assistant/services/actuator.py` — die Client-Seite
- `ACTUATOR_V1_PLAN.md` — Entstehungsgeschichte und Messungen (Path B, Modellwahl)
- `actuator_prototype/` — der validierte Prototyp: Schema-Generator und Testläufe
