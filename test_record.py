#!/usr/bin/env python3
"""ESP-Audio 5 Sekunden aufnehmen und als WAV speichern."""
import asyncio, wave, struct
import aioesphomeapi

HOST = "respeaker-openclaw.local"
PORT = 6053
OUT  = "/tmp/respeaker_test.wav"
SECS = 5


async def main():
    client = aioesphomeapi.APIClient(HOST, PORT, password=None)
    await client.connect(login=True)
    print("Verbunden. Starte Aufnahme …")

    buf: list[bytes] = []

    async def handle_start(*a): return 0
    async def handle_stop(abort): pass
    async def handle_audio(data: bytes): buf.append(data)

    client.subscribe_voice_assistant(
        handle_start=handle_start, handle_stop=handle_stop, handle_audio=handle_audio
    )
    await asyncio.sleep(0.5)

    entities, _ = await client.list_entities_services()
    btn = next((e for e in entities if 'Listening' in e.name), None)
    if btn:
        client.button_command(btn.key)
        print(f"Button gedrückt — {SECS}s aufnehmen, bitte sprechen …")
    await asyncio.sleep(SECS)

    raw = b"".join(buf)
    with wave.open(OUT, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)   # int16
        wf.setframerate(16000)
        wf.writeframes(raw)
    print(f"Gespeichert: {OUT}  ({len(raw)//2} samples, {len(raw)/32000:.1f}s)")


asyncio.run(main())
