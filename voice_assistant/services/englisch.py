"""Englische Stellen in deutschem Text finden (Musiktitel, Interpreten).

Die meisten Titel und Interpreten sind englisch, aber nicht alle, und jeden
einzeln in die Aussprache-Liste zu schreiben geht nicht (Jochen, 2026-10-15).
Deshalb eine Regel ohne Modell, Wort fuer Wort gegen zwei Wortlisten:

    E  nur in der englischen Liste   ("rhapsody", "days", "of")
    D  nur in der deutschen          ("durch", "bisschen", "läuft")
    b  in beiden                     ("wind", "die", "angel", "queen")
    ?  in keiner                     (Namen: "Rammstein", "Enya")

Englisch ist eine Folge von E-Woertern samt der b-Woerter direkt daneben
("Wind of Change" -> b E E, alles englisch) — ausser deutschen
Funktionswoertern am Rand ("von den Scorpions": "den" ist auch englisch). Alles
andere bleibt, wie es ist, also deutsch. Unbekannte Woerter (?) uebernehmen
Englisch vom Nachbarn, solange kein D-Wort daneben steht ("Horns Of
Baphomet", "Witchdoctor Woman") — in Titeln sind das fast immer Namen und
absichtlich falsch geschriebene Woerter. Der teure Fehler ist ein deutscher Titel, der englisch klingt;
ein uebersehener englischer klingt nur wie heute.

Wortlisten: die Debian-Pakete wamerican und wngerman (/usr/share/dict).
Fehlt eine, findet die Regel nichts.
"""

from __future__ import annotations

import re

_LISTE_EN = "/usr/share/dict/american-english"
_LISTE_DE = "/usr/share/dict/ngerman"

WORT = re.compile(r"[A-Za-zÄÖÜäöüßÀ-ÿ]+(?:['´’][A-Za-z]+)?")

# Deutsche Funktionswoerter, die auch englisch sind: am Rand einer englischen
# Stelle bleiben sie deutsch, mittendrin ("Ace In The Hole") nicht.
_DE_RAND = frozenset("""
    an also am bin die den der des denn so man was war hat hand in im um bitte
    rang will wo wer wie mit fast als bald
""".split())

_listen: tuple[frozenset[str], frozenset[str]] | None = None


def _lies(pfad: str) -> frozenset[str]:
    try:
        with open(pfad, encoding="utf-8", errors="ignore") as f:
            return frozenset(w.strip().lower() for w in f if w.strip())
    except OSError:
        return frozenset()


def listen() -> tuple[frozenset[str], frozenset[str]]:
    global _listen
    if _listen is None:
        _listen = (_lies(_LISTE_EN), _lies(_LISTE_DE))
    return _listen


def klasse(wort: str) -> str:
    """E, D, b oder ? fuer ein Wort (siehe Modul-Docstring)."""
    en, de = listen()
    w = wort.lower().replace("´", "'").replace("’", "'")
    im_en, im_de = w in en, w in de
    if im_en and not im_de:
        return "E"
    if im_de and not im_en:
        return "D"
    return "b" if im_en else "?"


def englische_stellen(text: str) -> list[tuple[int, int]]:
    """(Anfang, Ende) jeder englischen Stelle in ``text``."""
    woerter = [(m.start(), m.end(), klasse(m.group())) for m in WORT.finditer(text)]
    englisch = [k == "E" for _, _, k in woerter]
    # b-Woerter neben einer E-Folge gehoeren dazu, in beide Richtungen;
    # deutsche Funktionswoerter nur zwischen zwei englischen.
    n = len(woerter)
    for reihenfolge in (range(n), reversed(range(n))):
        vorher = False
        for i in reihenfolge:
            if woerter[i][2] == "b" and vorher and not _am_rand(text, woerter, i):
                englisch[i] = True
            vorher = englisch[i]
    for i in range(1, n - 1):
        if woerter[i][2] == "b" and englisch[i - 1] and _e_folgt(woerter, englisch, i):
            englisch[i] = True
    # Unbekannte Woerter: englisch, wenn ein Nachbar englisch ist und keiner deutsch.
    geaendert = True
    while geaendert:
        geaendert = False
        for i in range(n):
            if englisch[i] or woerter[i][2] != "?":
                continue
            nachbarn = [j for j in (i - 1, i + 1) if 0 <= j < n and _benachbart(text, woerter, i, j)]
            if (any(englisch[j] for j in nachbarn)
                    and not any(woerter[j][2] == "D" for j in nachbarn)):
                englisch[i] = geaendert = True
    stellen: list[tuple[int, int]] = []
    for (a, e, _), en in zip(woerter, englisch):
        if not en:
            continue
        # Zwischen zwei englischen Woertern nur Leerraum/Satzzeichen: verbinden.
        if stellen and englisch_luecke(text[stellen[-1][1]:a]):
            stellen[-1] = (stellen[-1][0], e)
        else:
            stellen.append((a, e))
    return stellen


def englisch_luecke(zwischen: str) -> bool:
    return not WORT.search(zwischen) and not re.search(r"[.!?:;]", zwischen)


def _am_rand(text: str, woerter, i: int) -> bool:
    """Funktionswort, das deutsch bleibt, solange es nicht eingeschlossen ist."""
    return text[woerter[i][0]:woerter[i][1]].lower() in _DE_RAND


def _e_folgt(woerter, englisch, i: int) -> bool:
    """Folgt nach Wort i (ueber weitere b-Woerter) ein englisches?"""
    for j in range(i + 1, len(woerter)):
        if englisch[j]:
            return True
        if woerter[j][2] != "b":
            return False
    return False


def _benachbart(text: str, woerter, i: int, j: int) -> bool:
    a, b = sorted((i, j))
    return englisch_luecke(text[woerter[a][1]:woerter[b][0]])
