"""Phase A des Wakeword-Studios: geführte echte Wakeword-Aufnahmen.

Nutzt denselben Mic-Pfad wie der Assistant (Profil aus config.yaml, Modus
local oder respeaker) — die Samples klingen also exakt so, wie das Modell sie
live zu hören bekommt (inkl. ReSpeaker-Gain/DC-Filter). Der laufende
Assistant-Service wird für die Dauer der Session gestoppt (Mic-Stream ist
exklusiv) und danach wieder gestartet.

Gegen Fehlstarts durch Nebengeräusche: beim Session-Start wird der
Grundpegel des Raums gemessen und die RMS-Schwelle darüber gelegt, und ein
Take beginnt erst bei zwei Sprach-Chunks in Folge. Nach [Enter] gibt es eine
kurze Totzeit (Tastenklick!) plus "Jetzt!"-Cue; während der Aufnahme zeigt
der LED-Ring/WLED die Recording-Phase. Jeder Take wird nach dem Scoring
automatisch vorgespielt (--no-play schaltet das ab).

Ablage: models/wakewords/<bundle>/samples/<sprecher>/<ts>_<stil>.wav
plus eine Metadaten-Zeile pro Take in samples/sessions.jsonl. Das samples/-
Verzeichnis ist im Projekt-Git ignoriert (Familienstimmen!) und wird als
eigenes privates Git-Repo versioniert (siehe samples/README.md).
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import time
import wave
from collections import deque
from datetime import datetime

import numpy as np
import webrtcvad

from voice_assistant.config import (
    RATE_OW,
    VAD_FRAME_SIZE,
    WAKEWORDS_DIR,
    Profile,
    load_profile,
)
from voice_assistant.services.leds import (
    LED_IDLE,
    LED_RECORDING,
    LedDirector,
)

SERVICE_UNIT = "openclaw-voice-assist.service"

# Aufnahme-Parameter (Wakewords sind kurz — enge Fenster halten Takes sauber)
PREROLL_SEC = 0.32       # Audio vor dem ersten Sprach-Chunk mitnehmen
WAIT_SPEECH_SEC = 15.0   # max. Wartezeit auf Sprachbeginn je Take
MAX_SPEECH_SEC = 3.0     # Hard-Cap ab Sprachbeginn
END_SILENCE_SEC = 0.6    # so viel Stille beendet den Take
TRAIL_KEEP_SEC = 0.25    # Reststille am Ende, die im Sample bleibt
ARM_DELAY_SEC = 0.7      # Totzeit nach [Enter] (Tastenklick nicht aufnehmen)
SPEECH_START_CHUNKS = 2  # so viele Sprach-Chunks in Folge starten den Take
CALIBRATE_SEC = 1.5      # Grundpegel-Messung beim Session-Start
NOISE_RMS_FACTOR = 2.5   # Schwelle = Faktor × gemessener Grundpegel
NOISE_RMS_FLOOR = 120.0  # Untergrenze in leisen Räumen (Grundpegel ~20 → 2.5× wäre
                         # immer noch unter Rascheln/Klicken; Flüstern liegt deutlich höher)
MIN_SPEECH_SEC = 0.35    # kürzere Takes werden als vermutliches Störgeräusch markiert

# Geführte Varianten — werden zyklisch durchlaufen. Ziel: die Streuung des
# Alltags abdecken (Distanz, Tempo, Lautstärke, Winkel), nicht Studioqualität.
VARIATIONS: list[tuple[str, str]] = [
    ("normal", "Normal — so, wie du den Assistenten im Alltag rufst"),
    ("normal", "Noch einmal ganz normal"),
    ("schnell", "Schnell und beiläufig"),
    ("deutlich", "Langsam und überdeutlich"),
    ("leise", "Leise, fast geflüstert"),
    ("laut", "Laut, wie quer durch den Raum gerufen"),
    ("fern", "Aus zwei bis drei Metern Entfernung, normal"),
    ("fern-laut", "Aus zwei bis drei Metern, laut gerufen"),
    ("abgewandt", "Vom Mikrofon abgewandt sprechen"),
    ("beilaeufig", "Im Vorbeigehen / mitten aus einer Bewegung heraus"),
]


# ---------------------------------------------------------------------------
# Service-, Audio- und LED-Hilfen
# ---------------------------------------------------------------------------

def _service_active() -> bool:
    result = subprocess.run(
        ["systemctl", "--user", "is-active", SERVICE_UNIT],
        capture_output=True, text=True,
    )
    return result.stdout.strip() == "active"


def _service_ctl(action: str) -> None:
    subprocess.run(["systemctl", "--user", action, SERVICE_UNIT], check=False)


def _make_source(profile: Profile):
    if profile.mode == "respeaker":
        from voice_assistant.audio.respeaker import RespeakerSource

        return RespeakerSource(profile.respeaker)
    from voice_assistant.audio.alsa import AlsaSource

    source = AlsaSource(profile.local_audio)
    source.start()
    return source


def _make_sink(profile: Profile):
    if profile.mode == "respeaker":
        from voice_assistant.audio.respeaker import RespeakerSink

        return RespeakerSink(profile.respeaker)
    from voice_assistant.audio.alsa import AlsaSink

    return AlsaSink(profile.local_audio.playback_device)


def _make_leds(profile: Profile) -> LedDirector:
    sinks = []
    if profile.leds.wled_enabled and profile.mode == "local":
        from voice_assistant.services.leds import WledLeds

        sinks.append(WledLeds(profile.leds.wled_host, enabled=True))
    if profile.leds.respeaker_ring_enabled and profile.mode == "respeaker":
        from voice_assistant.services.leds import RespeakerRing

        sinks.append(RespeakerRing(profile.respeaker, enabled=True))
    return LedDirector(*sinks)


def _kick_respeaker(source) -> None:
    """ReSpeaker: neue voice_assistant-Session anstoßen, falls der Stream steht."""
    client = getattr(source, "_client", None)
    if client is not None:
        client.press_start_button()


def _wait_for_stream(source, timeout: float = 25.0) -> bool:
    """Wartet bis echte (nicht-stumme) Audio-Daten fließen."""
    deadline = time.time() + timeout
    kicked = False
    while time.time() < deadline:
        chunk = source.read_chunk()
        if len(chunk) and np.any(chunk):
            return True
        if not kicked and time.time() > deadline - timeout / 2:
            _kick_respeaker(source)
            kicked = True
    return False


def _rms(chunk: np.ndarray) -> float:
    return float(np.sqrt(np.mean(chunk.astype(np.float32) ** 2)))


def _measure_noise(source, seconds: float = CALIBRATE_SEC) -> float:
    """Median-RMS des Raum-Grundpegels (Session-Start, niemand spricht)."""
    values: list[float] = []
    deadline = time.time() + seconds
    while time.time() < deadline:
        chunk = source.read_chunk()
        if len(chunk) and np.any(chunk):
            values.append(_rms(chunk))
    return float(np.median(values)) if values else 0.0


# Sprach-Erkennung pro Chunk — gleiche Logik wie assistant._is_speech_chunk
# (bewusst kopiert statt importiert: assistant.py zieht den kompletten
# Service-Stack mit herein).
def _is_speech_chunk(vad: webrtcvad.Vad, audio_16: np.ndarray, min_rms: float) -> bool:
    if min_rms > 0.0 and _rms(audio_16) < min_rms:
        return False
    result = False
    for i in range(0, len(audio_16), VAD_FRAME_SIZE):
        frame = audio_16[i : i + VAD_FRAME_SIZE]
        if len(frame) == VAD_FRAME_SIZE:
            result |= vad.is_speech(frame.tobytes(), RATE_OW)
    return result


def _record_take(source, vad: webrtcvad.Vad, min_rms: float) -> tuple[np.ndarray, float] | None:
    """Ein Take: auf Sprache warten, bis Stille aufnehmen, zuschneiden.

    Startet erst bei SPEECH_START_CHUNKS Sprach-Chunks in Folge — einzelne
    Störgeräusch-Chunks (Klicken, Rascheln) lösen keinen Take aus.
    Liefert (Samples, Sprachdauer in s) oder None (Timeout ohne Sprache).
    """
    source.flush()
    preroll: deque[np.ndarray] = deque()
    preroll_samples = 0
    pending: list[np.ndarray] = []   # Sprach-Kandidaten vor dem Start-Gate
    speech_chunks: list[np.ndarray] = []
    speech_chunk_count = 0
    silence_run = 0          # Chunks Stille seit dem letzten Sprach-Chunk
    speech_started = 0.0
    started = time.time()
    kicked = False

    def _push_preroll(chunk: np.ndarray) -> None:
        nonlocal preroll_samples
        preroll.append(chunk)
        preroll_samples += len(chunk)
        while preroll_samples - len(preroll[0]) >= int(PREROLL_SEC * RATE_OW):
            preroll_samples -= len(preroll.popleft())

    while True:
        chunk = source.read_chunk()
        if not len(chunk):
            continue
        chunk_sec = len(chunk) / RATE_OW
        is_speech = _is_speech_chunk(vad, chunk, min_rms)

        if not speech_chunks:
            if is_speech:
                pending.append(chunk.copy())
                if len(pending) >= SPEECH_START_CHUNKS:
                    speech_chunks = pending
                    speech_chunk_count = len(pending)
                    pending = []
                    speech_started = time.time()
            else:
                for p in pending:
                    _push_preroll(p)
                pending = []
                _push_preroll(chunk.copy())
                waited = time.time() - started
                if not kicked and waited > WAIT_SPEECH_SEC / 2 and not np.any(chunk):
                    _kick_respeaker(source)  # Stream steht (nur Nullen) → neu anstoßen
                    kicked = True
                if waited > WAIT_SPEECH_SEC:
                    return None
            continue

        speech_chunks.append(chunk.copy())
        if is_speech:
            speech_chunk_count += 1
            silence_run = 0
        else:
            silence_run += 1

        speech_sec = speech_chunk_count * chunk_sec
        if silence_run * chunk_sec >= END_SILENCE_SEC:
            trim = int(max(0, silence_run * len(chunk) - TRAIL_KEEP_SEC * RATE_OW))
            samples = np.concatenate(list(preroll) + speech_chunks)
            return (samples[: len(samples) - trim] if trim else samples), speech_sec
        if time.time() - speech_started > MAX_SPEECH_SEC:
            return np.concatenate(list(preroll) + speech_chunks), speech_sec


def _save_wav(path: str, samples: np.ndarray) -> None:
    with wave.open(path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(RATE_OW)
        wf.writeframes(samples.astype(np.int16).tobytes())


# ---------------------------------------------------------------------------
# Geführte Session
# ---------------------------------------------------------------------------

def _score_line(verdict: dict) -> str:
    mark = "✅ würde triggern" if verdict["triggered"] else "❌ würde NICHT triggern"
    return (
        f"max_score={verdict['max_score']:.2f}  streak={verdict['best_streak']} "
        f"robust={verdict['robust']} (Threshold {verdict['threshold']:.2f}) → {mark}"
    )


def run_record(args) -> int:
    profile = load_profile()

    bundle_dir = os.path.join(WAKEWORDS_DIR, args.bundle)
    if not os.path.isdir(bundle_dir):
        print(f"❌ Bundle nicht gefunden: {bundle_dir}")
        return 1

    speaker = "".join(
        c for c in args.speaker.strip().lower().replace(" ", "-")
        if c.isalnum() or c in "-_"
    )
    if not speaker:
        print("❌ Ungültiger Sprechername")
        return 1

    samples_dir = os.path.join(bundle_dir, "samples")
    speaker_dir = os.path.join(samples_dir, speaker)
    os.makedirs(speaker_dir, exist_ok=True)

    print("🔧 Lade Wakeword-Modell zum Sofort-Scoring …")
    from wakeword_studio.scoring import BundleScorer

    scorer = BundleScorer(args.bundle)

    print(
        f"\n🎙  Wakeword-Studio — Phase A: echte Aufnahmen\n"
        f"    Wakeword: {scorer.display}  (Bundle '{args.bundle}', Threshold {scorer.threshold:.2f})\n"
        f"    Sprecher: {speaker}\n"
        f"    Profil:   {profile.name} (mode: {profile.mode})\n"
        f"    Ziel:     {args.takes} Takes → {speaker_dir}/\n"
    )

    was_active = _service_active()
    if was_active and not args.keep_service:
        print(f"⏸  Stoppe {SERVICE_UNIT} (Mic-Stream ist exklusiv) …")
        _service_ctl("stop")
    elif was_active:
        print("⚠️  --keep-service: Assistant läuft weiter — im ReSpeaker-Modus wird das nicht funktionieren!")

    source = None
    sink = None
    leds = LedDirector()
    accepted: list[dict] = []
    try:
        source = _make_source(profile)
        print("⏳ Warte auf Audio-Stream …")
        if not _wait_for_stream(source):
            print("❌ Kein Audio vom Mikrofon — läuft der ReSpeaker / ist das Gerät frei?")
            return 1

        leds = _make_leds(profile)
        leds.set_phase(LED_IDLE)

        if args.min_rms is not None:
            min_rms = args.min_rms
            print(f"✅ Audio läuft. RMS-Schwelle (manuell): {min_rms:.0f}\n")
        else:
            print("🤫 Bitte kurz still sein — messe den Grundpegel des Raums …")
            noise = _measure_noise(source)
            min_rms = max(profile.vad_voice_rms_min, noise * NOISE_RMS_FACTOR, NOISE_RMS_FLOOR)
            print(
                f"✅ Grundpegel RMS≈{noise:.0f} → Sprach-Schwelle {min_rms:.0f} "
                f"(Override: --min-rms)\n"
            )

        vad = webrtcvad.Vad(profile.vad_aggressiveness)

        take = 0
        while take < args.takes:
            slug, instruction = VARIATIONS[take % len(VARIATIONS)]
            print(f"── Take {take + 1}/{args.takes} — {instruction}")
            try:
                input(f"   [Enter] drücken, dann »{scorer.display}« sagen … ")
            except (EOFError, KeyboardInterrupt):
                print("\n⏹  Session beendet.")
                break

            # Totzeit: Tastenklick abklingen lassen, dann erst scharf schalten
            time.sleep(ARM_DELAY_SEC)
            leds.set_phase(LED_RECORDING)
            print(f"   🔴 Jetzt: »{scorer.display}«")

            result = _record_take(source, vad, min_rms)
            leds.set_phase(LED_IDLE)
            if result is None:
                print("   ⚠️  Keine Sprache erkannt — Take wird wiederholt.\n")
                continue
            samples, speech_sec = result

            dur = len(samples) / RATE_OW
            verdict = scorer.score_pcm(samples)
            print(f"   📼 {dur:.2f}s (Sprache {speech_sec:.2f}s)   {_score_line(verdict)}")
            suspect = speech_sec < MIN_SPEECH_SEC
            if suspect:
                print("   ⚠️  Sehr kurz — vermutlich Störgeräusch, im Zweifel wiederholen.")

            ts = datetime.now().strftime("%Y%m%d-%H%M%S")
            filename = f"{ts}_{slug}.wav"
            path = os.path.join(speaker_dir, filename)
            _save_wav(path, samples)

            if not args.no_play:
                if sink is None:
                    sink = _make_sink(profile)
                print("   🔊 Zur Kontrolle …")
                sink.play_wav(path)
                source.flush()

            try:
                choice = input("   [Enter]=behalten  w=wiederholen  a=nochmal anhören  q=fertig: ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                choice = "q"

            while choice == "a":
                if sink is None:
                    sink = _make_sink(profile)
                sink.play_wav(path)
                source.flush()
                try:
                    choice = input("   [Enter]=behalten  w=wiederholen  q=fertig: ").strip().lower()
                except (EOFError, KeyboardInterrupt):
                    choice = "q"

            if choice == "w":
                os.unlink(path)
                print("   ↩  Verworfen, gleiche Variante nochmal.\n")
                continue

            meta = {
                "ts": datetime.now().isoformat(timespec="seconds"),
                "bundle": args.bundle,
                "speaker": speaker,
                "style": slug,
                "file": os.path.join(speaker, filename),
                "dur_s": round(dur, 2),
                "speech_s": round(speech_sec, 2),
                "max_score": round(verdict["max_score"], 4),
                "best_streak": verdict["best_streak"],
                "triggered": verdict["triggered"],
                "robust": verdict["robust"],
                "threshold": scorer.threshold,
                "min_rms": round(min_rms, 1),
                "profile": profile.name,
                "mode": profile.mode,
                "host": socket.gethostname(),
            }
            with open(os.path.join(samples_dir, "sessions.jsonl"), "a") as f:
                f.write(json.dumps(meta, ensure_ascii=False) + "\n")
            accepted.append(meta)
            print()
            take += 1
            if choice == "q":
                print("⏹  Session beendet.")
                break
    finally:
        try:
            leds.set_phase(LED_IDLE)
        except Exception:
            pass
        if source is not None:
            source.close()
        if was_active and not args.keep_service:
            print(f"▶️  Starte {SERVICE_UNIT} wieder …")
            _service_ctl("start")

    if not accepted:
        print("Keine Takes gespeichert.")
        return 1

    triggered = sum(1 for m in accepted if m["triggered"])
    print(f"\n📊 Zusammenfassung — {len(accepted)} Takes, Sprecher '{speaker}':")
    for m in accepted:
        mark = "✅" if m["triggered"] else "❌"
        print(f"   {mark} {m['file']:<40} {m['style']:<10} max={m['max_score']:.2f} streak={m['best_streak']}")
    print(
        f"\n   Live-Trigger-Quote: {triggered}/{len(accepted)} "
        f"bei Threshold {scorer.threshold:.2f}"
    )
    if triggered < len(accepted):
        print(
            "   Hinweis: ❌-Takes sind wertvoll fürs Test-Set — sie zeigen, wo das\n"
            "   Modell (oder der Threshold) noch Luft hat. Nicht löschen!"
        )
    print(
        f"\n   Erneut scoren:  python -m wakeword_studio score --bundle {args.bundle}\n"
        f"   Git-Commit im samples/-Repo nicht vergessen."
    )
    return 0


# ---------------------------------------------------------------------------
# score-Subcommand
# ---------------------------------------------------------------------------

def run_score(args) -> int:
    from wakeword_studio.scoring import BundleScorer

    bundle_dir = os.path.join(WAKEWORDS_DIR, args.bundle)
    paths = args.paths or [os.path.join(bundle_dir, "samples")]

    wavs: list[str] = []
    for p in paths:
        if os.path.isdir(p):
            for root, _dirs, files in os.walk(p):
                wavs += [os.path.join(root, f) for f in sorted(files) if f.endswith(".wav")]
        elif p.endswith(".wav"):
            wavs.append(p)
    if not wavs:
        print(f"❌ Keine WAV-Dateien gefunden in: {', '.join(paths)}")
        return 1

    scorer = BundleScorer(args.bundle, threshold=args.threshold)
    print(
        f"🔎 {len(wavs)} Datei(en) gegen '{args.bundle}' "
        f"(Threshold {scorer.threshold:.2f}, Trigger = Streak ≥ 3 mit 1-Gap):\n"
    )
    from wakeword_studio.scoring import load_wav_16k

    triggered = 0
    for path in wavs:
        try:
            samples = load_wav_16k(path)
            verdict = scorer.score_pcm(samples)
        except Exception as exc:
            print(f"   ⚠️  {path}: {exc}")
            continue
        mark = "✅" if verdict["triggered"] else "❌"
        triggered += verdict["triggered"]
        rel = os.path.relpath(path, bundle_dir)
        dur = len(samples) / 16000
        print(
            f"   {mark} {rel:<50} {dur:4.1f}s  max={verdict['max_score']:.2f} "
            f"streak={verdict['best_streak']} robust={verdict['robust']}"
        )

    print(f"\n   Trigger-Quote: {triggered}/{len(wavs)} bei Threshold {scorer.threshold:.2f}")
    return 0
