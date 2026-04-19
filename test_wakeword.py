#!/usr/bin/env python3
"""Wakeword-Test: ESP-Audio 30 Sekunden lauschen, openwakeword-Score anzeigen.

5-Sekunden-Countdown, dann 30s Lauschen. Sag "hey jarvis" mehrfach.
"""
import asyncio
import time
import sys

import numpy as np

HOST = "respeaker-openclaw.local"
PORT = 6053
SECS = 30
CHUNK_BYTES = 1280  # 640 int16 samples = 40ms @ 16kHz

sys.path.insert(0, "/home/pi/openclaw_voice_assist")


async def main():
    import aioesphomeapi
    from voice_assistant.wakeword.openwakeword_engine import OpenWakewordEngine

    audio_q: asyncio.Queue[bytes] = asyncio.Queue(maxsize=2000)

    async def handle_start(*a):
        return 0

    async def handle_stop(abort):
        pass

    async def handle_audio(data: bytes):
        try:
            audio_q.put_nowait(data)
        except asyncio.QueueFull:
            pass

    client = aioesphomeapi.APIClient(HOST, PORT, password=None)
    await client.connect(login=True)
    print(f"Verbunden: {(await client.device_info()).name}")

    entities, _ = await client.list_entities_services()
    btn = next((e for e in entities if hasattr(e, "name") and "Listening" in e.name), None)

    client.subscribe_voice_assistant(
        handle_start=handle_start,
        handle_stop=handle_stop,
        handle_audio=handle_audio,
    )
    await asyncio.sleep(0.5)

    if btn:
        client.button_command(btn.key)
        print("Start-Listening-Button gedrückt")
    else:
        print("WARNUNG: Kein 'Start Listening'-Button gefunden!")

    for i in range(5, 0, -1):
        print(f"\rStart in {i}s ...  ", end="", flush=True)
        await asyncio.sleep(1)
    print("\rLOSGEHT — sag 'hey jarvis'!          ")

    engine = OpenWakewordEngine("hey_jarvis")
    buf = b""
    max_score = 0.0
    triggered = 0
    chunks_total = 0
    start = time.time()

    print(f"Lausche {SECS}s ... (Score > 0.05 wird angezeigt)")
    while time.time() - start < SECS:
        try:
            data = await asyncio.wait_for(audio_q.get(), timeout=0.1)
        except asyncio.TimeoutError:
            continue
        if not data:
            continue
        chunks_total += 1
        buf += data
        while len(buf) >= CHUNK_BYTES:
            chunk, buf = buf[:CHUNK_BYTES], buf[CHUNK_BYTES:]
            samples = np.frombuffer(chunk, dtype=np.int16).astype(np.float32)
            samples -= samples.mean()
            samples = np.clip(samples * 8, -32768, 32767).astype(np.int16)
            score = engine.feed(samples)
            if score > max_score:
                max_score = score
            if score > 0.05:
                marker = " *** TRIGGER ***" if score > 0.5 else ""
                print(f"  t={time.time()-start:.1f}s  score={score:.3f}{marker}")
                if score > 0.5:
                    triggered += 1

    print(f"\nFertig. Audio-Chunks: {chunks_total}  Max-Score: {max_score:.3f}  Trigger: {triggered}x")


asyncio.run(main())
