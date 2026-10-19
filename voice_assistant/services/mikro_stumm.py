"""Mikrofon stumm schalten, mit Zeitgrenze (``mikro:``, seit 2026-10-19).

Jochens Anlass: bei Besuch, und beim Anhören von Aufnahmen am Rechner
sollen die abgespielten Sätze keinen Trigger auslösen. Geschaltet wird der
Mute-Schalter des XVF3800 (``RespeakerClient.mute_setzen``), der Audiostrom
läuft dabei weiter, nur aus Nullen (umgebung.py lernt daraus keine Ruhe).

Wege hierher, alle in diese eine Klasse, damit nur eine Stelle weiß, warum
und wie lange das Mikro stumm ist:

- Sprache: services/mikro_parser.py („Gaston, stell dich taub“). Wieder an
  per Sprache nur über einen Satelliten (Handy).
- MQTT (``mqtt_topic``): ein Zigbee-Knopf über Node-RED, oder das Topic des
  Knopfs bei zigbee2mqtt direkt. ``{"action": …}`` schaltet um,
  ``{"stumm": true|false|"umschalten", "minuten": n, "ansage": false}``
  gezielt. Retained-Nachrichten zählen nicht (alter Zustand beim Verbinden).
- Speak-Server ``POST /mikro`` (lokal) und darüber das MCP-Werkzeug ``mikro``
  des Brains.

**Ohne Zeitgrenze nur auf ausdrücklichen Wunsch.** Ein vergessener Mute macht
Gaston tagelang still tot, und das merkt man erst, wenn man ihn braucht.
Standard ``standard_minuten`` (120, Jochen 2026-10-19). Der Zustand steht in
``mikro.json`` und überlebt Neustarts: der Mute sitzt im Gerät und bliebe
sonst ohne Zeitgrenze stehen. Der Knopf am Gerät wird respektiert und nur
geloggt; drückt ihn jemand während einer Zeitgrenze, gilt sie nicht mehr.
"""

from __future__ import annotations

import json
import os
import queue
import threading
import time
from dataclasses import dataclass
from datetime import datetime

# Nach einem eigenen Schaltbefehl so lange keine Änderung als „Knopf am
# Gerät“ deuten: die Firmware liest den Zustand nur jede Sekunde.
_EIGENER_BEFEHL_S = 4.0
_TAKT_S = 1.0


@dataclass
class Ergebnis:
    ok: bool
    text: str                 # Ansage für den Lautsprecher
    stumm: bool | None        # Zustand danach (soweit bekannt)
    bis: float | None = None  # Epoch, None = ohne Zeitgrenze bzw. nicht stumm


def _uhrzeit(ts: float) -> str:
    t = datetime.fromtimestamp(ts + 30)     # auf die Minute gerundet, nicht abgeschnitten
    return f"{t.hour} Uhr" if t.minute == 0 else f"{t.hour} Uhr {t.minute}"


