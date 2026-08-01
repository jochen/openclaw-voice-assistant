"""stdio-MCP-Server: gibt dem OpenClaw-Brain den Voice-Aktuator frei.

Warum es diesen Server gibt — und warum er nur zwei Tools hat — steht im
Auftrag (scratchpad/auftrag_mcp_aktuator.md) und in ACTUATOR_INTERFACE.md.
Kurzform: am 2026-08-01 hat der Brain einen verhörten Spruch ("Türrollo" ->
"Tyrolo") geraten und per rohem curl gegen Home Assistant alle dreizehn
Rollos auf 50 % gefahren, ohne Rückfrage, danach dreimal nachgefeuert. Der
Brain hatte vollen HA-Zugriff und keine Ziel-Semantik. Node-RED bietet unter
/voiceact bereits eine abgesicherte Schnittstelle (Whitelist, Wertebereiche,
Bestätigungs-Gate bei kosten:hoch). Dieser Server legt genau diese
Schnittstelle über MCP an den Brain — und sonst nichts.

Wiederverwendung statt Neubau: die gesamte Vertragstreue (Refresh, Digest,
Retry/Dedup, Envelope) steckt in voice_assistant.services.actuator.Actuator.
Dieser Server ist nur der dünnen MCP-Hülle darüber.

Werkzeuge
---------
``haus_ziele``   — read-only Digest aus /capabilities: das geschlossene
                   Vokabular. Ein Ziel, das hier nicht steht, existiert nicht.
``haus_schalten`` — POST /intent, Antwort-Envelope unverändert zurück.

Ausdrücklich KEIN Freitext-Tool ("mach was ich meine"): der ganze Gewinn ist
das geschlossene Vokabular.

Transport
---------
Reines stdio-JSON-RPC (MCP), keine eigene Auth, kein Port. Der Token wird im
Prozess aus der Datei gelesen, die das Profil nennt — wie der mail-imap-Eintrag
in ~/.openclaw/openclaw.json, nur dass hier command+args auf diesen Moduleintritt
zeigen. Gestartet wird er von OpenClaw, nicht von Hand.

Abhängigkeiten: nur Standardbibliothek. Der Actuator nutzt urllib; der MCP-
Dialog ist wenige Dutzend Zeilen JSON-RPC von Hand, damit das öffentliche Repo
kein MCP-SDK einführen muss und der laufende Voice-Dienst unberührt bleibt.

stdout-Disziplin: der Actuator printed reichlich (Tokenwarnung, refresh-Erfolg,
…). Über stdio ist stdout aber der Protokollkanal — ein stray Print würde die
JSON-RPC-Verbindung zerstören. Daher wird beim Start sys.stdout auf stderr
umgebogen; die echten Protokollantworten gehen an den ursprünglichen stdout.
"""

from __future__ import annotations

import datetime
import json
import os
import sys
import traceback
import uuid

# WICHTIG: vor jeglicher Nutzung des Actuators den echten stdout sichern und
# sys.stdout auf stderr umbiegen. Actuator (und ggfs. config-Lader) printen
# sonst in den Protokollkanal. Muss ganz oben stehen, noch bevor actuator
# importiert wird — der Modulimport von voice_assistant.config könnte schon
# printen (z.B. Profilwarnungen).
_REAL_STDOUT = sys.stdout
sys.stdout = sys.stderr


# MCP-Protokollversion. "2024-11-05" ist die kleinste Version, die OpenClaws
# MCP-Clients sicher sprechen; tools/list + tools/call sind darin stabil.
_PROTO_VERSION = "2024-11-05"


