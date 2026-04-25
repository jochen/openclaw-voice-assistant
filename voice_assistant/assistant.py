"""Hauptloop und State-Machine."""

from __future__ import annotations

import os
import queue
import time
from collections import deque

import numpy as np
import webrtcvad

from voice_assistant.audio.alsa import AlsaSink, AlsaSource
from voice_assistant.audio.respeaker import RespeakerSink, RespeakerSource
from voice_assistant.config import (
    FOLLOWUP_BEEP_PATH,
    MAX_FOLLOWUP_ROUNDS,
    MIN_SPEECH_CHUNKS,
    OPENCLAW_TIMEOUT,
    PIPER_OUT,
    RATE_OW,
    SILENCE_CHUNKS_LIMIT,
    VAD_FRAME_SIZE,
    Profile,
    load_profile,
)
from voice_assistant.services import speaches as speaches_mod
from voice_assistant.services.leds import (
    LED_BOOT, LED_IDLE, LED_WAKEWORD, LED_RECORDING,
    LED_STT, LED_CONFIRMATION, LED_ERROR, LED_NEAR_MISS, LED_FOLLOWUP, LED_END,
    LedDirector, RespeakerRing, WledLeds,
)
from voice_assistant.services.speaches import SpeachesState
from voice_assistant.services.stt import LocalWhisperStt, SpeachesStt, SttPipeline
from voice_assistant.services.tts import (
    ReplySpeaker,
    SpeachesTts,
    ThinkingWorker,
    prerender_followup_beep,
    prerender_ja,
)
from voice_assistant.state import (
    STATE_FOLLOWUP,
    STATE_LISTENING,
    STATE_PAUSE,
    STATE_PROCESSING,
    STATE_RECORDING,
    STATE_WAITING,
    pending_reply_text,
    reply_done_event,
    stt_queue,
)
from voice_assistant.wakeword.openwakeword_engine import OpenWakewordEngine
from voice_assistant.wakeword.respeaker import RespeakerWakeword
from voice_assistant.workers import Workers


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
        return RespeakerWakeword(profile.respeaker)
    return OpenWakewordEngine("hey_jarvis")


def _make_leds(profile: Profile) -> LedDirector:
    sinks = []
    if profile.leds.wled_enabled:
        sinks.append(WledLeds(profile.leds.wled_host, enabled=True))
    if profile.leds.respeaker_ring_enabled and profile.mode == "respeaker":
        sinks.append(RespeakerRing(profile.respeaker, enabled=True))
    return LedDirector(*sinks)


def _is_speech_chunk(vad: webrtcvad.Vad, audio_16) -> bool:
    result = False
    for i in range(0, len(audio_16), VAD_FRAME_SIZE):
        frame = audio_16[i : i + VAD_FRAME_SIZE]
        if len(frame) == VAD_FRAME_SIZE:
            result |= vad.is_speech(frame.tobytes(), RATE_OW)
    return result


