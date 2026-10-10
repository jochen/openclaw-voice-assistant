#!/usr/bin/env python3
"""Laya auf den Aktuator feinabstimmen (Tor, Ziel, Aktion) und per laya-serve anbieten.

Laeuft NICHT im Projekt-venv, sondern in einem eigenen mit laya[serve] und
torch (CUDA fuers Training) — torch gehoert nicht in den Sprachassistenten:

    python3 -m venv ~/laya-test/venv
    ~/laya-test/venv/bin/pip install --force-reinstall "torch==2.14.0+cu130" \\
        --index-url https://download.pytorch.org/whl/cu130 --extra-index-url https://pypi.org/simple
    ~/laya-test/venv/bin/pip install "laya[serve]==0.3.21"

    ~/laya-test/venv/bin/python tools/laya_aktuator_train.py train --aus ~/laya-modelle/aktuator-v1
    ~/laya-test/venv/bin/python tools/laya_aktuator_train.py serve --ckpt ~/laya-modelle/aktuator-v1

(pip haelt ein installiertes "2.14.0+cpu" fuer "torch==2.14.0" erfuellt —
ohne --force-reinstall und die +cu130-Angabe bleibt die CPU-Fassung liegen.)

Im Betrieb laeuft der Checkpoint als Dienst `laya` in openclaw-voice-stack.
Gemessen wird gegen die ECHTEN Saetze:

    ow-venv/bin/python -m tools.actuator_tor_test --modell laya --laya-url ...   # nur Tor
    ow-venv/bin/python -m tools.aktuator_vergleich                               # ganze Kette

Was trainiert wird
------------------
Die drei Fragen aus voice_assistant/services/laya_intent.py — von dort
importiert, damit Training und Betrieb garantiert dieselben Fragen stellen:

    tor     auf jedem Satz
    ziel    auf jedem Satz mit schalten=true und bekanntem Ziel; Befehle ohne
            bestimmbares Ziel ("Rollo zu") lernen "keins"
    aktion  auf jedem Satz mit bekannter Aktion

Die ziel-Frage entsteht aus dem capabilities-Schnappschuss neben den
Trainingsdaten (testsets/tor_train.capabilities.json), nicht aus dem Live-
Endpunkt: ein Checkpoint gehoert zu genau einer Zielliste, deren Version in
seiner Konfiguration steht (`capabilities`). Neue Ziele heissen: neu erzeugen,
neu trainieren.

Die Reihenfolge der Optionen wird je Satz gemischt: Laya liest die Optionen
als Text, und im Betrieb steht die Liste in Digest-Reihenfolge. Ohne Mischen
koennte das Modell Positionen statt Namen lernen.

Was vom Laya-Notebook abweicht, und warum
-----------------------------------------
Vorlage: notebooks/laya_finetune_typed_decisions_2xT4_kaggle.ipynb (0.3.21).
Gleich: Sequenzbau (laya.common.build_sequence), Modell (build_model +
load_state_dict strict), Temperatur-Fit je Fragetyp auf zurueckgehaltenen
Daten, Speicherformat.

- Nur Kreuzentropie, ohne den RL-Term (RLCD). Log-Loss ist selbst eine
  strikt proper scoring rule. Ist die Kalibrierung auf echten Saetzen
  schlecht, ist das der erste Hebel, den man zurueckholt.
- Die Wort-Einbettungen bleiben eingefroren. 197 der 322 Mio. Parameter von
  mmBERT-base sind die Einbettungstabelle (256k Woerter); ohne sie passt das
  Training in die ~4 GB, die auf der 3060 Ti neben Speaches frei sind. Und
  6.500 Saetze sind zu wenig, um 256k Wortvektoren sinnvoll zu verschieben.
- head_max_len waechst mit der Zielliste (Default 256 reicht fuer ~20
  Optionen; 69 Ziele brauchen ~700 Token). Er wird aus der laengsten ziel-
  Frage berechnet und in die Konfiguration geschrieben, die Laya beim Laden
  liest.
- Zurueckgehalten wird je SATZ, nicht je Frage: sonst saehe das Training die
  tor-Frage eines Satzes, dessen ziel-Frage die Temperatur fittet.

Die Temperaturen werden auf zurueckgehaltenen SYNTHETISCHEN Saetzen gefittet.
Ob sie auf echten Saetzen tragen, zeigt erst die Kalibrier-Tabelle von
actuator_tor_test.py. AUROC haengt von ihnen nicht ab.

Messreihe: im Docstring von tools/actuator_tor_test.py (Tor) und
tools/aktuator_vergleich.py (ganze Kette).

Der ganze Lauf — Daten, Training, Deploy, Pruefung — und alles, was beim
ersten Mal schiefging, steht in LAYA_TRAINING.md. Das ist die Vorlage fuer
die Automatisierung; dieses Skript ist nur ein Schritt davon.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import sys
import time

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO)
from voice_assistant.services.laya_intent import (  # noqa: E402  (nur stdlib)
    AKTION_FRAGE, KEIN_BEFEHL, KEIN_ZIEL, TOR_FRAGE, VARIANTEN, ziel_frage,
)

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


def _q(frage: dict) -> dict:
    """laya_intent-Frage -> internes Format von build_sequence."""
    return {"t": frage["type"], "ins": frage["instructions"], "crit": frage.get("criteria") or {}}


def _item(tok, cfg, satz, name, frage, ziel_key, rng, mischen):
    """Eine Trainingssequenz. ziel_key: noul -> True/False, choice -> Kriterium
    oder eine Menge von Kriterien (weiches Ziel, gleich verteilt)."""
    from laya.common import QTYPES, build_sequence
    q = _q(frage)
    if q["t"] == "noul":
        order = [0, 1]
        target = [0.0, 1.0] if ziel_key else [1.0, 0.0]
    else:
        keys = list(q["crit"])
        order = list(range(len(keys)))
        if mischen:
            rng.shuffle(order)
        if isinstance(ziel_key, (set, frozenset)):
            target = [1.0 / len(ziel_key) if keys[i] in ziel_key else 0.0 for i in order]
        else:
            target = [1.0 if keys[i] == ziel_key else 0.0 for i in order]
        assert abs(sum(target) - 1.0) < 1e-6, (ziel_key, satz)
    ids, markers = build_sequence(tok, {"satz": satz}, q, cfg["max_len"], cfg["head_max_len"],
                                  option_order=order)
    assert len(markers) == len(order), satz
    return {"ids": ids, "markers": markers, "qtype": QTYPES[q["t"]], "target": target, "frage": name}


def _items_fuer(tok, cfg, zeilen, digest, rng, mischen, variante="getrennt"):
    if variante == "vereint":
        return _items_vereint(tok, cfg, zeilen, digest, rng, mischen)
    zf = ziel_frage(digest)
    items = []
    for z in zeilen:
        items.append(_item(tok, cfg, z["satz"], "tor", TOR_FRAGE, bool(z["schalten"]), rng, mischen))
        if z["schalten"]:
            if z.get("ziel") in digest:
                items.append(_item(tok, cfg, z["satz"], "ziel", zf, z["ziel"], rng, mischen))
            elif z.get("herkunft") == "synth:befehl_ohne_ziel":
                # Nur hier ist "kein Ziel" ein gesichertes Label. MASSIVE-Befehle
                # haben auch kein Ziel — aber weil sie aus einem fremden Haus
                # stammen, nicht weil keins gemeint war ("Licht in der Küche").
                items.append(_item(tok, cfg, z["satz"], "ziel", zf, KEIN_ZIEL, rng, mischen))
        if z.get("aktion") in AKTION_FRAGE["criteria"]:
            items.append(_item(tok, cfg, z["satz"], "aktion", AKTION_FRAGE, z["aktion"], rng, mischen))
    return items


def _items_vereint(tok, cfg, zeilen, digest, rng, mischen):
    """Eine ziel-Frage fuer JEDEN Satz, mit "keins" UND "kein_befehl".

    MASSIVE-Befehle sind sicher Befehle, ihr Ziel ist aber unbekannt (ein
    fremdes Haus). Statt ein Ziel zu raten, bekommen sie ein weiches Ziel:
    alles ausser kein_befehl, gleich verteilt. So tragen sie die
    Schaltabsicht bei, ohne ein Ziel zu behaupten. Das ist kostspielig: jede
    ziel-Sequenz traegt alle Optionen (~900 Token), und es sind ~4x so viele
    wie bei "getrennt" — das Training dauert entsprechend laenger."""
    zf = ziel_frage(digest, "vereint")
    alle_ausser_nein = frozenset(k for k in zf["criteria"] if k != KEIN_BEFEHL)
    items = []
    for z in zeilen:
        if not z["schalten"]:
            ziel = KEIN_BEFEHL
        elif z.get("ziel") in digest:
            ziel = z["ziel"]
        elif z.get("herkunft") == "synth:befehl_ohne_ziel":
            ziel = KEIN_ZIEL
        else:
            ziel = alle_ausser_nein
        items.append(_item(tok, cfg, z["satz"], "ziel", zf, ziel, rng, mischen))
        if z.get("aktion") in AKTION_FRAGE["criteria"]:
            items.append(_item(tok, cfg, z["satz"], "aktion", AKTION_FRAGE, z["aktion"], rng, mischen))
    return items


def _batch(items, pad_id, device):
    import torch
    n, L = len(items), max(len(it["ids"]) for it in items)
    kmax = max(len(it["markers"]) for it in items)
    ids = torch.full((n, L), pad_id, dtype=torch.long)
    att = torch.zeros((n, L), dtype=torch.long)
    mpos = torch.zeros((n, kmax), dtype=torch.long)
    mmask = torch.zeros((n, kmax), dtype=torch.bool)
    target = torch.zeros((n, kmax))
    for i, it in enumerate(items):
        ids[i, :len(it["ids"])] = torch.tensor(it["ids"])
        att[i, :len(it["ids"])] = 1
        k = len(it["markers"])
        mpos[i, :k] = torch.tensor(it["markers"])
        mmask[i, :k] = True
        target[i, :k] = torch.tensor(it["target"])
    return (ids.to(device), att.to(device), mpos.to(device), mmask.to(device),
            torch.tensor([it["qtype"] for it in items], device=device), target.to(device))


def _gruppen(items, max_token):
    """Batches nach Laenge sortiert und nach Tokenbudget geschnitten: eine ziel-
    Sequenz ist ~15x so lang wie eine tor-Sequenz, feste Batchgroessen
    verschwenden entweder Speicher oder sprengen ihn."""
    items = sorted(items, key=lambda it: len(it["ids"]))
    gruppen, cur = [], []
    for it in items:
        if cur and (len(cur) + 1) * len(it["ids"]) > max_token:
            gruppen.append(cur)
            cur = []
        cur.append(it)
    if cur:
        gruppen.append(cur)
    return gruppen


def _logits(model, items, pad_id, device, max_token=16384):
    import torch
    model.eval()
    out = []
    with torch.no_grad(), torch.autocast(device.type, dtype=torch.bfloat16, enabled=device.type == "cuda"):
        for g in _gruppen(items, max_token):
            ids, att, mpos, mmask, qt, tgt = _batch(g, pad_id, device)
            lg, _ = model(ids, att, mpos, mmask, qt)
            for i, it in enumerate(g):
                k = len(it["markers"])
                out.append((it, lg[i, :k].float().cpu()))
    return out


def _fit_temp(paare) -> float:
    import torch
    if len(paare) < 10:
        return 1.0
    kmax = max(len(z) for z, _ in paare)
    Z = torch.full((len(paare), kmax), -1e4)
    T = torch.zeros((len(paare), kmax))
    for i, (z, t) in enumerate(paare):
        Z[i, :len(z)] = z
        T[i, :len(t)] = torch.tensor(t)
    log_t = torch.zeros(1, requires_grad=True)
    opt = torch.optim.LBFGS([log_t], lr=0.1, max_iter=100)

    def closure():
        opt.zero_grad()
        loss = -(T * torch.log_softmax(Z / log_t.exp(), -1)).sum(-1).mean()
        loss.backward()
        return loss
    opt.step(closure)
    return float(log_t.exp().clamp(0.5, 5.0).item())   # Laya klemmt beim Laden auf [0.5, 5]


def _auswertung(ergebnis) -> str:
    teile = []
    for f in ("tor", "ziel", "aktion"):
        # Nur harte Ziele zaehlen: ein weiches Ziel (MASSIVE-Befehl, "alles
        # ausser kein_befehl") hat keine richtige Antwort. Erst stand es im
        # Nenner — die vereinte Frage zeigte 87,6 % statt ~99 %.
        sel = [(it, lg) for it, lg in ergebnis if it["frage"] == f and max(it["target"]) == 1.0]
        if sel:
            ok = sum(int(lg.argmax()) == it["target"].index(1.0) for it, lg in sel)
            teile.append(f"{f} {ok / len(sel):.1%} (n={len(sel)})")
    return ", ".join(teile)


def train(args) -> int:
    import torch
    from safetensors.torch import load_file, save_file
    from transformers import AutoTokenizer
    from laya.agent import _fix_tokenizer_config
    from laya.common import QTYPES, build_model, build_sequence

    device = torch.device(args.device)
    basis = _basis_dir(args.revision)
    _fix_tokenizer_config(basis)
    tok = AutoTokenizer.from_pretrained(os.path.join(basis, "tokenizer"))
    cfg = json.load(open(os.path.join(basis, "rl_agent_config.json")))

    caps = json.load(open(os.path.splitext(args.train)[0] + ".capabilities.json"))
    digest = caps["digest"]

    # Options-Budget aus der laengsten Frage (der ziel-Frage) ableiten.
    probe_ids, _ = build_sequence(tok, {"satz": ""}, _q(ziel_frage(digest, args.variante)), 8192, 8192)
    cfg["head_max_len"] = int(math.ceil((len(probe_ids) + 32) / 64) * 64)
    cfg["max_len"] = cfg["head_max_len"] + 192
    print(f"capabilities {caps['version']}, {len(digest)} Ziele -> head_max_len "
          f"{cfg['head_max_len']}, max_len {cfg['max_len']}")

    zeilen = _lade(args.train)
    rng = random.Random(args.seed)
    rng.shuffle(zeilen)
    n_kal = max(50, len(zeilen) // 10)
    kal = _items_fuer(tok, cfg, zeilen[:n_kal], digest, rng, mischen=False, variante=args.variante)
    trn = _items_fuer(tok, cfg, zeilen[n_kal:], digest, rng, mischen=True, variante=args.variante)
    zaehl = {f: sum(it["frage"] == f for it in trn) for f in ("tor", "ziel", "aktion")}
    print(f"{len(zeilen) - n_kal} Saetze Training -> {len(trn)} Sequenzen {zaehl}; "
          f"{n_kal} Saetze zurueckgehalten -> {len(kal)} Sequenzen")

    model = build_model(cfg, encoder_dir=os.path.join(basis, "encoder"))
    model.load_state_dict(load_file(os.path.join(basis, "model.safetensors")), strict=True)
    emb = model.encoder.get_input_embeddings()
    emb.weight.requires_grad_(False)
    # Eingefroren reicht bf16: spart ~400 MB (256k x 768 x 2 Byte). Ohne das
    # lief der erste Lauf mit ziel-Frage bei 3,4 GB in den Speicher-Deckel.
    # Gespeichert wird ohnehin in fp16.
    emb.to(torch.bfloat16)
    model.encoder.gradient_checkpointing_enable(gradient_checkpointing_kwargs={"use_reentrant": False})
    model.head_checkpointing = True
    model.to(device)
    n_train = sum(p.numel() for p in model.parameters() if p.requires_grad)
    print(f"trainierbar {n_train / 1e6:.0f} M von {sum(p.numel() for p in model.parameters()) / 1e6:.0f} M")

    enc = [p for n, p in model.named_parameters() if p.requires_grad and n.startswith("encoder.")]
    kopf = [p for n, p in model.named_parameters() if p.requires_grad and not n.startswith("encoder.")]
    opt = torch.optim.AdamW([{"params": enc, "lr": args.lr_encoder},
                             {"params": kopf, "lr": args.lr_kopf}], weight_decay=0.01)
    schritte = len(_gruppen(trn, args.max_token)) * args.epochen
    sched = torch.optim.lr_scheduler.OneCycleLR(
        opt, max_lr=[args.lr_encoder, args.lr_kopf], total_steps=schritte, pct_start=0.1)

    t0, schritt = time.time(), 0
    for ep in range(args.epochen):
        model.train()
        gruppen = _gruppen(trn, args.max_token)
        rng.shuffle(gruppen)
        summe, n = 0.0, 0
        for g in gruppen:
            ids, att, mpos, mmask, qt, ziel = _batch(g, tok.pad_token_id, device)
            with torch.autocast(device.type, dtype=torch.bfloat16, enabled=device.type == "cuda"):
                lg, act = model(ids, att, mpos, mmask, qt)
            lg = lg.float().masked_fill(~mmask, -1e4)
            loss = -(ziel * torch.log_softmax(lg, -1)).sum(-1).mean() + 0.0 * act.sum()
            opt.zero_grad(set_to_none=True)
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            opt.step()
            sched.step()
            summe, n, schritt = summe + loss.item(), n + 1, schritt + 1
            if schritt % 100 == 0:
                print(f"  Epoche {ep + 1} Schritt {schritt}/{schritte} Loss {summe / n:.4f} "
                      f"({time.time() - t0:.0f} s)", flush=True)
        print(f"Epoche {ep + 1}: Loss {summe / max(1, n):.4f}; zurueckgehalten: "
              f"{_auswertung(_logits(model, kal, tok.pad_token_id, device))}", flush=True)

    erg = _logits(model, kal, tok.pad_token_id, device)
    temps = list(cfg.get("temperature", [1.0, 1.0, 1.0]))
    for typ, idx in (("noul", QTYPES["noul"]), ("choice", QTYPES["choice"])):
        temps[idx] = _fit_temp([(lg, it["target"]) for it, lg in erg if it["qtype"] == idx])
        print(f"Temperatur {typ} {temps[idx]:.3f}")
    if device.type == "cuda":
        print(f"GPU-Spitze {torch.cuda.max_memory_allocated() / 2**30:.2f} GB")

    os.makedirs(args.aus, exist_ok=True)
    model.encoder.gradient_checkpointing_disable()
    save_file({k: v.half().contiguous().cpu() for k, v in model.state_dict().items()},
              os.path.join(args.aus, "model.safetensors"))
    model.encoder.config.save_pretrained(os.path.join(args.aus, "encoder"))
    tok.save_pretrained(os.path.join(args.aus, "tokenizer"))
    cfg.update({"fine_tuned": True, "model_name": "laya-multilingual-aktuator", "temperature": temps,
                "capabilities": caps["version"],
                "fragen_variante": args.variante,
                "fragen": ({"aktion": AKTION_FRAGE} if args.variante == "vereint"
                           else {"tor": TOR_FRAGE, "aktion": AKTION_FRAGE}),
                "training": {"daten": os.path.basename(args.train), "saetze": len(zeilen) - n_kal,
                             "sequenzen": zaehl, "epochen": args.epochen, "seed": args.seed,
                             "lr_encoder": args.lr_encoder, "lr_kopf": args.lr_kopf,
                             "einbettungen": "eingefroren", "loss": "kreuzentropie",
                             "dauer_s": round(time.time() - t0),
                             # Hub-Revision der Basis (Snapshot-Verzeichnis): ein
                             # Lauf auf einem anderen Rechner muss dieselbe nehmen.
                             "basis_revision": os.path.basename(os.path.dirname(basis))}})
    cfg.pop("temperature_by_options", None)
    json.dump(cfg, open(os.path.join(args.aus, "rl_agent_config.json"), "w"), indent=2, ensure_ascii=False)
    print(f"gespeichert: {args.aus}")
    return 0


def serve(args) -> int:
    """laya-serve mit dem eigenen Checkpoint unter dem Namen 'multilingual'."""
    import uvicorn
    from laya.router import Router
    from laya.serve import create_app
    if args.threads:
        import torch
        torch.set_num_threads(args.threads)
    router = Router(models={"multilingual": (os.path.abspath(args.ckpt), None)},
                    device=args.device, default="multilingual")
    router.preload(["multilingual"])
    uvicorn.run(create_app(router), host=args.host, port=args.port, log_level="warning")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("train")
    t.add_argument("--train", default=_TRAIN)
    t.add_argument("--aus", required=True)
    t.add_argument("--epochen", type=int, default=2)
    t.add_argument("--max-token", type=int, default=1536,
                   help="Tokenbudget je Batch; 8192 und 2048 sprengten die ~4 GB neben Speaches")
    t.add_argument("--lr-encoder", type=float, default=2e-5)
    t.add_argument("--lr-kopf", type=float, default=1e-4)
    t.add_argument("--seed", type=int, default=20260928)
    t.add_argument("--variante", choices=VARIANTEN, default="getrennt",
                   help="Fragesatz, siehe laya_intent.VARIANTEN")
    t.add_argument("--device", default="cuda")
    t.add_argument("--revision", help="Hub-Revision der Basis (Default: aktuell)")
    s = sub.add_parser("serve")
    s.add_argument("--ckpt", required=True)
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8096)
    s.add_argument("--device", default="cpu")
    s.add_argument("--threads", type=int, default=6)
    args = ap.parse_args()
    return train(args) if args.cmd == "train" else serve(args)


if __name__ == "__main__":
    sys.exit(main())
