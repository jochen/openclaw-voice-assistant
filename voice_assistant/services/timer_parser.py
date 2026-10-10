"""Timer-Befehle fest aus dem Transkript lesen — ohne Modell.

Schnellweg des Küchentimers: greift nur, wenn ein Satz eindeutig ein
Timer-Befehl ist und vollständig zerlegt werden kann. Alles andere liefert
None und geht unverändert weiter (Aktuator-Tor, dann Brain, der ein
eigenes Timer-Werkzeug hat). Ein übersehener Befehl ist also langsam, nicht
falsch.

Gebaut gegen echte Transkripte (data/timer/de_saetze.jsonl als Vorlagen,
eingesprochen 2026-10-10, Messung: tools/timer_parser_test.py). Was die
Aufnahmen gezeigt haben und hier deshalb so steht:

- Qwen schreibt Zahlwörter ("fünfundvierzig"), Speaches/medium Ziffern
  ("45", "1,5") — beides wird gelesen.
- "Timer" ist der Anker und wird auch IM Wort gesucht ("Nudeltimer").
  Die Anrede verschmilzt oft mit dem nächsten Wort ("Gastrostellen Timer",
  "Das Duschstellentimer", "Gastrostimer") — deshalb wird ein Name NIE aus
  dem ersten Wort und nie aus etwas mit "stell"/"gast" darin gemacht.
- "Viertelstunde" kommt als "vierte Stunde" oder "drei Viertelstunde" an.
- "klingeln" schreibt Qwen als "klingen" (3 von 3).
- Eine Zahl direkt vor "Klingeln" ist eine Anzahl ("mit einem Klingen"),
  keine Sekunde — live 2026-10-10 als 61 s gelesen.
- Eine Uhrzeit ("Timer auf acht Uhr") ist keine Dauer → None.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from voice_assistant.services.laya_intent import _zahlwort

STELLEN = "stellen"
VERLAENGERN = "verlaengern"
NOCH = "noch"            # verlängern, wenn es den Timer gibt, sonst stellen
ABFRAGEN = "abfragen"
LOESCHEN = "loeschen"
KLINGELN = "klingeln"
ALLE = "*"


@dataclass
class TimerBefehl:
    aktion: str
    name: str | None = None        # Anzeigename ("Nudeln"), ALLE, oder None
    dauer_s: int | None = None
    klingeln: int | None = None


_TOKEN = re.compile(r"\d+(?:[.,]\d+)?|[a-zäöüß]+")

_EINHEIT_S = {
    "sekunde": 1, "sekunden": 1, "sek": 1,
    "minute": 60, "minuten": 60, "min": 60,
    "stunde": 3600, "stunden": 3600, "std": 3600,
}
_VIERTEL = {"viertelstunde": 900, "dreiviertelstunde": 2700}
_HALB = {"anderthalb": 1.5, "eineinhalb": 1.5}
_ARTIKEL = {"der", "die", "das", "den", "dem", "des", "ein", "eine", "einen", "einem", "nen", "ne"}
# Wörter direkt hinter "Timer", die kein Name sind
_KEIN_NAME = _ARTIKEL | {
    "auf", "für", "fuer", "plus", "noch", "um", "soll", "läuft", "laeuft", "laufen",
    "abbrechen", "löschen", "loeschen", "stoppen", "gestern", "heute", "ist", "mit",
    "von", "und", "an", "aus", "ab", "stellen", "setzen", "bitte", "mal", "wie",
    "zu", "zum", "steht", "klingeln", "klingen", "vierte", "viertel", "halbe",
    "halben", "nur", "jetzt", "gleich", "so", "lange", "wieder", "neu", "starten",
    "auch", "aber", "im", "in", "dann", "doch", "schon", "bei", "beim",
    "welche", "welcher", "welchen", "guter", "neuer", "neuen", "alle",
}
_FRAGE = re.compile(r"\bwie (lange|viel|steht|weit)\b|\bwelche[rn]?\b|\bwas macht\b")
_LOESCH = re.compile(r"\b(lösch|losch|loesch)\w*|\babbrech\w*|\bbeende\w*|\bstopp?\w*")
_VERLAENGER = re.compile(r"\bverläng\w*|\bverlaeng\w*|\bplus\b|\bdazu\b|\bdrauf\b")
_KLINGEL = re.compile(r"\bkling(el)?n\b|\bklingen\b|\bbimmel\w*|\bpiep\w*")
_ABLEHNEN = re.compile(r"\berinner\w*|\bweck\w*")


def _zahl(tok: str) -> float | None:
    if tok[0].isdigit():
        return float(tok.replace(",", "."))
    if tok in _HALB:
        return _HALB[tok]
    if tok in ("einen", "einer", "einem"):
        return 1.0
    z = _zahlwort(tok)
    return float(z) if z is not None else None


def _mal(tok: str, folgend: str | None) -> int | None:
    """'zehnmal', '10mal', 'zehn mal', 'einem Klingeln' → 10 bzw. 1."""
    if tok.endswith("mal") and len(tok) > 3:
        z = _zahl(tok[:-3])
        return int(z) if z else None
    if folgend == "mal" or (folgend is not None and _KLINGEL.fullmatch(folgend)):
        z = _zahl(tok)
        return int(z) if z else None
    return None


def _dauer(toks: list[str]) -> int | None | bool:
    """Summe aller Zeitangaben in Sekunden. None = keine Dauer, False = nicht
    eindeutig (z. B. "3 5 Stunden", Uhrzeit)."""
    gesamt = 0.0
    gefunden = False
    zahlen: list[float] = []
    letzte_einheit = 0
    i = 0
    while i < len(toks):
        t = toks[i]
        nxt = toks[i + 1] if i + 1 < len(toks) else None
        if t == "uhr":
            return False
        if _mal(t, nxt) is not None:
            zahlen = []
            i += 1 if t.endswith("mal") and len(t) > 3 else 2  # "zehnmal" | "zehn mal", "einem Klingeln"
            continue
        if t in _VIERTEL or (t == "vierte" and nxt in ("stunde",)):
            basis = _VIERTEL.get(t, 900)
            gesamt += basis * (zahlen[-1] if zahlen else 1)
            gefunden, zahlen, letzte_einheit = True, [], 0
            i += 1 if t in _VIERTEL else 2
            continue
        if t in ("halbe", "halben") and nxt in _EINHEIT_S:
            gesamt += 0.5 * _EINHEIT_S[nxt] * (zahlen[-1] if zahlen else 1)
            gefunden, zahlen, letzte_einheit = True, [], 0
            i += 2
            continue
        if t in _EINHEIT_S:
            if not zahlen:
                zahlen = []
                i += 1
                continue                      # "die Stunde" ohne Zahl: keine Dauer
            if len(zahlen) > 1 and all(z == int(z) for z in zahlen) and zahlen[-2] != 1:
                return False                  # "3 5 Stunden"
            gesamt += zahlen[-1] * _EINHEIT_S[t]
            gefunden, letzte_einheit, zahlen = True, _EINHEIT_S[t], []
            i += 1
            continue
        z = _zahl(t)
        if z is not None:
            zahlen.append(z)
        elif t not in ("und", "auf", "für", "fuer", "um", "noch", "plus"):
            # "zwei Minuten dreißig": nackte Zahl hinter einer Einheit zählt
            # in der nächstkleineren Einheit.
            if zahlen and letzte_einheit > 1 and gefunden:
                gesamt += zahlen[-1] * (60 if letzte_einheit == 3600 else 1)
            zahlen = []
            letzte_einheit = 0
        i += 1
    if zahlen and letzte_einheit > 1 and gefunden:
        gesamt += zahlen[-1] * (60 if letzte_einheit == 3600 else 1)
    if not gefunden:
        return None
    return int(round(gesamt)) if gesamt > 0 else False


def _klingel_anzahl(toks: list[str]) -> int | None:
    for i, t in enumerate(toks):
        n = _mal(t, toks[i + 1] if i + 1 < len(toks) else None)
        if n:
            return n
    return None


def _name(toks: list[str], timer_idx: int) -> str | None:
    t = toks[timer_idx]
    if t.startswith("alle") or (timer_idx > 0 and toks[timer_idx - 1] == "alle"):
        return ALLE
    # 1) Kompositum "Nudeltimer" — nie das erste Wort, nie die Anrede
    vorne = t[: t.find("timer")] if "timer" in t else ""
    if vorne and timer_idx > 0 and "stell" not in vorne and not vorne.startswith("gast"):
        return vorne
    # 2) "für die Nudeln"
    for i, w in enumerate(toks):
        if w in ("für", "fuer"):
            j = i + 1
            while j < len(toks) and toks[j] in _ARTIKEL:
                j += 1
            if j < len(toks):
                k = toks[j]
                if (k not in _KEIN_NAME and k not in _EINHEIT_S and k not in _VIERTEL
                        and _zahl(k) is None and len(k) >= 3):
                    return k
    # 3) "Eier Timer" — das getrennt geschriebene Kompositum (Qwen ohne
    #    Kontext), mit denselben Sperren wie 1)
    if t == "timer" and timer_idx > 1:
        k = toks[timer_idx - 1]
        if (k not in _KEIN_NAME and k not in _EINHEIT_S and _zahl(k) is None
                and "stell" not in k and not k.startswith("gast") and len(k) >= 2):
            return k
    # 4) "Timer Reis auf …"
    if t == "timer" and timer_idx + 1 < len(toks):
        k = toks[timer_idx + 1]
        if (k not in _KEIN_NAME and k not in _EINHEIT_S and k not in _VIERTEL
                and _zahl(k) is None and _mal(k, None) is None and len(k) >= 3):
            return k
    return None


def schluessel(name: str | None) -> str | None:
    """Vergleichsform eines Namens: 'Nudeln'/'Nudel' → 'nudel', 'Eier' → 'ei'."""
    if name is None or name == ALLE:
        return name
    w = name.lower()
    if w.endswith("ier"):
        w = w[:-2]
    elif len(w) > 4 and w[-1] == "n" and w[-2] in "lr":
        w = w[:-1]
    return w


def parse(text: str) -> TimerBefehl | None:
    s = text.lower()
    toks = _TOKEN.findall(s)
    idx = next((i for i, t in enumerate(toks) if "timer" in t or t == "eieruhr"), None)
    if idx is None or _ABLEHNEN.search(s):
        return None
    dauer = _dauer(toks)
    if dauer is False:
        return None
    name = _name(toks, idx) if toks[idx] != "eieruhr" else None
    anzeige = name if name in (None, ALLE) else name.capitalize()
    anzahl = _klingel_anzahl(toks) if _KLINGEL.search(s) else None

    if _LOESCH.search(s) and dauer is None:
        return TimerBefehl(LOESCHEN, anzeige)
    if _FRAGE.search(s) and dauer is None:
        return TimerBefehl(ABFRAGEN, anzeige)
    if dauer is None:
        if anzahl:
            return TimerBefehl(KLINGELN, anzeige, klingeln=anzahl)
        return None                     # "stell einen Timer" ohne Dauer → Brain fragt nach
    if _FRAGE.search(s) or "?" in text:
        return None                     # Frage mit Zeitangabe — nicht raten
    if _VERLAENGER.search(s):
        return TimerBefehl(VERLAENGERN, anzeige, dauer)
    if re.search(r"\bnoch\b", s):
        return TimerBefehl(NOCH, anzeige, dauer, anzahl)
    return TimerBefehl(STELLEN, anzeige, dauer, anzahl)
