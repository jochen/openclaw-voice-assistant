#!/usr/bin/env python3
"""Diagnose: Button drücken, Audio-Stream prüfen."""
import asyncio
import aioesphomeapi

HOST = "respeaker-openclaw.local"
PORT = 6053


async def main():
    print(f"Verbinde mit {HOST}:{PORT} …")
    client = aioesphomeapi.APIClient(HOST, PORT, password=None)
    await client.connect(login=True)
    print(f"Verbunden: {(await client.device_info()).name}")

    entities, _ = await client.list_entities_services()
    print(f"\nEntitäten:")
    for e in entities:
        print(f"  {type(e).__name__}: key={e.key} name='{e.name}'")

    def on_log(msg):
        print(f"[ESP] {msg.message}")
    client.subscribe_logs(on_log, log_level=aioesphomeapi.LogLevel.LOG_LEVEL_INFO)

    chunks_received = 0

    async def handle_start(conversation_id, flags, audio_settings, wake_word_phrase):
        print(f"\n*** handle_start flags={flags} ***")
        return 0  # API_AUDIO — darf nicht None sein

    async def handle_stop(abort):
        print(f"\n--- handle_stop abort={abort}, chunks bisher={chunks_received}")

    async def handle_audio(data: bytes):
        nonlocal chunks_received
        chunks_received += 1
        if chunks_received % 100 == 1:
            print(f"  Audio fließt: {chunks_received} chunks")

    client.subscribe_voice_assistant(
        handle_start=handle_start,
        handle_stop=handle_stop,
        handle_audio=handle_audio,
    )

    await asyncio.sleep(1)

    btn = next((e for e in entities if hasattr(e, 'name') and 'Listening' in e.name), None)
    if btn:
        print(f"\nDrücke 'Start Listening' Button (key={btn.key}) …")
        client.button_command(btn.key)
    else:
        print("\nKEIN Start-Listening-Button! Entitäten:", [(e.name) for e in entities])

    print("Warte 20 Sekunden — sprich ins Mikrofon …")
    await asyncio.sleep(20)
    print(f"\nFertig. Gesamt chunks: {chunks_received}")


asyncio.run(main())