# ---------------------------------------------------------------------------
# Werkzeug-Logik — als Klasse gekapselt, damit Tests sie ohne MCP-Transport
# treiben können (gleicher Stil wie tests/test_actuator_verdict.py: Digest von
# Hand, urlopen gepatcht, kein Netz).
# ---------------------------------------------------------------------------
class GastonMCP:
    """Dünne Hülle über Actuator, die die beiden MCP-Werkzeuge anbietet.

    ``quelle`` ist hier fest "gaston" und kein Parameter — der Brain kann
    "brain" nicht einmal ausdrücken. Damit ist die Reservierungs-Regel
    (ACTUATOR_INTERFACE.md / Auftrag Punkt 1) datentechnisch garantiert, nicht
    nur prosaisch versprochen. Actuator.execute() wehrt "brain" zusätzlich ab.
    """

    QUELLE = "gaston"

    def __init__(self, actuator) -> None:
        self.act = actuator

    # -- haus_ziele ----------------------------------------------------------
    def ziele(self) -> list[dict]:
        """Digest aus /capabilities — das geschlossene Vokabular.

        Nutzt die rohen ziele, die refresh() vorhält, weil der Digest
        kosten/reversibel bewusst abwirft (die Stimme liest sie nie). Dem Brain
        fehlen genau diese Felder nicht: er soll Kosten reasonieren und wissen,
        ob ein Irrtum umkehrbar ist.
        """
        with self.act._lock:
            ziele = list(self.act.ziele or [])
        out = []
        for z in ziele:
            out.append({
                "id": z.get("id"),
                "namen": z.get("namen", []),
                "typ": z.get("typ"),
                "aktionen": z.get("aktionen", []),
                "wert": z.get("wert"),
                "kosten": z.get("kosten"),
                "reversibel": z.get("reversibel"),
                "mitglieder": z.get("mitglieder"),
            })
        return out

    # -- haus_schalten -------------------------------------------------------
    def schalten(self, ziel: str, aktion: str, wert=None, einheit=None,
                 bestaetigt: bool = False, request_id: str | None = None) -> dict:
        """POST /intent. Antwort-Envelope kommt unverändert zurück.

        ``request_id`` wird erzeugt, falls der Aufrufer keine mitgibt — sie ist
        der Idempotenz-Träger für Retries und den Handshake, also Pflicht.
        """
        rid = request_id or uuid.uuid4().hex
        intent = {
            "ist_kommando": True,
            "ziel": ziel,
            "aktion": aktion,
            "wert": wert,
            "einheit": einheit,
        }
        env = self.act.execute(intent, rid, bestaetigt=bestaetigt,
                               quelle=self.QUELLE)
        if env is None:
            # Transportfehler nach Retry — execute() liefert dann None. Wir
            # geben dem Brain einen konsistenten Umschlag zurück statt None,
            # damit er nicht raten muss, was passiert ist. Sprachliche Form
            # analog zur Graceful-Degradation im Vertrag.
            env = {
                "status": "nicht_erreichbar",
                "gesprochen": "Die Haussteuerung antwortet nicht.",
                "grund": None,
                "ausgefuehrt": None,
                "request_id": rid,
            }
        # Der Vertrag sieht vor, dass das Gate die request_id echot. Tut es das
        # einmal nicht, fehlt dem Brain genau der Träger, den der Handshake im
        # zweiten Schritt braucht. Da wir rid selbst erzeugt/gesendet haben,
        # füllen wir es hier — nur falls das Gate es weggelassen hat. Vom Gate
        # gelieferte Felder werden nicht angerührt (unverfälschte Durchleitung).
        env.setdefault("request_id", rid)
        self._log_turn(rid, ziel, aktion, wert, env)
        return env

    def _log_turn(self, rid: str, ziel: str, aktion: str, wert, env: dict) -> None:
        """Eine JSONL-Zeile an actuator_turns.log, phase "gaston".

        Ohne diesen Spiegel waren die Schaltungen des Brains unsichtbar — sie
        sind es heute, und deshalb hat den Vorfall niemand gemeldet. Format
        orientiert sich an _log_actuator_turn in assistant.py (ts zuerst,
        ensure_ascii=False). Best-effort: ein Log-Fehler darf den Aufruf nie
        scheitern lassen.
        """
        try:
            from voice_assistant.config import ACTUATOR_LOG_PATH
            meta = {
                "ts": datetime.datetime.now().isoformat(timespec="seconds"),
                "phase": "gaston",
                "request_id": rid,
                "ziel": ziel,
                "aktion": aktion,
                "wert": wert,
                "status": env.get("status"),
                "grund": env.get("grund"),
                "gesprochen": env.get("gesprochen"),
            }
            with open(ACTUATOR_LOG_PATH, "a") as f:
                f.write(json.dumps(meta, ensure_ascii=False) + "\n")
        except Exception as exc:  # Logging darf den Turn nie crashen
            print(f"⚠️  gaston-mcp log: {exc}")


