"""Hauptloop und State-Machine."""

from __future__ import annotations

import os
import queue
import time

import webrtcvad

from voice_assistant.audio.alsa import AlsaSink, AlsaSource
from voice_assistant.audio.respeaker import RespeakerSink, RespeakerSource
from voice_assistant.config import (
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
    LED_STT, LED_CONFIRMATION, LED_ERROR,
    LedDirector, RespeakerRing, WledLeds,
)
from voice_assistant.services.speaches import SpeachesState
from voice_assistant.services.stt import LocalWhisperStt, SpeachesStt, SttPipeline
from voice_assistant.services.tts import (
    ReplySpeaker,
    SpeachesTts,
    ThinkingWorker,
    prerender_ja,
)
from voice_assistant.state import (
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

    prerender_ja()

    # --- Wakeword + VAD ---
    wakeword = _make_wakeword(profile)
    print("🔧 Initialisiere WebRTC VAD...")
    vad = webrtcvad.Vad(3)
    print("✅ WebRTC VAD bereit")
    leds.set_boot_step(12)  # Wakeword-Modell geladen

    # --- Services zusammenstecken ---
    stt_pipeline = SttPipeline(speaches_stt, local_stt)
    speaker = ReplySpeaker(speaches_tts, audio_sink.play_wav, leds, profile.tts_prefix)
    thinking = ThinkingWorker(audio_sink.play_wav)
    workers = Workers(
        stt=stt_pipeline,
        speaker=speaker,
        thinking=thinking,
        openclaw_token=profile.openclaw_token,
        openclaw_session=profile.openclaw_session,
        telegram_bot_token=profile.telegram_bot_token,
        telegram_chat_id=profile.telegram_chat_id,
    )

    # --- State-Machine ---
    state = STATE_LISTENING
    state_start = time.time()
    recorded_chunks: list = []
    silence_counter = 0
    speech_detected = False
    wake_hits = 0  # Debounce: aufeinanderfolgende Frames über Threshold

    leds.set_phase(LED_IDLE)
    print("\n🎤 Bereit – warte auf 'hey jarvis'...\n")

    try:
        while True:
            audio_16 = audio_source.read_chunk()
            now = time.time()

            # --- LISTENING ---
            if state == STATE_LISTENING:
                score = wakeword.feed(audio_16)
                if score > 0.65:
                    wake_hits += 1
                else:
                    wake_hits = 0
                if wake_hits >= 2:
                    print(f"[{now:.1f}s] 🟢 WAKE WORD! Score: {score:.3f}")
                    leds.set_phase(LED_WAKEWORD)
                    if os.path.exists(PIPER_OUT):
                        print("🔊 Spiele Ja? ...")
                        audio_sink.play_wav(PIPER_OUT)
                    leds.set_phase(LED_RECORDING)
                    wake_hits = 0
                    wakeword.reset()
                    audio_source.flush()
                    state = STATE_RECORDING
                    state_start = time.time()
                    recorded_chunks = []
                    silence_counter = 0
                    speech_detected = False

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
                    reason = "Stille erkannt" if stop else "Timeout"
                    print(
                        f"[{now:.1f}s] ⏹  Aufnahme beendet ({reason}), "
                        f"{len(recorded_chunks) * 1280 / RATE_OW:.1f}s Audio"
                    )
                    if speech_detected and len(recorded_chunks) >= MIN_SPEECH_CHUNKS:
                        leds.set_phase(LED_STT)
                        state = STATE_PROCESSING
                        workers.start_stt(recorded_chunks.copy())
                    else:
                        print(f"[{now:.1f}s] ⚠️  Keine Sprache erkannt")
                        leds.set_phase(LED_IDLE)
                        state = STATE_LISTENING

            # --- PROCESSING (STT läuft) ---
            elif state == STATE_PROCESSING:
                try:
                    text = stt_queue.get_nowait()
                    if text:
                        print(f"[{now:.1f}s] 📤 Sende an OpenClaw: '{text}'")
                        leds.set_phase(LED_CONFIRMATION)
                        workers.start_confirmation(text)
                        reply_done_event.clear()
                        pending_reply_text[0] = None
                        thinking.start()
                        workers.start_openclaw_turn(text)
                        state = STATE_WAITING
                        state_start = now
                        print(
                            f"[{now:.1f}s] ⏳ Warte auf Antwort (max {OPENCLAW_TIMEOUT}s)..."
                        )
                    else:
                        print(f"[{now:.1f}s] ⚠️  Leere Transkription")
                        leds.set_phase(LED_IDLE)
                        state = STATE_LISTENING
                except queue.Empty:
                    if now - state_start > 60.0:
                        print("⚠️  STT Timeout!")
                        leds.set_phase(LED_ERROR)
                        state = STATE_LISTENING

            # --- WAITING (auf OpenClaw-Antwort + TTS) ---
            elif state == STATE_WAITING:
                if now - state_start > OPENCLAW_TIMEOUT + 30:
                    print(f"[{now:.1f}s] ⚠️  Gesamt-Timeout überschritten")
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
                    leds.set_phase(LED_IDLE)
                    state = STATE_LISTENING
                    print(f"[{now:.1f}s] 🎤 Bereit – warte auf 'hey jarvis'...")

            time.sleep(0.001)

    except KeyboardInterrupt:
        print("\n🛑 Beende...")
        leds.set_phase(LED_END)
        audio_source.close()
