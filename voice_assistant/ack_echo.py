"""Gastons eigenes "Ja?" im Transkript erkennen und entfernen.

Befund (2026-11-08): die Echo-Unterdrückung des XVF3800 nimmt das kurze
"Ja?" nach dem Wakewort nicht ganz weg. In jeder untersuchten Aufnahme mit
gespieltem "Ja?" steht 0,3–0,9 s nach dem Abspielbefehl ein Pegelstoß
(bis RMS 8491), den Qwen als "Ja." liest. Ist sonst nichts gesagt worden,
ging "Ja." an den Brain (5 Fälle 28.10.–04.11.); sonst hing es am Satz
("Ja. Ich wollte nur mal kurz testen …", "Gaston, schalte das Tischlicht
ein. Ja.").

Die Regel ist am Text, nicht am Audio: das Echo-Fenster stummzuschalten
hieße, Qwen eine Stille zu geben, und auf Stille erfindet Qwen Sätze ("Ich
habe keine Ahnung.", gemessen an denselben Aufnahmen). Angewendet wird sie
nur im Erst-Turn, wenn in genau dieser Aufnahme das "Ja?" lief: ein "Ja"
ohne vorherige Frage trägt dort keinen Inhalt.

Das ist das zweite Netz. Das erste ist der VAD (assistant._ACK_ECHO_SEC):
das Echo zählt nicht als Sprache, und eine Aufnahme ohne Sprache nach dem
"Ja?" kommt gar nicht erst zur STT. Nötig, weil Qwen dasselbe Echo auch
"So. Da." oder "Danke." liest (Live-Test 2026-11-08 12:10).
"""

from __future__ import annotations

import re

_SATZ = re.compile(r"[^.!?]+[.!?]*")
_JA_SAETZE = {("ja",), ("ja", "ja"), ("jaja",)}


def _ist_ja_satz(satz: str) -> bool:
    woerter = tuple(re.sub(r"[^\wäöüß ]", " ", satz.lower()).split())
    return woerter in _JA_SAETZE


def ohne_ack_echo(text: str) -> str:
    """Reine "Ja."-Sätze am Anfang und am Ende entfernen.

    Leerer Rückgabewert: das Transkript war nur das Echo. Ein "Ja" mitten im
    Satz oder mit weiteren Wörtern ("Ja, stopp.") bleibt stehen.
    """
    saetze = [s.strip() for s in _SATZ.findall(text or "") if s.strip()]
    n = len(saetze)
    while saetze and _ist_ja_satz(saetze[0]):
        saetze.pop(0)
    while saetze and _ist_ja_satz(saetze[-1]):
        saetze.pop()
    return text if len(saetze) == n else " ".join(saetze)
