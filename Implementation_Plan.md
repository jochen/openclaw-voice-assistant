# ReSpeaker XVF3800 + XIAO ESP32-S3 — Implementierungsprotokoll

## Ziel

ReSpeaker 4-Mic Array XVF3800 mit XIAO ESP32-S3 als Audio-Frontend für den
OpenClaw Voice Assistant, **ohne Home Assistant**, direkt über ESPHome Native
API (aioesphomeapi, Port 6053).

Pipeline: Wakeword (openwakeword auf Pi) → STT (Speaches) → OpenClaw → TTS
(Speaches, WAV-Playback über ESP media_player via HTTP announce).

---

## Architektur

```
Pi                                    ESP32-S3 (ESPHome)
────────────────────────────────      ─────────────────────────────
openwakeword ← PCM 16kHz mono   ←──  voice_assistant (API_AUDIO-Modus)
                                        XVF3800-Mic → I2S → ESP → aioesphomeapi
RespeakerSink.play_wav()
  → resample WAV auf 48000Hz stereo
  → HTTP-Server (Port 18800)      ──►  media_player (announce API)
  → send_voice_assistant_                ESP lädt WAV via HTTP
    announcement_await_response()        aic3104 DAC → Speaker
```

### Komponenten

| Datei | Funktion |
|---|---|
| `esphome/respeaker.yaml` | ESP32-S3 Firmware: I2S, Mic, Speaker, LED-Ring, voice_assistant, media_player |
| `voice_assistant/audio/respeaker.py` | `RespeakerClient` (asyncio-Bridge), `RespeakerSource` (Mic), `RespeakerSink` (TTS) |
| `voice_assistant/wakeword/respeaker.py` | Wakeword-Stub (Wakeword läuft auf Pi via openwakeword, nicht auf ESP) |
| `voice_assistant/services/leds.py` | `RespeakerRing`: LED-Phase via Number-Entity per API |
| `voice_assistant/config.py` | `RespeakerAudio`-Dataclass inkl. `volume` |

---

## Stolperfallen (chronologisch, mit Lösung)

### 1. ESPHome 2026.4: `media_player: platform: i2s_audio` entfernt

**Symptom:** Config-Validierung schlägt fehl mit "only available with framework
arduino".

**Ursache:** ESPHome 2026.4 hat die `i2s_audio`-Platform für `media_player:`
entfernt. Neue Architektur: `speaker: platform: i2s_audio` + `media_player:
platform: speaker`.

**Lösung:**
```yaml
speaker:
  - platform: i2s_audio
    id: i2s_out_speaker
    ...

media_player:
  - platform: speaker
    id: respeaker_player
    announcement_pipeline:
      speaker: i2s_out_speaker
      format: WAV
```

---

### 2. Announce-Timeout weil `api_client_` im ESP null ist

**Symptom:** `send_voice_assistant_announcement_await_response()` hängt bis
Timeout, obwohl ESP im Log "PLAYING" zeigt. `AnnounceFinished`-Message kommt
nie beim Pi an.

**Ursache:** ESPHome setzt `api_client_` intern erst, wenn auf der
Client-Seite `subscribe_voice_assistant()` aufgerufen wurde. Ohne Subscription
versucht ESP die `AnnounceFinished`-Response über einen nullptr zu senden.

**Lösung:** Vor dem Announce immer `subscribe_voice_assistant()` aufgerufen
haben (oder aktiv sein). `RespeakerClient._main()` macht das automatisch —
beim Test-Skript muss es explizit aufgerufen werden.

---

### 3. Announce-Timeout weil `media_player:` in `voice_assistant:` fehlte

**Symptom:** Announce läuft laut ESP-Log (`PLAYING → IDLE`), aber
`AnnounceFinished` kommt trotzdem nicht an.

**Ursache:** `voice_assistant:` Block im YAML hatte kein `media_player:`-Feld.
ESPHome routet den Announce-Callback nur wenn `media_player` im
voice_assistant konfiguriert ist.

**Lösung:**
```yaml
voice_assistant:
  id: va
  media_player: respeaker_player   # ← zwingend erforderlich
  ...
```

---

### 4. "Audio stream settings not compatible" — WAV-Format falsch

**Symptom:** ESP-Log zeigt Endlosschleife `[W] Audio stream settings not
compatible`.

**Ursache:** Pi schickt WAV in Originalformat (z.B. 22050 Hz mono von Piper,
16000 Hz mono von Speaches). ESP media_player erwartet exakt 48000 Hz stereo.

**Lösung:** In `RespeakerSink._to_48k_stereo()` vor dem Serving auf Pi-Seite
resampling + Stereo-Duplizierung:
```python
samples = resample_poly(samples, 48000 // g, rate // g).astype(np.int16)
stereo = np.column_stack([samples, samples])
# WAV schreiben: 2ch, 16bit, 48000Hz
```

---

### 5. Extreme Verzerrung / Rauschen trotz korrektem WAV

**Symptom:** Audio kommt aus Speaker, ist aber extrem verzerrt/übersteuert,
auch bei minimaler Amplitude (z.B. `* 0.0005`). Klingt wie weißes Rauschen
mit schwach erkennbarem Inhalt.

**Ursache:** I2S-Konfiguration: XVF3800 treibt den I2S-Bus als Master im
**32-Bit-Modus** (64 BCLK-Zyklen pro Frame). Der Speaker war auf `16bit`
(Standard) konfiguriert → 16-Bit-Sample landet falsch ausgerichtet im
32-Bit-Frame → aic3104 interpretiert Bit-Müll → Verzerrung.

