# openclaw-voice-enrol — OpenClaw Plugin

> [Deutsche Version unten](#deutsch)

OpenClaw plugin that lets the LLM manage speaker references for the [openclaw-voice-assistant](../). Three agent tools call the loopback enrolment server (`127.0.0.1:18791`) provided by `voice_assistant`:

| Tool | Purpose | Example trigger phrase |
|---|---|---|
| `voice_enroll_speaker(name)` | Save the last recording as `speakers/<name>.wav` | *"learn my voice, I am Jochen"* |
| `voice_list_speakers()`      | Return the list of enrolled speakers          | *"what voices do you know?"* |
| `voice_remove_speaker(name)` | Delete a reference                            | *"forget the voice of Jochen"* |

## Requirements

- OpenClaw with plugin support (`>= 2026.5`)
- The voice_assistant process is running (it owns the enrolment server). If it isn't running the tools return a helpful error.

## Install

From this repo's checkout:

```bash
openclaw plugins install --link ~/openclaw_voice_assist/openclaw-plugin/
openclaw gateway restart
openclaw plugins inspect voice-enrol --runtime --json
```

The last command should show `"status": "loaded"` and the three tool names under `toolNames`. After this, the LLM can invoke the tools whenever the user expresses an intent that matches the tool description.

## Layout

```
openclaw-plugin/
  package.json           npm metadata + openclaw.extensions
  openclaw.plugin.json   plugin manifest (tools contract, configSchema)
  index.js               ESM entry, definePluginEntry + registerTool
```

The plugin has no runtime dependencies beyond Node ≥ 18 (`fetch` is built-in).

## How it works

1. User says *"hey jarvis, learn my voice, I am Jochen, and let me tell you ..."* (≥ 5 s of speech)
2. `voice_assistant` records, saves the audio as `~/.openclaw/workspace/voice/last_recording.wav`, runs STT + diarization (Speaker: unknown — there's no reference yet), and forwards the text to OpenClaw
3. The LLM recognizes the enrolment intent and calls `voice_enroll_speaker(name="Jochen")`
4. The plugin POSTs `{"name": "Jochen"}` to `http://127.0.0.1:18791/enroll`
5. The enrolment server copies `last_recording.wav` to `speakers/jochen.wav` and timestamps a backup in `originals/`
6. Subsequent recordings are matched against `speakers/jochen.wav`; the wrapper sends `[Sprecher: jochen]` to the LLM

## Update / Uninstall

```bash
# Re-link after editing index.js (no rebuild needed — pure JS)
openclaw plugins install --link ~/openclaw_voice_assist/openclaw-plugin/
openclaw gateway restart

# Uninstall
openclaw plugins uninstall voice-enrol
openclaw gateway restart
```

## Notes

- `openclaw.plugin.json` requires `configSchema` even when the plugin has no settings — an empty object `{}` is fine.
- The runtime resolves `import { definePluginEntry } from "openclaw/plugin-sdk/plugin-entry"` automatically. No build step needed; the plugin is loaded from `index.js` directly.
- All three tools fall back to a graceful error message if the enrolment server is unreachable (e.g. voice_assistant not running).

---

## Deutsch

OpenClaw-Plugin, mit dem das LLM Stimm-Referenzen für den [openclaw-voice-assistant](../) verwalten kann. Drei Agenten-Tools rufen den Loopback-Enrolment-Server (`127.0.0.1:18791`) im `voice_assistant` an:

| Tool | Zweck | Beispiel-Trigger |
|---|---|---|
| `voice_enroll_speaker(name)` | Speichert die letzte Aufnahme als `speakers/<name>.wav` | *"lerne meine Stimme, ich bin Jochen"* |
| `voice_list_speakers()`      | Liefert die Liste enrolled Sprecher                   | *"welche Stimmen kennst du?"* |
| `voice_remove_speaker(name)` | Löscht eine Referenz                                  | *"vergiss die Stimme von Jochen"* |

### Voraussetzungen

- OpenClaw mit Plugin-Support (`>= 2026.5`)
- Der `voice_assistant`-Prozess läuft (er stellt den Enrolment-Server bereit). Wenn nicht, geben die Tools einen sinnvollen Fehler zurück.

### Installation

Aus diesem Repo-Checkout:

```bash
openclaw plugins install --link ~/openclaw_voice_assist/openclaw-plugin/
openclaw gateway restart
openclaw plugins inspect voice-enrol --runtime --json
```

Letztere Ausgabe sollte `"status": "loaded"` und die drei Tool-Namen unter `toolNames` zeigen. Danach ruft das LLM die Tools auf, sobald der Nutzer eine passende Intent äußert.

### Aufbau

```
openclaw-plugin/
  package.json           npm-Metadaten + openclaw.extensions
  openclaw.plugin.json   Plugin-Manifest (tools-Contract, configSchema)
  index.js               ESM-Entry, definePluginEntry + registerTool
```

Keine Runtime-Abhängigkeiten außer Node ≥ 18 (`fetch` ist eingebaut).

### Funktionsweise

Das Enrolment läuft über zwei Voice-Turns (Follow-up-Mechanismus):

**Turn 1 — Intent erkennen:**
1. Nutzer sagt *"hey jarvis, lerne meine Stimme, ich bin Jochen"*
2. LLM erkennt die Enrolment-Intent und antwortet mit dem Trainingssatz:
   > *"Kein Problem! Lies bitte folgenden Satz laut vor: 'Ich bin Jochen — bitte lerne jetzt meine Stimme. Über die grünen Felder und durch die tiefen Wälder reite ich gerne. Die süßen Äpfel und die reifen Birnen schmecken köstlich. Heute früh schien die Sonne, jetzt zieht Regen auf.'"*
3. LLM ruft `voice_enroll_speaker` noch **nicht** auf — es wartet auf den Follow-up

**Turn 2 — Trainingssatz aufnehmen:**
4. Nutzer liest den Trainingssatz laut vor (~10 s Sprache)
5. `voice_assistant` speichert diese Aufnahme als `last_recording.wav`
6. LLM ruft `voice_enroll_speaker(name="Jochen")` auf
7. Plugin POSTet `{"name": "Jochen"}` → Enrolment-Server kopiert `last_recording.wav` nach `speakers/jochen.wav` + Backup in `originals/`
8. Folge-Aufnahmen werden gegen `speakers/jochen.wav` gematcht

**Warum dieser Trainingssatz?** Er deckt alle phonetisch wichtigen deutschen Laute ab: alle Vokale (a, e, i, o, u), Umlaute (ä, ö, ü), Diphthonge (ei, au), Ich-Laut, Nasale und das deutsche R — typisch diagnostisch für individuelle Stimmen bei Wespeaker resnet34.

### Update / Deinstallation

```bash
# Nach index.js-Änderung neu linken (kein Build nötig — reines JS)
openclaw plugins install --link ~/openclaw_voice_assist/openclaw-plugin/
openclaw gateway restart

# Deinstallation
openclaw plugins uninstall voice-enrol
openclaw gateway restart
```

### Stolperfallen

- `openclaw.plugin.json` verlangt `configSchema` als Pflichtfeld, auch wenn das Plugin keine Settings hat — leeres Objekt `{}` reicht.
- Die Runtime löst `import { definePluginEntry } from "openclaw/plugin-sdk/plugin-entry"` automatisch auf. Kein Build-Step; das Plugin wird direkt aus `index.js` geladen.
- Alle drei Tools liefern eine freundliche Fehlermeldung zurück, wenn der Enrolment-Server nicht erreichbar ist (z.B. voice_assistant läuft nicht).
