#!/usr/bin/env python3
"""Laya auf die Torfrage feinabstimmen, und das Ergebnis per laya-serve anbieten.

Laeuft NICHT im Projekt-venv, sondern in einem eigenen mit laya[serve] und
torch (CUDA fuers Training) — torch gehoert nicht in den Sprachassistenten:

    python3 -m venv ~/laya-test/venv
    ~/laya-test/venv/bin/pip install torch --index-url https://download.pytorch.org/whl/cu128
    ~/laya-test/venv/bin/pip install "laya[serve]==0.3.21"

    ~/laya-test/venv/bin/python tools/laya_tor_train.py train --aus ~/laya-test/ckpt/tor-v1
    ~/laya-test/venv/bin/python tools/laya_tor_train.py serve --ckpt ~/laya-test/ckpt/tor-v1

Gemessen wird danach wie jedes andere Tor-Modell, gegen die ECHTEN Saetze:

    ow-venv/bin/python -m tools.actuator_tor_test --modell laya \\
        --laya-url http://127.0.0.1:8096 --laya-frage schlicht

Was hier vom Laya-Notebook abweicht, und warum
----------------------------------------------
Vorlage: notebooks/laya_finetune_typed_decisions_2xT4_kaggle.ipynb (Laya
0.3.21). Gleich: Sequenzbau (laya.common.build_sequence), Modell
(build_model + load_state_dict strict), ein Temperatur-Fit fuer noul auf
zurueckgehaltenen Daten, Speicherformat.

- Nur weiche Kreuzentropie, ohne den RL-Term (RLCD). Log-Loss ist selbst eine
  strikt proper scoring rule; fuer eine einzige Ja/Nein-Frage lohnt die
  Rauschexploration nicht. Wenn die Kalibrierung auf echten Saetzen schlecht
  ist, ist das der erste Hebel, den man zurueckholt.
- Die Wort-Einbettungen bleiben eingefroren. 197 der 322 Mio. Parameter von
  mmBERT-base sind die Einbettungstabelle (256k Woerter); ohne sie passen
  Gewichte + AdamW in die ~4 GB, die auf der 3060 Ti neben Speaches frei
  sind. Nebenbei: 6.500 Saetze sind zu wenig, um 256k Wortvektoren sinnvoll
  zu verschieben — eingefroren koennen sie auch nicht auf die paar
  Geraetenamen ueberanpassen.
- Die Frage ist fest: dieselbe noul-Frage wie `_LAYA_FRAGEN["schlicht"]` in
  actuator_tor_test.py. Sie steht auch in der gespeicherten Konfiguration
  (`tor_frage`), damit ein Checkpoint sagt, worauf er trainiert wurde.

Die Temperatur wird auf 10 % zurueckgehaltener SYNTHETISCHER Saetze gefittet
(wie im Notebook). Ob sie auf echten Saetzen traegt, zeigt erst die
Kalibrier-Tabelle von actuator_tor_test.py. AUROC haengt von ihr nicht ab.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import sys
import time

# Muss wortgleich zu tools/actuator_tor_test.py:_LAYA_FRAGEN["schlicht"] sein —
# sonst misst das Werkzeug eine andere Frage als die trainierte.
FRAGE = {
    "type": "noul",
    "instructions": "Will der Sprecher mit diesem Satz ein Gerät im Haus "
                    "schalten oder einstellen, zum Beispiel Licht, Rollo oder Heizung?",
}

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TRAIN = os.path.join(_REPO, "testsets", "tor_train.jsonl")
_BASIS = ("convaiinnovations/laya", "multilingual")


def _basis_dir(revision: str | None) -> str:
    from huggingface_hub import snapshot_download
    repo, sub = _BASIS
    d = snapshot_download(repo, allow_patterns=[f"{sub}/*"], revision=revision)
    return os.path.join(d, sub)


def _lade(pfad: str) -> list[dict]:
    zeilen = []
    for z in open(pfad, encoding="utf-8"):
        if z.strip() and not z.startswith("#"):
            d = json.loads(z)
            if d.get("schalten") is not None:
                zeilen.append(d)
    return zeilen


def _items(tok, cfg, zeilen):
    from laya.common import QTYPES, build_sequence
    q = {"t": FRAGE["type"], "ins": FRAGE["instructions"], "crit": {}}
    items = []
    for z in zeilen:
        ids, markers = build_sequence(tok, {"satz": z["satz"]}, q, cfg["max_len"], cfg["head_max_len"])
        assert len(markers) == 2, z["satz"]
        # noul: Optionen in der Reihenfolge [false, true]
        items.append({"ids": ids, "markers": markers, "qtype": QTYPES["noul"],
                      "target": [0.0, 1.0] if z["schalten"] else [1.0, 0.0],
                      "herkunft": z.get("herkunft", "")})
    return items


def _batch(items, pad_id, device):
    import torch
    n, L = len(items), max(len(it["ids"]) for it in items)
    ids = torch.full((n, L), pad_id, dtype=torch.long)
    att = torch.zeros((n, L), dtype=torch.long)
    for i, it in enumerate(items):
        ids[i, :len(it["ids"])] = torch.tensor(it["ids"])
        att[i, :len(it["ids"])] = 1
    mpos = torch.tensor([it["markers"] for it in items])
    mmask = torch.ones((n, 2), dtype=torch.bool)
    return (ids.to(device), att.to(device), mpos.to(device), mmask.to(device),
            torch.tensor([it["qtype"] for it in items], device=device),
            torch.tensor([it["target"] for it in items], device=device))


def _logits(model, items, pad_id, device, bs=64):
    import torch
    out = []
    model.eval()
    with torch.no_grad(), torch.autocast(device.type, dtype=torch.bfloat16, enabled=device.type == "cuda"):
        for i in range(0, len(items), bs):
            ids, att, mpos, mmask, qt, _ = _batch(items[i:i + bs], pad_id, device)
            lg, _ = model(ids, att, mpos, mmask, qt)
            out.append(lg.float().cpu())
    return torch.cat(out)


def _fit_temp(logits, targets) -> float:
    import torch
    log_t = torch.zeros(1, requires_grad=True)
    opt = torch.optim.LBFGS([log_t], lr=0.1, max_iter=100)

    def closure():
        opt.zero_grad()
        loss = -(targets * torch.log_softmax(logits / log_t.exp(), -1)).sum(-1).mean()
        loss.backward()
        return loss
    opt.step(closure)
    return float(log_t.exp().clamp(0.5, 5.0).item())   # Laya klemmt beim Laden auf [0.5, 5]


def train(args) -> int:
    import torch
    from safetensors.torch import load_file, save_file
    from transformers import AutoTokenizer
    from laya.agent import _fix_tokenizer_config
    from laya.common import build_model

    device = torch.device(args.device)
    basis = _basis_dir(args.revision)
    _fix_tokenizer_config(basis)
    tok = AutoTokenizer.from_pretrained(os.path.join(basis, "tokenizer"))
    cfg = json.load(open(os.path.join(basis, "rl_agent_config.json")))

    zeilen = _lade(args.train)
    rng = random.Random(args.seed)
    rng.shuffle(zeilen)
    n_kal = max(50, len(zeilen) // 10)
    kal, trn = _items(tok, cfg, zeilen[:n_kal]), _items(tok, cfg, zeilen[n_kal:])
    print(f"{len(trn)} Training, {len(kal)} zurueckgehalten fuer die Temperatur; "
          f"ja-Anteil {sum(it['target'][1] for it in trn) / len(trn):.0%}; "
          f"laengste Sequenz {max(len(it['ids']) for it in trn)} Token")

    model = build_model(cfg, encoder_dir=os.path.join(basis, "encoder"))
    model.load_state_dict(load_file(os.path.join(basis, "model.safetensors")), strict=True)
    emb = model.encoder.get_input_embeddings()
    emb.weight.requires_grad_(False)
    model.encoder.gradient_checkpointing_enable(gradient_checkpointing_kwargs={"use_reentrant": False})
    model.head_checkpointing = True
    model.to(device)
    n_train = sum(p.numel() for p in model.parameters() if p.requires_grad)
    print(f"trainierbar {n_train / 1e6:.0f} M von {sum(p.numel() for p in model.parameters()) / 1e6:.0f} M "
          f"Parametern (Einbettungen eingefroren)")

    enc = [p for n, p in model.named_parameters() if p.requires_grad and n.startswith("encoder.")]
    kopf = [p for n, p in model.named_parameters() if p.requires_grad and not n.startswith("encoder.")]
    opt = torch.optim.AdamW([{"params": enc, "lr": args.lr_encoder},
                             {"params": kopf, "lr": args.lr_kopf}], weight_decay=0.01)
    schritte = math.ceil(len(trn) / args.batch) * args.epochen
    sched = torch.optim.lr_scheduler.OneCycleLR(
        opt, max_lr=[args.lr_encoder, args.lr_kopf], total_steps=schritte, pct_start=0.1)

    t0, schritt = time.time(), 0
    for ep in range(args.epochen):
        model.train()
        rng.shuffle(trn)
        summe, n = 0.0, 0
        for i in range(0, len(trn), args.batch):
            ids, att, mpos, mmask, qt, ziel = _batch(trn[i:i + args.batch], tok.pad_token_id, device)
            with torch.autocast(device.type, dtype=torch.bfloat16, enabled=device.type == "cuda"):
                lg, act = model(ids, att, mpos, mmask, qt)
            loss = -(ziel * torch.log_softmax(lg.float(), -1)).sum(-1).mean() + 0.0 * act.sum()
            opt.zero_grad(set_to_none=True)
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            opt.step()
            sched.step()
            summe, n, schritt = summe + loss.item(), n + 1, schritt + 1
            if schritt % 50 == 0:
                print(f"  Epoche {ep + 1} Schritt {schritt}/{schritte} Loss {summe / n:.4f} "
                      f"({time.time() - t0:.0f} s)", flush=True)
        lg = _logits(model, kal, tok.pad_token_id, device)
        ziel = torch.tensor([it["target"] for it in kal])
        acc = (lg.argmax(-1) == ziel.argmax(-1)).float().mean().item()
        print(f"Epoche {ep + 1}: Loss {summe / max(1, n):.4f}, zurueckgehalten richtig {acc:.1%}")

    lg = _logits(model, kal, tok.pad_token_id, device)
    ziel = torch.tensor([it["target"] for it in kal])
    t_noul = _fit_temp(lg, ziel)
    print(f"Temperatur noul {t_noul:.3f} (auf {len(kal)} synthetischen Saetzen)")
    if device.type == "cuda":
        print(f"GPU-Spitze {torch.cuda.max_memory_allocated() / 2**30:.2f} GB")

    os.makedirs(args.aus, exist_ok=True)
    model.encoder.gradient_checkpointing_disable()
    save_file({k: v.half().contiguous().cpu() for k, v in model.state_dict().items()},
              os.path.join(args.aus, "model.safetensors"))
    model.encoder.config.save_pretrained(os.path.join(args.aus, "encoder"))
    tok.save_pretrained(os.path.join(args.aus, "tokenizer"))
    temps = list(cfg.get("temperature", [1.0, 1.0, 1.0]))
    temps[2] = t_noul
    cfg.update({"fine_tuned": True, "model_name": "laya-multilingual-tor", "temperature": temps,
                "tor_frage": FRAGE,
                "tor_training": {"daten": os.path.basename(args.train), "saetze": len(trn),
                                 "epochen": args.epochen, "seed": args.seed,
                                 "lr_encoder": args.lr_encoder, "lr_kopf": args.lr_kopf,
                                 "einbettungen": "eingefroren", "loss": "kreuzentropie",
                                 "dauer_s": round(time.time() - t0)}})
    cfg.pop("temperature_by_options", None)
    json.dump(cfg, open(os.path.join(args.aus, "rl_agent_config.json"), "w"), indent=2, ensure_ascii=False)
    print(f"gespeichert: {args.aus}")
    return 0


def serve(args) -> int:
    """laya-serve mit dem eigenen Checkpoint unter dem Namen 'multilingual'
    (so fragt actuator_tor_test.py). Eigener Port, damit der unveraenderte
    Basis-Server daneben weiterlaufen kann."""
    import uvicorn
    from laya.router import Router
    from laya.serve import create_app
    if args.threads:
        import torch
        torch.set_num_threads(args.threads)
    router = Router(models={"multilingual": (os.path.abspath(args.ckpt), None)},
                    device=args.device, default="multilingual")
    router.preload(["multilingual"])
    uvicorn.run(create_app(router), host="127.0.0.1", port=args.port, log_level="warning")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("train")
    t.add_argument("--train", default=_TRAIN)
    t.add_argument("--aus", required=True)
    t.add_argument("--epochen", type=int, default=2)
    t.add_argument("--batch", type=int, default=16)
    t.add_argument("--lr-encoder", type=float, default=2e-5)
    t.add_argument("--lr-kopf", type=float, default=1e-4)
    t.add_argument("--seed", type=int, default=20260928)
    t.add_argument("--device", default="cuda")
    t.add_argument("--revision", help="Hub-Revision der Basis (Default: aktuell)")
    s = sub.add_parser("serve")
    s.add_argument("--ckpt", required=True)
    s.add_argument("--port", type=int, default=8096)
    s.add_argument("--device", default="cpu")
    s.add_argument("--threads", type=int, default=6)
    args = ap.parse_args()
    return train(args) if args.cmd == "train" else serve(args)


if __name__ == "__main__":
    sys.exit(main())
