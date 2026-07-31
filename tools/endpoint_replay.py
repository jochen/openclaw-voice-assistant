#!/usr/bin/env python3
"""Endpointing-Parameter gegen das Aufnahme-Archiv messen statt raten.

Aufruf (Projekt-venv wird selbst gesucht):
    ow-venv/bin/python -m tools.endpoint_replay
    ow-venv/bin/python -m tools.endpoint_replay --nachlauf 1.0 --deckel 8.0
    ow-venv/bin/python -m tools.endpoint_replay --stt      # mit Wort-Beleg

Was hier gemessen wird
----------------------
Jede *_rec.wav im Trigger-Archiv ist eine vollständige Aufnahme, so wie sie
damals endete. Das Werkzeug spielt dieselbe Endpointing-Logik wie
`assistant.py` chunkweise darüber und sagt, WANN eine andere Parametrierung
die Aufnahme beendet hätte. Der Vergleich ist dadurch exakt, nicht simuliert:
Chunk-Größe, VAD-Aggressivität und der ODER-Entscheid über die 20-ms-Frames
sind identisch zum laufenden Betrieb.

Die eigentliche Frage — "schneidet ein kürzerer Nachlauf echte Kommandos ab?"
— beantwortet nur `--stt`: dabei wird das AM SIMULIERTEN SCHNITTPUNKT gekürzte
Audio erneut durch die STT geschickt und mit dem Transkript der vollen
Aufnahme verglichen. Alles ohne `--stt` sind Zeitangaben, keine Belege.

Warum das Archiv als Test-Set taugt (und wo nicht)
--------------------------------------------------
Es ist Betriebsmaterial, keine gestellte Aufnahme: echte Sprecher, echte
Störquellen, echte Abstände. Aber es ist NICHT gelabelt — welche Aufnahme
Fernseher enthielt und welche nicht, steht nirgends. Deshalb bezieht sich die
Auswertung auf das, was aus dem Turn geworden ist (wake_events.log: ausgang,
ein_satz; actuator_turns.log: war es ein Schaltbefehl). Ein Kommando, das
damals ausgeführt wurde, ist der belastbarste positive Fall, den es hier gibt.

Ein Vorbehalt aus der Praxis: die Ein-Satz-Einstufung im Log ist selbst
fehlbar — sie entsteht 0,4 s nach dem Trigger aus dem VAD, und Störgeräusch
kann sie auslösen (Vorfall 2026-08-01). Turns mit ein_satz=true sind also
KEINE reine Kommando-Menge.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import wave
from collections import Counter

import numpy as np

# --- venv-Re-Exec wie in voice_assistant/__main__.py -----------------------
_VENV = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "ow-venv", "bin", "python")
if os.path.exists(_VENV) and os.path.realpath(sys.executable) != os.path.realpath(_VENV):
    os.execv(_VENV, [_VENV, "-m", "tools.endpoint_replay", *sys.argv[1:]])

import webrtcvad  # noqa: E402

from voice_assistant.assistant import (  # noqa: E402
    _COMMAND_MIN_SPEECH_SEC,
    _PRE_ROLL_SEC,
)
from voice_assistant.config import (  # noqa: E402
    RATE_OW,
    RECORDING_MAX_SEC,
    TRIGGER_AUDIO_DIR,
    WAKE_LOG_PATH,
    WORKSPACE,
    load_profile,
)

ACTUATOR_LOG = os.path.join(WORKSPACE, "actuator_turns.log")


def _lade_jsonl(pfad: str) -> list[dict]:
    if not os.path.exists(pfad):
        return []
    zeilen = []
    with open(pfad) as f:
        for zeile in f:
            zeile = zeile.strip()
            if not zeile:
                continue
            try:
                zeilen.append(json.loads(zeile))
            except json.JSONDecodeError:
                continue
    return zeilen


def _turn_infos() -> dict[str, dict]:
    """Pro Wake-Clip-Dateiname: ein_satz, ausgang, Transkript.

    wake_events.log bezieht alle drei Zeilen-Arten (trigger/ack/outcome) auf
    denselben Dateinamen — das ist die einzige Klammer zwischen Audio und dem,
    was aus dem Turn wurde.
    """
    infos: dict[str, dict] = {}
    for row in _lade_jsonl(WAKE_LOG_PATH):
        audio = row.get("audio")
        if not audio:
            continue
        eintrag = infos.setdefault(audio, {})
        if row.get("result") == "ack":
            eintrag["ein_satz"] = bool(row.get("ein_satz"))
        elif row.get("result") == "outcome":
            eintrag["ausgang"] = row.get("ausgang")
            if row.get("transcript"):
                eintrag["transcript"] = row["transcript"]
    return infos


def _aktuator_transkripte() -> set[str]:
    return {
        (r.get("transcript") or "").strip()
        for r in _lade_jsonl(ACTUATOR_LOG)
        if r.get("transcript")
    }


def _lies_wav(pfad: str) -> np.ndarray:
    with wave.open(pfad) as w:
        return np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)


def _schnitt(
    audio: np.ndarray,
    vad: webrtcvad.Vad,
    chunk: int,
    nachlauf_chunks: int,
    dialog_nachlauf_chunks: int,
    min_sprach_chunks: int,
    deckel_sec: float,
    dialog_deckel_sec: float,
    rms_min: float,
    vorlauf_sec: float,
) -> tuple[float | None, float, float]:
    """(Schnittzeitpunkt in s oder None, Gesamtdauer in s, letzte Sprache in s).

    Die dritte Zahl ist die entscheidende: liegt der Schnitt (abzüglich seines
    Nachlaufs) VOR der letzten Sprache der Originalaufnahme, dann hat der
    Schnitt gesprochenes Material entfernt. Nur diese Fälle sind überhaupt
    verdächtig — alles andere kann per Konstruktion nichts verlieren, weil ein
    kürzerer Nachlauf nur hinten Stille wegnimmt. Das ist beweisbar aus der
    VAD-Spur und braucht keine STT; die STT klärt danach nur noch, ob das
    entfernte Material zum Kommando gehörte oder Störgeräusch war.

    Bildet STATE_RECORDING aus assistant.py nach: Sprache setzt den
    Stille-Zähler zurück, Stille zählt ihn hoch, ab nachlauf_chunks ist Schluss.
    Inklusive der Sperre — bis min_sprach_chunks Sprache zusammengekommen ist,
    gilt der Dialog-Nachlauf, sonst würde der Ausklang des Wakewords genügen,
    um eine Aufnahme in der Denkpause danach zu beenden.
    vorlauf_sec überspringt den Pre-Roll: der steht zwar in der Datei, ist im
    Betrieb aber schon aufgenommen, BEVOR die Aufnahme beginnt — der VAD sieht
    ihn nie. Ohne dieses Überspringen zählt das Wakeword selbst als Sprache,
    und jede Sperre, die sich auf gesprochene Menge stützt, misst sich blind.
    Auch der Deckel läuft ab dem Trigger, nicht ab Dateibeginn.

    None = die Aufnahme wäre bis zum Deckel bzw. Dateiende gelaufen.
    """
    frame = int(RATE_OW * 20 / 1000)
    dauer = len(audio) / RATE_OW
    sprache = False
    stille = 0
    sprach_chunks = 0
    schnitt: float | None = None
    letzte_sprache = 0.0
    start = int(RATE_OW * min(vorlauf_sec, dauer)) // chunk * chunk
    # Bis zum Dateiende durchlaufen, auch nach dem Schnitt: letzte_sprache muss
    # die GANZE Originalaufnahme kennen, sonst ist die Verlustprüfung blind.
    for k in range(start, len(audio) - chunk, chunk):
        c = audio[k:k + chunk]
        t = (k + chunk) / RATE_OW
        seit_trigger = t - start / RATE_OW
        scharf = sprach_chunks >= min_sprach_chunks
        rms = float(np.sqrt(np.mean(c.astype(np.float32) ** 2)))
        ist_sprache = False
        if rms_min <= 0.0 or rms >= rms_min:
            for j in range(0, chunk, frame):
                f = c[j:j + frame]
                if len(f) == frame and vad.is_speech(f.tobytes(), RATE_OW):
                    ist_sprache = True
                    break
        if ist_sprache:
            letzte_sprache = t
        if schnitt is not None:
            continue
        if seit_trigger > (deckel_sec if scharf else dialog_deckel_sec):
            schnitt = t
            continue
        if ist_sprache:
            sprache = True
            stille = 0
            sprach_chunks += 1
        elif sprache:
            stille += 1
            grenze = nachlauf_chunks if scharf else dialog_nachlauf_chunks
            if stille >= grenze:
                schnitt = t
    return schnitt, dauer, letzte_sprache


def _stt_gekuerzt(stt, audio: np.ndarray, bis_sec: float) -> str:
    # normalize=True wie im Betrieb (SttPipeline.run) — ohne das ist der
    # Vergleich mit dem archivierten Transkript nicht derselbe Versuch.
    from voice_assistant.services.stt import chunks_to_wav_bytes
    stueck = audio[:int(RATE_OW * bis_sec)]
    wav = chunks_to_wav_bytes([stueck], normalize=True)
    return (stt.transcribe(wav) or "").strip()


def _woerter(text: str) -> list[str]:
    return [w for w in "".join(
        c.lower() if c.isalnum() or c.isspace() else " " for c in (text or "")
    ).split() if w]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--nachlauf", type=float, default=None,
                    help="Stille bis Aufnahme-Ende in s (Default: command_silence_seconds des Profils)")
    ap.add_argument("--deckel", type=float, default=None,
                    help="harter Deckel in s (Default: command_max_seconds des Profils)")
    ap.add_argument("--min-sprache", type=float, default=_COMMAND_MIN_SPEECH_SEC,
                    help="Sprache in s, bevor der Kommando-Nachlauf greifen darf "
                         f"(Default {_COMMAND_MIN_SPEECH_SEC})")
    ap.add_argument("--rms-min", type=float, default=None,
                    help="Pegelschwelle für Sprache (Default: vad_voice_rms_min des Profils)")
    ap.add_argument("--stt", action="store_true",
                    help="gekürztes Audio erneut transkribieren und mit dem "
                         "Original vergleichen — nur so wird 'zu früh geschnitten' belegt")
    ap.add_argument("--speaches", default=None,
                    help="abweichende Speaches-Basis-URL (das Profil zeigt auf "
                         "den Host, auf dem der Assistent läuft — für eine "
                         "Auswertung woanders hier umbiegen)")
    ap.add_argument("--nur-kommandos", action="store_true",
                    help="nur Turns, aus denen ein ausgeführter Schaltbefehl wurde")
    ap.add_argument("--nur-ein-satz", action="store_true",
                    help="nur Turns, die damals als Ein-Satz eingestuft wurden — "
                         "NUR die bekommen im Betrieb das Kommando-Endpointing. "
                         "Ohne diesen Schalter misst man auch Turns mit, die "
                         "real weiter im Dialog-Modus laufen (zu pessimistisch)")
    args = ap.parse_args()

    if not os.path.isdir(TRIGGER_AUDIO_DIR):
        print(f"Kein Trigger-Archiv unter {TRIGGER_AUDIO_DIR}")
        return 1

    profil = load_profile()
    nachlauf = args.nachlauf if args.nachlauf is not None else profil.command_silence_seconds
    deckel = args.deckel if args.deckel is not None else profil.command_max_seconds
    rms_min = args.rms_min if args.rms_min is not None else profil.vad_voice_rms_min
    min_sprache = args.min_sprache
    vad = webrtcvad.Vad(profil.vad_aggressiveness)

    infos = _turn_infos()
    akt_tx = _aktuator_transkripte()

    stt = None
    if args.stt:
        from voice_assistant.services.speaches import SpeachesState
        from voice_assistant.services.stt import SpeachesStt
        stt = SpeachesStt(
            SpeachesState(),
            args.speaches or profil.speaches_base,
            profil.speaches_stt_model,
        )

    dateien = sorted(n for n in os.listdir(TRIGGER_AUDIO_DIR) if n.endswith("_rec.wav"))
    if not dateien:
        print("Keine Aufnahmen (*_rec.wav) im Archiv.")
        return 0

    print(f"Profil {profil.name}: Kommando-Nachlauf {nachlauf:.2f}s ab {min_sprache:.1f}s "
          f"Sprache, Deckel {deckel:.0f}s | Dialog {profil.silence_seconds:.1f}s/"
          f"{RECORDING_MAX_SEC:.0f}s | rms_min {rms_min:.0f}, VAD {profil.vad_aggressiveness}")
    print(f"{len(dateien)} Aufnahmen im Archiv")
    if not args.nur_ein_satz:
        print("HINWEIS: ohne --nur-ein-satz bekommt hier JEDE Aufnahme das "
              "Kommando-Endpointing. Im Betrieb trifft das nur Ein-Satz-Turns "
              "— diese Zahlen sind also eine Was-waere-wenn-Rechnung, nicht "
              "das erwartete Verhalten.")
    print()

    ergebnisse = []
    for name in dateien:
        pfad = os.path.join(TRIGGER_AUDIO_DIR, name)
        try:
            audio = _lies_wav(pfad)
        except (wave.Error, OSError):
            continue
        if len(audio) < RATE_OW // 4:
            continue
        wake_name = name.replace("_rec.wav", "_wake.wav")
        info = infos.get(wake_name, {})
        transcript = (info.get("transcript") or "").strip()
        ist_kommando = transcript in akt_tx and bool(transcript)
        if args.nur_kommandos and not ist_kommando:
            continue
        if args.nur_ein_satz and not info.get("ein_satz"):
            continue
        # 80-ms-Chunks: die Chunk-Größe des ALSA-16k-Pfads. Die Aufnahmen im
        # Archiv sind bereits auf 16 kHz normalisiert, die reale Chunk-Größe
        # der Quelle ist daran nicht mehr ablesbar.
        chunk = 1280
        chunk_sec = chunk / RATE_OW
        t, dauer, letzte_sprache = _schnitt(
            audio,
            vad,
            chunk,
            nachlauf_chunks=max(1, round(nachlauf / chunk_sec)),
            dialog_nachlauf_chunks=max(1, round(profil.silence_seconds / chunk_sec)),
            min_sprach_chunks=max(1, round(min_sprache / chunk_sec)),
            deckel_sec=deckel,
            dialog_deckel_sec=RECORDING_MAX_SEC,
            rms_min=rms_min,
            vorlauf_sec=_PRE_ROLL_SEC,
        )
        schnitt = t if t is not None else dauer
        ergebnisse.append({
            "name": name,
            "dauer": dauer,
            "schnitt": schnitt,
            "lief_durch": t is None,
            # Sprache jenseits des Schnitts = tatsächlich entferntes Material.
            # Ein halber Chunk Toleranz, damit Rundung am Chunk-Raster nicht
            # als Verlust durchgeht.
            "verlust_s": max(0.0, letzte_sprache - schnitt - 0.04),
            "ein_satz": info.get("ein_satz"),
            "ausgang": info.get("ausgang"),
            "kommando": ist_kommando,
            "transcript": transcript,
        })

    gespart = [e["dauer"] - e["schnitt"] for e in ergebnisse]
    lang_vorher = [e for e in ergebnisse if e["dauer"] > 8.0]
    lang_nachher = [e for e in ergebnisse if e["schnitt"] > 8.0]
    print(f"{'':24} {'vorher':>8} {'nachher':>8}")
    print(f"{'Dauer Median':24} {statistics.median(e['dauer'] for e in ergebnisse):7.1f}s "
          f"{statistics.median(e['schnitt'] for e in ergebnisse):7.1f}s")
    print(f"{'Dauer Maximum':24} {max(e['dauer'] for e in ergebnisse):7.1f}s "
          f"{max(e['schnitt'] for e in ergebnisse):7.1f}s")
    print(f"{'Aufnahmen ueber 8s':24} {len(lang_vorher):7d}  {len(lang_nachher):7d}")
    print(f"\nEingespart im Median: {statistics.median(gespart):.1f}s "
          f"(Summe {sum(gespart):.0f}s ueber {len(ergebnisse)} Aufnahmen)")

    nach_ausgang = Counter(e["ausgang"] or "unbekannt" for e in ergebnisse)
    print("Ausgaenge:", ", ".join(f"{k}={v}" for k, v in nach_ausgang.most_common()))

    gekuerzt = [e for e in ergebnisse if e["schnitt"] < e["dauer"] - 0.1]
    verdacht = [e for e in ergebnisse if e["verlust_s"] > 0.0]
    print(f"\n{len(gekuerzt)} Aufnahmen werden gekuerzt, davon entfernen "
          f"{len(verdacht)} gesprochenes Material (VAD-Spur, ohne STT).")
    if not verdacht:
        print("Alle Schnitte liegen hinter der letzten Sprache — es kann "
              "konstruktionsbedingt nichts verloren gehen.")
        return 0
    for e in sorted(verdacht, key=lambda e: -e["verlust_s"]):
        marke = "KOMMANDO" if e["kommando"] else "        "
        print(f"  {marke} {e['name']}  {e['dauer']:.1f}s → {e['schnitt']:.1f}s "
              f"(entfernt {e['verlust_s']:.1f}s Sprache, Ausgang: {e['ausgang']})")

    if not stt:
        print("\nOb das entfernte Material zum Kommando gehoerte oder "
              "Stoergeraeusch war, klaert nur --stt.")
        return 0

    print("\n--- Wortvergleich der Verdachtsfaelle (STT) ---")
    for e in verdacht:
        if not e["transcript"]:
            continue
        audio = _lies_wav(os.path.join(TRIGGER_AUDIO_DIR, e["name"]))
        neu = _stt_gekuerzt(stt, audio, e["schnitt"])
        marke = "KOMMANDO" if e["kommando"] else "        "
        print(f"  {marke} {e['name']}  {e['dauer']:.1f}s → {e['schnitt']:.1f}s")
        print(f"      voll:     {e['transcript'][:90]!r}")
        print(f"      gekuerzt: {neu[:90]!r}")
    print("\nDie Einstufung 'Kommando zerschnitten oder Stoergeraeusch "
          "entfernt' bleibt hier bewusst beim Leser — automatisch getroffen "
          "war sie in einem frueheren Anlauf falsch (STT-Wortvarianten wie "
          "'Gastro-Monitor an' vs. 'Gastro Monitoren' sahen aus wie Verlust).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
