"""Überwacher-Worker: periodisch Aktuator-Turns prüfen und nach Telegram melden.

STUFE 1 (nur LESEN + MELDEN, siehe tools/actuator_watch.py für die Begründung).
Dieser Worker läuft als Daemon-Thread im Voice-Assistant-Prozess und:

  1. liest actuator_turns.log (derselbe Spiegel-Kanal den der Aktuator schreibt)
  2. prüft jeden neuen Turn mit den Prüffunktionen aus tools.actuator_watch
  3. schickt Befunde (AKTIONS_MISMATCH, EXEC_DIFFERS) in einen separaten
     Telegram-Chat — NICHT in den Family-Voice-Chat, NICHT in die Haus-Session.
  4. respektiert Stille Stunden (default 01:00–07:00): in dieser Zeit wird
     nichts gesendet, Befunde werden gesammelt und danach gemeldet.

STATUS_PROBLEM wird bewusst NICHT nach Telegram geschickt: „abgelehnt" ist
oft nur eine Kosten-Rückfrage, „zurueckgestellt" der normale Handshake —
beides häufig und banal. Es bleibt in actuator_watch.jsonl fürs Archiv.

Warum im Voice-Assistant und nicht als eigenständiger Dienst: der Voice-
Assistant läuft ohnehin, kennt den Telegram-Bot, hat die config. Ein extra
Prozess wäre Overkill für etwas das alle paar Minuten eine Datei liest.
Die Prüflopgik ist in tools/actuator_watch.py — dieser Worker ist dünn:
rufen, filtern, senden.
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.request
from datetime import datetime

from voice_assistant.config import ACTUATOR_LOG_PATH, WORKSPACE
from voice_assistant.services.telegram import send as tg_send

# Prüffunktionen und Dedup aus dem Tool importieren
from tools.actuator_watch import (
    _lade_log,
    _lade_gesehen,
    _pruefe_aktions_mismatch,
    _pruefe_status_problem,
    _pruefe_exec_differs,
)

WATCH_PATH = os.path.join(WORKSPACE, "actuator_watch.jsonl")

# Nur diese Befund-Arten gehen nach Telegram. STATUS_PROBLEM bleibt archiviert
# aber still (siehe Modul-Doku).
_ALERT_ARTEN = {"AKTIONS_MISMATCH", "EXEC_DIFFERS"}


def _in_stillen_stunden(now: datetime, quiet_start: int, quiet_end: int) -> bool:
    """True wenn die aktuelle Stunde in den Stille-Zeit liegt.
    quiet_start/quiet_end als volle Stunden (0–23), z.B. 1 und 7.
    Unterstützt Bereich über Mitternacht (start > end), z.B. 22–7.
    """
    h = now.hour
    if quiet_start <= quiet_end:
        return quiet_start <= h < quiet_end
    # Bereich über Mitternacht, z.B. 22–7
    return h >= quiet_start or h < quiet_end


def _formatiere_alert(befund: dict) -> str:
    """Telegram-Nachricht für einen Befund."""
    art = befund.get("art", "?")
    ts = befund.get("ts") or "?"
    transcript = befund.get("transcript") or "?"
    detail = befund.get("detail") or ""
    intent = befund.get("intent") or {}
    ausg = befund.get("ausgefuehrt") or {}

    emoji = {"AKTIONS_MISMATCH": "🔍", "EXEC_DIFFERS": "⚠️"}.get(art, "📋")
    lines = [
        f"{emoji} {art}",
        f"Zeit: {ts}",
        f"Gesagt: \"{transcript}\"",
        f"Befund: {detail}",
    ]
    if intent:
        lines.append(f"Intent: ziel='{intent.get('ziel')}' aktion='{intent.get('aktion')}'")
    if ausg:
        lines.append(f"Ausgeführt: ziel='{ausg.get('ziel')}' aktion='{ausg.get('aktion')}'")
    gesprochen = befund.get("gesprochen")
    if gesprochen:
        lines.append(f"Gesprochen: {gesprochen}")
    lines.append("(Stufe 1 — nur Meldung, kein Eingriff)")
    return "\n".join(lines)


class Watcher:
    """Daemon-Thread: prüft periodisch actuator_turns.log auf Diskrepanzen."""

    def __init__(
        self,
        chat_id: str,
        bot_token: str,
        poll_interval: int = 300,  # 5 Minuten
        quiet_start: int = 1,
        quiet_end: int = 7,
    ) -> None:
        self.chat_id = chat_id
        self.bot_token = bot_token
        self.poll_interval = max(60, poll_interval)
        self.quiet_start = quiet_start
        self.quiet_end = quiet_end
        self._stop = threading.Event()

    def start(self) -> None:
        t = threading.Thread(target=self._loop, daemon=True, name="actuator-watcher")
        t.start()
        print(f"👁️  Überwacher: prüft alle {self.poll_interval}s, "
              f"meldet an Chat {self.chat_id}, "
              f"still {self.quiet_start:02d}–{self.quiet_end:02d} Uhr")

    def _loop(self) -> None:
        # Erster Check nach kurzem Delay (nicht beim Start feuern, wenn der
        # Voice-Assistant gerade andere Startup-Logs schreibt).
        while not self._stop.wait(self.poll_interval):
            try:
                self._check_once()
            except Exception as e:
                print(f"⚠️  Überwacher: Check-Fehler: {e}")

    def _check_once(self) -> None:
        """Ein Prüfdurchgang: Log lesen, neue Turns prüfen, melden."""
        turns = _lade_log(None)
        if not turns:
            return

        gesehen = _lade_gesehen()
        neue_befunde = []

        for turn in turns:
            rid = turn.get("request_id", "")
            if rid in gesehen:
                continue
            for pruefer in (_pruefe_aktions_mismatch, _pruefe_exec_differs):
                b = pruefer(turn)
                if b is None:
                    continue
                eintrag = {
                    "ts": turn.get("ts"),
                    "request_id": rid,
                    "transcript": turn.get("transcript"),
                    "speaker": turn.get("speaker"),
                    "wakeword": turn.get("wakeword"),
                    "intent": turn.get("intent"),
                    "status": turn.get("status"),
                    "ausgefuehrt": turn.get("ausgefuehrt"),
                    "gesprochen": turn.get("gesprochen"),
                    **b,
                }
                neue_befunde.append(eintrag)

        if not neue_befunde:
            return

        # Persistiere ALLE Befunde (inkl. STATUS_PROBLEM-falls welche dazukommen,
        # obwohl wir hier nur die zwei Alert-Arten prüfen — sicherheitshalber)
        with open(WATCH_PATH, "a") as f:
            for b in neue_befunde:
                f.write(json.dumps(b, ensure_ascii=False) + "\n")

        # Nach Telegram schicken, aber nur Alert-Arten und nur außerhalb
        # der Stille-Zeit.
        jetzt = datetime.now()
        still = _in_stillen_stunden(jetzt, self.quiet_start, self.quiet_end)

        alerts = [b for b in neue_befunde if b.get("art") in _ALERT_ARTEN]

        if not alerts:
            return

        if still:
            print(f"👁️  Überwacher: {len(alerts)} Befund(e) gesammelt — "
                  f"Stille Zeit bis {self.quiet_end:02d}:00, nicht gesendet")
            return

        for b in alerts:
            msg = _formatiere_alert(b)
            tg_send(self.bot_token, self.chat_id, msg)
            # Kurz Pause zwischen Messages, falls mehrere auf einmal kommen
            time.sleep(0.5)
