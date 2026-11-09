"""Neue Stimmen per Telegram: Probe zerlegen, vorsortieren, anhören, prüfen, anlegen.

Jochen 2026-11-09: Telegram ist der Hauptweg für neue Stimmen, „Vorverarbeitung
mit Beurteilung ist ein Muss“; per Telegram darf jeder anlegen (dort sind nur
Berechtigte), umschalten darf jeder. Der Ablauf ist der, der am selben Abend
von Hand die Stimme ``gaston`` ergab (``tools/stimmprobe_zerlegen.py``):

1. ``einreichen(datei, name)`` — der Brain gibt den Pfad der Telegram-Datei
   (``~/.openclaw/…/media/inbound/…``). Die Probe wird an ihren Pausen
   geschnitten, Stücke von 3–9 s, sich kaum überlappend, je auf −18 LUFS,
   Text per Qwen.
2. Vorsortieren, was sich messen lässt (alles andere entscheidet das Ohr —
   SER und Tonhöhe sagten das Urteil nicht voraus):
   - Probe endet mitten im Wort (Qwen setzt kein Satzzeichen) → raus. Solche
     Stücke setzen sich in der Ausgabe fort („Abbrauchst du …“).
   - Testsatz zweimal gerechnet und mitgeschrieben: fremdes Wort am Anfang,
     fehlendes Ende, Länge weit weg vom Median der Stücke (Stille, Atmen,
     Zischen — beim Kellerraum-Satz streute es 6,1–10,0 s).
3. Die besten ``HOERPROBEN_MAX`` gehen als Sprachnachricht in den Hauskanal:
   erst das Stück selbst, dann der Testsatz damit.
4. ``waehlen(stueck)`` — Abbruch-Test (``ABBRUCH_SAETZE`` × ``ABBRUCH_WDH``,
   Regel aus ``tools/tts_abbruch_test.py``). Latte 0 Abbrüche: die ganze
   zweisätzige Probe brach 9/60 ab, die einzelnen Sätze 0/60.
5. Bestanden → ins Lager, feste Sätze vorrechnen. Umschalten bleibt eigener
   Schritt (``voice_set_voice``).

Es läuft immer nur ein Auftrag; Meldungen gehen per Telegram, weil das
Zerlegen ~1 min und der Abbruch-Test ~4 min dauern.
"""
from __future__ import annotations

import difflib
import io
import json
import logging
import os
import re
import shutil
import statistics
import subprocess
import tempfile
import threading
import time
import wave
from typing import Callable

import numpy as np
from scipy.signal import resample_poly

log = logging.getLogger(__name__)

RATE = 24000
TESTSATZ = "Der Akku vom Staubsauger steht bei dreiundsechzig Prozent."
# Die heiklen Sätze der Abbruch-Messung (testsets/tts_abbruch/heikel.jsonl):
# an ihnen brach gaston2 am häufigsten ab, der Staubsauger-Satz 7 von 15.
ABBRUCH_SAETZE = [
    "Draußen sind es aktuell sieben Komma fünf Grad.",
    "Brauchst du daraus eine Einkaufsliste für die Zutaten?",
    "Im Kellerraum sind es gerade siebzehn Komma neun Grad.",
    "Der Akku vom Staubsauger steht bei dreiundsechzig Prozent.",
]
ABBRUCH_WDH = 15
HOERPROBEN_MAX = 5
VORAUSWAHL_MAX = 8      # so viele Stücke werden transkribiert und gerechnet
ZIEL_S = 6.0            # Regel für neue Stimmen: ein ruhiger Satz, 5–8 s
DAUER_MIN_S, DAUER_MAX_S = 3.0, 180.0
GROESSE_MAX = 25 * 1024 * 1024


# --- Audio ------------------------------------------------------------------

def laden(pfad: str) -> np.ndarray:
    """Beliebige Audiodatei (ogg/mp3/wav …) als 24-kHz-Mono-PCM."""
    roh = subprocess.run(["ffmpeg", "-v", "error", "-i", pfad, "-ac", "1", "-ar", str(RATE),
                          "-f", "s16le", "-"], capture_output=True, check=True, timeout=60).stdout
    return np.frombuffer(roh, dtype="<i2")


