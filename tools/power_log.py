#!/usr/bin/env python3
"""Leistungsaufnahme des GPU-Hosts mitschreiben — Grundlinie statt Schätzung.

Aufruf:
    sudo /usr/bin/python3 -m tools.power_log record          # Dauerbetrieb (systemd)
    /usr/bin/python3 -m tools.power_log report               # Auswertung, ganzer Bestand
    /usr/bin/python3 -m tools.power_log report --tage 7
    /usr/bin/python3 -m tools.power_log report --preis 0.30  # €/kWh für die Hochrechnung

Warum es dieses Werkzeug gibt
-----------------------------
Am 2026-08-13 war die Frage „was zieht der Rechner?" nur mit einer Schätzung
zu beantworten: die Chips melden ihre eigene Leistung, das Netzteil meldet
nichts. Chip-Summe im Leerlauf ~20 W, hochgerechnete Wandleistung 45–60 W —
und die zweite Zahl war geraten. Geschätzte Zahlen taugen nicht als
Entscheidungsgrundlage dafür, ob man am Betrieb etwas ändert. Also erst
messen, was messbar IST, eine Woche lang, und dann entscheiden.

Was gemessen wird (und was NICHT)
---------------------------------
Gemessen:
  cpu_pkg_w   RAPL ``package-0`` — CPU-Package, feinaufgelöst (Energiezähler,
              als Differenz über das Intervall; braucht root)
  cpu_core_w  RAPL ``core`` — nur die Kerne, Teilmenge von cpu_pkg_w
  apu_ppt_w   amdgpu ``power1_input`` (Label PPT) — Sockelleistung der APU,
              1-W-Granularität. ÜBERLAPPT mit cpu_pkg_w, nicht addieren.
              Steht als Gegenprobe zu RAPL in der Datei, nicht als Summand.
  gpu_w       NVML ``PowerUsage`` der dedizierten Karte
  chips_w     cpu_pkg_w + gpu_w — die einzige belastbare Summe hier

NICHT gemessen — und deshalb in keiner Spalte enthalten: Mainboard, RAM,
NVMe, Lüfter, Netzteil-Wirkungsgrad. Die Wandleistung ist damit systematisch
HÖHER als ``chips_w``, um einen Betrag, den dieses Werkzeug nicht kennt. Wer
die Wandleistung braucht, braucht eine messende Steckdose; ``chips_w`` ist
die Untergrenze, nicht die Antwort.

Zeilenformat
------------
Abgetastet wird jede Sekunde, geschrieben wird alle ``--intervall`` Sekunden
(Default 10) eine Zeile mit Mittel UND Maximum. Das Maximum steht dort,
damit kurze Lastspitzen — ein Sprach-Turn dauert Sekunden — nicht im Mittel
verschwinden. Eine Datei pro Tag, ~8600 Zeilen/Tag, ~700 kB.
"""

from __future__ import annotations

import argparse
import csv
import os
import signal
import statistics
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

LOG_DIR = Path(os.environ.get("POWER_LOG_DIR", "/var/log/power-log"))

RAPL_PKG = Path("/sys/class/powercap/intel-rapl:0")
RAPL_CORE = Path("/sys/class/powercap/intel-rapl:0:0")

SPALTEN = [
    "ts",
    "sekunden",
    "proben",
    "cpu_pkg_w",
    "cpu_pkg_w_max",
    "cpu_core_w",
    "apu_ppt_w",
    "gpu_w",
    "gpu_w_max",
    "gpu_util",
    "gpu_mem_mb",
    "gpu_sm_mhz",
    "chips_w",
    "load1",
]


# --------------------------------------------------------------------------
# Sensoren
# --------------------------------------------------------------------------


