"""Prüfung nach dem Trigger, im Schatten: hätte eine zweite Stufe verworfen?

Wozu
----
Das Wakewort-Modell allein kommt nicht stabil unter die FP/h-Schranke
(WAKEWORD_PROCESS.md, Runde 6 und die Trennläufe). Die Idee (Jochen,
2026-10-27): das Modell darf großzügig hören, Fehlauslöser sortiert eine
zweite Stufe danach aus. Offline gemessen an den v5-Triggern seit 2026-10-11:

- Fläche der Score-Kurve (Summe der Scores über der Schwelle): 12 von 15
  Fehlauslösern liegen unter 2,7, aber auch 13 von 64 echten Rufen.
- Qwen-ASR auf dem Wake-Clip, ohne Kontext: Median 0,09 s. Mit "Gaston" als
  Kontext hört es Gaston auch, wo keins war ("Was genau?" → "Gaston.").
- Beides zusammen (hohe Fläche geht durch, niedrige nur, wenn Qwen das Wort
  hört): 5 von 15 Fehlauslösern bleiben, 61 von 64 Rufen.

Das sind 15 Fehlauslöser und teils STT-Labels, also zu wenig für eine
Schranke. Deshalb erst der Schatten: gerechnet wird bei jedem Trigger,
geschrieben nach ``wake_pruefung.jsonl``, verworfen wird nichts. Die Fälle mit
``haette_verworfen`` hört Jochen an, dann wird gezählt.

Was NICHT passiert
------------------
Nichts am Turn ändert sich. Die Qwen-Anfrage läuft mit eigener Instanz und
ohne deren Zustand: ein Ausfall hier schickt keinen echten Turn in den
Cooldown. Die Text-Regel ist die von ``anrede.py`` über den ganzen Text (im
Wake-Clip steht das Wort am Ende); sie ist breit ("Gasthof", "Kastor" zählen),
der Text steht deshalb mit im Log, um engere Regeln offline nachzurechnen.
"""

from __future__ import annotations

import json
import queue
import threading
import time
from datetime import datetime

from voice_assistant.anrede import anrede_im_text
from voice_assistant.services.stt import chunks_to_wav_bytes

STATUS = "schatten"
# Der Clip ist 3 s lang, Qwen braucht ~0,1 s. Mehr Geduld hätte eine echte
# Schranke später ohnehin nicht.
TIMEOUT_SEC = 3.0


def flaeche(scores: list[float], threshold: float) -> float:
    """Summe der Scores über der Schwelle — Höhe und Dauer des Treffers."""
    return round(sum(s for s in scores if s > threshold), 3)


class WakePruefung:
    """Ein Thread, eine Warteschlange; jeder Trigger wird eine Zeile."""

    def __init__(self, stt, log_path: str, flaeche_grenze: float) -> None:
        self._stt = stt               # LlamaCppAsrStt oder None (dann nur Fläche)
        self._log_path = log_path
        self._grenze = flaeche_grenze
        self._queue: queue.Queue = queue.Queue(maxsize=20)
        threading.Thread(target=self._worker, name="wake-pruefung", daemon=True).start()

    def start(self, event: dict, chunks: list) -> None:
        """event: die Trigger-Zeile aus wake_events.log; chunks: der wake_ring."""
        job = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "status": STATUS,
            "audio": event.get("audio"),
            "bundle": event.get("bundle"),
            "hits": event.get("hits"),
            "peak": event.get("peak"),
            "flaeche": flaeche(event.get("scores") or [], float(event.get("threshold") or 0.0)),
            "flaeche_grenze": self._grenze,
        }
        try:
            self._queue.put_nowait((job, [c.copy() for c in chunks]))
        except queue.Full:
            job["fehler"] = "Warteschlange voll, nicht geprüft"
            self._schreiben(job)

    def _worker(self) -> None:
        while True:
            job, chunks = self._queue.get()
            try:
                self._pruefen(job, chunks)
            except Exception as e:  # nie den Thread verlieren
                job["fehler"] = f"{type(e).__name__}: {e}"
            self._schreiben(job)

    def _pruefen(self, job: dict, chunks: list) -> None:
        job["flaeche_ok"] = job["flaeche"] >= self._grenze
        if self._stt is None or not chunks:
            job["fehler"] = "keine STT" if self._stt is None else "kein Audio"
            return
        t0 = time.time()
        # _anfrage statt transcribe: kein Zustand, kein Log-Echo wie bei einem Turn.
        text = self._stt._anfrage(chunks_to_wav_bytes(chunks), None)
        job["qwen_ms"] = round((time.time() - t0) * 1000)
        job["text"] = text
        job["wort"] = anrede_im_text(text, job.get("bundle"), woerter=None)
        job["haette_verworfen"] = not (job["flaeche_ok"] or job["wort"])

    def _schreiben(self, job: dict) -> None:
        try:
            with open(self._log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(job, ensure_ascii=False) + "\n")
        except Exception as e:
            print(f"⚠️  wake_pruefung.jsonl nicht geschrieben: {e}")
