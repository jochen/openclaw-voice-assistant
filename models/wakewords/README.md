# Wakeword-Bundles

Jedes eigene Wakeword lebt in einem eigenen Unterverzeichnis:

```
models/wakewords/<name>/
  manifest.yaml       # Metadaten (siehe unten)
  <name>.tflite        # trainiertes openwakeword-Modell
  verifier.pkl         # optional: sprecherspezifischer Verifier (Meilenstein 3)
  samples/             # echte Referenz-Aufnahmen (16 kHz mono WAV) — NICHT committen
```

`manifest.yaml` (Mindestfelder):

```yaml
name: gaston
model: gaston.tflite
threshold: 0.5   # empfohlener Trigger-Score
```

- `manifest.yaml` + `.tflite` sind **portabel** und werden committed.
- `samples/` bleibt lokal (`.gitignore`: `models/wakewords/*/samples/`) — Stimmen
  der Familie gehören nicht auf GitHub.
- Existiert für einen in `config.yaml` unter `wakewords:` konfigurierten
  `bundle`-Namen kein Verzeichnis hier, wird der Name stattdessen als
  eingebauter openwakeword-Modellname behandelt (z.B. `hey_jarvis`, `alexa`).

Volle Spezifikation (Trainings-Pipeline, Studio-CLI, Validierung, Verifier):
siehe `Wakeword_Studio_Spec.md` im Projekt-Root.
