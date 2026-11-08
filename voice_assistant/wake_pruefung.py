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

Anfangs-Maß (``anfang``, seit 2026-10-31)
----------------------------------------
Die Fläche ist kein unabhängiger Beleg (dasselbe Modell, das ausgelöst hat),
und die Wortregel scheitert an Schreibvarianten: ein echter Ruf kam als
"Ganz toll. Also.". Deshalb wird Qwen zusätzlich gefragt, wie gut der
Wortanfang passt (hier "Gast"): an jeder Wortgrenze des freien Textes wird er
erzwungen (``LlamaCppAsrStt.anfang_logprob``), das Maximum steht als
``anfang_lp`` im Log. Nur der Anfang, weil Qwen bei echten Rufen oft sicher
"Gasthof"/"Gastor" hört; das volle "Gaston" bekam dann −20 bis −40.
``haette_verworfen_anfang`` ist das Urteil dieses Maßes allein,
``haette_verworfen`` bleibt die alte Regel.

Gemessen an den R6-Val-Clips (Original-3-s-Wake-Clips aus dem Korpus,
2026-10-31, mit genau diesem Code): echte Rufe Wortregel 190/223, Anfang ≥ −4
205/223; per Ohr gelabelte Fehlauslöser beide 0/23, bester −8,5. Median
0,31 s je Clip (p90 0,53). Die verhaltens-gelabelten Negativen des Pakets
taugen als Maßstab nicht: darunter sind Barge-in-Clips mit einem echten
"Stopp, Gaston" (Selbst-Label "abgebrochen" auf dem falschen Clip). Die
Grenze −4 ist am Korpus abgelesen, nicht an neuem Material geprüft.