**Lösung:**
```yaml
speaker:
  - platform: i2s_audio
    bits_per_sample: 32bit   # ← muss zum XVF3800-Master passen
    i2s_mode: secondary
    ...
```
Amplitude-Faktor dann bei `1.0` (kein künstliches Abschwächen nötig).

---

### 6. `MediaPlayerCommand.SET_VOLUME` existiert nicht

**Symptom:** `RespeakerClient loop crashed: SET_VOLUME`

**Ursache:** `aioesphomeapi.MediaPlayerCommand` hat keine `SET_VOLUME`-Konstante.
Volume wird als separater kwarg übergeben, nicht als Command.

**Lösung:**
```python
# Falsch:
self._api.media_player_command(key, command=MediaPlayerCommand.SET_VOLUME, volume=0.8)

# Richtig:
self._api.media_player_command(key, volume=0.8)
```

---

### 7. API_AUDIO-Modus: ESP's VAD feuert `handle_stop` nie

**Hintergrund:** Wenn `handle_start` den Wert `0` zurückgibt (API_AUDIO-Modus),
streamt der ESP Audio via `handle_audio`-Callbacks. In diesem Modus läuft
VAD **nicht** auf dem ESP — der Pi übernimmt das mit WebRTC VAD. Das bedeutet:
`handle_stop` wird **nur** gefeuert wenn die Session explizit vom Pi beendet
wird, nicht durch Stille-Erkennung auf dem ESP.

**Konsequenz:** TTS via `send_voice_assistant_event(TTS_STREAM_START, ...)` +
Audio-Chunks funktioniert in diesem Modus nicht — der ESP verarbeitet das nur
im State `WAITING_FOR_RESPONSE`, der nie erreicht wird weil der Pi-interne VAD
die Session steuert.

**Lösung:** TTS komplett über announce-API statt session-internem Streaming.

---

### 8. ThinkingWorker kollidiert mit Announce-API

**Symptom:** `press_start_button()` wird nach jedem Announce aufgerufen.
ThinkingWorker feuert währenddessen Lebenszeichen-Sätze als weiteren Announce
— Announces werden seriell abgearbeitet (jeder wartet auf `success=True`).
Das ist korrekt und blockiert nicht, dauert aber entsprechend länger.

**Beobachtung:** Kein Bug, nur zu beachten: Antwort-TTS und Lebenszeichen-TTS
laufen sequenziell über denselben Announce-Kanal.

---

### 9. 2-3s Verzögerung zwischen Wakeword und "Ja?"-Ausgabe

**Symptom:** Wakeword erkannt, aber "Ja?" kommt erst 2-3 Sekunden später.
Führt dazu dass der Nutzer schon gesprochen hat bevor die Aufnahme beginnt.

**Ursachen:**
- `WledLeds._run()` nutzte `subprocess.run()` → blockiert bis wled_controller.py
  fertig ist (HTTP-Call zum WLED, kann bei nicht erreichbarem Gerät 1-2s dauern).
  Wird zweimal aufgerufen (`clear()` + `single()`).
- `RespeakerRing._find_key()` rief beim **ersten** LED-Kommando
  `list_entities_services()` über die API auf (Netzwerkaufruf, bis zu 5s Timeout).

**Lösung:**
- `WledLeds`: `subprocess.run` → `subprocess.Popen` (fire-and-forget).
- LED-Phase-Key wird jetzt beim Client-Connect gecacht (zusammen mit Button/Player
  in `list_entities_services()`). `_find_key()` liest nur noch das Feld.
- WLED kann via `leds.wled.enabled: false` in config.yaml deaktiviert werden
  (kein OTA nötig).

**Lesson:** Alle LED-Operationen im Hauptloop müssen non-blocking sein —
jeder blockierende Call verzögert direkt die Nutzerreaktion.

---

### 10. Knacken mitten in der Wiedergabe

**Symptom:** Kein Knacken am Anfang/Ende, aber hörbare Knack-Artefakte
während der Wiedergabe.

**Ursache:** `resample_poly()` erzeugt durch Filterringing Werte außerhalb
von [-32768, 32767]. `.astype(np.int16)` **wrappte** statt zu clippen —
ein Wert von 32800 wird zu -32736 → plötzlicher Amplitudensprung → Knack.

**Lösung:**
```python
samples = np.clip(resample_poly(samples, up, down), -32768, 32767).astype(np.int16)
```

---

## Volumen-Konfiguration (ohne OTA)

```yaml
# config.yaml — respeaker-Sektion:
respeaker:
  host: "respeaker-openclaw.local"
  volume: 0.8   # 0.0–1.0, wird beim Connect via API gesetzt
```

`RespeakerClient` ruft beim Start `media_player_command(key, volume=cfg.volume)`
auf. Änderung erfordert nur Neustart des Python-Prozesses, kein OTA.

---

## Flashen / OTA

```bash
# Initial (USB):
esphome-venv/bin/esphome run esphome/respeaker.yaml --device /dev/ttyACM0

# OTA:
esphome-venv/bin/esphome run esphome/respeaker.yaml --device respeaker-openclaw.local

# Config validieren:
esphome-venv/bin/esphome config esphome/respeaker.yaml
```

ESPHome-Venv ist getrennt vom `ow-venv` (verschiedene Abhängigkeiten).
`aioesphomeapi` gehört ins `ow-venv` (Voice-Assistant-Runtime).

---

## Profil starten

```bash
source ~/ow-venv/bin/activate
GASTON_PROFILE=clawdpi_rs python -m voice_assistant
```

Oder Hostname-Map in `config.yaml` → `hostname_map:` pflegen für automatische
Profil-Auswahl.
