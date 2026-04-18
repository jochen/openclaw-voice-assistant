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
    """Platzhalter für den LED-Ring am ReSpeaker (ESPHome).

    Wird in Schritt 2 gefüllt. Aktuell nop, damit parallele Nutzung mit
    WLED bereits vorbereitet ist und keine NotImplementedError wirft.
    """

    def __init__(self, enabled: bool = False) -> None:
        self.enabled = enabled

    def single(self, idx: int, r: int, g: int, b: int) -> None:
        return

    def clear(self) -> None:
        return


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
