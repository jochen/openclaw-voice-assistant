"""Minimaler Client für einen MCP-Server über Streamable HTTP — nur stdlib.

Wofür: Argus (services/watcher.py) holt das Weltmodell des Hauses über den
Haus-MCP der Gegenstelle (in dieser Installation Node-RED, Werkzeug
``haus_weltmodell``). Bewusst der Code und nicht das Modell: ein Modell merkt
nicht, dass ihm Hauswissen fehlt — es erfindet dann Räume ("Mansardenzimmer",
Argus-Befund 2026-08-08). Also bekommt es das Wissen immer mit.

Nur das Nötigste des Protokolls: ``initialize`` (+ ``notifications/initialized``),
``tools/call``. Antworten kommen je nach Server als JSON oder als SSE-Strom
(``data:``-Zeilen); beides wird gelesen. Eine abgelaufene Session
(``Mcp-Session-Id`` unbekannt → 400/404) wird einmal neu aufgebaut.

Identität: jeder Client bekommt seinen eigenen Token (Jochen, 2026-10-04) —
damit kann die Gegenstelle unterscheiden, wer fragt. Argus benutzt also NICHT
den Token des Brains.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

_PROTOKOLL = "2024-11-05"


class HausMcpError(RuntimeError):
    pass


class HausMcp:
    def __init__(self, url: str, token: str = "", timeout: float = 10.0,
                 client_name: str = "argus") -> None:
        self.url = url
        self.token = token
        self.timeout = timeout
        self.client_name = client_name
        self._session: str | None = None
        self._id = 0
        self._lock = threading.Lock()

    # --- Transport -------------------------------------------------------
    def _post(self, payload: dict) -> dict | None:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if self._session:
            headers["Mcp-Session-Id"] = self._session
        req = urllib.request.Request(
            self.url, data=json.dumps(payload).encode("utf-8"),
            headers=headers, method="POST",
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            sid = r.headers.get("Mcp-Session-Id")
            if sid:
                self._session = sid
            body = r.read().decode("utf-8")
        if "id" not in payload:          # Notification: keine Antwort erwartet
            return None
        return _antwort_lesen(body, payload["id"])

    def _rpc(self, method: str, params: dict) -> dict:
        self._id += 1
        msg = self._post({"jsonrpc": "2.0", "id": self._id,
                          "method": method, "params": params})
        if msg is None:
            raise HausMcpError(f"{method}: keine Antwort")
        if "error" in msg:
            raise HausMcpError(f"{method}: {msg['error'].get('message', msg['error'])}")
        return msg.get("result") or {}

    def _verbinden(self) -> None:
        self._session = None
        self._rpc("initialize", {
            "protocolVersion": _PROTOKOLL,
            "capabilities": {},
            "clientInfo": {"name": self.client_name, "version": "1"},
        })
        try:
            self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        except (urllib.error.URLError, OSError):
            pass  # manche Server antworten auf Notifications mit 202/leer

    # --- API -------------------------------------------------------------
    def tool(self, name: str, arguments: dict | None = None) -> str:
        """Ruft ein Werkzeug und gibt dessen Text-Inhalt zurück."""
        with self._lock:
            for versuch in (1, 2):
                try:
                    if self._session is None:
                        self._verbinden()
                    res = self._rpc("tools/call", {"name": name,
                                                   "arguments": arguments or {}})
                    break
                except urllib.error.HTTPError as e:
                    # Session beim Server abgelaufen → einmal neu verbinden.
                    if versuch == 1 and e.code in (400, 404) and self._session:
                        self._session = None
                        continue
                    raise HausMcpError(f"{name}: HTTP {e.code}") from e
                except (urllib.error.URLError, OSError) as e:
                    raise HausMcpError(f"{name}: {e}") from e
        text = "".join(c.get("text", "") for c in res.get("content") or []
                       if c.get("type") == "text")
        if res.get("isError"):
            raise HausMcpError(f"{name}: {text[:200]}")
        return text


def _antwort_lesen(body: str, rpc_id: int) -> dict | None:
    """JSON-RPC-Antwort aus einem JSON- oder SSE-Körper holen."""
    body = body.strip()
    if not body:
        return None
    if body.startswith("{"):
        return json.loads(body)
    treffer = None
    for zeile in body.splitlines():
        if zeile.startswith("data:"):
            try:
                msg = json.loads(zeile[5:].strip())
            except json.JSONDecodeError:
                continue
            if msg.get("id") == rpc_id:
                treffer = msg
    return treffer


class Weltmodell:
    """Hält das Weltmodell aktuell: vor jeder Nutzung nur die Version prüfen
    (``nur_version``, ~20 Byte), bei Änderung das ganze Modell neu laden.

    ``aktuell()`` liefert ``(version, text, fehler)``. Ist der Server nicht
    erreichbar, bleibt das zuletzt geladene Modell in Gebrauch und ``fehler``
    sagt warum — Argus soll das im Befund vermerken, nicht verschweigen.
    Gab es noch nie ein Modell, ist ``text`` leer.
    """

    WERKZEUG = "haus_weltmodell"

    def __init__(self, mcp: HausMcp) -> None:
        self.mcp = mcp
        self.version: str | None = None
        self.text: str = ""

    def aktuell(self) -> tuple[str | None, str, str | None]:
        try:
            v = json.loads(self.mcp.tool(self.WERKZEUG, {"nur_version": True}))["version"]
            if v != self.version or not self.text:
                text = self.mcp.tool(self.WERKZEUG, {})
                self.version = str(json.loads(text).get("version", v))
                self.text = text
            return self.version, self.text, None
        except (HausMcpError, ValueError, KeyError, TypeError) as e:
            return self.version, self.text, str(e)
