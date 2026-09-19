# `current_speaker.json` — wer gerade am Mikrofon war

Der Voice-Assistant schreibt nach **jedem** Sprach-Turn eine kleine Datei in den
Workspace. Sie ist dafür da, dass ein Werkzeug **außerhalb dieses Repos** vor
einer folgenreichen Aktion selbst nachsehen kann, ob gerade ein bekannter
Sprecher geredet hat — statt sich darauf zu verlassen, dass ein Sprachmodell
eine Regel befolgt.

```
~/.openclaw/workspace/voice/current_speaker.json
```

## Warum es das gibt

Bis zum 2026-09-19 stand der erkannte Sprecher **nur** im Prompt:

```
🎤 [Sprecher: jochen] Mach das Licht an
```

Das ist ein Hinweis an ein Sprachmodell, keine Durchsetzung. Ob bei
`[Sprecher: unbekannt]` eine folgenreiche Aktion unterbleibt, entschied allein
Prosa in der `AGENTS.md` — und die hatte eine Fortsetzungs-Ausnahme: war kurz
vorher jemand Bekanntes erkannt worden, durfte die nächste unbekannte Eingabe
als Fortsetzung desselben Sprechers gelten.

Am **2026-09-18** ist genau das eingetreten. Im Fablab fiel die Diarization aus
(Speaches antwortete ab 20:01 mit HTTP 500), jeder Turn wurde dadurch zu
„unbekannt", und der Assistent hat Desktop-Rechner ausgeschaltet. Er hat es
hinterher selbst so beschrieben: *„Streng genommen hätte ich nachfragen sollen,
ob der Rechner wirklich aus darf. Aber du hast ja gesagt, also ist er runter."*

Daraus folgen die zwei Eigenschaften, die diese Datei haben muss:

1. Sie unterscheidet **„unbekannt"** (gemessen, niemandem zugeordnet) von
   **„ausgefallen"** (gar nicht gemessen). Vorher war beides dasselbe, und ein
   Dienstausfall sah aus wie ein Fremder.
2. Sie wird **auch bei Ausfall geschrieben**. Würde der Ausfall übersprungen,
   bliebe der letzte bekannte Sprecher stehen — der Fehler von oben, nur
   dauerhafter.

## Format

```json
{
  "ts": "2026-09-19T17:52:04",
  "epoch": 1789760324,
  "status": "bekannt",
  "name": "jochen",
  "label": "jochen",
  "wakeword": "hey_jarvis"
}
```

| Feld | Bedeutung |
|---|---|
| `ts` | lokale Zeit des Turns, ISO, sekundengenau — für Menschen |
| `epoch` | dieselbe Zeit als Unix-Sekunden — **danach rechnet der Leser das Alter** |
| `status` | `bekannt` · `unbekannt` · `ausgefallen` · `nicht_eingerichtet` |
| `name` | Sprechername, **nur** bei `bekannt` gesetzt, sonst `null` |
| `label` | Anzeigeform, identisch mit dem `[Sprecher: …]` im Prompt |
| `wakeword` | welches Wakeword den Turn ausgelöst hat (oder `null`) |

Geschrieben wird atomar (`tmp` + `rename`) — ein Leser sieht nie eine halbe
Datei. Das ist kein Luxus: eine abgeschnittene JSON-Datei würde beim Parsen
scheitern und ein nachlässiger Leser würde das als „kein bekannter Sprecher"
oder schlimmer als „egal" behandeln.

### Die vier Status

- **`bekannt`** — Diarization hat die Stimme einer hinterlegten Referenz
  zugeordnet. Nur hier ist `name` gesetzt.
- **`unbekannt`** — Die Erkennung lief und hat niemanden zugeordnet
  (anonymer Cluster `SPEAKER_00`, oder zu wenig Sprache). Eine **gültige
  Messung** mit dem Ergebnis „das ist keiner von den Bekannten".
- **`ausgefallen`** — Es wurde **nicht gemessen**: HTTP-Fehler, Timeout,
  Netzproblem, abgestürzter Worker, oder das Ergebnis kam nicht rechtzeitig.
