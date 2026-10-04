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


class TurnControl:
    """Abbruch-Schranke eines laufenden Turns ("Stopp Gaston" / Barge-in).

    Ein Turn beginnt mit begin() und bekommt dabei eine fortlaufende Nummer.
    Wer im Hintergrund fuer diesen Turn arbeitet (OpenClaw-Stream, TTS,
    Telegram-Spiegel), fragt vor jedem folgenreichen Schritt cancelled(nr).

    Die Nummer ist der eigentliche Punkt: ein verwaister Worker eines
    abgebrochenen Turns darf den INZWISCHEN gestarteten naechsten Turn weder
    stoppen noch in ihn hineinsprechen. cancelled(nr) ist deshalb auch dann
    True, wenn nr gar nicht mehr der aktuelle Turn ist — dieselbe Ueberlegung
    wie bei den turn-eigenen Queues in assistant.py.

    register_closer() nimmt Dinge auf, die einen Abbruch UEBERHAUPT erst
    wirksam machen: die offene SSE-Verbindung zu OpenClaw (deren Schliessen
    laut dessen Doku den Agent-Run serverseitig abbricht) und den laufenden
    Wiedergabe-Prozess. Ohne sie wuerde ein Abbruch nur ein Flag setzen,
    waehrend der Brain weiterarbeitet.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._turn = 0
        self._cancelled = False
        self._reason = ""
        self._closers: list = []

    def begin(self) -> int:
        """Naechster Turn. Loescht Abbruch-Flag und Closer des vorherigen."""
        with self._lock:
            self._turn += 1
            self._cancelled = False
            self._reason = ""
            self._closers = []
            return self._turn

    @property
    def turn(self) -> int:
        with self._lock:
            return self._turn

    @property
    def reason(self) -> str:
        with self._lock:
            return self._reason

    def register_closer(self, turn: int, fn) -> None:
        """fn wird beim Abbruch dieses Turns gerufen (idempotent halten).

        Ist der Turn beim Registrieren schon abgebrochen oder ueberholt, wird
        fn sofort gerufen — sonst bliebe eine Verbindung offen, die eine
        Mikrosekunde zu spaet aufgebaut wurde.

        turn=None heisst wie in turn_stopped() "kein abbrechbarer Turn": dann
        wird weder registriert noch geschlossen. Ohne diesen Fall wuerde ein
        Aufruf ohne Turn-Nummer als "ueberholt" gelesen und die eben
        geoeffnete Verbindung sofort wieder zugemacht.
        """
        if turn is None:
            return
        with self._lock:
            if turn == self._turn and not self._cancelled:
                self._closers.append(fn)
                return
        try:
            fn()
        except Exception:
            pass

    def cancel(self, reason: str = "") -> bool:
        """Laufenden Turn abbrechen. True, wenn DIESER Aufruf abgebrochen hat.

        Mehrfachaufrufe sind harmlos; nur der erste liefert True (damit die
        Quittung, der Vermerk an den Brain und die Protokollzeile genau einmal
        entstehen).
        """
        with self._lock:
            if self._cancelled:
                return False
            self._cancelled = True
            self._reason = reason
            closers, self._closers = self._closers, []
        for fn in closers:
            try:
                fn()
            except Exception as e:
                print(f"⚠️  Abbruch: Closer fehlgeschlagen: {e}")
        return True

    def cancelled(self, turn: int | None = None) -> bool:
        """True = dieser Turn soll aufhoeren (abgebrochen ODER ueberholt)."""
        with self._lock:
            if turn is not None and turn != self._turn:
                return True
            return self._cancelled


turn_control = TurnControl()


def turn_stopped(turn: int | None) -> bool:
    """True = dieser Turn ist abgebrochen/ueberholt und soll nichts mehr tun.

    turn=None heisst ausdruecklich "gehoert zu keinem abbrechbaren Turn" und
    ist deshalb NIE gestoppt: so sprechen die Quittungen, die Antworten des
    Aktuators und die Ansagen von aussen (speak_server → announce_worker). Ohne
    diese Unterscheidung wuerde ein Abbruch, auf den kein neuer Turn folgt
    (z.B. Barge-in ohne verstaendliche Aufnahme), den Assistenten bis zum
    naechsten Turn stumm schalten — und zwar lautlos.
    """
    return turn is not None and turn_control.cancelled(turn)


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