# ---------------------------------------------------------------------------
# Werkzeug-Schemata für tools/list — Beschreibungen sind deutsch, weil der
# Brain deutsch spricht und die Hinweise (Handshake, "ausgefuehrt"≠Zielzustand)
# genau so beim Modell ankommen müssen.
# ---------------------------------------------------------------------------
_TOOLS = [
    {
        "name": "haus_ziele",
        "description": (
            "Liefert das geschlossene Vokabular aller steuerbaren Hausziele "
            "(Digest aus /capabilities). Je Ziel: id, namen, typ, aktionen, "
            "wert, kosten, reversibel, mitglieder. Read-only. Ein Name oder "
            "eine id, die hier nicht vorkommt, existiert NICHT — rufe niemals "
            "haus_schalten mit einem Ziel auf, das nicht in dieser Liste steht. "
            "Frage dieses Werkzeug immer zuerst, wenn du nicht sicher bist, "
            "welche ids/aktionen erlaubt sind."
        ),
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "haus_schalten",
        "description": (
            "Schaltet ein Hausziel über den abgesicherten Node-RED-Pfad "
            "(/intent). Nimmt ziel (eine id aus haus_ziele) und aktion (eine "
            "der aktionen dieses Ziels). wert und einheit nur bei aktion "
            "'setzen'.\n"
            "Handshake: Kommt status 'zurueckgestellt' zurück, ist das KEIN "
            "Fehler — das Feld gesprochen enthält eine Rückfrage. Stelle sie "
            "dem Nutzer und warte auf die Antwort. Bei einem klaren Ja rufe "
            "haus_schalten ERNEUT auf, mit derselben request_id und "
            "bestaetigt=true. Bei Nein oder Unverständlichem brich ab und "
            "schalte nicht.\n"
            "ausgefuehrt heißt angenommen und abgesetzt, nicht Zielzustand "
            "erreicht. Ein Rollo braucht rund 30 Sekunden. Position ungleich "
            "Sollwert bedeutet in Bewegung, nicht fehlgeschlagen — Befehle nie "
            "wiederholen, weil der Zielwert noch nicht dasteht.\n"
            "request_id kannst du weglassen; sie wird automatisch erzeugt. "
            "Für den zweiten Handshake-Schritt MUSS dieselbe request_id wie im "
            "ersten Schritt übergeben werden."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "ziel": {"type": "string",
                         "description": "id des Ziels aus haus_ziele."},
                "aktion": {"type": "string",
                           "description": "eine der aktionen des Ziels."},
                "wert": {"type": ["integer", "null"],
                         "description": "bei aktion 'setzen': der Sollwert."},
                "einheit": {"type": ["string", "null"],
                            "description": "bei aktion 'setzen': z.B. 'prozent'."},
                "bestaetigt": {"type": "boolean", "default": False,
                               "description": "nur beim zweiten Handshake-Schritt true."},
                "request_id": {"type": "string",
                               "description": "beim Handshake: dieselbe wie im ersten Schritt."},
            },
            "required": ["ziel", "aktion"],
        },
    },
]


# ---------------------------------------------------------------------------
# Konfiguration laden — welches Profil hält den actuator-Block?
# ---------------------------------------------------------------------------
def _load_actuator_cfg():
    """Liefert (ActuatorConfig, profilname) für das Profil mit actuator.enabled.

    Reihenfolge: GASTON_PROFILE-Env (wenn gesetzt und vorhanden) > erstes Profil
    mit actuator.enabled:true > Profildetektion per Hostname. Letzteres ist der
    Fallback, falls jemand den actuator-Block ohne enabled markiert hat.

    Öffentliches Repo: kein Profilname steht fest im Code — alles kommt aus der
    (gitignorten) config.yaml des Betreibers.
    """
    from voice_assistant.config import (ActuatorConfig, _load_yaml,
                                        _parse_profile, _detect_profile_name)
    cfg = _load_yaml()
    profiles = cfg.get("profiles", {}) or {}

    env = os.environ.get("GASTON_PROFILE", "").strip().lower()
    chosen = env if (env and env in profiles) else None

    if chosen is None:
        for name, raw in profiles.items():
            if ((raw.get("actuator") or {}).get("enabled")):
                chosen = name
                break

    if chosen is None:
        chosen = _detect_profile_name(cfg)

    raw = profiles[chosen]
    prof = _parse_profile(chosen, raw)
    return prof.actuator, chosen


