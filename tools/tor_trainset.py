#!/usr/bin/env python3
"""Trainingsdaten fuer die Torfrage erzeugen: synthetisch aus den Zielen + MASSIVE.

Aufruf (Projekt-venv wird selbst gesucht):
    ow-venv/bin/python -m tools.tor_trainset --massive ~/laya-test/1.1/data/de-DE.jsonl
    ow-venv/bin/python -m tools.tor_trainset --ohne-massive --probe 20

Schreibt testsets/tor_train.jsonl (gitignored wie das Test-Set — die Saetze
tragen die Geraetenamen dieses Hauses). Gleiches Zeilenformat wie
tools/actuator_tor_test.py (satz, schalten, ziel, aktion, wert), dazu
`herkunft`. Nichts davon ist modellspezifisch; die Umrechnung in das Format
eines bestimmten Modells (Laya: Zielverteilung [P(nein), P(ja)]) macht das
Trainingsskript.

Warum synthetisch, und warum aus den Zielen
-------------------------------------------
Echte Saetze gibt es 334, davon 121 Kommandos — zu wenig zum Trainieren UND
Pruefen. Die Ziele aus /capabilities sind aber genau das, worauf das Tor
hoeren soll: jedes Ziel mit seinen `namen`, `aktionen` und seinem
Wertebereich wird zu Befehlen ausmultipliziert. Dieselben Namen liefern die
SCHWEREN Negative — Saetze, die ein Ziel nennen, aber nichts schalten
wollen ("Ist das Kuechenlicht an?", "Das Bürorollo klemmt"). Genau dort
braucht ein Modell, das nur auf Geraetewoerter anspringt, Nachhilfe.

Die zweite Luecke zeigte Laya zero-shot (2026-09-28): es hielt AUFTRAEGE an
den Brain ("trag ... in den Kalender ein", P 0,99) fuer Schaltbefehle.
Dagegen: Auftrags-Negative aus Vorlagen und aus MASSIVE (Amazon, CC BY 4.0,
de-DE, 16.521 Saetze aus Sprachassistenten-Anfragen, nach Absicht
gelabelt). MASSIVE bringt Formulierungsvielfalt, die keine Vorlage hat.

MASSIVE-Zuordnung, bewusst vorsichtig:
    ja         iot_hue_* (Licht), iot_wemo_* (Steckdose)
    weggelassen iot_coffee, iot_cleaning, audio_volume_*, play_radio,
               play_music — fuer dieses Haus mehrdeutig
    nein       alles andere
Die Wakewoerter von MASSIVE ("olly", "siri", "alexa") am Satzanfang werden
durch die eigenen ersetzt.

Was die Zahl NICHT ersetzt
--------------------------
Synthetische Saetze sind sauberer als echte STT: kein "Tyrolo", kein
Fernseher im Hintergrund. Gemessen wird deshalb AUSSCHLIESSLICH auf echten
Saetzen (actuator_tor_test.py); Saetze, die normalisiert auch im Test-Set
stehen, werden hier entfernt (Zahl wird ausgegeben), damit nichts vom
Pruefstoff ins Training rutscht.

Die Vorlagen sind deutsch — wie der Aktuator-Prompt die sprachabhaengige
Stelle. Wer das Tor fuer eine andere Sprache trainiert, ersetzt sie.
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import random
import re
import sys

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_VENV = os.path.join(_REPO, "ow-venv", "bin", "python")
if os.path.exists(_VENV) and os.path.realpath(sys.executable) != os.path.realpath(_VENV):
    os.execv(_VENV, [_VENV, "-m", "tools.tor_trainset", *sys.argv[1:]])

from voice_assistant.config import load_profile  # noqa: E402
from voice_assistant.services.actuator import Actuator  # noqa: E402

_AUS = os.path.join(_REPO, "testsets", "tor_train.jsonl")
_TESTSET = os.path.join(_REPO, "testsets", "actuator_tor.jsonl")

# Anrede am Satzanfang. Seit dem Pre-Roll traegt das Transkript das Wakewort
# mit, oft verhoert — die Formen stammen aus actuator_turns.log/endpoint.log.
# Installationsbezogen, deshalb per --anrede ersetzbar.
_ANREDE = ["Gaston, ", "Gaston ", "Gastau, ", "Gastronom, ", "Gastron ", "Gastro, ",
           "Gastraum, ", "Gestor, ", "Gast auf, ", "Das da, ", "Gasthaum, ", "Gusto, "]

# --- Artikel ----------------------------------------------------------------
# Der Kopf eines Namens entscheidet das Geschlecht: "Küchenrollo links" ->
# rollo, "Licht in der Küche" -> licht. Falsche Artikel waeren kein Drama (STT
# ist auch nicht grammatisch), aber sie sollen nicht die Regel sein.
_GESCHLECHT = [("beleuchtung", "die"), ("heizung", "die"), ("lampe", "die"),
               ("leiste", "die"), ("weiche", "die"), ("rollos", "die"),
               ("lichter", "die"), ("licht", "das"), ("rollo", "das"),
               ("spiegel", "der"), ("stop", "den"), ("ruhe", "die"),
               ("regenwasser", "das"), ("schutz", "der")]


def _artikel(name: str) -> str | None:
    """Artikel im Akkusativ, oder None, wenn der Name schon einen mitbringt."""
    n = name.lower()
    if re.match(r"(alle|die|der|das|den)\b", n) or " seine " in n:
        return None
    # Das letzte Wort mit bekannter Endung ist der Kopf: "Licht in der
    # Küche" -> licht, "Rollos auf der Westseite" -> rollos, "Badspiegel
    # Licht" -> licht.
    art = "das"
    for wort in n.split():
        for endung, a in _GESCHLECHT:
            if wort.endswith(endung):
                art = a
                break
    return "den" if art == "der" else art


def _mit_artikel(name: str, rng: random.Random) -> str:
    art = _artikel(name)
    if art is None or rng.random() < 0.15:     # auch mal ohne ("Küchenlicht an")
        return name
    return f"{art} {name}"


# --- Befehle ------------------------------------------------------------------
# {n} = Name mit Artikel, {w} = Wert. Mehrere Vorlagen je Aktion, damit das
# Modell nicht an einem Verb klebt.
_BEFEHL = {
    "ein": ["Mach {n} an", "Schalte {n} ein", "Schalt {n} an", "{n} an", "{n} einschalten",
            "Mach mal {n} an", "Kannst du {n} einschalten", "Mach bitte {n} an",
            "Schalt doch mal {n} ein", "Ich hätte gern {n} an", "Mach {n} wieder an"],
    "aus": ["Mach {n} aus", "Schalte {n} aus", "Schalt {n} ab", "{n} aus", "{n} ausschalten",
            "Mach mal {n} aus", "Kannst du {n} ausmachen", "Mach bitte {n} aus",
            "Schalt {n} wieder aus", "Mach {n} ab"],
    "auf": ["Mach {n} auf", "{n} hoch", "{n} auf", "Fahr {n} hoch", "Mach {n} bitte ganz auf",
            "Zieh {n} hoch", "Öffne {n}", "Mach {n} wieder auf", "Kannst du {n} hochfahren"],
    "zu": ["Mach {n} zu", "{n} runter", "{n} zu", "Fahr {n} runter", "Mach {n} bitte ganz zu",
           "Lass {n} runter", "Schließ {n}", "Mach {n} wieder zu", "Kannst du {n} runterfahren"],
    "setzen:prozent": ["Mach {n} auf {w} Prozent", "{n} auf {w}%", "Stell {n} auf {w} Prozent",
                       "Fahr {n} auf {w} Prozent", "{n} auf {w} Prozent bitte",
                       "Mach {n} halb zu", "Setz {n} auf {w} Prozent"],
    "setzen:grad": ["Stell {n} auf {w} Grad", "{n} auf {w} Grad", "Mach {n} auf {w} Grad",
                    "Dreh {n} auf {w} Grad", "Setz {n} auf {w} Grad",
                    "Stell {n} bitte auf {w} Grad", "Mach {n} wärmer, {w} Grad"],
    "aktivieren": ["{n} aktivieren", "Aktiviere {n}", "Mach {n} an", "Starte {n}", "{n} bitte"],
    "starten": ["{n}", "Starte {n}", "{n} starten", "{n}, sofort", "Mach {n}"],
}

# Ohne bestimmbares Ziel — laut Label-Regel trotzdem schalten=true (siehe
# actuator_tor_test.py): das Tor soll die Absicht erkennen, das Ziel klaert
# classify/verdict oder die Rueckfrage.
_BEFEHL_OHNE_ZIEL = [
    "Mach das Licht an", "Licht aus", "Rollo zu", "Rollo hoch", "Mach das Rollo auf",
    "Mach die Rollos runter", "Heizung auf 21 Grad", "Mach mal Licht", "Licht an bitte",
    "Rollo auf 70 Prozent", "Mach die Lampe aus", "Schalt das Licht wieder aus",
    "Mach es mal heller hier", "Mach das Licht hier aus", "Rollos hoch", "Dreh die Heizung runter",
    "Mach die Heizung wärmer", "Schalte die Steckdose ein", "Mach den Monitor an",
    "Schalt den Fernseher aus", "Mach die Lampe im Flur an",
]

# --- Schwere Negative: Ziel genannt, aber nichts schalten ---------------------
_NICHT_SCHALTEN = [
    "Ist {n} an?", "Ist {n} noch an?", "Hast du {n} ausgemacht?", "Wer hat {n} angemacht?",
    "Warum ist {n} an?", "{N} ist kaputt.", "{N} geht nicht mehr richtig.",
    "{N} flackert seit gestern.", "Ich habe {n} gerade ausgemacht.",
    "Wann wurde {n} zuletzt geschaltet?", "Wie lange war {n} heute an?",
    "Trag in den Kalender ein, dass {n} repariert werden muss.",
    "Setz eine neue Glühbirne für {n} auf die Einkaufsliste.",
    "Erinnere mich morgen daran, {n} anzuschauen.",
    "Schreib auf die To-do-Liste: {n} reparieren.", "Kannst du mir sagen, wie {n} heißt?",
    "Weißt du, ob {n} vorhin an war?", "Lass {n} so, wie es ist.",
    "{N} bitte nicht anfassen.", "Das mit {n} hat vorhin gut geklappt.",
]
_NICHT_SCHALTEN_ROLLO = [
    "Ist {n} offen?", "Warum ist {n} zu?", "Wie weit ist {n} offen?", "{N} klemmt.",
    "Wann geht {n} normalerweise runter?", "{N} macht komische Geräusche.",
    "Hat der Wind {n} runtergefahren?",
]
_NICHT_SCHALTEN_HEIZUNG = [
    "Wie warm ist {n} eingestellt?", "Auf wie viel Grad steht {n}?",
    "Läuft {n} gerade?", "{N} ist ganz kalt.", "Wie viel Strom braucht {n}?",
]

# --- Auftraege an den Brain, Geplauder ----------------------------------------
_AUFTRAG = [
    "Trag für {tag} {essen} in die Essensliste ein.", "Setz {ding} auf die Einkaufsliste.",
    "Trag mir einen Termin am {tag} um {uhr} Uhr ein.", "Erinnere mich {tag} an {ding}.",
    "Stell einen Timer auf {min} Minuten.", "Wie wird das Wetter {tag}?",
    "Was hatten wir {tag} zu essen?", "Mach mal einen Essensvorschlag für {tag}.",
    "Such in den Rezepten nach {essen}.", "Wer hat zuletzt angerufen?",
    "Zeig mir das Rezept für {essen} auf dem Tablet.", "Zeig die Urlaubsplanung auf dem Monitor.",
    "Die Petra muss {tag} Abend arbeiten, trag das bitte ein.",
    "Setz mir auf die To-do-Liste, {ding} zu besorgen.", "Schick eine Nachricht an die Familie.",
    "Wann hat der Grüne Baum dieses Jahr Urlaub?", "Erzähl mir einen Witz.",
    "Was ist der Unterschied zwischen {ding} und {essen}?", "Wie spät ist es?",
    "Lies mir die Termine für {tag} vor.", "Trag bitte ein, dass wir {tag} {essen} hatten.",
    "Vermerk bitte, dass {ding} leer ist.", "Kannst du mal schauen, ob wir {essen} gespeichert haben?",
]
_FUELL = {
    "tag": ["morgen", "heute", "Freitag", "Samstag", "Montag", "übermorgen", "Mittwoch", "gestern"],
    "essen": ["Spaghetti", "Flammkuchen", "Schweinebraten", "Pancakes", "Gemüsesuppe", "Wraps",
              "Currywurst", "Bruschetta", "Ratatouille"],
    "ding": ["Milch", "Batterien", "Kaffee", "Klopapier", "den Müll", "die Zeitung", "Brot"],
    "uhr": ["8", "16.15", "18.30", "10", "14"],
    "min": ["fünf", "10", "20", "drei"],
}
_GEPLAUDER = [
    "Danke.", "Vielen Dank.", "Das war ein Fehltrigger.", "Stopp.", "Ja, ja.", "Okay.",
    "Guten Morgen!", "Das war der Fernseher.", "Hallo.", "Sollte nur ein Test sein.",
    "Das war nichts für dich.", "Alles klar, passt schon.", "Was?", "Nein, lass mal.",
    "Ich wollte nur testen, ob du mich hörst.", "Wunderbar, danke dir.", "Ach so.",
]


def _ende(s: str, rng: random.Random) -> str:
    """STT-Oberflaeche: Satzzeichen am Ende mal da, mal nicht."""
    s = s.strip()
    if s[-1] not in ".!?":
        s += rng.choice([".", ".", "!", ""])
    return s


# Satzanfaenge, die nach der Anrede klein weitergehen ("Gaston, mach ...").
# Alles andere (meist ein Substantiv: "Gastau Wohnzimmer Roller auf 50%")
# bleibt gross — so steht es in den echten Transkripten.
_KLEIN_NACH_ANREDE = set("""mach macht mache schalte schalt schaltet stell setz fahr zieh öffne
schließ lass kannst dreh trag erinnere zeig wie was ist warum wer hast ich die das der den
lies erzähl such schick wann weißt vermerk starte aktiviere hat läuft auf wir es bitte
checke lösche hilf habe gibt spiel sag erhöhe reduziere ändere dimme""".split())


def _vorne(s: str, rng: random.Random, anrede: list[str], p: float = 0.6) -> str:
    s = s.strip()
    if rng.random() < p:
        erstes = s.split()[0].lower().strip(",.!?")
        if erstes in _KLEIN_NACH_ANREDE:
            s = s[0].lower() + s[1:]
        return rng.choice(anrede) + s
    return s[0].upper() + s[1:]


def _norm(s: str) -> str:
    return re.sub(r"[^\wäöüß]+", " ", s.lower()).strip()


# Geraete-Endungen, an denen ein Kompositum zerlegt wird. Die STT schreibt
# zusammengesetzte Namen oft getrennt ("Wohnzimmer Rollo", "Wohnzimmer
# Roller") — Laya kannte nur die Komposita aus den capabilities und gab
# getrennten Formen "keins" (Messung 2026-09-29/10-01, LAYA_TRAINING.md Nr. 10).
# Bewusst HIER und nicht in Node-RED: das ist eine Schreibvariante der STT,
# kein anderer Name. In den `namen` wuerde sie Regel A aufweichen ("rollos"
# allein wuerde zum Gruppenbeleg) und Gemmas Prompt verlaengern.
_ENDUNGEN = ("beleuchtung", "heizung", "rollos", "rollo", "lichter", "licht",
             "lampe", "leiste")


def _schreibvarianten(name: str) -> list[str]:
    """'Wohnzimmerrollo' -> ['Wohnzimmer Rollo', 'Wohnzimmer-Rollo'];
    'Rosazimmerrollo' zusaetzlich -> 'Rosa Zimmer Rollo'. Mehrwortnamen
    und Namen ohne bekannte Endung bleiben unberuehrt."""
    if " " in name:
        return []
    low = name.lower()
    for endung in _ENDUNGEN:
        if low.endswith(endung) and len(low) - len(endung) >= 3:
            vorn, hinten = name[:-len(endung)], endung.capitalize()
            out = [f"{vorn} {hinten}", f"{vorn}-{hinten}"]
            if vorn.lower().endswith("zimmer") and len(vorn) > len("zimmer") + 2:
                out.append(f"{vorn[:-6].capitalize()} Zimmer {hinten}")
            return out
    return []


def synthetisch(digest: dict, rng: random.Random, anrede: list[str],
                je_ziel: int) -> list[dict]:
    zeilen = []
    for zid, z in digest.items():
        namen = list(z.get("namen") or [zid])
        namen += [v for n in list(namen) for v in _schreibvarianten(n) if v not in namen]
        wert = z.get("wert") or {}
        befehle = []
        for aktion in z.get("aktionen") or []:
            schluessel = aktion
            if aktion == "setzen":
                schluessel = f"setzen:{wert.get('einheit', 'prozent')}"
            for vorlage in _BEFEHL.get(schluessel, []):
                befehle.append((aktion, vorlage))
        rng.shuffle(befehle)
        for i in range(min(je_ziel, len(befehle) * len(namen))):
            aktion, vorlage = befehle[i % len(befehle)]
            name = namen[(i // len(befehle) + i) % len(namen)]
            w = None
            if aktion == "setzen":
                lo, hi = wert.get("min", 0), wert.get("max", 100)
                w = (rng.choice([10, 20, 30, 40, 50, 60, 70, 80, 90]) if wert.get("einheit") == "prozent"
                     else rng.randint(max(lo, 16), min(hi, 24)))
                w = max(lo, min(hi, w))
                if "halb" in vorlage:
                    w = 50
            satz = vorlage.format(n=_mit_artikel(name, rng), w=w)
            zeilen.append({"satz": _ende(_vorne(satz, rng, anrede), rng), "schalten": True,
                           "ziel": zid, "aktion": aktion, "wert": w,
                           "herkunft": "synth:befehl"})
        # schwere Negative mit demselben Namen — nicht fuer Szenen/Routinen,
        # deren Namen selbst schon Befehle sind ("Stop alle Rollos")
        if z.get("typ") in ("szene", "routine"):
            continue
        neg = list(_NICHT_SCHALTEN)
        if z.get("typ") == "rollo" or "auf" in (z.get("aktionen") or []):
            neg += _NICHT_SCHALTEN_ROLLO * 2
        if z.get("typ") == "heizung":
            neg += _NICHT_SCHALTEN_HEIZUNG * 2
        for vorlage in rng.sample(neg, min(len(neg), max(4, je_ziel // 3))):
            n = _mit_artikel(rng.choice(namen), rng)
            satz = vorlage.format(n=n, N=n[0].upper() + n[1:])
            zeilen.append({"satz": _ende(_vorne(satz, rng, anrede, 0.4), rng), "schalten": False,
                           "ziel": None, "aktion": None, "wert": None,
                           "herkunft": "synth:ziel_ohne_schalten"})
    for satz in _BEFEHL_OHNE_ZIEL:
        for _ in range(2):
            zeilen.append({"satz": _ende(_vorne(satz, rng, anrede), rng), "schalten": True,
                           "ziel": None, "aktion": None, "wert": None,
                           "herkunft": "synth:befehl_ohne_ziel"})
    for _ in range(len(_AUFTRAG) * 12):
        v = rng.choice(_AUFTRAG)
        satz = v.format(**{k: rng.choice(w) for k, w in _FUELL.items()})
        zeilen.append({"satz": _ende(_vorne(satz, rng, anrede, 0.5), rng), "schalten": False,
                       "ziel": None, "aktion": None, "wert": None, "herkunft": "synth:auftrag"})
    for satz in _GEPLAUDER:
        zeilen.append({"satz": _ende(_vorne(satz, rng, anrede, 0.2), rng), "schalten": False,
                       "ziel": None, "aktion": None, "wert": None, "herkunft": "synth:geplauder"})
    return zeilen


_MASSIVE_JA = ("iot_hue_", "iot_wemo_")
_MASSIVE_WEG = ("iot_coffee", "iot_cleaning", "audio_volume_", "play_radio", "play_music")
_FREMDE_ANREDE = re.compile(r"^(olly|siri|alexa|hey|okay google|ok google)\b[\s,]*", re.I)


def massive(pfad: str, rng: random.Random, anrede: list[str], max_nein: int) -> list[dict]:
    ja, nein = [], []
    for zeile in open(pfad, encoding="utf-8"):
        d = json.loads(zeile)
        intent, utt = d["intent"], d["utt"].strip()
        if not utt or intent.startswith(_MASSIVE_WEG):
            continue
        hatte_anrede = bool(_FREMDE_ANREDE.match(utt))
        utt = _FREMDE_ANREDE.sub("", utt) or utt
        # MASSIVE ist klein und ohne Satzzeichen; STT liefert Grossschreibung
        # am Anfang und meist einen Punkt. Beides mischen.
        if rng.random() < 0.7:
            utt = _ende(utt, rng)
            utt = _vorne(utt, rng, anrede, 1.0 if hatte_anrede else 0.3)
        eintrag = {"satz": utt, "ziel": None, "aktion": None, "wert": None,
                   "herkunft": f"massive:{intent}"}
        (ja if intent.startswith(_MASSIVE_JA) else nein).append({**eintrag,
                                                                  "schalten": intent.startswith(_MASSIVE_JA)})
    rng.shuffle(nein)
    return ja + nein[:max_nein]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--massive", help="Pfad zu MASSIVE 1.1 data/de-DE.jsonl")
    ap.add_argument("--ohne-massive", action="store_true")
    ap.add_argument("--je-ziel", type=int, default=40, help="Befehle je Ziel")
    ap.add_argument("--massive-nein", type=int, default=3000,
                    help="hoechstens so viele MASSIVE-Negative (Ja-Saetze immer alle)")
    ap.add_argument("--seed", type=int, default=20260928)
    ap.add_argument("--aus", default=_AUS)
    ap.add_argument("--probe", type=int, default=0, help="n zufaellige Zeilen je Herkunft zeigen")
    args = ap.parse_args()
    if not args.massive and not args.ohne_massive:
        ap.error("--massive PFAD oder --ohne-massive")

    akt = Actuator(load_profile().actuator)
    if not akt.refresh():
        print("capabilities-refresh fehlgeschlagen — laeuft die Gegenstelle?")
        return 2
    rng = random.Random(args.seed)
    zeilen = synthetisch(akt.digest or {}, rng, _ANREDE, args.je_ziel)
    if args.massive:
        zeilen += massive(os.path.expanduser(args.massive), rng, _ANREDE, args.massive_nein)

    # Dubletten raus, und alles, was (normalisiert) im Test-Set steht
    test = set()
    if os.path.exists(_TESTSET):
        for z in open(_TESTSET, encoding="utf-8"):
            if z.strip() and not z.startswith("#"):
                test.add(_norm(json.loads(z)["satz"]))
    gesehen, rein, im_test = set(), [], 0
    for z in zeilen:
        k = _norm(z["satz"])
        if k in test:
            im_test += 1
            continue
        if k in gesehen:
            continue
        gesehen.add(k)
        rein.append(z)
    rng.shuffle(rein)

    # Schnappschuss der capabilities daneben: das Training baut daraus die
    # ziel-Frage (laya_intent.ziel_frage). Aendern sich die Ziele, passt ein
    # alter Checkpoint nicht mehr zur Frage, die der Betrieb stellt.
    with open(os.path.splitext(args.aus)[0] + ".capabilities.json", "w", encoding="utf-8") as o:
        json.dump({"version": akt.version, "digest": akt.digest}, o, ensure_ascii=False, indent=1)
    with open(args.aus, "w", encoding="utf-8") as o:
        o.write(f"# Tor-Trainingsdaten, erzeugt von tools/tor_trainset.py, capabilities "
                f"{akt.version}, seed {args.seed}\n")
        for z in rein:
            o.write(json.dumps(z, ensure_ascii=False) + "\n")

    zaehl = collections.Counter((z["herkunft"].split(":")[0] + ":" +
                                 (z["herkunft"].split(":")[1] if z["herkunft"].startswith("synth") else
                                  ("ja" if z["schalten"] else "nein"))) for z in rein)
    print(f"{len(rein)} Saetze nach {args.aus} (capabilities {akt.version}); "
          f"{im_test} entfernt, weil sie im Test-Set stehen")
    print(f"  ja {sum(z['schalten'] for z in rein)}, nein {sum(not z['schalten'] for z in rein)}")
    for k, n in sorted(zaehl.items()):
        print(f"  {k:32s} {n}")
    if args.probe:
        nach = collections.defaultdict(list)
        for z in rein:
            nach[z["herkunft"] if z["herkunft"].startswith("synth") else
                 "massive:" + ("ja" if z["schalten"] else "nein")].append(z["satz"])
        for k, s in sorted(nach.items()):
            print(f"\n--- {k}")
            for x in random.Random(1).sample(s, min(args.probe, len(s))):
                print("   ", x)
    return 0


if __name__ == "__main__":
    sys.exit(main())
