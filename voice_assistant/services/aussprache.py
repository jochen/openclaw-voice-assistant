"""Aussprache-Liste: Woerter, die Piper falsch ausspricht, gehen als Phoneme
an die Stimme.

Piper schickt jedes Wort durch die Ausspracheregeln von espeak-ng fuer die
Sprache der Stimme. Bei Fremdwoertern und Namen geht das schief ("Sauce" als
zˈaʊkə, "Andrew Jackson" als ˈandrˌeːf jˈakzoːn). Piper liest Text in
``[[ … ]]`` direkt als Lautschrift (piper1-gpl, auch in Speaches) — diese
Liste setzt die richtige an die Stelle des Wortes, direkt vor der Synthese.
Telegram, der Brain und die Logs sehen weiter den normalen Text.

Drei Schichten, die erste gewinnt:

1. ``eigen``    — vom Nutzer direkt korrigiert (Brain-Werkzeug), privat
2. ``ergaenzt`` — vom LLM erzeugt und per STT-Rueckprobe geprueft
                  (tools/aussprache_ergaenzen.py)
3. ``grundstock`` — aus dem deutschen Wiktionary (tools/aussprache_grundstock.py)

Daneben werden unbekannte Woerter gesammelt (``faelle``): weder in der Liste
noch im Wiktionary-Wortschatz. Aus ihnen erzeugt der naechste Ergaenzungslauf
Eintraege — automatisch, sobald genug zusammengekommen ist.

Dateiformat ueberall: ``wort<TAB>phoneme[<TAB>…]``, ``#`` leitet Kommentare ein.
"""

from __future__ import annotations

import gzip
import json
import os
import re
import threading
import time
import unicodedata

# Was die deutschen Piper-Stimmen je gehoert haben: die Ausgabe von espeak-de.
# Gemessen 2026-10-06 ueber 150 000 Wiktionary-Woerter. Ein Zeichen ausserhalb
# dieses Vorrats hat zwar eine Nummer im phoneme_id_map, aber die Stimme hat
# nie gelernt, wie es klingt — Wiktionary schreibt das r als ʁ (8 100 Eintraege
# der Liste), espeak als r/ɾ. Deshalb wird jede IPA auf diesen Vorrat abgebildet.
_ESPEAK_DE = set(" abcdefhijklmnoprstuvwxyzðøŋœɐɑɒɔəɛɜɡɨɪɹɾʃʊʌʏʒʲˈˌː̧̩̃θ")
_ABBILDUNG = {"æ": "ɛ", "ɫ": "l", "ɲ": "nj", "ʎ": "lj", "β": "b", "ɕ": "ç", "ɤ": "o",
              "ɣ": "ɡ", "ʈ": "t", "ɝ": "ɜ", "ʑ": "ʒ", "ʂ": "ʃ", "ɬ": "l", "ɥ": "j",
              "ħ": "h", "ɚ": "ɐ", "ɯ": "u", "ʋ": "v", "ʉ": "u", "ɺ": "ɾ", "ɘ": "ə",
              "ʀ": "r", "ɶ": "œ", "ʙ": "b", "ɓ": "b", "g": "ɡ", "ç": "ç"}
_VOKAL = set("aeiouyøœɛɔɪʊəɐɑæɜʏɒɶɘɵɤʌɯɨʉ")
_WORT = re.compile(r"[A-Za-zÄÖÜäöüßÀ-ÿ][A-Za-zÄÖÜäöüßÀ-ÿ'\-]*")


