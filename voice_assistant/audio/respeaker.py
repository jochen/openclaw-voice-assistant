"""Audio-Quelle/Senke via ESPHome Native API (ReSpeaker + XIAO ESP32S3).

Mic-Stream:  voice_assistant-Session (API_AUDIO-Modus) → Pi liest PCM-Chunks
TTS-Output:  announce API → ESP lädt WAV via HTTP → media_player → aic3104

RespeakerClient verwaltet die aioesphomeapi-Verbindung in einem eigenen
asyncio-Thread. Audio wird kontinuierlich via voice_assistant gestreamt.
TTS läuft vollständig unabhängig via announce (keine Session-State-Abhängigkeit).
"""

from __future__ import annotations

import asyncio
import http.server
import logging
import os
import queue
import shutil
import socket
import tempfile
import threading
import wave

import numpy as np
from scipy.signal import resample_poly

import aioesphomeapi

from voice_assistant.config import CHUNK_SIZE, RespeakerAudio

log = logging.getLogger(__name__)

_SAMPLES_PER_CHUNK = CHUNK_SIZE // 2  # 640 int16-Samples = 40 ms @ 16 kHz

_clients: dict[tuple[str, int], RespeakerClient] = {}
_clients_lock = threading.Lock()


def get_client(cfg: RespeakerAudio) -> RespeakerClient:
    key = (cfg.host, cfg.port)
    with _clients_lock:
        if key not in _clients:
            client = RespeakerClient(cfg)
            client.start()
            _clients[key] = client
        return _clients[key]


def _gcd(a: int, b: int) -> int:
    while b:
        a, b = b, a % b
    return a


class RespeakerClient:
    """Asyncio ESPHome client, bridged to the sync state-machine via queues.

    Ablauf:
    1. Verbinden → "Start Listening"-Button suchen
    2. Button drücken → ESP startet voice_assistant-Session → handle_start
    3. handle_audio liefert PCM-Chunks → audio_q
    4. TTS: RespeakerSink ruft announce API (HTTP) direkt über _api auf
    5. Nach TTS: Button erneut drücken → neue Session
    """

    def __init__(self, cfg: RespeakerAudio) -> None:
        self._cfg = cfg
        self._audio_q: queue.Queue[bytes] = queue.Queue(maxsize=500)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._api: aioesphomeapi.APIClient | None = None
        self._button_key: int | None = None
        self._player_key: int | None = None
        self.led_phase_key: int | None = None  # von RespeakerRing gelesen
        self._buf = b""
        self._in_session = False
        self._thread = threading.Thread(
            target=self._run_loop, daemon=True, name="respeaker-api"
        )

    def start(self) -> None:
        self._thread.start()

    # ------------------------------------------------------------------
    # Internal asyncio main loop
    # ------------------------------------------------------------------

    def _run_loop(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._main())
        except Exception as exc:
            log.error("RespeakerClient loop crashed: %s", exc)

    async def _main(self) -> None:
        self._api = aioesphomeapi.APIClient(
            self._cfg.host,
            self._cfg.port,
            password=None,
            noise_psk=self._cfg.encryption_key or None,
        )
        log.info("Verbinde mit ReSpeaker %s:%d …", self._cfg.host, self._cfg.port)
        await self._api.connect(login=True)
        log.info("ReSpeaker verbunden")

        entities, _ = await self._api.list_entities_services()
        for e in entities:
            if hasattr(e, "name") and "Listening" in e.name:
                self._button_key = e.key
                log.info("Start-Listening-Button key=%d", e.key)
            if hasattr(e, "name") and "Player" in e.name:
                self._player_key = e.key
                log.info("Media-Player key=%d", e.key)
            if hasattr(e, "name") and "LED Phase" in e.name:
                self.led_phase_key = e.key
                log.info("LED-Phase-Number key=%d", e.key)

        if self._player_key is not None:
            self._api.media_player_command(self._player_key, volume=self._cfg.volume)
            log.info("Lautstärke auf %.0f%% gesetzt", self._cfg.volume * 100)

        async def handle_start(
            conversation_id: str,
            flags: int,
            audio_settings: aioesphomeapi.VoiceAssistantAudioSettings,
            wake_word_phrase: str | None,
        ) -> int | None:
            self._in_session = True
            return 0  # API_AUDIO-Modus

        async def handle_stop(abort: bool) -> None:
            self._in_session = False
            self._audio_q.put(b"")  # EOS

        async def handle_audio(data: bytes) -> None:
            try:
                self._audio_q.put_nowait(data)
            except queue.Full:
                pass

        self._api.subscribe_voice_assistant(
            handle_start=handle_start,
            handle_stop=handle_stop,
            handle_audio=handle_audio,
        )

        await asyncio.sleep(2)
        await self._press_start_button()
        await asyncio.Future()  # run forever

    async def _press_start_button(self) -> None:
        if self._api and self._button_key is not None:
            try:
                self._api.button_command(self._button_key)
                log.info("Start-Listening-Button gedrückt")
            except Exception as exc:
                log.warning("button_command fehlgeschlagen: %s", exc)

    def press_start_button(self) -> None:
        """Aus Sync-Kontext: neue Session starten."""
        if self._loop:
            asyncio.run_coroutine_threadsafe(self._press_start_button(), self._loop)

    # ------------------------------------------------------------------
    # Sync API — State-Machine-Thread
    # ------------------------------------------------------------------

    def read_chunk(self) -> np.ndarray:
        """Gibt genau _SAMPLES_PER_CHUNK int16-Samples (16 kHz mono) zurück."""
        target = _SAMPLES_PER_CHUNK * 2  # Bytes
        while len(self._buf) < target:
            try:
                data = self._audio_q.get(timeout=0.15)
            except queue.Empty:
                self._buf = b""
                return np.zeros(_SAMPLES_PER_CHUNK, dtype=np.int16)
            if data == b"":  # EOS
                self._buf = b""
                return np.zeros(_SAMPLES_PER_CHUNK, dtype=np.int16)
            self._buf += data

        chunk, self._buf = self._buf[:target], self._buf[target:]
        samples = np.frombuffer(chunk, dtype=np.int16).astype(np.float32)
        samples -= samples.mean()
        samples = np.clip(samples * 4, -32768, 32767).astype(np.int16)
        return samples

    def flush(self) -> None:
        """Queue leeren."""
        while not self._audio_q.empty():
            try:
                self._audio_q.get_nowait()
            except queue.Empty:
                break
        self._buf = b""


