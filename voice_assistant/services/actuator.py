"""Voice-Aktuator: schneller lokaler Schalt-Pfad für Haussteuerung.

Fängt Schaltkommandos ("Mach das Küchenlicht an") direkt nach der STT ab,
bevor der langsame Remote-Brain (OpenClaw) bemüht wird. Pfad:

    STT-Text -> Intent-Klassifikation (kleines LLM, Schema+Prompt aus
    GET /capabilities) -> POST /intent an Node-RED -> {status, gesprochen}

Node-RED (noderedpi4) bleibt die einzige inhaltliche Validierungs- und
Ausführungsinstanz; dieses Modul erzwingt nur die geschlossene FORM des
Intents (response_format json_schema) und macht zusätzlich eine
client-seitige Sanity-Prüfung gegen den Digest (is_actionable), bevor
überhaupt gepostet wird.

Schema-Generator (refresh) und Prompt-Bauweise (System-Prompt) sind 1:1 aus
dem validierten Prototyp übernommen — siehe
actuator_prototype/capabilities_to_schema.py und
actuator_prototype/test_grammar.py sowie ACTUATOR_V1_PLAN.md für die
Geschichte/Entscheidungen dahinter. Die Prompt-Formulierung ist empirisch
validiert (5/5 auf 62 Ziele) und wird hier bewusst NICHT umformuliert.

Jeder Netzwerk-/Parse-Fehler wird gefangen und geloggt — der Assistant muss
ohne Aktuator (bzw. mit ihm im "kein Kommando"-Zustand) genauso weiterlaufen
wie vor diesem Umbau (graceful degradation, z.B. wenn der Gemma-Container aus
ist).
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request

from voice_assistant.config import ActuatorConfig


def _build_system_prompt(ziel_liste: str) -> str:
    """Baut den System-Prompt nach der SYS-Vorlage aus
    actuator_prototype/test_grammar.py (Verb-Regeln + Few-Shot-Beispiele +
    generierte Ziel-Liste). Formulierung nur gegen Messungen ändern.

    Abweichung vom Prototyp (2026-07-25): Einzahl/Mehrzahl-Regel + ein
    Few-Shot-Paar Einzelrollo/Raumgruppe. Anlass: "Mach alle Lichter in der
    Küche an" schaltete stillschweigend nur das kuechenlicht — bei einem
    geschlossenen Enum kann das Modell "mehrere" gar nicht ausdrücken und
    schnappt aufs nächste Einzelziel. Das Beispielpaar nutzt bewusst
    kuechenrollo_links/kuechenrollos: existierende ids, die denselben
    Einzel-gegen-Raumgruppe-Fall zeigen. Ein Beispiel mit einer noch nicht
    angelegten Licht-Gruppe würde eine id lehren, die es nicht gibt.
    """
    return f"""Du bist der lokale Schalt-Aktuator. Wandle den gesprochenen Satz in EIN JSON-Intent. Gib NUR das JSON aus.
aktion: ein/aus (Licht,Schalter), auf/zu (Rollo ganz oeffnen/schliessen; "hoch"=auf,"runter"=zu), setzen (Zahlenwert), aktivieren (Szene), starten (Routine).
wert(Zahl)+einheit nur bei setzen (prozent Rollo, grad Heizung), sonst null. Kein Steuerkommando -> ist_kommando=false, ziel="", rest null.
Waehle das passende ziel aus der Liste (id links). Aliase stehen rechts.
EINZAHL vs MEHRZAHL: "das <Geraet>" meint EIN einzelnes Ziel. "die"/"alle <Geraete> in <Raum>" meint das Sammel-Ziel fuer diesen Raum, falls die Liste eines fuehrt.