def _format_wake_scores(scores: deque) -> str:
    """Formatiert den Score-Verlauf eines Wakeword-Events als Einzeiler.

    Das letzte Element ist der Trigger-Frame (erster Score unter Threshold)
    und wird vom Rückwärts-Search ausgenommen — sonst liefert ein abrupter
    Abfall auf 0.00 fälschlich '(leer)'.
    | markiert die 0.65-Threshold-Kreuzungen (Anstieg und Abfall).
    """
    seq = list(scores)
    if len(seq) < 2:
        return "(leer)"
    trigger = seq[-1]   # letzter Frame: erster below-threshold Score
    search = seq[:-1]   # alles davor für die Rückwärts-Suche
    start = len(search)
    for i in range(len(search) - 1, -1, -1):
        if search[i] < 0.05:
            start = i + 1
            break
        start = i
    event = seq[start:]  # schließt trigger-Frame ein
    if not event or all(s < 0.65 for s in event):
        return "(leer)"
    parts: list[str] = []
    prev_above = event[0] >= 0.65
    for s in event:
        above = s >= 0.65
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

    prerender_ja(profile.locale.wakeword_ack)
    prerender_followup_beep()

    # --- Wakeword + VAD ---
    wakeword = _make_wakeword(profile)
    print("🔧 Initialising WebRTC VAD...")
    vad = webrtcvad.Vad(3)
    print("✅ WebRTC VAD ready")
    leds.set_boot_step(12)  # Wakeword-Modell geladen

    # --- Services zusammenstecken ---
    stt_pipeline = SttPipeline(speaches_stt, local_stt)
    speaker = ReplySpeaker(speaches_tts, audio_sink.play_wav, leds, profile.tts_prefix)
    thinking = ThinkingWorker(audio_sink.play_wav, profile.locale.thinking_phrases)
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
    )

    # --- State-Machine ---
    state = STATE_LISTENING
    state_start = time.time()
    recorded_chunks: list = []
    silence_counter = 0
    speech_detected = False
    wake_hits = 0                           # aufeinanderfolgende Frames über Threshold
    near_miss_until = 0.0                  # Timestamp bis Near-Miss-LED zurückgesetzt wird
    recent_scores: deque[float] = deque(maxlen=30)  # ~1.2s Rolling-Window aller Scores
    followup_round = 0                     # aktuelle Follow-up-Runde (0 = kein Follow-up aktiv)
    followup_rms_sum = 0.0
    followup_rms_count = 0
    followup_vad_speech = 0

    leds.set_phase(LED_IDLE)
    print("\n🎤 Ready – waiting for wakeword...\n")

    try:
        while True:
            audio_16 = audio_source.read_chunk()
            now = time.time()

            # --- LISTENING ---
            if state == STATE_LISTENING:
                if near_miss_until > 0.0 and now >= near_miss_until:
                    leds.set_phase(LED_IDLE)
                    near_miss_until = 0.0

                score = wakeword.feed(audio_16)
                if score is not None:
                    recent_scores.append(score)
                if score is None:
                    pass  # noch 1280 Samples sammeln bevor neue Prediction
                elif score > 0.65:
                    wake_hits += 1
                    if wake_hits == 3:
                        leds.set_phase(LED_WAKEWORD)
                        print(f"[{now:.1f}s] 🟢 Wakeword detected")
                else:
                    beam = getattr(audio_source, "beam_angle", None)
                    beam_str = f"  LED {beam:.0f} ({beam * 30:.0f}°)" if beam is not None else ""
                    if wake_hits >= 3:
                        print(f"[{now:.1f}s] 📊 {_format_wake_scores(recent_scores)}{beam_str}")
                        if os.path.exists(PIPER_OUT):
                            print("🔊 Playing acknowledgement...")
                            audio_sink.play_wav(PIPER_OUT)
                        leds.set_phase(LED_RECORDING)
                        near_miss_until = 0.0
                        wakeword.reset()
                        audio_source.flush()
                        state = STATE_RECORDING
                        state_start = time.time()
                        recorded_chunks = []
                        silence_counter = 0
                        speech_detected = False
                    elif wake_hits >= 1:
                        print(f"[{now:.1f}s] ⚡ Near-Miss ({wake_hits} Frame{'s' if wake_hits > 1 else ''}){beam_str}")
                        leds.set_phase(LED_NEAR_MISS)
                        near_miss_until = now + 0.6
                    wake_hits = 0

                # Sicherheits-Timeout: nicht länger als 1s auf Streak-Ende warten
                if wake_hits >= 25:
                    beam = getattr(audio_source, "beam_angle", None)
                    beam_str = f"  LED {beam:.0f} ({beam * 30:.0f}°)" if beam is not None else ""
                    print(f"[{now:.1f}s] 📊 {_format_wake_scores(recent_scores)} (Timeout){beam_str}")
                    if os.path.exists(PIPER_OUT):
                        print("🔊 Spiele Ja? ...")
                        audio_sink.play_wav(PIPER_OUT)
                    leds.set_phase(LED_RECORDING)
                    near_miss_until = 0.0
                    wakeword.reset()
                    audio_source.flush()
                    state = STATE_RECORDING
                    state_start = time.time()
                    recorded_chunks = []
                    silence_counter = 0
                    speech_detected = False
                    wake_hits = 0

            # --- RECORDING ---
            elif state == STATE_RECORDING:
                recorded_chunks.append(audio_16.copy())
                if _is_speech_chunk(vad, audio_16):
                    speech_detected = True
                    silence_counter = 0
                elif speech_detected:
                    silence_counter += 1

                timeout = (now - state_start) > 15.0
                stop = speech_detected and silence_counter >= SILENCE_CHUNKS_LIMIT

                if stop or timeout:
                    reason = "silence" if stop else "timeout"
                    print(
                        f"[{now:.1f}s] ⏹  Recording stopped ({reason}), "
                        f"{len(recorded_chunks) * 1280 / RATE_OW:.1f}s audio"
                    )
                    if speech_detected and len(recorded_chunks) >= MIN_SPEECH_CHUNKS:
                        leds.set_phase(LED_STT)
                        state = STATE_PROCESSING
                        workers.start_stt(recorded_chunks.copy())
                    else:
                        print(f"[{now:.1f}s] ⚠️  No speech detected")
                        leds.set_phase(LED_IDLE)
                        followup_round = 0
                        state = STATE_LISTENING

            # --- PROCESSING (STT running) ---
            elif state == STATE_PROCESSING:
                try:
                    text = stt_queue.get_nowait()
                    if text:
                        print(f"[{now:.1f}s] 📤 Sending to OpenClaw: '{text}'")
                        leds.set_phase(LED_CONFIRMATION)
                        workers.start_confirmation(text)
                        reply_done_event.clear()
                        pending_reply_text[0] = None
                        thinking.start()
                        workers.start_openclaw_turn(text)
                        state = STATE_WAITING
                        state_start = now
                        print(
                            f"[{now:.1f}s] ⏳ Waiting for reply (max {OPENCLAW_TIMEOUT}s)..."
                        )
                    else:
                        print(f"[{now:.1f}s] ⚠️  Empty transcription")
                        leds.set_phase(LED_IDLE)
                        state = STATE_LISTENING
                except queue.Empty:
                    if now - state_start > 60.0:
                        print("⚠️  STT timeout!")
                        leds.set_phase(LED_ERROR)
                        state = STATE_LISTENING

            # --- WAITING (for OpenClaw reply + TTS) ---
            elif state == STATE_WAITING:
                if now - state_start > OPENCLAW_TIMEOUT + 30:
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
                    if followup_round < MAX_FOLLOWUP_ROUNDS:
                        followup_round += 1
                        print(f"[{now:.1f}s] 🔄 Follow-up round {followup_round}/{MAX_FOLLOWUP_ROUNDS}")
                        if os.path.exists(FOLLOWUP_BEEP_PATH):
                            audio_sink.play_wav(FOLLOWUP_BEEP_PATH)
                        audio_source.flush()
                        leds.set_phase(LED_FOLLOWUP)
                        state = STATE_FOLLOWUP
                        state_start = time.time()
                        recorded_chunks = []
                        silence_counter = 0
                        speech_detected = False
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
                if _is_speech_chunk(vad, audio_16):
                    speech_detected = True
                    silence_counter = 0
                    followup_vad_speech += 1
                elif speech_detected:
                    silence_counter += 1

                timeout = (now - state_start) > 15.0
                stop = speech_detected and silence_counter >= SILENCE_CHUNKS_LIMIT

                if stop or timeout:
                    avg_rms = followup_rms_sum / followup_rms_count if followup_rms_count else 0.0
                    vad_str = f"{followup_vad_speech}/{followup_rms_count}"
                    reason = "Stille" if stop else "Timeout"
                    dur = len(recorded_chunks) * 1280 / RATE_OW
                    print(
                        f"[{now:.1f}s] ⏹  Follow-up beendet ({reason}), "
                        f"{dur:.1f}s, RMS={avg_rms:.4f}, VAD={vad_str}"
                    )
                    if speech_detected and len(recorded_chunks) >= MIN_SPEECH_CHUNKS:
                        leds.set_phase(LED_STT)
                        state = STATE_PROCESSING
                        workers.start_stt(recorded_chunks.copy())
                    else:
                        print(f"[{now:.1f}s] 🔇 Follow-up: insufficient speech")
                        leds.set_phase(LED_IDLE)
                        followup_round = 0
                        state = STATE_LISTENING

            time.sleep(0.001)

    except KeyboardInterrupt:
        print("\n🛑 Shutting down...")
        leds.set_phase(LED_END)
        audio_source.close()
