#!/usr/bin/env python3
"""Barge-in gegen die EIGENE Stimme messen statt raten.

Aufruf (Projekt-venv wird selbst gesucht):
    ow-venv/bin/python -m tools.bargein_echo_test digital
    ow-venv/bin/python -m tools.bargein_echo_test digital --saetze saetze.txt
    ow-venv/bin/python -m tools.bargein_echo_test akustisch
    ow-venv/bin/python -m tools.bargein_echo_test akustisch --wiederholungen 3

Die Frage, die hier beantwortet wird
------------------------------------
Barge-in laesst die Wakeword-Erkennung waehrend der eigenen Ansage
mitlaufen. Damit entsteht eine Fehlerquelle, die es vorher nicht gab: **Gaston
kann sich selbst abbrechen.** Und das ist kein exotischer Fall, sondern ein
naheliegender — die Bestaetigung wiederholt das Transkript, und weil der
Pre-Roll das Wakewort mitaufnimmt, steht "Gaston" (oder ein Verhoerer davon)
in sehr vielen Transkripten. Der Assistent sagt also regelmaessig selbst
"Ich habe verstanden: Gaston, mach das Kuechenlicht an".

Ohne Messung waere die Wahl zwischen "Barge-in an" und "Barge-in aus" eine
Vermutung. Deshalb zwei Modi, die zwei verschiedene Dinge messen:

**digital** — die gerenderten TTS-Saetze gehen DIREKT durch den Detektor, ohne
Lautsprecher, ohne Raum, ohne AEC. Das ist die untere Schranke: was hier
triggert, triggert aus dem Nutzsignal selbst, also immer. Braucht keine
Hardware und keinen gestoppten Service, misst aber NICHT den Echo-Weg.

**akustisch** — die Saetze werden ueber den echten Lautsprecher gespielt,
waehrend gleichzeitig der echte Mic-Pfad mitliest und durch den Detektor
laeuft. Das ist die Zahl, die zaehlt: sie enthaelt Raum, Pegel, Mikrofon und
(im respeaker-Modus) die Echo-Unterdrueckung des XVF3800. Stoppt dafuer die
User-Unit, weil der Mic-Stream exklusiv ist — wie ``wakeword_studio record``.

Ein Lauf ist eine Stichprobe, und zwar eine kleinere als sie aussieht
---------------------------------------------------------------------
Die TTS rendert **denselben Satz jedes Mal anders** (gemessen 2026-09-20: drei
Renderings von "Ich habe verstanden: Gaston, mach das Kuechenlicht an" ergaben
137294 / 130638 / 133710 Bytes). Jeder Durchlauf misst also eine andere
Realisierung, und genau deshalb trat der Selbst-Trigger in zwei Laeufen an
verschiedenen Saetzen und ueber verschiedene Gate-Pfade auf (einmal 2 Frames /
min_peak_short, einmal 1 Frame / min_peak_single). Wer einmal laeuft und nichts
sieht, hat nichts gezeigt — ``--wiederholungen`` ist der Normalfall.

Das ist keine Schwaeche des Werkzeugs, sondern die Wirklichkeit: im Betrieb
wird jeder Satz frisch gerendert, es gibt also auch dort keine feste Antwort
"dieser Satz triggert".

Lesart des Ergebnisses
----------------------
Ein Selbst-Trigger im akustischen Lauf heisst: im Betrieb wuerde sich der
Assistent an dieser Stelle selbst unterbrechen. Ein Near-Miss mit hohem Peak
heisst: es fehlt nicht viel, und eine spaetere Lockerung des Gates (oder ein
lauterer Lautsprecher) wuerde es ausloesen. Beides gehoert ins Protokoll,
bevor ``barge_in.enabled: true`` in ein Profil geschrieben wird.

Messreihe
---------
**2026-09-20, respeaker-Profil mit use_speaker, Bundle gaston (v3,
2026-09-16), Stimme speaches de_DE-thorsten-medium, Pegel-Gate 400,
volume 0.8.**

(Rechnername absichtlich nicht genannt: die READMEs verweisen Fremde auf dieses
Werkzeug, und fuer die Deutung der Zahl zaehlt der Aufbau — Modus, Bundle,
Stimme, Pegel —, nicht unser Host. Repo-Regel in CLAUDE.md.)

    Lauf              Saetze   Selbst-Trigger   hoechster Score
    digital              40       5 (12,5 %)              0,97
    akustisch            24       0                       0,07

**digital** (reines TTS-Signal, ohne Raum, ohne AEC): 5 Selbst-Trigger auf 40
Renderings, ueber ALLE drei Gate-Pfade — 1 Frame/min_peak_single (Peaks 0,76
und 0,81), 2 Frames/min_peak_short (0,93), 3 Frames/min_peak (0,97). Betroffen
waren vier verschiedene Saetze, darunter **"Das dauert noch einen Augenblick"**
— eine Denk-Phrase, in der das Wakewort gar nicht vorkommt. Das Modell springt
also auf Gastons Stimme an sich an, nicht bloss auf das wiederholte Wakewort.
Ursache ist strukturell: das Modell ist auf synthetischen **thorsten**-Stimmen
trainiert (models/wakewords/gaston/manifest.yaml, `voices:`), und mit genau so
einer Stimme spricht der Assistent — die eigene Stimme liegt IN der
Trainingsverteilung, sie ist fuer das Modell nicht "jemand anders", sie ist das
Trainingsmaterial.

**akustisch** (echter Lautsprecher → XVF3800-AEC → echtes Mikro): KEIN
Selbst-Trigger auf 24 Renderings, hoechster Score 0,07. Dass tatsaechlich Ton
kam, steht in denselben Zeilen: Raum-Grundpegel 36, Echo am Mikro 96-479 (also
3- bis 13-fach darueber, 24/24 Saetze messbar), und die Wiedergabe-Dauer passte
jeweils zur Laenge der Datei. Abstand zur niedrigsten Gate-Schwelle
(min_peak_single 0,75) damit rund zehnfach.

**Die Vorab-Erwartung war falsch, und das gehoert hierhin.** Vor dem Lauf stand
hier: "eher nicht — AEC daempft den Pegel, aber das Gate haengt am Score, und
der reagiert auf die Spektralform, die auch im Restecho erhalten bleibt."
Gemessen faellt der Score von 0,97 auf 0,07. Die Echo-Unterdrueckung nimmt dem
Signal nicht nur Pegel, sondern die Wakeword-Eigenschaft. Folge: in DIESER
Installation steht `while_speaking: true`.

Reichweite dieser Zahl: sie gilt fuer respeaker-Modus MIT `use_speaker` (nur
dann hat der XVF3800 die Referenz auf dem I2S-Ausgang) und fuer volume 0.8. Bei
`use_speaker: false` laeuft der Ton ueber ALSA, es gibt keine
Echo-Unterdrueckung, und dann ist die digitale Zahl die zutreffende — dort
gehoert `while_speaking: false`. Der Code-Default bleibt deshalb False.

Zwei Fallen, die dieser Lauf aufgedeckt hat (beide kosteten je einen Lauf)
------------------------------------------------------------------------
1. **Eine Ansage laeuft als PLAYING, nicht als ANNOUNCING.** Die erste Fassung
   der Senke wartete auf MediaPlayerState.ANNOUNCING, das nie kommt — jeder
   Satz lief in die 5-Sekunden-Startschranke und wurde dann "blind" abgewartet.
   Nebenwirkung fuer die Messung: der Hoerrahmen war dadurch 5-9 s laenger als
   die Ansage, und was in dieser Zeit im Raum passierte, landete als Score im
   Ergebnis (so entstanden die Werte 0,42-0,70 des ersten Laufs — NICHT aus dem
   Echo). Siehe RespeakerClient._BUSY_STATES.
2. **Speaches-WAVs luegen im Header.** `nframes` steht auf dem
   Streaming-Platzhalter 2147483647, bei 22050 Hz also 97391 Sekunden. Die
   Laenge muss aus den gelesenen Bytes kommen (siehe `_wav_seconds`), sonst
   gilt jede Wiedergabe als "nicht stattgefunden".

Und eine Lehre ueber dieses Werkzeug selbst: sein erstes Gueltigkeitskriterium
war ein geratener Mikrofon-Pegel (200). Gemessen liegt das Echo nach AEC bei
96-479 und der Raum bei 36 — die geratene Schwelle lag mitten im Messbereich
und erklaerte gueltige Laeufe fuer ungueltig. Jetzt entscheidet, ob die
Wiedergabe stattgefunden hat, und der Raum-Grundpegel wird gemessen statt
angenommen.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import threading
import time
import wave

# --- venv-Re-Exec wie in voice_assistant/__main__.py -----------------------
_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_VENV = os.path.join(_REPO, "ow-venv", "bin", "python")
if os.path.exists(_VENV) and os.path.realpath(sys.executable) != os.path.realpath(_VENV):
    os.execv(_VENV, [_VENV, "-m", "tools.bargein_echo_test", *sys.argv[1:]])

sys.path.insert(0, _REPO)

import numpy as np  # noqa: E402

from voice_assistant.bargein import BargeInDetector, BargeInHit, BargeInMiss  # noqa: E402
from voice_assistant.config import RATE_OW, Profile, load_profile  # noqa: E402
from voice_assistant.services.speaches import SpeachesState  # noqa: E402
from voice_assistant.services.tts import SpeachesTts, piper_synth  # noqa: E402

SERVICE_UNIT = "openclaw-voice-assist.service"

# Anteil der WAV-Laenge, den die Wiedergabe mindestens gedauert haben muss,
# damit sie als stattgefunden gilt. Das ist das Gueltigkeitskriterium dieses
# Werkzeugs — und es ist bewusst NICHT der Mikrofon-Pegel.
#
# Gelernt am 2026-09-20: die erste Fassung erklaerte eine Messung fuer
# ungueltig, wenn das Mikro leise blieb (geratene Schwelle 200). Gemessen liegt
# das Echo nach der Echo-Unterdrueckung des XVF3800 aber GENAU auf Raumniveau
# (110-340, einzelne Spitzen bis 1400) — der leise Mic-Pegel IST also das
# Ergebnis und nicht sein Fehlen. Was dagegen wirklich schiefgehen kann und
# beim ersten Lauf auch schiefging: es kommt gar kein Ton heraus. Das zeigt
# sich daran, dass play_wav sofort zurueckkehrt statt die Laenge der Datei
# abzuwarten — daran haengt die Gueltigkeit.
WIEDERGABE_MIN_ANTEIL = 0.6

# Saetze, die der Assistent im Betrieb WIRKLICH sagt. Die ersten vier sind der
# gefaehrliche Fall: die Bestaetigung wiederholt das Transkript, und das
# enthaelt wegen des Pre-Rolls sehr oft das Wakewort selbst (oder einen
# Verhoerer — "Gastau/Gastraum/Gastronom" stehen so in den Archiv-Transkripten).
STANDARD_SAETZE = [
    "Ich habe verstanden: Gaston, mach das Küchenlicht an.",
    "Ich habe verstanden: Gastau, Wohnzimmerrollo auf siebzig Prozent.",
    "Ich habe verstanden: Gaston, wie warm ist es draußen?",
    "Ich habe verstanden: Gastron, schalte den Monitor aus.",
    "Einen Moment, ich schaue nach.",
    "Das dauert noch einen Augenblick.",
    "Im Wohnzimmer sind es einundzwanzig Grad, draußen vierzehn.",
    "Erledigt, das Küchenlicht ist an.",
]


def _service_active() -> bool:
    result = subprocess.run(
        ["systemctl", "--user", "is-active", SERVICE_UNIT],
        capture_output=True, text=True,
    )
    return result.stdout.strip() == "active"


def _service_ctl(action: str) -> None:
    subprocess.run(["systemctl", "--user", action, SERVICE_UNIT], check=False)


def _detector(profile: Profile) -> BargeInDetector:
    """Detektor genau so bauen wie assistant.py es tut.

    Bewusst ueber dieselbe Config und dieselbe Fabrik: ein Messwerkzeug, das
    seinen eigenen Detektor zusammenstellt, misst irgendwann etwas anderes als
    der Betrieb.
    """
    from voice_assistant.assistant import _make_wakeword

    bi = profile.barge_in
    wakewords = bi.wakewords or profile.wakewords
    rms_min = bi.rms_min if bi.rms_min is not None else profile.wake_rms_min
    print(f"🔧 Detektor: {[w.bundle for w in wakewords]}, Pegel-Gate {rms_min:.0f}")
    return BargeInDetector(_make_wakeword(profile, wakewords), rms_min=rms_min)


def _render(profile: Profile, satz: str) -> str | None:
    """Satz als WAV rendern — bevorzugt Speaches (die echte Stimme), sonst Piper."""
    if profile.speaches_base:
        state = SpeachesState()
        tts = SpeachesTts(
            state, profile.speaches_base,
            profile.speaches_tts_model, profile.speaches_tts_voice,
        )
        audio = tts.synth(satz)
        if audio:
            import tempfile
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
                f.write(audio)
                return f.name
        print("   ⚠️  Speaches lieferte nichts → Piper-Fallback")
    return piper_synth(satz)


def _wav_seconds(path: str) -> float:
    """Laenge der Datei in Sekunden, aus den TATSAECHLICHEN Frames (0.0 wenn
    nicht lesbar).

    Bewusst nicht ueber ``getnframes()``: Speaches liefert seine WAVs mit dem
    Streaming-Platzhalter nframes=2147483647 im Header, was bei 22050 Hz rund
    97391 Sekunden ergibt. Die erste Fassung glaubte dem Header und erklaerte
    deshalb JEDE Wiedergabe fuer "nicht stattgefunden" (2026-09-20). Wer hier
    etwas aendert: die Laenge muss aus den gelesenen Bytes kommen.
    """
    try:
        with wave.open(path, "rb") as wf:
            rate, ch, sw = wf.getframerate(), wf.getnchannels(), wf.getsampwidth()
            roh = wf.readframes(wf.getnframes())
        teiler = rate * ch * sw
        return len(roh) / teiler if teiler else 0.0
    except Exception:
        return 0.0


def _wav_16k_mono(path: str) -> np.ndarray:
    """WAV als 16-kHz-mono-int16 lesen (wie die Pipeline es sieht)."""
    with wave.open(path, "rb") as wf:
        n_ch, rate = wf.getnchannels(), wf.getframerate()
        raw = wf.readframes(wf.getnframes())
    samples = np.frombuffer(raw, dtype=np.int16)
    if n_ch > 1:
        samples = samples.reshape(-1, n_ch)[:, 0]
    if rate != RATE_OW:
        from math import gcd
        from scipy.signal import resample_poly
        g = gcd(rate, RATE_OW)
        samples = np.clip(
            resample_poly(samples, RATE_OW // g, rate // g), -32768, 32767
        ).astype(np.int16)
    return samples


def _rms(samples: np.ndarray) -> float:
    if len(samples) == 0:
        return 0.0
    f = samples.astype(np.float64)
    return float(np.sqrt(np.mean(f * f)))


def _durch_detektor(det: BargeInDetector, samples: np.ndarray, chunk: int = 1280):
    """Samples chunkweise durch den Detektor; Treffer und Near-Misses sammeln."""
    treffer, near = [], []
    for i in range(0, len(samples) - chunk + 1, chunk):
        res = det.feed(samples[i:i + chunk])
        if isinstance(res, BargeInHit):
            treffer.append(res)
        elif isinstance(res, BargeInMiss):
            near.append(res)
    return treffer, near


def _bericht(zeilen: list, akustisch: bool = False, grundpegel: float = 0.0) -> int:
    """Ergebnis-Tabelle + Urteil. Rueckgabe = Exit-Code.

    zeilen: (satz, treffer, near, max_score, mic_rms, gespielt)

    max_score, mic_rms und gespielt sind hier nicht Beigabe, sondern der Grund,
    dass das Urteil eines ist: ein Lauf ohne Treffer kann zweierlei heissen —
    "die eigene Stimme kommt nicht durch" oder "es wurde gar nichts gespielt".
    Beim ersten akustischen Lauf am 2026-09-20 sah genau das gleich aus.
    """
    print("\n" + "=" * 72)
    print("ERGEBNIS")
    print("=" * 72)
    selbst_trigger = 0
    hoechster_score = 0.0
    nicht_gespielt = 0
    ueber_grundpegel = 0
    for satz, treffer, near, max_score, mic_rms, gespielt in zeilen:
        if akustisch and not gespielt:
            marke = "❌ NICHT GESPIELT"
            nicht_gespielt += 1
        elif treffer:
            marke = "🛑 SELBST-TRIGGER"
        elif near:
            marke = "⚡ Near-Miss"
        else:
            marke = "✓ still"
        pegel = f"Mic-RMS {mic_rms:6.0f}  " if akustisch else ""
        print(f"{marke:18s} Score max {max_score:.2f}  {pegel}{satz[:34]}")
        for t in treffer:
            print(f"                     → {t.hits} Frames, Peak {t.peak:.2f}, "
                  f"Fenster-RMS {t.rms:.0f}, Scores {t.scores[-6:]}")
        for n in near:
            print(f"                     → Near-Miss {n.failed_on}, {n.hits} Frames, "
                  f"Peak {n.peak:.2f}")
        selbst_trigger += len(treffer)
        hoechster_score = max(hoechster_score, max_score)
        if akustisch and grundpegel > 0 and mic_rms >= grundpegel * 1.5:
            ueber_grundpegel += 1

    print("-" * 72)
    print(f"Sätze: {len(zeilen)}   Selbst-Trigger: {selbst_trigger}   "
          f"höchster Score: {hoechster_score:.2f}")
    if akustisch and grundpegel > 0:
        print(f"Raum-Grundpegel: {grundpegel:.0f}   "
              f"Sätze messbar über dem Grundpegel: {ueber_grundpegel}/{len(zeilen)}")

    # Zuerst: hat ueberhaupt gespielt? Ein stummer Lautsprecher liefert kein
    # Ergebnis, sondern keins.
    if nicht_gespielt:
        print()
        print(f"MESSUNG UNGÜLTIG: bei {nicht_gespielt} von {len(zeilen)} Sätzen hat die")
        print("Wiedergabe nicht stattgefunden (play_wav kehrte sofort zurück).")
        print("Ohne Ton ist ein 'kein Selbst-Trigger' keine Aussage. Prüfen:")
        print("  1. Player-Zustand — eine Ansage läuft als PLAYING, nicht")
        print("     ANNOUNCING (siehe RespeakerClient._BUSY_STATES)")
        print("  2. Journal des ESP / der API-Verbindung")
        print("  3. respeaker.volume")
        return 2

    if selbst_trigger:
        print()
        print("URTEIL: So NICHT scharf schalten. Der Assistent würde sich an")
        print("diesen Stellen selbst unterbrechen. Wege, in dieser Reihenfolge:")
        print("  1. eigenes Bundle 'stopp_gaston' nachtrainieren: die Zwei-Wort-")
        print("     Phrase fällt in der eigenen Ansage praktisch nicht")
        print("  2. barge_in.rms_min anheben — hilft nur, wenn die eigene Ansage")
        print("     am Mikro deutlich leiser ankommt als ein Ruf aus dem Raum")
        print("     (die Fenster-RMS-Werte oben sagen, ob das so ist)")
        print("  3. im local-Modus: Lautsprecher-Pegel senken (dort gibt es")
        print("     keine Echo-Unterdrückung, im respeaker-Modus macht der")
        print("     XVF3800 das)")
        return 1

    print()
    if hoechster_score >= 0.5:
        print(f"URTEIL: kein Selbst-Trigger, aber der höchste Score lag bei")
        print(f"{hoechster_score:.2f} — das ist nicht viel Luft zur Gate-Schwelle.")
        print("Eine lautere Wiedergabe, ein anderer Raum oder ein neues Modell")
        print("kann das kippen. Nach jeder solchen Änderung neu messen.")
    else:
        print(f"URTEIL: kein Selbst-Trigger, höchster Score {hoechster_score:.2f} —")
        print("deutlicher Abstand zur Schwelle. Bleibt eine Stichprobe: nach")
        print("einem Modell-, Stimmen- oder Lautstärke-Wechsel neu messen.")
    return 0


def modus_digital(profile: Profile, saetze: list, wiederholungen: int = 1) -> int:
    """TTS-Sätze direkt durch den Detektor — ohne Lautsprecher und ohne Raum."""
    print("\n🔇 Modus: digital (kein Lautsprecher, kein Echo-Weg, untere Schranke)")
    det = _detector(profile)
    zeilen = []
    for satz in saetze * wiederholungen:
        print(f"\n▶ {satz}")
        pfad = _render(profile, satz)
        if not pfad:
            print("   ⚠️  konnte nicht gerendert werden — übersprungen")
            continue
        try:
            samples = _wav_16k_mono(pfad)
            det.reset()
            treffer, near = _durch_detektor(det, samples)
            print(f"   {len(samples) / RATE_OW:.1f}s Audio → "
                  f"{len(treffer)} Treffer, {len(near)} Near-Miss, "
                  f"Score max {det.max_score:.2f}")
            zeilen.append((satz, treffer, near, det.max_score, _rms(samples), True))
        finally:
            if os.path.exists(pfad):
                os.unlink(pfad)
    return _bericht(zeilen)


def modus_akustisch(profile: Profile, saetze: list, wiederholungen: int) -> int:
    """Sätze über den echten Lautsprecher spielen und dabei das Mikro mitlesen."""
    from voice_assistant.assistant import _make_audio

    print("\n🔊 Modus: akustisch (echter Lautsprecher + echtes Mikro, mit AEC)")
    war_aktiv = _service_active()
    if war_aktiv:
        print(f"⏸  Stoppe {SERVICE_UNIT} (Mic-Stream ist exklusiv) …")
        _service_ctl("stop")
        time.sleep(2.0)

    source, sink = _make_audio(profile)
    det = _detector(profile)
    zeilen = []
    grundpegel = 0.0
    try:
        source.start()
        time.sleep(2.0)  # Quelle/Verbindung hochfahren lassen

        # Grundpegel des Raums messen, solange nichts spielt — dieselbe
        # Vorgehensweise wie in wakeword_studio/recorder.py. Ohne diesen Bezug
        # ist ein absoluter Mic-RMS nicht deutbar: 150 kann "still" oder
        # "Echo kommt an" heissen, je nach Raum und Gain.
        print("\n🎚  Grundpegel des Raums messen (2 s, bitte still) …")
        ruhe: list = []
        ende = time.monotonic() + 2.0
        while time.monotonic() < ende:
            ruhe.append(source.read_chunk())
        from voice_assistant.wake_rms import loudest_window_rms
        grundpegel = (
            loudest_window_rms(np.concatenate(ruhe), rate=RATE_OW, window_ms=300)
            if ruhe else 0.0
        )
        print(f"   Grundpegel (lautestes 300-ms-Fenster): {grundpegel:.0f}")

        for runde in range(1, wiederholungen + 1):
            if wiederholungen > 1:
                print(f"\n--- Runde {runde}/{wiederholungen} ---")
            for satz in saetze:
                print(f"\n▶ {satz}")
                pfad = _render(profile, satz)
                if not pfad:
                    print("   ⚠️  konnte nicht gerendert werden — übersprungen")
                    continue
                det.reset()
                source.flush()
                fertig = threading.Event()

                dauer_soll = _wav_seconds(pfad)
                gespielt_s = [0.0]

                def _spielen(p=pfad, out=gespielt_s):
                    t0 = time.monotonic()
                    try:
                        sink.play_wav(p)
                    finally:
                        out[0] = time.monotonic() - t0
                        fertig.set()

                t = threading.Thread(target=_spielen, daemon=True)
                t.start()
                treffer, near, chunks = [], [], 0
                gehoert: list = []
                # Noch kurz ueber das Ende hinaus lesen: der Nachhall im Raum
                # ist Teil des Echo-Wegs und kann den Streak vollenden.
                nachlauf_bis = None
                while True:
                    c = source.read_chunk()
                    gehoert.append(c)
                    res = det.feed(c)
                    chunks += 1
                    if isinstance(res, BargeInHit):
                        treffer.append(res)
                    elif isinstance(res, BargeInMiss):
                        near.append(res)
                    if fertig.is_set():
                        if nachlauf_bis is None:
                            nachlauf_bis = time.monotonic() + 1.0
                        elif time.monotonic() >= nachlauf_bis:
                            break
                    if chunks > 2000:  # Reissleine (~80 s), falls nichts endet
                        print("   ⚠️  Abbruch: Wiedergabe endete nicht")
                        break
                if os.path.exists(pfad):
                    os.unlink(pfad)
                # Pegel des LAUTESTEN 300-ms-Fensters, nicht der Mittelwert
                # ueber alles: die Ansage ist kuerzer als das Zeitfenster, und
                # ein Mittelwert ueber die umgebende Stille wuerde jede
                # Wiedergabe kleinrechnen.
                mic = 0.0
                if gehoert:
                    alles = np.concatenate(gehoert)
                    from voice_assistant.wake_rms import loudest_window_rms
                    mic = loudest_window_rms(alles, rate=RATE_OW, window_ms=300)
                gespielt = (
                    dauer_soll > 0.0
                    and gespielt_s[0] >= dauer_soll * WIEDERGABE_MIN_ANTEIL
                )
                print(f"   {chunks} Chunks gehört → "
                      f"{len(treffer)} Treffer, {len(near)} Near-Miss, "
                      f"Score max {det.max_score:.2f}, Mic-RMS {mic:.0f}, "
                      f"Wiedergabe {gespielt_s[0]:.1f}s/{dauer_soll:.1f}s"
                      f"{'' if gespielt else '  ❌ NICHT GESPIELT'}")
                zeilen.append((satz, treffer, near, det.max_score, mic, gespielt))
    finally:
        try:
            source.close()
        except Exception:
            pass
        if war_aktiv:
            print(f"\n▶️  Starte {SERVICE_UNIT} wieder …")
            _service_ctl("start")
    return _bericht(zeilen, akustisch=True, grundpegel=grundpegel)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Barge-in gegen die eigene Stimme messen",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument("modus", choices=["digital", "akustisch"])
    ap.add_argument("--saetze", help="Textdatei, ein Satz je Zeile (statt der Standard-Sätze)")
    ap.add_argument("--wiederholungen", type=int, default=1,
                    help="Durchläufe über die Satzliste. Mehr als einer ist die "
                         "Regel, nicht die Ausnahme — siehe Docstring: die "
                         "TTS rendert denselben Satz jedes Mal anders.")
    args = ap.parse_args()

    profile = load_profile()
    if args.saetze:
        with open(args.saetze) as f:
            saetze = [z.strip() for z in f if z.strip()]
    else:
        saetze = STANDARD_SAETZE
    if not saetze:
        print("Keine Sätze zu messen.")
        return 2

    print(f"Profil: {profile.name} (mode {profile.mode})")
    if not profile.barge_in.enabled:
        print("ℹ️  barge_in ist im Profil AUS — gemessen wird trotzdem, mit den")
        print("   Werten, die beim Scharfschalten gelten würden.")

    wdh = max(1, args.wiederholungen)
    if args.modus == "digital":
        return modus_digital(profile, saetze, wdh)
    return modus_akustisch(profile, saetze, wdh)


if __name__ == "__main__":
    sys.exit(main())
