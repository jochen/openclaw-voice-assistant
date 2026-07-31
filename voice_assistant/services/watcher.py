"""Überwacher: semantische Prüfung jedes Aktuator-Turns via LLM (nur LESEN + MELDEN).

STUFE 1 (nur LESEN + MELDEN). Wird SOFORT nach jedem Aktuator-Turn aufgerufen
(in einem Daemon-Thread, blockiert die Hauptschleife nicht).

Architektur (siehe DESIGN-DELIBERATION 2026-07-24 im MemPalace):
  „schneller Aktuator + langsamer Aufseher" — Fan-out nach der Aufnahme:
  (a) kleines Modell → Intent → handelt; (b) STT → grosses Modell → prueft.

Der Aufseher bekommt Transkript + Intent und fragt ein stärkeres LLM: passen
sie zusammen? Ersetzt die frühere Regex-Heuristik (die Fehlalarme produzierte
bei „alle rollos auf 10%"). Das LLM erkennt subtilere Muster — Verneinungen,
Einschränkungen, Präposition vs. Aktion.

Zusätzlich deterministische Checks (kein LLM nötig):
  - EXEC_DIFFERS: Node-RED hat etwas anderes ausgeführt als der Intent
  - STATUS_PROBLEM: nicht „ausgefuehrt" (archiviert, nicht nach Telegram)

Bei LLM-Fehler (Provider down, Timeout): Meldung an Argus-Chat
„Überwachung konnte nicht erfolgen weil …".

Warum im Voice-Assistant und nicht in OpenClaw: OpenClaw zieht workspace,
AGENTS.md, Skills, Session-Kontext hoch — für eine Ja/Nein-Frage die ein
200-Token-Systemprompt beantwortet. Der Aktuator macht es vor: kurzer
API-Call, JSON zurück, kein Kontext. Derselbe Pattern hier.

TIMING: der check_turn() Aufruf passiert nachdem actuator.execute()
zurückkam und das Log geschrieben wurde — der Turn ist also vollständig
(status, ausgefuehrt, gesprochen alle gefüllt). Der Aufsefer sieht niemals
einen halbfertigen Turn.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime

from voice_assistant.config import WORKSPACE
from voice_assistant.services.telegram import send as tg_send

# Strukturelle Prüffunktionen aus dem Tool importieren
from tools.actuator_watch import _pruefe_exec_differs

WATCH_PATH = os.path.join(WORKSPACE, "actuator_watch.jsonl")

# Nur diese Befund-Arten gehen nach Telegram. STATUS_PROBLEM bleibt archiviert
# aber still (siehe Modul-Doku).
_ALERT_ARTEN = {"LLM_MISMATCH", "EXEC_DIFFERS"}

# System-Prompt für die semantische Prüfung. Kurz, geschlossen, deutsch.
_LLM_SYSTEM_PROMPT = """Du bist der Aufseher eines Sprach-Aktuators. Du bekommst das gesprochene Transkript und den Intent den der Aktuator daraus gebildet hat. Prüfe OB SIE ZUSAMMENPASSEN.

Häufige Fehler die du erkennst:
- Transkript sagt "aus", Intent sagt "ein" (oder umgekehrt)
- Transkript nennt ein anderes Ziel als der Intent ("Küchenlicht" gesagt, "Wohnzimmerlicht" klassifiziert)
- Verneinung oder Einschränkung im Transkript ignoriert ("alle außer der Küche", "nicht das Badlicht")
- "auf 10%" ist eine SETZEN-Aktion (Präposition), nicht die Aktion "auf" (ganz öffnen) — das ist KEIN Fehler