# ---------------------------------------------------------------------------
# HTTP-Server für TTS-WAV-Dateien
# ---------------------------------------------------------------------------

def _make_http_handler(serve_dir: str):
    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            filepath = os.path.join(serve_dir, self.path.lstrip("/"))
            if not os.path.isfile(filepath):
                self.send_response(404)
                self.end_headers()
                return
            with open(filepath, "rb") as f:
                data = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "audio/wav")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, fmt, *args):
            log.debug("[HTTP] " + fmt, *args)

    return _Handler


def _get_local_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.connect(("8.8.8.8", 80))
    ip = s.getsockname()[0]
    s.close()
    return ip


# ---------------------------------------------------------------------------
# High-level Source / Sink wrappers
# ---------------------------------------------------------------------------


class RespeakerSource:
    def __init__(self, cfg: RespeakerAudio) -> None:
        self._client = get_client(cfg)

    def start(self) -> None:
        pass

    def read_chunk(self) -> np.ndarray:
        return self._client.read_chunk()

    def flush(self) -> None:
        self._client.flush()

    def close(self) -> None:
        pass


class RespeakerSink:
    """TTS via ESPHome announce API: Pi → HTTP → ESP media_player → aic3104."""

    _HTTP_PORT = 18800

    def __init__(self, cfg: RespeakerAudio) -> None:
        self._client = get_client(cfg)
        self._serve_dir = tempfile.mkdtemp(prefix="respeaker_tts_")
        self._pi_ip = _get_local_ip()
        self._start_http_server()

    def _start_http_server(self) -> None:
        handler = _make_http_handler(self._serve_dir)
        srv = http.server.HTTPServer(("", self._HTTP_PORT), handler)
        threading.Thread(target=srv.serve_forever, daemon=True, name="respeaker-http").start()
        log.info("TTS-HTTP-Server auf :%d (%s)", self._HTTP_PORT, self._serve_dir)

    @staticmethod
    def _to_48k_stereo(src: str, dst: str) -> None:
        """WAV auf 48000 Hz Stereo 16-bit konvertieren (ESP erwartet das exakt)."""
        with wave.open(src, "rb") as wf:
            n_ch = wf.getnchannels()
            rate = wf.getframerate()
            raw = wf.readframes(wf.getnframes())
        samples = np.frombuffer(raw, dtype=np.int16)
        if n_ch > 1:
            samples = samples.reshape(-1, n_ch)[:, 0]
        if rate != 48000:
            from math import gcd
            g = gcd(rate, 48000)
            samples = np.clip(resample_poly(samples, 48000 // g, rate // g), -32768, 32767).astype(np.int16)

        # Fade-in/out (10ms) gegen Knacken bei DAC-Transient
        fade = int(48000 * 0.010)
        if len(samples) > fade * 2:
            samples = samples.astype(np.float32)
            samples[:fade] *= np.linspace(0, 1, fade)
            samples[-fade:] *= np.linspace(1, 0, fade)
            samples = samples.astype(np.int16)

        # 30ms Stille davor/danach — DAC-Settle-Zeit
        silence = np.zeros(int(48000 * 0.030), dtype=np.int16)
        samples = np.concatenate([silence, samples, silence])

        stereo = np.column_stack([samples, samples])
        with wave.open(dst, "wb") as wf:
            wf.setnchannels(2)
            wf.setsampwidth(2)
            wf.setframerate(48000)
            wf.writeframes(stereo.tobytes())

    def play_wav(self, path: str) -> None:
        client = self._client
        if client._loop is None or client._api is None:
            log.warning("RespeakerSink: kein API-Client verfügbar")
            return

        filename = f"{os.getpid()}_{threading.get_ident()}.wav"
        dest = os.path.join(self._serve_dir, filename)
        self._to_48k_stereo(path, dest)
        url = f"http://{self._pi_ip}:{self._HTTP_PORT}/{filename}"
        log.info("Announce → %s", url)

        fut = asyncio.run_coroutine_threadsafe(
            client._api.send_voice_assistant_announcement_await_response(
                media_id=url, timeout=60.0
            ),
            client._loop,
        )
        try:
            result = fut.result(timeout=65.0)
            log.info("Announce abgeschlossen: success=%s", result.success)
        except Exception as exc:
            log.error("Announce fehlgeschlagen: %s", exc)
        finally:
            try:
                os.unlink(dest)
            except OSError:
                pass

        # Neue Mic-Session nach TTS starten
        client.press_start_button()