class MikroSchalter:
    def __init__(self, client, standard_minuten: float, zustand_pfad: str,
                 log_pfad: str, ansagen=None) -> None:
        self._client = client
        self._standard_s = standard_minuten * 60
        self._zustand_pfad = zustand_pfad
        self._log_pfad = log_pfad
        self._ansagen = ansagen            # callable(text) oder None
        self._lock = threading.RLock()
        self._bis: float | None = None
        self._stumm_von_uns = False        # unser Mute (mit oder ohne Grenze)
        self._eigen_ts = 0.0
        self._gesehen: bool | None = None  # letzter beobachteter Gerätezustand
        self._abgeglichen = False
        self._eingang: queue.Queue = queue.Queue()
        self._zustand_lesen()
        threading.Thread(target=self._lauf, name="mikro-stumm", daemon=True).start()

    # --- öffentlich ---------------------------------------------------

    def stumm(self, sekunden: int | None = None, unbegrenzt: bool = False,
              quelle: str = "") -> Ergebnis:
        dauer = None if unbegrenzt else (sekunden or self._standard_s)
        with self._lock:
            if not self._client.mute_setzen(True):
                return self._fehler(quelle, "stumm")
            self._eigen_ts = time.monotonic()
            self._stumm_von_uns = True
            self._bis = time.time() + dauer if dauer else None
            self._gesehen = True
            self._zustand_schreiben()
        text = (f"Ich bin jetzt bis {_uhrzeit(self._bis)} taub." if self._bis
                else "Ich bin jetzt taub, bis mich jemand wieder anmacht.")
        self._log("stumm", quelle, bis=self._bis)
        return Ergebnis(True, text, True, self._bis)

    def an(self, quelle: str = "") -> Ergebnis:
        with self._lock:
            war = self._client.mute
            if not self._client.mute_setzen(False):
                return self._fehler(quelle, "an")
            self._eigen_ts = time.monotonic()
            self._stumm_von_uns = False
            self._bis = None
            self._gesehen = False
            self._zustand_schreiben()
        self._log("an", quelle, war_stumm=war)
        return Ergebnis(True, "Ich höre schon zu." if war is False else "Ich höre wieder zu.", False)

    def umschalten(self, quelle: str = "") -> Ergebnis:
        return self.an(quelle) if self._client.mute else self.stumm(quelle=quelle)

    def protokoll(self, **zeile) -> None:
        """Ein Sprach-Turn (Transkript, Sprecher) in mikro.log."""
        self._log("turn", "sprache", **zeile)

    def schnappschuss(self) -> dict:
        with self._lock:
            return {"stumm": self._client.mute, "bis": self._bis,
                    "bis_text": _uhrzeit(self._bis) if self._bis else None,
                    "von_uns": self._stumm_von_uns}

    def mqtt_starten(self, host: str, port: int, topic: str) -> None:
        """Wie der Rückspul-Marker (rewind.py): der Callback füllt nur eine
        Queue, geschaltet wird im eigenen Thread."""
        try:
            import paho.mqtt.client as mqtt
        except ImportError:
            print("⚠️  Mikro: paho-mqtt fehlt — kein MQTT-Eingang")
            return

        def on_connect(client, userdata, flags, reason_code, properties=None):
            client.subscribe(topic)
            print(f"🔇 Mikro: MQTT verbunden ({host}:{port}, {topic})")

        def on_message(client, userdata, msg):
            if msg.retain:
                return
            try:
                payload = json.loads(msg.payload.decode())
            except (ValueError, UnicodeDecodeError):
                payload = msg.payload.decode(errors="replace").strip()
            self._eingang.put(payload)

        try:
            client = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2)
            client.on_connect = on_connect
            client.on_message = on_message
            client.reconnect_delay_set(min_delay=1, max_delay=30)
            client.connect_async(host, port, keepalive=60)
            client.loop_start()
        except Exception as e:
            print(f"⚠️  Mikro: MQTT-Start fehlgeschlagen: {e}")

    def nachricht(self, payload, quelle: str = "mqtt", ansage: bool = True) -> Ergebnis | None:
        """Eine Nachricht von MQTT oder ``POST /mikro`` auswerten. ``ansage``:
        Standard, ob das Ergebnis gesprochen wird (der Brain spricht selbst).
        None = nichts zu tun (Statusmeldung des Knopfs ohne action)."""
        if isinstance(payload, str):
            payload = {"stumm": {"an": False, "aus": True, "stumm": True}.get(
                payload.lower(), "umschalten")}
        if not isinstance(payload, dict):
            return None
        if payload.get("action"):
            erg = self.umschalten(quelle)
        elif "stumm" in payload:
            soll = payload["stumm"]
            if soll == "umschalten":
                erg = self.umschalten(quelle)
            elif soll:
                minuten = payload.get("minuten")
                erg = self.stumm(int(float(minuten) * 60) if minuten else None,
                                 unbegrenzt=bool(payload.get("unbegrenzt")), quelle=quelle)
            else:
                erg = self.an(quelle)
        else:
            return None
        if self._ansagen and payload.get("ansage", ansage):
            self._ansagen(erg.text)
        return erg

    # --- intern -------------------------------------------------------

    def _fehler(self, quelle: str, was: str) -> Ergebnis:
        self._log("fehler", quelle, was=was)
        return Ergebnis(False, "Das Mikrofon lässt sich gerade nicht schalten.",
                        self._client.mute)

    def _lauf(self) -> None:
        while True:
            try:
                payload = self._eingang.get(timeout=_TAKT_S)
                self.nachricht(payload)
            except queue.Empty:
                pass
            except Exception as e:
                print(f"⚠️  Mikro: {e}")
            try:
                self._takt()
            except Exception as e:
                print(f"⚠️  Mikro: {e}")

    def _takt(self) -> None:
        geraet = self._client.mute
        if geraet is None:
            return                          # nicht verbunden
        with self._lock:
            if not self._abgeglichen:
                self._abgleichen(geraet)
            elif (geraet != self._gesehen
                  and time.monotonic() - self._eigen_ts > _EIGENER_BEFEHL_S):
                # Knopf am Gerät: respektieren, eine Zeitgrenze gilt nicht mehr
                self._log("knopf", "geraet", stumm=geraet)
                print(f"🔇 Mikro: Knopf am Gerät → {'stumm' if geraet else 'an'}")
                self._stumm_von_uns = False
                self._bis = None
                self._zustand_schreiben()
            self._gesehen = geraet
            ablauf = self._bis is not None and time.time() >= self._bis and geraet
        if ablauf:
            erg = self.an("zeit")
            if erg.ok and self._ansagen:
                self._ansagen(erg.text)

    def _abgleichen(self, geraet: bool) -> None:
        """Erster bekannter Gerätezustand nach dem Start gegen mikro.json."""
        self._abgeglichen = True
        if not geraet:
            if self._stumm_von_uns:
                self._stumm_von_uns, self._bis = False, None
                self._zustand_schreiben()
            return
        if self._stumm_von_uns:
            rest = "ohne Zeitgrenze" if self._bis is None else f"bis {_uhrzeit(self._bis)}"
            print(f"🔇 Mikro: stumm seit vor dem Start, {rest}")
        else:
            print("🔇 Mikro: stumm (Knopf am Gerät), keine Zeitgrenze")

    def _zustand_lesen(self) -> None:
        try:
            with open(self._zustand_pfad, encoding="utf-8") as fh:
                z = json.load(fh)
            self._stumm_von_uns = bool(z.get("stumm_von_uns"))
            self._bis = z.get("bis")
        except (FileNotFoundError, ValueError):
            pass

    def _zustand_schreiben(self) -> None:
        tmp = self._zustand_pfad + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"stumm_von_uns": self._stumm_von_uns, "bis": self._bis}, fh)
        os.replace(tmp, self._zustand_pfad)

    def _log(self, aktion: str, quelle: str, **mehr) -> None:
        zeile = {"ts": datetime.now().isoformat(timespec="seconds"), "aktion": aktion,
                 "quelle": quelle, **mehr}
        try:
            with open(self._log_pfad, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(zeile, ensure_ascii=False) + "\n")
        except OSError:
            pass
