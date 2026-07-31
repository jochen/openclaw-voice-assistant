"""Hauptloop und State-Machine."""

from __future__ import annotations

import json
import os
import queue
import re
import time
import uuid
from collections import deque
from datetime import datetime

import numpy as np
import webrtcvad

from voice_assistant.audio.alsa import AlsaSink, AlsaSource
from voice_assistant.audio.respeaker import RespeakerSink, RespeakerSource
from voice_assistant.config import (
    ACTUATOR_LOG_PATH,
    DIARIZATION_JOIN_TIMEOUT,
    ENDPOINT_LOG_PATH,
    FOLLOWUP_BEEP_PATH,
    LAST_RECORDING_PATH,
    MAX_FOLLOWUP_ROUNDS,
    MIN_SPEECH_CHUNKS,
    OPENCLAW_OVERALL_TIMEOUT,
    PIPER_OUT,
    RATE_OW,
    RECORDING_MAX_SEC,
    SILENCE_CHUNKS_LIMIT,
    TRIGGER_AUDIO_DIR,
    TRIGGER_AUDIO_MAX_AGE_DAYS,
    VAD_FRAME_SIZE,
    VOICE_ANALYSIS_BASE,
    VOICE_DIR,
    WAKE_LOG_PATH,
    WORKSPACE,
    Profile,
    WakewordConfig,
    load_profile,
)
from voice_assistant.services import speaches as speaches_mod
from voice_assistant.services.actuator import Actuator
from voice_assistant.services.diarization import SpeachesDiarizer
from voice_assistant.services.mood import MoodAnalyzer
from voice_assistant.services.enroll_server import start_enroll_server
from voice_assistant.services.speak_server import start_announce_worker, start_speak_server
from voice_assistant.services.voice_control import VoiceController
from voice_assistant.services.leds import (
    LED_BOOT, LED_IDLE, LED_WAKEWORD, LED_RECORDING,
    LED_STT, LED_CONFIRMATION, LED_ERROR, LED_NEAR_MISS, LED_FOLLOWUP, LED_END,
    LedDirector, RespeakerRing, WledLeds,
)
from voice_assistant.services.speaches import SpeachesState
from voice_assistant.services.stt import LocalWhisperStt, SpeachesStt, SttPipeline, chunks_to_wav_bytes
from voice_assistant.services.tts import (
    ReplySpeaker,
    SpeachesTts,
    ThinkingWorker,
    prerender_followup_beep,
    prerender_ja,
    rerender_ja_speaches,
)
from voice_assistant.state import (
    STATE_FOLLOWUP,
    STATE_LISTENING,
    STATE_PAUSE,
    STATE_PROCESSING,
    STATE_RECORDING,
    STATE_WAITING,
    current_state,
    pending_reply_text,
    reply_done_event,
    voice_state,
)
from voice_assistant.wakeword.openwakeword_engine import OpenWakewordEngine
from voice_assistant.wakeword.respeaker import RespeakerWakeword
from voice_assistant.workers import Workers


_STOP_CORE = r'(stopp?|halt|aus|abbrechen)'

# Ein-Satz-Kommando: nach dem Trigger wird das "Ja?" nicht sofort gespielt,
# sondern ACK_DELAY_SEC lang der VAD beobachtet. Wird weitergesprochen, kommt
# gar kein "Ja?" (LED-Ring reicht als Rückmeldung). Bleibt es still, wird
# "Ja?" gespielt wie bisher. Zusammen mit _PRE_ROLL_SEC ergibt das den
# Ein-Satz-Betrieb: die LED quittiert sofort, der Anfang geht nicht verloren.
_ACK_DELAY_SEC = 0.4
# Sprache (Summe, nicht am Stück), die eine Aufnahme enthalten muss, bevor der
# straffe Kommando-Nachlauf greifen darf. Der Ausklang des Wakewords liefert
# typisch 1–2 Chunks (~0,16 s) — 0,5 s liegt sicher darüber und ist von jedem
# echten Kommando nach einem halben Wort erreicht.
_COMMAND_MIN_SPEECH_SEC = 0.5

# Pre-Roll: so viel Audio VOR dem Trigger wird der Aufnahme vorangestellt.
#
# Der Trigger fällt rund 0,4 s nach dem Ende des gesprochenen Wakeworts (die
# openwakeword-Scores steigen erst am Wortende, dann muss der Streak noch
# auslaufen). Wer durchspricht, hat in dieser Zeit schon das erste Wort des
# Kommandos gesagt — es steckt im wake_ring, der bis 2026-07-28 verworfen
# wurde. Genau das war der abgeschnittene Anfang:
#   "Gaston Wohnzimmerrollo auf 70%"  → aufgenommen ab "…mmerrollo"
#                                     → STT "Manolo auf 70 Prozent" → alle_rollos
#
# Gemessen an vier archivierten Ein-Satz-Triggern (2026-07-27 20:42–20:44,
# wake+rec neu zusammengesetzt und gegen Speaches transkribiert):
#   Pre-Roll 0,0 s: "Manolo auf 70 Prozent" / "Rollo auf 70%"   (Ziel verfehlt)
#   Pre-Roll 0,4 s: "und Simarolo auf 70 Prozent"               (Schnitt im Wort)
#   Pre-Roll 1,2 s: "Gastau wohnt in Marallo auf 70%"           (Schnitt im "Gaston")
#   Pre-Roll 1,5 s: "Gastau, Wohnzimmerlollo auf 70%"           (vollständig)
# Ein Schnitt MITTEN im Wakewort ist schlechter als gar keiner — deshalb
# großzügig davor schneiden und das Wakewort mitschicken, statt es auf 100 ms
# genau treffen zu wollen. Dass "Gaston" (oder ein Verhörer davon) mit im
# Transkript steht, kostet nichts: gegen den echten Aktuator-Prompt gemessen
# (16 Fälle, 11 davon mit vorangestelltem "Gastau/Gastraum/Gastronom/…") —
# das Ziel wurde in allen Fällen richtig getroffen, eine eigene Anrede-Regel
# im Prompt war nicht nötig. Der Brain liest es ohnehin als Anrede.
_PRE_ROLL_SEC = 1.5
# 'bitte' war hier drin und hat jedes höfliche Schaltkommando verschluckt:
# "Schalt das Küchenlicht bitte aus" matchte über ANY(bitte)+CORE(aus) als
# Abbruch, die Anfrage starb wortlos (live beobachtet 2026-07-25 22:46).
# Eine Höflichkeitsfloskel steht in Abbrüchen wie in normalen Befehlen —
# sie trägt kein Signal und gehört deshalb nicht ins Muster.
_STOP_ANY = r'(stopp?|halt|aus|abbrechen|nein)'
# Im Follow-up reicht ein einzelnes "stop"/"stopp" — TV/Hintergrund-Wörter sollen nicht blockieren
_STOP_PATTERN_FOLLOWUP = re.compile(r'\bstopp?\b', re.IGNORECASE)
# Bei Erstanfrage muss eine 2-Wort-Kombi vorliegen, davon mind. eines ein Kern-Abbruchwort.
# Trenner ist \W+ (Whitespace ODER Satzzeichen), weil das STT die Wörter oft mit Kommas
# liefert ("Stopp, stopp, halt") — reiner \s+ würde das verfehlen.
_STOP_PATTERN_FIRST = re.compile(
    rf'\b{_STOP_CORE}\W+{_STOP_ANY}\b|\b{_STOP_ANY}\W+{_STOP_CORE}\b',
    re.IGNORECASE,
)


def _is_stop_command(text: str, followup_round: int) -> bool:
    if not text:
        return False
    if followup_round > 0:
        return bool(_STOP_PATTERN_FOLLOWUP.search(text))
    return bool(_STOP_PATTERN_FIRST.search(text))


# Aktuator-Handshake: Ja/Nein-Antwort auf eine Rückfrage (z.B. "alle Rollos"
# bei kosten=hoch). Nein zuerst prüfen — bei Mehrdeutigkeit lieber sicher
# abbrechen als versehentlich schalten.
_YES_PATTERN = re.compile(r'\b(ja|jawohl|jo|joa|okay|ok|klar|genau|richtig|bestätige|bestätigt|sicher)\b', re.IGNORECASE)
_NO_PATTERN = re.compile(r'\b(nein|nee|ne|nicht|abbrechen|stopp?|halt|lass)\b', re.IGNORECASE)


def _is_no(text: str) -> bool:
    return bool(text) and bool(_NO_PATTERN.search(text))


def _is_yes(text: str) -> bool:
    return bool(text) and bool(_YES_PATTERN.search(text))


def _save_last_recording(audio_chunks: list) -> None:
    """Speichert die aktuelle Aufnahme als WAV unter LAST_RECORDING_PATH.

    Wird nach jeder Sprach-Aufnahme gerufen — der OpenClaw-Enrolment-Tool
    kopiert dieselbe Datei dann nach speakers/<name>.wav.
    """
    try:
        os.makedirs(VOICE_DIR, exist_ok=True)
        with open(LAST_RECORDING_PATH, "wb") as f:
            f.write(chunks_to_wav_bytes(audio_chunks))
    except Exception as e:
        print(f"⚠️  Failed to save last_recording.wav: {e}")


def _save_trigger_audio(audio_chunks: list, bundle: str, kind: str, trigger_id: str) -> None:
    """Archiviert Trigger-Audio unter TRIGGER_AUDIO_DIR (best-effort).

    kind='wake': Ringpuffer-Mitschnitt rund ums Wakeword (Retraining-Clip),
    kind='rec': die anschließende Aufnahme (Quell-Identifikation TV/Radio/…),
    kind='nearmiss': Streak, der das Gate NICHT passiert hat — der eigentlich
      interessante Fall fürs Nachtraining (echter Ruf, der nicht ankam) bzw.
      für die FP-Bewertung eines gelockerten Gates (Gerede, das fast durchkam).
      Nur mit diesen Clips lässt sich min_hits/min_peak messen statt raten.
    """
    if not audio_chunks:
        return
    try:
        os.makedirs(TRIGGER_AUDIO_DIR, exist_ok=True)
        path = os.path.join(TRIGGER_AUDIO_DIR, f"{trigger_id}_{bundle}_{kind}.wav")
        with open(path, "wb") as f:
            f.write(chunks_to_wav_bytes(list(audio_chunks)))
    except Exception as e:
        print(f"⚠️  Trigger-Audio ({kind}) nicht gespeichert: {e}")


