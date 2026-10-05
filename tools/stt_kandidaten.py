#!/usr/bin/env python3
"""STT-Kandidaten auf einem GPU-Rechner transkribieren — ohne Repo, ohne Speaches.

Gegenstück zu tools/stt_vergleich.py: dort laufen Modelle über Speaches, das
nur faster-whisper bedient. Dieses Skript läuft auf einem beliebigen
GPU-Rechner im venv des jeweiligen Modells (NeMo, qwen-asr, transformers
vertragen sich nicht in einem venv), liest fertig normalisierte WAVs und
schreibt je Clip eine JSON-Zeile. Ausgewertet wird anschließend HIER mit
`stt_vergleich.py --transkripte`, also mit derselben Aktuator-Kette und
denselben Labels wie jeder andere Lauf.

    # hier: Clips so normalisieren, wie sie live an die STT gehen
    ow-venv/bin/python -m tools.stt_vergleich --export-wavs <ordner>
    # dort, je Modell im passenden venv:
    venv-fw/bin/python stt_kandidaten.py fw guillaumekln/faster-whisper-medium clips/ out/
    venv-nemo/bin/python stt_kandidaten.py nemo nvidia/parakeet-tdt-0.6b-v3 clips/ out/
    venv-qwen/bin/python stt_kandidaten.py qwen Qwen/Qwen3-ASR-1.7B clips/ out/
    venv-vox/bin/python stt_kandidaten.py vox mistralai/Voxtral-Mini-3B-2507 clips/ out/

Die Whisper-Basislinie gehört NICHT hierher, sondern über Speaches
(`stt_vergleich.py --speaches-lauf`): ein faster-whisper in anderer Fassung
stellt den Betrieb nicht nach (78/155 statt 151/155 wortgleich mit live).
Messreihe und Ergebnisse: Docstring von stt_vergleich.py.

`--prompt-datei` gibt Whisper einen Anfangs-Prompt (initial_prompt) bzw. Qwen3-ASR
einen Kontext, etwa die Gerätenamen der Installation. Die Datei bleibt außerhalb des Repos — die
Namen sind je Haus andere.

Verwerfungs-Signale: nur faster-whisper hat no_speech_prob (dazu avg_logprob,
compression_ratio). NeMo liefert auf Wunsch eine Token-Konfidenz, Qwen und
Voxtral liefern nur Text. Ein leerer Text gilt überall als verworfen.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys
import time


def _fw(modell: str, prompt: str | None):
    from faster_whisper import WhisperModel
    m = WhisperModel(modell, device="cuda", compute_type="int8")

    def run(pfad: str) -> dict:
        # Als Array statt Pfad: faster-whisper 1.2 dekodiert Dateien über PyAV,
        # und dessen neue Fassungen kennen ein dort benutztes Argument nicht mehr.
        import wave
        import numpy as np
        with wave.open(pfad) as w:
            audio = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
        segs, _ = m.transcribe(audio.astype(np.float32) / 32768.0, language="de",
                               beam_size=5, temperature=0.0, initial_prompt=prompt)
        segs = list(segs)
        n = len(segs) or 1
        return {"text": " ".join(s.text.strip() for s in segs).strip(),
                "nsp": round(sum(s.no_speech_prob for s in segs) / n, 3) if segs else 0.0,
                "logprob": round(sum(s.avg_logprob for s in segs) / n, 3) if segs else None,
                "kompression": round(max((s.compression_ratio for s in segs), default=0), 2)}
    return run


def _nemo(modell: str, prompt: str | None):
    import nemo.collections.asr as nemo_asr
    from omegaconf import open_dict
    m = nemo_asr.models.ASRModel.from_pretrained(modell).cuda().eval()
    canary = "canary" in modell.lower()
    konfidenz = False
    if not canary:
        try:
            dc = m.cfg.decoding
            with open_dict(dc):
                # Wort-Konfidenz stürzt in NeMo 3.0 bei "80%" ab (len(words) !=
                # len(word_confidence)); Token-Konfidenz trägt dieselbe Information.
                dc.confidence_cfg = {"preserve_token_confidence": True}
            m.change_decoding_strategy(dc)
            konfidenz = True
        except Exception as e:  # Konfidenz ist Zugabe, kein Muss
            print(f"keine Konfidenz: {e}", file=sys.stderr)

    def run(pfad: str) -> dict:
        kw = {"source_lang": "de", "target_lang": "de", "pnc": "yes"} if canary else {}
        h = m.transcribe([pfad], batch_size=1, verbose=False,
                         return_hypotheses=konfidenz, **kw)[0]
        h = h[0] if isinstance(h, list) else h
        text = h if isinstance(h, str) else h.text
        out = {"text": (text or "").strip()}
        wc = getattr(h, "token_confidence", None)
        if wc:
            vals = [float(x) for x in wc]
            out["konf_min"] = round(min(vals), 3)
            out["konf_mittel"] = round(sum(vals) / len(vals), 3)
        return out
    return run


def _qwen(modell: str, prompt: str | None):
    import torch
    from qwen_asr import Qwen3ASRModel
    m = Qwen3ASRModel.from_pretrained(modell, dtype=torch.bfloat16, device_map="cuda:0",
                                      max_new_tokens=256)

    def run(pfad: str) -> dict:
        kw = {"context": prompt} if prompt else {}
        r = m.transcribe(audio=pfad, language="German", **kw)[0]
        return {"text": (r.text or "").strip()}
    return run


def _vox(modell: str, prompt: str | None):
    import torch
    from transformers import AutoProcessor, VoxtralForConditionalGeneration
    proc = AutoProcessor.from_pretrained(modell)
    m = VoxtralForConditionalGeneration.from_pretrained(
        modell, torch_dtype=torch.bfloat16, device_map="cuda:0")

    def run(pfad: str) -> dict:
        inp = proc.apply_transcription_request(language="de", audio=pfad, model_id=modell)
        inp = inp.to("cuda:0", dtype=torch.bfloat16)
        out = m.generate(**inp, max_new_tokens=200, do_sample=False)
        text = proc.batch_decode(out[:, inp.input_ids.shape[1]:], skip_special_tokens=True)[0]
        return {"text": text.strip()}
    return run


_ENGINES = {"fw": _fw, "nemo": _nemo, "qwen": _qwen, "vox": _vox}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("engine", choices=sorted(_ENGINES))
    ap.add_argument("modell")
    ap.add_argument("clips", help="Ordner mit normalisierten *_rec.wav")
    ap.add_argument("ausgabe", help="Ordner für <name>.jsonl")
    ap.add_argument("--prompt-datei")
    ap.add_argument("--name", help="Name des Laufs (Default: aus dem Modell)")
    args = ap.parse_args()

    prompt = open(args.prompt_datei, encoding="utf-8").read().strip() if args.prompt_datei else None
    name = args.name or args.modell.split("/")[-1] + ("+prompt" if prompt else "")
    clips = sorted(glob.glob(os.path.join(args.clips, "*_rec.wav")))
    t0 = time.monotonic()
    run = _ENGINES[args.engine](args.modell, prompt)
    lade_s = round(time.monotonic() - t0, 1)
    run(clips[0])                       # Aufwärmen gehört nicht in die Latenz

    os.makedirs(args.ausgabe, exist_ok=True)
    ziel = os.path.join(args.ausgabe, name.replace("/", "_") + ".jsonl")
    with open(ziel, "w", encoding="utf-8") as f:
        f.write(json.dumps({"name": name, "engine": args.engine, "modell": args.modell,
                            "prompt": bool(prompt), "lade_s": lade_s}) + "\n")
        for i, pfad in enumerate(clips):
            t = time.monotonic()
            e = run(pfad)
            e["ms"] = int((time.monotonic() - t) * 1000)
            e["clip"] = os.path.basename(pfad)
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
            print(f"\r{name}: {i + 1}/{len(clips)}", end="", file=sys.stderr, flush=True)
    print(f"\n-> {ziel}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
