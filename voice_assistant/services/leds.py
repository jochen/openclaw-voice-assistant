"""LED-Status — WLED über das vorhandene wled_controller.py Skript.

Das Skript läuft bewusst als Subprozess (wie bisher), weil es im Venv liegt
und wir keine zusätzliche Importzeit im Hauptprozess haben wollen.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

_PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_CONTROLLER = os.path.join(_PROJECT_DIR, "wled_controller.py")


class WledLeds:
    """Steuert den WLED-Streifen über das CLI-Skript."""

    def __init__(self, host: str, enabled: bool = True) -> None:
        self.enabled = enabled
        self.host = host
        self._cmd = [sys.executable, _CONTROLLER, f"--host={host}"]

    def _run(self, args: list[str]) -> None:
        if not self.enabled:
            return
        subprocess.run(
            self._cmd + args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def single(self, idx: int, r: int, g: int, b: int) -> None:
        self._run(["single", str(idx), str(r), str(g), str(b)])

    def clear(self) -> None:
        self._run(["clear"])
        time.sleep(0.05)


class RespeakerRing:
    """LED-Ring am ReSpeaker via ESPHome Number-Entity 'LED Phase'.

    Phasen: 0=idle(blau), 1=listening(grün), 2=thinking(gelb), 3=replying(cyan)
    """

    # assistant.py LED-Index → ESP-Phase
    _PHASE: dict[int, int] = {0: 0, 1: 1, 2: 2, 3: 0, 4: 2, 5: 3}

    def __init__(self, cfg: object, enabled: bool = True) -> None:
        import asyncio
        from voice_assistant.audio.respeaker import get_client
        self._client = get_client(cfg)  # type: ignore[arg-type]
        self.enabled = enabled
        self._key: int | None = None
        self._asyncio = asyncio

    def _find_key(self) -> bool:
        if self._key is not None:
            return True
        if self._client._api is None or self._client._loop is None:
            return False
        fut = self._asyncio.run_coroutine_threadsafe(
            self._client._api.list_entities_services(),
            self._client._loop,
        )
        try:
            entities, _ = fut.result(timeout=5.0)
            for e in entities:
                if hasattr(e, "name") and "LED Phase" in e.name:
                    self._key = e.key
                    return True
        except Exception:
            pass
        return False

    def _set_phase(self, phase: int) -> None:
        if not self.enabled:
            return
        if not self._find_key() or self._key is None:
            return
        api = self._client._api
        if api:
            api.number_command(self._key, float(phase))

    def single(self, idx: int, r: int, g: int, b: int) -> None:
        self._set_phase(self._PHASE.get(idx, 0))

    def clear(self) -> None:
        self._set_phase(0)


class LedDirector:
    """Verteilt Status-Kommandos auf alle aktiven LED-Senken."""

    def __init__(self, *sinks: object) -> None:
        self.sinks = sinks

    def single(self, idx: int, r: int, g: int, b: int) -> None:
        for sink in self.sinks:
            sink.single(idx, r, g, b)  # type: ignore[attr-defined]

    def clear(self) -> None:
        for sink in self.sinks:
            sink.clear()  # type: ignore[attr-defined]
