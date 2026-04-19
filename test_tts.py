#!/usr/bin/env python3
"""Isolierter TTS-Test: Session aufbauen, sprechen, sofort WAV streamen.

Ablauf:
  1. Verbinden + Button drücken → ESP startet Session
  2. User spricht → ESP VAD → handle_stop
  3. Sofort WAV über TTS_STREAM_START + audio + TTS_STREAM_END + RUN_END senden
  4. Kein STT / kein OpenClaw — reiner Speaker-Test

Verwendung:
  source /home/pi/openclaw_voice_assist/ow-venv/bin/activate
  python test_tts.py
"""
import asyncio
import sys
import wave
import numpy as np

sys.path.insert(0, "/home/pi/openclaw_voice_assist")

HOST = "respeaker-openclaw.local"
PORT = 6053
WAV  = "/home/pi/.openclaw/workspace/ja.wav"


def _resample_to_16k(samples: np.ndarray, src_rate: int) -> np.ndarray:
    if src_rate == 16000:
        return samples
    from scipy.signal import resample_poly
    from math import gcd
    g = gcd(src_rate, 16000)
    return resample_poly(samples, 16000 // g, src_rate // g).astype(np.int16)


async def main():
    import aioesphomeapi

    print(f"Verbinde mit {HOST}:{PORT} …")
    client = aioesphomeapi.APIClient(HOST, PORT, password=None)
    await client.connect(login=True)
    info = await client.device_info()
    print(f"Verbunden: {info.name}")

    # Button-Key suchen
    entities, _ = await client.list_entities_services()
    button_key = None
    for e in entities:
        if hasattr(e, "name") and "Listening" in e.name:
            button_key = e.key
            print(f"Button 'Start Listening' key={e.key}")
        print(f"  Entity: {type(e).__name__} name={getattr(e, 'name', '?')} key={getattr(e, 'key', '?')}")

    stop_event = asyncio.Event()
    start_event = asyncio.Event()

    async def handle_start(conversation_id, flags, audio_settings, wake_word_phrase):
        print(f"[handle_start] conversation_id={conversation_id} flags={flags}")
        start_event.set()
        return 0  # API_AUDIO Modus

    async def handle_stop(abort: bool):
        print(f"[handle_stop] abort={abort}")
        stop_event.set()

    async def handle_audio(data: bytes):
        pass  # nicht nötig für diesen Test

    client.subscribe_voice_assistant(
        handle_start=handle_start,
        handle_stop=handle_stop,
        handle_audio=handle_audio,
    )

    print("Drücke Start-Listening-Button …")
    await asyncio.sleep(1)
    client.button_command(button_key)

    print("Warte auf Session-Start …")
    await asyncio.wait_for(start_event.wait(), timeout=10)
    print(f"Session gestartet (stop_event würde bei handle_stop feuern).")
    print("Warte 3s dann sende TTS direkt — ohne auf VAD-Ende zu warten …")
    await asyncio.sleep(3)

    # WAV laden + auf 16 kHz resamplen
    with wave.open(WAV, "rb") as wf:
        n_ch = wf.getnchannels()
        rate = wf.getframerate()
        raw  = wf.readframes(wf.getnframes())
    samples = np.frombuffer(raw, dtype=np.int16)
    if n_ch > 1:
        samples = samples.reshape(-1, n_ch)[:, 0]
    samples = _resample_to_16k(samples, rate)
    pcm = samples.tobytes()
    duration_s = len(samples) / 16000
    print(f"WAV: {rate} Hz → 16000 Hz, {len(samples)} Samples, {duration_s:.2f}s, {len(pcm)} Bytes")

    # TTS streamen
    print("Sende VOICE_ASSISTANT_TTS_STREAM_START …")
    client.send_voice_assistant_event(
        aioesphomeapi.VoiceAssistantEventType.VOICE_ASSISTANT_TTS_STREAM_START, None
    )

    print(f"Sende {len(pcm) // 1024 + 1} Audio-Chunks …")
    for i in range(0, len(pcm), 1024):
        client.send_voice_assistant_audio(pcm[i : i + 1024])
        await asyncio.sleep(0)

    print("Sende VOICE_ASSISTANT_TTS_STREAM_END …")
    client.send_voice_assistant_event(
        aioesphomeapi.VoiceAssistantEventType.VOICE_ASSISTANT_TTS_STREAM_END, None
    )

    print(f"Warte {duration_s:.1f}s auf Wiedergabe …")
    await asyncio.sleep(duration_s + 1.0)

    print("Sende VOICE_ASSISTANT_RUN_END …")
    client.send_voice_assistant_event(
        aioesphomeapi.VoiceAssistantEventType.VOICE_ASSISTANT_RUN_END, None
    )

    print("Fertig. Hast du etwas aus dem Speaker gehört? (Ctrl+C zum Beenden)")
    await asyncio.sleep(3)


asyncio.run(main())