def piper_phoneme(ipa: str) -> str:
    """Wiktionary-IPA in die Schreibweise, auf der Piper trainiert ist.

    - ʁ wie bei espeak: r vor einem Vokal (rˈoːt, maɾkˈiːrən), sonst ɾ.
    - ̯ (unsilbisch) und ʔ kommen bei espeak nicht vor: weg (aɪ̯ -> aɪ).
    - n̩/l̩ -> ən/əl, ɐ -> ɜ, ɐ̯ -> ɾ: espeaks Schreibung derselben Laute.
    - Betonung direkt vor den Vokal (kˈœ statt ˈkœ) — espeaks Stellung.
    - Alles andere ausserhalb des espeak-Vorrats: naechster Laut oder weg.
    """
    s = unicodedata.normalize("NFD", ipa.lower()).strip().strip("[]/")
    # Silbische Konsonanten und vokalisiertes r so, wie espeak sie schreibt:
    # -en als ən, -er als ɜ, das r nach Vokal (Bar, Uhr) als ɾ.
    for alt, neu in (("n̩", "ən"), ("l̩", "əl"), ("m̩", "əm"), ("ŋ̍", "əŋ"),
                     ("ɐ̯", "ɾ"), ("ɐ", "ɜ")):
        s = s.replace(alt, neu)
    zeichen = [_ABBILDUNG.get(c, c) for c in s]
    zeichen = list("".join(zeichen))
    aus, offen = [], ""
    for i, ch in enumerate(zeichen):
        if ch == "ʁ":
            folge = next((c for c in zeichen[i + 1:] if c not in "ˈˌ"), "")
            ch = "r" if folge in _VOKAL else "ɾ"
        if ch in "ˈˌ":
            offen = ch
            continue
        if ch not in _ESPEAK_DE or ch == " " and not aus:
            continue
        if offen and ch in _VOKAL:
            aus.append(offen)
            offen = ""
        aus.append(ch)
    return re.sub(r" +", " ", "".join(aus)).strip()


def _lies_tsv(pfad: str) -> dict[str, str]:
    eintraege: dict[str, str] = {}
    try:
        with open(pfad, encoding="utf-8") as f:
            for zeile in f:
                if not zeile.strip() or zeile.startswith("#"):
                    continue
                teile = zeile.rstrip("\n").split("\t")
                if len(teile) >= 2 and teile[0] and teile[1]:
                    eintraege[teile[0]] = teile[1]
    except FileNotFoundError:
        pass
    return eintraege