def _wav(pcm: np.ndarray, rate: int = RATE) -> bytes:
    b = io.BytesIO()
    with wave.open(b, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(np.asarray(pcm, dtype="<i2").tobytes())
    return b.getvalue()


def wav16k(pcm: np.ndarray, rate: int) -> bytes:
    x = resample_poly(pcm.astype(np.float32), 16000, rate) if rate != 16000 else pcm.astype(np.float32)
    return _wav(np.clip(x, -32768, 32767).astype("<i2"), 16000)


def normalisieren(pcm: np.ndarray, ziel: str) -> None:
    """Auf −18 LUFS (Ausgabe dann ≈ −16 wie thorsten, gemessen 2026-11-09)."""
    with tempfile.NamedTemporaryFile(suffix=".wav") as f:
        f.write(_wav(pcm))
        f.flush()
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", f.name, "-af",
                        "loudnorm=I=-18:TP=-1.5:LRA=11", "-ar", str(RATE), "-ac", "1", ziel],
                       check=True, timeout=60)


def ogg_opus(wav_bytes: bytes) -> bytes:
    """Für Telegram-Sprachnachrichten (sendVoice will OGG/Opus)."""
    return subprocess.run(["ffmpeg", "-v", "error", "-i", "-", "-c:a", "libopus", "-b:a", "48k",
                           "-f", "ogg", "-"], input=wav_bytes, capture_output=True,
                          check=True, timeout=60).stdout


def stuecke(pcm: np.ndarray, pause: float = 0.08, mn: float = 3.0,
            mx: float = 9.0) -> list[tuple[int, int]]:
    """Kandidaten als (anfang, ende) in Samples: zusammenhängende Folgen von
    Sprachabschnitten zwischen Pausen, Gesamtlänge mn..mx Sekunden."""
    f = RATE // 50
    n = len(pcm) // f
    if n == 0:
        return []
    x = pcm[:n * f].astype(np.float64).reshape(n, f)
    db = 20 * np.log10(np.sqrt(np.mean(x ** 2, axis=1)) / 32768 + 1e-9)
    # Schwelle über dem Grundpegel, nicht absolut: Hörspiel-Proben haben
    # Hintergrund (gastone2: −30 bis −35 dB unter der Spitze, nie „still“).
    laut = db > np.percentile(db, 10) + 6
    abschnitte, i = [], 0
    while i < n:
        if not laut[i]:
            i += 1
            continue
        j = i
        stille = 0
        while j < n and stille * 0.02 < pause:
            stille = 0 if laut[j] else stille + 1
            j += 1
        abschnitte.append((i * f, (j - stille) * f))
        i = j
    rand = int(0.08 * RATE)
    kandidaten = []
    for a in range(len(abschnitte)):
        for b in range(a, len(abschnitte)):
            s, e = max(0, abschnitte[a][0] - rand), min(len(pcm), abschnitte[b][1] + rand)
            if mn <= (e - s) / RATE <= mx:
                kandidaten.append((s, e))
    return kandidaten


def vorauswahl(kand: list[tuple[int, int]], n: int = VORAUSWAHL_MAX,
               ziel_s: float = ZIEL_S, doppelt: float = 0.85) -> list[tuple[int, int]]:
    """Die n Stücke, die der Ziellänge am nächsten liegen, ohne Fast-Doppel:
    deckt ein Stück mehr als ``doppelt`` des längeren eines schon gewählten
    ab, ist es nur dasselbe mit verschobenem Rand (gastone2: Anfang 3,12 oder
    3,32 s). Ein Stück, das in einem längeren liegt, bleibt — von Hand gewählt
    wurde am 2026-11-09 genau so eins (zweiter Satz ohne seinen Nachsatz)."""
    gewaehlt: list[tuple[int, int]] = []
    for s, e in sorted(kand, key=lambda k: abs((k[1] - k[0]) / RATE - ziel_s)):
        if all(max(0, min(e, e2) - max(s, s2)) <= doppelt * max(e - s, e2 - s2)
               for s2, e2 in gewaehlt):
            gewaehlt.append((s, e))
        if len(gewaehlt) >= n:
            break
    return sorted(gewaehlt)


