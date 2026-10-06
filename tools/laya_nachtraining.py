#!/usr/bin/env python3
"""Laya nachts neu trainieren, wenn der Checkpoint nicht mehr zu den capabilities passt.

Aufruf (Projekt-venv wird selbst gesucht; im Betrieb per laya-nachtraining.timer):

    ow-venv/bin/python -m tools.laya_nachtraining                 # pruefen, ggf. trainieren + umschalten
    ow-venv/bin/python -m tools.laya_nachtraining --trocken       # nur sagen, was passieren wuerde
    ow-venv/bin/python -m tools.laya_nachtraining --erzwingen     # auch bei passender Version
    ow-venv/bin/python -m tools.laya_nachtraining --ohne-umschalten

Was einen Lauf ausloest: die capabilities-Version im /health des laufenden
Laya-Containers (`checkpoint.capabilities`, laya/serve.py im Stack) weicht
von der Live-Version ab. Sonst endet der Lauf sofort. Geprueft wird einmal
je Nacht, nicht bei jeder Aenderung: capabilities aendern sich in Schueben
(LAYA_TRAINING.md, „Was die Automatisierung als Ganzes leisten muss“, 1).

Ablauf — jede Stufe eine Falle aus LAYA_TRAINING.md:

 1. Daten: tools/tor_trainset.py gegen die Live-capabilities, Schnappschuss
    und Daten ins private Git von testsets/.
 2. GPU frei: `laya` und die Container aus --weichen stoppen (Falle 3, 12).
    Gemma entscheidet in der Zeit (Laya-Ausfall ist vorgesehen), die STT
    faellt von Qwen auf Parakeet zurueck. Speaches bleibt an. Vorher wird
    geprueft, dass torch im Trainings-venv CUDA sieht (Falle 1) und genug
    Grafikspeicher frei ist.
 3. Training in NEUE Verzeichnisse aktuator-v{N}a/-b, zwei Seeds (die
    Streuung zwischen Laeufen liegt bei 3–9 Saetzen, LAYA_TRAINING.md
    „Stand“). Bei OOM mit halbem Tokenbudget erneut (Falle 3).
 4. Messen: jeder Kandidat UND der laufende Checkpoint gegen das Test-Set,
    gegen dieselben Live-capabilities, in einem Pruef-Container auf
    --pruef-port (nie auf dem Live-Port, sonst entscheidet der Assistent
    mit dem Kandidaten). Ausfaelle machen eine Messung ungueltig (Falle 7).
 5. Die weichenden Container wieder an, Qwen antwortet wieder (Falle 12).
 6. Schranke: bester Kandidat = wenigste FALSCH, dann meiste richtig.
    Umgeschaltet wird nur, wenn er keine Ausfaelle hat, nicht mehr FALSCH als
    der laufende, und seine capabilities noch die Live-Version sind
    (Entscheidung Jochen 2026-10-06: automatisch, mit Schranken).
 7. Umschalten: LAYA_CKPT in der compose-Datei, --force-recreate (Falle 5),
    /health nennt den neuen Checkpoint, Rauchtest = Test-Set gegen den
    Live-Port mit denselben Zahlen (Falle 4, 6 — waermt auch vor). Faellt
    der Rauchtest durch, zurueck auf den alten.
 8. Bericht: Zeile nach laya_nachtraining.jsonl (die Messreihe, maschinell)
    und leise nach Telegram (Argus-Gruppe). Alte Checkpoints bleiben liegen.

Installationsspezifisches (Pfade, compose-Datei, Containernamen) kommt aus
Argumenten oder Umgebungsvariablen, nicht aus dem Code — die Unit liest sie
aus ~/.config/openclaw/laya-nachtraining.env (systemd/laya-nachtraining.service).
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_VENV = os.path.join(_REPO, "ow-venv", "bin", "python")
if os.path.exists(_VENV) and os.path.realpath(sys.executable) != os.path.realpath(_VENV):
    os.execv(_VENV, [_VENV, "-m", "tools.laya_nachtraining", *sys.argv[1:]])

from voice_assistant.config import WORKSPACE, load_profile  # noqa: E402
from voice_assistant.services import container, telegram  # noqa: E402
from voice_assistant.services.actuator import Actuator  # noqa: E402
from voice_assistant.services.aktuator_schatten import laya_checkpoint  # noqa: E402
from tools import aktuator_vergleich  # noqa: E402

_TESTSETS = os.path.join(_REPO, "testsets")
_BERICHT = os.path.join(WORKSPACE, "laya_nachtraining.jsonl")
_SEEDS = (("a", 20260928), ("b", 7))


def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def sh(*cmd, cwd=None, check=True, **kw) -> subprocess.CompletedProcess:
    r = subprocess.run(cmd, cwd=cwd, text=True, capture_output=True, **kw)
    if check and r.returncode:
        raise RuntimeError(f"{' '.join(cmd[:4])}… -> {r.returncode}: {(r.stderr or r.stdout)[-400:]}")
    return r


def warte_health(url: str, sekunden: int = 180) -> dict | None:
    t0 = time.time()
    while time.time() - t0 < sekunden:
        try:
            with urllib.request.urlopen(url.rstrip("/") + "/health", timeout=5) as r:
                return json.loads(r.read().decode())
        except Exception:
            time.sleep(3)
    return None


def frei_mib() -> int:
    r = sh("nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits")
    return int(r.stdout.split()[0])


def laeuft(name: str) -> bool:
    r = sh("podman", "inspect", name, "--format", "{{.State.Running}}", check=False)
    return r.stdout.strip() == "true"


def naechste_version(modelle: str) -> int:
    n = [int(m.group(1)) for d in os.listdir(modelle)
         if (m := re.fullmatch(r"aktuator-v(\d+)[a-z]*", d))]
    return max(n, default=0) + 1


def ckpt_caps(pfad: str) -> str | None:
    try:
        return json.load(open(os.path.join(pfad, "rl_agent_config.json"))).get("capabilities")
    except (OSError, ValueError):
        return None


def zahlen(z) -> dict:
    c = z["laya"]
    return {k: c[k] for k in ("richtig", "verpasst", "FALSCH", "Ausfall")}


def zeile(name: str, z: dict | None) -> str:
    if not z:
        return f"{name}: nicht gemessen"
    return f"{name}: {z['richtig']}/{z['verpasst']}/{z['FALSCH']}" + (
        f" ({z['Ausfall']} Ausfälle!)" if z["Ausfall"] else "")


class Lauf:
    def __init__(self, args, profil) -> None:
        self.a, self.profil = args, profil
        self.cfg = profil.actuator
        self.akt = Actuator(self.cfg)
        self.bericht: dict = {"ts": datetime.now().isoformat(timespec="seconds")}
        self.gestoppt: list[str] = []
        self.image: str | None = None

    # --- Bausteine ----------------------------------------------------
    def messen(self, url: str) -> dict | None:
        """Test-Set gegen einen Laya-Endpunkt, Live-capabilities des Aktuators."""
        self.cfg.laya_url = url
        try:
            z, *_ = aktuator_vergleich.messen(self.akt, url, self.cfg.laya_schwelle,
                                              mit_gemma=False)
            return zahlen(z)
        except Exception as exc:
            log(f"Messung gegen {url} gescheitert: {exc}")
            return None

    def pruef_container(self, ckpt: str) -> dict | None:
        """Checkpoint im Pruef-Container auf der GPU messen."""
        name = "laya-nachtraining-pruefung"
        sh("podman", "rm", "-f", name, check=False)
        port = self.a.pruef_port
        sh("podman", "run", "-d", "--rm", "--name", name, "--device", "nvidia.com/gpu=all",
           "--security-opt", "label=disable", "-p", f"127.0.0.1:{port}:{port}",
           "-e", f"LAYA_CKPT=/modelle/{ckpt}", "-e", "LAYA_DEVICE=cuda", "-e", f"LAYA_PORT={port}",
           "-e", "HF_HUB_OFFLINE=1", "-v", f"{self.a.modelle}:/modelle:ro", self.image)
        try:
            url = f"http://127.0.0.1:{port}"
            h = warte_health(url)
            if not h or (h.get("checkpoint") or {}).get("name") != ckpt:
                log(f"Pruef-Container fuer {ckpt} kam nicht hoch: {h}")
                return None
            return self.messen(url)
        finally:
            sh("podman", "stop", name, check=False)

    def trainieren(self, aus: str, seed: int) -> bool:
        budget = 1536
        while budget >= 384:
            logdatei = aus + ".log"
            t0 = time.time()
            with open(logdatei, "w") as lf:
                r = subprocess.run([self.a.trainer_python, os.path.join(_REPO, "tools",
                                    "laya_aktuator_train.py"), "train", "--aus", aus,
                                    "--seed", str(seed), "--max-token", str(budget)],
                                   cwd=_REPO, stdout=lf, stderr=subprocess.STDOUT)
            text = open(logdatei, errors="replace").read()
            if r.returncode == 0 and ckpt_caps(aus):
                log(f"{os.path.basename(aus)}: trainiert in {(time.time() - t0) / 60:.1f} min")
                return True
            if "out of memory" in text.lower():
                budget //= 2
                log(f"{os.path.basename(aus)}: OOM — neu mit Tokenbudget {budget}")
                continue
            log(f"{os.path.basename(aus)}: Training gescheitert (rc {r.returncode}), "
                f"Log {logdatei}:\n{text[-600:]}")
            return False
        return False

    def compose_ckpt(self, neu: str | None = None) -> str:
        """LAYA_CKPT aus der compose-Datei lesen oder setzen; gibt den alten zurueck."""
        pfad = self.a.compose
        s = open(pfad, encoding="utf-8").read()
        treffer = re.findall(r"LAYA_CKPT=/modelle/([^\s\"']+)", s)
        if len(treffer) != 1:
            raise RuntimeError(f"LAYA_CKPT steht {len(treffer)}-mal in {pfad}, erwartet einmal")
        if neu:
            open(pfad, "w", encoding="utf-8").write(
                s.replace(f"LAYA_CKPT=/modelle/{treffer[0]}", f"LAYA_CKPT=/modelle/{neu}"))
        return treffer[0]

    def recreate_laya(self) -> dict | None:
        d = os.path.dirname(os.path.abspath(self.a.compose))
        # Im eigenen Scope: sonst haengt die Port-Weiterleitung im cgroup
        # dieser Unit und stirbt mit ihr (services/container.py).
        sh(*container.im_eigenen_scope(
            self.a.container, "podman-compose", "-f", os.path.basename(self.a.compose),
            "up", "-d", "--force-recreate", self.a.container), cwd=d)
        return warte_health(self.cfg_url_live)

    def melden(self, text: str) -> None:
        log(text)
        wt = self.profil.watcher
        if wt.chat_id and not self.a.trocken:
            telegram.send(wt.bot_token or self.profil.telegram_bot_token, wt.chat_id, text,
                          leise=True)

    # --- Ablauf -------------------------------------------------------
    def run(self) -> int:
        self.cfg_url_live = self.cfg.laya_url
        if not self.cfg.enabled or not self.cfg_url_live:
            log("Aktuator aus oder keine actuator.laya_url — nichts zu tun")
            return 2
        if not self.akt.refresh():
            self.melden("⚠️ Laya-Nachtraining: capabilities nicht abrufbar — kein Lauf")
            return 2
        live = self.akt.version
        ck = laya_checkpoint(self.cfg_url_live) or {}
        self.bericht.update(live=live, alt=ck)
        if ck.get("capabilities") == live and not self.a.erzwingen:
            log(f"Checkpoint {ck.get('name')} passt zu capabilities {live} — nichts zu tun")
            return 0
        if not ck.get("name"):
            # Ohne Namen kein Vergleich gegen den laufenden — also auch kein
            # Umschalten. Container weg oder /health ohne checkpoint-Feld.
            self.melden(f"⚠️ Laya-Nachtraining: laufender Checkpoint unbekannt "
                        f"({self.cfg_url_live}/health) — kein Lauf")
            return 2
        log(f"Checkpoint {ck['name']} ist fuer {ck.get('capabilities')}, live {live} -> Nachtraining")
        n = naechste_version(self.a.modelle)
        kandidaten = [(f"aktuator-v{n}{s}", seed) for s, seed in _SEEDS]
        if self.a.trocken:
            log(f"trocken: wuerde {', '.join(k for k, _ in kandidaten)} trainieren, "
                f"stoppen: {self.a.container} {' '.join(self.a.weichen)}")
            return 0

        # 1. Daten
        r = sh(sys.executable, "-m", "tools.tor_trainset", "--massive", self.a.massive, cwd=_REPO)
        log(r.stdout.strip().splitlines()[0])
        snap = json.load(open(os.path.join(_TESTSETS, "tor_train.capabilities.json")))
        if snap.get("version") != live:
            self.melden(f"⚠️ Laya-Nachtraining: Daten fuer {snap.get('version')}, live {live} "
                        "— capabilities aenderten sich waehrend des Laufs, abgebrochen")
            return 1
        sh("git", "add", "tor_train.jsonl", "tor_train.capabilities.json", cwd=_TESTSETS)
        sh("git", "commit", "-q", "-m", f"Trainingsdaten capabilities {live} (laya_nachtraining)",
           cwd=_TESTSETS, check=False)

        r = sh(self.a.trainer_python, "-c", "import torch; print(torch.cuda.is_available())",
               check=False)
        if r.stdout.strip() != "True":
            self.melden(f"⚠️ Laya-Nachtraining: torch im Trainings-venv sieht keine GPU "
                        f"({self.a.trainer_python}) — kein Training (LAYA_TRAINING.md Falle 1)")
            return 1
        self.image = sh("podman", "inspect", self.a.container, "--format",
                        "{{.ImageName}}").stdout.strip()

        gemessen: dict[str, dict | None] = {}
        try:
            # 2. GPU frei
            for name in [self.a.container, *self.a.weichen]:
                if laeuft(name):
                    sh("podman", "stop", name)
                    self.gestoppt.append(name)
            time.sleep(3)
            frei = frei_mib()
            log(f"gestoppt: {' '.join(self.gestoppt)}; frei {frei} MiB")
            if frei < self.a.min_frei_mib:
                self.melden(f"⚠️ Laya-Nachtraining: nur {frei} MiB Grafikspeicher frei "
                            f"(mindestens {self.a.min_frei_mib}) — kein Training")
                return 1

            # 3. Training
            fertig = [(k, seed) for k, seed in kandidaten
                      if self.trainieren(os.path.join(self.a.modelle, k), seed)]

            # 4. Messen, der laufende zuerst
            gemessen[ck["name"]] = self.pruef_container(ck["name"])
            for k, _ in fertig:
                gemessen[k] = self.pruef_container(k)
        finally:
            # 5. Was wir gestoppt haben, wieder an — auch laya mit dem alten
            #    Checkpoint: bis zum Umschalten entscheidet er, nicht Gemma.
            for name in self.gestoppt:
                if not laeuft(name):
                    # Nicht `podman start` direkt: die Port-Weiterleitung
                    # starb sonst mit dieser Unit (2026-10-06 03:31, Qwen und
                    # Laya einen Tag lang vom Host aus tot).
                    container.starten(name)
        self.bericht["gemessen"] = gemessen
        self.qwen_pruefen()

        # 6. Schranke
        alt = gemessen.get(ck["name"])
        gueltig = {k: z for k, z in gemessen.items()
                   if k != ck["name"] and z and z["Ausfall"] == 0}
        text = [f"Laya-Nachtraining capabilities {live} (bisher {ck['name']} für {ck.get('capabilities')})",
                "Test-Set richtig/verpasst/FALSCH:", zeile(ck["name"], alt)]
        text += [zeile(k, gemessen.get(k)) for k, _ in kandidaten]
        if not gueltig:
            self.bericht["ergebnis"] = "kein gueltiger Kandidat"
            self.melden("⚠️ " + "\n".join(text + ["Kein gültiger Kandidat — bleibt beim alten."]))
            return 1
        best = min(gueltig, key=lambda k: (gueltig[k]["FALSCH"], -gueltig[k]["richtig"]))
        bz = gueltig[best]
        self.akt.refresh()
        grund = None
        if not alt or alt["Ausfall"]:
            grund = "der laufende Checkpoint ließ sich nicht messen"
        elif bz["FALSCH"] > alt["FALSCH"]:
            grund = f"{best} hat mehr FALSCH ({bz['FALSCH']} gegen {alt['FALSCH']})"
        elif ckpt_caps(os.path.join(self.a.modelle, best)) != self.akt.version:
            grund = f"capabilities sind inzwischen {self.akt.version}"
        elif self.a.ohne_umschalten:
            grund = "--ohne-umschalten"
        if grund:
            self.bericht["ergebnis"] = f"nicht umgeschaltet: {grund}"
            self.melden("\n".join(text + [f"Nicht umgeschaltet: {grund}. "
                                          f"{best} liegt bereit."]))
            return 0

        # 7. Umschalten + Rauchtest
        vorher = self.compose_ckpt(best)
        h = self.recreate_laya()
        rauch = self.messen(self.cfg_url_live) if h else None
        name_ok = h and (h.get("checkpoint") or {}).get("name") == best
        if not (name_ok and rauch and rauch["Ausfall"] == 0 and rauch["FALSCH"] == bz["FALSCH"]):
            self.compose_ckpt(vorher)
            self.recreate_laya()
            self.bericht["ergebnis"] = f"Rauchtest durchgefallen, zurueck auf {vorher}"
            self.melden("⚠️ " + "\n".join(text + [
                f"{best} geschaltet, Rauchtest durchgefallen ({zeile('live', rauch)}, "
                f"/health {((h or {}).get('checkpoint') or {}).get('name')}) — zurück auf {vorher}."]))
            return 1
        self.compose_commit(best)
        self.bericht["ergebnis"] = f"umgeschaltet auf {best}"
        self.melden("\n".join(text + [f"✅ Umgeschaltet auf {best}, Rauchtest {zeile('live', rauch)}. "
                                      f"Zurück: LAYA_CKPT={vorher}."]))
        return 0

    def qwen_pruefen(self) -> None:
        """Nach dem Wiederanlauf: antwortet die STT, die wir gestoppt hatten? (Falle 12)"""
        url = getattr(self.profil, "stt_llamacpp_url", "")
        if not url:
            return
        u = urllib.parse.urlsplit(url)
        h = warte_health(f"{u.scheme}://{u.netloc}", sekunden=300)
        if h is None:
            self.melden(f"⚠️ Laya-Nachtraining: STT ({u.netloc}) antwortet nach dem "
                        "Wiederanlauf nicht — Parakeet springt ein, bitte nachsehen")

    def compose_commit(self, best: str) -> None:
        d = os.path.dirname(os.path.abspath(self.a.compose))
        f = os.path.basename(self.a.compose)
        diff = sh("git", "diff", "--unified=0", "--", f, cwd=d, check=False).stdout
        geaendert = [z for z in diff.splitlines() if z[:1] in "+-" and z[:3] not in ("+++", "---")]
        if all("LAYA_CKPT=" in z for z in geaendert):
            sh("git", "add", "--", f, cwd=d, check=False)
            sh("git", "commit", "-q", "-m", f"laya: LAYA_CKPT auf {best} (laya_nachtraining)",
               cwd=d, check=False)
        else:
            log(f"{f} hat weitere Aenderungen — nicht committet")


def main() -> int:
    env = os.environ.get
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--compose", default=env("LAYA_COMPOSE"),
                    help="compose-Datei mit LAYA_CKPT (env LAYA_COMPOSE)")
    ap.add_argument("--modelle", default=env("LAYA_MODELLE", os.path.expanduser("~/laya-modelle")))
    ap.add_argument("--massive", default=env("LAYA_MASSIVE"), help="MASSIVE 1.1 de-DE.jsonl")
    ap.add_argument("--trainer-python", default=env("LAYA_TRAIN_PYTHON",
                                                    os.path.expanduser("~/laya-test/venv/bin/python")))
    ap.add_argument("--container", default=env("LAYA_CONTAINER", "laya"))
    ap.add_argument("--weichen", default=env("LAYA_GPU_WEICHEN", ""),
                    help="weitere Container, die waehrend des Trainings aus sind (Leerzeichen-getrennt)")
    ap.add_argument("--pruef-port", type=int, default=int(env("LAYA_PRUEF_PORT", "8097")))
    ap.add_argument("--min-frei-mib", type=int, default=int(env("LAYA_MIN_FREI_MIB", "4500")))
    ap.add_argument("--erzwingen", action="store_true")
    ap.add_argument("--ohne-umschalten", action="store_true")
    ap.add_argument("--trocken", action="store_true")
    a = ap.parse_args()
    a.weichen = a.weichen.split()
    a.modelle = os.path.expanduser(a.modelle)
    if not a.compose or not a.massive:
        ap.error("--compose und --massive (oder LAYA_COMPOSE, LAYA_MASSIVE) sind noetig")

    os.makedirs(a.modelle, exist_ok=True)
    sperre = open(os.path.join(a.modelle, ".nachtraining.lock"), "w")
    try:
        fcntl.flock(sperre, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log("laeuft schon")
        return 1
    lauf = Lauf(a, load_profile())
    try:
        rc = lauf.run()
    except Exception as exc:
        lauf.bericht["ergebnis"] = f"Fehler: {exc}"
        lauf.melden(f"⚠️ Laya-Nachtraining abgebrochen: {exc}")
        rc = 1
    if "ergebnis" in lauf.bericht:
        with open(_BERICHT, "a", encoding="utf-8") as f:
            f.write(json.dumps(lauf.bericht, ensure_ascii=False) + "\n")
    return rc


if __name__ == "__main__":
    sys.exit(main())
