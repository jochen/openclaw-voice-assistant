#!/usr/bin/env python3
"""Timer-Sätze einsprechen und sofort durch STT und Aktuator-Ketten schicken.

Aufruf (Projekt-venv wird selbst gesucht):
    ow-venv/bin/python -m tools.timer_aufnahme --sprecher jochen          # aufnehmen
    ow-venv/bin/python -m tools.timer_aufnahme --sprecher jochen --ab 17  # ab Satz 17
    ow-venv/bin/python -m tools.timer_aufnahme auswerten                  # gespeicherte neu rechnen
    ow-venv/bin/python -m tools.timer_aufnahme auswerten --kontext-extra Timer
    ow-venv/bin/python -m tools.timer_aufnahme auswerten --ohne-kontext

Wozu
----
Der Timer-Parser (Küchentimer, Plan vom 2026-10-10) soll gegen das arbeiten,
was die STT wirklich schreibt — nicht gegen ausgedachte Sätze. Die Vorlagen
stehen in data/timer/de_saetze.jsonl (öffentlich, ausgedacht); die Aufnahmen
samt Transkripten sind Familienstimmen und landen in testsets/timer/
(gitignored, eigenes privates Git).

Je Satz wird aufgenommen wie im Live-Betrieb (Profil-Mikrofon, im
respeaker-Modus über den ReSpeaker; der Assistent wird dafür gestoppt, der
Mic-Strom ist exklusiv) und dann:

    Qwen3-ASR   mit demselben Kontext wie live (Wakewort + je Ziel der erste
                Name, assistant._stt_kontext) — die erste STT-Stufe
    medium      Speaches, der Rückfall und das Modell der Messwerkzeuge
    Laya/Gemma  die Aktuator-Ketten auf dem Qwen-Transkript, wie im Betrieb
                (aktuator_schatten.kette_laya / kette_gemma). Ein Timer-Satz,
                den der Parser NICHT erkennt, läuft live genau dort hinein —
                er darf dort nichts schalten.

Die WAV ist das Rohsignal; an die STT geht sie normalisiert, wie live
(SttPipeline, normalize=True).

`auswerten` rechnet alle gespeicherten Aufnahmen neu, z. B. mit einem
zusätzlichen Kontextwort für Qwen (--kontext-extra Timer) — so lässt sich
messen, ob "Timer" im Kontext die Schreibweise stabilisiert.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import time
import wave
from datetime import datetime

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_VENV = os.path.join(_REPO, "ow-venv", "bin", "python")
if os.path.exists(_VENV) and os.path.realpath(sys.executable) != os.path.realpath(_VENV):
    os.execv(_VENV, [_VENV, "-m", "tools.timer_aufnahme", *sys.argv[1:]])

import numpy as np  # noqa: E402
import webrtcvad  # noqa: E402

from voice_assistant.config import RATE_OW, load_profile  # noqa: E402
from voice_assistant.services import aktuator_schatten  # noqa: E402
from voice_assistant.services.actuator import Actuator  # noqa: E402
from voice_assistant.services.aktuator_schatten import ausgang  # noqa: E402
from voice_assistant.services.speaches import SpeachesState  # noqa: E402
from voice_assistant.services.stt import (  # noqa: E402
    LlamaCppAsrStt, SpeachesStt, chunks_to_wav_bytes,
)
from wakeword_studio import recorder as rec  # noqa: E402
from voice_assistant.services.leds import LED_IDLE, LED_RECORDING, LedDirector  # noqa: E402

_SAETZE = os.path.join(_REPO, "data", "timer", "de_saetze.jsonl")
_ZIEL = os.path.join(_REPO, "testsets", "timer")
_LOG = os.path.join(_ZIEL, "aufnahmen.jsonl")

# Ganze Sätze statt Wakewort: längerer Deckel, Nachlauf wie das
# Kommando-Endpointing (command_silence_seconds 1,0 s) plus etwas Luft,
# damit "Timer ... zwei Minuten dreißig" nicht in der Pause abreißt.
MAX_SPEECH_SEC = 12.0
END_SILENCE_SEC = 1.3


def _saetze() -> list[dict]:
    with open(_SAETZE, encoding="utf-8") as f:
        return [json.loads(z) for z in f if z.strip()]


def _qwen_kontext(akt: Actuator, profil, extra: str | None) -> str | None:
    """Wie assistant._stt_kontext: Wakewort + je Ziel der erste Name."""
    if not akt.ziele:
        return None
    namen: list[str] = []
    for z in akt.ziele:
        n = (z.get("namen") or [None])[0]
        if n and n not in namen:
            namen.append(n)
    wort = profil.wakewords[0].bundle.replace("_", " ").title()
    if extra:
        namen.append(extra)
    return f"{wort}, " + ", ".join(namen) + "."


class Rechner:
    """STT + Aktuator-Ketten, einmal aufgebaut, je Aufnahme aufgerufen."""

    def __init__(self, profil, kontext_extra: str | None = None, ohne_kontext: bool = False) -> None:
        self.profil = profil
        self.akt = Actuator(profil.actuator)
        if not self.akt.refresh():
            print("⚠️  capabilities-refresh fehlgeschlagen — Qwen ohne Kontext, keine Ketten")
            self.akt = None
        else:
            self.akt.aufwaermen()
        self.qwen = None
        if profil.stt_llamacpp_url:
            kontext = ((lambda: _qwen_kontext(self.akt, profil, kontext_extra))
                       if self.akt and not ohne_kontext else None)
            self.qwen = LlamaCppAsrStt(profil.stt_llamacpp_url, kontext=kontext)
        self.medium = SpeachesStt(SpeachesState(), profil.speaches_base,
                                  profil.speaches_stt_model)

    def rechne(self, samples: np.ndarray) -> dict:
        wav = chunks_to_wav_bytes([samples.astype(np.int16)], normalize=True)
        erg: dict = {}
        for name, stt in (("qwen", self.qwen), ("medium", self.medium)):
            if stt is None:
                erg[name] = None
                continue
            t0 = time.time()
            try:
                erg[name] = stt.transcribe(wav)
            except Exception as e:  # Werkzeug: weiter mit dem nächsten Satz
                erg[name] = None
                erg[f"{name}_fehler"] = f"{type(e).__name__}: {e}"
            erg[f"{name}_ms"] = round((time.time() - t0) * 1000)
        text = erg.get("qwen") or erg.get("medium")
        if self.akt and text:
            for wer, kette in (("laya", lambda t: aktuator_schatten.kette_laya(self.akt, t, timeout=10)),
                               ("gemma", lambda t: aktuator_schatten.kette_gemma(self.akt, t))):
                if wer == "laya" and not self.akt.cfg.laya_url:
                    continue
                try:
                    e = kette(text)
                    erg[wer] = ausgang(e.intent, e.verdict)
                except Exception as ex:
                    erg[wer] = f"Fehler: {type(ex).__name__}: {ex}"
        return erg


def _zeige(erg: dict) -> None:
    print(f"   Qwen:   {erg.get('qwen')!r}  ({erg.get('qwen_ms')} ms)")
    print(f"   medium: {erg.get('medium')!r}  ({erg.get('medium_ms')} ms)")
    if "laya" in erg or "gemma" in erg:
        warn = lambda a: "" if a in (None, "Brain", "Rückfrage") or str(a).startswith(("Ausfall", "Fehler", "kein")) else "   ⚠️ würde SCHALTEN"
        for wer in ("laya", "gemma"):
            if wer in erg:
                print(f"   {wer:<6}  {erg[wer]}{warn(erg[wer])}")


def _sprecher(name: str) -> str:
    return "".join(c for c in name.strip().lower().replace(" ", "-") if c.isalnum() or c in "-_")


def aufnehmen(args) -> int:
    profil = load_profile()
    sprecher = _sprecher(args.sprecher)
    if not sprecher:
        print("❌ --sprecher fehlt oder ist ungültig")
        return 1
    saetze = _saetze()
    ordner = os.path.join(_ZIEL, sprecher)
    os.makedirs(ordner, exist_ok=True)

    print("🔧 Lade capabilities, wärme Gemma vor, verbinde STT …")
    rechner = Rechner(profil)

    print(f"\n🎙  Timer-Sätze — {len(saetze)} Vorlagen, Sprecher {sprecher}, ab Nr. {args.ab}\n"
          f"    Profil {profil.name} ({profil.mode}) → {ordner}/\n"
          f"    Sag den Satz so, wie du ihn in der Küche sagen würdest — mit\n"
          f"    \"Gaston\" vorneweg. Eigene Formulierung ist erwünscht (dann 'e').\n")

    was_active = rec._service_active()
    if was_active:
        print(f"⏸  Stoppe {rec.SERVICE_UNIT} (Mic-Stream ist exklusiv) …")
        rec._service_ctl("stop")

    source = None
    leds = LedDirector()
    try:
        source = rec._make_source(profil)
        print("⏳ Warte auf Audio-Stream …")
        if not rec._wait_for_stream(source):
            print("❌ Kein Audio vom Mikrofon")
            return 1
        leds = rec._make_leds(profil)
        leds.set_phase(LED_IDLE)
        print("🤫 Bitte kurz still sein — messe den Grundpegel …")
        noise = rec._measure_noise(source)
        min_rms = max(profil.vad_voice_rms_min, noise * rec.NOISE_RMS_FACTOR, rec.NOISE_RMS_FLOOR)
        print(f"✅ Grundpegel RMS≈{noise:.0f} → Sprach-Schwelle {min_rms:.0f}\n")
        vad = webrtcvad.Vad(profil.vad_aggressiveness)

        i = args.ab - 1
        while i < len(saetze):
            vorlage = saetze[i]
            print(f"── {i + 1}/{len(saetze)}  »{vorlage['satz']}«")
            if vorlage.get("hinweis"):
                print(f"   ({vorlage['hinweis']})")
            try:
                eingabe = input("   [Enter] = aufnehmen   s = überspringen   q = Ende: ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                break
            if eingabe == "q":
                break
            if eingabe == "s":
                i += 1
                print()
                continue

            time.sleep(rec.ARM_DELAY_SEC)
            leds.set_phase(LED_RECORDING)
            print("   🔴 Jetzt sprechen …")
            take = rec._record_take(source, vad, min_rms,
                                    max_speech_sec=MAX_SPEECH_SEC, end_silence_sec=END_SILENCE_SEC)
            leds.set_phase(LED_IDLE)
            if take is None:
                print("   ⚠️  Keine Sprache erkannt — nochmal.\n")
                continue
            samples, sprache_s = take
            erg = rechner.rechne(samples)
            _zeige(erg)
            try:
                wahl = input("   [Enter] = behalten   e = behalten, eigene Formulierung"
                             "   w = wiederholen   q = behalten + Ende: ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                wahl = "q"
            if wahl == "w":
                print("   ↩  Verworfen.\n")
                continue

            ts = datetime.now().strftime("%Y%m%d-%H%M%S")
            datei = f"{i + 1:02d}_{ts}.wav"
            rec._save_wav(os.path.join(ordner, datei), samples)
            zeile = {
                "ts": datetime.now().isoformat(timespec="seconds"),
                "nr": i + 1, "vorlage": vorlage["satz"], "erwartet": vorlage["erwartet"],
                "eigene_formulierung": wahl == "e",
                "sprecher": sprecher, "file": os.path.join(sprecher, datei),
                "dur_s": round(len(samples) / RATE_OW, 2), "sprache_s": round(sprache_s, 2),
                "min_rms": round(min_rms, 1), "profil": profil.name, "host": socket.gethostname(),
                **erg,
            }
            with open(_LOG, "a", encoding="utf-8") as f:
                f.write(json.dumps(zeile, ensure_ascii=False) + "\n")
            print()
            i += 1
            if wahl == "q":
                break
    finally:
        try:
            leds.set_phase(LED_IDLE)
        except Exception:
            pass
        if source is not None:
            source.close()
        if was_active:
            print(f"▶️  Starte {rec.SERVICE_UNIT} wieder …")
            rec._service_ctl("start")
    print(f"\n📄 {_LOG}\n   Neu rechnen: ow-venv/bin/python -m tools.timer_aufnahme auswerten")
    return 0


def _lies_wav(pfad: str) -> np.ndarray:
    with wave.open(pfad, "rb") as wf:
        return np.frombuffer(wf.readframes(wf.getnframes()), dtype=np.int16)


def auswerten(args) -> int:
    if not os.path.exists(_LOG):
        print(f"❌ {_LOG} fehlt — erst aufnehmen")
        return 1
    with open(_LOG, encoding="utf-8") as f:
        zeilen = [json.loads(z) for z in f if z.strip()]
    rechner = Rechner(load_profile(), kontext_extra=args.kontext_extra, ohne_kontext=args.ohne_kontext)
    aus = []
    for z in zeilen:
        pfad = os.path.join(_ZIEL, z["file"])
        if not os.path.exists(pfad):
            print(f"⚠️  fehlt: {pfad}")
            continue
        erg = rechner.rechne(_lies_wav(pfad))
        print(f"── {z['nr']:>2}  »{z['vorlage']}«" + ("  [eigene Formulierung]" if z.get("eigene_formulierung") else ""))
        _zeige(erg)
        aus.append({**{k: z[k] for k in ("nr", "vorlage", "erwartet", "file") if k in z},
                    "kontext_extra": args.kontext_extra, "ohne_kontext": args.ohne_kontext, **erg})
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            for a in aus:
                f.write(json.dumps(a, ensure_ascii=False) + "\n")
        print(f"\n📄 {args.json}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("modus", nargs="?", default="aufnehmen", choices=("aufnehmen", "auswerten"))
    ap.add_argument("--sprecher", default="", help="Name des Sprechers (aufnehmen)")
    ap.add_argument("--ab", type=int, default=1, help="mit Satz Nr. N beginnen (1-basiert)")
    ap.add_argument("--kontext-extra", default=None,
                    help="auswerten: zusätzliches Wort im Qwen-Kontext (z. B. Timer)")
    ap.add_argument("--ohne-kontext", action="store_true",
                    help="auswerten: Qwen ohne Kontext (Gegenprobe zum Live-Kontext)")
    ap.add_argument("--json", default=None, help="auswerten: Ergebnis als JSONL schreiben")
    args = ap.parse_args()
    return aufnehmen(args) if args.modus == "aufnehmen" else auswerten(args)


if __name__ == "__main__":
    sys.exit(main())
