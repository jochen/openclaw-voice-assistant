"""Überwacher-Worker: periodisch Aktuator-Turns prüfen und nach Telegram melden.

STUFE 1 (nur LESEN + MELDEN). Dieser Worker läuft als Daemon-Thread im
Voice-Assistant-Prozess und:

  1. liest actuator_turns.log (derselbe Spiegel-Kanal den der Aktuator schreibt)
  2. prüft jeden neuen Turn:
     - SEMANTISCHE PRÜFUNG (LLM): passt das Transkript zum Intent? Ein
       stärkeres Modell als der Aktuator sieht subtilere Muster — Verneinungen,
       Einschränkungen, „auf 10%" als Präposition vs. Aktion. Ersetzt die
       frühere Regex-Heuristik (die Fehlalarme produzierte bei „alle rollos
       auf 10%"). Siehe DESIGN-DELIBERATION 2026-07-24 im MemPalace:
       „schneller Aktuator + langsamer Aufseher".
     - STRUKTURELLE PRÜFUNGEN (deterministisch): EXEC_DIFFERS (Node-RED hat
       etwas anderes ausgeführt als der Intent) und STATUS_PROBLEM (nicht
       „ausgefuehrt"). Diese sind Feldvergleiche — kein LLM nötig.
  3. schickt Befunde in einen separaten Telegram-Chat („Argus"), NICHT in
     den Family-Voice-Chat.
  4. respektiert Stille Stunden (default 01:00–07:00).

Bei LLM-Fehler (Provider down, Timeout, leerer Response): Meldung an Argus
„Überwachung konnte nicht erfolgen weil …" — der User sieht dass etwas nicht
stimmt, statt im Dunkeln zu stehen.

Warum im Voice-Assistant und nicht in OpenClaw: OpenClaw zieht workspace,
AGENTS.md, Skills, Session-Kontext hoch — für eine Ja/Nein-Frage die ein
200-Token-Systemprompt beantwortet. Der Aktuator macht es vor: kurzer
API-Call, JSON zurück, kein Kontext. Derselbe Pattern hier.
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime

from voice_assistant.config import ACTUATOR_LOG_PATH, WORKSPACE
from voice_assistant.services.telegram import send as tg_send

# Dedup + strukturelle Prüffunktionen aus dem Tool importieren
from tools.actuator_watch import (
    _lade_log,
    _lade_gesehen,
    _pruefe_status_problem,
    _pruefe_exec_differs,
)

WATCH_PATH = os.path.join(WORKSPACE, "actuator_watch.jsonl")

# Nur diese Befund-Arten gehen nach Telegram. STATUS_PROBLEM bleibt archiviert
# aber still (siehe Modul-Doku).
_ALERT_ARTEN = {"LLM_MISMATCH", "EXEC_DIFFERS"}

# System-Prompt für die semantische Prüfung. Kurz, geschlossen, deutsch.
# Bewusst kein Kontext, keine Skills, keine Tools — nur Ja/Nein + Grund.
_LLM_SYSTEM_PROMPT = """Du bist der Aufseher eines Sprach-Aktuators. Du bekommst das gesprochene Transkript und den Intent den der Aktuator daraus gebildet hat. Prüfe OB SIE ZUSAMMENPASSEN.

Häufige Fehler die du erkennst:
- Transkript sagt "aus", Intent sagt "ein" (oder umgekehrt)
- Transkript nennt ein anderes Ziel als der Intent ("Küchenlicht" gesagt, "Wohnzimmerlicht" klassifiziert)
- Verneinung oder Einschränkung im Transkript ignoriert ("alle außer der Küche", "nicht das Badlicht")
- "auf 10%" ist eine SETZEN-Aktion (Präposition), nicht die Aktion "auf" (ganz öffnen) — das ist KEIN Fehler

Antworte NUR als JSON:
{"ok": true}                          — Transkript und Intent passen zusammen
{"ok": false, "grund": "..."}         — sie passen nicht; grund in einem kurzen Satz

