"""Background threads: STT, OpenClaw turn."""

from __future__ import annotations

import queue
import threading

from voice_assistant.services import openclaw, telegram
from voice_assistant.services.diarization import (
    STATUS_NICHT_EINGERICHTET,
    SpeachesDiarizer,
    SpeakerVerdict,
    run_diarization,
    verdict_from_speaker,
)
from voice_assistant.services.leds import LED_IDLE
from voice_assistant.services.mood import MoodAnalyzer, run_mood
from voice_assistant.services.stt import SttPipeline, chunks_to_wav_bytes
from voice_assistant.services.tts import ReplySpeaker, ThinkingWorker
from voice_assistant.state import (
    STATE_WAITING,
    current_state,
    pending_reply_text,
    reply_done_event,
    turn_control,
    turn_stopped,
)


class Workers:
    def __init__(
        self,
        stt: SttPipeline,
        speaker: ReplySpeaker,
        thinking: ThinkingWorker,
        openclaw_token: str,
        openclaw_session: str,
        telegram_bot_token: str,
        telegram_chat_id: str,
        confirmation_prefix: str = "Ich habe verstanden: ",
        no_reply_fallback: str = "Entschuldigung, ich konnte keine Antwort erhalten.",
        voice_instruction: str = "",
        diarizer: SpeachesDiarizer | None = None,
        mood_analyzer: MoodAnalyzer | None = None,
        use_stream: bool = True,
        voice_controller=None,  # VoiceController | None — nur Halten/Durchreichen
        abort_notify_brain: bool = False,
    ) -> None:
        self.stt = stt
        self.speaker = speaker
        self.thinking = thinking
        self.openclaw_token = openclaw_token
        self.openclaw_session = openclaw_session
        self.telegram_bot_token = telegram_bot_token
        self.telegram_chat_id = telegram_chat_id
        self.confirmation_prefix = confirmation_prefix
        self.no_reply_fallback = no_reply_fallback
        self.voice_instruction = voice_instruction
        self.diarizer = diarizer
        self.mood_analyzer = mood_analyzer
        self.use_stream = use_stream
        # Nach einem Abbruch eine Systemnachricht in dieselbe Session posten
        # (barge_in.notify_brain). Siehe openclaw.notify_abort.
        self.abort_notify_brain = abort_notify_brain
        # VoiceController wird in assistant.py verwendet (apply_speaker_default
        # vor start_confirmation/start_openclaw_turn). Workers hält die Referenz
        # für späteren Zugriff durch OpenClaw-Tools (HTTP-Endpoint), falls nötig.
        self.voice_controller = voice_controller

    def start_stt(self, audio_chunks: list, out_q: queue.Queue) -> threading.Thread:
        """STT im Hintergrund; Ergebnis landet in out_q.

        out_q ist eine turn-eigene Queue (assistant.py erzeugt sie pro Aufnahme).
        So kann das Ergebnis eines abgebrochenen Turns (z.B. STT-Timeout in der
        State-Machine) nie in einen späteren Turn durchsickern — der verwaiste
        Worker schreibt in eine nicht mehr referenzierte Queue.
        """
        t = threading.Thread(
            target=self.stt.run,
            args=(audio_chunks, out_q),
            daemon=True,
        )
        t.start()
        return t

    def start_diarization(
        self, audio_chunks: list, out_q: queue.Queue
    ) -> threading.Thread | None:
        """Diarization parallel zur STT. Ergebnis landet in out_q (turn-eigen).

        Wenn kein Diarizer konfiguriert ist, wird sofort ein Urteil
        "nicht eingerichtet" geschoben — die State-Machine kann sich darauf
        verlassen, immer ein Element abzuholen. Bewusst NICHT "unbekannt":
        ein Profil ohne Erkennung hat nichts gemessen und soll nicht so
        aussehen, als habe es jemanden nicht wiedererkannt.
        """
        if self.diarizer is None:
            out_q.put(SpeakerVerdict(None, STATUS_NICHT_EINGERICHTET))
            return None
        t = threading.Thread(
            target=self._diarize_worker,
            args=(audio_chunks, out_q),
            daemon=True,
        )
        t.start()
        return t

    def _diarize_worker(self, audio_chunks: list, out_q: queue.Queue) -> None:
        wav_bytes = chunks_to_wav_bytes(audio_chunks)
        run_diarization(self.diarizer, wav_bytes, out_q)

    def start_mood(
        self, audio_chunks: list, out_q: queue.Queue
    ) -> threading.Thread | None:
        """Stimmungsanalyse parallel zur STT. Ergebnis landet in out_q (turn-eigen).

        Wenn kein MoodAnalyzer konfiguriert ist, wird sofort None in die Queue
        geschoben — die State-Machine kann sich darauf verlassen, immer ein
        Element abzuholen.
        """
        if self.mood_analyzer is None:
            out_q.put(None)
            return None
        t = threading.Thread(
            target=self._mood_worker,
            args=(audio_chunks, out_q),
            daemon=True,
        )
        t.start()
        return t

    def _mood_worker(self, audio_chunks: list, out_q: queue.Queue) -> None:
        wav_bytes = chunks_to_wav_bytes(audio_chunks)
        run_mood(self.mood_analyzer, wav_bytes, out_q)

    def start_confirmation(self, recognized_text: str, turn: int | None = None) -> threading.Thread:
        t = threading.Thread(
            target=self.speaker.speak,
            args=(f"{self.confirmation_prefix}{recognized_text}",),
            kwargs={"restore_leds": False, "turn": turn},
            daemon=True,
        )
        t.start()
        return t

    def start_openclaw_turn(
        self,
        user_text: str,
        speaker: SpeakerVerdict | str | None = None,
        mood: dict | None = None,
        session: str | None = None,
        turn: int | None = None,
    ) -> threading.Thread:
        """session: Routing-Ziel des getriggerten Wakewords (x-openclaw-session-key).
        None → Fallback auf self.openclaw_session (Profil-Default).

        speaker ist das Urteil der Diarization. Ein blosser Name (altes
        Aufruf-Schema) wird weiterhin angenommen und als "bekannt" gelesen."""
        if not isinstance(speaker, SpeakerVerdict):
            speaker = verdict_from_speaker(speaker)
        t = threading.Thread(
            target=self._openclaw_turn,
            args=(user_text, speaker, mood, session, turn),
            daemon=True,
        )
        t.start()
        return t

    # --- internal workers ---
    def _openclaw_turn(
        self, user_text: str, speaker: SpeakerVerdict | None = None,
        mood: dict | None = None, session: str | None = None,
        turn: int | None = None,
    ) -> None:
        try:
            self._run_openclaw_turn(user_text, speaker, mood, session, turn)
        finally:
            # Hat die Hauptschleife den Turn per Overall-Timeout schon verlassen,
            # setzt niemand mehr die LED nach dem (verspäteten) Sprechen zurück —
            # der Ring bliebe sonst dauerhaft auf AUDIO_OUT (grün pulsierend).
            # Nach einem Abbruch laeuft die Hauptschleife bereits im naechsten
            # Turn (Aufnahme). Dann ist der Ring NICHT verwaist, und ein
            # LED_IDLE von hier wuerde die Aufnahme-Farbe ueberschreiben.
            if current_state[0] != STATE_WAITING and not turn_stopped(turn):
                try:
                    self.speaker.leds.set_phase(LED_IDLE)
                except Exception:
                    pass

    def _finish_cancelled(self, session_key: str) -> None:
        """Aufraeumen nach einem Abbruch (Barge-in).

        Kein Vorlesen, kein Telegram-Spiegel, kein Follow-up: der Nutzer hat
        den Turn gerade weggeworfen, da ist jede Ausgabe ein Widerspruch zur
        Anweisung. pending_reply_text=None haelt das Follow-up-Gate in
        assistant.py zu; reply_done_event wird trotzdem gesetzt, damit eine
        Hauptschleife, die noch in WAITING steht, nicht bis zum Overall-Timeout
        haengt.

        Der Vermerk an den Brain laeuft in einem eigenen Thread: er ist ein
        vollwertiger (wenn auch stummer) Turn und darf den Abschluss hier
        nicht aufhalten.
        """
        print("🛑 Turn abgebrochen — kein Vorlesen, kein Telegram, kein Follow-up")
        self.thinking.stop()
        pending_reply_text[0] = None
        reply_done_event.set()
        if self.abort_notify_brain:
            threading.Thread(
                target=openclaw.notify_abort,
                args=(self.openclaw_token, session_key),
                daemon=True,
            ).start()

    def _run_openclaw_turn(
        self, user_text: str, speaker: SpeakerVerdict | None = None,
        mood: dict | None = None, session: str | None = None,
        turn: int | None = None,
    ) -> None:
        # Wakeword-Routing: session_key kommt vom getriggerten Wakeword
        # (assistant.py); None (z.B. altes Aufruf-Schema) fällt auf den
        # Profil-Default zurück. Eigener Name, weil "session" weiter unten
        # bereits für das ReplyStreamSession-Objekt vergeben ist.
        session_key = session or self.openclaw_session
        verdict = speaker if isinstance(speaker, SpeakerVerdict) else verdict_from_speaker(speaker)
        speaker_label = verdict.label

        # Die User-Eingabe wird erst gespiegelt, sobald eine echte (Nicht-
        # NO_REPLY-)Antwort feststeht. So bleibt der Chat sauber, wenn OpenClaw
        # die Aufnahme als Fernseher/Hintergrund erkennt und NO_REPLY liefert.
        posted = [False]

        def post_user_input() -> None:
            if posted[0]:
                return
            posted[0] = True
            telegram.send(
                self.telegram_bot_token,
                self.telegram_chat_id,
                user_text,
                prefix=f"🎤 [{speaker_label}] ",
            )

        def finish_no_reply(full_reply: str | None) -> None:
            # OpenClaw hat die Eingabe als nicht-adressiert erkannt: komplett
            # abbrechen — keine Audioausgabe, kein Telegram, kein Follow-up
            # (pending_reply_text=None ⇒ Gate in assistant.py bleibt zu).
            print(f"🔇 NO_REPLY → abgebrochen (kein Audio/Telegram/Follow-up): '{full_reply}'")
            pending_reply_text[0] = None
            reply_done_event.set()

        # --- Streaming-Pfad: Sätze werden gesprochen, sobald sie generiert sind ---
        timed_out = False
        if self.use_stream:
            session = self.speaker.stream_session(restore_leds=True, turn=turn)

            def guarded_feed(sentence: str) -> None:
                # NO_REPLY-Sentinel niemals sprechen; echte Sätze posten erst
                # jetzt die User-Eingabe (vor der ersten realen Ausgabe).
                if openclaw.is_no_reply(sentence):
                    return
                post_user_input()
                session.feed(sentence)

            full_reply, timed_out = openclaw.query_stream(
                user_text,
                token=self.openclaw_token,
                session=session_key,
                voice_instruction=self.voice_instruction,
                speaker=verdict.name,
                speaker_label=speaker_label,
                mood=mood,
                on_sentence=guarded_feed,
                on_first_text=self.thinking.stop,
                control=turn_control,
                turn=turn,
            )
            spoke = session.end()

            if turn_stopped(turn):
                self._finish_cancelled(session_key)
                return

            if openclaw.is_no_reply(full_reply):
                finish_no_reply(full_reply)
                return

            if spoke:
                # Antwort wurde (zumindest teilweise) live gesprochen
                print(f"✅ OpenClaw stream complete: '{full_reply or ''}'")
                post_user_input()
                if full_reply:
                    telegram.send(
                        self.telegram_bot_token,
                        self.telegram_chat_id,
                        full_reply,
                        prefix="🔊 ",
                    )
                pending_reply_text[0] = full_reply
                reply_done_event.set()
                return

            if full_reply:
                # Text kam, wurde aber nicht gesprochen (z.B. leer nach clean) → normal
                post_user_input()
                telegram.send(
                    self.telegram_bot_token,
                    self.telegram_chat_id,
                    full_reply,
                    prefix="🔊 ",
                )
                pending_reply_text[0] = full_reply
                self.speaker.speak(full_reply, turn=turn)
                reply_done_event.set()
                return

            if timed_out:
                # Server arbeitet vermutlich noch am Turn: Auftrag NICHT erneut
                # posten (Doppel-Ausführung!), stattdessen Stand/Ergebnis abfragen.
                print("⚠️  Stream-Timeout → Status-Nachfrage statt erneutem Auftrag")
                full_reply = openclaw.query_status(
                    token=self.openclaw_token,
                    session=session_key,
                    on_done=self.thinking.stop,
                )
            else:
                print("⚠️  Streaming ohne Ausgabe → non-streaming Fallback")
                full_reply = None
        else:
            full_reply = None

        # --- Non-streaming-Pfad (Flag aus ODER Streaming lieferte nichts) ---
        if full_reply is None and not (self.use_stream and timed_out):
            full_reply = openclaw.query(
                user_text,
                token=self.openclaw_token,
                session=session_key,
                voice_instruction=self.voice_instruction,
                speaker=verdict.name,
                speaker_label=speaker_label,
                mood=mood,
                on_done=self.thinking.stop,
            )

        if turn_stopped(turn):
            self._finish_cancelled(session_key)
            return

        if openclaw.is_no_reply(full_reply):
            finish_no_reply(full_reply)
            return

        if full_reply:
            print(f"✅ OpenClaw complete: '{full_reply}'")
            post_user_input()
            telegram.send(
                self.telegram_bot_token,
                self.telegram_chat_id,
                full_reply,
                prefix="🔊 ",
            )
            pending_reply_text[0] = full_reply
            self.speaker.speak(full_reply, turn=turn)
        else:
            # Echter Leerlauf/Fehler (kein Sentinel): Eingabe spiegeln + Fallback ansagen
            post_user_input()
            pending_reply_text[0] = None
            self.speaker.speak(self.no_reply_fallback, turn=turn)

        reply_done_event.set()
