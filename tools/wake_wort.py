"""Welches Wakewort die Wake-Werkzeuge meinen und woran sie es im STT-Text erkennen.

Die Werkzeuge des Nachtrainings (``wake_triage``, ``wake_corpus``,
``wake_paket``, ``wake_live_bilanz``) entscheiden an STT-Text, ob in einem
Clip das Wort zu hören war. Was dafür zählt, hängt an Wort, Sprache und
STT-Modell und steht deshalb im Manifest des Bundles, nicht im Code::

    # models/wakewords/<bundle>/manifest.yaml
    text_erkennung:
      muster: "gast|kast|gasch|aston|caston"   # Regex, irgendwo im Text (Triage)
      wortanfaenge: [gast, gas, gest, kast]    # ein Wort beginnt so (Paket, Schnitt)
      sprache: de                              # Sprache für die STT mit Wortzeiten

Fehlt der Block, gelten die ersten vier Buchstaben des Bundle-Namens als
Muster und Wortanfang und ``de`` als Sprache. Das ist ein Anfang, kein
Ergebnis: die Verhörer der eigenen STT kommen erst aus der eigenen Triage.

Welches Bundle gemeint ist: ``--bundle`` am Werkzeug, sonst das erste eigene
Bundle aus dem ``wakewords:``-Block des Profils, sonst ``gaston``.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BUNDLE_DIR = os.path.join(REPO, "models", "wakewords")
RUECKFALL = "gaston"
_SATZZEICHEN = ",.!?-:;„“\"'"


@dataclass(frozen=True)
class WortErkennung:
    bundle: str
    muster: re.Pattern
    wortanfaenge: tuple[str, ...]
    sprache: str

    def im_text(self, text: str | None) -> bool:
        """Irgendwo im Text (weit gefasst, für die Triage)."""
        return bool(self.muster.search(text or ""))

    def wort_beginnt(self, wort: str) -> bool:
        return wort.strip().lower().strip(_SATZZEICHEN).startswith(self.wortanfaenge)

    def gehoert(self, text: str | None) -> bool:
        """Ein Wort des Texts beginnt wie das Wakewort."""
        return any(self.wort_beginnt(w) for w in (text or "").lower().split())


def _manifest(bundle: str) -> dict:
    import yaml
    pfad = os.path.join(BUNDLE_DIR, bundle, "manifest.yaml")
    if not os.path.exists(pfad):
        return {}
    with open(pfad) as f:
        return yaml.safe_load(f) or {}


def laden(bundle: str | None = None) -> WortErkennung:
    bundle = bundle or standard_bundle()
    block = _manifest(bundle).get("text_erkennung") or {}
    anfang = bundle[:4].lower()
    muster = block.get("muster") or re.escape(anfang)
    worte = tuple(w.lower() for w in (block.get("wortanfaenge") or [anfang]))
    return WortErkennung(bundle=bundle, muster=re.compile(muster, re.IGNORECASE),
                         wortanfaenge=worte, sprache=str(block.get("sprache") or "de"))


def standard_bundle() -> str:
    """Erstes eigenes Bundle des Profils (Verzeichnis vorhanden), sonst gaston."""
    try:
        from voice_assistant.config import _detect_profile_name, _load_yaml
        cfg = _load_yaml()
        eintraege = cfg["profiles"][_detect_profile_name(cfg)].get("wakewords") or []
        for e in eintraege:
            b = e.get("bundle") if isinstance(e, dict) else None
            if b and os.path.isdir(os.path.join(BUNDLE_DIR, b)):
                return b
    except (Exception, SystemExit):   # keine config.yaml o. ä.: Rückfall
        pass
    return RUECKFALL


_aktiv: WortErkennung | None = None


def setzen(bundle: str | None) -> WortErkennung:
    """Vom ``main`` eines Werkzeugs: ab jetzt gilt dieses Bundle."""
    global _aktiv
    _aktiv = laden(bundle)
    return _aktiv


def aktiv() -> WortErkennung:
    global _aktiv
    if _aktiv is None:
        _aktiv = laden()
    return _aktiv
