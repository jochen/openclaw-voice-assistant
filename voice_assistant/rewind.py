"""Rückspul-Puffer: die letzten Minuten Mikrofon — samt Score-Verlauf.

Wozu
----
Near-Miss und Schatten-Aufnahme (``nearmiss_shadow.py``) sehen nur Rufe, bei
denen das Modell wenigstens die Schwelle gestreift hat. Am 2026-09-27
meldete Jochen Rufe, auf die Gaston gar nicht reagierte — auch nicht mit
einem Near-Miss. Solche Rufe hinterlassen bisher nichts: kein Clip, kein
Score, keine Zeile.

Dieser Puffer hält die letzten ``seconds`` Sekunden Audio im RAM (16 kHz
mono int16, 120 s ≈ 4 MB) und daneben JEDEN Score, den eine Wakeword-Engine
dazu geliefert hat. Auf die Platte kommt davon nur etwas bei einem Anlass:

* **Trigger** — die ``before_trigger_seconds`` davor. Wer nach vergeblichen
  Versuchen doch durchkommt, hat die Versuche genau hier drin. Das ist
  wake_triages Wiederholungs-Regel, erweitert auf Versuche ohne Near-Miss.
* **Marker** — ein manueller Anlass (Zigbee-Taster per MQTT): der ganze
  Puffer. Das Dateiende ist der Moment des Tastendrucks; laut Jochen vergehen
  vom "er hat nicht reagiert" bis zum Druck grob 1–4 s, der verlorene Ruf
  liegt also wenige Sekunden vor dem Ende — nicht irgendwo in 120 s.

Der Score-Verlauf ist der eigentliche Gewinn. An ihm ist beim Anhören
ablesbar, woran ein Ruf scheiterte: Score nahe 0 (Modell hört "Gaston"
nicht → Nachtraining), knapp unter der Schwelle (Schwelle), oder eine
Zeitlücke im Audio (Mikro-Strom war weg — dann kein Modellproblem).

Was NICHT passiert
------------------
Nichts wird bewertet oder gelabelt. Jede Zeile im Index trägt
``"status": "vorlaeufig"``; ausgewertet wird gemeinsam und mit dem Ohr.

Datenschutz: Der Trigger-Anlass speichert regelmäßig Raumgespräch (30 s vor
jedem Trigger), deutlich mehr als die ~3-s-Clips. Es bleibt im lokalen
Archiv und fällt mit ihm nach TRIGGER_AUDIO_MAX_AGE_DAYS weg.

Dateien
-------
``<stamm>.wav`` im Trigger-Archiv plus ``<stamm>.json`` daneben (Scores,
Zustände, Zeitlücken, Pegel je Sekunde). Alle Positionen darin sind
Sekunden **in der WAV-Datei**, nicht Wanduhrzeit — Lücken im Mikro-Strom
stehen separat unter ``luecken``.
"""

from __future__ import annotations

import json
import os
import queue
import threading
from collections import deque
from datetime import datetime

import numpy as np

from voice_assistant.config import RATE_OW
from voice_assistant.services.stt import chunks_to_wav_bytes

STATUS = "vorlaeufig"

# Zwei Chunks, zwischen denen mehr Wanduhrzeit vergeht als Audio, deuten auf
# einen stockenden oder abgerissenen Mikro-Strom. 0,5 s liegt weit über dem
# normalen Jitter (Chunks von 27–80 ms).
LUECKE_SEC = 0.5


