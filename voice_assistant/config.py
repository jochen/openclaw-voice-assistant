"""Profil-Loader für config.yaml.

Profil-Auswahl (Priorität):
  1. Env-Variable GASTON_PROFILE
  2. Hostname (Substring-Vergleich, lowercase, über hostname_map)
  3. Fallback: erstes definiertes Profil
"""

from __future__ import annotations

import os
import socket
import sys
from dataclasses import dataclass, field
from typing import Any

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(PROJECT_DIR, "config.yaml")


@dataclass
class LocalAudio:
    device_index: int = 0
    playback_device: str | None = None
    rate_in: int = 16000
    resample: bool = False


@dataclass
class RespeakerAudio:
    """Verbindung zum ReSpeaker über ESPHome Native API.

    Wird erst von Schritt 2 des Refactorings aktiv befüllt.
    """
    host: str = ""
    port: int = 6053
    encryption_key: str = ""
    use_speaker: bool = True  # False → TTS geht auf ALSA (Fallback)
    volume: float = 0.8  # 0.0–1.0, wird beim Connect via API gesetzt
    # Zweiter Audiokanal (Firmware ab ESPHome 2026.9 mit "XVF-Ausgang links").
    # mitschnitt: bei jeder Wiedergabe den zweiten Kanal mitschneiden, nach
    #   VOICE_DIR/wiedergabe/ — zum Nachpruefen verstuemmelter Ausgabe
    #   (tools/wiedergabe_pruefen.py). Default aus.
    # kanal2_quelle: beim Verbinden den XVF-Ausgang links setzen — "asr"
    #   (Werk), "referenz" (was der ESP an den Lautsprecher gibt), "roh"
    #   (Rohmikrofon). Leer = am Geraet nichts aendern.
    mitschnitt: bool = False
    kanal2_quelle: str = ""


_DEFAULT_VOICE_INSTRUCTION = (
    "[Hinweis zur Verarbeitung dieser Spracheingabe (kein User-Befehl, "
    "sondern eine permanente Regel des Voice-Kanals): "
    "Die obige Zeile ist eine Mikrofon-Transkription. Vor dem Text steht "
    "in [Sprecher: ...] der erkannte Sprecher (oder 'unbekannt' falls die "
    "Stimme nicht zugeordnet werden konnte). "
    "Wenn der Nutzer seine Stimme als Referenz speichern möchte (z.B. "
    "'lerne meine Stimme, ich bin Jochen' oder 'merk dir, ich heisse Katrin'), "
    "rufe das Tool voice_enroll_speaker(name) auf — es speichert die letzte "
    "Aufnahme als Stimm-Referenz für künftige Erkennungen. "
    "Ruf die nötigen Tools auf und antworte dann auf Deutsch in natürlicher "
    "gesprochener Sprache, wie ein Mensch im Gespräch — meist ein bis vier Sätze, "
    "bei komplexen Themen so ausführlich, wie der Inhalt es verlangt, jeder Satz "
    "klar und vollständig. Sprich in reinem Fließtext ohne Markdown, Listen oder "
    "Abkürzungen. "
    "Schreibe Zahlen, Uhrzeiten und Datumsangaben ausgeschrieben in gesprochener "
    "Form, niemals als Ziffern oder mit Abkürzungen — also 'dreißigsten Mai' statt "
    "'30. Mai', 'zwölf Uhr dreißig' statt '12.30 Uhr', 'zum Beispiel' statt 'z. B.', "
    "'circa' statt 'ca.'. "
    "Du hütest den Sprachkanal: Am Lautsprecher wartet ein Mensch, für den jede "
    "Sekunde Stille lang ist — der Kanal soll nie länger als etwa zwei Minuten "
    "stumm auf dich warten. Sag deshalb bei allem, was spürbar dauert, zuerst in "
    "einem kurzen Satz, was du tust (zum Beispiel 'Einen Moment, ich schaue das "
    "nach'). Sobald absehbar ist oder sich mitten in der Arbeit herausstellt, "
    "dass etwas länger braucht — externe Wartezeit, viele Schritte, unerwartete "
    "Zusatzarbeit —, gib sofort eine kurze gesprochene Rückmeldung, dass du dich "
    "kümmerst und dich meldest, erledige die Arbeit im Hintergrund und sag das "
    "Ergebnis über das Tool voice_speak_text an (kurz und gesprochen "
    "zusammengefasst). "
    "Beides zusammen ist ein Fehler: was du per voice_speak_text ansagst, darf "
    "nicht auch noch in deiner Antwort stehen. Der Mensch hört es sonst zweimal "
    "— das zweite Mal Minuten später, aus dem Zusammenhang gerissen, weil eine "
    "Ansage bis zur nächsten Ruhepause wartet. Entweder du antwortest direkt "
    "(dann kein voice_speak_text), oder deine Antwort endet mit der kurzen "
    "Zwischenmeldung und das Ergebnis kommt ausschließlich per voice_speak_text. "
    "Das Mandat gilt fürs Antworten, nicht fürs Schalten: lieber eine kurze "
    "Rückfrage als ein geratenes Gerät. "
    "Niemals etwas erfinden — entweder Tool aufrufen oder sagen was du nicht "
    "weißt. Das gilt auch für Ziele: ein Gerät, das du im Transkript nicht "
    "sicher wiedererkennst, wird nicht erraten.]"
)


# Hinter das Transkript gehängt, wenn anrede.anrede_im_text() nein sagt und der
# Sprecher nicht bekannt ist — nur im Erst-Turn nach dem Wakewort. Ein Beleg,
# keine Anweisung zum Schweigen: echte Befehle mit verhörtem Wakewort ("Das Tor,
# schalt das Tischlicht aus") tragen denselben Hinweis, und der Inhalt zeigt,
# dass sie gemeint sind.
_DEFAULT_ANREDE_HINWEIS = (
    "[Hinweis zur Aufnahme: Das Wakewort kommt in dieser Transkription nicht vor, "
    "und der Sprecher ist nicht bekannt. Bisher war das fast immer eine "
    "Fehlauslösung — Fernseher, Radio, Hörspiel oder ein Gespräch im Raum, das "
    "nicht dir galt. Ist der Text erkennbar an dich gerichtet (eine Frage oder ein "
    "Auftrag an den Assistenten), antworte normal. Sonst antworte ausschließlich "
    "mit NO_REPLY.]"
)


@dataclass
class LocaleConfig:
    wakeword_ack: str = "Ja?"
    confirmation_prefix: str = "Ich habe verstanden: "
    no_reply_fallback: str = "Entschuldigung, ich konnte keine Antwort erhalten."
    openclaw_voice_instruction: str = _DEFAULT_VOICE_INSTRUCTION
    anrede_hinweis: str = _DEFAULT_ANREDE_HINWEIS
    thinking_phrases: list = field(default_factory=lambda: [
        "Einen Moment bitte.",
        "Ich schaue kurz nach.",
        "Ich bin noch dabei.",
        "Fast fertig.",
        "Noch einen Augenblick.",
    ])


@dataclass
class LedsConfig:
    wled_enabled: bool = True
    wled_host: str = "wled.local"
    respeaker_ring_enabled: bool = False


