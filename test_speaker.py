#!/usr/bin/env python3
"""Testet ReSpeaker-Speaker via ESPHome Announce-API + lokalem HTTP-Server."""
import asyncio
import http.server
import threading
import os
import socket
import sys
import tempfile
import wave
import numpy as np
from scipy.signal import resample_poly
from math import gcd

sys.path.insert(0, "/home/pi/openclaw_voice_assist")

HOST_ESP = "respeaker-openclaw.local"
PORT_ESP  = 6053
WAV       = "/home/pi/.openclaw/workspace/ja.wav"
HTTP_PORT = 18800


def to_48k_stereo(src: str) -> str:
    """WAV auf 48000 Hz Stereo 16-bit konvertieren."""
    with wave.open(src, "rb") as wf:
        n_ch = wf.getnchannels()
        rate = wf.getframerate()
        raw = wf.readframes(wf.getnframes())
    samples = np.frombuffer(raw, dtype=np.int16)
    if n_ch > 1:
        samples = samples.reshape(-1, n_ch)[:, 0]
    if rate != 48000:
        g = gcd(rate, 48000)
        samples = np.clip(resample_poly(samples, 48000 // g, rate // g), -32768, 32767).astype(np.int16)
    samples = (samples.astype(np.float32) * 1).astype(np.int16)
    stereo = np.column_stack([samples, samples])
    tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False, dir="/tmp")
    with wave.open(tmp.name, "wb") as wf:
        wf.setnchannels(2)
        wf.setsampwidth(2)
        wf.setframerate(48000)
        wf.writeframes(stereo.tobytes())
    print(f"  WAV konvertiert: {rate}Hz mono → 48000Hz stereo ({len(stereo)} Frames)")
    return tmp.name


class LoggingHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(f"  [HTTP] {self.address_string()} → {fmt % args}")


async def main():
    import aioesphomeapi

    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.connect(("8.8.8.8", 80))
    pi_ip = s.getsockname()[0]
    s.close()

    converted = to_48k_stereo(WAV)
    serve_dir = os.path.dirname(converted)
    filename = os.path.basename(converted)

    os.chdir(serve_dir)
    srv = http.server.HTTPServer(("", HTTP_PORT), LoggingHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    media_url = f"http://{pi_ip}:{HTTP_PORT}/{filename}"
    print(f"HTTP-Server läuft. URL: {media_url}")

    client = aioesphomeapi.APIClient(HOST_ESP, PORT_ESP, password=None)
    await client.connect(login=True)
    print(f"Verbunden: {(await client.device_info()).name}")

    # Subscription nötig damit ESP api_client_ setzt und AnnounceFinished zurückschickt
    async def _handle_start(conv_id, flags, audio_settings, wake_word):
        return None  # keine Session starten
    async def _handle_stop(abort):
        pass
    client.subscribe_voice_assistant(handle_start=_handle_start, handle_stop=_handle_stop)
    await asyncio.sleep(0.5)

    print("Sende Announce-Request …")
    try:
        result = await client.send_voice_assistant_announcement_await_response(
            media_id=media_url,
            timeout=30.0,
        )
        print(f"✅ Ergebnis: success={result.success}")
    except Exception as e:
        print(f"❌ Fehler: {e}")


asyncio.run(main())