class Aussprache:
    """Lexikon + Fall-Sammler. Thread-sicher, Dateien werden bei Aenderung neu gelesen."""

    def __init__(
        self,
        grundstock: str | list[str],
        ergaenzt: str,
        eigen: str,
        faelle: str,
        bekannt: str = "",
    ) -> None:
        # Grundstock = alles, was im Repo liegt (Wiktionary + veroeffentlichte
        # Ergaenzungen); als eine Schicht, die Reihenfolge der Liste gilt.
        self.grundstock = [grundstock] if isinstance(grundstock, str) else list(grundstock)
        self.pfade = {"eigen": eigen, "ergaenzt": ergaenzt}
        self.faelle_pfad = faelle
        self.bekannt_pfad = bekannt
        self._lock = threading.Lock()
        self._stand: dict[str, float] = {}
        self._lexikon: dict[str, str] = {}
        self._bekannt: frozenset[str] | None = None
        self._gemeldet: set[str] = set()

        self._neu_laden()

    # --- Lexikon ----------------------------------------------------------
    def _mtimes(self) -> dict[str, float]:
        m = {}
        for p in [*self.grundstock, *self.pfade.values()]:
            try:
                m[p] = os.path.getmtime(p)
            except OSError:
                m[p] = 0.0
        return m

    def _neu_laden(self) -> None:
        lex: dict[str, str] = {}
        herkunft: dict[str, str] = {}
        for schicht, pfad in ([("grundstock", p) for p in self.grundstock]
                              + [("ergaenzt", self.pfade["ergaenzt"]), ("eigen", self.pfade["eigen"])]):
            teil = _lies_tsv(pfad)                              # spaetere gewinnen
            lex.update(teil)
            herkunft.update(dict.fromkeys(teil, schicht))
        self._lexikon = lex
        self._herkunft = herkunft
        self._stand = self._mtimes()

    def auskunft(self, wort: str) -> dict:
        """Was gilt fuer dieses Wort, und woher stammt es (fuer den Brain)."""
        with self._lock:
            self._aktuell()
            ph = self.nachschlagen(wort)
            schluessel = wort if wort in self._lexikon else (
                wort[:1].lower() + wort[1:] if wort[:1].isupper() else wort[:1].upper() + wort[1:])
            return {"wort": wort, "phoneme": ph,
                    "quelle": self._herkunft.get(schluessel) if ph else None,
                    "espeak": espeak_de(wort)}

    def _aktuell(self) -> None:
        if self._mtimes() != self._stand:
            self._neu_laden()

    def nachschlagen(self, wort: str) -> str | None:
        lex = self._lexikon
        if wort in lex:
            return lex[wort]
        # Satzanfang: "Sauce" steht drin, "sauce" nicht — und umgekehrt fuer
        # kleingeschriebene Woerter am Satzanfang.
        alt = wort[:1].lower() + wort[1:] if wort[:1].isupper() else wort[:1].upper() + wort[1:]
        return lex.get(alt)

    def anwenden(self, text: str) -> str:
        """Text mit ``[[Phonemen]]`` statt der bekannten Problemwoerter."""
        with self._lock:
            self._aktuell()
            if not self._lexikon:
                return text

            def ersetze(m: re.Match) -> str:
                wort = m.group(0)
                ph = self.nachschlagen(wort)
                if ph:
                    return f"[[{ph}]]"
                if "-" in wort:             # Koffein-Gehalt, Wake-on-LAN-Paket
                    teile = wort.split("-")
                    return "-".join(f"[[{p}]]" if (p := self.nachschlagen(t)) else t for t in teile)
                return wort

            return _WORT.sub(ersetze, text)

    # --- Faelle -----------------------------------------------------------
    def _bekannte(self) -> frozenset[str]:
        if self._bekannt is None:
            try:
                with gzip.open(self.bekannt_pfad, "rt", encoding="utf-8") as f:
                    self._bekannt = frozenset(w.rstrip("\n") for w in f)
            except (OSError, ValueError):
                self._bekannt = frozenset()
        return self._bekannt

    def _ist_bekannt(self, wort: str, bekannt: frozenset[str]) -> bool:
        def da(w: str) -> bool:
            return w in bekannt or w.capitalize() in bekannt

        if wort in bekannt or da(wort.lower()):
            return True
        # Zusammensetzung aus bekannten Teilen, auch aus mehr als zwei
        # (Kuechen·arbeits·platten·licht), mit Fugen-s: die spricht espeak wie
        # ihre Teile, kein Fall.
        w = wort.lower()
        teilbar = [False] * (len(w) + 1)
        teilbar[0] = True
        for ende in range(3, len(w) + 1):
            for anfang in range(0, ende - 2):
                if not teilbar[anfang]:
                    continue
                stueck = w[anfang:ende]
                if da(stueck) or (stueck[:1] == "s" and anfang > 0 and len(stueck) > 3 and da(stueck[1:])):
                    teilbar[ende] = True
                    break
        return teilbar[len(w)]

    def fall_sammeln(self, text: str) -> int:
        """Unbekannte Woerter aus einem gesprochenen Satz vormerken. Gibt die
        Zahl neuer Faelle zurueck. Ohne Wortschatz-Datei wird nichts gesammelt
        — sonst waere jedes Wort ein Fall."""
        bekannt = self._bekannte()
        if not bekannt or not self.faelle_pfad:
            return 0
        if getattr(_faden, "ohne_sammeln", False):
            return 0
        neu = []
        with self._lock:
            for w in (t.strip("'") for m in _WORT.finditer(text) for t in m.group(0).split("-")):
                if len(w) < 3 or w in self._gemeldet or self.nachschlagen(w):
                    continue
                if w.isupper() and len(w) <= 5:     # Abkuerzungen buchstabiert espeak
                    continue
                if self._ist_bekannt(w, bekannt):
                    continue
                self._gemeldet.add(w)
                neu.append(w)
            if neu:
                os.makedirs(os.path.dirname(self.faelle_pfad), exist_ok=True)
                with open(self.faelle_pfad, "a", encoding="utf-8") as f:
                    for w in neu:
                        f.write(json.dumps({"wort": w, "satz": text[:300],
                                            "ts": time.strftime("%Y-%m-%dT%H:%M:%S")},
                                           ensure_ascii=False) + "\n")
        return len(neu)

    # --- Direkte Korrektur (Brain-Werkzeug) -------------------------------
    def eigen_setzen(self, wort: str, phoneme: str) -> None:
        pfad = self.pfade["eigen"]
        with self._lock:
            eintraege = _lies_tsv(pfad)
            eintraege[wort] = phoneme
            os.makedirs(os.path.dirname(pfad), exist_ok=True)
            tmp = pfad + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write("# Eigene Aussprache-Korrekturen (privat, nicht im Repo)\n")
                for w in sorted(eintraege):
                    f.write(f"{w}\t{eintraege[w]}\n")
            os.replace(tmp, pfad)
            self._neu_laden()

    def eigen_loeschen(self, wort: str) -> bool:
        pfad = self.pfade["eigen"]
        with self._lock:
            eintraege = _lies_tsv(pfad)
            if wort not in eintraege:
                return False
            del eintraege[wort]
            with open(pfad, "w", encoding="utf-8") as f:
                f.write("# Eigene Aussprache-Korrekturen (privat, nicht im Repo)\n")
                for w in sorted(eintraege):
                    f.write(f"{w}\t{eintraege[w]}\n")
            self._neu_laden()
            return True