# --- Aktuator: Prompt-Vorlagen (sprachabhängig, deshalb hier und nicht im Code)
#
# Der Prompt ist die einzige Stelle, an der dieses Projekt Deutsch VORAUSSETZT,
# und er nennt Beispiel-ids aus DIESER Installation (flurlicht, felixheizung,
# kuechenrollo_links, …). Beides gehört nicht in ein öffentliches Repo als
# unveränderliche Wahrheit — wer eine andere Sprache oder andere Geräte hat,
# überschreibt `actuator.system_prompt` im Profil und ist fertig.
#
# Die Beispiele sind gemessen, nicht geraten (siehe tools/actuator_grammar_test.py
# und den Docstring von actuator._build_system_prompt). Wer sie ersetzt, misst
# neu — auch in der eigenen Sprache. Weniger Beispiele waren dabei mehrfach
# besser als mehr.
#
# Platzhalter, die zur Laufzeit ersetzt werden (einfache Textersetzung, KEIN
# str.format — die geschweiften Klammern der JSON-Beispiele bleiben deshalb
# so stehen, wie man sie schreibt):
#   {kontrast}      aus den capabilities erzeugte Einzelgerät-gegen-Gruppe-Paare
#   {ziel_liste}    die Ziel-Liste
#   {gruppen_regel} aus den capabilities erzeugte Regel(n) für Geräte-Mehrzahl
#                   ohne Raumangabe — steht bewusst GANZ AM ENDE, hinter der
#                   Ziel-Liste (gemessen: davor wirkt sie nicht)
_DEFAULT_ACTUATOR_PROMPT = """Du bist der lokale Schalt-Aktuator. Wandle den gesprochenen Satz in EIN JSON-Intent. Gib NUR das JSON aus.
aktion: ein/aus (Licht,Schalter), auf/zu (Rollo ganz oeffnen/schliessen; "hoch"=auf,"runter"=zu), setzen (Zahlenwert), aktivieren (Szene), starten (Routine).
wert(Zahl)+einheit nur bei setzen (prozent Rollo, grad Heizung), sonst null. Kein Steuerkommando -> ist_kommando=false, ziel="", rest null.
Waehle das passende ziel aus der Liste (id links). Aliase stehen rechts.
EINZAHL vs MEHRZAHL: "das <Geraet>" meint EIN einzelnes Ziel. "die"/"alle <Geraete> in <Raum>" meint das Sammel-Ziel fuer diesen Raum, falls die Liste eines fuehrt.

Beispiele:
Schalte das Flurlicht ein -> {"ist_kommando":true,"aktion":"ein","ziel":"flurlicht","wert":null,"einheit":null}
Stell die Felixheizung auf 22 Grad -> {"ist_kommando":true,"aktion":"setzen","ziel":"felixheizung","wert":22,"einheit":"grad"}
Mach das Kuechenrollo links zu -> {"ist_kommando":true,"aktion":"zu","ziel":"kuechenrollo_links","wert":null,"einheit":null}
Mach alle Rollos in der Kueche zu -> {"ist_kommando":true,"aktion":"zu","ziel":"kuechenrollos","wert":null,"einheit":null}
Mach alle Rollos zu -> {"ist_kommando":true,"aktion":"zu","ziel":"alle_rollos","wert":null,"einheit":null}
Wohnzimmerrollo auf 70% -> {"ist_kommando":true,"aktion":"setzen","ziel":"wohnzimmerrollo","wert":70,"einheit":"prozent"}
Rollo auf 70% -> {"ist_kommando":false,"aktion":null,"ziel":"","wert":null,"einheit":null}
Rollo zu -> {"ist_kommando":false,"aktion":null,"ziel":"","wert":null,"einheit":null}
{kontrast}Erzaehl mir einen Witz -> {"ist_kommando":false,"aktion":null,"ziel":"","wert":null,"einheit":null}

Bekannte Ziele:
{ziel_liste}

{gruppen_regel}"""

# Eine Zeile je Gruppe, die einen Alias "alle <Mehrzahl>" führt. Platzhalter:
#   {einzahl_gross} {einzahl}  aus dem `typ` der Mitglieder ("rollo" -> Rollo/ROLLO)
#   {mehrzahl}                 das Wort aus dem "alle …"-Alias
#   {ziel}                     die id der Gruppe
# Steht der Satz erst hinter der Ziel-Liste, trägt er — davor gewinnt die Liste.
_DEFAULT_ACTUATOR_GRUPPEN_REGEL = (
    '{einzahl_gross} OHNE RAUM: "{einzahl}" oder "{mehrzahl}" OHNE Raumangabe und OHNE '
    '"alle" ist KEIN Kommando fuer {ziel}. Antworte ist_kommando=false. '
    'Nur "alle {mehrzahl}" (mit dem Wort "alle") ist {ziel}.'
)

# Satzschablonen für die aus den capabilities erzeugten Kontrast-Beispiele.
# Fehlt eine Aktion, wird "<aktion> {}" genommen.
_DEFAULT_ACTUATOR_BEISPIEL_SAETZE = {
    "ein": "Schalte {} ein", "aus": "Schalte {} aus",
    "auf": "Mach {} auf", "zu": "Mach {} zu",
    "aktivieren": "Aktiviere {}", "starten": "Starte {}",
}

# Torfrage VOR der Klassifikation: "will der Sprecher etwas schalten?" — eine
# eigene, kurze Entscheidung mit der Antwort ja/nein (ein bis zwei Token).
# Anlass (2026-09-23): der Aktuator schaltete aus Gerede heraus ("…den
# gesamten Kalender bitte komplett sperren" -> rollostop/starten). Die
# Klassifikation muss sich auf EIN Ziel festlegen, auch wenn keines gemeint
# ist; die Torfrage darf einfach nein sagen.
#
# Gemessen 2026-09-23 auf 182 Saetzen (32 aus actuator_grammar_test.py, 65
# echte Aktuator-Turns, 85 echte Brain-Turns), Gemma-4-E2B, zusammen mit der
# Klassifikation: ohne Tor 172 richtig / 3 falsch geschaltet; mit diesem Tor
# 163 / 0. Der Preis sind Kommandos, die das Tor uebersieht (16 von 86, viele
# davon STT-Kauderwelsch) — die gehen an den Brain, also langsam statt falsch.
# Varianten (Messreihe):
#   T1  Klassifikations-Prompt auf ja/nein umgeschrieben   161 / 0, 19 uebersehen
#   T2  eigener Prompt, "verhoerte Woerter zaehlen"       163 / 2  (Kauderwelsch kam durch)
#   T3  T2 + Beispiele Einzahl-mit-Wert / Mehrzahl         163 / 0, 16 uebersehen  <- dieser
#   T4  T3 ohne die Verhoert-Zeile                         163 / 0, 17 uebersehen
# Die Beispiele stammen bewusst NICHT aus den Messsaetzen.
#
# Platzhalter wie beim Klassifikations-Prompt: {ziel_liste}, {gruppen_regel}
# (letztere aus actuator.tor_gruppen_regel). Die Mehrzahl-Regel ist hier eine
# ANDERE als im Klassifikations-Prompt: "die Rollos zu" macht erst die lokale
# Mehrzahl-Regel NACH dem Modell zu alle_rollos — die Torfrage muss also ja
# sagen, sonst kommt der Satz dort nie an (mit dem umgeschriebenen
# Klassifikations-Prompt sagte sie nein: "Rollos runter" P(ja)=0,000).
_DEFAULT_ACTUATOR_TOR_PROMPT = """Du bist das Tor vor dem Schalt-Aktuator. Entscheide NUR: Will der Sprecher mit diesem Satz eines der bekannten Ziele schalten? Antworte NUR mit ja oder nein.
ja: ein Schaltwunsch fuer ein Ziel aus der Liste: ein/aus, auf/zu, hoch/runter, auf einen Wert setzen, Szene aktivieren, Routine starten. Auch wenn einzelne Woerter verhoert klingen, zaehlt die erkennbare Absicht.
nein: Gespraech, Fragen, Kommentare, Bestaetigungen, Erzaehlungen, Wuensche an etwas, das nicht in der Liste steht. Dass ein Raum oder Geraet im Satz vorkommt, macht ihn noch nicht zum Schaltwunsch.

Beispiele:
Schalte das Flurlicht ein -> ja
Stell die Felixheizung auf 22 Grad -> ja
Mach das Kuechenrollo links zu -> ja
Die Rollos bitte hoch -> ja
Rollos wieder rauf -> ja
Mach mal das Licht im Flur aus -> ja
Das Rollo bitte runter -> nein
Rollo auf 30 Prozent -> nein
Ja, passt so. -> nein
Im Wohnzimmer ist es heute richtig gemuetlich -> nein
Das Licht war gestern viel zu hell -> nein
Wie spaet ist es? -> nein

Bekannte Ziele:
{ziel_liste}

{gruppen_regel}"""

_DEFAULT_ACTUATOR_TOR_GRUPPEN_REGEL = (
    '{einzahl_gross} OHNE RAUM: "{einzahl}" in der Einzahl OHNE Raumangabe ist nein. '
    'Die Mehrzahl "{mehrzahl}" ist ja, auch ohne das Wort "alle".'
)


