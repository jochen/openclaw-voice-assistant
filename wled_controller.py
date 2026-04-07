#!/usr/bin/env python3
"""
WLED Controller für Status-LEDs (10 LEDs auf D1 Mini)

Host wird per --host=<hostname> übergeben oder fällt auf WLED_HOST zurück.
WLED_HOST kann als Umgebungsvariable gesetzt werden.
"""

import os
import requests
import time
from typing import List, Tuple

WLED_HOST = os.environ.get("WLED_HOST", "wled.local")
WLED_URL = f"http://{WLED_HOST}/json/state"

class WLEDController:
    def __init__(self, host: str = None):
        if host:
            self.host = host
            self.url = f"http://{host}/json/state"
        else:
            self.url = WLED_URL

    def set_power(self, on: bool) -> bool:
        """Ein-/Ausschalten"""
        resp = requests.post(self.url, json={"on": on}, timeout=2)
        return resp.json().get("success", False)

    def set_brightness(self, value: int) -> bool:
        """Helligkeit 0-255"""
        resp = requests.post(self.url, json={"bri": value}, timeout=2)
        return resp.json().get("success", False)

    def set_led(self, index: int, color: Tuple[int, int, int]) -> bool:
        """
        Einzelne LED setzen (Index 0-9)
        color: (R, G, B) jeweils 0-255
        """
        start = index
        stop = index + 1
        resp = requests.post(
            self.url,
            json={"seg": [{"start": start, "stop": stop, "col": [list(color)]}]},
            timeout=2
        )
        return resp.json().get("success", False)

    def set_leds(self, colors: List[Tuple[int, int, int]]) -> bool:
        """
        Mehrere LEDs gleichzeitig setzen (Liste von Farben, Länge = 10)
        """
        if len(colors) != 10:
            raise ValueError("Exactly 10 colors required")
        segments = []
        for i, col in enumerate(colors):
            segments.append({
                "start": i,
                "stop": i + 1,
                "col": [list(col)]
            })
        resp = requests.post(self.url, json={"seg": segments}, timeout=2)
        return resp.json().get("success", False)

    def clear(self) -> bool:
        """Alle LEDs ausschalten"""
        return self.set_power(False)

    def test_all(self) -> bool:
        """Testmuster: Regenbogen über alle 10 LEDs"""
        rainbow = [
            (255, 0, 0),     # Rot
            (255, 165, 0),   # Orange
            (255, 255, 0),   # Gelb
            (0, 255, 0),     # Grün
            (0, 0, 255),     # Blau
            (75, 0, 130),    # Indigo
            (238, 130, 238), # Violett
            (255, 192, 203), # Pink
            (0, 255, 255),   # Cyan
            (255, 0, 255)    # Magenta
        ]
        return self.set_leds(rainbow) and self.set_power(True)

    def single_test(self, index: int, color: Tuple[int, int, int]) -> bool:
        """Einzeltest: eine LED anzeigen, andere aus"""
        colors = [(0,0,0)] * 10
        colors[index] = color
        return self.set_leds(colors) and self.set_power(True)

    def get_status(self) -> dict:
        """Zustand abrufen"""
        resp = requests.get(f"http://{self.host}/json", timeout=2)
        return resp.json()

if __name__ == "__main__":
    import sys
    args = sys.argv[1:]
    host_arg = None
    if args and args[0].startswith("--host="):
        host_arg = args[0].split("=", 1)[1]
        args = args[1:]
    ctrl = WLEDController(host=host_arg)

    if not args:
        print("Usage: wled_controller.py [--host=<host>] <cmd> [args]")
        print("  test             - Regenbogen-Test")
        print("  single N R G B   - LED N (0-9) auf Farbe (R,G,B)")
        print("  clear            - Alle LEDs aus")
        print("  status           - Status anzeigen")
        sys.exit(1)

    cmd = args[0]
    if cmd == "test":
        ctrl.test_all()
        print("Regenbogen-Test gestartet")
    elif cmd == "clear":
        ctrl.clear()
        print("Alle LEDs aus")
    elif cmd == "status":
        import json
        print(json.dumps(ctrl.get_status(), indent=2))
    elif cmd == "single" and len(args) == 5:
        idx = int(args[1])
        r, g, b = int(args[2]), int(args[3]), int(args[4])
        ctrl.single_test(idx, (r, g, b))
        print(f"LED {idx} auf ({r},{g},{b}) gesetzt")
    else:
        print("Unbekannter Befehl")
        sys.exit(1)