def _pre_roll(ring: deque, seconds: float) -> list:
    """Die letzten `seconds` Sekunden des Ringpuffers als Chunk-Liste.

    Ganze Chunks, nicht sample-genau — die Granularität (40 ms ReSpeaker,
    27/80 ms ALSA) ist gegenüber _PRE_ROLL_SEC ohne Belang, und ein
    ungeteilter Chunk hält die Aufnahme lückenlos an das an, was der
    Hauptloop danach aus der Quelle liest.
    """
    want = int(RATE_OW * seconds)
    out: list = []
    have = 0
    for chunk in reversed(ring):
        out.append(chunk)
        have += len(chunk)
        if have >= want:
            break
    out.reverse()
    return out


def _log_wake_event(meta: dict) -> None:
    """Hängt eine JSONL-Zeile pro Wakeword-Entscheidung an (Trigger UND Near-Miss).

    Zweck: die Gate-Parameter (min_hits/min_peak/min_peak_short/threshold)
    offline gegen echte Daten sweepen zu können, statt sie live zu raten —
    und zusammen mit der `audio`-Datei einen beschrifteten Datensatz fürs
    Nachtraining aufzubauen. Der volle Score-Verlauf steht mit drin, damit
    sich ein alternatives Gate rein rechnerisch nachspielen lässt, ohne die
    WAVs erneut durchs Modell zu schicken. Best-effort.
    """
    try:
        meta = {"ts": datetime.now().isoformat(timespec="seconds"), **meta}
        with open(WAKE_LOG_PATH, "a") as f:
            f.write(json.dumps(meta, ensure_ascii=False) + "\n")
    except Exception as exc:  # Logging darf den Loop nie crashen
        print(f"⚠️  wake-log: {exc}")


def _log_actuator_turn(meta: dict) -> None:
    """Spiegel-Kanal: eine JSONL-Zeile je Turn, den der Aktuator selbst erledigt hat.

    Diese Turns überspringen den Brain — ohne diesen Log gäbe es sie nirgends,
    und der geplante Überwacher könnte den einzigen Fall nicht sehen, der ihn
    interessiert: Transkript gegen tatsächlich Ausgeführtes. Das MQTT-Echo
    voiceact/executed allein reicht dafür nicht, es kennt den gesprochenen
    Satz nicht ("alle Lichter" steht nirgends drin).

    Bewusst NICHT in die Haus-Session und NICHT nach Telegram (Entscheidung
    Jochen, 2026-07-25): Schaltvorgänge sollen weder die Gesprächs-Session
    zumüllen noch im Chat auftauchen. Wer den Log konsumiert — Überwacher-Agent,
    Auswertung, etwas anderes — ist offen; deshalb erst mal nur schreiben.
    Best-effort.
    """
    try:
        meta = {"ts": datetime.now().isoformat(timespec="seconds"), **meta}
        with open(ACTUATOR_LOG_PATH, "a") as f:
            f.write(json.dumps(meta, ensure_ascii=False) + "\n")
    except Exception as exc:  # Logging darf den Loop nie crashen
        print(f"⚠️  aktuator-log: {exc}")


def _cleanup_trigger_audio() -> None:
    """Löscht Trigger-Archiv-Dateien älter als TRIGGER_AUDIO_MAX_AGE_DAYS."""
    try:
        if not os.path.isdir(TRIGGER_AUDIO_DIR):
            return
        cutoff = time.time() - TRIGGER_AUDIO_MAX_AGE_DAYS * 86400
        removed = 0
        for name in os.listdir(TRIGGER_AUDIO_DIR):
            path = os.path.join(TRIGGER_AUDIO_DIR, name)
            if name.endswith(".wav") and os.path.getmtime(path) < cutoff:
                os.remove(path)
                removed += 1
        if removed:
            print(f"🧹 Trigger-Archiv: {removed} Datei(en) > {TRIGGER_AUDIO_MAX_AGE_DAYS} Tage gelöscht")
    except Exception as e:
        print(f"⚠️  Trigger-Archiv-Cleanup: {e}")


def _make_audio(profile: Profile):
    """Baut passende AudioSource + AudioSink je nach mode."""
    if profile.mode == "respeaker":
        source = RespeakerSource(profile.respeaker)
        if profile.respeaker.use_speaker:
            sink = RespeakerSink(profile.respeaker)
        else:
            sink = AlsaSink(profile.local_audio.playback_device)
        return source, sink
    # mode == "local"
    return AlsaSource(profile.local_audio), AlsaSink(profile.local_audio.playback_device)


def _make_wakeword(profile: Profile):
    if profile.mode == "respeaker":
        return RespeakerWakeword(profile.respeaker, profile.wakewords)
    return OpenWakewordEngine(profile.wakewords)


def _wakeword_ack_path(bundle: str, multi: bool) -> str:
    """Dateipfad der vorgerenderten Quittung ('Ja?') für ein Wakeword.

    Ist nur EIN Wakeword konfiguriert (Default- oder Rückwärtskompat-Fall):
    exakt PIPER_OUT (ja.wav) wie bisher. Bei mehreren Wakewords bekommt jedes
    seine eigene Datei, damit unterschiedliche Ack-Texte nebeneinander bestehen.
    """
    if not multi:
        return PIPER_OUT
    safe = re.sub(r"[^a-zA-Z0-9_-]+", "_", bundle) or "wakeword"
    return os.path.join(WORKSPACE, f"ack_{safe}.wav")


def _make_leds(profile: Profile) -> LedDirector:
    sinks = []
    if profile.leds.wled_enabled:
        sinks.append(WledLeds(profile.leds.wled_host, enabled=True))
    if profile.leds.respeaker_ring_enabled and profile.mode == "respeaker":
        sinks.append(RespeakerRing(profile.respeaker, enabled=True))
    return LedDirector(*sinks)


def _chunk_speech_stats(
    vad: webrtcvad.Vad, audio_16: np.ndarray, min_rms: float = 0.0
) -> tuple[bool, float, int, int]:
    """(ist_sprache, rms, sprach-frames, frames_gesamt) für einen Chunk.

    Der Sprach-Entscheid verodert die 20-ms-VAD-Frames: EIN Frame von vier
    (bei 80-ms-Chunks) genügt. Das ist bewusst empfindlich — dass es in Lärm
    zu empfindlich ist, ist bekannt, lässt sich aber nur mit Zahlen ändern.
    Deshalb kommen Pegel und Frame-Anteil hier mit heraus und landen im
    endpoint.log: erst damit ist entscheidbar, ob ein Frame-Anteil-Gate
    (z.B. 3 von 4) oder eine Pegelschwelle die Fehlauslöser trennt, ohne
    echte Kommandos zu zerschneiden.
    """
    rms = float(np.sqrt(np.mean(audio_16.astype(np.float32) ** 2)))
    frames = 0
    speech_frames = 0
    for i in range(0, len(audio_16), VAD_FRAME_SIZE):
        frame = audio_16[i : i + VAD_FRAME_SIZE]
        if len(frame) == VAD_FRAME_SIZE:
            frames += 1
            speech_frames += vad.is_speech(frame.tobytes(), RATE_OW)
    if min_rms > 0.0 and rms < min_rms:
        return False, rms, speech_frames, frames
    return speech_frames > 0, rms, speech_frames, frames


def _is_speech_chunk(
    vad: webrtcvad.Vad, audio_16: np.ndarray, min_rms: float = 0.0
) -> bool:
    return _chunk_speech_stats(vad, audio_16, min_rms)[0]


# Endet ein Transkript "sauber" (Satzzeichen) oder offen (Konjunktion/Füllwort)?
# Ein offenes Ende ist ein starkes Indiz, dass die Aufnahme mitten im Satz
# abgeschnitten wurde — die wichtigste Metrik zum Beurteilen von Pause-Cuts.
_OPEN_TAIL = {
    "und", "oder", "aber", "weil", "dass", "wenn", "also", "dann", "noch",
    "ähm", "äh", "öh", "hm", "mit", "für", "auf", "in", "zu", "der", "die",
    "das", "ein", "eine", "ich", "wir", "ist", "war",
}


