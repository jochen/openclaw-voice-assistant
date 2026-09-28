"""Laya als Aktuator-Klassifikator: Fragen, Wert-Leser, Umrechnung ins Intent.

Laya (github.com/NandhaKishorM/laya) erzeugt keinen Text, sondern beantwortet
typisierte Fragen in einem Forward-Pass. Der Aktuator braucht drei davon:

    tor     noul    will der Sprecher ein Gerät schalten?         P(ja)
    ziel    choice  welches Ziel aus /capabilities (oder "keins")?
    aktion  choice  ein/aus/auf/zu/setzen/aktivieren/starten

Den WERT kann Laya nicht liefern (keine freien Zahlen) — den liest
`lese_wert()` deterministisch aus dem Satz. Das ist keine Notlösung: die
Zahl steht im Transkript, ein Modell kann sie dort nur abschreiben oder
falsch abschreiben.

Heraus kommt dasselbe Intent-Dict wie aus Actuator.classify() (ist_kommando,
ziel, aktion, wert, einheit). Danach laufen _mehrzahl_gruppe() und verdict()
unverändert — beide Klassifikatoren werden also mit derselben Elle gemessen.

Dieses Modul ist absichtlich nur stdlib: es wird auch vom Trainingsskript
(tools/laya_aktuator_train.py, eigener venv mit torch) importiert, damit Training
und Betrieb garantiert dieselben Fragen stellen. Eine Frage, die im Training
anders lautet als im Betrieb, ist ein anderes Modell.

Ziele stehen nirgends im Code: die ziel-Frage entsteht aus dem Digest der
capabilities, wie der Gemma-Prompt.
"""

from __future__ import annotations

import json
import re
import time
import urllib.request

TOR_FRAGE = {
    "type": "noul",
    "instructions": "Will der Sprecher mit diesem Satz ein Gerät im Haus "
                    "schalten oder einstellen, zum Beispiel Licht, Rollo oder Heizung?",
}

KEIN_ZIEL = "keins"

AKTIONEN = {
    "ein": "einschalten, anmachen",
    "aus": "ausschalten, ausmachen",
    "auf": "öffnen, hochfahren",
    "zu": "schließen, runterfahren",
    "setzen": "auf einen Wert stellen, Prozent oder Grad",
    "aktivieren": "eine Szene aktivieren",
    "starten": "eine Routine starten",
}


def ziel_frage(digest: dict) -> dict:
    """Die ziel-Frage aus dem Digest. Reihenfolge = Reihenfolge im Digest;
    im Training wird sie gemischt, damit das Modell nicht an Positionen lernt."""
    kriterien = {zid: ", ".join(z.get("namen") or [zid]) for zid, z in digest.items()}
    kriterien[KEIN_ZIEL] = "kein bestimmtes Gerät erkennbar"
    return {"type": "choice", "instructions": "Welches Gerät im Haus ist gemeint?",
            "criteria": kriterien}


AKTION_FRAGE = {"type": "choice", "instructions": "Was soll mit dem Gerät passieren?",
                "criteria": AKTIONEN}


def fragen(digest: dict) -> dict:
    return {"tor": TOR_FRAGE, "ziel": ziel_frage(digest), "aktion": AKTION_FRAGE}


# --- Wert ---------------------------------------------------------------------
_EINER = {"null": 0, "ein": 1, "eins": 1, "eine": 1, "zwei": 2, "drei": 3, "vier": 4,
          "fünf": 5, "sechs": 6, "sieben": 7, "acht": 8, "neun": 9}
_ZEHNER = {"zwanzig": 20, "dreißig": 30, "dreissig": 30, "vierzig": 40, "fünfzig": 50,
           "sechzig": 60, "siebzig": 70, "achtzig": 80, "neunzig": 90}
_SONDER = {"zehn": 10, "elf": 11, "zwölf": 12, "dreizehn": 13, "vierzehn": 14,
           "fünfzehn": 15, "sechzehn": 16, "siebzehn": 17, "achtzehn": 18,
           "neunzehn": 19, "hundert": 100, "einhundert": 100}