# --- Mitschrift prüfen (geteilt mit tools/tts_abbruch_test.py) ------------------

def woerter(text: str) -> list[str]:
    return [w for w in re.findall(r"[a-zäöüß0-9]+", text.lower()) if w]


def aehnlich(a: str, b: str, grenze: float = 0.7) -> bool:
    return a == b or difflib.SequenceMatcher(None, a, b).ratio() >= grenze


def ende_da(satz: str, mitschrift: str) -> bool:
    """Eines der letzten zwei Wörter steht unter den letzten vier der
    Mitschrift (unscharf, Qwen verhört Namen)."""
    ziel, hoert = woerter(satz), woerter(mitschrift)
    if not ziel:
        return True
    return any(aehnlich(z, h) for z in ziel[-2:] for h in hoert[-4:])


def anfang_sauber(satz: str, mitschrift: str) -> bool:
    """Erstes Wort gleich (streng: „Abbrauchst“ zu „Brauchst“ ist 0,89 ähnlich)
    und kein Wort zu viel — sonst hat das Modell die Probe fortgesetzt."""
    ziel, hoert = woerter(satz), woerter(mitschrift)
    if not ziel or not hoert:
        return False
    return aehnlich(ziel[0], hoert[0], 0.9) and len(hoert) <= len(ziel) + 1


def probe_endet_sauber(text: str) -> bool:
    """Qwen setzt am Ende eines vollständigen Satzes ein Satzzeichen; ein
    Stück, das mitten im Wort endet („…Rohstoffs ab“), bekam keins."""
    t = text.strip()
    return len(woerter(t)) >= 4 and t[-1:] in ".!?"


def ist_abbruch(satz: str, mitschrift: str, audio_s: float, tempo_s_je_zeichen: float) -> bool:
    """Regel der Abbruch-Messung: Ende fehlt UND kürzer als 80 % des Erwarteten."""
    return not ende_da(satz, mitschrift) and audio_s < 0.8 * tempo_s_je_zeichen * len(satz)


# --- Eingaben -----------------------------------------------------------------

_UMLAUTE = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss"})


def name_pruefen(name: str) -> str:
    n = re.sub(r"[^a-z0-9_]+", "_", (name or "").strip().lower().translate(_UMLAUTE)).strip("_")
    if not re.fullmatch(r"[a-z][a-z0-9_]{1,30}", n):
        raise ValueError(f"Name {name!r} taugt nicht (Buchstaben, Ziffern, _; 2–31 Zeichen)")
    return n


def datei_pruefen(pfad: str, erlaubt: list[str]) -> str:
    """Nur Dateien unter den erlaubten Ordnern (OpenClaws Eingangs-Ablage):
    der Brain soll keine beliebigen Dateien des Rechners verschicken lassen."""
    echt = os.path.realpath(os.path.expanduser(pfad or ""))
    if not any(echt.startswith(os.path.realpath(os.path.expanduser(o)) + os.sep) for o in erlaubt):
        raise ValueError("Datei liegt nicht in der Eingangs-Ablage von OpenClaw")
    if not os.path.isfile(echt):
        raise ValueError("Datei nicht gefunden")
    if os.path.getsize(echt) > GROESSE_MAX:
        raise ValueError("Datei zu groß (höchstens 25 MB)")
    return echt


# --- Ablauf ---------------------------------------------------------------------

