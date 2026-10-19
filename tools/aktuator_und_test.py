#!/usr/bin/env python3
"""Doppelbefehle mit „und“ („…das Abendlicht ein und mach den Wohnzimmerrollo zu“) messen.

Aufruf (Projekt-venv wird selbst gesucht):
    ow-venv/bin/python -m tools.aktuator_und_test --erzeugen   # Set aus dem Tor-Set bauen
    ow-venv/bin/python -m tools.aktuator_und_test              # messen (Kette des Betriebs)
    ow-venv/bin/python -m tools.aktuator_und_test --alle --json out.json

Was gemessen wird
-----------------
Was am Ende AUSGEFUEHRT worden waere, als Menge von ziel/aktion(/wert) —
heute ist das hoechstens eins, weil der Aktuator je Satz einen Intent bildet
(aktuator_schatten.entscheiden, also Laya bzw. Gemma laut Profil). Eine
spaetere Zerlegung liefert mehrere; dieses Werkzeug misst beide gleich.

Je Fall:
    richtig   genau die gemeinten Schaltungen; bei brain=true: nichts
              geschaltet (Brain oder Rueckfrage)
    verpasst  nichts geschaltet, obwohl nur Schaltungen gemeint waren
              (geht an den Brain: langsam, nicht falsch)
    halb      nur ein Teil des Gemeinten geschaltet, der Rest still verloren.
              So am 2026-10-03 20:24: „…das Tischlicht und die
              Küchenbeleuchtung aus“ → nur kuechenbeleuchtung/aus, gemeldet
              als Erfolg. Bei brain=true heisst halb: geschaltet, die Frage
              oder der Auftrag daneben ist verloren.
    FALSCH    etwas geschaltet, das nicht gemeint war. FALSCH muss 0 bleiben.
    Ausfall   die Kette konnte nicht urteilen — kein Ergebnis, eigene Spalte.

Das Set (privat, testsets/aktuator_und/, die Ziel-ids gehoeren zum Haus)
-----------------------------------------------------------------------
erzeugt.jsonl         aus testsets/actuator_tor.jsonl geklebt (--erzeugen):
                      zwei gelabelte Befehle, Befehl + Nicht-Befehl (beide
                      Reihenfolgen), zwei Nicht-Befehle. Verbinder „ und “,
                      „, und “, „. Und “, „ und dann “. Die Labels kommen aus
                      dem Tor-Set, neu gelabelt wird nichts.
handgeschrieben.jsonl wie Menschen es sagen und die STT es schreibt: Verb
                      nur einmal („Tischlicht und Sofalicht aus“), Verb
                      fehlt hinten („…an und den Rollo zu“), drei Befehle,
                      Komma statt „und“, Punkt + „Und“ (Qwen), Wiederholungen;
                      dazu Gegenbeispiele (Befehl + Frage, Erzaehlung,
                      Ziel ohne Raum, Zeitangabe).

Eine Zeile:
    {"satz": "...", "soll": [{"ziel", "aktion", "wert"?}, ...],
     "brain": false, "art": "befehl+befehl", "quelle": "erzeugt"|"hand"}
`brain: true` heisst: im Satz steckt mehr als Schalten (oder es ist gar
nichts zu schalten); richtig ist dann nur „nichts geschaltet“, der Brain
erledigt den ganzen Satz. `soll` nennt trotzdem die Schaltungen darin, damit
„halb“ von „FALSCH“ unterscheidbar bleibt.

VORBEHALT: alle Saetze sind erzeugt oder ausgedacht, nicht gesprochen. Die
Zahl zeigt, ob eine Zerlegung kann, was sie koennen soll — nicht, wie oft
so gesprochen wird. Echte Doppelbefehle aus dem Betrieb gehoeren spaeter mit
quelle "echt" dazu.

Messreihe (gilt fuer capabilities- und Laya-Checkpoint-Version)
---------------------------------------------------------------

    2026-10-19  Nulllinie: Aktuator von heute (Laya, ein Intent je Satz),
                capabilities b05e5198, erzeugt --seed 1 (200) + hand (53)
                                     richtig verpasst  halb FALSCH
                erzeugt befehl+befehl      0       41    55     24
                erzeugt befehl+gerede     21        0    39      0
                erzeugt gerede+gerede     20        0     0      0
                hand (alle Arten)         16        3    24     10
                gesamt                    57       44   118     34
                Median 80 ms je Satz. Die 34 FALSCH sind keine Haelften,
                sondern Mischungen: Laya nimmt das Ziel aus der einen und
                die Aktion (oder den Wert) aus der anderen Haelfte — „Küchen-
                licht aus und das Flurlicht an“ → Flurlicht aus, „Fernseher
                aus und mach das Wohnzimmerlicht an“ → Wohnzimmerlicht aus,
                „Küchenlicht aus … Wohnzimmerrollo zu“ → Wohnzimmerlicht aus
                (ein drittes Geraet). Doppelbefehle schalten heute also nicht
                nur halb, sondern in gut einem Fuenftel falsch.

    2026-10-19  Set neu erzeugt (Anrede der zweiten Haelfte weg, „Ja. Ja,
                und …“ nicht mehr als Gerede), sonst wie oben. Dann
                services/aktuator_und.py (--kette zerlegt, der Default).
                                     richtig verpasst  halb FALSCH
                Laya  heute                51       44   124     34
                Laya  zerlegt             215       38     0      0
                Gemma heute                39        7   193     14
                Gemma zerlegt             212       38     0      3
                Laya zerlegt: Median 158 ms je Satz (zwei Anfragen), Gemma
                2425 ms. Die 38 verpassten sind sicher (Brain): Haelften,
                die der Klassifikator auch allein nicht schafft (Verhoerer,
                Regel A, „Wohnzimmer … dazu“) oder Restteile („das da“).
                Die 3 FALSCH bei Gemma waren ohne Zerlegung genauso FALSCH,
                es sind Gemma-Fehler an einer Haelfte: „Esstischrohlos auf“
                → ein einzelnes Rollo statt der Gruppe, „Küchenbeleuchtung aus“
                → ein, und der Bericht „Er hat das Kinderzimmerlicht angemacht …“ (kein
                Aktionswort, wird gar nicht zerlegt). Live entscheidet Laya.
                --einzel: 27 der 371 Tor-Saetze werden zerlegt, Ergebnis
                unveraendert (Laya und Gemma je 26 richtig, 1 verpasst).
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import random
import re
import statistics
import sys

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_VENV = os.path.join(_REPO, "ow-venv", "bin", "python")
if os.path.exists(_VENV) and os.path.realpath(sys.executable) != os.path.realpath(_VENV):
    os.execv(_VENV, [_VENV, "-m", "tools.aktuator_und_test", *sys.argv[1:]])

_TOR_SET = os.path.join(_REPO, "testsets", "actuator_tor.jsonl")
_ORDNER = os.path.join(_REPO, "testsets", "aktuator_und")
_ERZEUGT = os.path.join(_ORDNER, "erzeugt.jsonl")
_HAND = os.path.join(_ORDNER, "handgeschrieben.jsonl")

# Anrede vorne an der zweiten Haelfte weg, auch die Verhoerer der STT
# („Gastau“, „Gastronom“, „Kasten“): in einem echten Doppelbefehl steht das
# Wakewort nur einmal, am Anfang.
_ANREDE = re.compile(r"^\s*(?:hey\s+|hallo\s+|ok(?:ay)?\s+)?(?:gast|gäst|kast|gest)\w*\s*[,.!?]?\s*",
                     re.I)
# Erstes Wort der zweiten Haelfte klein schreiben, wenn es kein Nomen ist
# (mitten im Satz schreibt die STT „und mach“, nicht „und Mach“).
_KLEIN = {"mach", "mache", "machst", "schalt", "schalte", "schaltest", "fahr", "fahre", "stell",
          "stelle", "setz", "setze", "dreh", "drehe", "bitte", "das", "die", "den", "der", "dem",
          "kannst", "könntest", "würdest", "kann", "mal", "jetzt", "auch", "noch", "ich", "wir",
          "du", "es", "was", "wie", "wann", "wo", "ist", "sind", "hast", "hat", "sag", "lass",
          "alle", "und", "ja", "nein", "nee", "danke", "okay", "gut", "schön", "super"}
_VERBINDER = (" und ", ", und ", ". Und ", " und dann ")


# ── Set erzeugen ──────────────────────────────────────────────────────────

def _lesen(pfad: str) -> list[dict]:
    with open(pfad, encoding="utf-8") as f:
        return [json.loads(z) for z in f if z.strip() and not z.startswith("#")]


def _soll(f: dict) -> dict:
    s = {"ziel": f["ziel"], "aktion": f["aktion"]}
    if f.get("wert") is not None:
        s["wert"] = f["wert"]
    return s


def _vorne(satz: str) -> str:
    return satz.strip().rstrip(".!?…, ")


def _hinten(satz: str) -> str:
    rest = _ANREDE.sub("", satz.strip())
    if not rest:
        return rest
    erstes = rest.split()[0].strip(",.!?").lower()
    return rest[0].lower() + rest[1:] if erstes in _KLEIN else rest


def kleben(a: str, b: str, verbinder: str) -> str:
    return _vorne(a) + verbinder + _hinten(b)


def _nur_fuellwoerter(satz: str) -> bool:
    """„Ja. Ja.“ ist kein Gerede neben einem Befehl, sondern Fuellung."""
    from voice_assistant.services.aktuator_und import _fuelle
    return _fuelle(satz)


def erzeugen(seed: int = 1, n_befehle: int = 120, n_gemischt: int = 60,
             n_gerede: int = 20) -> list[dict]:
    tor = _lesen(_TOR_SET)
    befehle = [f for f in tor if f.get("schalten") is True and f.get("ziel") and f.get("aktion")
               and "alexa" not in f["satz"].lower()]
    gerede = [f for f in tor if f.get("schalten") is False
              and 2 <= len(f["satz"].split()) <= 12 and "alexa" not in f["satz"].lower()
              and not _nur_fuellwoerter(f["satz"])]
    rng = random.Random(seed)
    zeilen, gesehen = [], set()

    def neu(satz, soll, brain, art, teile):
        if satz in gesehen:
            return False
        gesehen.add(satz)
        zeilen.append({"satz": satz, "soll": soll, "brain": brain, "art": art,
                       "quelle": "erzeugt", "teile": teile})
        return True

    while sum(1 for z in zeilen if z["art"] == "befehl+befehl") < n_befehle:
        a, b = rng.sample(befehle, 2)
        if a["ziel"] == b["ziel"]:
            continue
        v = rng.choice(_VERBINDER)
        neu(kleben(a["satz"], b["satz"], v), [_soll(a), _soll(b)], False, "befehl+befehl",
            [a["satz"], b["satz"]])
    while sum(1 for z in zeilen if z["art"] == "befehl+gerede") < n_gemischt:
        a, g = rng.choice(befehle), rng.choice(gerede)
        v = rng.choice(_VERBINDER)
        satz = kleben(a["satz"], g["satz"], v) if rng.random() < 0.5 else kleben(g["satz"], a["satz"], v)
        neu(satz, [_soll(a)], True, "befehl+gerede", [a["satz"], g["satz"]])
    while sum(1 for z in zeilen if z["art"] == "gerede+gerede") < n_gerede:
        g1, g2 = rng.sample(gerede, 2)
        neu(kleben(g1["satz"], g2["satz"], rng.choice(_VERBINDER)), [], True, "gerede+gerede",
            [g1["satz"], g2["satz"]])
    return zeilen


# ── Messen ────────────────────────────────────────────────────────────────

def _schluessel(s: dict) -> tuple:
    return (s.get("ziel"), s.get("aktion"), s.get("wert") if s.get("aktion") == "setzen" else None)


def bewerte(fall: dict, geschaltet: list[dict] | None) -> str:
    if geschaltet is None:
        return "Ausfall"
    soll = {_schluessel(s) for s in fall["soll"]}
    ist = [_schluessel(s) for s in geschaltet]
    if any(s not in soll for s in ist):
        return "FALSCH"
    if fall.get("brain"):
        return "richtig" if not ist else "halb"
    if set(ist) == soll:
        return "richtig"
    return "verpasst" if not ist else "halb"


def kette_heute(akt, satz: str) -> tuple[list[dict] | None, str, float]:
    """Der Aktuator von heute: ein Intent je Satz."""
    from voice_assistant.services.actuator import VERDICT_AUSFUEHRBAR
    from voice_assistant.services.aktuator_schatten import entscheiden
    e = entscheiden(akt, satz)
    if e.ausfall:
        return None, e.ausgang, e.ms
    if e.verdict != VERDICT_AUSFUEHRBAR:
        return [], e.ausgang, e.ms
    return [dict(e.intent)], e.ausgang, e.ms


def kette_zerlegt(akt, satz: str) -> tuple[list[dict] | None, str, float]:
    """Mit Zerlegung (services/aktuator_und.py); ohne Doppelbefehl wie heute."""
    from voice_assistant.services import aktuator_und
    z = aktuator_und.entscheiden(akt, satz)
    if z is None:
        return kette_heute(akt, satz)
    if any(e.ausfall for e in z.entscheidungen):
        return None, "Ausfall", z.ms
    if not z.ausfuehrbar:
        return [], f"Brain: {z.grund}", z.ms
    return [dict(i) for i in z.intents], " + ".join(z.teile), z.ms


_KETTEN = {"heute": kette_heute, "zerlegt": kette_zerlegt}


def _zeige(liste: list[dict] | None) -> str:
    if liste is None:
        return "Ausfall"
    if not liste:
        return "nichts"
    return ", ".join(f"{s['ziel']}/{s['aktion']}" + (f"={s['wert']}" if s.get("wert") is not None
                                                      and s.get("aktion") == "setzen" else "")
                     for s in liste)


def messen(akt, faelle: list[dict], alle: bool, json_aus: str | None, kette: str) -> int:
    klassen = ("richtig", "verpasst", "halb", "FALSCH", "Ausfall")
    je_art: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    gesamt = collections.Counter()
    ms, zeilen = [], []
    for f in faelle:
        geschaltet, ausgang, t = _KETTEN[kette](akt, f["satz"])
        k = bewerte(f, geschaltet)
        art = f"{f['quelle']}:{f['art']}"
        je_art[art][k] += 1
        gesamt[k] += 1
        ms.append(t)
        zeilen.append({**f, "ist": geschaltet, "ausgang": ausgang, "klasse": k, "ms": t})
    print(f"{len(faelle)} Saetze, capabilities {akt.version}, {akt.cfg.klassifikator}, "
          f"Kette {kette}\n")
    print(f"{'':28s} " + " ".join(f"{k:>8s}" for k in klassen))
    for art in sorted(je_art):
        print(f"{art:28s} " + " ".join(f"{je_art[art][k]:8d}" for k in klassen))
    print(f"{'gesamt':28s} " + " ".join(f"{gesamt[k]:8d}" for k in klassen))
    if ms:
        print(f"\nLatenz je Satz median {statistics.median(ms):.0f} ms, max {max(ms):.0f} ms")
    if gesamt["Ausfall"]:
        print(f"⚠️  {gesamt['Ausfall']} Ausfaelle — keine gueltige Messung")
    print()
    for z in zeilen:
        if alle or z["klasse"] != "richtig":
            soll = _zeige(z["soll"]) + (" (+Brain)" if z.get("brain") else "")
            print(f"  {z['klasse']:8s} {z['satz'][:100]}\n"
                  f"           soll {soll}\n           ist  {_zeige(z['ist'])}  [{z['ausgang']}]")
    if json_aus:
        with open(json_aus, "w", encoding="utf-8") as f:
            json.dump({"capabilities": akt.version, "kette": kette, "faelle": zeilen}, f, ensure_ascii=False, indent=1)
    return 1 if gesamt["FALSCH"] or gesamt["Ausfall"] else 0


def einzel(akt, alle: bool) -> int:
    """Das Tor-Set (ein Befehl je Satz, oder keiner) mit und ohne Zerlegung:
    die Zerlegung darf dort nichts schlechter machen."""
    from tools.aktuator_vergleich import bewerte as bewerte_einzel
    from voice_assistant.services import aktuator_und
    with akt._lock:
        namen = [n for z in (akt.digest or {}).values() for n in (z.get("namen") or [])]
    faelle = [f for f in _lesen(_TOR_SET) if f.get("schalten") is not None]
    zerlegt = [f for f in faelle if aktuator_und.zerlegen(f["satz"], namen) is not None]
    print(f"Tor-Set: {len(faelle)} Saetze, davon {len(zerlegt)} zerlegt "
          f"(nur die werden verglichen, der Rest laeuft unveraendert)\n")
    vorher, nachher = collections.Counter(), collections.Counter()
    for f in zerlegt:
        e_alt = _einzel_heute(akt, f["satz"])
        k_alt = bewerte_einzel(f, *e_alt)
        geschaltet, ausgang, _ = kette_zerlegt(akt, f["satz"])
        k_neu = _bewerte_liste(f, geschaltet)
        vorher[k_alt] += 1
        nachher[k_neu] += 1
        if alle or k_alt != k_neu or k_neu != "richtig":
            print(f"  {k_alt:8s} → {k_neu:8s} {f['satz'][:90]}\n"
                  f"           {ausgang}")
    print(f"\nvorher  {dict(vorher)}\nnachher {dict(nachher)}")
    return 1 if nachher["FALSCH"] > vorher["FALSCH"] else 0


def _einzel_heute(akt, satz: str):
    from voice_assistant.services.aktuator_schatten import entscheiden
    e = entscheiden(akt, satz)
    return e.intent, e.verdict


def _bewerte_liste(f: dict, geschaltet: list[dict] | None) -> str:
    """Wie aktuator_vergleich.bewerte, fuer eine Liste von Schaltungen."""
    if geschaltet is None:
        return "Ausfall"
    if f.get("schalten") and f.get("ziel"):
        if not geschaltet:
            return "verpasst"
        soll = _schluessel({"ziel": f["ziel"], "aktion": f.get("aktion"), "wert": f.get("wert")})
        ok = len(geschaltet) == 1 and (soll == _schluessel(geschaltet[0]) or (
            f.get("wert") is None and soll[:2] == _schluessel(geschaltet[0])[:2]))
        return "richtig" if ok else "FALSCH"
    return "FALSCH" if geschaltet else "richtig"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--erzeugen", action="store_true", help=f"{_ERZEUGT} neu bauen")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--nur", choices=("erzeugt", "hand"), help="nur einen Teil des Sets messen")
    ap.add_argument("--alle", action="store_true", help="auch die richtigen Faelle zeigen")
    ap.add_argument("--kette", choices=sorted(_KETTEN), default="zerlegt",
                    help="heute = ein Intent je Satz (Nulllinie), zerlegt = mit aktuator_und")
    ap.add_argument("--klassifikator", choices=("laya", "gemma"),
                    help="Default: actuator.klassifikator des Profils")
    ap.add_argument("--einzel", action="store_true",
                    help="Gegenprobe: Tor-Set mit und ohne Zerlegung")
    ap.add_argument("--json", help="Ergebnis je Satz hierhin schreiben")
    args = ap.parse_args()

    if args.erzeugen:
        zeilen = erzeugen(args.seed)
        os.makedirs(_ORDNER, exist_ok=True)
        with open(_ERZEUGT, "w", encoding="utf-8") as f:
            f.write(f"# Erzeugt von tools/aktuator_und_test.py --erzeugen --seed {args.seed} "
                    "aus testsets/actuator_tor.jsonl. Nicht von Hand aendern.\n")
            for z in zeilen:
                f.write(json.dumps(z, ensure_ascii=False) + "\n")
        print(f"{len(zeilen)} Saetze → {_ERZEUGT}")
        return 0

    faelle = []
    for pfad, quelle in ((_ERZEUGT, "erzeugt"), (_HAND, "hand")):
        if args.nur and args.nur != quelle:
            continue
        if not os.path.exists(pfad):
            print(f"❌ {pfad} fehlt (privat; erzeugt.jsonl: --erzeugen)")
            return 2
        faelle += [{**z, "quelle": z.get("quelle", quelle)} for z in _lesen(pfad)]

    from voice_assistant.config import load_profile
    from voice_assistant.services.actuator import Actuator
    cfg = load_profile().actuator
    if args.klassifikator:
        cfg.klassifikator = args.klassifikator
    akt = Actuator(cfg)
    if not akt.refresh():
        print("capabilities-refresh fehlgeschlagen — laeuft die Gegenstelle?")
        return 2
    if cfg.klassifikator == "gemma":
        akt.aufwaermen()   # Kaltstart je Prompt 8–12 s auf der Vega, sonst Ausfaelle
    if args.einzel:
        return einzel(akt, args.alle)
    return messen(akt, faelle, args.alle, args.json, args.kette)


if __name__ == "__main__":
    sys.exit(main())
