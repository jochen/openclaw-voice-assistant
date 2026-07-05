"""Background threads: STT, OpenClaw turn."""

from __future__ import annotations

import threading

from voice_assistant.services import openclaw, telegram
from voice_assistant.services.diarization import SpeachesDiarizer, run_diarization
from voice_assistant.services.leds import LED_IDLE
from voice_assistant.services.mood import MoodAnalyzer, run_mood
from voice_assistant.services.stt import SttPipeline, chunks_to_wav_bytes
from voice_assistant.services.tts import ReplySpeaker, ThinkingWorker
from voice_assistant.state import (
    STATE_WAITING,
    current_state,
    mood_queue,
    pending_reply_text,
    reply_done_event,
    speaker_queue,
    stt_queue,
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
        # VoiceController wird in assistant.py verwendet (apply_speaker_default
        # vor start_confirmation/start_openclaw_turn). Workers hält die Referenz
        # für späteren Zugriff durch OpenClaw-Tools (HTTP-Endpoint), falls nötig.
        self.voice_controller = voice_controller

    def start_stt(self, audio_chunks: list) -> threading.Thread:
        t = threading.Thread(
            target=self.stt.run,
            args=(audio_chunks, stt_queue),
            daemon=True,
        )
        t.start()
        return t

    def start_diarization(self, audio_chunks: list) -> threading.Thread | None:
        """Diarization parallel zur STT. Ergebnis landet in speaker_queue.

        Wenn kein Diarizer konfiguriert ist, wird sofort None in die Queue
        geschoben — die State-Machine kann sich darauf verlassen, immer ein
        Element abzuholen.
        """
        if self.diarizer is None:
            speaker_queue.put(None)
            return None
        t = threading.Thread(
            target=self._diarize_worker,
            args=(audio_chunks,),
            daemon=True,
        )
        t.start()
        return t

    def _diarize_worker(self, audio_chunks: list) -> None:
        wav_bytes = chunks_to_wav_bytes(audio_chunks)
        run_diarization(self.diarizer, wav_bytes, speaker_queue)

    def start_mood(self, audio_chunks: list) -> threading.Thread | None:
        """Stimmungsanalyse parallel zur STT. Ergebnis landet in mood_queue.

        Wenn kein MoodAnalyzer konfiguriert ist, wird sofort None in die Queue
        geschoben — die State-Machine kann sich darauf verlassen, immer ein
        Element abzuholen.
        """
        if self.mood_analyzer is None:
            mood_queue.put(None)
            return None
        t = threading.Thread(
            target=self._mood_worker,
            args=(audio_chunks,),
            daemon=True,
        )
        t.start()
        return t

    def _mood_worker(self, audio_chunks: list) -> None:
        wav_bytes = chunks_to_wav_bytes(audio_chunks)
        run_mood(self.mood_analyzer, wav_bytes, mood_queue)

    def start_confirmation(self, recognized_text: str) -> threading.Thread:
        t = threading.Thread(
            target=self.speaker.speak,
            args=(f"{self.confirmation_prefix}{recognized_text}",),
            kwargs={"restore_leds": False},
            daemon=True,
        )
        t.start()
        return t

    def start_openclaw_turn(
        self,
        user_text: str,
        speaker: str | None = None,
        mood: dict | None = None,
        session: str | None = None,
    ) -> threading.Thread:
        """session: Routing-Ziel des getriggerten Wakewords (x-openclaw-session-key).
        None → Fallback auf self.openclaw_session (Profil-Default)."""
        t = threading.Thread(
            target=self._openclaw_turn,
            args=(user_text, speaker, mood, session),
            daemon=True,
        )
        t.start()
        return t

    # --- internal workers ---
    def _openclaw_turn(
        self, user_text: str, speaker: str | None = None, mood: dict | None = None,
        session: str | None = None,
    ) -> None:
        try:
            self._run_openclaw_turn(user_text, speaker, mood, session)
        finally:
            # Hat die Hauptschleife den Turn per Overall-Timeout schon verlassen,
            # setzt niemand mehr die LED nach dem (verspäteten) Sprechen zurück —
            # der Ring bliebe sonst dauerhaft auf AUDIO_OUT (grün pulsierend).
            if current_state[0] != STATE_WAITING:
                try:
                    self.speaker.leds.set_phase(LED_IDLE)
                except Exception:
                    pass

    def _run_openclaw_turn(
        self, user_text: str, speaker: str | None = None, mood: dict | None = None,
        session: str | None = None,
    ) -> None:
        # Wakeword-Routing: session_key kommt vom getriggerten Wakeword
        # (assistant.py); None (z.B. altes Aufruf-Schema) fällt auf den
        # Profil-Default zurück. Eigener Name, weil "session" weiter unten
        # bereits für das ReplyStreamSession-Objekt vergeben ist.
        session_key = session or self.openclaw_session
        speaker_label = speaker if speaker else "unbekannt"

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
            session = self.speaker.stream_session(restore_leds=True)

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
                speaker=speaker,
                mood=mood,
                on_sentence=guarded_feed,
                on_first_text=self.thinking.stop,
            )
            spoke = session.end()

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
                self.speaker.speak(full_reply)
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
                speaker=speaker,
                mood=mood,
                on_done=self.thinking.stop,
            )

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
            self.speaker.speak(full_reply)
        else:
            # Echter Leerlauf/Fehler (kein Sentinel): Eingabe spiegeln + Fallback ansagen
            post_user_input()
            pending_reply_text[0] = None
            self.speaker.speak(self.no_reply_fallback)

        reply_done_event.set()
