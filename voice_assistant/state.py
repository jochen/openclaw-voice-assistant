"""Geteilte Laufzeit-Objekte (Events, Queues, Locks, State-Konstanten)."""

import queue
import threading


class VoiceState:
    """Threadsicherer Holder für die aktiv gewählte TTS-Stimme + Tempo."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.model: str | None = None    # None = Default aus Profil benutzen
        self.voice: str | None = None
        self.speed: float = 1.0
        self.last_speaker: str | None = None  # zuletzt erkannter Sprecher
        # Sprecher, dem die aktuell aktive TEMPORÄRE Stimme „gehört".
        # None = aktive Stimme ist Profil-Default oder eine gespeicherte
        # Präferenz (kein temporärer Besitz).
        self.voice_owner: str | None = None
        # Zeitpunkt der letzten Stimmen-Anwendung (time.monotonic-Skala).
        self.last_apply_ts: float = 0.0

    def set(
        self,
        model: str | None = None,
        voice: str | None = None,
        speed: float | None = None,
    ) -> None:
        with self._lock:
            if model is not None:
                self.model = model
            if voice is not None:
                self.voice = voice
            if speed is not None:
                self.speed = speed

    def get(self) -> tuple[str | None, str | None, float]:
        with self._lock:
            return self.model, self.voice, self.speed

    def set_last_speaker(self, spk: str | None) -> None:
        with self._lock:
            self.last_speaker = spk

    def get_last_speaker(self) -> str | None:
        with self._lock:
            return self.last_speaker

    def set_voice_owner(self, owner: str | None) -> None:
        with self._lock:
            self.voice_owner = owner

    def get_voice_owner(self) -> str | None:
        with self._lock:
            return self.voice_owner

    def set_last_apply_ts(self, ts: float) -> None:
        with self._lock:
            self.last_apply_ts = ts

    def get_last_apply_ts(self) -> float:
        with self._lock:
            return self.last_apply_ts


voice_state = VoiceState()


class LastSpoken:
    """Threadsicherer Halter für die zuletzt gesprochene Antwort."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.text: str | None = None
        self.wav_path: str | None = None

    def update(self, text: str, wav_path: str) -> None:
        with self._lock:
            self.text = text
            self.wav_path = wav_path

    def get(self) -> tuple[str | None, str | None]:
        with self._lock:
            return self.text, self.wav_path


last_spoken = LastSpoken()

# State-Machine der Hauptschleife
STATE_LISTENING = 0
STATE_RECORDING = 1
STATE_PROCESSING = 2
STATE_WAITING = 3
STATE_PAUSE = 4
STATE_FOLLOWUP = 5

tts_lock = threading.Lock()
reply_done_event = threading.Event()
pending_reply = threading.Event()
pending_reply_text: list[str | None] = [None]

# STT/Diarization/Mood laufen über turn-eigene Queues (assistant.py erzeugt sie
# pro Aufnahme frisch und übergibt sie an die Worker). Bewusst KEINE
# prozessweiten FIFOs mehr: ein einziges nicht abgeholtes Ergebnis (z.B. nach
# STT- oder Diarization-Join-Timeout) hätte eine geteilte Queue dauerhaft um
# eins versetzt, sodass jeder Turn das Transkript des vorherigen verarbeitet.

# Extern angefragte Ansagen (speak_server → announce_worker)
announce_queue: "queue.Queue[str]" = queue.Queue()
# Aktueller State der Hauptschleife — wird von assistant.py gesetzt
current_state: list[int] = [STATE_LISTENING]
