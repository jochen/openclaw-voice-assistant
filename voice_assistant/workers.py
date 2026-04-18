"""Hintergrund-Threads: STT, OpenClaw-Turn.

Die Worker bekommen ihre Abhängigkeiten via Konstruktor — kein Modul-State.
"""

from __future__ import annotations

import threading

from voice_assistant.services import openclaw, telegram
from voice_assistant.services.stt import SttPipeline
from voice_assistant.services.tts import ReplySpeaker, ThinkingWorker
from voice_assistant.state import (
    pending_reply_text,
    reply_done_event,
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
    ) -> None:
        self.stt = stt
        self.speaker = speaker
        self.thinking = thinking
        self.openclaw_token = openclaw_token
        self.openclaw_session = openclaw_session
        self.telegram_bot_token = telegram_bot_token
        self.telegram_chat_id = telegram_chat_id

    def start_stt(self, audio_chunks: list) -> threading.Thread:
        t = threading.Thread(
            target=self.stt.run,
            args=(audio_chunks, stt_queue),
            daemon=True,
        )
        t.start()
        return t

    def start_confirmation(self, recognized_text: str) -> threading.Thread:
        t = threading.Thread(
            target=self.speaker.speak,
            args=(f"Ich habe verstanden: {recognized_text}",),
            kwargs={"restore_leds": False},
            daemon=True,
        )
        t.start()
        return t

    def start_openclaw_turn(self, user_text: str) -> threading.Thread:
        t = threading.Thread(
            target=self._openclaw_turn,
            args=(user_text,),
            daemon=True,
        )
        t.start()
        return t

    # --- interne Worker ---
    def _openclaw_turn(self, user_text: str) -> None:
        telegram.send(
            self.telegram_bot_token,
            self.telegram_chat_id,
            user_text,
            prefix="🎤 ",
        )

        full_reply = openclaw.query(
            user_text,
            token=self.openclaw_token,
            session=self.openclaw_session,
            on_done=self.thinking.stop,
        )

        if full_reply:
            print(f"✅ OpenClaw komplett: '{full_reply[:80]}...'")
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
            self.speaker.speak("Entschuldigung, ich konnte keine Antwort erhalten.")

        reply_done_event.set()
