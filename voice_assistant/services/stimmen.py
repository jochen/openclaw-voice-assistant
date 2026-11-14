"""Eigene Stimmen für den Antwort-Dienst (CosyVoice), lokal verwaltet.

Jeder Assistent hält seine Stimmen selbst (Jochen 2026-11-09: „die Verwaltung
lokal, die Stimme wird bei Auswahl an den Dienst gegeben“). Der Dienst ist nur
ein Rechenwerk: kennt er eine Stimme nicht (HTTP 404), lädt der Assistent sie
hoch und fragt noch einmal. Ein Neustart des Dienstes ist damit egal, und die
Häuser sind getrennt: jede Kennung beginnt mit dem Namensraum des Assistenten,
Gaston sieht nie die Stimmen aus dem Fablab.

Ablage je Stimme (Ordner ``<ordner>/<name>/``):

- ``probe.wav`` — die aufbereitete Probe (Lautheit, Länge; siehe stimm_probe)
- ``probe.txt`` — was in der Probe gesagt wird (CosyVoice braucht den Text)
- ``meta.json`` — optional: ``beschreibung``, ``quelle``, ``angelegt``,
  ``freigegeben`` (fehlt = freigegeben; ``false`` = nicht wählbar)

Dazu ``<ordner>/_standard.json``: die Standard-Stimme „für alle“, per
``voice_set_voice`` mit ``fuer_alle`` gesetzt. Sie gewinnt gegen
``antwort_tts_voice`` im Profil, solange es die Stimme gibt (Anlass
2026-11-09: der Brain sollte „für alle“ umstellen, hatte dafür kein Werkzeug
und sagte trotzdem „erledigt“).

Die Kennung beim Dienst trägt einen Prüfwert über Probe und Text
(``gaston/gaston2-1a2b3c4d``): wer eine Probe ersetzt, bekommt beim nächsten
Satz von selbst die neue, ohne dass jemand den Dienst aufräumen muss.

Gelernt beim Bau (2026-11-08/09, Hörseiten): CosyVoice übernimmt Lautstärke,
Tempo und Gefühl der Probe. Eine gespielte Szene klingt „zu emotional“, eine
Probe mit −31 LUFS kommt 14 dB leiser heraus als thorsten. Deshalb gehört vor
das Anlegen eine Aufbereitung mit Hörprobe, nicht nur ein Kopieren.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.request
import wave

import numpy as np

from voice_assistant.services.tts import FREMDE_MODELLE, SpeachesTts

log = logging.getLogger(__name__)


class StimmenLager:
    """Die eigenen Stimmen eines Assistenten und ihr Abgleich mit dem Dienst."""

    def __init__(self, ordner: str, namensraum: str, dienst_url: str, modell: str,
                 standard: str, timeout: float = 15.0) -> None:
        self.ordner = ordner
        self.namensraum = namensraum
        self.dienst_url = dienst_url.rstrip("/")
        self.modell = modell
        self.standard = standard
        self.timeout = timeout
        self._lock = threading.Lock()
        gesetzt = self._standard_gesetzt()
        if gesetzt and gesetzt != standard and self.hat(gesetzt):
            log.info("Standard-Stimme %s (gesetzt, statt %s aus dem Profil)", gesetzt, standard)
            self.standard = gesetzt

    def _standard_gesetzt(self) -> str | None:
        try:
            with open(os.path.join(self.ordner, "_standard.json"), encoding="utf-8") as f:
                return str(json.load(f).get("stimme") or "") or None
        except (OSError, ValueError, AttributeError):
            return None

    def standard_setzen(self, name: str, von: str | None = None) -> None:
        """Standard-Stimme für alle, dauerhaft (überlebt Neustarts)."""
        if not self.hat(name):
            raise ValueError(f"Stimme {name!r} gibt es nicht")
        pfad = os.path.join(self.ordner, "_standard.json")
        with open(pfad + ".tmp", "w", encoding="utf-8") as f:
            json.dump({"stimme": name, "von": von or "", "zeit": time.strftime("%Y-%m-%dT%H:%M:%S")},
                      f, ensure_ascii=False)
        os.replace(pfad + ".tmp", pfad)
        self.standard = name

    # --- lokal ----------------------------------------------------------

    def _pfad(self, name: str, datei: str) -> str:
        return os.path.join(self.ordner, name, datei)

    def _meta(self, name: str) -> dict:
        try:
            with open(self._pfad(name, "meta.json"), encoding="utf-8") as f:
                meta = json.load(f)
            return meta if isinstance(meta, dict) else {}
        except (OSError, ValueError):
            return {}

    def hat(self, name: str | None) -> bool:
        """Stimme liegt vollständig vor und ist freigegeben."""
        if not name or "/" in name or name.startswith("."):
            return False
        return (os.path.isfile(self._pfad(name, "probe.wav"))
                and os.path.isfile(self._pfad(name, "probe.txt"))
                and self._meta(name).get("freigegeben", True) is not False)

    def namen(self) -> list[str]:
        try:
            eintraege = sorted(os.listdir(self.ordner))
        except OSError:
            return []
        return [n for n in eintraege if self.hat(n)]

    def eintraege(self) -> list[dict]:
        return [{"name": n, "beschreibung": str(self._meta(n).get("beschreibung", ""))}
                for n in self.namen()]

    def wirksam(self, model: str | None, voice: str | None) -> str:
        """Die Stimme, die gerade spricht: die gewählte, wenn sie eine von
        unseren ist, sonst die Standard-Stimme des Profils. Eine Piper-Wahl
        (z. B. eine alte gespeicherte Vorliebe) zählt hier nicht als Wahl."""
        if model == self.modell and self.hat(voice):
            return voice  # type: ignore[return-value]
        return self.standard

    def dienst_id(self, name: str) -> str:
        h = hashlib.sha1()
        with open(self._pfad(name, "probe.wav"), "rb") as f:
            h.update(f.read())
        with open(self._pfad(name, "probe.txt"), "rb") as f:
            h.update(f.read())
        return f"{self.namensraum}/{name}-{h.hexdigest()[:8]}"

    # --- Dienst ---------------------------------------------------------

    def hochladen(self, name: str) -> str:
        """Stimme beim Dienst anlegen; gibt die Kennung zurück. Wirft bei Fehler."""
        if not self.hat(name):
            raise FileNotFoundError(f"Stimme {name!r} liegt nicht vor")
        kennung = self.dienst_id(name)
        with open(self._pfad(name, "probe.wav"), "rb") as f:
            wav = f.read()
        with open(self._pfad(name, "probe.txt"), encoding="utf-8") as f:
            text = f.read().strip()
        body = json.dumps({"id": kennung, "text": text,
                           "wav_b64": base64.b64encode(wav).decode("ascii")}).encode()
        req = urllib.request.Request(f"{self.dienst_url}/v1/voices", data=body,
                                     headers={"Content-Type": "application/json"}, method="POST")
        with self._lock, urllib.request.urlopen(req, timeout=self.timeout) as resp:
            resp.read()
        log.info("Stimme %s beim Dienst angelegt (%s)", name, kennung)
        print(f"🗣️  Stimme {name} beim Antwort-Dienst angelegt ({kennung})")
        return kennung

    def vorab_hochladen(self, name: str) -> None:
        """Im Hintergrund, best effort: der erste Satz soll nicht warten."""
        def _lauf() -> None:
            try:
                self.hochladen(name)
            except Exception as e:  # noqa: BLE001 — beim ersten Satz noch einmal
                log.warning("Stimme %s vorab nicht hochgeladen: %s", name, e)
        threading.Thread(target=_lauf, name=f"stimme-{name}", daemon=True).start()


# Feste kurze Sätze („Ja?“, „Okay.“, Denk-Phrasen) je Stimme: gemessen am
# 2026-11-09 schwankt CosyVoice bei so kurzen Texten stark — „Ja?“ kam mit
# 0,8–1,6 s heraus (Piper 0,3 s), mal mit fast 1 s Stille davor, mal über die
# ganze Länge klingend. Darum mehrere Versuche, Stille abschneiden, und der
# zur erwarteten Länge passendste wird gespeichert. Geschätzt, nicht gehört:
# die Auswahl gehört einmal per Hörseite abgenommen.
_FEST_VERSUCHE = 5
_RAND_S = 0.04          # bleibt vor und hinter der Sprache stehen
_LAUF_MIN = 3           # 20-ms-Fenster am Stück ab, die als Sprache zählen
_KLICK_MAX = 7          # ein Ton bis 140 ms am Anfang …
_KLICK_PAUSE = 5        # … mit 100 ms Pause dahinter ist ein Knackser
_CACHE_MAX = 400        # Dateien je Lager-Cache; älteste fliegen


def feste_saetze(profile) -> list[str]:
    """Die festen Sätze eines Profils: Wakewort-Quittungen, Denk-Phrasen und
    die Abbruch-Quittung. Geteilt von assistant.py und tools/feste_saetze_hoeren.py."""
    saetze = [w.ack for w in profile.wakewords] + list(profile.locale.thinking_phrases)
    if profile.barge_in.enabled and profile.barge_in.ack:
        saetze.append(profile.barge_in.ack)
    return [t for t in dict.fromkeys(saetze) if t]


def _erwartet_s(text: str) -> float:
    return 0.25 + 0.06 * len(text.strip())


def _sprache_schneiden(pcm: np.ndarray, rate: int) -> np.ndarray:
    """Stille vorn und hinten weg (Fenster 20 ms, Schwelle 5 % der Spitze).

    Ein einzelnes lautes Fenster zählt nicht als Sprachbeginn: CosyVoice setzt
    bei kurzen Sätzen gern einen Knackser oder einen kurzen Vorlauf vor die
    Sprache. Erst ab ``_LAUF_MIN`` lauten Fenstern in Folge (60 ms) gilt der
    Satz als angefangen (Jochen, 2026-11-14: „da ist sehr viel Vorlauf dabei
    und eine Art Klackgeräusch“). Das Ende bleibt beim letzten lauten Fenster:
    ein Verschlusslaut am Wortende ist ein kurzer Knall nach kleiner Lücke
    (das „k“ von „Augenblick“, 20–40 ms) und fiele mit derselben Regel weg —
    gemessen 2026-11-14 an 3 von 63 festen Sätzen im Cache."""
    f = max(1, rate // 50)
    n = len(pcm) // f
    if n == 0:
        return pcm
    rms = np.sqrt(np.mean(pcm[:n * f].astype(np.float64).reshape(n, f) ** 2, axis=1))
    if rms.max() <= 0:
        return pcm[:0]
    laut = rms > 0.05 * rms.max()
    laeufe: list[tuple[int, int]] = []
    i = 0
    while i < n:
        if not laut[i]:
            i += 1
            continue
        j = i
        while j < n and laut[j]:
            j += 1
        laeufe.append((i, j))
        i = j
    # Ein kurzer Ton direkt am Anfang, gefolgt von einer Pause, ist ein
    # Knackser oder ein Anlaut des Synthesizers, nicht der Satz. CosyVoice
    # setzt so etwas gern vor „Ja?“ (Jochen, 2026-11-14).
    while len(laeufe) > 1:
        (a, b), (c, _) = laeufe[0], laeufe[1]
        if b - a <= _KLICK_MAX and c - b >= _KLICK_PAUSE:
            laeufe.pop(0)
            continue
        break
    beginn = next((a for a, b in laeufe if b - a >= _LAUF_MIN), None)
    if beginn is None:
        return pcm[:0]
    ende = laeufe[-1][1]
    rand = int(_RAND_S * rate)
    kern = pcm[max(0, beginn * f - rand):min(len(pcm), ende * f + rand)]
    return _weich_an_den_raendern(kern, rate)


def _weich_an_den_raendern(pcm: np.ndarray, rate: int, ms: float = 5.0) -> np.ndarray:
    """5 ms Rampe am Anfang und Ende: der Schnitt trifft selten die Nullinie,
    ein harter Sprung knackt sonst hörbar beim Abspielen (Jochen, 2026-11-14)."""
    n = min(int(ms / 1000 * rate), len(pcm) // 2)
    if n < 2:
        return pcm
    out = pcm.astype(np.float32).copy()
    rampe = np.linspace(0.0, 1.0, n, dtype=np.float32)
    out[:n] *= rampe
    out[-n:] *= rampe[::-1]
    return np.clip(out, -32768, 32767).astype(pcm.dtype)


def text_gehoert(mitschrift: str, ziel: str) -> bool:
    """Steht jedes Wort des (kurzen) Ziels unscharf in der Mitschrift?"""
    import difflib
    def w(t: str) -> list[str]:
        return re.findall(r"[a-zäöüß0-9]+", t.lower())
    hoert = w(mitschrift)
    return bool(hoert) and all(
        any(z == h or difflib.SequenceMatcher(None, z, h).ratio() >= 0.7 for h in hoert)
        for z in w(ziel))


def stt_pruefer(asr):
    """Prüfer für AntwortTts aus einem STT mit ``_anfrage(wav16k, kontext)``."""
    def pruefen(pcm: np.ndarray, rate: int, ziel: str) -> bool:
        from scipy.signal import resample_poly
        x = resample_poly(pcm.astype(np.float32), 16000, rate) if rate != 16000 else pcm
        mitschrift = asr._anfrage(AntwortTts._wav_bytes(np.clip(x, -32768, 32767), 16000), None) or ""
        ok = text_gehoert(mitschrift, ziel)
        if not ok:
            print(f"🗣️  Kandidat für {ziel!r} verworfen, gehört: {mitschrift!r}")
        return ok
    return pruefen


def wav_dauer(pfad: str) -> float:
    with wave.open(pfad, "rb") as w:
        return w.getnframes() / float(w.getframerate())


class AntwortTts(SpeachesTts):
    """Der Antwort-Dienst mit einer Stimme aus dem Lager.

    Welche, steht in voice_state (``model`` = Modell des Dienstes, ``voice`` =
    Name im Lager), sonst die Standard-Stimme. Ein 404 heißt hier „Stimme dem
    Dienst unbekannt“: hochladen, noch einmal fragen (SpeachesTts.synth_strom
    ruft dafür ``_modell_laden``)."""

    def __init__(self, lager: StimmenLager, state, timeout: float = 8.0,
                 cache: str | None = None, pruefen=None) -> None:
        """pruefen: Callable[[np.ndarray, int, str], bool] | None — hört ein
        Kandidat für einen festen Satz wirklich diesen Satz? (STT-Rückprobe;
        Anlass: ein „Okay.“-Kandidat war nur „äm“, Jochen 2026-11-09)."""
        self._name: str | None = None
        super().__init__(state, lager.dienst_url, lager.modell, lager.standard,
                         fest=True, timeout=timeout)
        self.lager = lager
        self.cache = cache
        self.pruefen = pruefen
        self._in_arbeit: set[str] = set()
        self._cache_lock = threading.Lock()
        FREMDE_MODELLE.add(lager.modell)

    def stimme_jetzt(self) -> str:
        from voice_assistant.state import voice_state
        vm, vv, _ = voice_state.get()
        return self.lager.wirksam(vm, vv)

    # --- ganze Sätze als Datei (Satelliten, feste Sätze) ------------------

    def _roh(self, text: str, name: str) -> tuple[np.ndarray, int]:
        """Ein Satz am Stück (Rate des Dienstes, mono), Upload bei 404."""
        def anfrage():
            req = self._anfrage(self.model, self.lager.dienst_id(name), 1.0, text)
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return resp.read()
        try:
            try:
                daten = anfrage()
            except urllib.error.HTTPError as e:
                if e.code != 404:
                    raise
                self.lager.hochladen(name)
                daten = anfrage()
            if daten[:4] != b"RIFF" or len(daten) < 100:
                raise ValueError(f"keine WAV vom Antwort-Dienst ({len(daten)} Bytes)")
            rate = int.from_bytes(daten[24:28], "little")
            pcm = np.frombuffer(daten[44:len(daten) - (len(daten) - 44) % 2], dtype="<i2")
            self.state.mark_tts_ok()
            return pcm, rate
        except Exception:
            self.state.mark_tts_failed()
            raise

    @staticmethod
    def _wav_bytes(pcm: np.ndarray, rate: int) -> bytes:
        import io
        b = io.BytesIO()
        with wave.open(b, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(rate)
            w.writeframes(pcm.astype("<i2").tobytes())
        return b.getvalue()

    def synth(self, text: str) -> bytes | None:
        """Wie SpeachesTts.synth (Datei-Weg, z. B. Satelliten), mit Stimme aus
        dem Lager. Kurze feste Sätze kommen aus dem Cache."""
        pfad = self.feste_datei(text, nur_vorhanden=True)
        if pfad:
            with open(pfad, "rb") as f:
                return f.read()
        try:
            pcm, rate = self._roh(text, self.stimme_jetzt())
        except Exception as e:  # noqa: BLE001 — Rückfall macht der Aufrufer
            print(f"⚠️  Antwort-Dienst (Datei) fehlgeschlagen: {e}")
            return None
        return self._wav_bytes(pcm, rate)

    # --- feste kurze Sätze ------------------------------------------------

    def _cache_pfad(self, text: str, name: str) -> str | None:
        if not self.cache:
            return None
        ordner = re.sub(r"[^A-Za-z0-9_.-]+", "_", self.lager.dienst_id(name))
        schluessel = hashlib.sha1(text.strip().encode("utf-8")).hexdigest()[:16]
        return os.path.join(self.cache, ordner, schluessel + ".wav")

    def _fest_rechnen(self, text: str, name: str, pfad: str) -> str | None:
        kandidaten = []
        for _ in range(_FEST_VERSUCHE):
            pcm, rate = self._roh(text, name)
            kern = _sprache_schneiden(pcm, rate)
            if len(kern) <= 0.15 * rate:
                continue
            if self.pruefen is not None:
                try:
                    if not self.pruefen(kern, rate, text):
                        continue
                except Exception as e:  # noqa: BLE001 — Prüfer weg: ohne ihn weiter
                    log.warning("STT-Rückprobe fester Satz ausgefallen: %s", e)
            kandidaten.append((abs(len(kern) / rate - _erwartet_s(text)), kern, rate))
        if not kandidaten:
            return None
        _, kern, rate = min(kandidaten, key=lambda k: k[0])
        os.makedirs(os.path.dirname(pfad), exist_ok=True)
        tmp = pfad + ".tmp"
        with open(tmp, "wb") as f:
            f.write(self._wav_bytes(kern, rate))
        os.replace(tmp, pfad)
        print(f"🗣️  Fester Satz {text!r} in {name}: {len(kern) / rate:.2f} s "
              f"(aus {len(kandidaten)} Versuchen)")
        self._cache_aufraeumen()
        return pfad

    def fest_kandidaten(self, text: str, name: str, n: int, versuche: int) -> list[tuple[np.ndarray, int]]:
        """Bis zu ``n`` Fassungen eines festen Satzes, die die STT-Rückprobe
        bestehen, Stille geschnitten, die zur erwarteten Länge passendste zuerst."""
        kandidaten = []
        for _ in range(versuche):
            if len(kandidaten) >= n:
                break
            pcm, rate = self._roh(text, name)
            kern = _sprache_schneiden(pcm, rate)
            if len(kern) <= 0.15 * rate:
                continue
            if self.pruefen is not None and not self.pruefen(kern, rate, text):
                continue
            kandidaten.append((kern, rate))
        return sorted(kandidaten, key=lambda k: abs(len(k[0]) / k[1] - _erwartet_s(text)))

    def fest_uebernehmen(self, text: str, name: str, wav_pfad: str, quelle: str = "") -> str:
        """Eine gehörte Fassung wird die gespielte (Hörseite, Telegram-Wahl)."""
        ziel = self._cache_pfad(text, name)
        if ziel is None:
            raise ValueError("kein Cache für feste Sätze eingerichtet")
        os.makedirs(os.path.dirname(ziel), exist_ok=True)
        with open(wav_pfad, "rb") as f:
            daten = f.read()
        with open(ziel + ".tmp", "wb") as f:
            f.write(daten)
        os.replace(ziel + ".tmp", ziel)
        with open(os.path.join(self.cache, "auswahl.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps({"zeit": time.strftime("%Y-%m-%dT%H:%M:%S"), "stimme": name,
                                "satz": text, "dienst_id": self.lager.dienst_id(name),
                                "dauer_s": round(wav_dauer(ziel), 2), "quelle": quelle},
                               ensure_ascii=False) + "\n")
        return ziel

    def _cache_aufraeumen(self) -> None:
        try:
            dateien = [os.path.join(d, f) for d, _, fs in os.walk(self.cache or "") for f in fs
                       if f.endswith(".wav")]
            if len(dateien) > _CACHE_MAX:
                for alt in sorted(dateien, key=os.path.getmtime)[:len(dateien) - _CACHE_MAX]:
                    os.unlink(alt)
        except OSError:
            pass

    def feste_datei(self, text: str, nur_vorhanden: bool = False,
                    name: str | None = None) -> str | None:
        """Pfad der gespeicherten Fassung von ``text`` in der aktuellen Stimme.

        nur_vorhanden: nichts rechnen, was fehlt, wird im Hintergrund gerechnet
        (der Aufrufer nimmt dann diesmal Piper). Sonst rechnen und warten."""
        name = name or self.stimme_jetzt()
        try:
            pfad = self._cache_pfad(text, name)
        except OSError:
            return None   # Probe fehlt im Lager
        if pfad is None:
            return None
        if os.path.exists(pfad):
            return pfad
        if nur_vorhanden:
            self.vorbereiten([text], name)
            return None
        try:
            return self._fest_rechnen(text, name, pfad)
        except Exception as e:  # noqa: BLE001
            print(f"⚠️  Fester Satz {text!r} nicht gerechnet: {e}")
            return None

    def vorbereiten(self, texte: list[str], name: str | None = None) -> None:
        """Feste Sätze im Hintergrund rechnen (beim Start, nach Stimmwechsel)."""
        name = name or self.stimme_jetzt()
        with self._cache_lock:
            offen = [t for t in texte if t and f"{name}|{t}" not in self._in_arbeit]
            self._in_arbeit.update(f"{name}|{t}" for t in offen)
        if not offen or not self.cache:
            return

        def _lauf() -> None:
            try:
                for t in offen:
                    if self.state.tts_ok():
                        self.feste_datei(t, name=name)
            finally:
                with self._cache_lock:
                    self._in_arbeit.difference_update(f"{name}|{t}" for t in offen)
        threading.Thread(target=_lauf, name="feste-saetze", daemon=True).start()

    @property
    def voice(self) -> str:  # für das Journal: wer gerade spricht
        return self._name or self.lager.standard

    @voice.setter
    def voice(self, _wert: str) -> None:
        pass

    def _parameter(self, text: str) -> tuple[str, str, float, str]:
        from voice_assistant.state import voice_state
        vm, vv, _ = voice_state.get()
        self._name = self.lager.wirksam(vm, vv)
        return self.model, self.lager.dienst_id(self._name), 1.0, text

    def _modell_laden(self, model: str) -> None:
        print(f"⚠️  Antwort-Dienst kennt die Stimme {self._name} nicht — lade sie hoch …")
        self.lager.hochladen(self._name or self.lager.standard)