def _zahlwort(w: str) -> int | None:
    w = w.lower()
    if w in _SONDER:
        return _SONDER[w]
    if w in _ZEHNER:
        return _ZEHNER[w]
    if w in _EINER:
        return _EINER[w]
    m = re.fullmatch(r"(\w+?)und(\w+)", w)       # einundzwanzig, fünfundsiebzig
    if m and m.group(1) in _EINER and m.group(2) in _ZEHNER:
        return _EINER[m.group(1)] + _ZEHNER[m.group(2)]
    return None


def lese_wert(satz: str) -> int | None:
    """Zahl für aktion=setzen aus dem Transkript: Ziffern ("70%", "22 Grad"),
    Zahlwörter ("zwanzig Prozent", "einundzwanzig Grad"), "halb" = 50.
    Nimmt die letzte Zahl im Satz — die steht bei Befehlen am Ende, eine
    Zahl davor ist eher Teil eines Namens oder eines Nebensatzes."""
    kandidaten: list[int] = []
    for tok in re.findall(r"\d+|[a-zäöüß]+", satz.lower()):
        if tok.isdigit():
            kandidaten.append(int(tok))
        elif (z := _zahlwort(tok)) is not None and tok not in ("ein", "eine", "eins"):
            kandidaten.append(z)
        elif tok in ("halb", "halbe", "halben"):
            kandidaten.append(50)
    return kandidaten[-1] if kandidaten else None


# --- Anfrage und Umrechnung ----------------------------------------------------
class LayaUrteil:
    __slots__ = ("p_ja", "ziel", "p_ziel", "aktion", "p_aktion", "wert", "ms", "fehler")

    def __init__(self, p_ja=None, ziel=None, p_ziel=None, aktion=None, p_aktion=None,
                 wert=None, ms=0.0, fehler=None):
        self.p_ja, self.ziel, self.p_ziel = p_ja, ziel, p_ziel
        self.aktion, self.p_aktion, self.wert = aktion, p_aktion, wert
        self.ms, self.fehler = ms, fehler

    def als_dict(self) -> dict:
        r = lambda x: None if x is None else round(x, 4)  # noqa: E731
        return {"p_ja": r(self.p_ja), "ziel": self.ziel, "p_ziel": r(self.p_ziel),
                "aktion": self.aktion, "p_aktion": r(self.p_aktion), "wert": self.wert,
                "ms": round(self.ms), "fehler": self.fehler}


def frage_laya(url: str, satz: str, digest: dict, timeout: float = 5.0,
               model: str = "multilingual") -> LayaUrteil:
    """Alle drei Fragen in einer Anfrage an laya-serve (/v1/systemone)."""
    t0 = time.time()
    body = {"model": model, "state": {"satz": satz}, "questions": fragen(digest)}
    req = urllib.request.Request(url.rstrip("/") + "/v1/systemone",
                                 data=json.dumps(body).encode(),
                                 headers={"content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            a = json.load(r)["answers"]
        ms = (time.time() - t0) * 1000
        z, ak = a["ziel"], a["aktion"]
        return LayaUrteil(
            p_ja=float(a["tor"]["noul"]),
            ziel=z["choice"], p_ziel=float(z["probabilities"][z["choice"]]),
            aktion=ak["choice"], p_aktion=float(ak["probabilities"][ak["choice"]]),
            wert=lese_wert(satz) if ak["choice"] == "setzen" else None, ms=ms)
    except Exception as e:
        return LayaUrteil(ms=(time.time() - t0) * 1000, fehler=f"{type(e).__name__}: {e}")


def als_intent(u: LayaUrteil, digest: dict, schwelle: float = 0.5) -> dict | None:
    """LayaUrteil -> Intent im Format von Actuator.classify().

    None bei Ausfall (wie classify). Tor nein oder ziel "keins" -> kein
    Kommando; das entspricht der Prompt-Regel bei Gemma ("Rollo ohne Raum
    -> kein Kommando"), und _mehrzahl_gruppe() kann danach die Mehrzahl
    ("die Rollos zu") genauso auffangen wie bei Gemma.
    """
    if u.fehler or u.p_ja is None:
        return None
    if u.p_ja < schwelle or u.ziel in (None, KEIN_ZIEL):
        return {"ist_kommando": False, "aktion": None, "ziel": None, "wert": None, "einheit": None}
    einheit = ((digest.get(u.ziel) or {}).get("wert") or {}).get("einheit") if u.wert is not None else None
    return {"ist_kommando": True, "aktion": u.aktion, "ziel": u.ziel,
            "wert": u.wert, "einheit": einheit}
