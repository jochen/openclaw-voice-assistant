"""Geteilte Laufzeit-Objekte (Events, Queues, Locks, State-Konstanten)."""

import queue
import threading


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

stt_queue: "queue.Queue[str | None]" = queue.Queue()
speaker_queue: "queue.Queue[str | None]" = queue.Queue()
mood_queue: "queue.Queue[str | None]" = queue.Queue()

# Extern angefragte Ansagen (speak_server → announce_worker)
announce_queue: "queue.Queue[str]" = queue.Queue()
# Aktueller State der Hauptschleife — wird von assistant.py gesetzt
current_state: list[int] = [STATE_LISTENING]