class RaplZaehler:
    """Ein RAPL-Energiezähler als Leistung über die Zeit.

    RAPL liefert Mikrojoule seit Start, kein Watt. Leistung ist die Differenz
    zweier Ablesungen geteilt durch die verstrichene Zeit. Der Zähler läuft
    bei ``max_energy_range_uj`` über — bei ~65 kJ und realistischer Last erst
    nach Minuten, aber unbehandelt gäbe das genau dann einen absurden
    negativen Ausreißer, wenn niemand hinschaut.
    """

    def __init__(self, pfad: Path):
        self.datei = pfad / "energy_uj"
        self.name = (pfad / "name").read_text().strip()
        try:
            self.spanne = int((pfad / "max_energy_range_uj").read_text())
        except OSError:
            self.spanne = 0
        self._uj = self._lesen()
        self._t = time.monotonic()

    def _lesen(self) -> int | None:
        try:
            return int(self.datei.read_text())
        except PermissionError:
            return None
        except OSError:
            return None

    def watt(self) -> float | None:
        """Leistung seit der letzten Abfrage. Erste Abfrage liefert None."""
        jetzt = time.monotonic()
        uj = self._lesen()
        if uj is None or self._uj is None:
            self._uj, self._t = uj, jetzt
            return None
        dt = jetzt - self._t
        duj = uj - self._uj
        if duj < 0 and self.spanne:
            duj += self.spanne
        self._uj, self._t = uj, jetzt
        if dt <= 0 or duj < 0:
            return None
        return duj / 1_000_000.0 / dt


def apu_ppt_watt() -> float | None:
    """amdgpu ``power1_input`` in Watt, falls die APU sie meldet."""
    for hw in sorted(Path("/sys/class/hwmon").glob("hwmon*")):
        try:
            if (hw / "name").read_text().strip() != "amdgpu":
                continue
            return int((hw / "power1_input").read_text()) / 1_000_000.0
        except OSError:
            continue
    return None


class Gpu:
    """NVML statt ``nvidia-smi``-Aufruf je Sekunde.

    Ein Prozessstart pro Sekunde kostet selbst spürbar CPU — bei einem
    Werkzeug, das Stromverbrauch misst, wäre das eine Messung, die ihren
    eigenen Gegenstand verfälscht.
    """

    def __init__(self):
        self.handle = None
        self.nvml = None
        try:
            import pynvml

            pynvml.nvmlInit()
            self.nvml = pynvml
            self.handle = pynvml.nvmlDeviceGetHandleByIndex(0)
        except Exception as e:  # noqa: BLE001 — jeder Fehler heißt: keine GPU-Daten
            print(f"⚠️  NVML nicht verfügbar ({e}) — GPU-Spalten bleiben leer", file=sys.stderr)

    def probe(self) -> dict[str, float | None]:
        leer = {"gpu_w": None, "gpu_util": None, "gpu_mem_mb": None, "gpu_sm_mhz": None}
        if self.handle is None:
            return leer
        n = self.nvml
        werte = dict(leer)
        try:
            werte["gpu_w"] = n.nvmlDeviceGetPowerUsage(self.handle) / 1000.0
        except Exception:  # noqa: BLE001
            pass
        try:
            werte["gpu_util"] = float(n.nvmlDeviceGetUtilizationRates(self.handle).gpu)
        except Exception:  # noqa: BLE001
            pass
        try:
            werte["gpu_mem_mb"] = n.nvmlDeviceGetMemoryInfo(self.handle).used / 1024 / 1024
        except Exception:  # noqa: BLE001
            pass
        try:
            werte["gpu_sm_mhz"] = float(n.nvmlDeviceGetClockInfo(self.handle, n.NVML_CLOCK_SM))
        except Exception:  # noqa: BLE001
            pass
        return werte


# --------------------------------------------------------------------------
# Aufzeichnen
# --------------------------------------------------------------------------


def _mittel(werte: list[float | None]) -> float | None:
    echte = [w for w in werte if w is not None]
    return statistics.fmean(echte) if echte else None


def _max(werte: list[float | None]) -> float | None:
    echte = [w for w in werte if w is not None]
    return max(echte) if echte else None


def _rund(wert: float | None, stellen: int = 2) -> str:
    return "" if wert is None else f"{wert:.{stellen}f}"


def tagesdatei(zeit: datetime) -> Path:
    return LOG_DIR / f"power-{zeit:%Y-%m-%d}.csv"


