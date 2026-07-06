"""Phase A des Wakeword-Studios: geführte echte Wakeword-Aufnahmen.

Nutzt denselben Mic-Pfad wie der Assistant (Profil aus config.yaml, Modus
local oder respeaker) — die Samples klingen also exakt so, wie das Modell sie
live zu hören bekommt (inkl. ReSpeaker-Gain/DC-Filter). Der laufende
Assistant-Service wird für die Dauer der Session gestoppt (Mic-Stream ist
exklusiv) und danach wieder gestartet.

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

SERVICE_UNIT = "openclaw-voice-assist.service"

# Aufnahme-Parameter (Wakewords sind kurz — enge Fenster halten Takes sauber)
PREROLL_SEC = 0.32       # Audio vor dem ersten Sprach-Chunk mitnehmen
WAIT_SPEECH_SEC = 15.0   # max. Wartezeit auf Sprachbeginn je Take
MAX_SPEECH_SEC = 3.0     # Hard-Cap ab Sprachbeginn
END_SILENCE_SEC = 0.6    # so viel Stille beendet den Take
TRAIL_KEEP_SEC = 0.25    # Reststille am Ende, die im Sample bleibt

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
# Service- und Audio-Hilfen
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


# Sprach-Erkennung pro Chunk — gleiche Logik wie assistant._is_speech_chunk
# (bewusst kopiert statt importiert: assistant.py zieht den kompletten
# Service-Stack mit herein).
def _is_speech_chunk(vad: webrtcvad.Vad, audio_16: np.ndarray, min_rms: float) -> bool:
    if min_rms > 0.0:
        rms = float(np.sqrt(np.mean(audio_16.astype(np.float32) ** 2)))
        if rms < min_rms:
            return False
    result = False
    for i in range(0, len(audio_16), VAD_FRAME_SIZE):
        frame = audio_16[i : i + VAD_FRAME_SIZE]
        if len(frame) == VAD_FRAME_SIZE:
            result |= vad.is_speech(frame.tobytes(), RATE_OW)
    return result


def _record_take(source, vad: webrtcvad.Vad, min_rms: float) -> np.ndarray | None:
    """Ein Take: auf Sprache warten, bis Stille aufnehmen, zuschneiden.

    None wenn innerhalb WAIT_SPEECH_SEC keine Sprache kam.
    """
    source.flush()
    preroll: deque[np.ndarray] = deque()
    preroll_samples = 0
    speech_chunks: list[np.ndarray] = []
    silence_run = 0          # Chunks Stille seit dem letzten Sprach-Chunk
    speech_started = 0.0
    started = time.time()
    kicked = False

    while True:
        chunk = source.read_chunk()
        if not len(chunk):
            continue
        chunk_sec = len(chunk) / RATE_OW
        is_speech = _is_speech_chunk(vad, chunk, min_rms)

        if not speech_chunks:
            if is_speech:
                speech_chunks.append(chunk.copy())
                speech_started = time.time()
            else:
                preroll.append(chunk.copy())
                preroll_samples += len(chunk)
                while preroll_samples - len(preroll[0]) >= int(PREROLL_SEC * RATE_OW):
                    preroll_samples -= len(preroll.popleft())
                waited = time.time() - started
                if not kicked and waited > WAIT_SPEECH_SEC / 2 and not np.any(chunk):
                    _kick_respeaker(source)  # Stream steht (nur Nullen) → neu anstoßen
                    kicked = True
                if waited > WAIT_SPEECH_SEC:
                    return None
            continue

        speech_chunks.append(chunk.copy())
        silence_run = 0 if is_speech else silence_run + 1

        if silence_run * chunk_sec >= END_SILENCE_SEC:
            trim = int(max(0, silence_run * len(chunk) - TRAIL_KEEP_SEC * RATE_OW))
            samples = np.concatenate(list(preroll) + speech_chunks)
            return samples[: len(samples) - trim] if trim else samples
        if time.time() - speech_started > MAX_SPEECH_SEC:
            return np.concatenate(list(preroll) + speech_chunks)


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
        f"(Threshold {verdict['threshold']:.2f}) → {mark}"
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
    accepted: list[dict] = []
    try:
        source = _make_source(profile)
        print("⏳ Warte auf Audio-Stream …")
        if not _wait_for_stream(source):
            print("❌ Kein Audio vom Mikrofon — läuft der ReSpeaker / ist das Gerät frei?")
            return 1
        print("✅ Audio läuft.\n")

        vad = webrtcvad.Vad(profile.vad_aggressiveness)
        min_rms = profile.vad_voice_rms_min

        take = 0
        while take < args.takes:
            slug, instruction = VARIATIONS[take % len(VARIATIONS)]
            print(f"── Take {take + 1}/{args.takes} — {instruction}")
            try:
                input(f"   [Enter] drücken, dann »{scorer.display}« sagen … ")
            except (EOFError, KeyboardInterrupt):
                print("\n⏹  Session beendet.")
                break

            samples = _record_take(source, vad, min_rms)
            if samples is None:
                print("   ⚠️  Keine Sprache erkannt — Take wird wiederholt.\n")
                continue

            dur = len(samples) / RATE_OW
            verdict = scorer.score_pcm(samples)
            print(f"   📼 {dur:.2f}s   {_score_line(verdict)}")

            ts = datetime.now().strftime("%Y%m%d-%H%M%S")
            filename = f"{ts}_{slug}.wav"
            path = os.path.join(speaker_dir, filename)
            _save_wav(path, samples)

            try:
                choice = input("   [Enter]=behalten  w=wiederholen  a=anhören  q=fertig: ").strip().lower()
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
                "max_score": round(verdict["max_score"], 4),
                "best_streak": verdict["best_streak"],
                "triggered": verdict["triggered"],
                "threshold": scorer.threshold,
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
    triggered = 0
    for path in wavs:
        try:
            verdict = scorer.score_wav(path)
        except Exception as exc:
            print(f"   ⚠️  {path}: {exc}")
            continue
        mark = "✅" if verdict["triggered"] else "❌"
        triggered += verdict["triggered"]
        rel = os.path.relpath(path, bundle_dir)
        print(f"   {mark} {rel:<50} max={verdict['max_score']:.2f} streak={verdict['best_streak']}")

    print(f"\n   Trigger-Quote: {triggered}/{len(wavs)} bei Threshold {scorer.threshold:.2f}")
    return 0