def _ends_clean(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return False
    if t[-1] in ".!?…":
        return True
    last = re.sub(r"[^\wäöüß]", "", t.split()[-1].lower())
    return last not in _OPEN_TAIL


def _signal_stats(rms: list[float], speech_frames: int, total_frames: int) -> dict:
    """Pegel- und VAD-Kennzahlen einer Aufnahme fürs endpoint.log.

    rms_p10 ist der Ruhepegel der Aufnahme (Grundrauschen bzw. Lücken zwischen
    den Sätzen einer Störquelle), rms_max der lauteste Moment — bei einem
    echten Ruf der Sprecher selbst. Das Verhältnis der beiden ist die Zahl,
    an der sich entscheiden lässt, ob eine Pegelschwelle den Fernseher vom
    Sprecher trennen kann. vad_frame_ratio zeigt, wie knapp der ODER-Entscheid
    über die 20-ms-Frames jeweils ausging.
    """
    if not rms:
        return {}
    s = sorted(rms)
    return {
        "rms_p10": round(s[len(s) // 10], 1),
        "rms_median": round(s[len(s) // 2], 1),
        "rms_max": round(s[-1], 1),
        "vad_frame_ratio": (
            round(speech_frames / total_frames, 3) if total_frames else None
        ),
    }


def _log_endpoint(meta: dict) -> None:
    """Hängt eine JSONL-Zeile mit Endpointing-Telemetrie an. Best-effort."""
    try:
        meta = {"ts": datetime.now().isoformat(timespec="seconds"), **meta}
        with open(ENDPOINT_LOG_PATH, "a") as f:
            f.write(json.dumps(meta, ensure_ascii=False) + "\n")
    except Exception as exc:  # Logging darf den Loop nie crashen
        print(f"⚠️  endpoint-log: {exc}")


def _gate_passed(
    wake_hits: int,
    wake_peak: float,
    min_hits: int,
    min_peak: float,
    min_peak_short: float,
    min_peak_single: float,
) -> bool:
    """Entscheidet, ob ein beendeter Streak triggern darf.

    Zwei Wege, absichtlich als ODER: der bisherige Streak-Weg (min_hits Frames
    plus gestufte Peak-Anforderung) und — falls konfiguriert — ein einzelner
    sehr hoher Frame. Der zweite ist ADDITIV; min_hits bleibt unangetastet,
    damit die Gap-Toleranz im Streak-Zähler unverändert erst ab min_hits
    greift (sonst würde sie zwei unabhängige Rausch-Spitzen zusammennähen).

    Der 1-Frame-Weg existiert, weil 5 der 6 nachweislich echten, verlorenen
    Rufe vom 2026-07-26 als EINZELNE Spitze ankamen (Nachbar-Frames bei 0.01)
    und damit an min_hits scheiterten, nicht am Peak. Siehe
    WakewordHit.min_peak_single für die Datenbasis und tools/wake_triage.py
    für das Verfahren, mit dem echte Rufe von Rauschen getrennt wurden.
    """
    if wake_hits >= min_hits and wake_peak >= _required_peak(
        wake_hits, min_peak, min_peak_short
    ):
        return True
    return wake_hits == 1 and min_peak_single > 0.0 and wake_peak >= min_peak_single


def _required_peak(
    wake_hits: int, min_peak: float, min_peak_short: float, min_peak_single: float = 0.0
) -> float:
    """Peak-Anforderung abhängig von der Streak-Länge.

    Kurz-Streaks (< 3 Frames, also der min_hits-2-Pfad) müssen min_peak_short
    erreichen, längere Streaks min_peak. Datenbasis Live-Logs 2026-07-08..13:
    alle vier 2-Frame-Trigger waren False Positives (Peaks 0.70/0.77/0.83/0.92),
    der einzige echte 2-Frame-Ruf peakte 0.93 — ab 3 Frames tragen die
    zusätzlichen Hits die Evidenz, dort bleibt min_peak ausreichend.
    """
    if wake_hits == 1 and min_peak_single > 0.0:
        return min_peak_single
    return min_peak_short if wake_hits < 3 else min_peak


def _format_wake_scores(scores: deque, threshold: float) -> str:
    """Formatiert den Score-Verlauf eines Wakeword-Events als Einzeiler.

    Anker ist der letzte Frame >= Threshold; von dort läuft die
    Rückwärts-Suche mit derselben 1-Frame-Gap-Toleranz wie der Live-Streak.
    Trailing-Frames (tolerierter Gap + Trigger-Frame) dürfen daher beliebig
    tief abfallen, ohne das Event auf '(leer)' zu schrumpfen.
    | markiert die Threshold-Kreuzungen (Anstieg und Abfall) — mit dem
    Threshold des Wakeword, das den Streak ausgelöst hat, sonst werden
    schwache Trigger (alle Frames unter 0.65) als '(leer)' unsichtbar.
    """
    seq = list(scores)
    last_hit = next(
        (i for i in range(len(seq) - 1, -1, -1) if seq[i] >= threshold), None
    )
    if last_hit is None:
        return "(leer)"
    start = last_hit
    gap_used = False
    for i in range(last_hit - 1, -1, -1):
        if seq[i] < 0.05:
            if gap_used:
                break
            gap_used = True
        start = i
    # führende Fast-Null-Frames (z.B. der Gap, an dem die Suche endete) kappen
    while start < last_hit and seq[start] < 0.05:
        start += 1
    event = seq[start:]  # schließt Gap-/Trigger-Frames am Ende ein
    parts: list[str] = []
    prev_above = event[0] >= threshold
    for s in event:
        above = s >= threshold
        if above != prev_above:
            parts.append("|")
            prev_above = above
        parts.append(f"{s:.2f}")
    return " ".join(parts)


def run() -> None:
    profile = load_profile()

    # --- LEDs — Boot-Sequenz startet ---
    leds = _make_leds(profile)
    leds.set_phase(LED_BOOT)
    leds.set_boot_step(0)

    # --- Speaches-Zustand + Service-Wrapper ---
    speaches = SpeachesState()
    speaches_stt = SpeachesStt(
        speaches, profile.speaches_base, profile.speaches_stt_model
    )
    speaches_tts = SpeachesTts(
        speaches,
        profile.speaches_base,
        profile.speaches_tts_model,
        profile.speaches_tts_voice,
    )

    # --- VoiceController + voice_state mit Profil-Defaults initialisieren ---
    # Callback: bei jedem erfolgreichen Stimmwechsel die "Ja?"-Quittung
    # (PIPER_OUT) in der nun aktiven Stimme neu rendern. Minimale Kopplung —
    # Quittungstext + SpeachesTts stecken hier im Closure, nicht im Controller.
    def _rerender_ack() -> None:
        rerender_ja_speaches(speaches_tts, profile.locale.wakeword_ack)

    voice_controller = VoiceController(
        base=profile.speaches_base,
        default_model=profile.speaches_tts_model,
        default_voice=profile.speaches_tts_voice,
        on_voice_changed=_rerender_ack,
    )
    voice_state.set(
        model=profile.speaches_tts_model,
        voice=profile.speaches_tts_voice,
        speed=1.0,
    )

    # --- Audio (Source + Sink) baut und startet ---
    audio_source, audio_sink = _make_audio(profile)
    audio_source.start()
    leds.set_boot_step(4)   # Audio-Source gestartet (ESP-Verbindung läuft an)

    # --- Lokale STT + TTS-Hilfen ---
    local_stt = LocalWhisperStt()
    speaches_mod.check_at_startup(
        speaches,
        profile.speaches_base,
        profile.speaches_stt_model,
        profile.speaches_tts_model,
    )
    leds.set_boot_step(8)   # Speaches-Check abgeschlossen

    # Wakeword-Quittungen: bei genau einem Wakeword (Default/Rückwärtskompat)
    # exakt wie bisher eine Datei (PIPER_OUT); bei mehreren je Wakeword eine
    # eigene, damit unterschiedliche Ack-Texte möglich sind.
    _multi_wakewords = len(profile.wakewords) > 1
    ack_paths: dict[str, str] = {}
    for _ww in profile.wakewords:
        _path = _wakeword_ack_path(_ww.bundle, _multi_wakewords)
        ack_paths[_ww.bundle] = _path
        prerender_ja(_ww.ack, out_path=_path)
    prerender_followup_beep()

    # --- Wakeword + VAD ---
    wakeword = _make_wakeword(profile)
    wakeword_by_name: dict[str, WakewordConfig] = {w.bundle: w for w in profile.wakewords}
    # Aktuell "aktives" Wakeword — bestimmt Session/Stimme des laufenden
    # Dialogs. Bleibt bei Kurz-Follow-ups (kein neues Wakeword) unverändert
    # bis zum nächsten Trigger.
    current_wakeword: WakewordConfig = profile.wakewords[0]
    print("🔧 Initialising WebRTC VAD...")
    vad = webrtcvad.Vad(profile.vad_aggressiveness)
    _vad_rms_min = profile.vad_voice_rms_min
    # Endpointing zeitbasiert: silence_seconds gilt auf jedem Profil gleich.
    # Die reale Chunk-Länge variiert je Quelle (ALSA-16k=80ms, 48k-resample≈27ms,
    # ReSpeaker=40ms), darum wird das Chunk-Limit erst beim ersten Audio aufgelöst.
    _silence_seconds = profile.silence_seconds
    _silence_override = profile.silence_chunks_limit  # > 0 = explizit in Chunks
    _chunk_sec = 0.0                                  # beim ersten Chunk gemessen
    _silence_limit = _silence_override or SILENCE_CHUNKS_LIMIT  # Fallback bis Messung
    # Kommando-Modus (Ein-Satz): eigener, deutlich kürzerer Nachlauf. Wird wie
    # _silence_limit erst beim ersten Chunk in Chunks aufgelöst.
    _command_silence_limit = _silence_limit
    # So viel Sprache muss in der Aufnahme stecken, bevor der straffe
    # Kommando-Nachlauf greifen darf (Sperre gegen den Wakeword-Ausklang).
    _command_min_speech_chunks = 1
    print(
        f"✅ WebRTC VAD ready "
        f"(aggressiveness={profile.vad_aggressiveness}, "
        f"rms_min={_vad_rms_min:.0f}, "
        f"silence_seconds={_silence_seconds:.2f}"
        f"{f', override={_silence_override} chunks' if _silence_override else ''}, "
        f"kommando={profile.command_silence_seconds:.2f}s/{profile.command_max_seconds:.0f}s)"
    )
    leds.set_boot_step(12)  # Wakeword-Modell geladen

    # --- Services zusammenstecken ---
    stt_pipeline = SttPipeline(speaches_stt, local_stt)
    speaker = ReplySpeaker(speaches_tts, audio_sink.play_wav, leds, profile.tts_prefix)
    thinking = ThinkingWorker(
        audio_sink.play_wav, profile.locale.thinking_phrases, speaches=speaches_tts
    )
    diarizer = SpeachesDiarizer(profile.speaches_base) if profile.speaches_base else None
    if not VOICE_ANALYSIS_BASE:
        # Bis 2026-07 war das eine Konstante im Code. Wer von einer älteren
        # Fassung aktualisiert, verliert die Analyse sonst wortlos.
        print("ℹ️  Stimmungsanalyse aus (voice_analysis_base nicht in config.yaml gesetzt)")
    mood_analyzer = MoodAnalyzer(VOICE_ANALYSIS_BASE) if VOICE_ANALYSIS_BASE else None
    workers = Workers(
        stt=stt_pipeline,
        speaker=speaker,
        thinking=thinking,
        openclaw_token=profile.openclaw_token,
        openclaw_session=profile.openclaw_session,
        telegram_bot_token=profile.telegram_bot_token,
        telegram_chat_id=profile.telegram_chat_id,
        confirmation_prefix=profile.locale.confirmation_prefix,
        no_reply_fallback=profile.locale.no_reply_fallback,
        voice_instruction=profile.locale.openclaw_voice_instruction,
        diarizer=diarizer,
        mood_analyzer=mood_analyzer,
        use_stream=profile.openclaw_stream,
        voice_controller=voice_controller,
    )

    # Lokale HTTP-Server (von OpenClaw-Tools angesprochen)
    start_enroll_server()
    start_speak_server(voice_controller=voice_controller)
    start_announce_worker(
        speaker,
        telegram_bot_token=profile.telegram_bot_token,
        telegram_chat_id=profile.telegram_chat_id,
    )

    # --- Voice-Aktuator v1 (schneller lokaler Schalt-Pfad, siehe
    # ACTUATOR_V1_PLAN.md) — fehlt/schlägt fehl: Assistant läuft unverändert
    # weiter (Brain-Pfad wie vor diesem Umbau), Fehler dürfen den Start nie
    # verhindern.
    actuator: Actuator | None = None
    if profile.actuator.enabled and not profile.actuator.base_url:
        # Früher stand hier ein echter Host als Default; seit das Repo
        # öffentlich ist, ist er leer. Ohne diese Meldung liefe der Aktuator
        # gegen "/capabilities" und schwiege sich über den Grund aus.
        print("⚠️  Aktuator aktiviert, aber actuator.base_url ist leer — "
              "bitte im Profil setzen (siehe ACTUATOR_INTERFACE.md). Aktuator bleibt aus.")
    elif profile.actuator.enabled:
        try:
            actuator = Actuator(profile.actuator)
            actuator.start()
            if actuator.ready:
                n_ziele = len(actuator.digest or {})
                print(f"🔌 Aktuator aktiv — {n_ziele} Ziele, Version {actuator.version}")
            else:
                print("⚠️  Aktuator aktiviert, aber initialer refresh() fehlgeschlagen — startet ohne Ziel-Vokabular, Poll/MQTT versuchen es weiter")
        except Exception as e:
            print(f"⚠️  Aktuator-Start fehlgeschlagen: {e}")
            actuator = None

    # --- Überwacher (semantische Prüfung jedes Aktuator-Turns via LLM) ---
    # Fehlt der `watcher:`-Block oder enabled=false: kein Overseer. Der
    # Overseer braucht den Aktuator (er prüft dessen Turns), also nur starten
    # wenn auch der Aktuator aktiv ist.
    overseer = None
    if profile.watcher.enabled and profile.watcher.chat_id and actuator is not None:
        try:
            from voice_assistant.services.watcher import Overseer
            wt = profile.watcher
            bot_tok = wt.bot_token or profile.telegram_bot_token
            overseer = Overseer(
                chat_id=wt.chat_id,
                bot_token=bot_tok,
                quiet_start=wt.quiet_start,
                quiet_end=wt.quiet_end,
                llm_url=wt.llm_url,
                llm_model=wt.llm_model,
                llm_api_key=wt.llm_api_key,
                llm_timeout=wt.llm_timeout,
            )
            llm_info = f", Modell {wt.llm_model}" if wt.llm_model else ""
            print(f"👁️  Überwacher aktiv{llm_info}, meldet an Chat {wt.chat_id}, "
                  f"still {wt.quiet_start:02d}–{wt.quiet_end:02d} Uhr")
        except Exception as e:
            print(f"⚠️  Überwacher-Start fehlgeschlagen: {e}")
    elif profile.watcher.enabled and not profile.watcher.chat_id:
        print("⚠️  Überwacher aktiviert, aber watcher.chat_id ist leer — "
              "kein Ziel für Meldungen. Überwacher bleibt aus.")

    # --- State-Machine ---
    state = STATE_LISTENING
    state_start = time.time()
    recorded_chunks: list = []
    # Turn-eigene Ergebnis-Queues: pro Aufnahme frisch erzeugt und an die
    # Worker übergeben. Bricht die State-Machine einen Turn ab (STT-Timeout,
    # Diarization-Join-Timeout), schreiben die verwaisten Worker in diese –
    # dann nicht mehr referenzierten – Objekte, statt ein Ergebnis in einen
    # späteren Turn durchsickern zu lassen (früher: prozessweite FIFOs, die
    # sich nach einem einzigen verpassten Ergebnis dauerhaft um eins versetzten).
    turn_stt_q: queue.Queue | None = None
    turn_spk_q: queue.Queue | None = None
    turn_mood_q: queue.Queue | None = None
    silence_counter = 0
    max_internal_pause = 0                  # längste Pause, die durch Sprache wieder aufging
    speech_detected = False
    endpoint_meta: dict = {}                # Endpointing-Telemetrie der aktuellen Aufnahme
    # Endpointing-Parameter des LAUFENDEN Turns. Start immer im Dialog-Modus;
    # die Ein-Satz-Entscheidung nach _ACK_DELAY_SEC schaltet ggf. auf Kommando.
    turn_mode = "dialog"
    turn_silence_limit = _silence_limit
    turn_max_sec = RECORDING_MAX_SEC
    turn_ein_satz = False                   # Ein-Satz erkannt → Kommando-Modus scharf machen
    turn_speech_chunks = 0                  # Chunks mit Sprache seit Aufnahmebeginn
    # Pegel/VAD-Verlauf der laufenden Aufnahme (Rohstoff fürs Nachtunen).
    turn_rms: list[float] = []
    turn_frames_speech = 0
    turn_frames_total = 0
    wake_hits = 0                           # aufeinanderfolgende Frames über Threshold
    # Ein einzelner Frame unter Threshold beendet den Streak NICHT (echte
    # "Gaston"-Rufe tauchen mitten im Wort kurz ab und wurden als 2+1-Near-Miss
    # gespalten). FP-geprüft: 3 Hits mit 1-Frame-Lücke = 0.00 FP/h und
    # 2 Hits mit 1-Frame-Lücke = 0.09 FP/h auf 10.7h Validierungs-Audio
    # (eval_gap.py, 2026-07-05/06).
    wake_gap_used = False
    # Bester Score des aktuellen Streaks — echte Rufe peaken deutlich über
    # dem Threshold (gaston: 0.92-0.99 auf Test-Set + Live-Logs), FPs aus
    # Gesprächsfetzen bleiben flach (FP 2026-07-07: 0.41/0.68). min_peak 0.7
    # eliminiert beide gemessenen FPs (eval_peak.py: 0.00 FP/h auf 10.7h)
    # und kostet nur 1 von 11 triggernden Test-Takes (Peak 0.50).
    wake_peak = 0.0
    wake_announced = False                 # 🟢-Meldung/LED dieses Streaks schon raus?
    # Benötigte Streak-Länge des aktuellen Streaks — kommt pro Wakeword aus
    # manifest.yaml/Config (kurze Wörter erreichen kürzere Streaks).
    current_min_hits = 3
    current_min_peak = 0.0
    current_min_peak_short = 0.0
    current_min_peak_single = 0.0
    current_threshold = 0.65               # Threshold des aktiven Streaks (fürs Score-Log)
    near_miss_until = 0.0                  # Timestamp bis Near-Miss-LED zurückgesetzt wird
    recent_scores: deque[float] = deque(maxlen=30)  # ~1.2s Rolling-Window aller Scores
    # Ringpuffer der letzten ~3s Mic-Audio im LISTENING — enthält beim Trigger
    # das Wakeword selbst (der Trigger fällt 1-2 Frames nach Wortende) und
    # wird als Retraining-/Analyse-Clip archiviert (siehe TRIGGER_AUDIO_DIR).
    wake_ring: deque = deque()
    wake_ring_samples = 0
    _wake_ring_max = int(RATE_OW * 3.0)
    trigger_audio_id: str | None = None    # verbindet wake- und rec-Clip eines Triggers
    # Wake-Clip des laufenden Turns — bleibt über das Ende der Aufnahme hinaus
    # stehen (anders als trigger_audio_id), damit der AUSGANG des Turns noch auf
    # den Trigger bezogen protokolliert werden kann. Siehe _log_outcome().
    turn_audio: str | None = None
    trigger_audio_bundle = ""
    followup_round = 0                     # aktuelle Follow-up-Runde (0 = kein Follow-up aktiv)
    followup_rms_sum = 0.0
    followup_rms_count = 0
    followup_vad_speech = 0
    # Hält beim Aktuator-Handshake {"intent":..., "request_id":...} zwischen
    # Rückfrage und der ja/nein-Antwort im nächsten Turn. Muss überall, wo ein
    # Handshake ins Leere läuft, wieder auf None zurückgesetzt werden — sonst
    # sickert er in einen späteren Turn durch.
    pending_confirm: dict | None = None

    # Ein-Satz-Kommando: "Ja?" wird verzögert gespielt. ack_pending=True nach
    # dem Trigger, ack_deadline = Triggerzeit + _ACK_DELAY_SEC. Im
    # STATE_RECORDING wird nach Ablauf entschieden: Sprache erkannt → kein
    # "Ja?", Still → "Ja?" spielen. ack_path wird vom Trigger-Pfad gesetzt.
    ack_pending = False
    ack_deadline = 0.0
    ack_path = ""
    # Wie viele der recorded_chunks aus dem Pre-Roll stammen (also VOR dem
    # Trigger aufgenommen wurden) — die haben den VAD nie gesehen und dürfen
    # bei den Sprach-Gates nicht mitzählen.
    pre_roll_chunks = 0

    def _log_outcome(ausgang: str, **extra) -> None:
        """Protokolliert, was aus einem Trigger geworden ist — einmal je Trigger.

        Das ist der Rohstoff für die selbstlabelnde Triage (tools/wake_triage.py):
        ein Trigger, aus dem ein ausgeführtes Schaltkommando wurde, war
        garantiert ein echter Ruf; einer, den der Nutzer mit einem Stopp-Wort
        abgebrochen hat, garantiert ein Fehltrigger. Beides steht sonst nirgends
        in einer Form, die sich auf den Wake-Clip beziehen lässt — der
        Aktuator-Log kennt die Audio-Datei nicht, und das Journal ist kein
        Datenformat.

        Nur der erste Turn nach dem Wakeword zählt (Follow-ups haben keinen
        eigenen Trigger), und nur einmal: turn_audio wird danach geleert.
        """
        nonlocal turn_audio
        if turn_audio is None:
            return
        _log_wake_event({"result": "outcome", "audio": turn_audio,
                         "ausgang": ausgang, **extra})
        turn_audio = None

    def _flush_endpoint(transcript: str | None = None, **extra) -> None:
        """Schreibt die Endpointing-Telemetrie des Turns — auf JEDEM Ausgang.

        Vorher hing das am normalen Pfad: ein mit Stopp-Wort abgebrochener
        oder als "keine Sprache" verworfener Turn schrieb gar keine Zeile.
        Damit fehlten ausgerechnet die kaputten Aufnahmen im endpoint.log —
        der Fernseh-Vorfall vom 2026-08-01 stand nirgends darin, obwohl er
        21,9 s lief. Genau die sind aber die Fälle, gegen die getunt wird.
        """
        nonlocal endpoint_meta
        if not endpoint_meta:
            return
        if transcript is not None:
            extra.setdefault("transcript", transcript)
            extra.setdefault("ends_clean", _ends_clean(transcript))
        endpoint_meta.update(extra)
        _log_endpoint(endpoint_meta)
        endpoint_meta = {}

    _cleanup_trigger_audio()

    leds.set_phase(LED_IDLE)
    print("\n🎤 Ready – waiting for wakeword...\n")

    try:
        while True:
            audio_16 = audio_source.read_chunk()
            now = time.time()
            current_state[0] = state

            # Reale Chunk-Länge einmalig messen → zeitbasiertes Endpointing auflösen
            if _chunk_sec == 0.0 and len(audio_16) > 0:
                _chunk_sec = len(audio_16) / RATE_OW
                if not _silence_override:
                    _silence_limit = max(1, round(_silence_seconds / _chunk_sec))
                # Nie geduldiger als der Dialog-Modus: wer silence_chunks_limit
                # (Legacy, in Chunks) kurz stellt, würde sonst mit einem
                # Kommando LÄNGER warten als mit einer normalen Anfrage.
                _command_silence_limit = min(
                    _silence_limit,
                    max(1, round(profile.command_silence_seconds / _chunk_sec)),
                )
                _command_min_speech_chunks = max(
                    1, round(_COMMAND_MIN_SPEECH_SEC / _chunk_sec)
                )
                print(
                    f"📏 Endpointing: chunk={_chunk_sec * 1000:.0f}ms, "
                    f"silence_limit={_silence_limit} chunks "
                    f"≈ {_silence_limit * _chunk_sec:.2f}s, "
                    f"kommando={_command_silence_limit} chunks "
                    f"≈ {_command_silence_limit * _chunk_sec:.2f}s"
                )

            # --- LISTENING ---
            if state == STATE_LISTENING:
                if len(audio_16) > 0:
                    wake_ring.append(audio_16.copy())
                    wake_ring_samples += len(audio_16)
                    while wake_ring_samples > _wake_ring_max and len(wake_ring) > 1:
                        wake_ring_samples -= len(wake_ring.popleft())

                if near_miss_until > 0.0 and now >= near_miss_until:
                    leds.set_phase(LED_IDLE)
                    near_miss_until = 0.0

                hit = wakeword.feed(audio_16)
                if hit is not None:
                    recent_scores.append(hit.score)
                if hit is None:
                    pass  # noch 1280 Samples sammeln bevor neue Prediction
                elif hit.score > hit.threshold:
                    wake_hits += 1
                    wake_peak = max(wake_peak, hit.score)
                    # Best-scorender Kandidat dieses Frames wird zum aktiven
                    # Wakeword — bleibt stehen, bis der Streak endet (unten).
                    current_wakeword = wakeword_by_name.get(hit.name, current_wakeword)
                    current_min_hits = hit.min_hits
                    current_min_peak = hit.min_peak
                    current_min_peak_short = hit.min_peak_short
                    current_min_peak_single = hit.min_peak_single
                    current_threshold = hit.threshold
                    if not wake_announced and _gate_passed(
                        wake_hits, wake_peak, current_min_hits,
                        current_min_peak, current_min_peak_short, current_min_peak_single,
                    ):
                        wake_announced = True
                        leds.set_phase(LED_WAKEWORD)
                        print(f"[{now:.1f}s] 🟢 Wakeword detected: {current_wakeword.bundle}")
                elif wake_hits >= current_min_hits and not wake_gap_used:
                    # erste Lücke im Streak tolerieren (zählt nicht als Hit) —
                    # aber erst ab min_hits echten Hits, sonst näht die Toleranz
                    # zwei unabhängige Noise-Bursts zu einem Trigger zusammen
                    wake_gap_used = True
                else:
                    beam = getattr(audio_source, "beam_angle", None)
                    beam_str = f"  LED {beam:.0f} ({beam * 30:.0f}°)" if beam is not None else ""
                    if _gate_passed(
                        wake_hits, wake_peak, current_min_hits,
                        current_min_peak, current_min_peak_short, current_min_peak_single,
                    ):
                        print(f"[{now:.1f}s] 📊 [{current_wakeword.bundle}] {_format_wake_scores(recent_scores, current_threshold)}{beam_str}")
                        trigger_audio_id = datetime.now().strftime("%Y%m%d_%H%M%S")
                        trigger_audio_bundle = current_wakeword.bundle
                        _save_trigger_audio(list(wake_ring), trigger_audio_bundle, "wake", trigger_audio_id)
                        turn_audio = f"{trigger_audio_id}_{trigger_audio_bundle}_wake.wav"
                        # Trigger genauso protokollieren wie den Near-Miss —
                        # ein Sweep braucht beide Klassen im selben Log.
                        _log_wake_event({
                            "result": "trigger",
                            "bundle": trigger_audio_bundle,
                            "hits": wake_hits,
                            "peak": round(wake_peak, 3),
                            "required_peak": round(
                                _required_peak(wake_hits, current_min_peak, current_min_peak_short, current_min_peak_single), 3
                            ),
                            "min_hits": current_min_hits,
                            "threshold": current_threshold,
                            "scores": [round(s, 3) for s in recent_scores],
                            "beam": float(beam) if beam is not None else None,
                            "audio": f"{trigger_audio_id}_{trigger_audio_bundle}_wake.wav",
                        })
                        # Ein-Satz-Kommando: LED SOFORT, kein play_wav.
                        # Pre-Roll aus dem Ring VOR dem clear() abgreifen — er
                        # trägt den Anfang eines durchgesprochenen Kommandos
                        # (siehe _PRE_ROLL_SEC). Und KEIN flush: was seit dem
                        # letzten gelesenen Chunk in der Quelle liegt, schließt
                        # die Lücke zwischen Pre-Roll und Aufnahme.
                        # "Ja?" kommt verzögert (siehe STATE_RECORDING, ack_pending).
                        pre_roll = _pre_roll(wake_ring, _PRE_ROLL_SEC)
                        wake_ring.clear()
                        wake_ring_samples = 0
                        leds.set_phase(LED_RECORDING)
                        voice_controller.set_default_voice(current_wakeword.tts_voice)
                        near_miss_until = 0.0
                        wakeword.reset()
                        state = STATE_RECORDING
                        state_start = time.time()
                        recorded_chunks = list(pre_roll)
                        pre_roll_chunks = len(pre_roll)
                        silence_counter = 0
                        max_internal_pause = 0
                        speech_detected = False
                        turn_mode = "dialog"
                        turn_silence_limit = _silence_limit
                        turn_max_sec = RECORDING_MAX_SEC
                        turn_ein_satz = False
                        turn_speech_chunks = 0
                        turn_rms = []
                        turn_frames_speech = 0
                        turn_frames_total = 0
                        ack_pending = True
                        ack_deadline = now + _ACK_DELAY_SEC
                        ack_path = ack_paths.get(current_wakeword.bundle, PIPER_OUT)
                    elif wake_hits >= 1:
                        # Wakeword-Name mitloggen: current_wakeword wurde von den
                        # Frames dieses Streaks gesetzt → erlaubt False-Positive-
                        # Zuordnung pro Modell.
                        last_scores = " ".join(f"{s:.2f}" for s in list(recent_scores)[-5:])
                        peak_str = (
                            f", Peak {wake_peak:.2f} < {_required_peak(wake_hits, current_min_peak, current_min_peak_short, current_min_peak_single):.2f}"
                            if wake_hits >= current_min_hits or wake_hits == 1
                            else ""
                        )
                        print(f"[{now:.1f}s] ⚡ Near-Miss [{current_wakeword.bundle}] ({wake_hits} Frame{'s' if wake_hits > 1 else ''}: {last_scores}{peak_str}){beam_str}")
                        # Near-Miss-Clip + Event archivieren: das ist der Fall,
                        # den wir zum Nachjustieren von min_hits/min_peak (und
                        # fürs Nachtraining) brauchen — bislang fiel er weg.
                        # wake_ring wird bewusst NICHT geleert: folgt kurz
                        # darauf der echte Ruf, behält der seinen vollen
                        # 3-Sekunden-Kontext.
                        nm_id = datetime.now().strftime("%Y%m%d_%H%M%S")
                        _save_trigger_audio(
                            list(wake_ring), current_wakeword.bundle, "nearmiss", nm_id
                        )
                        _log_wake_event({
                            "result": "nearmiss",
                            "bundle": current_wakeword.bundle,
                            "hits": wake_hits,
                            "peak": round(wake_peak, 3),
                            "required_peak": round(
                                _required_peak(wake_hits, current_min_peak, current_min_peak_short, current_min_peak_single), 3
                            ),
                            "min_hits": current_min_hits,
                            "threshold": current_threshold,
                            "failed_on": "min_hits" if wake_hits < current_min_hits else "min_peak",
                            "scores": [round(s, 3) for s in recent_scores],
                            "beam": float(beam) if beam is not None else None,
                            "audio": f"{nm_id}_{current_wakeword.bundle}_nearmiss.wav",
                        })
                        leds.set_phase(LED_NEAR_MISS)
                        near_miss_until = now + 0.6
                    wake_hits = 0
                    wake_peak = 0.0
                    wake_announced = False
                    wake_gap_used = False

                # Sicherheits-Timeout: nicht länger als 1s auf Streak-Ende warten
                # (Peak-Bedingung gilt auch hier — echte Rufe peaken früh)
                if wake_hits >= 25 and wake_peak >= current_min_peak:
                    beam = getattr(audio_source, "beam_angle", None)
                    beam_str = f"  LED {beam:.0f} ({beam * 30:.0f}°)" if beam is not None else ""
                    print(f"[{now:.1f}s] 📊 [{current_wakeword.bundle}] {_format_wake_scores(recent_scores, current_threshold)} (Timeout){beam_str}")
                    trigger_audio_id = datetime.now().strftime("%Y%m%d_%H%M%S")
                    trigger_audio_bundle = current_wakeword.bundle
                    _save_trigger_audio(list(wake_ring), trigger_audio_bundle, "wake", trigger_audio_id)
                    turn_audio = f"{trigger_audio_id}_{trigger_audio_bundle}_wake.wav"
                    _log_wake_event({
                        "result": "trigger",
                        "via": "timeout",
                        "bundle": trigger_audio_bundle,
                        "hits": wake_hits,
                        "peak": round(wake_peak, 3),
                        "required_peak": round(current_min_peak, 3),
                        "min_hits": current_min_hits,
                        "threshold": current_threshold,
                        "scores": [round(s, 3) for s in recent_scores],
                        "beam": float(beam) if beam is not None else None,
                        "audio": f"{trigger_audio_id}_{trigger_audio_bundle}_wake.wav",
                    })
                    # Ein-Satz-Kommando: identisch zu Trigger-Pfad 1 — LED sofort,
                    # kein play_wav, Pre-Roll statt flush, "Ja?" verzögert.
                    pre_roll = _pre_roll(wake_ring, _PRE_ROLL_SEC)
                    wake_ring.clear()
                    wake_ring_samples = 0
                    leds.set_phase(LED_RECORDING)
                    voice_controller.set_default_voice(current_wakeword.tts_voice)
                    near_miss_until = 0.0
                    wakeword.reset()
                    state = STATE_RECORDING
                    state_start = time.time()
                    recorded_chunks = list(pre_roll)
                    pre_roll_chunks = len(pre_roll)
                    silence_counter = 0
                    max_internal_pause = 0
                    speech_detected = False
                    turn_mode = "dialog"
                    turn_silence_limit = _silence_limit
                    turn_max_sec = RECORDING_MAX_SEC
                    turn_ein_satz = False
                    turn_speech_chunks = 0
                    turn_rms = []
                    turn_frames_speech = 0
                    turn_frames_total = 0
                    ack_pending = True
                    ack_deadline = now + _ACK_DELAY_SEC
                    ack_path = ack_paths.get(current_wakeword.bundle, PIPER_OUT)
                    wake_hits = 0
                    wake_peak = 0.0
                    wake_announced = False
                    wake_gap_used = False

            # --- RECORDING ---
            elif state == STATE_RECORDING:
                recorded_chunks.append(audio_16.copy())
                _is_speech, _rms, _sf, _tf = _chunk_speech_stats(
                    vad, audio_16, _vad_rms_min
                )
                turn_rms.append(_rms)
                turn_frames_speech += _sf
                turn_frames_total += _tf
                if _is_speech:
                    if speech_detected:
                        max_internal_pause = max(max_internal_pause, silence_counter)
                    speech_detected = True
                    silence_counter = 0
                    turn_speech_chunks += 1
                elif speech_detected:
                    silence_counter += 1

                # Ein-Satz-Kommando: "Ja?" verzögert spielen. Nach _ACK_DELAY_SEC
                # entscheiden: Sprache erkannt → User spricht weiter → kein "Ja?"
                # (LED-Ring reicht). Still → "Ja?" spielen wie bisher.
                # play_wav blockiert ~0,6s — währenddessen läuft Audio weiter in
                # den Puffer, der nächste Loop-Durchlauf holt es ab. KEIN flush.
                if ack_pending and now >= ack_deadline:
                    ack_pending = False
                    # Diese Entscheidung ist die einzige verlässliche Auskunft
                    # darüber, ob durchgesprochen wurde — aus dem Wake-Clip
                    # allein ist sie NICHT rekonstruierbar (er endet, bevor das
                    # nächste Wort beginnt; eine Tail-RMS-Heuristik traf gegen
                    # diese Wahrheit nur 6 von 9). Deshalb mitschreiben: erst
                    # damit lässt sich messen, ob Ein-Satz-Rufe schlechter
                    # triggern als Rufe mit Pause (tools/wake_triage.py).
                    _log_wake_event({
                        "result": "ack",
                        "bundle": current_wakeword.bundle,
                        "ein_satz": speech_detected,
                        "audio": (f"{trigger_audio_id}_{trigger_audio_bundle}_wake.wav"
                                  if trigger_audio_id else None),
                    })
                    if speech_detected:
                        # Ein-Satz heißt in der Praxis: kurzer, knapper
                        # Schaltbefehl, der bewusst am Brain vorbeigeht — da
                        # zählt Tempo, und Denkpausen kommen nicht vor. Der
                        # straffe Nachlauf greift aber erst, wenn genug
                        # Sprache da war (siehe Sperre oben). Er begrenzt
                        # zugleich den Schaden, wenn die Einstufung von
                        # Störgeräusch kam (Fernseher, Vorfall 2026-08-01):
                        # so ein Turn endet nach Sekunden statt am Deckel.
                        turn_ein_satz = True
                        print(f"[{now:.1f}s] ⚡ Ein-Satz erkannt — kein Ja?")
                    elif os.path.exists(ack_path):
                        print(f"[{now:.1f}s] 🔊 Playing acknowledgement...")
                        audio_sink.play_wav(ack_path)

                # Kommando-Modus wird NICHT schon von der Ein-Satz-Einstufung
                # scharf, sondern erst, wenn wirklich gesprochen wurde. Grund:
                # der Ein-Satz-Entscheid springt auch auf den Ausklang des
                # Wakewords an (belegt an 20260730_181211: "Gaston", 2,0 s
                # Pause, dann erst das Kommando — trotzdem ein_satz=true; 30
                # von 37 protokollierten Entscheidungen lauten "Ein-Satz").
                # Ohne diese Sperre stirbt so eine Aufnahme mitten in der
                # Pause und das Kommando ist komplett weg. Mit ihr verhält
                # sich der Fall wie bisher, bis genug Sprache da war.
                if (
                    turn_ein_satz
                    and turn_mode == "dialog"
                    and turn_speech_chunks >= _command_min_speech_chunks
                ):
                    turn_mode = "kommando"
                    turn_silence_limit = _command_silence_limit
                    turn_max_sec = profile.command_max_seconds
                    print(
                        f"[{now:.1f}s] ⚡ Kommando-Modus: "
                        f"{turn_silence_limit * _chunk_sec:.1f}s Nachlauf, "
                        f"max {turn_max_sec:.0f}s"
                    )

                timeout = (now - state_start) > turn_max_sec
                stop = speech_detected and silence_counter >= turn_silence_limit

                if stop or timeout:
                    reason = "silence" if stop else "timeout"
                    pre_roll_sec = pre_roll_chunks * _chunk_sec
                    dur = len(recorded_chunks) * _chunk_sec
                    print(
                        f"[{now:.1f}s] ⏹  Recording stopped ({reason}), "
                        f"{dur:.1f}s audio (davon {pre_roll_sec:.1f}s Pre-Roll)"
                    )
                    ack_pending = False  # egal ob gespielt oder nicht — Turn ist vorbei
                    endpoint_meta = {
                        "phase": "recording",
                        "reason": reason,
                        "dur_s": round(dur, 2),
                        "pre_roll_s": round(pre_roll_sec, 2),
                        "mode": turn_mode,
                        "silence_limit": turn_silence_limit,
                        "silence_seconds": round(turn_silence_limit * _chunk_sec, 2),
                        "max_seconds": turn_max_sec,
                        "max_internal_pause_s": round(max_internal_pause * _chunk_sec, 2),
                        "followup_round": followup_round,
                        **_signal_stats(turn_rms, turn_frames_speech, turn_frames_total),
                    }
                    if trigger_audio_id is not None:
                        _save_trigger_audio(recorded_chunks, trigger_audio_bundle, "rec", trigger_audio_id)
                        trigger_audio_id = None
                    # Der Pre-Roll zählt für MIN_SPEECH_CHUNKS nicht mit — sonst
                    # wäre das Gate allein durch ihn immer erfüllt.
                    if speech_detected and (len(recorded_chunks) - pre_roll_chunks) >= MIN_SPEECH_CHUNKS:
                        leds.set_phase(LED_STT)
                        state = STATE_PROCESSING
                        turn_stt_q = queue.Queue()
                        turn_spk_q = queue.Queue()
                        turn_mood_q = queue.Queue()
                        workers.start_stt(recorded_chunks.copy(), turn_stt_q)
                        workers.start_diarization(recorded_chunks.copy(), turn_spk_q)
                        workers.start_mood(recorded_chunks.copy(), turn_mood_q)
                    else:
                        print(f"[{now:.1f}s] ⚠️  No speech detected")
                        _log_outcome("keine_sprache")
                        _flush_endpoint(ausgang="keine_sprache")
                        leds.set_phase(LED_IDLE)
                        followup_round = 0
                        state = STATE_LISTENING

            # --- PROCESSING (STT running) ---
            elif state == STATE_PROCESSING:
                try:
                    text = turn_stt_q.get_nowait()
                    if pending_confirm is not None:
                        # Ja/Nein-Antwort auf eine Aktuator-Rückfrage (Handshake).
                        try:
                            turn_spk_q.get(timeout=DIARIZATION_JOIN_TIMEOUT)
                        except queue.Empty:
                            pass
                        try:
                            turn_mood_q.get(timeout=DIARIZATION_JOIN_TIMEOUT)
                        except queue.Empty:
                            pass
                        _flush_endpoint(text, ausgang="handshake")
                        confirm_intent = pending_confirm["intent"]
                        confirm_request_id = pending_confirm["request_id"]
                        confirm_resp = None
                        if _is_no(text):
                            print(f"[{now:.1f}s] 🔌 Aktuator: Rückfrage verneint ('{text}') → abgebrochen")
                            speaker.speak("Okay, dann nicht.")
                            antwort = "nein"
                        elif _is_yes(text):
                            antwort = "ja"
                            confirm_resp = actuator.execute(confirm_intent, confirm_request_id, bestaetigt=True)
                            if confirm_resp is None:
                                print(f"[{now:.1f}s] 🔌 Aktuator: Bestätigung — Haussteuerung antwortet nicht")
                                speaker.speak("Die Haussteuerung antwortet nicht.")
                            else:
                                status = confirm_resp.get("status")
                                print(f"[{now:.1f}s] 🔌 Aktuator: Bestätigung ('{text}') → {status}")
                                speaker.speak(confirm_resp.get("gesprochen") or "Erledigt.")
                        else:
                            print(f"[{now:.1f}s] 🔌 Aktuator: Rückfrage-Antwort unverständlich ('{text}') → abgebrochen")
                            speaker.speak("Ich habe das nicht verstanden. Abgebrochen.")
                            antwort = "unverstanden"
                        # Zweite Zeile zum selben request_id: der Überwacher
                        # sieht so, ob eine Rückfrage bestätigt oder verworfen
                        # wurde — sonst endete die Spur bei "zurueckgestellt".
                        _log_actuator_turn({
                            "phase": "handshake",
                            "request_id": confirm_request_id,
                            "transcript": pending_confirm.get("transcript"),
                            "speaker": pending_confirm.get("speaker"),
                            "antwort_transcript": text,
                            "antwort": antwort,
                            "intent": confirm_intent,
                            "status": (confirm_resp or {}).get(
                                "status", "abgebrochen" if antwort != "ja" else "keine_antwort"
                            ),
                            "ausgefuehrt": (confirm_resp or {}).get("ausgefuehrt"),
                            "gesprochen": (confirm_resp or {}).get("gesprochen"),
                        })
                        pending_confirm = None
                        leds.set_phase(LED_IDLE)
                        followup_round = 0
                        # Über PAUSE zurück: speaker.speak() blockiert die
                        # Hauptschleife, das Mikro puffert derweil die eigene
                        # Ansage mit. PAUSE macht audio_source.flush() +
                        # wakeword.reset(), sonst läuft der TTS-Nachhall in die
                        # Wakeword-Erkennung. pending_reply_text leeren, damit
                        # PAUSE keine Follow-up-Runde startet.
                        pending_reply_text[0] = None
                        state = STATE_PAUSE
                        state_start = time.time()
                    elif _is_stop_command(text, followup_round):
                        print(f"[{now:.1f}s] 🛑 Stop word detected: '{text}'")
                        _log_outcome("stopwort", transcript=text)
                        _flush_endpoint(text, ausgang="stopwort")
                        # Diarization- und Mood-Resultat verwerfen damit Queues nicht überlaufen
                        try:
                            turn_spk_q.get(timeout=DIARIZATION_JOIN_TIMEOUT)
                        except queue.Empty:
                            pass
                        try:
                            turn_mood_q.get(timeout=DIARIZATION_JOIN_TIMEOUT)
                        except queue.Empty:
                            pass
                        leds.set_phase(LED_IDLE)
                        followup_round = 0
                        state = STATE_LISTENING
                    elif text:
                        # --- Aktuator-Vorlauf: Schaltkommando? Dann Brain überspringen. ---
                        intent = None
                        if actuator is not None and actuator.ready:
                            intent = actuator.classify(text)
                        if intent is not None and actuator.is_actionable(intent):
                            # Sprecher wird hier nur MITGESCHRIEBEN, nicht
                            # angewandt: der Aktuator antwortet mit Node-REDs
                            # fertigem Satz, eine Sprecher-Stimme braucht er
                            # nicht. Für den Überwacher ist "wer hat das
                            # gesagt" aber Teil des Bildes (späteres
                            # Sprecher-Gate), also wegwerfen wäre schade.
                            try:
                                act_spk = turn_spk_q.get(timeout=DIARIZATION_JOIN_TIMEOUT)
                            except queue.Empty:
                                act_spk = None
                            try:
                                turn_mood_q.get(timeout=DIARIZATION_JOIN_TIMEOUT)
                            except queue.Empty:
                                pass
                            _save_last_recording(recorded_chunks)
                            _flush_endpoint(text, ausgang="aktuator")
                            request_id = str(uuid.uuid4())
                            resp = actuator.execute(intent, request_id)
                            ziel = intent.get("ziel")
                            aktion = intent.get("aktion")
                            _log_actuator_turn({
                                "phase": "intent",
                                "request_id": request_id,
                                "transcript": text,
                                "speaker": act_spk,
                                "wakeword": current_wakeword.bundle,
                                "intent": intent,
                                "latency_ms": round(actuator.last_latency_ms),
                                "status": (resp or {}).get("status", "keine_antwort"),
                                "ausgefuehrt": (resp or {}).get("ausgefuehrt"),
                                "grund": (resp or {}).get("grund"),
                                "gesprochen": (resp or {}).get("gesprochen"),
                            })
                            # Ein ausgefuehrtes Schaltkommando ist der stärkste
                            # freie Beleg, dass der Trigger ein echter Ruf war —
                            # ein Fehltrigger erzeugt so gut wie nie ein gültiges
                            # Intent, das die Haussteuerung auch ausführt.
                            _log_outcome(
                                "aktuator",
                                status=(resp or {}).get("status", "keine_antwort"),
                                ziel=ziel, aktion=aktion, transcript=text,
                            )
                            if overseer is not None:
                                overseer.check_turn({
                                    "ts": datetime.now().isoformat(timespec="seconds"),
                                    "request_id": request_id,
                                    "transcript": text,
                                    "speaker": act_spk,
                                    "intent": intent,
                                    "status": (resp or {}).get("status", "keine_antwort"),
                                    "ausgefuehrt": (resp or {}).get("ausgefuehrt"),
                                    "gesprochen": (resp or {}).get("gesprochen"),
                                })
                            if resp is None:
                                print(
                                    f"[{now:.1f}s] 🔌 Aktuator: {ziel}/{aktion} "
                                    f"({actuator.last_latency_ms:.0f} ms) → keine Antwort"
                                )
                                speaker.speak("Die Haussteuerung antwortet nicht.")
                                leds.set_phase(LED_ERROR)
                                followup_round = 0
                                pending_reply_text[0] = None
                                state = STATE_PAUSE
                                state_start = time.time()
                            else:
                                status = resp.get("status")
                                print(
                                    f"[{now:.1f}s] 🔌 Aktuator: {ziel}/{aktion} "
                                    f"({actuator.last_latency_ms:.0f} ms) → {status}"
                                )
                                if status == "zurueckgestellt":
                                    # Handshake: Rückfrage sprechen, direkt in
                                    # FOLLOWUP wechseln (nicht über PAUSE).
                                    speaker.speak(resp.get("gesprochen") or "Bist du sicher?")
                                    pending_confirm = {
                                        "intent": intent,
                                        "request_id": request_id,
                                        "transcript": text,
                                        "speaker": act_spk,
                                    }
                                    audio_source.flush()
                                    wakeword.reset()
                                    if os.path.exists(FOLLOWUP_BEEP_PATH):
                                        audio_sink.play_wav(FOLLOWUP_BEEP_PATH)
                                    audio_source.flush()
                                    leds.set_phase(LED_FOLLOWUP)
                                    state = STATE_FOLLOWUP
                                    state_start = time.time()
                                    recorded_chunks = []
                                    pre_roll_chunks = 0
                                    silence_counter = 0
                                    max_internal_pause = 0
                                    speech_detected = False
                                    followup_rms_sum = 0.0
                                    followup_rms_count = 0
                                    followup_vad_speech = 0
                                else:
                                    # ausgefuehrt | abgelehnt | unbekanntes_ziel:
                                    # Node-RED liefert 'gesprochen' fertig formuliert.
                                    speaker.speak(resp.get("gesprochen") or "")
                                    leds.set_phase(LED_IDLE)
                                    followup_round = 0
                                    pending_reply_text[0] = None
                                    state = STATE_PAUSE
                                    state_start = time.time()
                        else:
                            if actuator is not None and actuator.ready:
                                print(
                                    f"[{now:.1f}s] 🔌 Aktuator: kein Kommando "
                                    f"({actuator.last_latency_ms:.0f} ms) → Brain"
                                )
                            # --- Brain-Pfad wie bisher (unverändert) ---
                            _save_last_recording(recorded_chunks)
                            try:
                                spk = turn_spk_q.get(timeout=DIARIZATION_JOIN_TIMEOUT)
                            except queue.Empty:
                                print(f"[{now:.1f}s] ⚠️  Diarization timeout — Sprecher unbekannt")
                                spk = None
                            try:
                                mood = turn_mood_q.get(timeout=DIARIZATION_JOIN_TIMEOUT)
                            except queue.Empty:
                                print(f"[{now:.1f}s] ⚠️  Mood timeout — Stimmung unbekannt")
                                mood = None
                            spk_label = spk if spk else "unbekannt"
                            if isinstance(mood, dict):
                                mood_label = f"a{mood.get('arousal', 0):.2f} v{mood.get('valence', 0):.2f} d{mood.get('dominance', 0):.2f}"
                            else:
                                mood_label = ""
                            _flush_endpoint(text, speaker=spk_label, ausgang="brain")
                            print(f"[{now:.1f}s] 📤 Sending to OpenClaw [{spk_label}{' | ' + mood_label if mood_label else ''}]: '{text}'")
                            _log_outcome("brain", transcript=text)
                            # Sprecher-Stimme sofort setzen (async Laden im Hintergrund).
                            # last_speaker nur bei positiver ID überschreiben — ein
                            # nicht zuordenbarer Kurz-Follow-up (spk=None) soll den
                            # zuletzt erkannten Sprecher (= Besitzer einer manuell
                            # gesetzten Stimme) nicht verlieren.
                            if spk:
                                voice_state.set_last_speaker(spk)
                            voice_controller.apply_speaker_default(spk)
                            leds.set_phase(LED_CONFIRMATION)
                            workers.start_confirmation(text)
                            reply_done_event.clear()
                            pending_reply_text[0] = None
                            thinking.start()
                            workers.start_openclaw_turn(
                                text, speaker=spk, mood=mood, session=current_wakeword.session
                            )
                            state = STATE_WAITING
                            state_start = now
                            print(
                                f"[{now:.1f}s] ⏳ Waiting for reply (max {OPENCLAW_OVERALL_TIMEOUT}s)..."
                            )
                    else:
                        print(f"[{now:.1f}s] ⚠️  Empty transcription")
                        _log_outcome("leer")
                        _flush_endpoint(ausgang="leer")
                        try:
                            turn_spk_q.get(timeout=DIARIZATION_JOIN_TIMEOUT)
                        except queue.Empty:
                            pass
                        try:
                            turn_mood_q.get(timeout=DIARIZATION_JOIN_TIMEOUT)
                        except queue.Empty:
                            pass
                        leds.set_phase(LED_IDLE)
                        followup_round = 0
                        state = STATE_LISTENING
                except queue.Empty:
                    if now - state_start > 60.0:
                        print("⚠️  STT timeout!")
                        leds.set_phase(LED_ERROR)
                        # Turn abgebrochen: verwaiste Worker schreiben in diese
                        # (nun dereferenzierten) Queues — nichts sickert durch.
                        turn_stt_q = turn_spk_q = turn_mood_q = None
                        pending_confirm = None
                        followup_round = 0
                        state = STATE_LISTENING

            # --- WAITING (for OpenClaw reply + TTS) ---
            elif state == STATE_WAITING:
                if now - state_start > OPENCLAW_OVERALL_TIMEOUT:
                    print(f"[{now:.1f}s] ⚠️  Overall timeout exceeded")
                    thinking.stop()
                    leds.set_phase(LED_ERROR)
                    state = STATE_LISTENING
                elif reply_done_event.is_set():
                    reply_done_event.clear()
                    state = STATE_PAUSE
                    state_start = now

            # --- PAUSE ---
            elif state == STATE_PAUSE:
                if now - state_start > 1.0:
                    audio_source.flush()
                    wakeword.reset()
                    print(f"[{now:.1f}s] ⏸  PAUSE done, followup_round={followup_round}/{MAX_FOLLOWUP_ROUNDS}")
                    if followup_round < MAX_FOLLOWUP_ROUNDS and pending_reply_text[0] is not None:
                        followup_round += 1
                        print(f"[{now:.1f}s] 🔄 Follow-up round {followup_round}/{MAX_FOLLOWUP_ROUNDS}")
                        if os.path.exists(FOLLOWUP_BEEP_PATH):
                            audio_sink.play_wav(FOLLOWUP_BEEP_PATH)
                        audio_source.flush()
                        leds.set_phase(LED_FOLLOWUP)
                        state = STATE_FOLLOWUP
                        state_start = time.time()
                        recorded_chunks = []
                        pre_roll_chunks = 0
                        silence_counter = 0
                        max_internal_pause = 0
                        speech_detected = False
                        # Follow-ups sind Dialog, kein Kommando: der Nutzer
                        # antwortet auf eine Rückfrage und darf überlegen.
                        # Explizit zurücksetzen — sonst schleppt ein
                        # vorangegangener Ein-Satz-Turn seinen straffen
                        # Nachlauf in die Folgerunde.
                        turn_mode = "dialog"
                        turn_silence_limit = _silence_limit
                        turn_max_sec = RECORDING_MAX_SEC
                        turn_ein_satz = False
                        turn_speech_chunks = 0
                        turn_rms = []
                        turn_frames_speech = 0
                        turn_frames_total = 0
                        followup_rms_sum = 0.0
                        followup_rms_count = 0
                        followup_vad_speech = 0
                    else:
                        followup_round = 0
                        leds.set_phase(LED_IDLE)
                        state = STATE_LISTENING
                        print(f"[{now:.1f}s] 🎤 Ready – waiting for wakeword...")

            # --- FOLLOWUP (nach Antwort automatisch zuhören) ---
            elif state == STATE_FOLLOWUP:
                recorded_chunks.append(audio_16.copy())
                rms = float(np.sqrt(np.mean((audio_16.astype(np.float32) / 32768.0) ** 2)))
                followup_rms_sum += rms
                followup_rms_count += 1
                _is_speech, _rms_abs, _sf, _tf = _chunk_speech_stats(
                    vad, audio_16, _vad_rms_min
                )
                turn_rms.append(_rms_abs)
                turn_frames_speech += _sf
                turn_frames_total += _tf
                if _is_speech:
                    if speech_detected:
                        max_internal_pause = max(max_internal_pause, silence_counter)
                    speech_detected = True
                    silence_counter = 0
                    followup_vad_speech += 1
                elif speech_detected:
                    silence_counter += 1

                timeout = (now - state_start) > turn_max_sec
                stop = speech_detected and silence_counter >= turn_silence_limit

                if stop or timeout:
                    avg_rms = followup_rms_sum / followup_rms_count if followup_rms_count else 0.0
                    vad_str = f"{followup_vad_speech}/{followup_rms_count}"
                    reason = "Stille" if stop else "Timeout"
                    dur = len(recorded_chunks) * _chunk_sec
                    print(
                        f"[{now:.1f}s] ⏹  Follow-up beendet ({reason}), "
                        f"{dur:.1f}s, RMS={avg_rms:.4f}, VAD={vad_str}"
                    )
                    vad_ratio = (
                        followup_vad_speech / followup_rms_count
                        if followup_rms_count else 0.0
                    )
                    endpoint_meta = {
                        "phase": "followup",
                        "reason": "silence" if stop else "timeout",
                        "dur_s": round(dur, 2),
                        "mode": turn_mode,
                        "silence_limit": turn_silence_limit,
                        "silence_seconds": round(turn_silence_limit * _chunk_sec, 2),
                        "max_seconds": turn_max_sec,
                        "max_internal_pause_s": round(max_internal_pause * _chunk_sec, 2),
                        "avg_rms": round(avg_rms, 4),
                        "vad_ratio": round(vad_ratio, 2),
                        "followup_round": followup_round,
                        **_signal_stats(turn_rms, turn_frames_speech, turn_frames_total),
                    }
                    # VAD-Anteil-Gate: echte Äußerungen liegen bei >= 50 %
                    # Sprachanteil, Halluzinations-Aufnahmen (Hintergrund-
                    # geräusche) bei ~14 % — unter 20 % gar nicht erst zu STT
                    if (
                        speech_detected
                        and len(recorded_chunks) >= MIN_SPEECH_CHUNKS
                        and vad_ratio >= 0.20
                    ):
                        leds.set_phase(LED_STT)
                        state = STATE_PROCESSING
                        turn_stt_q = queue.Queue()
                        turn_spk_q = queue.Queue()
                        turn_mood_q = queue.Queue()
                        workers.start_stt(recorded_chunks.copy(), turn_stt_q)
                        workers.start_diarization(recorded_chunks.copy(), turn_spk_q)
                        workers.start_mood(recorded_chunks.copy(), turn_mood_q)
                    else:
                        print(f"[{now:.1f}s] 🔇 Follow-up: insufficient speech (VAD {vad_ratio:.0%})")
                        _flush_endpoint(ausgang="followup_zu_wenig_sprache")
                        leds.set_phase(LED_IDLE)
                        pending_confirm = None
                        followup_round = 0
                        state = STATE_LISTENING

            time.sleep(0.001)

    except KeyboardInterrupt:
        print("\n🛑 Shutting down...")
        leds.set_phase(LED_END)
        audio_source.close()