Beispiele:
Schalte das Flurlicht ein -> {{"ist_kommando":true,"aktion":"ein","ziel":"flurlicht","wert":null,"einheit":null}}
Stell die Felixheizung auf 22 Grad -> {{"ist_kommando":true,"aktion":"setzen","ziel":"felixheizung","wert":22,"einheit":"grad"}}
Mach das Kuechenrollo links zu -> {{"ist_kommando":true,"aktion":"zu","ziel":"kuechenrollo_links","wert":null,"einheit":null}}
Mach alle Rollos in der Kueche zu -> {{"ist_kommando":true,"aktion":"zu","ziel":"kuechenrollos","wert":null,"einheit":null}}
Mach alle Rollos zu -> {{"ist_kommando":true,"aktion":"zu","ziel":"alle_rollos","wert":null,"einheit":null}}
Erzaehl mir einen Witz -> {{"ist_kommando":false,"aktion":null,"ziel":"","wert":null,"einheit":null}}

Bekannte Ziele:
{ziel_liste}"""


class Actuator:
    def __init__(self, cfg: ActuatorConfig) -> None:
        self.cfg = cfg
        try:
            with open(cfg.token_file) as f:
                self._token = f.read().strip()
        except Exception as e:
            print(f"⚠️  Aktuator: Token-Datei nicht lesbar ({cfg.token_file}): {e}")
            self._token = ""

        self._lock = threading.Lock()
        # In-memory, keine Dateien — alle None bis der erste refresh() lief.
        self.schema: dict | None = None
        self.request_template: dict | None = None
        self.digest: dict | None = None          # id -> {namen, typ, aktionen, wert}
        self.system_prompt: str | None = None
        self.version: str | None = None
        # Letzte classify()-Latenz in ms — fürs Logging in assistant.py.
        self.last_latency_ms: float = 0.0

    @property
    def ready(self) -> bool:
        with self._lock:
            return self.system_prompt is not None

    # ------------------------------------------------------------------
    # capabilities -> Schema/Prompt/Digest
    # ------------------------------------------------------------------
    def _fetch_capabilities(self) -> dict:
        req = urllib.request.Request(
            f"{self.cfg.base_url}/capabilities",
            headers={"X-Actuator-Token": self._token},
        )
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.loads(r.read().decode())

    def refresh(self) -> bool:
        """Holt /capabilities und baut Schema+Prompt+Digest komplett neu (in-memory).

        AKTIONEN-GETRIEBEN wie capabilities_to_schema.py:build() — Vokabular
        (ziel/aktion/einheit) kommt aus den Daten, nicht aus fest kodierten Typen.
        Bei Erfolg wird alles atomar unter Lock gesetzt; jede Exception wird
        gefangen, geloggt, und False zurückgegeben.
        """
        try:
            caps = self._fetch_capabilities()
            ziele = caps.get("ziele", [])

            ids = sorted(z["id"] for z in ziele)
            verbs = sorted({a for z in ziele for a in z.get("aktionen", [])})
            einheiten = sorted({z["wert"]["einheit"] for z in ziele if "wert" in z})

            schema = {
                "type": "object",
                "additionalProperties": False,
                "required": ["ist_kommando", "ziel", "aktion", "wert", "einheit"],
                "properties": {
                    "ist_kommando": {"type": "boolean"},
                    # "" = kein/unbekanntes Ziel (nur zusammen mit ist_kommando=false)
                    "ziel": {"enum": ids + [""]},
                    "aktion": {"enum": verbs + [None]},
                    "wert": {"type": ["integer", "null"]},
                    "einheit": {"enum": einheiten + [None]},
                },
            }
            request_template = {
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {"name": "intent", "strict": True, "schema": schema},
                },
                "chat_template_kwargs": {"enable_thinking": False},
                "temperature": 0,
                "max_tokens": 200,
            }

            digest = {
                z["id"]: {
                    "namen": z.get("namen", []),
                    "typ": z.get("typ"),
                    "aktionen": z.get("aktionen", []),
                    "wert": z.get("wert"),
                }
                for z in ziele
            }

            # Ziel-Liste fürs Prompt in der Reihenfolge, wie /capabilities sie
            # liefert (wie im Prototyp — NICHT alphabetisch sortiert wie die
            # Schema-Enums).
            lines = []
            for z in ziele:
                al = " / ".join(z.get("namen", []))
                rng = ""
                w = z.get("wert")
                if w:
                    rng = f"  [{w['einheit']} {w['min']}-{w['max']}]"
                lines.append(
                    f'- {z["id"]}: {al}  (aktionen: {",".join(z.get("aktionen", []))}){rng}'
                )
            system_prompt = _build_system_prompt("\n".join(lines))
            version = caps.get("version")

            with self._lock:
                self.schema = schema
                self.request_template = request_template
                self.digest = digest
                self.system_prompt = system_prompt
                self.version = version

            print(f"🔌 Aktuator: capabilities aktualisiert — {len(ids)} Ziele, Version {version}")
            return True
        except Exception as e:
            print(f"⚠️  Aktuator: refresh fehlgeschlagen: {e}")
            return False

    # ------------------------------------------------------------------
    # Hintergrund: MQTT-Change-Notification + Poll-Fallback
    # ------------------------------------------------------------------
    def start(self) -> None:
        """Einmaliges refresh(), danach Daemon-Thread für MQTT + Poll-Fallback."""
        self.refresh()
        t = threading.Thread(target=self._background_loop, daemon=True)
        t.start()

    def _background_loop(self) -> None:
        mqtt_mod = None
        if not self.cfg.mqtt_host:
            # Leerer Host = bewusst kein MQTT. Wichtig für Installationen ohne
            # Broker: sonst liefe der Client endlos gegen einen Default-Host,
            # den es dort gar nicht gibt. Der Poll deckt denselben Zweck ab,
            # nur träger. Siehe ACTUATOR_INTERFACE.md.
            print("🔌 Aktuator: kein mqtt_host konfiguriert — nur Poll-Fallback")
        else:
            try:
                import paho.mqtt.client as mqtt_mod
            except ImportError:
                print("⚠️  Aktuator: paho-mqtt nicht installiert — nur Poll-Fallback aktiv")

        if mqtt_mod is not None:
            self._start_mqtt(mqtt_mod)

        self._poll_loop()

    def _start_mqtt(self, mqtt_mod) -> None:
        def on_connect(client, userdata, flags, reason_code, properties=None):
            try:
                client.subscribe("voiceact/capabilities_changed")
                print(f"🔌 Aktuator: MQTT verbunden ({self.cfg.mqtt_host}:{self.cfg.mqtt_port})")
            except Exception as e:
                print(f"⚠️  Aktuator: MQTT subscribe fehlgeschlagen: {e}")

        def on_disconnect(client, userdata, disconnect_flags, reason_code, properties=None):
            print(f"⚠️  Aktuator: MQTT getrennt ({reason_code}) — reconnect via paho-Loop")

        def on_message(client, userdata, msg):
            try:
                payload = json.loads(msg.payload.decode())
                new_version = payload.get("version")
                with self._lock:
                    current_version = self.version
                if new_version and new_version != current_version:
                    print(
                        f"🔌 Aktuator: capabilities_changed → {new_version} "
                        f"(bisher {current_version}) — refresh"
                    )
                    self.refresh()
            except Exception as e:
                print(f"⚠️  Aktuator: MQTT on_message Fehler: {e}")

        try:
            client = mqtt_mod.Client(callback_api_version=mqtt_mod.CallbackAPIVersion.VERSION2)
            client.on_connect = on_connect
            client.on_disconnect = on_disconnect
            client.on_message = on_message
            client.reconnect_delay_set(min_delay=1, max_delay=30)
            client.connect_async(self.cfg.mqtt_host, self.cfg.mqtt_port, keepalive=60)
            client.loop_start()
        except Exception as e:
            print(f"⚠️  Aktuator: MQTT-Start fehlgeschlagen: {e}")

    def _poll_loop(self) -> None:
        """Fallback-Poll, falls MQTT nie ankommt (oder gar nicht verfügbar ist)."""
        poll_sec = max(1, self.cfg.refresh_poll_sec)
        while True:
            time.sleep(poll_sec)
            try:
                caps = self._fetch_capabilities()
                new_version = caps.get("version")
                with self._lock:
                    current_version = self.version
                if new_version != current_version:
                    print(
                        f"🔌 Aktuator: Poll erkennt neue Version {new_version} "
                        f"(bisher {current_version}) — refresh"
                    )
                    self.refresh()
            except Exception as e:
                print(f"⚠️  Aktuator: Poll-Fehler: {e}")

    # ------------------------------------------------------------------
    # Klassifikation + Sanity + Ausführung
    # ------------------------------------------------------------------
    def classify(self, text: str) -> dict | None:
        """STT-Text -> Intent-Dict via LLM, oder None bei jedem Fehler/Timeout.

        Latenz landet in self.last_latency_ms (nicht im Rückgabe-Dict).
        """
        with self._lock:
            request_template = self.request_template
            system_prompt = self.system_prompt
        if request_template is None or system_prompt is None:
            return None

        body = dict(request_template)
        body["messages"] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": text},
        ]
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            self.cfg.llm_url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        t0 = time.time()
        try:
            with urllib.request.urlopen(req, timeout=self.cfg.llm_timeout) as r:
                out = r.read().decode()
            self.last_latency_ms = (time.time() - t0) * 1000
            content = json.loads(out)["choices"][0]["message"]["content"]
            return json.loads(content)
        except Exception as e:
            self.last_latency_ms = (time.time() - t0) * 1000
            print(f"⚠️  Aktuator: classify fehlgeschlagen ({self.last_latency_ms:.0f} ms): {e}")
            return None

    def is_actionable(self, intent: dict) -> bool:
        """Client-seitige Sanity gegen den Digest — zusätzlich zur Schema-Form.

        True nur wenn ist_kommando gesetzt, ziel nicht leer, aktion nicht None,
        UND ziel/aktion tatsächlich im aktuellen Digest bekannt sind.
        """
        if not intent:
            return False
        if not intent.get("ist_kommando"):
            return False
        ziel = intent.get("ziel")
        if not ziel:
            return False
        aktion = intent.get("aktion")
        if aktion is None:
            return False
        with self._lock:
            digest = self.digest
        if digest is None:
            return False
        entry = digest.get(ziel)
        if entry is None:
            return False
        return aktion in (entry.get("aktionen") or [])

    def execute(self, intent: dict, request_id: str, bestaetigt: bool = False) -> dict | None:
        """POST /intent. Bewusst KEIN konfidenz-Feld (sicherer Default: kosten=hoch
        fragt dann immer nach, siehe ACTUATOR_V1_PLAN.md).

        HTTP 200 -> geparstes JSON. Transportfehler/Timeout -> GENAU EIN Retry
        mit derselben request_id (Node-RED dedupliziert per request_id, TTL 60s).
        HTTP 4xx/5xx -> kein Retry, None (definitive fachliche oder Envelope-
        Antwort bereits durch Node-RED getroffen).
        """
        body = {
            "ist_kommando": True,
            "ziel": intent.get("ziel"),
            "aktion": intent.get("aktion"),
            "wert": intent.get("wert"),
            "einheit": intent.get("einheit"),
            "quelle": "aktuator",
            "request_id": request_id,
        }
        if bestaetigt:
            body["bestaetigt"] = True
        data = json.dumps(body).encode("utf-8")

        def _post() -> dict:
            req = urllib.request.Request(
                f"{self.cfg.base_url}/intent",
                data=data,
                headers={
                    "X-Actuator-Token": self._token,
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=self.cfg.intent_timeout) as r:
                return json.loads(r.read().decode())

        try:
            return _post()
        except urllib.error.HTTPError as e:
            body_err = e.read().decode(errors="replace")
            print(f"⚠️  Aktuator: /intent HTTP {e.code}: {body_err[:200]}")
            return None
        except Exception as e:
            print(f"⚠️  Aktuator: /intent Transportfehler ({e}) — 1 Retry mit gleicher request_id")
            try:
                return _post()
            except urllib.error.HTTPError as e2:
                body_err = e2.read().decode(errors="replace")
                print(f"⚠️  Aktuator: /intent Retry HTTP {e2.code}: {body_err[:200]}")
                return None
            except Exception as e2:
                print(f"⚠️  Aktuator: /intent Retry fehlgeschlagen: {e2}")
                return None