def record(intervall: int, takt: float) -> int:
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    pkg = RaplZaehler(RAPL_PKG) if RAPL_PKG.exists() else None
    core = RaplZaehler(RAPL_CORE) if RAPL_CORE.exists() else None
    if pkg and pkg.watt() is None and os.geteuid() != 0:
        print("⚠️  RAPL nicht lesbar — als root starten, sonst fehlen die CPU-Spalten", file=sys.stderr)
    gpu = Gpu()

    laeuft = True

    def stoppen(signum, rahmen):  # noqa: ARG001
        nonlocal laeuft
        laeuft = False

    signal.signal(signal.SIGTERM, stoppen)
    signal.signal(signal.SIGINT, stoppen)

    proben: dict[str, list] = {k: [] for k in ("pkg", "core", "ppt", "gpu_w", "gpu_util", "gpu_mem_mb", "gpu_sm_mhz")}
    fenster_start = time.monotonic()
    naechste = fenster_start

    while laeuft:
        naechste += takt
        proben["pkg"].append(pkg.watt() if pkg else None)
        proben["core"].append(core.watt() if core else None)
        proben["ppt"].append(apu_ppt_watt())
        g = gpu.probe()
        for k in ("gpu_w", "gpu_util", "gpu_mem_mb", "gpu_sm_mhz"):
            proben[k].append(g[k])

        if time.monotonic() - fenster_start >= intervall or not laeuft:
            _zeile_schreiben(proben, time.monotonic() - fenster_start)
            for liste in proben.values():
                liste.clear()
            fenster_start = time.monotonic()

        schlaf = naechste - time.monotonic()
        if schlaf > 0:
            time.sleep(schlaf)
        else:
            naechste = time.monotonic()

    if any(proben.values()):
        _zeile_schreiben(proben, time.monotonic() - fenster_start)
    return 0


def _zeile_schreiben(proben: dict[str, list], dauer: float) -> None:
    jetzt = datetime.now()
    datei = tagesdatei(jetzt)
    neu = not datei.exists()

    pkg_m = _mittel(proben["pkg"])
    gpu_m = _mittel(proben["gpu_w"])
    chips = None if (pkg_m is None or gpu_m is None) else pkg_m + gpu_m

    try:
        load1 = os.getloadavg()[0]
    except OSError:
        load1 = None

    zeile = {
        "ts": jetzt.isoformat(timespec="seconds"),
        "sekunden": f"{dauer:.1f}",
        "proben": str(len(proben["pkg"])),
        "cpu_pkg_w": _rund(pkg_m),
        "cpu_pkg_w_max": _rund(_max(proben["pkg"])),
        "cpu_core_w": _rund(_mittel(proben["core"])),
        "apu_ppt_w": _rund(_mittel(proben["ppt"])),
        "gpu_w": _rund(gpu_m),
        "gpu_w_max": _rund(_max(proben["gpu_w"])),
        "gpu_util": _rund(_mittel(proben["gpu_util"]), 1),
        "gpu_mem_mb": _rund(_mittel(proben["gpu_mem_mb"]), 0),
        "gpu_sm_mhz": _rund(_mittel(proben["gpu_sm_mhz"]), 0),
        "chips_w": _rund(chips),
        "load1": _rund(load1),
    }

    with datei.open("a", newline="") as f:
        schreiber = csv.DictWriter(f, fieldnames=SPALTEN)
        if neu:
            schreiber.writeheader()
        schreiber.writerow(zeile)


# --------------------------------------------------------------------------
# Auswerten
# --------------------------------------------------------------------------


def _spalte(zeilen: list[dict], name: str) -> list[float]:
    werte = []
    for z in zeilen:
        roh = z.get(name, "")
        if roh:
            try:
                werte.append(float(roh))
            except ValueError:
                pass
    return werte


def _perzentil(werte: list[float], p: float) -> float:
    geordnet = sorted(werte)
    if not geordnet:
        return 0.0
    k = min(len(geordnet) - 1, max(0, int(round(p / 100.0 * (len(geordnet) - 1)))))
    return geordnet[k]


