"""LED-Status — WLED (lokal) oder ReSpeaker LED-Ring (exklusiv je Modus).

WLED und ReSpeaker werden nie gleichzeitig verwendet:
  mode=local  → WledLeds
  mode=respeaker → RespeakerRing
"""

from __future__ import annotations

import os
import subprocess
import sys

_PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_CONTROLLER = os.path.join(_PROJECT_DIR, "wled_controller.py")

# Phasenkonstanten — müssen mit esphome/respeaker.yaml übereinstimmen
LED_BOOT         = 0
LED_IDLE         = 1
LED_WAKEWORD     = 2
LED_RECORDING    = 3
LED_STT          = 4
LED_CONFIRMATION = 5
LED_OPENCLAW     = 6
LED_ANSWER_GLOW  = 7
LED_AUDIO_OUT    = 8
LED_END          = 9
LED_ERROR        = 10
LED_FOLLOWUP     = 11
LED_NEAR_MISS    = 12

# WLED: Phase → (LED-Index, R, G, B) oder None = alles aus
_WLED_PHASE: dict[int, tuple[int, int, int, int] | None] = {
    LED_BOOT:         None,
    LED_IDLE:         (0, 0, 0, 50),
    LED_WAKEWORD:     (1, 255, 0, 0),
    LED_RECORDING:    (1, 255, 0, 0),
    LED_STT:          (2, 255, 165, 0),
    LED_CONFIRMATION: (2, 255, 200, 0),
    LED_OPENCLAW:     (4, 128, 0, 255),
    LED_ANSWER_GLOW:  (5, 0, 128, 0),
    LED_AUDIO_OUT:    (5, 0, 220, 0),
    LED_END:          None,
    LED_ERROR:        (0, 128, 0, 0),
    LED_FOLLOWUP:     (0, 200, 150, 0),
    LED_NEAR_MISS:    (1, 255, 96, 0),
}


class WledLeds:
    """Steuert den WLED-Streifen über das CLI-Skript."""

    def __init__(self, host: str, enabled: bool = True) -> None:
        self.enabled = enabled
        self.host = host
        self._cmd = [sys.executable, _CONTROLLER, f"--host={host}"]

    def _run(self, args: list[str]) -> None:
        if not self.enabled:
            return
        subprocess.Popen(
            self._cmd + args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def set_phase(self, phase: int) -> None:
        entry = _WLED_PHASE.get(phase)
        self._run(["clear"])
        if entry is not None:
            idx, r, g, b = entry
            self._run(["single", str(idx), str(r), str(g), str(b)])

    def set_boot_step(self, step: int) -> None:
        pass  # WLED hat keine Boot-Sequenz


class RespeakerRing:
    """LED-Ring am ReSpeaker via ESPHome Number-Entities."""

    def __init__(self, cfg: object, enabled: bool = True) -> None:
        from voice_assistant.audio.respeaker import get_client
        self._client = get_client(cfg)  # type: ignore[arg-type]
        self.enabled = enabled

    def _number_command(self, key: int | None, value: float) -> None:
        if not self.enabled or key is None:
            return
        api = self._client._api
        if api:
            api.number_command(key, value)

    def set_phase(self, phase: int) -> None:
        self._number_command(self._client.led_phase_key, float(phase))

    def set_boot_step(self, step: int) -> None:
        self._number_command(self._client.boot_step_key, float(step))


class LedDirector:
    """Verteilt Kommandos auf alle aktiven LED-Senken."""

    def __init__(self, *sinks: object) -> None:
        self.sinks = sinks

    def set_phase(self, phase: int) -> None:
        for sink in self.sinks:
            sink.set_phase(phase)  # type: ignore[attr-defined]

    def set_boot_step(self, step: int) -> None:
        for sink in self.sinks:
            if hasattr(sink, "set_boot_step"):
                sink.set_boot_step(step)
