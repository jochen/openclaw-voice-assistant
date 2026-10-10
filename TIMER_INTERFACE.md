# Schnittstelle des Küchentimers

*Was der Assistent von einer Timer-Anzeige erwartet, und warum sie so
aussieht, wie sie aussieht.*

Der Küchentimer (`voice_assistant/services/kuechentimer.py`) lebt im
Assistenten: Er hält die Uhr, merkt den Ablauf und überlebt Neustarts
(`~/.openclaw/workspace/timer.json`). Anzeigen und Klingeln übernehmen
**Senken**. Eine Senke ist jede Gegenstelle mit einem HTTP-Endpunkt, der
diesen Vertrag erfüllt, etwa ein Tablet an der Küchenwand, ein Monitor oder
ein Display am ESP. Der Assistent kennt keine Anzeige mit Namen, sondern nur
URLs aus dem Profil.

```
"Gaston, Nudeltimer acht Minuten"
   → Parser (fest) oder Brain (Werkzeug)
   → Küchentimer im Assistenten ── POST Zustand ──→ Senke(n): anzeigen, klingeln
                                 ←─ "klingelt: […]" ─┘
     keine Senke bestätigt → Lautsprecher des Assistenten klingelt
```

Wer nur eine Senke bauen will, liest **Teil 1**. Warum es so geschnitten ist,
steht in **Teil 2**.

---

## Teil 1: Der Vertrag

### Konfiguration auf der Assistenten-Seite

```yaml
    timer:
      enabled: true
      klingeln: 3              # Klingel-Folgen je Ablauf (Default; per Sprache je Timer änderbar)
      klingel_abstand_s: 5     # Abstand der Folgen
      nachlauf_max_s: 1800     # so lange bleibt ein abgelaufener Timer sichtbar
      ansage: true             # "Der Nudel-Timer ist abgelaufen." am Lautsprecher
      lautsprecher_rueckfall: true
      senken:
        - name: kueche
          url: "http://<host>:<port>/api/timer"
          token: ""            # optional, dann Header Authorization: Bearer <token>
```

Fehlt der Block, gibt es keinen Timer, und Timer-Sätze gehen an den Brain wie
jeder andere Satz. Ohne `senken` klingelt nur der Lautsprecher.

### `POST {url}`: der Zustand

Der Assistent schickt **immer den ganzen Zustand**, nie einzelne Ereignisse:
nach jeder Änderung, beim Ablauf, beim Start und spätestens alle 30 Sekunden
als Herzschlag.

```json
{
  "art": "kuechentimer",
  "version": 1,
  "seq": 17,
  "nachlauf_max_s": 1800,
  "klingel_abstand_s": 5,
  "timer": [
    {"id": "3f9a01c2", "name": "Nudel", "dauer_s": 480, "rest_s": 312.4, "klingeln": 3, "still": false},
    {"id": "b7e2d410", "name": null,    "dauer_s": 300, "rest_s": -95.0, "klingeln": 3, "still": true}
  ]
}
```

| Feld | Bedeutung |
|---|---|
| `seq` | steigt bei jeder Änderung. Ein Herzschlag hat dieselbe `seq` wie der Stand davor. |
| `timer` | **vollständige** Liste, nach Ablaufzeit sortiert. Was fehlt, ist gelöscht. |
| `id` | bleibt über Verlängern und Klingelanzahl hinweg gleich. Neu stellen ergibt eine neue `id`. |
| `name` | Anzeigename oder `null` (der eine namenlose Timer) |
| `dauer_s` | Gesamtdauer, für einen Fortschrittsbalken |
| `rest_s` | Restzeit **in dem Moment, in dem gesendet wird**. Negativ heißt abgelaufen vor so vielen Sekunden. |
| `klingeln` | wie viele Klingel-Folgen dieser Timer beim Ablauf bekommt |
| `still` | `true`: nicht (mehr) klingeln, entweder ausgeklingelt oder per „Gaston, stopp“ beendet |

### Was die Senke tut