class StimmenEingang:
    """Ein Auftrag zur Zeit; Ergebnisse per ``melden``/``melden_audio``."""

    def __init__(self, lager, asr, melden: Callable[[str], None],
                 melden_audio: Callable[[bytes, str], None], ordner: str,
                 erlaubt: list[str], vorbereiten: Callable[[str], None] | None = None,
                 tts_bauen: Callable | None = None, abbruch_wdh: int = ABBRUCH_WDH) -> None:
        from voice_assistant.services.speaches import SpeachesState
        from voice_assistant.services.stimmen import AntwortTts, StimmenLager
        self.lager, self.asr = lager, asr
        self.melden, self.melden_audio = melden, melden_audio
        self.ordner, self.erlaubt = ordner, erlaubt
        self.vorbereiten = vorbereiten
        self.abbruch_wdh = abbruch_wdh
        self._lock = threading.Lock()
        self.laeuft: str | None = None
        # Probe-Lager je Auftrag; Kennungen beim Dienst in eigenem Namensraum
        self._tts_bauen = tts_bauen or (lambda d: AntwortTts(
            StimmenLager(d, f"{lager.namensraum}-eingang", lager.dienst_url, lager.modell,
                         "", timeout=60), SpeachesState(), timeout=60))

    # --- öffentlich (Sprech-Server) ---

    def einreichen(self, datei: str, name: str, beschreibung: str = "") -> dict:
        name = name_pruefen(name)
        if self.lager.hat(name) or os.path.isdir(os.path.join(self.lager.ordner, name)):
            raise ValueError(f"Stimme {name!r} gibt es schon — bitte anderen Namen")
        pfad = datei_pruefen(datei, self.erlaubt)
        self._starten(name, lambda: self._zerlegen(pfad, name, beschreibung))
        return {"ok": True, "name": name,
                "text": f"Ich bereite die Probe für die Stimme {name} auf. Die Hörproben "
                        "kommen in etwa einer Minute als Sprachnachrichten in den Chat."}

    def waehlen(self, stueck: int, name: str | None = None, text: str | None = None) -> dict:
        name = name_pruefen(name) if name else self._letzter()
        auftrag = self._auftrag(name)
        stuecke = {k["nr"]: k for k in auftrag.get("hoerproben", [])}
        if int(stueck) not in stuecke:
            raise ValueError(f"Stück {stueck} gibt es bei {name} nicht "
                             f"(zur Wahl: {', '.join(map(str, sorted(stuecke))) or 'keins'})")
        self._starten(name, lambda: self._pruefen(name, stuecke[int(stueck)], text))
        return {"ok": True, "name": name,
                "text": f"Ich prüfe Stück {stueck} für die Stimme {name} auf abgeschnittene "
                        "Sätze, das dauert drei bis vier Minuten. Das Ergebnis kommt in den Chat."}

    # --- intern ---

    def _starten(self, name: str, arbeit: Callable[[], None]) -> None:
        if not self._lock.acquire(blocking=False):
            raise RuntimeError(f"Es läuft gerade schon ein Auftrag ({self.laeuft})")
        self.laeuft = name

        def lauf() -> None:
            try:
                arbeit()
            except Exception as e:  # noqa: BLE001 — Fehler gehört in den Chat
                log.exception("Stimmen-Eingang %s", name)
                self.melden(f"⚠️ Stimme {name}: Aufbereitung abgebrochen ({e})")
            finally:
                self.laeuft = None
                self._lock.release()
        threading.Thread(target=lauf, name=f"stimmen-eingang-{name}", daemon=True).start()

    def _dir(self, name: str) -> str:
        return os.path.join(self.ordner, name)

    def _auftrag(self, name: str) -> dict:
        try:
            with open(os.path.join(self._dir(name), "auftrag.json"), encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            raise ValueError(f"Kein Auftrag für {name!r} — erst eine Probe einreichen") from None

    def _auftrag_schreiben(self, name: str, auftrag: dict) -> None:
        with open(os.path.join(self._dir(name), "auftrag.json"), "w", encoding="utf-8") as f:
            json.dump(auftrag, f, ensure_ascii=False, indent=1)

    def _letzter(self) -> str:
        try:
            offen = [n for n in os.listdir(self.ordner)
                     if os.path.isfile(os.path.join(self.ordner, n, "auftrag.json"))
                     and not self.lager.hat(n)]
        except OSError:
            offen = []
        if not offen:
            raise ValueError("Kein offener Auftrag — erst eine Probe einreichen")
        return max(offen, key=lambda n: os.path.getmtime(os.path.join(self.ordner, n, "auftrag.json")))

    def _hoeren(self, pcm: np.ndarray, rate: int) -> str:
        return (self.asr._anfrage(wav16k(pcm, rate), None) or "").strip()

    def _zerlegen(self, pfad: str, name: str, beschreibung: str) -> None:
        d = self._dir(name)
        if os.path.isdir(d):
            shutil.rmtree(d)
        lager_dir = os.path.join(d, "lager")
        os.makedirs(lager_dir)
        pcm = laden(pfad)
        dauer = len(pcm) / RATE
        if not DAUER_MIN_S <= dauer <= DAUER_MAX_S:
            raise ValueError(f"Probe ist {dauer:.0f} s lang, brauche 3 s bis 3 min")
        kand = stuecke(pcm)
        if not kand and dauer <= 9.0:
            kand = [(0, len(pcm))]     # kurze Probe ohne Pause: ganz nehmen
        auswahl = vorauswahl(kand)
        stuecke_info, raus = [], {"satzende": 0, "testsatz": 0}
        for i, (s, e) in enumerate(auswahl, 1):
            k = f"k{i:02d}"
            os.makedirs(os.path.join(lager_dir, k))
            normalisieren(pcm[s:e], os.path.join(lager_dir, k, "probe.wav"))
            text = self._hoeren(pcm[s:e], RATE)
            with open(os.path.join(lager_dir, k, "probe.txt"), "w", encoding="utf-8") as f:
                f.write(text)
            info = {"k": k, "von_s": round(s / RATE, 2), "bis_s": round(e / RATE, 2), "text": text}
            if not probe_endet_sauber(text):
                raus["satzende"] += 1
                continue
            stuecke_info.append(info)
        tts = self._tts_bauen(lager_dir)
        for info in stuecke_info:
            laeufe = []
            for _ in range(2):
                roh, rate = tts._roh(TESTSATZ, info["k"])
                m = self._hoeren(roh, rate)
                laeufe.append({"pcm": roh, "rate": rate, "audio_s": len(roh) / rate,
                               "sauber": anfang_sauber(TESTSATZ, m) and ende_da(TESTSATZ, m),
                               "mitschrift": m})
            info["laeufe"] = laeufe
        alle = [l_["audio_s"] for i in stuecke_info for l_ in i["laeufe"] if l_["sauber"]]
        median = statistics.median(alle) if alle else 0.0
        bewertet = []
        for info in stuecke_info:
            for l_ in info["laeufe"]:
                l_["laenge_ok"] = bool(median) and 0.75 <= l_["audio_s"] / median <= 1.35
                l_["fehler"] = (not l_["sauber"]) + (not l_["laenge_ok"])
            fehler = sum(l_["fehler"] for l_ in info["laeufe"])
            if fehler >= 2:
                raus["testsatz"] += 1
                continue
            abweichung = statistics.mean(abs(l_["audio_s"] / median - 1) for l_ in info["laeufe"])
            bewertet.append((fehler, abweichung, info))
        bewertet.sort(key=lambda b: (b[0], b[1]))
        hoerproben = []
        for nr, (_, _, info) in enumerate(bewertet[:HOERPROBEN_MAX], 1):
            bester = min(info["laeufe"], key=lambda l_: l_["fehler"])
            with wave.open(os.path.join(lager_dir, info["k"], "probe.wav")) as w:
                probe = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2")
                prate = w.getframerate()
            satz = bester["pcm"] if bester["rate"] == prate else resample_poly(
                bester["pcm"].astype(np.float32), prate, bester["rate"]).astype("<i2")
            hoerprobe = np.concatenate([probe, np.zeros(int(0.7 * prate), dtype="<i2"), satz])
            hoerproben.append({"nr": nr, "k": info["k"], "von_s": info["von_s"],
                               "bis_s": info["bis_s"], "text": info["text"]})
            self._auftrag_schreiben(name, {"name": name, "beschreibung": beschreibung,
                                           "quelle": os.path.basename(pfad), "dauer_s": round(dauer, 1),
                                           "hoerproben": hoerproben, "zeit": time.strftime("%Y-%m-%dT%H:%M:%S")})
            if nr == 1:
                self.melden(f"🎙️ Stimme {name}: {len(kand)} Stücke in der Probe ({dauer:.0f} s), "
                            f"{len(auswahl)} angesehen, {raus['satzende']} enden mitten im Wort, "
                            f"{raus['testsatz']} fielen im Testsatz auf. Hörproben: je das Stück, "
                            "dann ein Testsatz damit.")
            self.melden_audio(ogg_opus(_wav(hoerprobe, prate)),
                              f"Stück {nr} ({info['bis_s'] - info['von_s']:.1f} s): „{info['text']}“")
        if not hoerproben:
            self._auftrag_schreiben(name, {"name": name, "hoerproben": []})
            self.melden(f"⚠️ Stimme {name}: kein brauchbares Stück gefunden ({len(kand)} Stücke, "
                        f"{raus['satzende']} enden mitten im Wort, {raus['testsatz']} fielen im "
                        "Testsatz auf). Eine ruhige Probe mit ganzen Sätzen hilft, 5–8 s je Satz.")
            return
        self.melden(f"Welches Stück soll die Stimme {name} werden? Antworte z. B. „nimm Stück 1“. "
                    "Stimmt der Text unter einem Stück nicht, sag mir den richtigen dazu.")

    def _pruefen(self, name: str, stueck: dict, text: str | None) -> None:
        lager_dir = os.path.join(self._dir(name), "lager")
        k = stueck["k"]
        if text and text.strip():
            with open(os.path.join(lager_dir, k, "probe.txt"), "w", encoding="utf-8") as f:
                f.write(text.strip())
        tts = self._tts_bauen(lager_dir)
        zeilen = []
        for _ in range(self.abbruch_wdh):
            for satz in ABBRUCH_SAETZE:
                roh, rate = tts._roh(satz, k)
                m = self._hoeren(roh, rate)
                zeilen.append((satz, m, len(roh) / rate))
        tempo = statistics.median([a / len(s) for s, m, a in zeilen if ende_da(s, m)] or [0.08])
        abbrueche = sum(ist_abbruch(s, m, a, tempo) for s, m, a in zeilen)
        n = len(zeilen)
        auftrag = self._auftrag(name)
        auftrag["abbruch"] = {"stueck": stueck["nr"], "n": n, "abbrueche": abbrueche}
        self._auftrag_schreiben(name, auftrag)
        if abbrueche:
            self.melden(f"⚠️ Stück {stueck['nr']} für {name}: {abbrueche} von {n} Sätzen brachen "
                        "vorzeitig ab. So taugt es nicht als Stimme — nimm ein anderes Stück.")
            return
        ziel = os.path.join(self.lager.ordner, name)
        os.makedirs(ziel)
        for datei in ("probe.wav", "probe.txt"):
            shutil.copyfile(os.path.join(lager_dir, k, datei), os.path.join(ziel, datei))
        meta = {"beschreibung": auftrag.get("beschreibung") or f"eigene Stimme {name}",
                "quelle": f"Telegram, {auftrag.get('quelle', '')}, Stück {stueck['nr']} "
                          f"({stueck['von_s']}–{stueck['bis_s']} s), −18 LUFS",
                "abbruch": f"0/{n}", "angelegt": time.strftime("%Y-%m-%d")}
        with open(os.path.join(ziel, "meta.json"), "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False)
        if self.vorbereiten:
            self.vorbereiten(name)
        self.melden(f"✅ Stimme {name} ist angelegt (0 von {n} Sätzen abgebrochen). "
                    f"Umschalten mit „nimm die Stimme {name}“.")
