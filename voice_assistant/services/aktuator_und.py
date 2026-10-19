"""Doppelbefehle mit „und“: den Satz zerlegen, jeden Teil einzeln entscheiden.

„Gaston, schalte das Abendlicht ein und mach den Wohnzimmerrollo zu.“ Der
Aktuator bildet einen Intent je Satz. Bei zwei Befehlen schaltete er nicht
nur halb, sondern mischte oft Ziel und Aktion der beiden Haelften: „Küchen-
licht aus und das Flurlicht an“ → Flurlicht aus (tools/aktuator_und_test.py,
Nulllinie 2026-10-19: 34 FALSCH von 253).

Ablauf:

1. ``zerlegen()`` trennt fest, ohne Modell, an „und“ (auch „und dann“,
   „. Und“), an Kommas und Satzenden. Anrede vorne und reine Fuellteile
   („okay“, „bitte“, „äh“) fallen weg. Ein Teil ohne Aktionswort leiht sich
   die Aktion vom Ende des naechsten: „schalt Tischlicht und Sofalicht aus“
   → „schalt Tischlicht aus“ + „Sofalicht aus“. Namen aus den capabilities,
   die selbst ein „und“ oder Komma enthalten, werden nicht zerschnitten.
2. ``entscheiden()`` schickt jeden Teil durch dieselbe Kette wie einen
   ganzen Satz (aktuator_schatten.entscheiden, also auch Regel A je Teil).
3. Geschaltet wird nur, wenn ALLE Teile ausfuehrbar sind und kein Ziel
   doppelt vorkommt. Sonst geht der ganze Satz an den Brain — auch nicht
   als ein Satz an den Aktuator, denn genau dort entstanden die Mischungen.
   Keine Rueckfrage zu einer Haelfte: das waere ein Dialog ueber einen
   halben Satz.

Kein Doppelbefehl (``zerlegen`` → None): kein „und“ und keine zwei Teile mit
Aktionswort, oder weniger als zwei Teile uebrig. Dann laeuft alles wie
bisher. Nur Kommas ohne „und“ zaehlen erst ab zwei Teilen mit Aktionswort,
sonst zerfiele „Okay, mach das Licht an“.

Regelaenderungen nur gegen tools/aktuator_und_test.py (dort auch
``--einzel``: das Tor-Set darf durch die Zerlegung nicht schlechter werden).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from voice_assistant.services.actuator import VERDICT_AUSFUEHRBAR

# Anrede samt STT-Verhoerern („Gastau“, „Gestor“, „Gast auf“).
_ANREDE = re.compile(r"^\s*(?:hey\s+|hallo\s+|ok(?:ay)?\s+)?(?:gast|gäst|kast|gest)\w*(?:\s+auf\b)?"
                     r"\s*[,.!?]?\s*", re.I)
_TRENNER = re.compile(r"\s*[,.;:!?]?\s+(?:und\s+)+(?:dann\s+|danach\s+)?|\s*[,;]\s+|[.!?]+\s+",
                      re.I)
_HAT_UND = re.compile(r"\bund\b", re.I)
_AKTION = re.compile(
    r"\b(?:an|aus|ein|auf|zu|hoch|rauf|runter|herunter|hinunter|dazu|hinzu|stumm)"
    r"(?:schalten|machen|fahren|ziehen|drehen)?\b|\bab(?:schalten|machen)\b"
    r"|\b(?:öffne|öffnen|schließe|schließen|aktiviere|aktivieren|starte|starten|stopp?|raus)\b"
    r"|\d+\s*(?:%|prozent|grad)", re.I)
# Die Aktion am Ende eines Teils, zum Ausleihen nach vorne.
_AKTION_HINTEN = re.compile(
    r"((?:auf\s+)?\d+(?:[,.]\d+)?\s*(?:%|prozent|grad)?"
    r"|\b(?:an|aus|ein|auf|zu|hoch|rauf|runter)(?:schalten|machen|fahren|ziehen)?)"
    r"(?:\s+(?:bitte|mal|wieder|bitte\s+wieder))?\s*[.!?]*\s*$", re.I)
_FUELL = {"und", "okay", "ok", "ja", "nein", "nee", "äh", "ähm", "öh", "hm", "bitte", "also", "gut",
          "danke", "so", "dann", "danach", "mal", "hey", "noch", "auch", "jetzt", "gleich",
          "schön", "super", "prima"}
# Bedingung, Zeitpunkt, Verneinung: kein sofortiger Doppelbefehl, der ganze
# Satz gehoert dem Brain („wenn es dunkel wird, mach … an und … zu“).
_BRAIN = re.compile(r"\b(?:wenn|falls|sobald|bevor|nachdem|sondern|nicht|kein(?:e|en|em|er|es)?|später|morgen"
                    r"|in\s+\w+\s+(?:sekunden|minuten|stunden)|um\s+\d+(?:[:.]\d+)?\s*uhr)\b", re.I)
_OHNE_VERB = re.compile(r"(?:[A-ZÄÖÜ]|(?:das|die|den|der|dem|alle|auch|noch)\b)")
_NOMEN = re.compile(r"(?<!^)\b[A-ZÄÖÜ]")
_WORT = re.compile(r"[\wäöüß%]+", re.I)
_MASKE = "⁣"   # unsichtbares Trennzeichen statt Leerzeichen in geschuetzten Namen


@dataclass
class Zerlegung:
    teile: list[str]
    entscheidungen: list = field(default_factory=list)   # aktuator_schatten.Entscheidung je Teil
    grund: str | None = None        # warum nicht ausfuehrbar (nur Log)

    @property
    def ausfuehrbar(self) -> bool:
        return bool(self.entscheidungen) and self.grund is None

    @property
    def intents(self) -> list[dict]:
        return [e.intent for e in self.entscheidungen]

    @property
    def ms(self) -> float:
        return sum(e.ms for e in self.entscheidungen)


def _schuetzen(text: str, namen: list[str]) -> str:
    for name in namen:
        if _HAT_UND.search(name) or "," in name:
            muster = re.compile(re.escape(name), re.I)
            text = muster.sub(lambda m: m.group(0).replace(" ", _MASKE), text)
    return text


def _fuelle(teil: str) -> bool:
    woerter = [w.lower() for w in _WORT.findall(teil)]
    return not woerter or all(w in _FUELL for w in woerter)


def zerlegen(text: str, namen: list[str] = ()) -> list[str] | None:
    """Die Teile eines Doppelbefehls, oder None (kein Doppelbefehl)."""
    if not text:
        return None
    rest = _schuetzen(_ANREDE.sub("", text.strip()), list(namen))
    hat_und = bool(_HAT_UND.search(rest))
    teile = [t.strip(" ,.;:!?") for t in _TRENNER.split(rest)]
    teile = [t.replace(_MASKE, " ") for t in teile if t and not _fuelle(t)]
    teile = [t for t in teile if not _ANREDE.fullmatch(t + " ")]
    if len(teile) < 2:
        return None
    mit_aktion = [bool(_AKTION.search(t)) for t in teile]
    if not any(mit_aktion) or (not hat_und and sum(mit_aktion) < 2):
        return None
    # Aktion vom Ende des naechsten Teils leihen (von hinten nach vorne,
    # damit „A, B und C aus“ durchreicht).
    for i in range(len(teile) - 2, -1, -1):
        # Nur an einen Teil mit Geraet (Nomen): „mach mal das, das Tischlicht
        # aus“ soll nicht „mach mal das aus“ werden. Und nur von einem Teil
        # ohne eigenes Verb vorne („die Kloheizung auf 22 Grad“), sonst wird
        # aus „…Tischlicht raus. Und stell die Gästeheizung auf 22 Grad“ ein
        # Tischlicht auf 22 Grad.
        if (not mit_aktion[i] and mit_aktion[i + 1] and _NOMEN.search(teile[i])
                and _OHNE_VERB.match(teile[i + 1])):
            m = _AKTION_HINTEN.search(teile[i + 1])
            if m:
                teile[i] = f"{teile[i]} {m.group(1)}"
                mit_aktion[i] = True
    return teile


def entscheiden(actuator, text: str) -> Zerlegung | None:
    """None = kein Doppelbefehl, der Satz geht den gewohnten Weg."""
    from voice_assistant.services import aktuator_schatten
    with actuator._lock:
        digest = dict(actuator.digest or {})
    namen = [n for z in digest.values() for n in (z.get("namen") or [])]
    teile = zerlegen(text, namen)
    if teile is None:
        return None
    z = Zerlegung(teile)
    m = _BRAIN.search(text)
    if m:
        z.grund = f"„{m.group(0)}“ im Satz"
        return z
    for teil in teile:
        e = aktuator_schatten.entscheiden(actuator, teil)
        z.entscheidungen.append(e)
        if e.verdict != VERDICT_AUSFUEHRBAR:
            z.grund = f"Teil „{teil}“: {e.ausgang}" + (f" ({e.grund})" if e.grund else "")
            return z
    ziele = [e.intent.get("ziel") for e in z.entscheidungen]
    if len(set(ziele)) != len(ziele):
        z.grund = f"Ziel doppelt: {', '.join(ziele)}"
    return z