1. **Zeit rechnen mit der eigenen monotonen Uhr ab dem Empfang**:
   `rest = rest_s − (jetzt_monoton − empfangen_monoton)`. Die Wanduhr der
   Senke wird nicht gebraucht und soll nicht benutzt werden.
2. **Anzeigen**: Laufende Timer zählen herunter. Abgelaufene zählen weiter ins
   Negative und müssen **auf einen Blick** anders aussehen (Farbe), etwa
   „Nudel, abgelaufen vor 2:30“. Bei `rest < −nachlauf_max_s` verschwindet
   ein Timer auch ohne neuen Stand.
3. **Klingeln**: Sobald ein Timer mit `rest ≤ 0` und `still: false` zum ersten
   Mal gesehen wird, klingelt die Senke für diese `id` höchstens `klingeln`
   Folgen im Abstand `klingel_abstand_s`. Kommt `still: true` oder
   verschwindet der Timer, hört sie sofort auf. Je `id` wird nur einmal
   geklingelt. Nach dem Verlängern wird es ein neuer Ablauf: `rest_s` ist dann
   wieder positiv, und die Senke darf die `id` erneut klingeln lassen.
4. **Antworten**:

   ```json
   {"anzeigen": 1, "klingelt": ["3f9a01c2"]}
   ```

   `anzeigen` gibt an, wie viele Anzeigen gerade verbunden sind (zur
   Information). `klingelt` listet die `id`s, deren Klingeln **tatsächlich
   begonnen hat**. Das ist die einzige Rückmeldung, auf die der Assistent
   reagiert: Fehlt eine abgelaufene `id` in **allen** Antworten (oder
   antwortet keine Senke binnen 5 s), klingelt der Lautsprecher des
   Assistenten. Eine Senke, die nur weiterreicht, etwa an einen Browser per
   WebSocket, wartet mit der Antwort, bis dieser das Klingeln bestätigt hat,
   höchstens etwa 3 s.
5. Jede andere Antwort als 2xx mit diesem JSON zählt als „klingelt nicht“.

Eine Senke ohne Lautsprecher antwortet `"klingelt": []`. Dann klingelt der
Assistent selbst, und die Senke zeigt nur an.

### Der Assistent als Gegenstelle

Für den Brain und zum Ausprobieren am Assistenten, nur auf `127.0.0.1`:

| | |
|---|---|
| `GET  :18792/timer` | derselbe Zustand, den die Senken bekommen |
| `POST :18792/timer` | `{"aktion": "stellen", "name": "Nudel", "dauer_s": 480}` → `{"text": "Nudel-Timer, acht Minuten.", "ok": true, "rueckfrage": false, "zustand": {…}}` |
| `POST :18792/timer/still` | Klingeln beenden → `{"klingelte": true}` |

Die möglichen Werte für `aktion` sind `stellen`, `verlaengern` (um `dauer_s`
verlängern), `noch` (verlängern oder neu stellen), `loeschen` (`name: "*"`
löscht alle), `abfragen` und `klingeln` (Anzahl für diesen Timer). Gesprochen
wird dabei nichts, `text` ist die Ansage für den Aufrufer. `rueckfrage: true`
heißt, dass der Name fehlt und mehrere Timer in Frage kommen.

### Mindest-Implementierung

Eine Senke, die nur ins Log schreibt und nie klingelt, sodass der Assistent
klingelt (Python, Standardbibliothek):

```python
import json, time
from http.server import BaseHTTPRequestHandler, HTTPServer

class Senke(BaseHTTPRequestHandler):
    def do_POST(self):
        stand = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        for t in stand["timer"]:
            r = t["rest_s"]
            zeit = f"{int(abs(r)) // 60}:{int(abs(r)) % 60:02d}"
            print(t["name"] or "Timer", ("abgelaufen vor " if r <= 0 else "noch ") + zeit)
        body = json.dumps({"anzeigen": 0, "klingelt": []}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

HTTPServer(("0.0.0.0", 8099), Senke).serve_forever()
```

### Abnahme-Prüfung

Mit einem kurzen Timer am laufenden Assistenten:

