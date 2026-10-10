"""Küchentimer: Zustand, Ablauf und Ausgabe-Senken.

Der Assistent hält die Uhr. Gestellt wird über den festen Parser
(services/timer_parser.py, Schnellweg vor dem Aktuator-Tor) oder über den
Brain (HTTP am Sprech-Server, später als MCP-Werkzeug). Angezeigt und
geklingelt wird auf **Senken** — jede Gegenstelle, die den Vertrag in
TIMER_INTERFACE.md erfüllt (hier: der Küchen-Tablet-Viewer). Der Assistent
kennt keine Anzeige mit Namen, nur URLs aus dem Profil.

Was hier bewusst anders ist als bei Alexa (Jochen, 2026-10-10):

- Ein Timer klingelt ``klingeln``-mal und hört **von selbst** auf. "Gaston,
  stopp" beendet nur das Klingeln; nötig sein soll es nicht.
- Nach dem Ablauf bleibt er sichtbar und zählt weiter ins Negative
  ("abgelaufen vor 2:30"), bis ``nachlauf_max_s`` erreicht ist.
- Ein neuer Timer ohne Namen ersetzt den alten ohne Namen; gleiches gilt
  für denselben Namen ("Nudeln" = "Nudel", siehe ``schluessel``).
- "lösch den Timer" ohne Namen bei mehreren → Rückfrage, nicht raten.

Senken bekommen immer den **ganzen Zustand** (kein Ereignis-Strom) und die
**Restzeit** statt einer Uhrzeit — die Uhr des Tablets ist nicht verlässlich.
Klingelt keine Senke (keine Anzeige verbunden, Fehler, Zeitüberschreitung),
klingelt der Lautsprecher des Assistenten.

Nur Standardbibliothek; Senken und Lautsprecher kommen als Aufrufe herein,
damit das hier ohne Audio und Netz testbar ist (tests/test_kuechentimer.py).
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import asdict, dataclass, field
from typing import Callable

from voice_assistant.services.timer_parser import (
    ABFRAGEN, ALLE, KLINGELN, LOESCHEN, NOCH, STELLEN, VERLAENGERN, TimerBefehl, schluessel,
)

VERTRAG_VERSION = 1

# "Gaston, stopp" kommt oft erst, wenn das Klingeln gerade aufgehört hat.
# So lange danach gehört ein Stopp noch dem Timer und nicht dem Brain.
STOPP_NACHLAUF_S = 15.0

# "gast…" davor: die STT zieht "Gaston stopp" gern zu "Gastostop" zusammen
# (siehe assistant._STOP_PATTERN_FOLLOWUP).
_STOPP = re.compile(r"\b(?:gast\w*?)?(stopp?|halt|ruhe|aus|still|ok(ay)?|danke|genug)\b",
                    re.IGNORECASE)
# Name für "der Timer ohne Namen" in einer Rückfrage-Antwort (None hieße
# "kein Name genannt" und führte zur selben Rückfrage zurück).
OHNE_NAMEN = ""
_WORT = re.compile(r"[a-zäöüß]+", re.IGNORECASE)


def ist_stopp(text: str) -> bool:
    """Kurzer Satz mit einem Stopp-Wort ("Gaston, stopp", "Okay, danke").
    Lang darf er nicht sein: "Stopp den Pizzatimer" ist ein Löschbefehl und
    geht an den Parser, "mach das Licht aus" ist kein Timer-Stopp."""
    worte = [w for w in _WORT.findall(text or "") if not w.lower().startswith("gast")]
    return bool(_STOPP.search(text or "")) and len(worte) <= 3 and "timer" not in text.lower()

# Ein Timer, der während eines Neustarts abgelaufen ist, klingelt beim Start
# nur, wenn er höchstens so lange vorbei ist — sonst klingelt die Küche um
# 7 Uhr für die Nudeln von gestern Abend.
_SPAET_KLINGELN_S = 60.0


@dataclass
class Timer:
    id: str
    name: str | None          # Anzeigename ("Nudeln") oder None
    ende: float               # Wanduhr des Assistenten (epoch)
    dauer_s: int              # zuletzt gestellte Gesamtdauer, für Fortschritt
    klingeln: int
    abgelaufen: bool = False  # Ablauf wurde bemerkt und gemeldet
    still: bool = False       # Klingeln beendet (von selbst oder "Gaston, stopp")
    klingel_ab: float = 0.0   # wann das Klingeln begann (= Ablauf bemerkt)


@dataclass
class Senke:
    name: str
    url: str
    token: str = ""


@dataclass
class Ergebnis:
    text: str                       # kurze Ansage für den Lautsprecher
    ok: bool = True
    rueckfrage: bool = False        # Nutzer muss den Namen nennen
    timer: list[dict] = field(default_factory=list)


# --- Sprache ---------------------------------------------------------------
# Deutsch, wie der Parser. Wer eine andere Sprache braucht, ersetzt Parser
# und diese Texte gemeinsam.

try:
    from num2words import num2words as _n2w
except Exception:                   # pragma: no cover — optional
    _n2w = None


def _zahl(n: int, weiblich: bool = True) -> str:
    if n == 1:
        return "eine" if weiblich else "ein"
    return _n2w(n, lang="de") if _n2w else str(n)


def dauer_text(s: float) -> str:
    """480 → 'acht Minuten', 4200 → 'eine Stunde zehn Minuten', 150 →
    'zwei Minuten dreißig Sekunden'."""
    s = int(round(abs(s)))
    h, rest = divmod(s, 3600)
    m, sek = divmod(rest, 60)
    teile = []
    if h:
        teile.append(f"{_zahl(h)} Stunde" + ("" if h == 1 else "n"))
    if m:
        teile.append(f"{_zahl(m)} Minute" + ("" if m == 1 else "n"))
    if sek and not h:
        teile.append(f"{_zahl(sek)} Sekunde" + ("" if sek == 1 else "n"))
    return " ".join(teile) or "null Sekunden"


def timer_text(name: str | None) -> str:
    return f"{name}-Timer" if name else "Der Timer"


# --- Dienst ----------------------------------------------------------------

class KuechenTimer:
    def __init__(
        self,
        pfad: str,
        senken: list[Senke],
        klingeln: int = 3,
        klingel_abstand_s: float = 5.0,
        nachlauf_max_s: float = 1800.0,
        ansage: Callable[[str], None] | None = None,
        lautsprecher_klingeln: Callable[[int, Callable[[], bool]], None] | None = None,
        uhr: Callable[[], float] = time.time,
        senden: Callable[[Senke, dict, float], dict | None] | None = None,
        herzschlag_s: float = 30.0,
        senke_timeout_s: float = 5.0,
    ) -> None:
        self.pfad = pfad
        self.senken = senken
        self.klingeln_default = max(1, int(klingeln))
        self.klingel_abstand_s = klingel_abstand_s
        self.nachlauf_max_s = nachlauf_max_s
        self.ansage = ansage
        self.lautsprecher_klingeln = lautsprecher_klingeln
        self.uhr = uhr
        self.senden = senden or _http_senden
        self.herzschlag_s = herzschlag_s
        self.senke_timeout_s = senke_timeout_s
        self._lock = threading.RLock()
        self._timer: dict[str | None, Timer] = {}     # schluessel(name) → Timer
        self._seq = 0
        self._letzter_push = 0.0
        self._pool = ThreadPoolExecutor(max_workers=max(2, len(senken)),
                                        thread_name_prefix="timer-senke")
        self._wach = threading.Event()
        self._laden()

    # -- Persistenz ---------------------------------------------------------

    def _laden(self) -> None:
        try:
            with open(self.pfad, encoding="utf-8") as f:
                roh = json.load(f)
        except FileNotFoundError:
            return
        except Exception as e:
            print(f"⚠️  Timer: {self.pfad} nicht lesbar ({e}), starte leer")
            return
        jetzt = self.uhr()
        for t in roh.get("timer", []):
            try:
                timer = Timer(**t)
            except TypeError:
                continue
            vorbei = jetzt - timer.ende
            if vorbei > self.nachlauf_max_s:
                continue
            if vorbei > _SPAET_KLINGELN_S and not timer.abgelaufen:
                timer.abgelaufen = timer.still = True   # zu spät zum Klingeln, nur anzeigen
            self._timer[schluessel(timer.name)] = timer
        if self._timer:
            print(f"⏲️  Timer: {len(self._timer)} aus {self.pfad} übernommen")

    def _speichern(self) -> None:
        tmp = self.pfad + ".tmp"
        try:
            os.makedirs(os.path.dirname(self.pfad) or ".", exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"timer": [asdict(t) for t in self._timer.values()]}, f,
                          ensure_ascii=False, indent=1)
            os.replace(tmp, self.pfad)
        except Exception as e:
            print(f"⚠️  Timer: speichern fehlgeschlagen ({e})")

    # -- Zustand für Senken -------------------------------------------------

    def _klingelt(self, t: Timer, jetzt: float) -> bool:
        return (t.abgelaufen and not t.still
                and jetzt - t.klingel_ab < t.klingeln * self.klingel_abstand_s)

    def schnappschuss(self) -> dict:
        with self._lock:
            jetzt = self.uhr()
            timer = sorted(self._timer.values(), key=lambda t: t.ende)
            return {
                "art": "kuechentimer",
                "version": VERTRAG_VERSION,
                "seq": self._seq,
                "nachlauf_max_s": self.nachlauf_max_s,
                "klingel_abstand_s": self.klingel_abstand_s,
                "timer": [{
                    "id": t.id,
                    "name": t.name,
                    "dauer_s": t.dauer_s,
                    "rest_s": round(t.ende - jetzt, 1),
                    "klingeln": t.klingeln,
                    "still": t.still,
                } for t in timer],
            }

    def _geaendert(self, push: bool = True) -> None:
        """Nach jeder Änderung: Nummer hoch, speichern, an alle Senken (ohne
        zu warten — der Sprechweg soll nicht an einer toten Senke hängen)."""
        self._seq += 1
        self._speichern()
        if push:
            self._push()
        self._wach.set()

    def _push(self) -> None:
        stand = self.schnappschuss()
        self._letzter_push = self.uhr()
        for s in self.senken:
            self._pool.submit(self._senden_sicher, s, stand)

    def _senden_sicher(self, s: Senke, stand: dict) -> dict | None:
        try:
            return self.senden(s, stand, self.senke_timeout_s)
        except Exception as e:
            print(f"⚠️  Timer-Senke {s.name}: {e}")
            return None

    # -- Befehle ------------------------------------------------------------

    def ausfuehren(self, b: TimerBefehl) -> Ergebnis:
        with self._lock:
            if b.aktion == STELLEN:
                return self.stellen(b.name, b.dauer_s, b.klingeln)
            if b.aktion in (VERLAENGERN, NOCH):
                return self.verlaengern(b.name, b.dauer_s, neu_wenn_fehlt=b.aktion == NOCH,
                                        klingeln=b.klingeln)
            if b.aktion == LOESCHEN:
                return self.loeschen(b.name)
            if b.aktion == ABFRAGEN:
                return self.abfragen(b.name)
            if b.aktion == KLINGELN:
                return self.klingeln_setzen(b.name, b.klingeln)
        return Ergebnis("Das kann ich mit dem Timer nicht.", ok=False)

    def _finde(self, name: str | None) -> tuple[Timer | None, Ergebnis | None]:
        """Ohne Namen: der einzige. Mehrere → Rückfrage, auch wenn einer davon
        namenlos ist (Jochen 2026-10-10: "Rückfrage passt")."""
        if name == OHNE_NAMEN:
            t = self._timer.get(None)
            return (t, None) if t else (None, Ergebnis("Es gibt keinen Timer ohne Namen.", ok=False))
        if name is not None:
            t = self._timer.get(schluessel(name))
            if t is None:
                return None, Ergebnis(f"Einen {name}-Timer gibt es nicht.", ok=False)
            return t, None
        if len(self._timer) == 1:
            return next(iter(self._timer.values())), None
        if not self._timer:
            return None, Ergebnis("Es läuft kein Timer.", ok=False)
        return None, self._rueckfrage()

    def _rueckfrage(self) -> Ergebnis:
        namen = [t.name or "den ohne Namen"
                 for t in sorted(self._timer.values(), key=lambda t: t.ende)]
        liste = ", ".join(namen[:-1]) + " oder " + namen[-1]
        return Ergebnis(f"Welchen Timer? {liste}?", ok=False, rueckfrage=True)

    def stellen(self, name: str | None, dauer_s: int | None,
                klingeln: int | None = None) -> Ergebnis:
        if not dauer_s or dauer_s <= 0:
            return Ergebnis("Wie lange soll der Timer laufen?", ok=False, rueckfrage=True)
        with self._lock:
            t = Timer(id=uuid.uuid4().hex[:8], name=name, ende=self.uhr() + dauer_s,
                      dauer_s=int(dauer_s), klingeln=int(klingeln or self.klingeln_default))
            self._timer[schluessel(name)] = t
            self._geaendert()
        text = f"{name}-Timer, {dauer_text(dauer_s)}." if name else f"Timer, {dauer_text(dauer_s)}."
        return Ergebnis(text, timer=[asdict(t)])

    def verlaengern(self, name: str | None, dauer_s: int | None,
                    neu_wenn_fehlt: bool = False, klingeln: int | None = None) -> Ergebnis:
        if not dauer_s or dauer_s <= 0:
            return Ergebnis("Um wie viel soll ich verlängern?", ok=False, rueckfrage=True)
        with self._lock:
            t, fehler = self._finde(name)
            if t is None:
                if neu_wenn_fehlt and name != OHNE_NAMEN and not (fehler and fehler.rueckfrage):
                    return self.stellen(name, dauer_s, klingeln)
                return fehler
            jetzt = self.uhr()
            # Abgelaufen: ab jetzt weiter, nicht ab dem alten Ende.
            basis = max(t.ende, jetzt)
            t.ende = basis + dauer_s
            t.dauer_s = int(round(t.ende - jetzt)) if t.abgelaufen else t.dauer_s + int(dauer_s)
            t.abgelaufen = t.still = False
            if klingeln:
                t.klingeln = int(klingeln)
            self._geaendert()
            rest = t.ende - jetzt
        return Ergebnis(f"{timer_text(t.name)} läuft noch {dauer_text(rest)}.")

    def loeschen(self, name: str | None) -> Ergebnis:
        with self._lock:
            if name == ALLE:
                n = len(self._timer)
                self._timer.clear()
                self._geaendert()
                return Ergebnis("Alle Timer gelöscht." if n else "Es lief kein Timer.")
            t, fehler = self._finde(name)
            if t is None:
                return fehler
            del self._timer[schluessel(t.name)]
            self._geaendert()
        return Ergebnis(f"{timer_text(t.name)} ist gelöscht.")

    def abfragen(self, name: str | None) -> Ergebnis:
        with self._lock:
            jetzt = self.uhr()
            if name not in (None, ALLE):
                t, fehler = self._finde(name)
                liste = [t] if t else []
                if fehler:
                    return fehler
            else:
                liste = sorted(self._timer.values(), key=lambda t: t.ende)
            if not liste:
                return Ergebnis("Es läuft kein Timer.")
            saetze = []
            for t in liste:
                rest = t.ende - jetzt
                if rest > 0:
                    saetze.append(f"{timer_text(t.name)} läuft noch {dauer_text(rest)}.")
                else:
                    saetze.append(f"{timer_text(t.name)} ist seit {dauer_text(rest)} abgelaufen.")
            return Ergebnis(" ".join(saetze), timer=[asdict(t) for t in liste])

    def klingeln_setzen(self, name: str | None, anzahl: int | None) -> Ergebnis:
        """"Der Timer soll zehnmal klingeln" — gilt nur für diesen Timer
        (Jochen 2026-10-10), der Default bleibt."""
        if not anzahl or anzahl <= 0:
            return Ergebnis("Wie oft soll er klingeln?", ok=False, rueckfrage=True)
        with self._lock:
            t, fehler = self._finde(name)
            if t is None:
                return fehler
            t.klingeln = int(anzahl)
            self._geaendert()
        return Ergebnis(f"{timer_text(t.name)} klingelt {_zahl(int(anzahl), weiblich=False)}mal.")

    def klingeln_aus(self, nachlauf_s: float = 0.0) -> bool:
        """'Gaston, stopp' — beendet nur das Klingeln, der Timer bleibt
        sichtbar. True, wenn gerade etwas klingelte oder vor höchstens
        ``nachlauf_s`` aufgehört hat (dann gehört das Stopp dem Timer und
        nicht dem Turn)."""
        with self._lock:
            jetzt = self.uhr()
            klingelnd = [t for t in self._timer.values() if self._klingelt(t, jetzt)]
            for t in klingelnd:
                t.still = True
            if klingelnd:
                self._geaendert()
            kuerzlich = any(
                t.abgelaufen and jetzt - t.klingel_ab
                < t.klingeln * self.klingel_abstand_s + nachlauf_s
                for t in self._timer.values())
            return bool(klingelnd) or (nachlauf_s > 0 and kuerzlich)

    def antwort_auf_rueckfrage(self, b: TimerBefehl, text: str) -> Ergebnis:
        """Antwort auf "Welchen Timer? Nudel oder Pizza?": den Namen im Satz
        suchen ("die Nudeln", "den Pizzatimer", "alle") und den Befehl damit
        wiederholen. Kein Treffer → nichts tun, nicht raten."""
        with self._lock:
            worte = [w.lower() for w in _WORT.findall(text or "")]
            if "alle" in worte and b.aktion in (LOESCHEN, ABFRAGEN):
                return self.ausfuehren(TimerBefehl(b.aktion, ALLE, b.dauer_s, b.klingeln))
            namen = {schluessel(t.name): t.name for t in self._timer.values() if t.name}
            for w in worte:
                for k in (schluessel(w), schluessel(w.replace("timer", "")) if "timer" in w else None):
                    if k and k in namen:
                        return self.ausfuehren(TimerBefehl(b.aktion, namen[k], b.dauer_s,
                                                           b.klingeln))
            if any(w in ("ohne", "namenlos", "normale", "einfache") for w in worte) and None in self._timer:
                return self.ausfuehren(TimerBefehl(b.aktion, OHNE_NAMEN, b.dauer_s, b.klingeln))
        return Ergebnis("Den Timer habe ich nicht gefunden. Ich lasse alles, wie es ist.",
                        ok=False)

    def _soll_still(self, t: Timer) -> bool:
        """Für den Lautsprecher: aufhören, wenn gestoppt, gelöscht, ersetzt,
        verlängert oder die Klingelzeit vorbei ist."""
        with self._lock:
            return self._timer.get(schluessel(t.name)) is not t or not self._klingelt(t, self.uhr())

    def klingelt_gerade(self) -> bool:
        with self._lock:
            jetzt = self.uhr()
            return any(self._klingelt(t, jetzt) for t in self._timer.values())

    # -- Ablauf -------------------------------------------------------------

    def tick(self) -> None:
        """Einmal prüfen: abgelaufen? zu alt? Herzschlag fällig? Der Thread
        ruft das laufend; Tests rufen es direkt."""
        faellig: list[Timer] = []
        with self._lock:
            jetzt = self.uhr()
            alt = [k for k, t in self._timer.items() if jetzt - t.ende > self.nachlauf_max_s]
            for k in alt:
                del self._timer[k]
            ausgeklingelt = False
            for t in self._timer.values():
                if not t.abgelaufen and t.ende <= jetzt:
                    t.abgelaufen = True
                    t.klingel_ab = jetzt
                    faellig.append(t)
                elif t.abgelaufen and not t.still and not self._klingelt(t, jetzt):
                    t.still = ausgeklingelt = True     # hört von selbst auf
            stand = None
            if faellig:
                # Ablauf: gesendet wird synchron in _melden, nicht hier.
                self._geaendert(push=False)
                stand = self.schnappschuss()
                self._letzter_push = jetzt
            elif alt or ausgeklingelt:
                self._geaendert()
            elif self.senken and jetzt - self._letzter_push >= self.herzschlag_s:
                # Herzschlag: eine neu gestartete Senke kennt sonst nichts,
                # bis sich etwas ändert.
                self._push()
        if faellig:
            self._melden(faellig, stand)

    def _melden(self, faellig: list[Timer], stand: dict) -> None:
        """Ablauf: Senken fragen, ob sie klingeln (synchron, mit Zeitschranke);
        wer nicht bestätigt wird, klingelt am Lautsprecher. Dazu einmal die
        Ansage."""
        bestaetigt: set[str] = set()
        if self.senken:
            futures = [self._pool.submit(self._senden_sicher, s, stand) for s in self.senken]
            wait(futures, timeout=self.senke_timeout_s + 1)
            for f in futures:
                antwort = f.result() if f.done() else None
                if isinstance(antwort, dict):
                    bestaetigt.update(str(i) for i in antwort.get("klingelt") or [])
        for t in faellig:
            ort = "Senke" if t.id in bestaetigt else "Lautsprecher"
            print(f"⏰ Timer abgelaufen: {t.name or '(ohne Namen)'} — klingelt über {ort}")
            if self.ansage:
                self.ansage(f"{timer_text(t.name)} ist abgelaufen.")
            if t.id not in bestaetigt and self.lautsprecher_klingeln:
                threading.Thread(target=self.lautsprecher_klingeln,
                                 args=(t.klingeln, lambda t=t: self._soll_still(t)),
                                 name="timer-klingeln", daemon=True).start()

    def start(self) -> threading.Thread:
        def _lauf() -> None:
            while True:
                try:
                    self.tick()
                except Exception as e:
                    print(f"⚠️  Timer-Tick: {e}")
                self._wach.wait(0.5)
                self._wach.clear()

        with self._lock:
            self._push()                # Senken kennen den Stand sofort
        th = threading.Thread(target=_lauf, name="kuechentimer", daemon=True)
        th.start()
        return th


def _http_senden(s: Senke, stand: dict, timeout: float) -> dict | None:
    kopf = {"Content-Type": "application/json"}
    if s.token:
        kopf["Authorization"] = f"Bearer {s.token}"
    req = urllib.request.Request(s.url, data=json.dumps(stand).encode(), headers=kopf,
                                 method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        roh = r.read()
    try:
        antwort = json.loads(roh or b"{}")
    except ValueError:
        return None
    return antwort if isinstance(antwort, dict) else None


# --- Klingelton für den Lautsprecher ----------------------------------------

def klingel_wav(pfad: str) -> None:
    """Eine Klingel-Folge: vier Rechteck-Pieper 2,5 kHz. Am Küchentablet
    gemessen (2026-10-10): ein Sinus 440 Hz war hörbar, aber zu leise; das
    Rechteck "deutlich lauter und für einen Wecker/Timer wirklich gut"
    (Jochen). Hier halb so laut wie dort, der Lautsprecher des Assistenten
    steht näher."""
    import wave

    rate = 16000
    an, aus = int(rate * 0.1), int(rate * 0.1)
    periode = rate // 2500
    ton = bytearray()
    for _ in range(4):
        for i in range(an):
            v = 8000 if (i % periode) < periode // 2 else -8000
            ton += int(v).to_bytes(2, "little", signed=True)
        ton += b"\x00\x00" * aus
    with wave.open(pfad, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(bytes(ton))