@dataclass
class ActuatorConfig:
    """Voice-Aktuator v1 — schneller lokaler Schalt-Pfad (siehe ACTUATOR_V1_PLAN.md
    und voice_assistant/services/actuator.py). Default enabled=False: Profile ohne
    den `actuator:`-Block verhalten sich exakt wie vor diesem Umbau."""
    enabled: bool = False
    # Leer = muss im Profil gesetzt werden. Kein echter Host als Default —
    # das Repo ist öffentlich, die Adresse gehört in die (gitignorierte)
    # config.yaml. Siehe ACTUATOR_INTERFACE.md.
    base_url: str = ""
    # leer -> Default <repo-root>/voiceact-token.txt (siehe _parse_profile,
    # aus PROJECT_DIR abgeleitet statt hart kodiert)
    token_file: str = ""
    llm_url: str = "http://localhost:8090/v1/chat/completions"
    llm_timeout: float = 5.0
    intent_timeout: float = 1.5
    mqtt_host: str = ""
    mqtt_port: int = 1883
    refresh_poll_sec: int = 600
    # Sprach-/installationsabhängig — siehe die Vorlagen oben.
    system_prompt: str = _DEFAULT_ACTUATOR_PROMPT
    gruppen_regel: str = _DEFAULT_ACTUATOR_GRUPPEN_REGEL
    beispiel_saetze: dict = field(
        default_factory=lambda: dict(_DEFAULT_ACTUATOR_BEISPIEL_SAETZE)
    )
    # Ziel-Typen, für die der Beispielblock im Prompt den Einzelgerät-gegen-
    # Gruppe-Fall schon zeigt. Der erzeugte Kontrast-Block überspringt sie —
    # ein zweites Beispiel für dieselbe Lehre hilft nicht, es schadet
    # (gemessen, siehe actuator._kontrast_beispiel). Wer den Prompt ersetzt,
    # pflegt diese Liste mit.
    beispiel_typen: list = field(default_factory=lambda: ["rollo"])
    # Torfrage vor der Klassifikation (siehe _DEFAULT_ACTUATOR_TOR_PROMPT).
    # Default aus: ohne den Eintrag verhält sich ein Profil wie vorher.
    tor_enabled: bool = False
    tor_prompt: str = _DEFAULT_ACTUATOR_TOR_PROMPT
    tor_gruppen_regel: str = _DEFAULT_ACTUATOR_TOR_GRUPPEN_REGEL
    # Zweiter Klassifikator: Laya (laya-serve unter laya_url) beantwortet
    # Tor, Ziel und Aktion in einem Durchlauf. Leer = kein Laya; dann
    # entscheidet Gemma allein wie vor dem Einbau. Siehe
    # voice_assistant/services/aktuator_schatten.py und laya_intent.py.
    laya_url: str = ""
    # Als Entscheider muss Laya schnell sein (gemessen ~90 ms): nach diesem
    # Timeout entscheidet Gemma im selben Turn.
    laya_timeout: float = 2.0
    laya_schwelle: float = 0.5
    # Tor ja, Geraet unklar -> Rueckfrage statt Brain (Entscheidung
    # 2026-10-01, Begruendung in aktuator_schatten.py).
    laya_rueckfrage: bool = True
    # Wer entscheidet: "gemma" (Default — ohne den Eintrag wie vorher) oder
    # "laya". Mit "laya" ist Gemma der Rueckfall, wenn Laya nicht antwortet.
    klassifikator: str = "gemma"
    # Die nicht entscheidende Kette im Schatten mitlaufen lassen
    # (actuator_schatten.log, tools/aktuator_vergleich.py --schatten).
    # Entscheidet Gemma, laeuft Laya mit, sobald laya_url gesetzt ist.
    schatten: bool = True


@dataclass
class RewindConfig:
    """Rückspul-Puffer (voice_assistant/rewind.py): die letzten Minuten Mikro
    samt Score-Verlauf im RAM, auf die Platte nur bei einem Anlass. Default
    enabled=False: ohne den `rewind:`-Block wird nichts gepuffert."""
    enabled: bool = False
    # Pufferlaenge. Ein manueller Marker kommt Sekunden nach dem verlorenen
    # Ruf — 120 s decken auch den Griff zum Taster am anderen Ende des Raums.
    seconds: float = 120.0
    # Bei jedem Trigger so viel VOR dem Trigger sichern (0 = aus): kommt der
    # Nutzer nach vergeblichen Versuchen doch durch, stecken sie hier drin.
    before_trigger_seconds: float = 30.0
    # Manueller Marker per MQTT, z.B. ein Zigbee-Taster via zigbee2mqtt. Jede
    # Nachricht mit nicht-leerem "action" sichert den ganzen Puffer; Status-
    # Meldungen des Geraets (Batterie, Linkqualitaet) haben keins.
    # Leerer Host oder leeres Topic = kein Marker.
    marker_mqtt_host: str = ""
    marker_mqtt_port: int = 1883
    marker_topic: str = ""


@dataclass
class WatcherConfig:
    """Überwacher Stufe 1 (nur LESEN + MELDEN, siehe services/watcher.py und
    tools/actuator_watch.py). Default enabled=False: ohne den `watcher:`-Block
    läuft kein Überwacher-Thread."""
    enabled: bool = False
    # Telegram-Ziel für Befund-Meldungen. Leer = kein Melden (nur sammeln).
    # Separater Chat, NICHT der Family-Voice-Chat — bewusst getrennt.
    chat_id: str = ""
    # Bot-Token, leer = profil-telegram_bot_token wird genutzt (gleicher Bot,
    # anderer Chat).
    bot_token: str = ""
    # Prüintervall in Sekunden (min 60, default 5 Min).
    poll_interval: int = 300
    # Stille Stunden (volle Stunden 0–23). In dieser Zeit wird nichts gesendet,
    # Befunde gesammelt. Default 01:00–07:00. Bereich über Mitternacht möglich.
    quiet_start: int = 1
    quiet_end: int = 7
    # LLM-Endpoint für die semantische Prüfung (Transkript vs. Intent).
    # OpenAI-kompatibel, wie actuator.llm_url. Leer = semantische Prüfung aus.
    llm_url: str = ""
    # Modellname, z.B. "vllm/release/glm-5-2". Leer = semantische Prüfung aus.
    llm_model: str = ""
    # API-Key für den Provider. Leer = kein Auth-Header (lokaler Endpunkt).
    llm_api_key: str = ""
    # Timeout für den LLM-Call in Sekunden. Höher als der Aktuator (5s) weil
    # der Overseer-Thread nicht blockiert — lieber spät melden als gar nicht.
    # Bei Lastspitzen/Reasoning braucht GLM-5-2 manchmal >10s.
    llm_timeout: float = 30.0
    # Haus-MCP der Gegenstelle (Streamable HTTP), aus dem Argus das Weltmodell
    # holt (Werkzeug haus_weltmodell). Leer = ohne Weltmodell, Prompt wie bis
    # 2026-10-04. Eigener Token je Client — NICHT der des Brains, damit die
    # Gegenstelle unterscheiden kann, wer fragt.
    haus_mcp_url: str = ""
    haus_mcp_token: str = ""


@dataclass
class DienstWaechterConfig:
    """Dienst-Waechter (services/dienstwaechter.py): merkt, wenn STT, Laya,
    Klassifikations-LLM oder Speaches wegbleiben, meldet es an die Gruppe des
    Ueberwachers (watcher.chat_id) und startet lokale Container neu.

    Default enabled=False: ohne den Block laeuft nichts. Die Dienste stehen
    nicht hier, sie kommen aus dem Profil (jede gesetzte URL, die der
    Assistent benutzt); ``dienste`` ergaenzt weitere als {name, url}.
    """
    enabled: bool = False
    intervall: float = 60.0
    # So lange darf ein Dienst weg sein, bevor gemeldet und geheilt wird —
    # ein Container-Neustart von Hand oder ein Modell-Laden soll nicht melden.
    gnadenfrist: float = 180.0
    heilen: bool = True
    max_heilversuche: int = 3
    heil_abstand: float = 1800.0
    # systemd-User-Units, waehrend derer Ausfaelle geplant sind (z.B. ein
    # Training, das Container stoppt, um die GPU frei zu haben).
    ruhe_units: tuple[str, ...] = ()
    dienste: tuple[tuple[str, str], ...] = ()