Du bist STUFE 1: du greifst NIEMALS ein, du schaltest nichts. Du meldest nur.
Aber du bist ein denkender Aufseher, kein Matcher. Zeige deinen Gedankengang
und überlege, was DU tun würdest, um einen erkannten Fehler zu beheben — als
VORSCHLAG, nicht als Befehl. Genauer gesagt: wenn der Aktuator etwas Falsches
ausgeführt hat, beschreibe knapp, wie man es rückgängig machen und das
gemeinte stattdessen tun würde (z.B. "Rollo wieder schließen, dann ganz
öffnen"). Wenn nichts Falsches ausgeführt wurde oder es unklar ist, schreibe
"keine". Dieser Vorschlag wird NUR angezeigt, nie ausgeführt — er dient dem
Menschen zur Einschätzung, ob deine Korrektur-Ideen vernünftig sind.

Antworte NUR als JSON:
{"ok": true}
{"ok": false, "grund": "...", "gedanke": "...", "korrektur": "..."}

  grund      — kurzer Satz: was passt nicht zusammen
  gedanke    — 1 bis 3 Sätze: wie kommst du zu diesem Schluss? Was im
               Transkript hat dich überzeugt, was im Intent widerspricht dem?
               Das ist dein Gedankeneinblick, damit ein Mensch nachvollziehen
               kann, ob dein Urteil schlüssig ist.
  korrektur  — was DU vorschlagen würdest zu tun, um den Fehler zu beheben
               (rückgängig + gemeintes tun), als freier Text; oder "keine"
               wenn nichts Falsches ausgeführt wurde oder unklar ist

Wenn du unsicher bist, antworte ok:true (lieber nichts melden als falsch alarmieren)."""


def _in_stillen_stunden(now: datetime, quiet_start: int, quiet_end: int) -> bool:
    """True wenn die aktuelle Stunde in den Stille-Zeit liegt."""
    h = now.hour
    if quiet_start <= quiet_end:
        return quiet_start <= h < quiet_end
    return h >= quiet_start or h < quiet_end


def _llm_pruefe(turn: dict, llm_url: str, llm_model: str, api_key: str,
                timeout: float) -> dict | None:
    """Semantische Prüfung via LLM. Gibt einen Befund-Dict zurück oder None
    wenn alles ok. Bei Fehler wird ein Befund mit art=LLM_ERROR zurückgegeben.

    Retry: bei Timeout oder Netzwerkfehler EIN Retry mit vollem Timeout.
    Der Overseer-Thread blockiert nichts — lieber spät melden als gar nicht.
    """
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
        "max_tokens": 500,
        "chat_template_kwargs": {"enable_thinking": False},
    }).encode("utf-8")

    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    def _attempt() -> dict | None:
        """Ein LLM-Versuch. Wirft bei Timeout/Netzwerkfehler."""
        req = urllib.request.Request(llm_url, data=body, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            out = json.loads(r.read().decode())
        msg = out["choices"][0]["message"]
        content = (msg.get("content") or "").strip()
        if not content:
            content = (msg.get("reasoning") or "").strip()
        if not content:
            return {"art": "LLM_ERROR", "detail": "LLM antwortete ohne content (leer)."}
        json_match = re.search(r'\{[^{}]*"ok"[^{}]*\}', content)
        if json_match:
            result = json.loads(json_match.group())
        else:
            result = json.loads(content)
        if result.get("ok"):
            return None
        return {
            "art": "LLM_MISMATCH",
            "detail": result.get("grund", "Transkript und Intent passen nicht zusammen."),
            "gedanke": (result.get("gedanke") or "").strip(),
            "korrektur": (result.get("korrektur") or "").strip(),
        }

    try:
        return _attempt()
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        # Retry bei Timeout/Netzwerk — der Provider kann temporär langsam sein.
        # Kurze Pause, dann voller Timeout nochmal.
        print(f"⚠️  Überwacher: LLM-Timeout/Netzwerk ({e}), Retry in 2s …")
        time.sleep(2)
        try:
            return _attempt()
        except Exception as e2:
            return {"art": "LLM_ERROR", "detail": f"LLM-Prüfung nach Retry fehlgeschlagen: {e2}"}
    except Exception as e:
        return {"art": "LLM_ERROR", "detail": f"LLM-Prüfung fehlgeschlagen: {e}"}


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
    gedanke = befund.get("gedanke")
    if gedanke:
        lines.append(f"Gedanke: {gedanke}")
    if intent:
        lines.append(f"Intent: ziel='{intent.get('ziel')}' aktion='{intent.get('aktion')}'")
    if ausg:
        lines.append(f"Ausgeführt: ziel='{ausg.get('ziel')}' aktion='{ausg.get('aktion')}'")
    gesprochen = befund.get("gesprochen")
    if gesprochen:
        lines.append(f"Gesprochen: {gesprochen}")
    korrektur = befund.get("korrektur")
    if korrektur and korrektur.lower() != "keine":
        lines.append(f"Korrektur-Vorschlag: {korrektur}")
    lines.append("(Stufe 1 — nur Meldung, kein Eingriff)")
    return "\n".join(lines)


class Overseer:
    """Event-gesteuerter Überwacher: check_turn() nach jedem Aktuator-Turn.

    Kein periodischer Poller mehr — die Prüfung passiert sofort nach dem Turn,
    in einem Daemon-Thread der nicht blockiert. Siehe DESIGN-DELIBERATION
    2026-07-24: „Fan-out nach der Aufnahme: (a) handelt; (b) prueft".
    """

    def __init__(
        self,
        chat_id: str,
        bot_token: str,
        quiet_start: int = 1,
        quiet_end: int = 7,
        llm_url: str = "",
        llm_model: str = "",
        llm_api_key: str = "",
        llm_timeout: float = 10.0,
    ) -> None:
        self.chat_id = chat_id
        self.bot_token = bot_token
        self.quiet_start = quiet_start
        self.quiet_end = quiet_end
        self.llm_url = llm_url
        self.llm_model = llm_model
        self.llm_api_key = llm_api_key
        self.llm_timeout = llm_timeout

    def check_turn(self, turn: dict) -> None:
        """Prüft einen Aktuator-Turn sofort, in einem Daemon-Thread.
        Blockiert die Hauptschleife nicht."""
        t = threading.Thread(
            target=self._check_turn_sync,
            args=(turn,),
            daemon=True,
            name="overseer-check",
        )
        t.start()

    def _check_turn_sync(self, turn: dict) -> None:
        """Die eigentliche Prüfung — läuft im Thread."""
        befunde = []
        llm_error = None

        # 1. Semantische Prüfung via LLM
        if self.llm_url and self.llm_model:
            llm_befund = _llm_pruefe(
                turn, self.llm_url, self.llm_model,
                self.llm_api_key, self.llm_timeout,
            )
            if llm_befund is not None:
                if llm_befund.get("art") == "LLM_ERROR":
                    llm_error = llm_befund
                else:
                    befunde.append({**turn, **llm_befund})

        # 2. Strukturelle Prüfung (deterministisch)
        exec_befund = _pruefe_exec_differs(turn)
        if exec_befund is not None:
            befunde.append({**turn, **exec_befund})

        # LLM-Fehler: melden, nicht archivieren
        if llm_error:
            self._send_telegram(
                f"❌ Überwachung konnte nicht erfolgen weil:\n"
                f"{llm_error['detail']}\n"
                f"(Turn: {(turn.get('transcript') or '?')[:60]})"
            )

        if not befunde:
            return

        # Persistiere alle Befunde
        try:
            with open(WATCH_PATH, "a") as f:
                for b in befunde:
                    f.write(json.dumps(b, ensure_ascii=False) + "\n")
        except Exception as e:
            print(f"⚠️  Überwacher: JSONL-Schreiben fehlgeschlagen: {e}")

        # Nach Telegram schicken (nur Alert-Arten, nur außerhalb Stille-Zeit)
        alerts = [b for b in befunde if b.get("art") in _ALERT_ARTEN]
        if not alerts:
            return

        if _in_stillen_stunden(datetime.now(), self.quiet_start, self.quiet_end):
            print(f"👁️  Überwacher: {len(alerts)} Befund(e) gesammelt — Stille Zeit")
            return

        for b in alerts:
            msg = _formatiere_alert(b)
            self._send_telegram(msg)
            time.sleep(0.5)

    def _send_telegram(self, msg: str) -> None:
        """Sendet eine Telegram-Nachricht an den Argus-Chat."""
        try:
            tg_send(self.bot_token, self.chat_id, msg)
        except Exception as e:
            print(f"⚠️  Überwacher: Telegram-Senden fehlgeschlagen: {e}")
