"""Schatten-Aufnahme nach einem Near-Miss: was wurde NACH dem Fast-Ruf gesagt?

Wozu
----
Ob das Gate echte Rufe verliert, belegt bisher nur eine Handlung des Nutzers:
er wiederholt sich (Near-Miss, dem binnen Sekunden ein Trigger folgt —
``tools/wake_triage.py``, Regel 1). Ein verlorener Ruf, den niemand
wiederholt, taucht dort nie auf. Und der Near-Miss-Clip selbst hilft wenig:
er endet beim Wakewort, die STT bekommt ein einzelnes "Gaston" und macht
daraus "Gestalt", "Gasthof", "Kastor". Das eigentliche Indiz — der Auftrag
DANACH — wurde nie aufgenommen.

Anlass: am 2026-09-25 wurde der 1-Frame-Gate-Pfad abgeschaltet
(``min_peak_single`` 0.0). Ob das Rufe kostet, soll sich an genau diesen
Near-Misses zeigen (WAKEWORD_PROCESS.md, "Wette 1-Frame-Pfad aus").

Was passiert
------------
Nach jedem Near-Miss im Leerlauf werden still ``TAIL_SEC`` Sekunden
weiter mitgeschnitten, samt Pre-Roll (das Wakewort steckt darin, wie bei
einem echten Turn). Die Datei ``<id>_<bundle>_nearmiss_folge.wav`` liegt
neben dem Near-Miss-Clip. Danach, in einem eigenen Thread und erst wenn
kein Turn läuft: STT und die Aktuator-Torfrage. Ergebnis als eine Zeile in
``nearmiss_shadow.jsonl``.

Was NICHT passiert
------------------
Nichts wird ausgeführt, gesprochen, gespiegelt oder an den Brain geschickt.
Und das Ergebnis ist **kein Label**: ``wake_triage`` liest die Datei nicht.
STT und Torfrage sind hier eine vorläufige Einschätzung zum gemeinsamen
Auswerten und Anhören — ob sie als Label taugen (und mit welcher Frage:
"schalten?" übersieht Fragen an den Brain), ist erst noch zu messen.
Deshalb trägt jede Zeile ``"status": "vorlaeufig"``.

Die Schatten-STT benutzt ``SpeachesStt.transcribe_raw``: ohne Wirkung auf
den Cooldown-Zustand, sonst schickte ein Ausfall hier den nächsten echten
Turn auf die lokale Whisper-Instanz. Kein Fallback, kein Halluzinations-
Filter — ``no_speech_prob`` wird mitgeschrieben, verworfen wird nichts.
"""

from __future__ import annotations

import json
import os
import queue
import threading
import time
from datetime import datetime

import numpy as np

from voice_assistant.config import RATE_OW
from voice_assistant.services.stt import chunks_to_wav_bytes

# Länge des Mitschnitts NACH dem Near-Miss. Ein durchgesprochenes Kommando
# braucht im Archiv median ~5,8 s inkl. 1,5 s Pre-Roll (endpoint_replay,
# 2026-08-01); 6 s danach fassen es samt einer Denkpause.
TAIL_SEC = 6.0

# Wie lange die Auswertung auf das Ende eines laufenden Turns wartet, bevor
# sie aufgibt. Sie soll dem echten Turn weder Speaches noch die Vega-iGPU
# (Torfrage) streitig machen.
MAX_WARTEN_SEC = 300.0

STATUS = "vorlaeufig"


