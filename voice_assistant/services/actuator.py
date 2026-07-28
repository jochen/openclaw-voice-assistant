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
Geschichte/Entscheidungen dahinter. Die Prompt-Formulierung wird bewusst NICHT
umformuliert, sondern nur gegen tools/actuator_grammar_test.py geaendert —
siehe die Messreihe im Docstring von _build_system_prompt.

Jeder Netzwerk-/Parse-Fehler wird gefangen und geloggt — der Assistant muss
ohne Aktuator (bzw. mit ihm im "kein Kommando"-Zustand) genauso weiterlaufen
wie vor diesem Umbau (graceful degradation, z.B. wenn der Gemma-Container aus
ist).
"""

from __future__ import annotations

import json
import re
import threading
import time
import urllib.error
import urllib.request

from voice_assistant.config import ActuatorConfig


# Ziel-Typen, deren Einzelgerät-gegen-Gruppe-Fall der statische Beispielblock in
# _build_system_prompt schon vorführt. Statischer und generierter Teil bilden EINEN
# Prompt — sie müssen voneinander wissen, sonst doppeln sie dieselbe Lehre.
# Wer unten ein Beispiel hinzufügt oder entfernt, pflegt diese Menge mit.
_STATISCH_GEZEIGTE_TYPEN = {"rollo"}

# Umgangssprachliche Aktionswörter -> kanonische Aktion des Schemas. Nur für
# das Mehrzahl-Muster unten; der normale Weg über das Modell braucht das nicht.
_AKTIONSWORT = {
    "an": "ein", "ein": "ein", "einschalten": "ein",
    "aus": "aus", "ausschalten": "aus",
    "auf": "auf", "hoch": "auf", "rauf": "auf", "oeffne": "auf", "öffne": "auf",
    "zu": "zu", "runter": "zu", "herunter": "zu", "schliess": "zu", "schließ": "zu",
}
_AKTIONSTEIL = (
    r"(?:auf\s+(?P<wert>\d{1,3})\s*(?:prozent|%|grad)"
    r"|(?P<wort>" + "|".join(sorted(_AKTIONSWORT, key=len, reverse=True)) + r"))"
)
# Gruppen-Alias der Form "alle <EinWort>" — daraus entsteht das Mehrzahl-Muster.
# Genau EIN Wort: "alle Rollos" trägt, "alle Rollos in der Küche" nicht (das ist
# eine Raumgruppe, die der Nutzer auch benennt).
_ALLE_ALIAS = re.compile(r"^alle\s+(\w+)$", re.IGNORECASE)


def _mehrzahl_muster(digest: dict) -> list[tuple, ]:
    """[(Muster, ziel_id)] für "‹Mehrzahl› ‹Aktion›" ohne Raumangabe.

    Wird bei jedem refresh() aus den capabilities gebaut — im Code steht KEINE
    Ziel-id und kein Gerätewort. Eine Gruppe nimmt daran teil, indem sie einen
    Alias der Form "alle <Mehrzahl>" führt (also genau das Wort, mit dem man
    sie ohne Raum meint). Führt keine Gruppe so einen Alias, entfällt der
    Mechanismus ersatzlos — dieselbe Bauweise wie _kontrast_beispiel, aus dem
    gleichen Grund: ids sind je Installation andere.

    Mehrdeutige Wörter werden verworfen: nennen zwei Gruppen dieselbe
    Mehrzahl, kann niemand entscheiden welche gemeint ist.
    """
    kandidaten: dict[str, list[str]] = {}
    for zid, z in digest.items():
        if not z.get("mitglieder"):
            continue
        for name in z.get("namen") or []:
            m = _ALLE_ALIAS.match(name.strip())
            if m:
                kandidaten.setdefault(m.group(1).lower(), []).append(zid)
    muster = []
    for wort, ziele in kandidaten.items():
        if len(ziele) != 1:
            continue
        muster.append((
            re.compile(
                r"^\s*(?:mach|macht|fahr|fahre|schalte|schalt)?\s*(?:mal\s+)?"
                r"(?:die\s+|alle\s+)?" + re.escape(wort) + r"\s+" + _AKTIONSTEIL +
                r"\s*[.!?]*\s*$",
                re.IGNORECASE,
            ),
            ziele[0],
        ))
    return muster


def _kontrast_beispiel(digest: dict) -> str:
    """Erzeugt ein Einzelgerät-gegen-Gruppe-Beispielpaar aus den Live-Daten.

    Die Namensregel (ACTUATOR_INTERFACE.md, Teil 2) verlangt, dass eine Gruppe
    keine morphologische Variante ihrer Mitglieder ist. Wo sie sich trotzdem
    ähneln, verschluckt die Gruppe den Einzelbefehl: gemessen 2026-07-26 ging
    "Mach das Badlicht oben an" auf badbeleuchtungoben statt badlichtoben, in
    jeder Formulierung. Ein Beispielpaar für genau dieses Paar hob 13/16 auf
    16/16 (auf zurückgehaltenen Sätzen, keine Regression anderswo).

    Fest verdrahten lässt es sich nicht — ids sind je Installation andere, und
    ein Beispiel mit unbekannter id wäre schlimmer als keins. Also je Gruppe
    (erkennbar an `mitglieder`) ein Paar, erzeugt bei jedem refresh(). Fehlt
    `mitglieder` in den capabilities, entfällt der Block ersatzlos.

    Zwei Dinge, die gemessen wurden und nicht offensichtlich sind:

    Nur EIN Paar für die riskanteste Gruppe reicht nicht. Der Versuch, sie über
    Zeichenähnlichkeit (SequenceMatcher zwischen Gruppen- und Mitglieds-id) zu
    bestimmen, wählte wohnzimmerlicht/wohnzimmerbeleuchtung — ein Paar, das gar
    nicht verwechselt wird — und ließ das echte Problem stehen (34/38).
    Zeichenähnlichkeit misst nicht Verwechslungsrisiko. Ein Paar pro Gruppe
    braucht keine Heuristik und kann nichts falsch wählen.

    Für das Mitglied wird der LÄNGSTE Alias genommen, nicht namen[0]. Klingt
    nach Kosmetik, entschied aber alles: badlichtoben führt namen[0]
    "Badlichtoben" (schreibt so niemand) und daneben "Badlicht oben". Mit
    namen[0] blieb es bei 35/38, mit dem längsten Alias 38/38. Der längere
    Alias ist die gesprochene Form, und darauf trifft das Gehörte.
    """
    block = []
    for gid, g in digest.items():
        mitglieder = [m for m in (g.get("mitglieder") or []) if m in digest]
        if not mitglieder:
            continue
        mid = mitglieder[0]
        # Typen, für die der statische Beispielblock den Einzel-gegen-Gruppe-Fall
        # schon zeigt (kuechenrollo_links/kuechenrollos), werden übersprungen —
        # ein zweites Beispiel für dieselbe Lehre hilft nicht, es schadet.
        # Gemessen am 44-Satz-Set, als noderedpi4 allen 9 Gruppen `mitglieder`
        # gab: alle Gruppen 40/44, nur die noch nicht gezeigten Typen 41/44,
        # ohne Block 36/44 (dann bricht badlichtoben wieder). Der Ausfall bei
        # "alle Gruppen" war `badobenheizung` -> badlichtoben: das eigene
        # Beispiel "Schalte Badlicht oben ein" wurde selbst zum Attraktor.
        # MEHR FEW-SHOTS SIND NICHT BESSER. Ebenfalls verworfen: Paare zwischen
        # verschachtelten Gruppen (mitglieder ⊂ mitglieder) — 9 Paare mit
        # Dubletten, 27/30 und ein neuer Ausfall bei einem Nicht-Kommando.
        if digest[mid].get("typ") in _STATISCH_GEZEIGTE_TYPEN:
            continue
        # Verb, das für Gruppe UND Mitglied gültig ist. Hart "ein" zu nehmen
        # ginge nur bei Lichtern gut — bekäme eine Rollo-Gruppe ein
        # `mitglieder`-Feld, lehrte das Beispiel eine Aktion, die für dieses
        # Ziel gar nicht erlaubt ist, und Node-RED würde sie ablehnen.
        gemeinsam = [x for x in (digest[gid].get("aktionen") or [])
                     if x in (digest[mid].get("aktionen") or [])]
        if not gemeinsam:
            continue
        aktion = gemeinsam[0]
        satz = {
            "ein": "Schalte {} ein", "aus": "Schalte {} aus",
            "auf": "Mach {} auf", "zu": "Mach {} zu",
            "aktivieren": "Aktiviere {}", "starten": "Starte {}",
        }.get(aktion, aktion + " {}")
        mitglied_namen = digest[mid].get("namen") or [mid]
        gruppen_namen = digest[gid].get("namen") or [gid]
        # Für die Gruppe bevorzugt der "alle …"-Alias — das ist die
        # Formulierung, die verwechselt wird. "Schalte X ein" statt "Mach das
        # X an", weil ein Artikel bei Namen wie "Majas Stehlampe" nicht passt.
        gruppen_phrase = next(
            (n for n in gruppen_namen if n.lower().startswith("alle")), gruppen_namen[0]
        )
        block.append(
            f'{satz.format(max(mitglied_namen, key=len))} -> {{"ist_kommando":true,"aktion":"{aktion}","ziel":"{mid}","wert":null,"einheit":null}}\n'
            f'{satz.format(gruppen_phrase)} -> {{"ist_kommando":true,"aktion":"{aktion}","ziel":"{gid}","wert":null,"einheit":null}}\n'
        )
    return "".join(block)


def _build_system_prompt(ziel_liste: str, kontrast: str = "") -> str:
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

    Die ROLLO-OHNE-RAUM-Regel steht als EINZIGE Regel HINTER der Ziel-Liste,
    nicht oben bei den anderen. Nur die STELLUNG ist geändert — Wortlaut und
    die zwei Gegenbeispiele sind dieselben wie vorher.

    Gemessen 2026-07-28 mit tools/actuator_grammar_test.py, 36 Fälle, je
    3 Läufe (innerhalb eines Prozesses stabil, zwischen Prozessen schwankt es
    um ein bis zwei Fälle — Varianten deshalb immer back-to-back messen):

        A  Regel oben (bis dahin produktiv)   31/36   4x Einzahl -> alle_rollos
        F  Regel hinter der Ziel-Liste        32/36   0x
        M  wie F, Regel umformuliert          32/36   0x, exakt dieselben Fehler
        M2 nur umformuliert, Stellung oben    31/36   zusätzlich Tischlicht kaputt

    Die Ziel-Liste ist mit Abstand der längste Teil des Prompts; steht die
    Regel davor, gewinnt am Ende die Liste voller Rollo-ids. Der Effekt ist
    reine Stellung: M sagt wörtlich 'Mehrzahl ("Rollos zu") ist alle_rollos'
    und ändert am Ergebnis nichts.

    NICHT gemacht, weil gemessen SCHLECHTER — jeweils dieselbe Ursache, ein
    kleines Modell greift ein genanntes Ziel eher auf, als dass es das Verbot
    dazu befolgt:
      - Regel verbietet alle_rollos ausdrücklich .................. 24-25/36
      - Regel zählt die Artikelformen auf ("das Rollo", "die Rollos") .. 33/36*
      - Regel auf die Einzahl verengt, Mehrzahl positiv erlaubt ..... 31/36
      - vier statt zwei Gegenbeispiele .............................. 30/36*
      (* unter der später korrigierten Erwartung gemessen, siehe unten)

    OFFEN und bewusst so: "Rollos runter" / "Die Rollos hoch" (Mehrzahl ohne
    Raum) liefern ist_kommando=false und gehen damit an den Brain, obwohl
    alle_rollos richtig wäre (Entscheidung Jochen 2026-07-28: die Mehrzahl
    meint umgangssprachlich sehr wohl alle). Keine der gemessenen
    Prompt-Varianten bekommt beides gleichzeitig hin; die Einzahl richtig zu
    halten hat Vorrang, weil "Rollo zu" nachts sonst das ganze Haus schliesst.
    Das ist ein Fall für eine deterministische Regel im Code, nicht für den
    Prompt — der Aktuator soll ohne Internet arbeiten, und der Brain ist keine
    offline verfügbare Ausweichstelle.

    Anlass für die Neumessung: die Zahl "20/20" aus der Einführung galt für
    62 Ziele (capabilities e3d6af78). Inzwischen sind es 67, darunter die
    zusätzlichen Rollo-Gruppen rollos_ganzes_haus und rollos_im_westen — die
    Messung war nicht falsch, sondern veraltet. Nach jeder Änderung an den
    capabilities gehört sie deshalb wiederholt.
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
Wohnzimmerrollo auf 70% -> {{"ist_kommando":true,"aktion":"setzen","ziel":"wohnzimmerrollo","wert":70,"einheit":"prozent"}}
Rollo auf 70% -> {{"ist_kommando":false,"aktion":null,"ziel":"","wert":null,"einheit":null}}
Rollo zu -> {{"ist_kommando":false,"aktion":null,"ziel":"","wert":null,"einheit":null}}
{kontrast}Erzaehl mir einen Witz -> {{"ist_kommando":false,"aktion":null,"ziel":"","wert":null,"einheit":null}}

Bekannte Ziele:
{ziel_liste}

ROLLO OHNE RAUM: "Rollo" oder "Rollos" OHNE Raumangabe und OHNE "alle" ist KEIN Kommando fuer alle_rollos. Antworte ist_kommando=false. Nur "alle Rollos" (mit dem Wort "alle") ist alle_rollos."""


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
        # [(Muster, ziel_id)] aus den capabilities — siehe _mehrzahl_muster
        self.mehrzahl_muster: list = []
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
                    # Nur bei Sammel-Zielen: die ziel-ids, in die sich die
                    # Gruppe entfaltet. Der Assistent steuert sie nicht einzeln
                    # an — er baut daraus das Kontrast-Beispiel unten, und der
                    # Überwacher sieht im Mitschnitt, wozu eine Gruppe wird.
                    "mitglieder": z.get("mitglieder"),
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
            system_prompt = _build_system_prompt("\n".join(lines), _kontrast_beispiel(digest))
            mehrzahl = _mehrzahl_muster(digest)
            version = caps.get("version")

            with self._lock:
                self.schema = schema
                self.request_template = request_template
                self.digest = digest
                self.system_prompt = system_prompt
                self.mehrzahl_muster = mehrzahl
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
    def _mehrzahl_gruppe(self, text: str, intent: dict) -> dict:
        """Geräte-MEHRZAHL ohne Raum: Modell sagt nein, gemeint ist die Gruppe.

        Die ROLLO-OHNE-RAUM-Regel im Prompt (siehe _build_system_prompt) fängt
        die Einzahl ab — "Rollo zu" darf nachts nicht das ganze Haus schließen.
        Sie fängt aber die Mehrzahl mit ab, und "Mach die Rollos zu" meint
        umgangssprachlich sehr wohl alle (Entscheidung Jochen 2026-07-28).
        Keine der gemessenen Prompt-Varianten trennt beides; eine, die die
        Mehrzahl ausdrücklich erlaubt, verliert dafür die Einzahl (31/36 gegen
        32/36, alle Zahlen im Docstring von _build_system_prompt).

        Also hier, deterministisch: kein Modell, kein Prompt-Risiko. Das ist
        auch die einzig zulässige Stelle dafür — die Anlage soll ohne Internet
        schalten, ein "geht dann eben an den Brain" ist keine Lösung.

        Absichtlich als VOLLTREFFER-Muster und nicht als Suche nach dem Wort:
        alles, was mehr Wörter mitbringt ("Mach die Rollos im ganzen Haus zu",
        "Mach die Küchenrollos zu"), fällt durch und bleibt beim Modell. Ein zu
        weites Muster wäre hier genau der Fehler, den die Prompt-Regel
        verhindern soll — nur umgekehrt.

        Welche Wörter und welche Ziele das sind, steht NICHT im Code: die
        Muster kommen aus den capabilities (_mehrzahl_muster). Dieses Repo ist
        öffentlich und die ids sind je Installation andere.

        Greift ausschließlich, wenn das Modell schon "kein Kommando" gesagt hat.
        Ein positiv erkanntes Intent wird nie überschrieben.
        """
        if intent.get("ist_kommando"):
            return intent
        with self._lock:
            muster = list(self.mehrzahl_muster)
            digest = self.digest or {}
        for pattern, zid in muster:
            m = pattern.match(text or "")
            if not m:
                continue
            ziel = digest.get(zid)
            if not ziel:
                continue
            if m.group("wert") is not None:
                aktion, wert = "setzen", int(m.group("wert"))
            else:
                aktion, wert = _AKTIONSWORT.get(m.group("wort").lower()), None
            if not aktion or aktion not in (ziel.get("aktionen") or []):
                continue
            einheit = (ziel.get("wert") or {}).get("einheit") if wert is not None else None
            print(f"🔌 Aktuator: Mehrzahl ohne Raum → {zid}/{aktion} (lokale Regel)")
            return {"ist_kommando": True, "aktion": aktion, "ziel": zid,
                    "wert": wert, "einheit": einheit}
        return intent

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
            intent = json.loads(content)
            return self._mehrzahl_gruppe(text, intent)
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