@dataclass
class AusspracheConfig:
    """Aussprache-Liste fuer Piper-Stimmen (services/aussprache.py).

    Default enabled=False. Die Liste ist sprachgebunden (``sprache``): der
    Grundstock liegt als ``data/aussprache/<sprache>_*.tsv`` im Repo, eigene
    und neu erzeugte Eintraege im Workspace. Das LLM fuer den
    Ergaenzungslauf (tools/aussprache_ergaenzen.py) ist irgendein
    OpenAI-kompatibler Endpunkt; ohne ``llm_url`` wird nur gesammelt.
    """
    enabled: bool = False
    sprache: str = "de"
    llm_url: str = ""
    llm_model: str = ""
    llm_api_key: str = ""
    # Ab so vielen gesammelten Faellen laeuft ein Ergaenzungslauf.
    min_faelle: int = 50


@dataclass
class BargeInConfig:
    """Abbruch mitten im Turn ("Stopp Gaston"), optional pro Profil.

    Ohne den Profil-Block `barge_in:` verhaelt sich ein Profil exakt wie vor
    dem Einbau (enabled=False). Das Fenster ist STATE_WAITING: Bestaetigung
    ("Ich habe verstanden: ..."), Denk-Phrasen und das Vorlesen der Antwort.

    wakewords: leer = dieselben Bundles wie im `wakewords:`-Block des Profils.
        Ein eigenes Bundle (z.B. ein nachtrainiertes "stopp_gaston") wird hier
        eingetragen und ist dann eine reine Config-Aenderung — der Code kennt
        keinen Bundle-Namen.
    rms_min: None = wake_rms_min des Profils uebernehmen. Eigener Wert nur mit
        eigener Messung (tools/bargein_echo_test.py), denn das Fenster liegt
        NEBEN der eigenen Wiedergabe und hat damit eine andere Grundlast als
        das Wakeword-Gate im Leerlauf.
    ack: kurze Quittung nach einem Abbruch. Leer = stumm (nur LED).
    notify_brain: nach dem Abbruch eine Systemnachricht in dieselbe Session
        posten, damit der Brain den Abbruch im Verlauf sieht und beim naechsten
        Turn nicht weiterarbeitet. Kostet einen zusaetzlichen Turn.
    """
    enabled: bool = False
    wakewords: list = field(default_factory=list)
    rms_min: float | None = None
    ack: str = "Okay."
    notify_brain: bool = True
    # Kurzes Signal im Moment des Abbruchs (fallender Doppelton + rote LED).
    #
    # Es quittiert etwas anderes als `ack`: der Beep sagt "ich habe mitten im
    # Satz aufgehoert und hoere jetzt zu" und kommt SOFORT, ohne auf die STT zu
    # warten. `ack` bestaetigt hinterher den verstandenen Abbruch. Am
    # 2026-09-20 fehlte der Beep und der Abbruch war 18 Sekunden lang an
    # nichts zu erkennen — ausser daran, dass die Stimme aufhoerte.
    beep: bool = True
    # Auch waehrend der EIGENEN Ansage lauschen (Bestaetigung, Denk-Phrasen,
    # Vorlesen) — oder nur in den Luecken dazwischen.
    #
    # Default False, und das ist gemessen, nicht vorsichtig geraten
    # (tools/bargein_echo_test.py digital, 2026-09-20): das gaston-Modell
    # erkennt Gastons EIGENE Stimme. Der Satz "Ich habe verstanden: Gastau,
    # Wohnzimmerrollo auf siebzig Prozent" ergab zwei Frames mit Peak 0.975 und
    # haette den Turn selbst abgebrochen. Der Grund ist strukturell: das Modell
    # ist auf synthetischen thorsten-Stimmen trainiert, und genau damit spricht
    # der Assistent (speaches_tts_voice: de_DE-thorsten-medium) — die eigene
    # Stimme liegt in der Trainingsverteilung. Dazu kommt, dass die
    # Bestaetigung das Transkript wiederholt, in dem wegen des Pre-Rolls sehr
    # oft das Wakewort steht.
    #
    # Mit False ist Selbst-Abbruch strukturell unmoeglich (es wird nur gehoert,
    # wenn nichts gesprochen wird) und der Abbruch greift trotzdem beim Denken
    # und Warten — also im Fall, der wirklich weh tut: der Brain arbeitet
    # Sekunden bis Minuten. True gehoert erst zusammen mit einem eigenen
    # Bundle ("stopp_gaston") oder nach einem akustischen Lauf, der zeigt,
    # dass die Echo-Unterdrueckung das wegnimmt.
    while_speaking: bool = False


@dataclass
class WakewordConfig:
    """Ein aktives Wakeword + sein Routing-Ziel (Multi-Wakeword, Meilenstein 1
    der Wakeword-Studio-Spec, siehe Wakeword_Studio_Spec.md Teil 2).

    bundle: Name eines Bundle-Verzeichnisses unter models/wakewords/<bundle>/
        ODER ein eingebauter openwakeword-Modellname ('hey_jarvis', 'alexa', …).
        Auflösung passiert in wakeword/openwakeword_engine.py.
    threshold: None = aus manifest.yaml (oder Default 0.5) ableiten.
    """
    bundle: str
    session: str = ""
    ack: str = ""
    tts_voice: str = ""
    threshold: float | None = None
    # None = aus manifest.yaml (oder Default 3) ableiten. Siehe WakewordHit.min_hits.
    min_hits: int | None = None
    # None = aus manifest.yaml (oder Default 0.0 = aus) ableiten.
    # Siehe WakewordHit.min_peak.
    min_peak: float | None = None
    # None = aus manifest.yaml (oder Fallback auf min_peak) ableiten.
    # Siehe WakewordHit.min_peak_short.
    min_peak_short: float | None = None
    # None = aus manifest.yaml (oder Default 0.0 = aus) ableiten.
    # Siehe WakewordHit.min_peak_single.
    min_peak_single: float | None = None