def espeak_de(text: str) -> str:
    """So spricht Piper den Text OHNE Liste (espeak-ng, Regeln "de")."""
    global _espeak
    if _espeak is None:
        from piper.phonemize_espeak import EspeakPhonemizer
        _espeak = EspeakPhonemizer()
    return "".join(sum(_espeak.phonemize("de", text), []))


def aus_umschreibung(umschreibung: str) -> str:
    """Deutsche Umschreibung ("Sohße", "Ändru Dschäckßn") als Phoneme.

    Der direkte Weg fuer Korrekturen per Sprache: wer die richtige Aussprache
    sagen will, umschreibt sie in deutscher Schreibung — und genau die liest
    espeak-de richtig. Im Blindtest am 2026-10-06 gewann diese deutsche
    Annaeherung in vier von sechs Faellen.
    """
    return espeak_de(umschreibung)


_espeak = None
_faden = threading.local()


def ohne_sammeln(funktion):
    """``funktion`` so ausfuehren, dass dabei keine Faelle gesammelt werden.

    Fuer die Bestaetigung "Ich habe verstanden: …": sie wiederholt das rohe
    STT-Transkript, und dessen Verhoerer ("Callsender") sind kein Aussprache-,
    sondern ein Erkennungsproblem. Thread-lokal, weil die Bestaetigung im
    eigenen Thread satzweise synthetisiert — ein Praefix-Vergleich saehe nur
    den ersten Satz.
    """
    def _lauf(*args, **kwargs):
        _faden.ohne_sammeln = True
        try:
            return funktion(*args, **kwargs)
        finally:
            _faden.ohne_sammeln = False
    return _lauf

_REPO_DATEN = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "data", "aussprache")


def pfade(sprache: str = "de") -> dict:
    """Wo die Schichten einer Sprache liegen. Repo = oeffentlich, Workspace = privat."""
    from voice_assistant.config import VOICE_DIR
    ws = os.path.join(VOICE_DIR, "aussprache")
    return {
        "grundstock": [os.path.join(_REPO_DATEN, f"{sprache}_wiktionary.tsv"),
                       os.path.join(_REPO_DATEN, f"{sprache}_ergaenzt.tsv")],
        "ergaenzt": os.path.join(ws, f"{sprache}_ergaenzt.tsv"),
        "eigen": os.path.join(ws, f"{sprache}_eigen.tsv"),
        "faelle": os.path.join(ws, f"{sprache}_faelle.jsonl"),
        "bekannt": os.path.join(ws, f"bekannt_{sprache}.txt.gz"),
    }


def fuer_sprache(sprache: str = "de") -> "Aussprache":
    return Aussprache(**pfade(sprache))


# --- Prozessweite Instanz -------------------------------------------------
_instanz: Aussprache | None = None


def einrichten(a: Aussprache | None) -> None:
    global _instanz
    _instanz = a


def aktiv() -> Aussprache | None:
    return _instanz


def fuer_piper(text: str, modell: str | None = None) -> str:
    """Einstieg fuer die TTS: nur bei Piper-Stimmen ersetzen — eine andere
    Engine laese ``[[…]]`` woertlich vor."""
    a = _instanz
    if a is None or (modell and "piper" not in modell.lower()):
        return text
    try:
        a.fall_sammeln(text)
        return a.anwenden(text)
    except Exception as e:      # Aussprache darf die Ausgabe nie verhindern
        print(f"⚠️  Aussprache: {type(e).__name__}: {e}")
        return text
