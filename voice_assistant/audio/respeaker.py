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
import time
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
        self._beam_key: int | None = None
        self.led_phase_key: int | None = None   # von RespeakerRing gelesen
        self.boot_step_key: int | None = None   # von RespeakerRing.set_boot_step gelesen
        self.beam_angle: float = 0.0            # aktueller Beam-Winkel in Grad (0–360)
        self._last_led_phase: int = 1           # 1 = LED_IDLE — nach Reconnect wiederherstellen
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

    _RECONNECT_DELAY = 15  # Sekunden bis zum nächsten Verbindungsversuch

    def _reset_state(self) -> None:
        self._api = None
        self._button_key = None
        self._player_key = None
        self.led_phase_key = None
        self.boot_step_key = None
        self._in_session = False
        self._buf = b""
        while not self._audio_q.empty():
            try:
                self._audio_q.get_nowait()
            except queue.Empty:
                break

    def _run_loop(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        while True:
            try:
                self._loop.run_until_complete(self._main())
            except Exception as exc:
                log.error(
                    "RespeakerClient connection error: %s — retry in %ds",
                    exc, self._RECONNECT_DELAY,
                )
            self._reset_state()
            time.sleep(self._RECONNECT_DELAY)

    async def _main(self) -> None:
        self._api = aioesphomeapi.APIClient(
            self._cfg.host,
            self._cfg.port,
            password=None,
            noise_psk=self._cfg.encryption_key or None,
        )
        log.info("Connecting to ReSpeaker %s:%d …", self._cfg.host, self._cfg.port)

        done: asyncio.Future = asyncio.get_event_loop().create_future()

        async def _on_stop(expected: bool) -> None:
            if not done.done():
                done.set_exception(
                    ConnectionError(f"API-Verbindung getrennt (expected={expected})")
                )

        await self._api.connect(login=True, on_stop=_on_stop)
        log.info("ReSpeaker connected")

        entities, _ = await self._api.list_entities_services()
        for e in entities:
            if hasattr(e, "name") and "Listening" in e.name:
                self._button_key = e.key
                log.info("Start-Listening button key=%d", e.key)
            if hasattr(e, "name") and "Player" in e.name:
                self._player_key = e.key
                log.info("Media-Player key=%d", e.key)
            if hasattr(e, "name") and "LED Phase" in e.name:
                self.led_phase_key = e.key
                log.info("LED phase number key=%d", e.key)
            if hasattr(e, "name") and "Boot Step" in e.name:
                self.boot_step_key = e.key
                log.info("Boot step number key=%d", e.key)
            if hasattr(e, "name") and "Voice Direction" in e.name:
                self._beam_key = e.key
                log.info("Beam sensor key=%d", e.key)

        if self._player_key is not None:
            self._api.media_player_command(self._player_key, volume=self._cfg.volume)
            log.info("Volume set to %.0f%%", self._cfg.volume * 100)

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

        def on_state(state: object) -> None:
            if self._beam_key is not None and getattr(state, "key", None) == self._beam_key:
                new_angle = float(getattr(state, "state", 0.0))
                if new_angle != self.beam_angle:
                    log.debug("Beam: LED %d → LED %d (%d°)", int(self.beam_angle), int(new_angle), int(new_angle) * 30)
                self.beam_angle = new_angle

        self._api.subscribe_states(on_state)

        await asyncio.sleep(2)
        if self.led_phase_key is not None:
            self._api.number_command(self.led_phase_key, float(self._last_led_phase))
            log.info("LED phase %d restored after reconnect", self._last_led_phase)
        await self._press_start_button()

        await done  # bricht aus wenn _on_stop feuert (Verbindungsabbruch)

    async def _press_start_button(self) -> None:
        if self._api and self._button_key is not None:
            try:
                self._api.button_command(self._button_key)
                log.info("Start-Listening button pressed")
            except Exception as exc:
                log.warning("button_command failed: %s", exc)

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

    @property
    def beam_angle(self) -> float:
        return self._client.beam_angle


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
        log.info("TTS HTTP server on :%d (%s)", self._HTTP_PORT, self._serve_dir)

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
            log.warning("RespeakerSink: no API client available")
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
            log.info("Announce completed: success=%s", result.success)
        except Exception as exc:
            log.error("Announce failed: %s", exc)
        finally:
            try:
                os.unlink(dest)
            except OSError:
                pass

        # Neue Mic-Session nach TTS starten
        client.press_start_button()