@dataclass
class Profile:
    """Gebündelte Profil-Konfiguration, nach Sachgebiet gruppiert."""
    name: str

    # mode = "local" (ALSA + openwakeword) oder "respeaker" (ESPHome Stream)
    mode: str = "local"

    local_audio: LocalAudio = field(default_factory=LocalAudio)
    respeaker: RespeakerAudio = field(default_factory=RespeakerAudio)
    leds: LedsConfig = field(default_factory=LedsConfig)

    # Speaches
    speaches_base: str = ""
    speaches_stt_model: str = ""
    # STT im eigenen Prozess (onnx-asr, CPU) VOR Speaches, z. B.
    # "istupakov/parakeet-tdt-0.6b-v3-onnx". Leer = aus, Speaches zuerst wie
    # bisher. speaches_stt_model bleibt dann Rückfall und das Modell der
    # Messwerkzeuge. Siehe services/stt.OnnxAsrStt.
    stt_onnx_model: str = ""
    stt_onnx_threads: int = 8
    # STT über einen llama-server mit Audio-Modell (Qwen3-ASR), VOR onnx-asr
    # und Speaches. Leer = aus. Mit stt_llamacpp_kontext bekommt das Modell
    # Wakewort + Gerätenamen aus /capabilities mit (gemessen deutlich besser
    # bei Gerätenamen). Siehe services/stt.LlamaCppAsrStt.
    stt_llamacpp_url: str = ""
    stt_llamacpp_kontext: bool = True
    # Sprechererkennung per Stimm-Fingerabdruck auf der CPU statt Diarization
    # über Speaches (services/sprecher_verifikation.py). Default aus. Schwelle
    # und Abstand nur gegen tools/sprecher_verifikation_test.py ändern.
    sprecher_verifikation: bool = False
    sprecher_schwelle: float = 0.40
    sprecher_abstand: float = 0.15
    speaches_tts_model: str = ""
    speaches_tts_voice: str = ""

    # OpenClaw
    openclaw_token: str = ""
    openclaw_session: str = ""
    # Streaming-Antwort (/v1/responses mit stream=true): Sätze werden gesprochen,
    # sobald sie generiert sind. Bei Fehler automatischer Fallback auf non-streaming.
    openclaw_stream: bool = True
    # Hinweis an den Brain, wenn im Erst-Turn das Wakewort im Transkript fehlt
    # und der Sprecher nicht bekannt ist (voice_assistant/anrede.py). Der Text
    # steht in locale.anrede_hinweis. Default aus.
    anrede_hinweis: bool = False

    # Telegram
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    # TTS
    tts_prefix: str = ""

    # VAD-Empfindlichkeit — per Profil überschreibbar
    # vad_aggressiveness: 0 (least) … 3 (most aggressive noise rejection)
    vad_aggressiveness: int = 3
    # RMS-Mindestschwelle für Sprachdetektion; 0 = deaktiviert.
    # Chunks unter diesem Pegel zählen nie als Sprache, auch wenn VAD True sagt.
    # Nützlich in Lärm-Umgebungen (Fablab): Hintergrundrauschen-RMS messen,
    # dann Schwelle knapp darüber setzen (z.B. 900 wenn Rauschen ca. 724 RMS).
    vad_voice_rms_min: float = 0.0
    # Pegel-Gate fürs WAKEWORD (nicht VAD!). 0 = deaktiviert. RMS des lautesten
    # 300-ms-Fensters im wake_ring zum Trigger-Zeitpunkt; unterschreitet der
    # Pegel diese Schwelle, feuert der Trigger NICHT. Blockt leise Fehltrigger
    # (Fernseher, Tastatur, ferne Gespräche), die am Score-Gate vorbeikommen.
    # BEWUSST ein eigener Parameter, nicht vad_voice_rms_min wiederverwendet —
    # der ist schon fürs VAD/Endpointing in Gebrauch (assistant.py:_chunk_speech_stats,
    # recorder.py). Absolute RMS-Werte sind gain-abhängig (ReSpeaker ×4); ändert
    # sich Hardware/Gain, verschiebt sich die Skala. Änderungen an dieser
    # Schwelle NUR gegen tools/wake_rms_replay.py (siehe WAKEWORD_PROCESS.md).
    # Messreihe und Begründung 300: Docstring von tools/wake_rms_replay.py.
    wake_rms_min: float = 0.0
    # Schatten-Aufnahme nach jedem Near-Miss (voice_assistant/nearmiss_shadow.py):
    # 6 s mitschneiden, STT + Torfrage NUR ins Log, nie ausgefuehrt. Default aus.
    nearmiss_shadow: bool = False
    # Endpointing: Stille-Dauer (Sekunden) bis die Aufnahme beendet wird.
    # ZEITBASIERT — gilt identisch auf allen Profilen, egal wie lang ein
    # Audio-Chunk je nach Quelle real ist (ALSA-16k=80ms, ALSA-48k-resample≈27ms,
    # ReSpeaker=40ms). Der frühere chunk-basierte Wert war je Profil 0,67–2,0 s.
    silence_seconds: float = 2.0
    # Legacy-Override in *Chunks*. > 0 schlägt silence_seconds; nur für
    # Rückwärtskompatibilität. Bevorzugt silence_seconds setzen.
    silence_chunks_limit: int = 0
    # Endpointing im KOMMANDO-Modus: greift, sobald ein Turn als Ein-Satz
    # eingestuft wurde (der Nutzer spricht durch, ohne das "Ja?" abzuwarten).
    # Solche Turns sind fast immer kurze Schaltbefehle für den Aktuator — da
    # zählt Tempo, und lange Denkpausen kommen nicht vor. Gemessen über 59
    # störungsfreie Turns im Archiv (dur ≤ 6 s): Sprechpause im Satz p90 0,44 s
    # / max 1,68 s, Netto-Sprechzeit max 3,88 s. 1,0 s Nachlauf hätte davon 2
    # Turns zu früh geschnitten, 8 s Deckel keinen einzigen (1,5 s Pre-Roll +
    # 3,9 s Sprechzeit + 1,0 s Nachlauf ≈ 6,4 s).
    # Der Dialog-Modus (Nutzer hat das "Ja?" abgewartet) behält
    # silence_seconds / RECORDING_MAX_SEC — dort sind lange Sätze normal.
    command_silence_seconds: float = 1.0
    command_max_seconds: float = 8.0

    # Locale
    locale: LocaleConfig = field(default_factory=LocaleConfig)

    # Wakewords — fehlt der Block in config.yaml: ein Eintrag 'hey_jarvis'
    # mit Profil-Defaults (Rückwärtskompatibilität, siehe _parse_wakewords).
    wakewords: list = field(default_factory=list)

    # Voice-Aktuator v1 — fehlt der Block: enabled=False, Profil verhält sich
    # wie bisher (siehe ActuatorConfig).
    actuator: ActuatorConfig = field(default_factory=ActuatorConfig)

    # Überwacher Stufe 1 — fehlt der Block: kein Watcher-Thread.
    watcher: WatcherConfig = field(default_factory=WatcherConfig)

    # Aussprache-Liste — fehlt der Block: Piper spricht wie bisher.
    aussprache: AusspracheConfig = field(default_factory=AusspracheConfig)

    # Dienst-Waechter — fehlt der Block: kein Waechter-Thread.
    dienstwaechter: DienstWaechterConfig = field(default_factory=DienstWaechterConfig)

    # Rückspul-Puffer — fehlt der Block: nichts wird gepuffert.
    rewind: RewindConfig = field(default_factory=RewindConfig)

    # Abbruch mitten im Turn ("Stopp Gaston"). Default: aus.
    barge_in: BargeInConfig = field(default_factory=BargeInConfig)


def _load_yaml() -> dict[str, Any]:
    try:
        import yaml  # type: ignore[import-not-found]
    except ImportError:
        print("❌  PyYAML not installed: pip install pyyaml")
        sys.exit(1)
    if not os.path.exists(CONFIG_PATH):
        print(f"❌  config.yaml not found: {CONFIG_PATH}")
        print("    Copy config.example.yaml to config.yaml and fill in your values.")
        sys.exit(1)
    with open(CONFIG_PATH, "r") as f:
        return yaml.safe_load(f) or {}


def _detect_profile_name(cfg: dict[str, Any]) -> str:
    profiles = cfg.get("profiles", {})
    hostname_map = cfg.get("hostname_map", {})

    env = os.environ.get("GASTON_PROFILE", "").strip().lower()
    if env and env in profiles:
        return env

    hostname = socket.gethostname().lower()
    for key, profile in hostname_map.items():
        if key in hostname:
            return profile

    fallback = next(iter(profiles), None)
    if fallback:
        print(f"⚠️  No profile for hostname '{hostname}' → using '{fallback}'")
        return fallback

    print("❌  No profiles defined in config.yaml.")
    sys.exit(1)


def _parse_wakeword_entry(
    entry: dict[str, Any] | None,
    openclaw_session: str,
    speaches_tts_voice: str,
    wakeword_ack: str,
) -> WakewordConfig | None:
    """Ein Eintrag aus einem `wakewords:`-artigen Block → WakewordConfig.

    Eigene Funktion, weil der Barge-in-Block (`barge_in.wakewords:`) dasselbe
    Schema benutzt — ein zweiter Parser wuerde beim naechsten neuen Feld
    auseinanderlaufen.
    """
    entry = entry or {}
    bundle = str(entry.get("bundle", "")).strip()
    if not bundle:
        print("⚠️  wakewords-Eintrag ohne 'bundle' übersprungen")
        return None
    threshold_raw = entry.get("threshold")
    min_hits_raw = entry.get("min_hits")
    min_peak_raw = entry.get("min_peak")
    min_peak_short_raw = entry.get("min_peak_short")
    min_peak_single_raw = entry.get("min_peak_single")
    return WakewordConfig(
        bundle=bundle,
        session=str(entry.get("session") or openclaw_session),
        ack=str(entry.get("ack") or wakeword_ack),
        tts_voice=str(entry.get("tts_voice") or speaches_tts_voice),
        threshold=float(threshold_raw) if threshold_raw is not None else None,
        min_hits=int(min_hits_raw) if min_hits_raw is not None else None,
        min_peak=float(min_peak_raw) if min_peak_raw is not None else None,
        min_peak_short=float(min_peak_short_raw) if min_peak_short_raw is not None else None,
        min_peak_single=float(min_peak_single_raw) if min_peak_single_raw is not None else None,
    )


def _parse_wakewords(
    raw: dict[str, Any], openclaw_session: str, speaches_tts_voice: str, wakeword_ack: str
) -> list[WakewordConfig]:
    """Baut die Liste aktiver Wakewords aus dem optionalen `wakewords:`-Block.

    Fehlt der Block: ein Eintrag 'hey_jarvis' mit den Profil-Defaults — exakt
    das bisherige Verhalten (Rückwärtskompatibilität wie beim alten flachen
    YAML-Schema).
    """
    entries_raw = raw.get("wakewords")
    if not entries_raw:
        return [
            WakewordConfig(
                bundle="hey_jarvis",
                session=openclaw_session,
                ack=wakeword_ack,
                tts_voice=speaches_tts_voice,
            )
        ]

    result: list[WakewordConfig] = []
    for entry in entries_raw:
        wc = _parse_wakeword_entry(entry, openclaw_session, speaches_tts_voice, wakeword_ack)
        if wc is not None:
            result.append(wc)
    if not result:
        print("⚠️  wakewords-Block leer/ungültig → Fallback auf 'hey_jarvis'")
        return [
            WakewordConfig(
                bundle="hey_jarvis",
                session=openclaw_session,
                ack=wakeword_ack,
                tts_voice=speaches_tts_voice,
            )
        ]
    return result