class RewindBuffer:
    """Nur die Hauptschleife füttert; gesichert wird im Hintergrund."""

    def __init__(self, seconds: float, archive_dir: str, index_path: str) -> None:
        self._max_samples = int(RATE_OW * seconds)
        self._archive_dir = archive_dir
        self._index_path = index_path
        self._audio: deque = deque()   # (pos, t, zustand, chunk)
        self._scores: deque = deque()  # (pos, score, quelle)
        self._samples = 0
        self._pos = 0                  # fortlaufender Sample-Zähler
        self._marker: queue.Queue = queue.Queue()

    # --- Füttern (Hauptschleife) -----------------------------------------

    def feed(self, t: float, zustand: str, chunk: np.ndarray) -> None:
        if len(chunk) == 0:
            return
        self._audio.append((self._pos, t, zustand, chunk.copy()))
        self._pos += len(chunk)
        self._samples += len(chunk)
        while self._samples > self._max_samples and len(self._audio) > 1:
            self._samples -= len(self._audio.popleft()[3])
        erster = self._audio[0][0]
        while self._scores and self._scores[0][0] < erster:
            self._scores.popleft()

    def score(self, score: float, quelle: str) -> None:
        """Score zum zuletzt gefütterten Chunk (quelle: leerlauf | bargein)."""
        self._scores.append((self._pos, float(score), quelle))

    # --- Sichern ---------------------------------------------------------

    def sichern(self, anlass: str, stamm: str, seconds: float | None = None,
                extra: dict | None = None) -> None:
        """Schnappschuss jetzt, Schreiben im Hintergrund (blockiert nicht)."""
        audio = list(self._audio)
        if seconds is not None and audio:
            grenze = self._pos - int(RATE_OW * seconds)
            audio = [a for a in audio if a[0] + len(a[3]) > grenze]
        if not audio:
            return
        scores = [s for s in self._scores if s[0] > audio[0][0]]
        threading.Thread(
            target=self._schreiben,
            args=(anlass, stamm, audio, scores, extra or {}),
            name="rewind-sichern", daemon=True,
        ).start()

    def _schreiben(self, anlass, stamm, audio, scores, extra) -> None:
        try:
            meta = auswerten(audio, scores)
            os.makedirs(self._archive_dir, exist_ok=True)
            wav = os.path.join(self._archive_dir, f"{stamm}.wav")
            with open(wav, "wb") as f:
                f.write(chunks_to_wav_bytes([a[3] for a in audio]))
            meta.update({"anlass": anlass, "status": STATUS, **extra})
            with open(os.path.join(self._archive_dir, f"{stamm}.json"), "w",
                      encoding="utf-8") as f:
                json.dump(meta, f, ensure_ascii=False)
            zeile = {
                "ts": datetime.now().isoformat(timespec="seconds"),
                "status": STATUS,
                "anlass": anlass,
                "audio": f"{stamm}.wav",
                "dauer_sec": meta["dauer_sec"],
                "score_max": meta["score_max"],
                "luecken": len(meta["luecken"]),
                **extra,
            }
            with open(self._index_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(zeile, ensure_ascii=False) + "\n")
            print(f"⏪ Rückspul gesichert ({anlass}): {stamm}.wav, "
                  f"{meta['dauer_sec']:.0f} s, Score max {meta['score_max']:.2f}"
                  f"{', ' + str(len(meta['luecken'])) + ' Lücke(n)' if meta['luecken'] else ''}")
        except Exception as e:
            print(f"⚠️  Rückspul ({anlass}) nicht gesichert: {e}")

    # --- Manueller Marker (MQTT) -----------------------------------------

    def marker_abholen(self) -> list[dict]:
        """Seit dem letzten Aufruf eingegangene Marker (Hauptschleife)."""
        out = []
        while True:
            try:
                out.append(self._marker.get_nowait())
            except queue.Empty:
                return out

    def marker_starten(self, host: str, port: int, topic: str) -> None:
        """MQTT-Client für den Marker. Der Callback füllt NUR eine Queue —
        der Puffer gehört der Hauptschleife."""
        try:
            import paho.mqtt.client as mqtt
        except ImportError:
            print("⚠️  Rückspul: paho-mqtt fehlt — kein Marker")
            return

        def on_connect(client, userdata, flags, reason_code, properties=None):
            client.subscribe(topic)
            print(f"⏪ Rückspul-Marker verbunden ({host}:{port}, {topic})")

        def on_message(client, userdata, msg):
            if msg.retain:
                return  # alter Zustand beim Verbinden, kein Tastendruck
            try:
                payload = json.loads(msg.payload.decode())
            except (ValueError, UnicodeDecodeError):
                return
            action = payload.get("action") if isinstance(payload, dict) else None
            if action:
                self._marker.put({"action": str(action), "topic": msg.topic})

        try:
            client = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2)
            client.on_connect = on_connect
            client.on_message = on_message
            client.reconnect_delay_set(min_delay=1, max_delay=30)
            client.connect_async(host, port, keepalive=60)
            client.loop_start()
        except Exception as e:
            print(f"⚠️  Rückspul-Marker: MQTT-Start fehlgeschlagen: {e}")


def auswerten(audio: list, scores: list) -> dict:
    """Metadaten zu einem Schnappschuss — Positionen in WAV-Sekunden."""
    p0 = audio[0][0]
    t0 = audio[0][1]

    def sek(pos: int) -> float:
        return round((pos - p0) / RATE_OW, 3)

    luecken = []
    zustaende = []
    for i, (pos, t, zustand, chunk) in enumerate(audio):
        if not zustaende or zustaende[-1][1] != zustand:
            zustaende.append([sek(pos), zustand])
        if i:
            vpos, vt, _, vchunk = audio[i - 1]
            erwartet = len(vchunk) / RATE_OW
            if t - vt - erwartet > LUECKE_SEC:
                luecken.append([sek(pos), round(t - vt - erwartet, 2)])

    samples = np.concatenate([a[3] for a in audio]).astype(np.float32)
    pegel = [
        round(float(np.sqrt(np.mean(samples[i:i + RATE_OW] ** 2))))
        for i in range(0, len(samples), RATE_OW)
    ]
    werte = [s[1] for s in scores]
    return {
        "beginn": datetime.fromtimestamp(t0).isoformat(timespec="seconds"),
        "dauer_sec": round(len(samples) / RATE_OW, 2),
        "wanduhr_sec": round(audio[-1][1] - t0, 2),
        "score_max": round(max(werte), 3) if werte else 0.0,
        "scores": [[sek(p), round(s, 3), q] for p, s, q in scores],
        "zustaende": zustaende,
        "luecken": luecken,
        "pegel_pro_sekunde": pegel,
    }