Schranke (``schranke: true``, seit 2026-11-08)
---------------------------------------------
Erste Live-Woche mit Ohr-Labels (R7 voll l2, 2026-10-31 bis 11-07, 74
geprüfte Trigger): echte Rufe lagen alle bei ≥ −5,05, per Ohr gelabelte
Fehlauslöser alle bei ≤ −8,2. Mit −4 hätte das Maß alle 13 Fehlauslöser der
Woche verworfen, aber auch 2 von 48 echten Rufen ("Kasten." −5,05, "Das
doch." −4,49). Die Grenze steht deshalb in der Lücke (Profil, hier −6,5).

Mit der Schranke entscheidet nur das Anfangs-Maß, die alte Regel (Fläche,
Wort) bleibt reines Log. Der Turn wartet nicht: die Aufnahme läuft, während
Qwen rechnet (Median 0,43 s, p90 0,72 s), nur "Ja?" und das Ende der
Aufnahme warten auf das Urteil (``entscheid``). Ein Ausfall von Qwen oder
eine überschrittene Frist lässt durch, die Schranke darf keine Rufe kosten,
nur weil ein Dienst fehlt. Verworfene Trigger stehen in wake_events.log als
Near-Miss mit ``failed_on: "anfang"``.
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
STATUS_SCHRANKE = "schranke"
# So lange wartet die Aufnahme höchstens auf das Urteil, danach durch.
# Gemessen max 1,27 s über 74 Trigger.
SCHRANKE_FRIST_SEC = 2.0

# Ergebnisse von entscheid()
WARTEN, DURCH, VERWORFEN, FRIST, AUSFALL = "warten", "durch", "verworfen", "frist", "ausfall"
# Der Clip ist 3 s lang, Qwen braucht ~0,1 s. Mehr Geduld hätte eine echte
# Schranke später ohnehin nicht.
TIMEOUT_SEC = 3.0
# Höchstens so viele Wortgrenzen für das Anfangs-Maß (je eine Anfrage, ~0,05 s).
# Ein 3-s-Clip hat selten mehr als zehn Wörter.
ANFANG_MAX_STELLEN = 15


def flaeche(scores: list[float], threshold: float) -> float:
    """Summe der Scores über der Schwelle — Höhe und Dauer des Treffers."""
    return round(sum(s for s in scores if s > threshold), 3)


def anfang_mass(stt, wav: bytes, text: str, anfang: str) -> tuple[float, int]:
    """Bestes log P(Wort beginnt mit ``anfang``) über die Wortgrenzen von ``text``.

    -> (Wert, Stelle).

    Stelle = Zahl der Wörter davor. Das Wort steht im Wake-Clip meist am
    Ende, aber nicht immer ("spiel ein bisschen Gastor.").
    """
    woerter = (text or "").split()[:ANFANG_MAX_STELLEN - 1]
    werte = [stt.anfang_logprob(wav, " ".join(woerter[:i]), anfang)
             for i in range(len(woerter) + 1)]
    beste = max(range(len(werte)), key=werte.__getitem__)
    return round(werte[beste], 3), beste


class Urteil:
    """Handle auf eine laufende Prüfung; ``job`` füllt der Worker."""

    def __init__(self, job: dict) -> None:
        self.job = job
        self.fertig = threading.Event()


def entscheid(urteil: Urteil, frist_um: bool) -> str:
    """WARTEN, DURCH, VERWORFEN, oder FRIST/AUSFALL (beide lassen durch)."""
    if not urteil.fertig.is_set():
        return FRIST if frist_um else WARTEN
    v = urteil.job.get("haette_verworfen_anfang")
    if v is None:
        return AUSFALL
    return VERWORFEN if v else DURCH


def woerter_nach_wakewort(job: dict) -> int | None:
    """Wörter in Qwens Text des Wake-Clips hinter dem Wort mit dem Anfang.

    None ohne Anfangs-Maß. Gebraucht für das verspätete "Ja?": stehen hier
    schon Wörter, war der Befehl gesagt, bevor der Trigger feuerte.
    """
    if "anfang_nach" not in job:
        return None
    woerter = (job.get("text") or "").split()
    return max(0, len(woerter) - job["anfang_nach"] - 1)


class WakePruefung:
    """Ein Thread, eine Warteschlange; jeder Trigger wird eine Zeile."""

    def __init__(self, stt, log_path: str, flaeche_grenze: float,
                 anfang: str = "", anfang_grenze: float = -4.0,
                 schranke: bool = False) -> None:
        self._stt = stt               # LlamaCppAsrStt oder None (dann nur Fläche)
        self._log_path = log_path
        self._grenze = flaeche_grenze
        self._anfang = anfang         # leer: Anfangs-Maß aus
        self._anfang_grenze = anfang_grenze
        # Nur ein Vermerk im Log: entscheiden tut assistant.py über entscheid().
        self.schranke = schranke and bool(anfang)
        self._queue: queue.Queue = queue.Queue(maxsize=20)
        threading.Thread(target=self._worker, name="wake-pruefung", daemon=True).start()

    def start(self, event: dict, chunks: list) -> Urteil:
        """event: die Trigger-Zeile aus wake_events.log; chunks: der wake_ring."""
        job = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "status": STATUS_SCHRANKE if self.schranke else STATUS,
            "audio": event.get("audio"),
            "bundle": event.get("bundle"),
            "hits": event.get("hits"),
            "peak": event.get("peak"),
            "flaeche": flaeche(event.get("scores") or [], float(event.get("threshold") or 0.0)),
            "flaeche_grenze": self._grenze,
        }
        urteil = Urteil(job)
        try:
            self._queue.put_nowait((urteil, [c.copy() for c in chunks]))
        except queue.Full:
            job["fehler"] = "Warteschlange voll, nicht geprüft"
            self._schreiben(job)
            urteil.fertig.set()
        return urteil

    def _worker(self) -> None:
        while True:
            urteil, chunks = self._queue.get()
            job = urteil.job
            try:
                self._pruefen(job, chunks)
            except Exception as e:  # nie den Thread verlieren
                job["fehler"] = f"{type(e).__name__}: {e}"
            urteil.fertig.set()     # vor dem Schreiben: die Aufnahme wartet darauf
            self._schreiben(job)

    def _pruefen(self, job: dict, chunks: list) -> None:
        job["flaeche_ok"] = job["flaeche"] >= self._grenze
        if self._stt is None or not chunks:
            job["fehler"] = "keine STT" if self._stt is None else "kein Audio"
            return
        t0 = time.time()
        # _anfrage statt transcribe: kein Zustand, kein Log-Echo wie bei einem Turn.
        wav = chunks_to_wav_bytes(chunks)
        text = self._stt._anfrage(wav, None)
        job["qwen_ms"] = round((time.time() - t0) * 1000)
        job["text"] = text
        job["wort"] = anrede_im_text(text, job.get("bundle"), woerter=None)
        job["haette_verworfen"] = not (job["flaeche_ok"] or job["wort"])
        if self._anfang and getattr(self._stt, "sprache", None):
            try:
                t0 = time.time()
                lp, nach = anfang_mass(self._stt, wav, text, self._anfang)
                job.update(anfang=self._anfang, anfang_lp=lp, anfang_nach=nach,
                           anfang_ms=round((time.time() - t0) * 1000),
                           anfang_grenze=self._anfang_grenze,
                           haette_verworfen_anfang=lp < self._anfang_grenze)
            except Exception as e:  # das alte Urteil bleibt stehen
                job["anfang_fehler"] = f"{type(e).__name__}: {e}"

    def _schreiben(self, job: dict) -> None:
        try:
            with open(self._log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(job, ensure_ascii=False) + "\n")
        except Exception as e:
            print(f"⚠️  wake_pruefung.jsonl nicht geschrieben: {e}")