def _parse_barge_in(
    raw: dict[str, Any],
    profile_wakewords: list[WakewordConfig],
    wake_rms_min: float,
    openclaw_session: str,
    speaches_tts_voice: str,
    wakeword_ack: str,
) -> BargeInConfig:
    """Baut den optionalen `barge_in:`-Block. Fehlt er, ist Barge-in aus.

    Die Wakeword-Liste faellt bewusst auf die Profil-Wakewords zurueck: Stufe 1
    des Abbruchs benutzt dasselbe, bereits gemessene Modell ("Stopp Gaston" —
    das "Stopp" steckt im Pre-Roll und wird per STT geprueft). Ein eigenes,
    nachtrainiertes Bundle ist spaeter nur ein Eintrag hier.
    """
    raw_bi = raw.get("barge_in") or {}
    _d = BargeInConfig()
    if not raw_bi:
        return _d

    entries_raw = raw_bi.get("wakewords") or []
    wakewords: list[WakewordConfig] = []
    for entry in entries_raw:
        wc = _parse_wakeword_entry(entry, openclaw_session, speaches_tts_voice, wakeword_ack)
        if wc is not None:
            wakewords.append(wc)
    if not wakewords:
        wakewords = list(profile_wakewords)

    rms_raw = raw_bi.get("rms_min")
    ack_raw = raw_bi.get("ack")
    return BargeInConfig(
        enabled=bool(raw_bi.get("enabled", _d.enabled)),
        wakewords=wakewords,
        rms_min=float(rms_raw) if rms_raw is not None else wake_rms_min,
        ack=_d.ack if ack_raw is None else str(ack_raw),
        notify_brain=bool(raw_bi.get("notify_brain", _d.notify_brain)),
        while_speaking=bool(raw_bi.get("while_speaking", _d.while_speaking)),
        beep=bool(raw_bi.get("beep", _d.beep)),
    )