Wenn du unsicher bist, antworte ok:true (lieber nichts melden als falsch alarmieren)."""


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


def _llm_pruefe(turn: dict, llm_url: str, llm_model: str, api_key: str,
                timeout: float) -> dict | None:
    """Semantische Prüfung via LLM. Gibt einen Befund-Dict zurück oder None
    wenn alles ok. Bei Fehler wird ein spezieller Befund mit art=LLM_ERROR
    zurückgegeben, damit der Worker ihn als Fehlermeldung behandeln kann."""
    transcript = turn.get("transcript", "")
    intent = turn.get("intent") or {}
    if not transcript or not intent.get("ist_kommando"):
        return None

    user_msg = (
        f'Transkript: "{transcript}"\n'
        f'Intent: {json.dumps(intent, ensure_ascii=False)}'
    )
    body = json.dumps({
        "model": llm_model,
        "messages": [
            {"role": "system", "content": _LLM_SYSTEM_PROMPT},
            {"role": "user", "content": user_msg},
        ],
        "temperature": 0,
        "max_tokens": 100,
    }).encode("utf-8")

    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    req = urllib.request.Request(llm_url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            out = json.loads(r.read().decode())
        content = out["choices"][0]["message"]["content"].strip()
        # Parse das JSON aus der Antwort
        result = json.loads(content)
        if result.get("ok"):
            return None
        return {
            "art": "LLM_MISMATCH",
            "detail": result.get("grund", "Transkript und Intent passen nicht zusammen."),
        }
    except Exception as e:
        return {
            "art": "LLM_ERROR",
            "detail": f"LLM-Prüfung fehlgeschlagen: {e}",
        }


def _formatiere_alert(befund: dict) -> str:
    """Telegram-Nachricht für einen Befund."""
    art = befund.get("art", "?")
    ts = befund.get("ts") or "?"
    transcript = befund.get("transcript") or "?"
    detail = befund.get("detail") or ""
    intent = befund.get("intent") or {}
    ausg = befund.get("ausgefuehrt") or {}

    emoji = {"LLM_MISMATCH": "🔍", "EXEC_DIFFERS": "⚠️", "LLM_ERROR": "❌"}.get(art, "📋")
    lines = [
        f"{emoji} {art}",
        f"Zeit: {ts}",
        f'Gesagt: "{transcript}"',
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
        poll_interval: int = 300,
        quiet_start: int = 1,
        quiet_end: int = 7,
        llm_url: str = "",
        llm_model: str = "",
        llm_api_key: str = "",
        llm_timeout: float = 10.0,
    ) -> None:
        self.chat_id = chat_id
        self.bot_token = bot_token
        self.poll_interval = max(60, poll_interval)
        self.quiet_start = quiet_start
        self.quiet_end = quiet_end
        self.llm_url = llm_url
        self.llm_model = llm_model
        self.llm_api_key = llm_api_key
        self.llm_timeout = llm_timeout
        self._stop = threading.Event()

    def start(self) -> None:
        t = threading.Thread(target=self._loop, daemon=True, name="actuator-watcher")
        t.start()
        llm_info = ""
        if self.llm_url and self.llm_model:
            llm_info = f", Modell {self.llm_model}"
        print(f"👁️  Überwacher: prüft alle {self.poll_interval}s{llm_info}, "
              f"meldet an Chat {self.chat_id}, "
              f"still {self.quiet_start:02d}–{self.quiet_end:02d} Uhr")

    def _loop(self) -> None:
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
        llm_errors = []

        for turn in turns:
            rid = turn.get("request_id", "")
            if rid in gesehen:
                continue

            # 1. Semantische Prüfung via LLM (ersetzt die alte Regex-Heuristik)
            if self.llm_url and self.llm_model:
                llm_befund = _llm_pruefe(
                    turn, self.llm_url, self.llm_model,
                    self.llm_api_key, self.llm_timeout,
                )
                if llm_befund is not None:
                    if llm_befund.get("art") == "LLM_ERROR":
                        llm_errors.append((rid, llm_befund))
                    else:
                        eintrag = self._make_eintrag(turn, rid, llm_befund)
                        neue_befunde.append(eintrag)

            # 2. Strukturelle Prüfungen (deterministisch, kein LLM)
            for pruefer in (_pruefe_exec_differs,):
                b = pruefer(turn)
                if b is None:
                    continue
                eintrag = self._make_eintrag(turn, rid, b)
                neue_befunde.append(eintrag)

        # LLM-Fehler separat behandeln: Meldung an Argus
        if llm_errors:
            for rid, err in llm_errors:
                msg = (f"❌ Überwachung konnte nicht erfolgen weil:\n"
                       f"{err['detail']}\n"
                       f"(Turn {rid})")
                if not _in_stillen_stunden(datetime.now(),
                                           self.quiet_start, self.quiet_end):
                    tg_send(self.bot_token, self.chat_id, msg)

        if not neue_befunde:
            return

        # Persistiere alle Befunde
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
            time.sleep(0.5)

    def _make_eintrag(self, turn: dict, rid: str, befund: dict) -> dict:
        return {
            "ts": turn.get("ts"),
            "request_id": rid,
            "transcript": turn.get("transcript"),
            "speaker": turn.get("speaker"),
            "wakeword": turn.get("wakeword"),
            "intent": turn.get("intent"),
            "status": turn.get("status"),
            "ausgefuehrt": turn.get("ausgefuehrt"),
            "gesprochen": turn.get("gesprochen"),
            **befund,
        }
