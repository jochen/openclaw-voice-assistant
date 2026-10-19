"""„Gaston, stell dich taub“ fest aus dem Transkript lesen — ohne Modell.

Schnellweg wie Timer und Musik: greift nur bei eindeutigen Sätzen, alles
andere geht an den Brain (der hat das MCP-Werkzeug ``mikro``). Wieder
einschalten per Sprache geht nur über einen Satelliten (Handy): das stumme
Haupt-Mikrofon hört den Satz nicht.

Regeln:

- **Stumm**: „taub“ mit stell/sei/mach/schalt, „Mikro(fon) aus/stumm“,
  „hör weg“, „hör nicht mehr zu“.
- **Wieder an**: „hör wieder zu“, „Mikro(fon) wieder an/ein“, „nicht mehr
  taub“.
- **Dauer** wie beim Timer („für eine halbe Stunde“). Keine Dauer → der
  Standard aus dem Profil (Jochen 2026-10-19: zwei Stunden, ein vergessener
  Mute macht Gaston still tot). „bis ich dich wieder anmache“, „bis auf
  Weiteres“, „dauerhaft“ → ohne Zeitgrenze. Eine Uhrzeit („bis 22 Uhr“)
  oder eine unklare Dauer → None, das rechnet der Brain.
- **Keine Frage, keine Verneinung**: „bist du taub?“, „stell dich nicht taub“,
  „warum ist das Mikro aus“. Ausnahme: das „nicht mehr“ der Muster selbst.
- **Kurz**: höchstens ``_MAX_WOERTER`` Wörter.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from voice_assistant.services.timer_parser import _TOKEN, _dauer


@dataclass
class MikroBefehl:
    stumm: bool
    sekunden: int | None = None     # None = Standard (stumm) bzw. egal (an)
    unbegrenzt: bool = False


_MAX_WOERTER = 16

_MIKRO = r"(?:mikro|mikrofon|mikrophon|mic)s?"
_AN = re.compile(
    rf"\bhör\w*\s+(?:\w+\s+){{0,2}}wieder\s+zu\b|\bnicht\s+mehr\s+taub\b"
    rf"|\b{_MIKRO}\s+(?:\w+\s+){{0,2}}(?:wieder\s+)?(?:an|ein)(?:schalten|machen)?\b"
    rf"|\b(?:schalt|mach)\w*\s+(?:\w+\s+){{0,2}}{_MIKRO}\s+(?:\w+\s+)?(?:wieder\s+)?(?:an|ein)\b")
_STUMM = re.compile(
    rf"\b(?:stell|sei|mach|schalt)\w*\s+(?:\w+\s+){{0,6}}taub\b|\btaub\s+(?:stellen|schalten|machen)\b"
    rf"|\b{_MIKRO}\s+(?:\w+\s+){{0,2}}(?:aus|stumm|ab)(?:schalten|machen)?\b"
    rf"|\b(?:schalt|mach)\w*\s+(?:\w+\s+){{0,2}}{_MIKRO}\s+(?:\w+\s+)?(?:aus|stumm|ab)\b"
    rf"|\bhör\w*\s+(?:\w+\s+){{0,2}}(?:weg|nicht\s+mehr\s+zu)\b")
_UNBEGRENZT = re.compile(
    r"\bbis\s+(?:ich|du|wir|man|jemand)\b.*\b(?:wieder|an|ein)\w*|\bbis\s+auf\s+weiteres\b"
    r"|\bdauerhaft\b|\bfür\s+immer\b|\bohne\s+zeit\w*")
_UHRZEIT = re.compile(r"\buhr\b|\bbis\s+(?:morgen|heute|nachher|später)\b|\d+[:.]\d+")
_FRAGE = {"bist", "warum", "wieso", "weshalb", "wie", "wann", "ob", "wer", "hast", "ist"}
_VERNEINUNG = re.compile(r"\b(?:nicht|kein\w*|nie)\b")


def parse(text: str) -> MikroBefehl | None:
    if not text:
        return None
    klein = text.lower()
    woerter = _TOKEN.findall(klein)
    if not woerter or len(woerter) > _MAX_WOERTER or set(woerter) & _FRAGE:
        return None
    an, stumm = _AN.search(klein), _STUMM.search(klein)
    if an and stumm and "nicht mehr taub" in an.group(0):
        stumm = None                    # „sei nicht mehr taub“
    if bool(an) == bool(stumm):
        return None
    treffer = an or stumm
    # Verneinung ausserhalb des Musters: „stell dich nicht taub“
    rest = klein[:treffer.start()] + " " + klein[treffer.end():]
    if _VERNEINUNG.search(rest) or (stumm and _VERNEINUNG.search(treffer.group(0))
                                    and "nicht mehr zu" not in treffer.group(0)):
        return None
    if an:
        return MikroBefehl(stumm=False)
    if _UNBEGRENZT.search(klein):
        return MikroBefehl(stumm=True, unbegrenzt=True)
    if _UHRZEIT.search(klein):
        return None
    dauer = _dauer(woerter)
    if dauer is False:
        return None
    return MikroBefehl(stumm=True, sekunden=dauer)