- **`nicht_eingerichtet`** — Das Profil hat gar keine Diarization konfiguriert.

## Wie ein Leser das benutzt

Die Regel, die aus dem Vorfall folgt:

> Eine folgenreiche Aktion nur ausführen, wenn der **letzte frische** Sprach-Turn
> den Status `bekannt` hatte.

„Frisch" heißt: `now - epoch <= FENSTER`. Ist der letzte Sprach-Turn älter (oder
fehlt die Datei), kommt die Anfrage nicht vom Mikrofon — dann greift die Regel
nicht, und z.B. eine Anweisung per Chat läuft normal durch. Das ist Absicht: der
Chat ist über den Messenger-Account authentifiziert und laut `AGENTS.md`
ausdrücklich der Weg, eine Rückfrage zu bestätigen.

Das Fenster ist ein Kompromiss. Zu groß, und eine harmlose Chat-Anweisung wird
minutenlang mitblockiert; zu klein, und ein Turn mit langer Werkzeug-Phase
rutscht durch. **120 Sekunden** ist der Ausgangswert — ein per Stimme
ausgelöster Schaltbefehl erreicht das Werkzeug normalerweise in Sekunden.

### Mindest-Implementierung

```python
import json, time

FENSTER = 120  # Sekunden

def sprecher_freigabe(pfad):
    """(erlaubt, begruendung). Fehlt die Datei, kam es nicht vom Mikrofon."""
    try:
        with open(pfad, encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        return True, "kein frischer Sprach-Turn"

    alter = time.time() - float(d.get("epoch", 0))
    if alter > FENSTER:
        return True, "kein frischer Sprach-Turn"
    if d.get("status") == "bekannt":
        return True, f"erkannt: {d.get('name')}"
    return False, {
        "unbekannt": "der Sprecher wurde nicht erkannt",
        "ausgefallen": "die Sprechererkennung war ausgefallen",
        "nicht_eingerichtet": "es gibt keine Sprechererkennung",
    }.get(d.get("status"), "der Sprecher steht nicht fest")
```

Wichtig an dieser Form: ein Fehler beim Lesen führt zu `True`. Das klingt
falsch, ist aber richtig — die Datei ist das Signal „gerade sprach jemand", und
ohne Signal gibt es keinen Sprach-Turn, den man gaten müsste. Ein kaputter
Voice-Assistant darf nicht den Chat lahmlegen.

### Abnahme-Prüfung

1. Per Stimme etwas Folgenreiches auslösen, während man selbst enrolled ist →
   läuft durch, `status` in der Datei ist `bekannt`.
2. Speaches-Diarization abschalten, dasselbe nochmal → **abgelehnt**, und die
   Ablehnung sagt „Erkennung war ausgefallen", nicht „unbekannter Sprecher".
   Das ist der Fablab-Fall vom 2026-09-18.
3. Dieselbe Aktion per Chat auslösen, mehr als `FENSTER` Sekunden später →
   läuft durch.

## Was das nicht ist

Das ist **keine Sicherheitsgrenze gegen ein Sprachmodell mit Shell-Zugang**.
Wer als derselbe Benutzer Kommandos ausführen darf, kann die Datei auch
schreiben oder das Werkzeug umgehen.

Der Nutzen ist ein anderer und trotzdem echt: der beobachtete Fehler war kein
Angriff, sondern eine **Rationalisierung** — das Modell hat eine Regel gelesen,
eine Ausnahme darin gefunden und sich selbst überzeugt. Gegen diesen Fehler
hilft eine Schranke, die das Modell aktiv unterlaufen müsste statt sie nur
auszulegen. Deshalb gibt es auch bewusst **keine Option und keine
Umgebungsvariable**, die das Gate abschaltet: was man abschalten kann, wird
wegargumentiert.

Die echte Grenze wäre, die Power-Aktionen hinter einen Dienst zu legen, den der
Agent nicht als derselbe Benutzer erreicht. Das ist offen.