class NearMissShadow:
    """Ein Mitschnitt zur Zeit; ein neuer Near-Miss schließt den alten ab."""

    def __init__(self, stt, actuator, archive_dir: str, log_path: str,
                 turn_laeuft, tail_sec: float = TAIL_SEC) -> None:
        self._stt = stt
        self._actuator = actuator
        self._archive_dir = archive_dir
        self._log_path = log_path
        self._turn_laeuft = turn_laeuft
        self._tail_samples = int(RATE_OW * tail_sec)
        self._aktiv: dict | None = None
        self._queue: queue.Queue = queue.Queue(maxsize=50)
        threading.Thread(target=self._worker, name="nearmiss-shadow", daemon=True).start()

    @property
    def aktiv(self) -> bool:
        return self._aktiv is not None

    def start(self, event: dict, pre_roll: list) -> None:
        """event: die Near-Miss-Zeile aus wake_events.log (mit "audio")."""
        if self._aktiv is not None:
            self._abschliessen("neuer Near-Miss")
        self._aktiv = {
            "audio": event.get("audio") or "unbekannt_nearmiss.wav",
            "bundle": event.get("bundle", ""),
            "chunks": [c.copy() for c in pre_roll],
            "pre_samples": sum(len(c) for c in pre_roll),
            "tail_samples": 0,
            "event": event,
        }

    def feed(self, chunk: np.ndarray) -> None:
        a = self._aktiv
        if a is None or len(chunk) == 0:
            return
        a["chunks"].append(chunk.copy())
        a["tail_samples"] += len(chunk)
        if a["tail_samples"] >= self._tail_samples:
            self._abschliessen("voll")

    def abbrechen(self, grund: str) -> None:
        """Leerlauf verlassen (Trigger): mit dem Bisherigen abschließen.

        Folgt auf den Near-Miss ein Trigger, ist das der Wiederholungsfall —
        den Rest hat die Aufnahme des Turns, und das Label vergibt dort
        ohnehin wake_triage. Der Mitschnitt bleibt trotzdem erhalten.
        """
        if self._aktiv is not None:
            self._abschliessen(grund)

    def _abschliessen(self, ende: str) -> None:
        a, self._aktiv = self._aktiv, None
        # Gleicher Stamm wie der Near-Miss-Clip, damit beide nebeneinander liegen.
        datei = a["audio"].replace("_nearmiss.wav", "_nearmiss_folge.wav")
        try:
            os.makedirs(self._archive_dir, exist_ok=True)
            with open(os.path.join(self._archive_dir, datei), "wb") as f:
                f.write(chunks_to_wav_bytes(a["chunks"]))
        except Exception as e:
            print(f"⚠️  Near-Miss-Folge nicht gespeichert: {e}")
            datei = None
        job = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "status": STATUS,
            "nearmiss_audio": a["audio"],
            "folge_audio": datei,
            "bundle": a["bundle"],
            "hits": a["event"].get("hits"),
            "peak": a["event"].get("peak"),
            "failed_on": a["event"].get("failed_on"),
            "rms": a["event"].get("rms"),
            "pre_roll_sec": round(a["pre_samples"] / RATE_OW, 2),
            "folge_sec": round(a["tail_samples"] / RATE_OW, 2),
            "ende": ende,
        }
        try:
            self._queue.put_nowait((job, a["chunks"]))
        except queue.Full:
            job["fehler"] = "Warteschlange voll, nicht ausgewertet"
            self._schreiben(job)

    # --- Auswertung im Hintergrund ---------------------------------------

    def _worker(self) -> None:
        while True:
            job, chunks = self._queue.get()
            try:
                self._auswerten(job, chunks)
            except Exception as e:  # nie den Thread verlieren
                job["fehler"] = f"{type(e).__name__}: {e}"
            self._schreiben(job)

    def _auswerten(self, job: dict, chunks: list) -> None:
        t0 = time.time()
        while self._turn_laeuft():
            if time.time() - t0 > MAX_WARTEN_SEC:
                job["fehler"] = "Turn lief zu lange, nicht ausgewertet"
                return
            time.sleep(0.5)
        job["gewartet_sec"] = round(time.time() - t0, 1)

        if self._stt is None:
            job["fehler"] = "keine STT"
            return
        if not self._stt.state.stt_ok():
            job["fehler"] = "Speaches im Cooldown"
            return
        t1 = time.time()
        roh = self._stt.transcribe_raw(chunks_to_wav_bytes(chunks, normalize=True))
        job["stt_ms"] = round((time.time() - t1) * 1000)
        text = (roh.get("text") or "").strip()
        segmente = roh.get("segments") or []
        job["stt_text"] = text
        job["no_speech_prob"] = (
            round(sum(s.get("no_speech_prob", 0.0) for s in segmente) / len(segmente), 3)
            if segmente else None
        )

        if self._actuator is None or not text:
            return
        urteil = self._actuator.tor(text)
        job["tor_ja"] = urteil.ja
        job["tor_p_ja"] = round(urteil.p_ja, 4) if urteil.p_ja is not None else None
        job["tor_ms"] = round(urteil.ms)
        if getattr(urteil, "fehler", None):
            job["tor_fehler"] = urteil.fehler

    def _schreiben(self, job: dict) -> None:
        try:
            with open(self._log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(job, ensure_ascii=False) + "\n")
        except Exception as e:
            print(f"⚠️  nearmiss_shadow.jsonl nicht geschrieben: {e}")
            return
        text = job.get("stt_text")
        tor = job.get("tor_ja")
        print(f"👥 Near-Miss-Folge ({STATUS}): "
              f"{repr(text) if text else '—'}"
              f"{'' if tor is None else f'  Tor: {tor}'}"
              f"{'  ' + job['fehler'] if job.get('fehler') else ''}")
