"""Audio-Quelle/Senke via ESPHome Native API (ReSpeaker + XIAO ESP32S3).

Platzhalter — wird in Schritt 2 des Refactorings implementiert (aioesphomeapi).
"""

from __future__ import annotations


class RespeakerSource:
    def __init__(self, *args, **kwargs) -> None:
        raise NotImplementedError(
            "Respeaker-Modus ist noch nicht implementiert. "
            "Setze mode: local in config.yaml."
        )

    def start(self) -> None: ...
    def read_chunk(self): ...
    def flush(self) -> None: ...
    def close(self) -> None: ...


class RespeakerSink:
    def __init__(self, *args, **kwargs) -> None:
        raise NotImplementedError(
            "Respeaker-Sink ist noch nicht implementiert."
        )

    def play_wav(self, path: str) -> None: ...