```bash
curl -s -XPOST localhost:18792/timer -d '{"aktion":"stellen","name":"Probe","dauer_s":20}'
```

1. Die Senke zeigt „Probe“ sofort an und zählt herunter, ohne neuen POST
   abzuwarten.
2. Nach 20 s klingelt die Senke. Im Journal des Assistenten steht
   `Timer abgelaufen: Probe — klingelt über Senke`. Steht dort
   `Lautsprecher`, kam die `id` nicht in `klingelt` zurück.
3. Nach `klingeln × klingel_abstand_s` hört sie von selbst auf. Die Anzeige
   wechselt die Farbe und zählt weiter: „abgelaufen vor 0:16 …“.
4. Senke neu starten: Spätestens nach 30 s ist der Timer wieder da, mit
   richtiger Zeit, und er klingelt **nicht** erneut (`still: true`).
5. `curl -s -XPOST localhost:18792/timer -d '{"aktion":"loeschen","name":"Probe"}'`
   lässt den Timer verschwinden.

---

## Teil 2: Warum so

**Der Zustand liegt im Assistenten, nicht in der Anzeige.** Der Timer muss
gestellt, abgefragt und gelöscht werden können, auch wenn das Tablet gerade
die Verbindung verloren hat, was es in dieser Installation regelmäßig tut.
Wer den Zustand hält, muss erreichbar sein, wenn gesprochen wird, und das ist
der Assistent.

**Ganzer Zustand statt Ereignissen.** Eine Senke, die eine Nachricht verpasst
(Neustart, WLAN weg), wäre bei Ereignissen dauerhaft falsch. Mit dem ganzen
Zustand korrigiert sie sich beim nächsten POST, spätestens beim Herzschlag.
Dafür braucht es keine Quittungen, keine Wiederholungen und keine
Reihenfolge-Logik. Die Größe ist kein Argument, denn in einer Küche laufen
selten mehr als drei Timer.

**Restzeit statt Uhrzeit.** Die Uhr des Küchentablets (Android 5) war nicht
verlässlich. Eine Restzeit braucht nur eine monotone Uhr auf der Senke. Die
Laufzeit der Nachricht (Millisekunden im Heimnetz) geht dabei verloren, und
das ist bei einem Küchentimer egal.

**Die Senke meldet, ob sie wirklich klingelt.** „Angezeigt“ heißt noch nicht
„gehört“. Am Küchentablet blockiert der Browser `<audio>` ohne
Benutzergeste. Nur WebAudio spielt (gemessen 2026-10-10, auch nach 36 Minuten
mit gedimmtem Bildschirm). Eine Anzeige ohne Ton, ein Tablet ohne Strom oder
eine abgerissene WebSocket-Verbindung darf den Timer nicht stumm machen.
Deshalb zählt nur die Bestätigung, und ohne sie klingelt der Lautsprecher.

**Klingelt N-mal und hört von selbst auf.** Bei Alexa muss jeder Timer
abgestellt werden. Jochen: „Das ist quatsch, denn jeder Küchentimer hört
normal von selbst auf zu klingeln.“ Wer nicht in der Küche ist, sieht
danach an der Farbe und am negativen Zähler, wie lange es her ist.
„Gaston, stopp“ beendet nur das Klingeln. Nötig sein soll es nicht.

**Die Klingelzeit zählt ab dem Bemerken, nicht ab dem Ablauf.** Läuft ein
Timer während eines Neustarts des Assistenten ab, wäre die Klingelzeit sonst
schon vorbei, bevor zum ersten Mal geklingelt wird. Ist der Ablauf länger als
60 s her, wird gar nicht mehr geklingelt, sondern nur angezeigt. Sonst klingelt
die Küche am Morgen für die Nudeln vom Vorabend.

**Ein namenloser Timer ersetzt den namenlosen.** Wer zweimal „Timer fünf
Minuten“ sagt, hat sich verbessert und will keine zwei Timer. Gleiches gilt für
denselben Namen in anderer Form: „Nudeltimer“ und „Timer für die Nudeln“ sind
derselbe Timer.
