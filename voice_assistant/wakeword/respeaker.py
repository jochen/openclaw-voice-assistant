"""Wakeword-Trigger, der vom ReSpeaker (ESPHome micro_wakeword) kommt.

Platzhalter — wird in Schritt 2 implementiert.
"""

from __future__ import annotations


class RespeakerWakeword:
    def __init__(self, *args, **kwargs) -> None:
        raise NotImplementedError(
            "Respeaker-Wakeword ist noch nicht implementiert. "
            "Setze mode: local in config.yaml."
        )

    def feed(self, audio_16k): ...
    def reset(self) -> None: ...