def report(tage: int | None, preis: float | None) -> int:
    if not LOG_DIR.exists():
        print(f"Keine Daten: {LOG_DIR} existiert nicht.", file=sys.stderr)
        return 1

    dateien = sorted(LOG_DIR.glob("power-*.csv"))
    if tage:
        grenze = (datetime.now() - timedelta(days=tage)).strftime("power-%Y-%m-%d.csv")
        dateien = [d for d in dateien if d.name >= grenze]
    if not dateien:
        print("Keine Daten im gewählten Zeitraum.", file=sys.stderr)
        return 1

    zeilen: list[dict] = []
    for d in dateien:
        with d.open(newline="") as f:
            zeilen.extend(csv.DictReader(f))
    if not zeilen:
        print("Dateien vorhanden, aber leer.", file=sys.stderr)
        return 1

    abdeckung = sum(_spalte(zeilen, "sekunden"))
    gedeckt = (f"{abdeckung / 60:.1f} min" if abdeckung < 3600
               else f"{abdeckung / 3600:.1f} h")
    print(f"Bestand: {len(dateien)} Datei(en), {len(zeilen)} Zeilen, {gedeckt} Abdeckung")
    print(f"Von {zeilen[0]['ts']} bis {zeilen[-1]['ts']}\n")

    print(f"{'Größe':<14}{'Mittel':>9}{'Median':>9}{'p95':>9}{'Max':>9}")
    print("-" * 50)
    for name, einheit in (
        ("cpu_pkg_w", "W"),
        ("gpu_w", "W"),
        ("chips_w", "W"),
        ("gpu_util", "%"),
        ("load1", ""),
    ):
        w = _spalte(zeilen, name)
        if not w:
            continue
        hoch = _spalte(zeilen, name + "_max") or w
        print(f"{name:<14}{statistics.fmean(w):>9.2f}{statistics.median(w):>9.2f}"
              f"{_perzentil(w, 95):>9.2f}{max(hoch):>9.2f}  {einheit}")

    chips = _spalte(zeilen, "chips_w")
    if chips and abdeckung > 0:
        mittel = statistics.fmean(chips)
        kwh_jahr = mittel * 8760 / 1000
        print(f"\nChip-Summe im Mittel: {mittel:.1f} W")
        print(f"Hochgerechnet: {kwh_jahr:.0f} kWh/Jahr", end="")
        if preis:
            print(f" ≈ {kwh_jahr * preis:.0f} € bei {preis:.2f} €/kWh")
        else:
            print()
        print("  Untergrenze — ohne Board, RAM, NVMe, Lüfter und Netzteilverlust.")

    gpu = _spalte(zeilen, "gpu_w")
    util = _spalte(zeilen, "gpu_util")
    if gpu and util and len(gpu) == len(util):
        leerlauf = [g for g, u in zip(gpu, util) if u < 1.0]
        if leerlauf:
            anteil = len(leerlauf) / len(gpu) * 100
            print(f"\nGPU zu {anteil:.1f} % der Zeit unbeschäftigt (util < 1 %), "
                  f"dabei {statistics.fmean(leerlauf):.1f} W im Mittel")
            print(f"  Bereitschaftskosten: {statistics.fmean(leerlauf) * 8760 / 1000:.0f} kWh/Jahr, "
                  "wenn das so bleibt.")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    unter = p.add_subparsers(dest="befehl")

    rec = unter.add_parser("record", help="Dauerbetrieb, schreibt CSV je Tag")
    rec.add_argument("--intervall", type=int, default=10, help="Sekunden je CSV-Zeile (Default 10)")
    rec.add_argument("--takt", type=float, default=1.0, help="Sekunden je Probe (Default 1.0)")

    rep = unter.add_parser("report", help="Auswertung des Bestands")
    rep.add_argument("--tage", type=int, default=None, help="nur die letzten N Tage")
    rep.add_argument("--preis", type=float, default=None, help="€/kWh für die Hochrechnung")

    args = p.parse_args(argv)
    if args.befehl == "record":
        return record(args.intervall, args.takt)
    if args.befehl == "report":
        return report(args.tage, args.preis)
    p.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
