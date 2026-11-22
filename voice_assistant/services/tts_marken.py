"""Klang-Marken in Antworten (``tts_marken``, seit 2026-11-22).

Der Brain darf einzelne Sätze mit deutschen Marken versehen — ein Lacher, ein
Atemzug, eine Betonung. Ein Antwort-Dienst mit CosyVoice3 setzt sie als
Geräusch bzw. Betonung um; jede andere Stimme (Piper, Speaches) bekommt sie
ersatzlos entfernt, damit nichts wörtlich vorgelesen wird.

Gehört und ausgewählt 2026-11-22 (Hörtest, Stimme eines Hörbuch-Ausschnitts):

=================  ==========================  ======================
Marke              CosyVoice3                  Ergebnis
=================  ==========================  ======================
``[lachen]``       ``[laughter]``              gut hörbar
``<lachend>…``     ``<laughter>…</laughter>``  lachend gesprochen
``[atmen]``        ``[breath]``                gut hörbar
``[luftholen]``    ``[quick_breath]``          gut hörbar
``[seufzen]``      ``[sigh]``                  unsicher
``[husten]``       ``[cough]``                 sehr gut
``[hm]``           ``[mn]``                    sehr gut
``[psst]``         ``[hissing]``               sehr gut
``[äh]``           ``[vocalized-noise]``       gut hörbar
``[schnalzen]``    ``[lipsmack]``              klang nach Schnalzer
``**Wort**``       ``<strong>Wort</strong>``   sehr gut
=================  ==========================  ======================

Nicht übernommen (nichts zu hören): ``[clucking]``, ``[noise]``. Anweisungen
an CosyVoice („sag es fröhlich“, ``inference_instruct2``) brachten einen
starken englischen Akzent — deshalb nur Marken im Text.

Ablauf: ``clean_for_tts`` (services/tts.py) entfernt Markdown und alle
Sonderzeichen. Vorher ersetzt ``schuetzen`` die Marken durch Platzhalter aus
Buchstaben, danach macht ``wiederherstellen`` daraus die lesbare deutsche Form
(``[lachen]``, ``**Wort**``, ``<lachend>…</lachend>``). Erst der Dienst
entscheidet: ``fuer_cosyvoice`` oder ``entfernen``.
"""

from __future__ import annotations

import re

GERAEUSCHE = {
    "lachen": "[laughter]",
    "atmen": "[breath]",
    "luftholen": "[quick_breath]",
    "seufzen": "[sigh]",
    "husten": "[cough]",
    "hm": "[mn]",
    "psst": "[hissing]",
    "äh": "[vocalized-noise]",
    "schnalzen": "[lipsmack]",
}

# Platzhalter nur aus Buchstaben: übersteht den Zeichenfilter und die
# Verbalisierung in clean_for_tts.
_P = "qxmarkeqx"
_GERAEUSCH_RE = re.compile(r"\[\s*(" + "|".join(map(re.escape, GERAEUSCHE)) + r")\s*\]", re.IGNORECASE)
_BETONT_RE = re.compile(r"\*\*\s*([^*\n]+?)\s*\*\*")
_LACHEND_RE = re.compile(r"<\s*lachend\s*>(.+?)<\s*/\s*lachend\s*>", re.IGNORECASE | re.DOTALL)
_PLATZHALTER_RE = re.compile(rf"\s*{_P}(\w+?){_P}\s*")


def schuetzen(text: str) -> str:
    """Vor dem Zeichenfilter: Marken → Platzhalter."""
    text = _LACHEND_RE.sub(lambda m: f" {_P}lachendauf{_P} {m.group(1)} {_P}lachendzu{_P} ", text)
    text = _BETONT_RE.sub(lambda m: f" {_P}betontauf{_P} {m.group(1)} {_P}betontzu{_P} ", text)
    return _GERAEUSCH_RE.sub(lambda m: f" {_P}{m.group(1).lower()}{_P} ", text)


def wiederherstellen(text: str) -> str:
    """Nach dem Zeichenfilter: Platzhalter → lesbare deutsche Marken."""
    def ersetzen(m: re.Match) -> str:
        name = m.group(1)
        return {"betontauf": " **", "betontzu": "** ", "lachendauf": " <lachend>",
                "lachendzu": "</lachend> "}.get(name, f" [{name}] ")
    text = _PLATZHALTER_RE.sub(ersetzen, text)
    return _aufraeumen(text)


def _aufraeumen(text: str) -> str:
    text = re.sub(r"\s{2,}", " ", text)
    text = re.sub(r"\s+([,.!?;:])", r"\1", text)
    return text.strip()


def hat_marken(text: str) -> bool:
    return bool(_GERAEUSCH_RE.search(text) or _BETONT_RE.search(text) or _LACHEND_RE.search(text))


def fuer_cosyvoice(text: str) -> str:
    """Deutsche Marken → CosyVoice3-Tags."""
    text = _LACHEND_RE.sub(lambda m: f"<laughter>{m.group(1).strip()}</laughter>", text)
    text = _BETONT_RE.sub(lambda m: f"<strong>{m.group(1)}</strong>", text)
    return _GERAEUSCH_RE.sub(lambda m: GERAEUSCHE[m.group(1).lower()], text)


def entfernen(text: str) -> str:
    """Marken ersatzlos weg (Piper, Speaches, Mitschnitt-Text)."""
    text = _LACHEND_RE.sub(lambda m: m.group(1), text)
    text = _BETONT_RE.sub(lambda m: m.group(1), text)
    text = _GERAEUSCH_RE.sub(" ", text)
    text = _aufraeumen(text)
    # stand die Marke vorn ("[hm], mal sehen"), bleibt kein Komma am Anfang
    return re.sub(r"^[,;:]\s*", "", text)