def _build_server() -> GastonMCP:
    from voice_assistant.services.actuator import Actuator
    cfg, name = _load_actuator_cfg()
    if not cfg.enabled:
        print(f"⚠️  gaston-mcp: actuator im Profil '{name}' nicht enabled — "
              f"haus_ziele bleibt leer, haus_schalten versucht /intent dennoch.")
    act = Actuator(cfg)
    ok = act.refresh()
    if not ok:
        print(f"⚠️  gaston-mcp: refresh fehlgeschlagen (Profil '{name}') — "
              f"haus_ziele liefert nichts, haus_schalten probiert /intent direkt.")
    return GastonMCP(act)


# ---------------------------------------------------------------------------
# JSON-RPC über stdio
# ---------------------------------------------------------------------------
def _send(obj: dict) -> None:
    _REAL_STDOUT.write(json.dumps(obj, ensure_ascii=False) + "\n")
    _REAL_STDOUT.flush()


def _result(req_id, result: dict) -> None:
    _send({"jsonrpc": "2.0", "id": req_id, "result": result})


def _error(req_id, code: int, message: str) -> None:
    _send({"jsonrpc": "2.0", "id": req_id,
           "error": {"code": code, "message": message}})


def _call_tool(server: GastonMCP, name: str, args: dict) -> dict:
    """Führt ein Werkzeug aus und gibt das MCP-result-Objekt zurück."""
    if name == "haus_ziele":
        ziele = server.ziele()
        text = json.dumps({"ziele": ziele}, ensure_ascii=False, indent=2)
        is_error = len(ziele) == 0
        content = ([{"type": "text",
                     "text": "Aktuell keine Ziele geladen — /capabilities nicht erreichbar."}]
                   if is_error else
                   [{"type": "text", "text": text}])
        return {"content": content, "isError": is_error}

    if name == "haus_schalten":
        env = server.schalten(
            ziel=args.get("ziel"),
            aktion=args.get("aktion"),
            wert=args.get("wert"),
            einheit=args.get("einheit"),
            bestaetigt=bool(args.get("bestaetigt", False)),
            request_id=args.get("request_id"),
        )
        return {"content": [{"type": "text",
                             "text": json.dumps(env, ensure_ascii=False, indent=2)}]}

    raise ValueError(f"unbekanntes Werkzeug: {name}")


def serve() -> None:
    server = _build_server()

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue  # kaputter Frame ignorieren — nicht unsere Sache
        if not isinstance(msg, dict):
            continue

        # Benachrichtigungen tragen keine id und bekommen keine Antwort.
        if "id" not in msg:
            continue

        req_id = msg["id"]
        method = msg.get("method")

        try:
            if method == "initialize":
                _result(req_id, {
                    "protocolVersion": _PROTO_VERSION,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "gaston-aktuator", "version": "1.0"},
                })
            elif method == "ping":
                _result(req_id, {})
            elif method == "tools/list":
                _result(req_id, {"tools": _TOOLS})
            elif method == "tools/call":
                params = msg.get("params") or {}
                _result(req_id, _call_tool(server, params.get("name"),
                                           params.get("arguments") or {}))
            else:
                _error(req_id, -32601, f"Methode nicht unterstützt: {method}")
        except ValueError as e:
            _error(req_id, -32602, str(e))
        except Exception as e:  # nie den Dialog abreisen lassen
            print(f"⚠️  gaston-mcp: {method} fehlgeschlagen: {e}\n"
                  f"{traceback.format_exc()}")
            _error(req_id, -32603, f"interner Fehler: {e}")


def main() -> None:
    try:
        serve()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
