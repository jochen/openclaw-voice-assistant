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
        # Zustand des Media-Players (fuer die Wiedergabe-Verfolgung, siehe
        # RespeakerSink.play_wav). Condition statt Event, weil auf einen
        # WECHSEL gewartet wird und nicht auf ein einmaliges Signal.
        self._player_cv = threading.Condition()
        self._player_state = None
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
        with self._player_cv:
            self._player_state = None
            self._player_cv.notify_all()
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

        async def handle_audio(data: bytes, data2: bytes | None = None) -> None:
            # data2 kam mit einer neueren aioesphomeapi-Version dazu (optionales
            # Zusatzfeld) - fuer unsere Single-Channel-Pipeline ohne Belang.
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
            key = getattr(state, "key", None)
            if self._beam_key is not None and key == self._beam_key:
                new_angle = float(getattr(state, "state", 0.0))
                if new_angle != self.beam_angle:
                    log.debug("Beam: LED %d → LED %d (%d°)", int(self.beam_angle), int(new_angle), int(new_angle) * 30)
                self.beam_angle = new_angle
            elif self._player_key is not None and key == self._player_key:
                with self._player_cv:
                    self._player_state = getattr(state, "state", None)
                    self._player_cv.notify_all()
                log.debug("Media-Player: state=%s", self._player_state)

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

    # ------------------------------------------------------------------
    # Wiedergabe über den Media-Player (NICHT über die Announce-API)
    # ------------------------------------------------------------------
    #
    # Die Announce-API (send_voice_assistant_announcement_*) beendet die
    # voice_assistant-Session des ESP: handle_stop feuert, der Audio-Strom
    # reisst ab, und danach muss der Start-Button neu gedrueckt werden. Genau
    # deshalb war der Pi waehrend JEDER Ansage taub — kein Wakeword, kein
    # Barge-in, nichts. Der Media-Player laesst die VA-Session unberuehrt, der
    # Mikrofon-Strom laeuft durch die Wiedergabe hindurch (das Echo nimmt der
    # XVF3800 per AEC weg, er hat die Referenz auf dem I2S-Ausgang).
    #
    # Zweiter Gewinn: eine Announce-Wiedergabe war nicht abbrechbar, ein
    # Media-Player-STOP ist es.

    def _player_command(self, **kwargs) -> bool:
        """Media-Player-Befehl aus einem fremden Thread absetzen.

        Ueber call_soon_threadsafe und nicht direkt wie die LED-Befehle in
        services/leds.py: der asyncio-Transport ist nicht threadsicher, und
        ein verschluckter Wiedergabe-Befehl laesst einen Turn haengen (eine
        verschluckte LED-Farbe nicht).
        """
        loop, api, key = self._loop, self._api, self._player_key
        if loop is None or api is None or key is None:
            return False
        try:
            loop.call_soon_threadsafe(
                lambda: api.media_player_command(key, **kwargs)
            )
            return True
        except Exception as exc:
            log.warning("media_player_command failed: %s", exc)
            return False

    def play_url(self, url: str) -> bool:
        """Spielt eine URL als Ansage. True, wenn der Befehl abgesetzt wurde.

        Das ist genau der Aufruf, den ESPHomes voice_assistant-Komponente in
        ``on_announce`` selbst macht (media_player mit media_url +
        announcement) — wir umgehen also nur ihre State-Machine, nicht ihren
        Wiedergabe-Weg. EIN Unterschied bleibt und ist der erste Verdaechtige,
        falls im Betrieb etwas klemmt: ESPHome setzt dort zusaetzlich
        ``command=ENQUEUE``. Hier bewusst nicht — "jetzt spielen" ist die
        Semantik, die wir brauchen, und eine Warteschlange koennte nach einem
        Abbruch (STOP) einen Rest-Eintrag behalten. Laut ESPHomes eigenem
        Kommentar spielt auch ein ENQUEUE bei leerer Liste sofort, die beiden
        Wege fallen im Normalfall also zusammen.
        """
        if self._player_key is None:
            log.warning("play_url: keine API-Verbindung / kein Media-Player")
            return False
        with self._player_cv:
            self._player_state = None
        return self._player_command(media_url=url, announcement=True)

    def stop_playback(self) -> None:
        self._player_command(command=aioesphomeapi.MediaPlayerCommand.STOP)

    # Zustaende, die "der Player gibt gerade Ton aus" bedeuten.
    #
    # Gemessen am 2026-09-20 gegen die echte Hardware: eine Ansage ueber
    # media_player_command(media_url=…, announcement=True) laeuft als
    # **PLAYING** (2 → 1), NICHT als ANNOUNCING (4). Die erste Fassung wartete
    # nur auf ANNOUNCING, lief deshalb jedes Mal in die Start-Zeitschranke und
    # kostete 5 s pro Satz. Beide Zustaende zu akzeptieren ist zugleich robust
    # gegen ESPHome-Versionen, die es anders melden.
    _BUSY_STATES = (
        aioesphomeapi.MediaPlayerState.PLAYING,
        aioesphomeapi.MediaPlayerState.ANNOUNCING,
    )

    def wait_player(self, busy: bool, timeout: float) -> bool:
        """Wartet, bis der Player Ton ausgibt (busy=True) bzw. fertig ist.

        False = Zeit abgelaufen, ohne dass der Zustand eintrat. Der Aufrufer
        faellt dann auf die Laenge der WAV-Datei zurueck; ein ausbleibendes
        Zustands-Event darf einen Turn nicht haengen lassen.
        """
        deadline = time.monotonic() + timeout
        with self._player_cv:
            while True:
                if (self._player_state in self._BUSY_STATES) == busy:
                    return True
                rest = deadline - time.monotonic()
                if rest <= 0:
                    return False
                self._player_cv.wait(rest)

    def in_session(self) -> bool:
        return self._in_session

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
    # Zeit, die der ESP bekommt, um eine abgesetzte Ansage aufzugreifen
    # (Datei ziehen + Decoder starten).
    _START_TIMEOUT = 5.0
    # Zuschlag auf die Laenge der Datei, bis das Ende-Ereignis da sein muss.
    _END_MARGIN = 5.0

    def __init__(self, cfg: RespeakerAudio) -> None:
        self._client = get_client(cfg)
        self._serve_dir = tempfile.mkdtemp(prefix="respeaker_tts_")
        self._pi_ip = _get_local_ip()
        self._lock = threading.Lock()
        self._stopped = False
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

    @staticmethod
    def _wav_seconds(path: str) -> float:
        """Laenge der Datei in Sekunden (0.0, wenn nicht lesbar).

        Grundlage der Zeitschranken unten: ohne sie muesste ein ausbleibendes
        Zustands-Event des Players mit einer festen Wartezeit abgefangen
        werden, die entweder Turns haengen laesst oder lange Antworten
        abschneidet.

        **Nur auf die von _to_48k_stereo geschriebene Datei anwenden, nie direkt
        auf eine Speaches-Ausgabe.** Die rechnet hier ueber ``getnframes()``,
        und Speaches setzt das auf den Streaming-Platzhalter 2147483647 (bei
        22050 Hz = 97391 Sekunden). Hier ist es sicher, weil ``dest`` von uns
        selbst mit korrektem Header geschrieben wurde — siehe
        SpeachesTts.synth() fuer die Falle im Ganzen.
        """
        try:
            with wave.open(path, "rb") as wf:
                rate = wf.getframerate()
                return wf.getnframes() / rate if rate else 0.0
        except Exception:
            return 0.0

    def play_wav(self, path: str) -> None:
        client = self._client
        if client._loop is None or client._api is None:
            log.warning("RespeakerSink: no API client available")
            return

        with self._lock:
            if self._stopped:
                return

        filename = f"{os.getpid()}_{threading.get_ident()}.wav"
        dest = os.path.join(self._serve_dir, filename)
        self._to_48k_stereo(path, dest)
        url = f"http://{self._pi_ip}:{self._HTTP_PORT}/{filename}"
        dauer = self._wav_seconds(dest)
        log.info("Play → %s (%.1fs)", url, dauer)

        try:
            if not client.play_url(url):
                return
            # Bis der Player Ton ausgibt (PLAYING/ANNOUNCING, siehe
            # _BUSY_STATES). Faellt das Zustands-Event aus, wird nicht ewig
            # gewartet, sondern unten ueber die Laenge der Datei
            # zurueckgefallen.
            gestartet = client.wait_player(busy=True, timeout=self._START_TIMEOUT)
            if gestartet:
                fertig = client.wait_player(
                    busy=False, timeout=dauer + self._END_MARGIN
                )
                if not fertig:
                    log.warning("Wiedergabe: kein Ende-Zustand nach %.1fs", dauer + self._END_MARGIN)
            else:
                log.warning("Wiedergabe: kein Abspiel-Zustand vom Player — warte %.1fs blind", dauer)
                self._sleep_unless_stopped(dauer + 0.3)
        except Exception as exc:
            log.error("Wiedergabe fehlgeschlagen: %s", exc)
        finally:
            try:
                os.unlink(dest)
            except OSError:
                pass

        # Der Media-Player laesst die voice_assistant-Session in Ruhe, ein
        # Button-Druck ist also im Normalfall nicht mehr noetig. Ist die
        # Session trotzdem weg (Reconnect, ESP-Neustart mitten in der Ansage),
        # wird sie hier geholt — sonst bliebe der Pi stumm-taub.
        if not client.in_session():
            log.info("Keine Mic-Session nach der Wiedergabe → Start-Button")
            client.press_start_button()

    def _sleep_unless_stopped(self, sekunden: float) -> None:
        ende = time.monotonic() + sekunden
        while time.monotonic() < ende:
            with self._lock:
                if self._stopped:
                    return
            time.sleep(0.05)

    def stop(self) -> None:
        with self._lock:
            self._stopped = True
        self._client.stop_playback()

    def resume(self) -> None:
        """Abbruch-Marke loeschen — der naechste Turn darf wieder sprechen."""
        with self._lock:
            self._stopped = False