def _parse_profile(name: str, raw: dict[str, Any]) -> Profile:
    """Baut aus dem rohen Profil-Dict ein Profile-Objekt.

    Unterstützt sowohl das *alte* flache Schema (device_index, playback_device, …
    direkt auf Profil-Ebene) als auch das neue geschachtelte Schema (mit
    `mode`, `local_audio`, `respeaker`, `leds`).
    """
    mode = str(raw.get("mode", "local")).lower()
    if mode not in ("local", "respeaker"):
        print(f"⚠️  Unknown mode '{mode}' in profile '{name}' → using 'local'")
        mode = "local"

    # --- Local-Audio: neues Schema hat Vorrang, altes ist Fallback ---
    local_raw = raw.get("local_audio") or raw.get("alsa") or {}
    local_audio = LocalAudio(
        device_index=int(local_raw.get("device_index", raw.get("device_index", 0))),
        playback_device=local_raw.get("playback_device", raw.get("playback_device")),
        rate_in=int(local_raw.get("rate_in", raw.get("rate_in", 16000))),
        resample=bool(local_raw.get("resample", raw.get("resample", False))),
    )

    # --- Respeaker ---
    resp_raw = raw.get("respeaker") or {}
    respeaker = RespeakerAudio(
        host=str(resp_raw.get("host", "")),
        port=int(resp_raw.get("port", 6053)),
        encryption_key=str(resp_raw.get("encryption_key", "")),
        use_speaker=bool(resp_raw.get("use_speaker", True)),
        volume=float(resp_raw.get("volume", 0.8)),
        mitschnitt=bool(resp_raw.get("mitschnitt", False)),
        kanal2_quelle=str(resp_raw.get("kanal2_quelle", "") or ""),
    )

    # --- LEDs: neues Schema + Rückwärtskompatibilität für wled_host ---
    leds_raw = raw.get("leds") or {}
    wled_raw = leds_raw.get("wled") or {}
    ring_raw = leds_raw.get("respeaker_ring") or {}
    wled_host = wled_raw.get("host") or raw.get("wled_host") or "wled.local"
    leds = LedsConfig(
        wled_enabled=bool(wled_raw.get("enabled", True)),
        wled_host=str(wled_host),
        respeaker_ring_enabled=bool(ring_raw.get("enabled", False)),
    )

    # --- Aktuator: neues Schema, analog zum leds-Block ---
    actuator_raw = raw.get("actuator") or {}
    _dact = ActuatorConfig()
    actuator = ActuatorConfig(
        enabled=bool(actuator_raw.get("enabled", _dact.enabled)),
        base_url=str(actuator_raw.get("base_url", _dact.base_url)),
        token_file=str(actuator_raw.get("token_file") or os.path.join(PROJECT_DIR, "voiceact-token.txt")),
        llm_url=str(actuator_raw.get("llm_url", _dact.llm_url)),
        llm_timeout=float(actuator_raw.get("llm_timeout", _dact.llm_timeout)),
        intent_timeout=float(actuator_raw.get("intent_timeout", _dact.intent_timeout)),
        mqtt_host=str(actuator_raw.get("mqtt_host", _dact.mqtt_host)),
        mqtt_port=int(actuator_raw.get("mqtt_port", _dact.mqtt_port)),
        refresh_poll_sec=int(actuator_raw.get("refresh_poll_sec", _dact.refresh_poll_sec)),
        system_prompt=str(actuator_raw.get("system_prompt") or _dact.system_prompt),
        gruppen_regel=str(actuator_raw.get("gruppen_regel") or _dact.gruppen_regel),
        beispiel_saetze=dict(actuator_raw.get("beispiel_saetze") or _dact.beispiel_saetze),
        beispiel_typen=list(actuator_raw.get("beispiel_typen") or _dact.beispiel_typen),
        tor_enabled=bool(actuator_raw.get("tor_enabled", _dact.tor_enabled)),
        tor_prompt=str(actuator_raw.get("tor_prompt") or _dact.tor_prompt),
        tor_gruppen_regel=str(actuator_raw.get("tor_gruppen_regel") or _dact.tor_gruppen_regel),
        # schatten_* sind die Namen vom 2026-09-29 (da war Laya nur Schatten)
        laya_url=str(actuator_raw.get("laya_url") or actuator_raw.get("schatten_url") or _dact.laya_url),
        laya_timeout=float(actuator_raw.get("laya_timeout", _dact.laya_timeout)),
        laya_schwelle=float(actuator_raw.get("laya_schwelle",
                                             actuator_raw.get("schatten_schwelle", _dact.laya_schwelle))),
        laya_rueckfrage=bool(actuator_raw.get("laya_rueckfrage", _dact.laya_rueckfrage)),
        klassifikator=str(actuator_raw.get("klassifikator") or _dact.klassifikator),
        schatten=bool(actuator_raw.get("schatten", _dact.schatten)),
    )
    if actuator.klassifikator not in ("gemma", "laya"):
        print(f"⚠️  actuator.klassifikator '{actuator.klassifikator}' unbekannt — Gemma entscheidet")
        actuator.klassifikator = "gemma"
    if actuator.klassifikator == "laya" and not actuator.laya_url:
        print("⚠️  actuator.klassifikator laya, aber laya_url leer — Gemma entscheidet")
        actuator.klassifikator = "gemma"

    # --- Überwacher: separater Block, analog zu actuator ---
    watcher_raw = raw.get("watcher") or {}
    _dw = WatcherConfig()
    watcher = WatcherConfig(
        enabled=bool(watcher_raw.get("enabled", _dw.enabled)),
        chat_id=str(watcher_raw.get("chat_id", _dw.chat_id)),
        # bot_token leer = Profil-Telegram-Token wird genutzt
        bot_token=str(watcher_raw.get("bot_token", _dw.bot_token)),
        poll_interval=int(watcher_raw.get("poll_interval", _dw.poll_interval)),
        quiet_start=int(watcher_raw.get("quiet_start", _dw.quiet_start)),
        quiet_end=int(watcher_raw.get("quiet_end", _dw.quiet_end)),
        llm_url=str(watcher_raw.get("llm_url", _dw.llm_url)),
        llm_model=str(watcher_raw.get("llm_model", _dw.llm_model)),
        llm_api_key=str(watcher_raw.get("llm_api_key", _dw.llm_api_key)),
        llm_timeout=float(watcher_raw.get("llm_timeout", _dw.llm_timeout)),
        haus_mcp_url=str(watcher_raw.get("haus_mcp_url", _dw.haus_mcp_url)),
        haus_mcp_token=str(watcher_raw.get("haus_mcp_token", _dw.haus_mcp_token)),
    )

    dw_raw = raw.get("dienstwaechter") or {}
    _ddw = DienstWaechterConfig()
    dienstwaechter = DienstWaechterConfig(
        enabled=bool(dw_raw.get("enabled", _ddw.enabled)),
        intervall=max(10.0, float(dw_raw.get("intervall", _ddw.intervall))),
        gnadenfrist=float(dw_raw.get("gnadenfrist", _ddw.gnadenfrist)),
        heilen=bool(dw_raw.get("heilen", _ddw.heilen)),
        max_heilversuche=int(dw_raw.get("max_heilversuche", _ddw.max_heilversuche)),
        heil_abstand=float(dw_raw.get("heil_abstand", _ddw.heil_abstand)),
        ruhe_units=tuple(str(u) for u in dw_raw.get("ruhe_units") or ()),
        dienste=tuple((str(d["name"]), str(d["url"])) for d in dw_raw.get("dienste") or ()),
    )

    as_raw = raw.get("aussprache") or {}
    _das = AusspracheConfig()
    aussprache = AusspracheConfig(
        enabled=bool(as_raw.get("enabled", _das.enabled)),
        sprache=str(as_raw.get("sprache", _das.sprache)),
        llm_url=str(as_raw.get("llm_url", _das.llm_url)),
        llm_model=str(as_raw.get("llm_model", _das.llm_model)),
        llm_api_key=str(as_raw.get("llm_api_key", _das.llm_api_key)),
        min_faelle=int(as_raw.get("min_faelle", _das.min_faelle)),
    )

    rewind_raw = raw.get("rewind") or {}
    _drw = RewindConfig()
    rewind = RewindConfig(
        enabled=bool(rewind_raw.get("enabled", _drw.enabled)),
        seconds=float(rewind_raw.get("seconds", _drw.seconds)),
        before_trigger_seconds=float(
            rewind_raw.get("before_trigger_seconds", _drw.before_trigger_seconds)
        ),
        marker_mqtt_host=str(rewind_raw.get("marker_mqtt_host") or _drw.marker_mqtt_host),
        marker_mqtt_port=int(rewind_raw.get("marker_mqtt_port", _drw.marker_mqtt_port)),
        marker_topic=str(rewind_raw.get("marker_topic") or _drw.marker_topic),
    )

    locale_raw = raw.get("locale") or {}
    _dloc = LocaleConfig()
    locale = LocaleConfig(
        wakeword_ack=str(locale_raw.get("wakeword_ack", _dloc.wakeword_ack)),
        confirmation_prefix=str(locale_raw.get("confirmation_prefix", _dloc.confirmation_prefix)),
        no_reply_fallback=str(locale_raw.get("no_reply_fallback", _dloc.no_reply_fallback)),
        openclaw_voice_instruction=str(locale_raw.get("openclaw_voice_instruction", _dloc.openclaw_voice_instruction)),
        anrede_hinweis=str(locale_raw.get("anrede_hinweis", _dloc.anrede_hinweis)),
        thinking_phrases=list(locale_raw.get("thinking_phrases", _dloc.thinking_phrases)),
    )

    wakewords = _parse_wakewords(
        raw,
        openclaw_session=str(raw.get("openclaw_session", "")),
        speaches_tts_voice=str(raw.get("speaches_tts_voice", "")),
        wakeword_ack=locale.wakeword_ack,
    )
    barge_in = _parse_barge_in(
        raw,
        profile_wakewords=wakewords,
        wake_rms_min=float(raw.get("wake_rms_min", 0.0)),
        openclaw_session=str(raw.get("openclaw_session", "")),
        speaches_tts_voice=str(raw.get("speaches_tts_voice", "")),
        wakeword_ack=locale.wakeword_ack,
    )

    return Profile(
        name=name,
        mode=mode,
        local_audio=local_audio,
        respeaker=respeaker,
        leds=leds,
        speaches_base=str(raw.get("speaches_base", "")),
        speaches_stt_model=str(raw.get("speaches_stt_model", "")),
        stt_onnx_model=str(raw.get("stt_onnx_model", "") or ""),
        stt_onnx_threads=int(raw.get("stt_onnx_threads", 8)),
        stt_llamacpp_url=str(raw.get("stt_llamacpp_url", "") or ""),
        stt_llamacpp_kontext=bool(raw.get("stt_llamacpp_kontext", True)),
        sprecher_verifikation=bool(raw.get("sprecher_verifikation", False)),
        sprecher_schwelle=float(raw.get("sprecher_schwelle", 0.40)),
        sprecher_abstand=float(raw.get("sprecher_abstand", 0.15)),
        speaches_tts_model=str(raw.get("speaches_tts_model", "")),
        speaches_tts_voice=str(raw.get("speaches_tts_voice", "")),
        openclaw_token=str(raw.get("openclaw_token", "")),
        openclaw_session=str(raw.get("openclaw_session", "")),
        openclaw_stream=bool(raw.get("openclaw_stream", True)),
        anrede_hinweis=bool(raw.get("anrede_hinweis", False)),
        telegram_bot_token=str(raw.get("telegram_bot_token", "")),
        telegram_chat_id=str(raw.get("telegram_chat_id", "")),
        tts_prefix=str(raw.get("tts_prefix", "")),
        vad_aggressiveness=int(raw.get("vad_aggressiveness", 3)),
        vad_voice_rms_min=float(raw.get("vad_voice_rms_min", 0.0)),
        wake_rms_min=float(raw.get("wake_rms_min", 0.0)),
        nearmiss_shadow=bool(raw.get("nearmiss_shadow", False)),
        silence_seconds=float(raw.get("silence_seconds", 2.0)),
        silence_chunks_limit=int(raw.get("silence_chunks_limit", 0)),
        command_silence_seconds=float(raw.get("command_silence_seconds", 1.0)),
        command_max_seconds=float(raw.get("command_max_seconds", 8.0)),
        locale=locale,
        actuator=actuator,
        watcher=watcher,
        dienstwaechter=dienstwaechter,
        aussprache=aussprache,
        rewind=rewind,
        wakewords=wakewords,
        barge_in=barge_in,
    )


def load_profile() -> Profile:
    cfg = _load_yaml()
    name = _detect_profile_name(cfg)
    raw = cfg["profiles"][name]
    profile = _parse_profile(name, raw)
    print(f"🖥️  Profile: {name} (hostname: {socket.gethostname()}, mode: {profile.mode})")
    return profile


# --- Konstanten, die profil-unabhängig sind ---
_HOME = os.path.expanduser("~")
WORKSPACE = os.path.join(_HOME, ".openclaw", "workspace")
PIPER_MODEL_EMO = os.path.join(_HOME, "openclaw_voice_assist", "models", "piper", "de_DE-thorsten_emotional-medium.onnx")
PIPER_MODEL = os.path.join(_HOME, "openclaw_voice_assist", "models", "piper", "de_DE-thorsten-low.onnx")
PIPER_OUT = os.path.join(WORKSPACE, "ja.wav")
WHISPER_MODEL = "small"
WHISPER_LANGUAGE = "de"

OPENCLAW_RESPONSES_URL = "http://127.0.0.1:18789/v1/responses"
OPENCLAW_TIMEOUT = 300
# SSE-Stream: während langer Tool-Phasen kommen minutenlang keine Bytes —
# der Read-Timeout muss den kompletten Agentic-Loop überleben, sonst reißt
# die Verbindung kurz vor der fertigen Antwort ab (Vorfall 2026-07-02).
OPENCLAW_STREAM_TIMEOUT = 600
# Hauptschleife wartet maximal so lange auf reply_done_event; deckt den
# Stream-Timeout plus Rest ab. Verspätete Antworten werden trotzdem noch
# gesprochen (Worker-Thread läuft weiter, LED-Reset übernimmt der Worker).
OPENCLAW_OVERALL_TIMEOUT = OPENCLAW_STREAM_TIMEOUT + 60

