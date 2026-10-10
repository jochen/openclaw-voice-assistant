"""Laya-Training auf einem anderen Rechner per ssh — für tools/laya_nachtraining.py.

Daheim stoppt jedes Training `laya` und die STT-Stufe Qwen (die 8-GB-Karte
reicht nicht für beides), deshalb lief es nur nachts. Ein zweiter Rechner mit
freier GPU trainiert, ohne dass daheim etwas ausfällt — also gleich, wenn sich
die capabilities geändert haben (Jochen 2026-10-10). Ist er nicht da, bleibt
es beim Nachtlauf daheim.

Was dort liegen muss (einmalig, `--fern-verz`, Default ~/laya-train):

    venv/   torch 2.14.0+cu130 und laya[serve]==0.3.21, wie im Laya-Image
            (Kopfzeilen von tools/laya_aktuator_train.py)

Alles andere bringt jeder Lauf mit und löscht es danach wieder
(`<verz>/lauf/`: Code, Trainingsdaten, Checkpoints, Logs — Jochen: die Daten
nach dem Training löschen). Die Kandidaten werden dort gemessen, mit dem
`serve.py` des Laya-Images (identischer Server, gleiche laya/torch-Fassung)
hinter einem ssh-Tunnel, und danach nach Hause geholt.

Drei Fallen, gemessen beim Bau:

- **Basis-Revision.** Der Hub-Cache dort hatte eine andere Revision der
  Basis (7b928d8) als daheim (55cf4c4). Ohne Festlegung wäre der Fern-
  Kandidat stillschweigend auf einer anderen Basis trainiert. Deshalb wird
  die Revision von daheim übergeben und dort vorher gezogen.
- **Das LLM dort teilt sich die Karten.** Erst wird versucht, daneben zu
  trainieren (Spitze ~2,9 GB je Kandidat). Reicht der Platz nicht oder
  kommt OOM, wird es gestoppt und danach im eigenen Scope wieder gestartet
  (Jochen: „wenn der Speicher nicht reicht, können wir das LLM entladen“).
  Im eigenen Scope, weil die Port-Weiterleitung sonst an dieser ssh-Sitzung
  hängt (services/container.py, dieselbe Falle wie daheim).
- **Ein Prozess ohne Terminal stirbt nicht mit ssh.** Der Prüf-Server
  schreibt seine PID und wird ausdrücklich beendet.

Messreihe (Fablab-Server, 2× RTX 5060 Ti, LLM belegt 12–13 GB je Karte):

    2026-10-10 18:46  v7a 11,5 min auf Karte 0 (3,4 GB frei, Spitze 3,3 GB);
                      v7b OOM — die Meldung stand nicht in den letzten 600
                      Zeichen des Logs und wurde übersehen (seither grep über
                      das ganze Log). Holen 647 MB in ~13 min (~0,85 MB/s).
                      Umgeschaltet auf v7a: 349/21/1 statt 346/23/2,
                      Rauchtest daheim mit denselben Zahlen (andere GPU-
                      Generation, gleiche Ergebnisse).
    2026-10-10 19:20  Probelauf --erzwingen --ohne-umschalten: v8b OOM ->
                      LLM gestoppt, v8b neu, LLM danach im eigenen Scope
                      wieder an (healthy). Nur der beste geholt, mit Vorlage
                      in 4,4 min. Schranke hielt: v8a 350/18/3, v8b
                      337/24/10 — nicht umgeschaltet.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import threading
import time

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Was das Training dort braucht, relativ zum Repo (laya_aktuator_train.py
# importiert laya_intent, nur stdlib).
_CODE = ("tools/laya_aktuator_train.py", "voice_assistant/__init__.py",
         "voice_assistant/services/__init__.py", "voice_assistant/services/laya_intent.py")
_DATEN = ("tor_train.jsonl", "tor_train.capabilities.json")
_BASIS_REPO = "convaiinnovations/laya"


def basis_revision_daheim() -> str | None:
    """Die Revision, mit der daheim (HF_HUB_OFFLINE) trainiert würde."""
    hub = os.environ.get("HF_HUB_CACHE") or os.path.join(
        os.environ.get("HF_HOME") or os.path.expanduser("~/.cache/huggingface"), "hub")
    pfad = os.path.join(hub, "models--" + _BASIS_REPO.replace("/", "--"), "refs", "main")
    try:
        return open(pfad).read().strip() or None
    except OSError:
        return None


class Fern:
    def __init__(self, ziel: str, verz: str, llm: str, min_frei_mib: int, log) -> None:
        self.ziel, self.verz, self.llm = ziel, verz.rstrip("/"), llm
        self.min_frei, self.log = min_frei_mib, log
        self.lauf = f"{self.verz}/lauf"
        self.py = f"{self.verz}/venv/bin/python"
        self.llm_gestoppt = False
        self._llm_lock = threading.Lock()
        self.revision: str | None = None

    # --- ssh ------------------------------------------------------------
    def _ssh_argv(self, *extra: str) -> list[str]:
        return ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
                "-o", "ServerAliveInterval=30", *extra, self.ziel]

    def ssh(self, befehl: str, check: bool = True, timeout: float | None = 120
            ) -> subprocess.CompletedProcess:
        r = subprocess.run(self._ssh_argv() + [befehl], text=True, capture_output=True,
                           timeout=timeout)
        if check and r.returncode:
            raise RuntimeError(f"fern {befehl[:60]!r} -> {r.returncode}: "
                               f"{(r.stderr or r.stdout)[-400:]}")
        return r

    def rsync(self, *args: str) -> None:
        r = subprocess.run(["rsync", "-a", "-e", "ssh -o BatchMode=yes -o ConnectTimeout=10",
                            *args], text=True, capture_output=True, cwd=_REPO)
        if r.returncode:
            raise RuntimeError(f"rsync {' '.join(args[-2:])} -> {r.returncode}: {r.stderr[-400:]}")

    # --- Bereitschaft -----------------------------------------------------
    def bereit(self, revision: str | None) -> str | None:
        """None, wenn dort trainiert werden kann; sonst der Grund."""
        if not revision:
            return "Basis-Revision daheim unbekannt"
        try:
            r = self.ssh(f"{shlex.quote(self.py)} -c 'import torch,laya;"
                         f"print(torch.cuda.is_available())'", check=False, timeout=60)
        except (subprocess.TimeoutExpired, OSError) as exc:
            return f"nicht erreichbar ({type(exc).__name__})"
        if r.returncode == 255:
            return f"nicht erreichbar ({r.stderr.strip()[-120:]})"
        if r.stdout.strip() != "True":
            return f"venv ohne CUDA ({(r.stderr or r.stdout).strip()[-160:]})"
        # Ab hier absolut: die Befehle wechseln in den Laufordner, ein
        # relativer venv-Pfad zeigte dann ins Leere (erster Lauf 2026-10-10).
        self.verz = self.ssh(f"cd {shlex.quote(self.verz)} && pwd").stdout.strip()
        self.lauf = f"{self.verz}/lauf"
        self.py = f"{self.verz}/venv/bin/python"
        # Dieselbe Basis wie daheim, notfalls jetzt aus dem Hub ziehen.
        r = self.ssh(f"{shlex.quote(self.py)} -c "
                     + shlex.quote("from huggingface_hub import snapshot_download as s;"
                                   f"s({_BASIS_REPO!r}, allow_patterns=['multilingual/*'],"
                                   f" revision={revision!r})"),
                     check=False, timeout=900)
        if r.returncode:
            return f"Basis-Revision {revision[:7]} dort nicht verfügbar: {r.stderr.strip()[-160:]}"
        self.revision = revision
        return None

    def gpus(self) -> list[tuple[int, int]]:
        """[(Index, frei MiB)], die freieste zuerst."""
        r = self.ssh("nvidia-smi --query-gpu=index,memory.free --format=csv,noheader,nounits")
        g = [tuple(int(x) for x in z.split(",")) for z in r.stdout.strip().splitlines()]
        return sorted(g, key=lambda t: -t[1])

    # --- LLM dort ---------------------------------------------------------
    def llm_stoppen(self, grund: str) -> bool:
        with self._llm_lock:
            if self.llm_gestoppt:
                return True
            if not self.llm:
                return False
            self.log(f"fern: stoppe {self.llm} ({grund})")
            self.ssh(f"podman stop {shlex.quote(self.llm)}", timeout=120)
            self.llm_gestoppt = True
            time.sleep(3)
            return True

    def llm_starten(self) -> str | None:
        """Wieder an, im eigenen Scope. None = läuft, sonst Fehlertext."""
        if not self.llm_gestoppt:
            return None
        r = self.ssh("systemd-run --user --scope --quiet --collect podman start "
                     + shlex.quote(self.llm), check=False, timeout=120)
        if r.returncode:
            return f"podman start {self.llm}: {(r.stderr or r.stdout).strip()[-200:]}"
        self.llm_gestoppt = False
        self.log(f"fern: {self.llm} wieder gestartet (eigener Scope)")
        return None

    # --- Lauf -------------------------------------------------------------
    def vorbereiten(self, testsets: str, serve_py: str) -> None:
        self.ssh(f"rm -rf {shlex.quote(self.lauf)} && mkdir -p "
                 f"{shlex.quote(self.lauf)}/testsets {shlex.quote(self.lauf)}/ckpt")
        self.rsync("-R", *_CODE, f"{self.ziel}:{self.lauf}/")  # -R: Pfade relativ zum Repo
        self.rsync(*[os.path.join(testsets, d) for d in _DATEN], serve_py,
                   f"{self.ziel}:{self.lauf}/testsets/")
        self.ssh(f"mv {shlex.quote(self.lauf)}/testsets/serve.py {shlex.quote(self.lauf)}/")

    def trainieren(self, name: str, seed: int, gpu: int) -> bool:
        budget = 1536
        while budget >= 384:
            t0 = time.time()
            befehl = (f"cd {shlex.quote(self.lauf)} && CUDA_VISIBLE_DEVICES={gpu} "
                      f"HF_HUB_OFFLINE=1 PYTHONUNBUFFERED=1 {shlex.quote(self.py)} "
                      f"tools/laya_aktuator_train.py train --aus ckpt/{name} --seed {seed} "
                      f"--max-token {budget} --revision {self.revision} "
                      f"> ckpt/{name}.log 2>&1; echo rc=$?; tail -c 600 ckpt/{name}.log; "
                      f"test -f ckpt/{name}/rl_agent_config.json && echo CKPT_OK; "
                      # im ganzen Log suchen: die letzten 600 Zeichen schnitten
                      # "CUDA out of memory" ab (zweiter Lauf 2026-10-10)
                      f"grep -qi 'out of memory' ckpt/{name}.log && echo OOM_ERKANNT")
            r = self.ssh(befehl, check=False, timeout=3 * 3600)
            if "CKPT_OK" in r.stdout and "rc=0" in r.stdout:
                self.log(f"fern: {name} auf GPU {gpu} trainiert in "
                         f"{(time.time() - t0) / 60:.1f} min")
                return True
            if "OOM_ERKANNT" in r.stdout:
                # Erst das LLM dort entladen, erst dann am Rezept sparen.
                if self.llm and not self.llm_gestoppt:
                    self.llm_stoppen(f"OOM bei {name}")
                    continue
                budget //= 2
                self.log(f"fern: {name}: OOM — neu mit Tokenbudget {budget}")
                continue
            self.log(f"fern: {name}: Training gescheitert:\n{(r.stdout + r.stderr)[-600:]}")
            return False
        return False

    def alle_trainieren(self, kandidaten: list[tuple[str, int]]) -> list[str]:
        """Kandidaten auf die Karten verteilen; reicht keine, LLM entladen."""
        platz = [g for g, frei in self.gpus() if frei >= self.min_frei]
        if not platz:
            self.llm_stoppen(f"keine Karte mit {self.min_frei} MiB frei")
            platz = [g for g, frei in self.gpus() if frei >= self.min_frei]
        if not platz:
            raise RuntimeError(f"auch ohne LLM keine Karte mit {self.min_frei} MiB frei")
        self.log(f"fern: Karten mit Platz: {platz}")
        # je Karte eine Warteschlange; zwei Karten = zwei Kandidaten parallel
        schlangen: dict[int, list] = {g: [] for g in platz}
        for i, k in enumerate(kandidaten):
            schlangen[platz[i % len(platz)]].append(k)
        fertig: list[str] = []

        def abarbeiten(gpu: int, liste: list) -> None:
            for name, seed in liste:
                if self.trainieren(name, seed, gpu):
                    fertig.append(name)

        threads = [threading.Thread(target=abarbeiten, args=(g, l)) for g, l in schlangen.items()]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        return [k for k, _ in kandidaten if k in fertig]

    def pruef_server(self, name: str, port: int, gpu: int) -> subprocess.Popen:
        """serve.py dort, per ssh-Tunnel auf 127.0.0.1:<port> daheim."""
        befehl = (f"cd {shlex.quote(self.lauf)} && echo $$ > serve.pid && exec env "
                  f"CUDA_VISIBLE_DEVICES={gpu} LAYA_CKPT=$PWD/ckpt/{name} LAYA_DEVICE=cuda "
                  f"LAYA_PORT={port} HF_HUB_OFFLINE=1 {shlex.quote(self.py)} serve.py "
                  f"> serve-{name}.log 2>&1")
        return subprocess.Popen(
            self._ssh_argv("-o", "ExitOnForwardFailure=yes",
                           "-L", f"127.0.0.1:{port}:127.0.0.1:{port}") + [befehl],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def pruef_server_stoppen(self, p: subprocess.Popen) -> None:
        self.ssh(f"kill $(cat {shlex.quote(self.lauf)}/serve.pid) 2>/dev/null; true",
                 check=False, timeout=30)
        p.terminate()
        try:
            p.wait(10)
        except subprocess.TimeoutExpired:
            p.kill()

    def holen(self, name: str, modelle: str, vorlage: str | None = None) -> None:
        """Checkpoint nach Hause. Die Leitung schaffte beim ersten Lauf nur
        ~0,85 MB/s (647 MB = 12 min). Die eingefrorenen Wort-Einbettungen
        (197 von 322 Mio. Parametern) sind in jedem Checkpoint gleich — mit
        dem laufenden als Vorlage im Ziel überträgt rsync nur die Differenz."""
        ziel = os.path.join(modelle, name)
        if vorlage and os.path.isdir(vorlage) and not os.path.exists(ziel):
            subprocess.run(["cp", "-a", "--reflink=auto", vorlage, ziel], check=True)
        self.rsync("--delete", "--stats", f"{self.ziel}:{self.lauf}/ckpt/{name}/", ziel + "/")
        self.rsync(f"{self.ziel}:{self.lauf}/ckpt/{name}.log", os.path.join(modelle, name) + ".log")

    def aufraeumen(self) -> None:
        self.ssh(f"rm -rf {shlex.quote(self.lauf)}", check=False, timeout=120)
