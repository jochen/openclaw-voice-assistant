"""Geteilte Laufzeit-Objekte (Events, Queues, Locks, State-Konstanten)."""

import queue
import threading

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