# Aus der (gitignorierten) config.yaml statt als Konstante im Repo — sonst
# stünde eine echte interne Adresse öffentlich. Leer = Stimmungsanalyse aus.
VOICE_ANALYSIS_BASE = str(_load_yaml().get("voice_analysis_base", ""))

SPEACHES_TIMEOUT = 15
SPEACHES_RETRY_COOLDOWN = 60

# Wakeword-Bundles (Wakeword-Studio, Teil 1 der Spec): models/wakewords/<name>/
# mit manifest.yaml + .tflite. Existiert kein Bundle-Verzeichnis für einen
# konfigurierten Namen, wird er als eingebauter openwakeword-Modellname
# durchgereicht (z.B. 'hey_jarvis', 'alexa').
WAKEWORDS_DIR = os.path.join(PROJECT_DIR, "models", "wakewords")

# Audio-Parameter (Wakeword läuft immer auf 16 kHz mono int16)
RATE_OW = 16000
CHUNK_SIZE = 1280
CHANNELS = 1
VAD_FRAME_SIZE = int(RATE_OW * 20 / 1000)
SILENCE_CHUNKS_LIMIT = 25
MIN_SPEECH_CHUNKS = 4

MAX_FOLLOWUP_ROUNDS = 3
# Wie oft der Aktuator bei VERDICT_UNKLAR nachfragt, bevor er aufgibt. Eins:
# eine zweite Rückfrage zum selben verhörten Wort bringt nichts, die STT hört
# es wieder gleich falsch — sie nervt nur.
MAX_UNKLAR_ROUNDS = 1
FOLLOWUP_BEEP_PATH = os.path.join(WORKSPACE, "followup_beep.wav")
# Abbruch-Signal (Barge-in): zwei kurze FALLENDE Toene, damit es sich vom
# steigenden Follow-up-Beep hoerbar unterscheidet — "ich habe aufgehoert"
# gegen "ich hoere jetzt zu".
ABORT_BEEP_PATH = os.path.join(WORKSPACE, "abort_beep.wav")
LAST_REPLY_WAV = os.path.join(WORKSPACE, "last_reply.wav")
LAST_REPLY_TXT = os.path.join(WORKSPACE, "last_reply.txt")

# Endpointing-Telemetrie: eine JSONL-Zeile pro Aufnahme/Follow-up zum
# empirischen Tunen von silence_seconds (Pausen-Verhalten je Sprecher).
ENDPOINT_LOG_PATH = os.path.join(WORKSPACE, "endpoint.log")
# Eine JSONL-Zeile je Wakeword-Entscheidung (Trigger + Near-Miss) — Datenbasis
# fürs Sweepen der Gate-Parameter und fürs Wakeword-Nachtraining.
WAKE_LOG_PATH = os.path.join(WORKSPACE, "wake_events.log")
# Spiegel-Kanal des Aktuators: eine JSONL-Zeile je Schalt-Turn, den der
# Aktuator selbst erledigt hat (der Brain sieht diese Turns nicht). Bewusst
# ein EIGENER lokaler Log statt Haus-Session oder Telegram — Schaltvorgänge
# sollen weder die Gesprächs-Session zumüllen noch im Chat auftauchen.
# Das ist das Rohmaterial für den späteren Aktuator-Überwacher.
ACTUATOR_LOG_PATH = os.path.join(WORKSPACE, "actuator_turns.log")
# Jede Entscheidung der Torfrage (ja UND nein) — Rohmaterial zum Nachtunen.
# Bewusst NICHT in actuator_turns.log: der Überwacher meldet dort jede Zeile,
# deren Status nicht "ausgefuehrt" ist, und die Nein-Zeilen sind die Mehrheit.
ACTUATOR_TOR_LOG_PATH = os.path.join(WORKSPACE, "actuator_tor.log")
# Schattenbetrieb (actuator.schatten_url): je Satz die echte Entscheidung und
# die des Schatten-Klassifikators nebeneinander. Auswertung:
# tools/aktuator_vergleich.py --schatten
ACTUATOR_SCHATTEN_LOG_PATH = os.path.join(WORKSPACE, "actuator_schatten.log")

# Aufnahme-Hard-Cap (Silence-Detection beendet normal früher).
# 30 s erlaubt einen längeren Enrolment-Satz: "lerne meine Stimme, ich bin Jochen,
# und erzähle dir jetzt eine kleine Geschichte ..."
RECORDING_MAX_SEC = 30.0

# Voice-Workspace: Live-Aufnahme + Sprecher-Referenzen
VOICE_DIR = os.path.join(WORKSPACE, "voice")
LAST_RECORDING_PATH = os.path.join(VOICE_DIR, "last_recording.wav")
# Trigger-Audio-Archiv: pro Wakeword-Trigger der Mic-Mitschnitt rund ums
# Wakeword (Ringpuffer, ~3 s vor Trigger) + die anschließende Aufnahme.
# Zweck: False-Positive-Analyse und Retraining mit echten FP-Clips als
# adversarial negatives (Wakeword-Studio auf ai-stack) — die starken FPs
# (Peaks 0.94-0.98, Logs 2026-07-08..13) sind score-seitig nicht filterbar.
TRIGGER_AUDIO_DIR = os.path.join(VOICE_DIR, "triggers")
TRIGGER_AUDIO_MAX_AGE_DAYS = 30
# Dauerhafter Korpus: das Archiv oben löscht sich nach 30 Tagen selbst, die
# LABELS dazu (wake_review.jsonl, per Ohr gefällt) leben unbegrenzt weiter.
# Am 2026-08-22 hat ein Service-Neustart 56 Dateien aufgeräumt, darunter das
# Audio zu 6 Ohr-Urteilen — die Messbasis von tools/wake_rms_replay.py schrumpfte
# still von 26 auf 20 belegte Fehltrigger. Gelabelte Clips gehören deshalb aus
# dem selbstlöschenden Verzeichnis heraus: tools/wake_corpus.py sichert sie
# hierher, und _cleanup_trigger_audio verschont ungesicherte Ohr-Urteile.
WAKE_CORPUS_DIR = os.path.join(VOICE_DIR, "corpus")
# Ohr-Urteile (tools/review_audio.py). Hier, weil auch der Laufzeit-Cleanup sie
# kennen muss — nicht nur die Werkzeuge.
WAKE_REVIEW_PATH = os.path.join(WORKSPACE, "wake_review.jsonl")
SPEAKERS_DIR = os.path.join(VOICE_DIR, "speakers")
SPEAKER_ORIGINALS_DIR = os.path.join(VOICE_DIR, "originals")
SPEAKER_VOICES_PATH = os.path.join(VOICE_DIR, "speaker_voices.json")

# Wer zuletzt per Stimme gesprochen hat — geschrieben nach JEDEM Voice-Turn,
# gelesen von Werkzeugen AUSSERHALB dieses Repos (siehe SPEAKER_STATE.md), die
# vor einer folgenreichen Aktion wissen müssen, ob gerade ein bekannter Sprecher
# am Mikrofon war. Bewusst eine Datei und kein Prompt-Hinweis: das Sprachmodell
# soll daran nicht vorbeireden können.
CURRENT_SPEAKER_PATH = os.path.join(VOICE_DIR, "current_speaker.json")

# Schwellwert für „lange Pause" (Sekunden): Eine temporäre, besitzergebundene
# Stimme (per voice_set_voice ohne for_speaker gesetzt) bleibt nur erhalten,
# solange derselbe Sprecher innerhalb dieser Zeit weiterspricht. Vergeht mehr
# Zeit, fällt apply_speaker_default auf den Profil-Default (Thorsten) zurück.
VOICE_RESET_PAUSE_SEC = 180

# Lokaler Enrolment-HTTP-Server (von OpenClaw-Tool angesprochen)
ENROLL_SERVER_HOST = "127.0.0.1"
ENROLL_SERVER_PORT = 18791

# Lokaler Speak-HTTP-Server (OpenClaw kann Text vorlesen lassen)
SPEAK_SERVER_HOST = "127.0.0.1"
SPEAK_SERVER_PORT = 18792

DIARIZATION_TIMEOUT = 15
# Maximale Wartezeit auf Diarization-Ergebnis nach STT-Fertigstellung
DIARIZATION_JOIN_TIMEOUT = 2.0
