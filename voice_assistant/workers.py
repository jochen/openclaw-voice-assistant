"""Background threads: STT, OpenClaw turn."""

from __future__ import annotations

import threading

from voice_assistant.services import openclaw, telegram
from voice_assistant.services.diarization import SpeachesDiarizer, run_diarization
from voice_assistant.services.stt import SttPipeline, chunks_to_wav_bytes
from voice_assistant.services.tts import ReplySpeaker, ThinkingWorker
from voice_assistant.state import (
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
        use_stream: bool = True,
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
        self.use_stream = use_stream

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
        self, user_text: str, speaker: str | None = None
    ) -> threading.Thread:
        t = threading.Thread(
            target=self._openclaw_turn,
            args=(user_text, speaker),
            daemon=True,
        )
        t.start()
        return t

    # --- internal workers ---
    def _openclaw_turn(self, user_text: str, speaker: str | None = None) -> None:
        speaker_label = speaker if speaker else "unbekannt"
        telegram.send(
            self.telegram_bot_token,
            self.telegram_chat_id,
            user_text,
            prefix=f"🎤 [{speaker_label}] ",
        )

        # --- Streaming-Pfad: Sätze werden gesprochen, sobald sie generiert sind ---
        if self.use_stream:
            session = self.speaker.stream_session(restore_leds=True)
            full_reply = openclaw.query_stream(
                user_text,
                token=self.openclaw_token,
                session=self.openclaw_session,
                voice_instruction=self.voice_instruction,
                speaker=speaker,
                on_sentence=session.feed,
                on_first_text=self.thinking.stop,
            )
            spoke = session.end()

            if spoke:
                # Antwort wurde (zumindest teilweise) live gesprochen
                print(f"✅ OpenClaw stream complete: '{(full_reply or '')[:80]}...'")
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

            print("⚠️  Streaming ohne Ausgabe → non-streaming Fallback")

        # --- Non-streaming-Pfad (Flag aus ODER Streaming lieferte nichts) ---
        full_reply = openclaw.query(
            user_text,
            token=self.openclaw_token,
            session=self.openclaw_session,
            voice_instruction=self.voice_instruction,
            speaker=speaker,
            on_done=self.thinking.stop,
        )

        if full_reply:
            print(f"✅ OpenClaw complete: '{full_reply[:80]}...'")
            telegram.send(
                self.telegram_bot_token,
                self.telegram_chat_id,
                full_reply,
                prefix="🔊 ",
            )
            pending_reply_text[0] = full_reply
            self.speaker.speak(full_reply)
        else:
            pending_reply_text[0] = None
            self.speaker.speak(self.no_reply_fallback)

        reply_done_event.set()
