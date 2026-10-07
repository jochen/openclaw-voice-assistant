"""Lokale Podman-Container finden und neu starten — ohne dass sie mit dem
Aufrufer sterben.

Die Falle (2026-10-06 03:31): rootless Podman startet die Port-Weiterleitung
(``rootlessport``) als Kind des aufrufenden ``podman``-Prozesses, und sie
bleibt in dessen cgroup. Rief eine systemd-Unit ``podman start`` auf
(``laya-nachtraining-probe``), dann toetete systemd beim Ende der Unit die
Weiterleitung mit — der Container lief innen gesund weiter, sein Port war vom
Host aus tot. Qwen-STT und Laya waren so einen ganzen Tag unerreichbar, und
alles lief still ueber die Rueckfall-Stufen.

Deshalb laeuft jeder Start hier in einem EIGENEN transienten Scope
(``systemd-run --user --scope``): der lebt, solange seine Prozesse leben, und
gehoert keiner Unit, die ihn beim Beenden mitnimmt. Wer in diesem Projekt
einen Container startet, nimmt ``starten()`` — nicht ``podman start`` direkt.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from urllib.parse import urlparse

_LOKAL = {"127.0.0.1", "localhost", "::1", "0.0.0.0", ""}


def _run(*cmd: str, timeout: float = 60.0) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, text=True, capture_output=True, timeout=timeout)


def verfuegbar() -> bool:
    return shutil.which("podman") is not None and shutil.which("systemd-run") is not None


def im_eigenen_scope(name: str, *cmd: str) -> list[str]:
    """``cmd`` als eigener transienter Scope (siehe Modul-Docstring)."""
    einheit = f"container-{name}-{int(time.time() * 1000)}"
    return ["systemd-run", "--user", "--scope", "--quiet", "--collect",
            f"--unit={einheit}", *cmd]


def ist_lokal(url: str) -> bool:
    return (urlparse(url).hostname or "") in _LOKAL


def container_fuer_url(url: str) -> str | None:
    """Der lokale Container, der den Port dieser URL veroeffentlicht.

    Mehrere Container koennen denselben Port tragen (nur einer kann laufen):
    ein laufender gewinnt, sonst nur ein eindeutiger gestoppter. None = keiner
    oder nicht eindeutig — dann wird nicht geheilt, nur gemeldet.
    """
    if not ist_lokal(url) or not verfuegbar():
        return None
    port = urlparse(url).port
    if port is None:
        return None
    r = _run("podman", "ps", "-a", "--format", "json")
    if r.returncode:
        return None
    laufend, gestoppt = [], []
    for c in json.loads(r.stdout or "[]"):
        for p in c.get("Ports") or []:
            if p.get("host_port") == port and (p.get("host_ip") or "") in _LOKAL:
                name = (c.get("Names") or [None])[0]
                (laufend if c.get("State") == "running" else gestoppt).append(name)
    if len(laufend) == 1:
        return laufend[0]
    if not laufend and len(gestoppt) == 1:
        return gestoppt[0]
    return None


def laeuft(name: str) -> bool:
    r = _run("podman", "inspect", name, "--format", "{{.State.Running}}")
    return r.returncode == 0 and r.stdout.strip() == "true"


def starten(name: str) -> subprocess.CompletedProcess:
    return _run(*im_eigenen_scope(name, "podman", "start", name), timeout=120.0)


def neu_starten(name: str) -> str:
    """Stoppen (falls er laeuft) und im eigenen Scope starten. Gibt Klartext
    zurueck, was getan wurde.

    Bewusst nicht ``podman restart``: nach dem Vorfall vom 2026-10-06 war
    conmon tot, und ``restart`` scheiterte mit "conmon exited prematurely" —
    ``stop`` + ``start`` kam durch.
    """
    schritte = []
    if laeuft(name):
        r = _run("podman", "stop", "-t", "10", name)
        schritte.append("gestoppt" if r.returncode == 0
                        else f"stop scheiterte ({(r.stderr or '').strip()[-120:]})")
    r = starten(name)
    schritte.append("gestartet" if r.returncode == 0
                    else f"start scheiterte ({(r.stderr or '').strip()[-120:]})")
    return ", ".join(schritte)
