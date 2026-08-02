"""Pegel-Gate fürs Wakeword — die gemeinsam genutzte Rechnung.

Warum eine eigene Datei: das Pegel-Gate lebt live in ``assistant.py`` und
offline im Replay ``tools/wake_rms_replay.py``. Damit beide DASSELBE messen,
steht die Fenster-Rechnung hier genau einmal und wird von beiden importiert.
Eine zweite Implementierung würde unausweichlich driften — und dann misst das
Replay etwas anderes als das Gate tut, ohne dass es jemand bemerkt.

Siehe ``tools/wake_rms_replay.py`` für die Messreihe, die diesen Wert begründet,
und ``WAKEWORD_PROCESS.md`` für die Regel, dass Änderungen am Gate nur gegen
das Replay gemacht werden.
"""

from __future__ import annotations

import numpy as np


def loudest_window_rms(
    samples: np.ndarray, rate: int = 16000, window_ms: int = 300
) -> float:
    """RMS des lautesten ``window_ms``-Fensters im Audio.

    Sample-genau via kumulierter Quadrate: findet das energiereichste
    zusammenhängende Fenster beliebiger Lage, nicht nur an Chunk-Grenzen. Das
    ist bewusst unabhängig von der Chunk-Granularität der Quelle (ALSA-16k=
    80 ms, ALSA-48k-resample ≈ 27 ms, ReSpeaker = 40 ms) — das live aus dem
    ``wake_ring`` zusammengesetzte Audio ist sowieso ein fortlaufender
    Sample-Strom, und das Replay greift auf dieselben 16-kHz-WAVs zu.

    ``window_ms`` ist Vorgabe aus der Messung: 300 ms decken das Wakewort
    selbst („Gas-ton" ≈ 250 ms) ein, ohne schon die folgende Stille
    mitzurechnen, die den Pegel ziehen würde.

    Bekannte Schwäche (ehrlich, siehe Replay-Docstring): absolute RMS-Werte
    sind gain-abhängig. Der ReSpeaker verstärkt ×4; ändert sich Hardware oder
    Gain, verschiebt sich die ganze Skala und die Schwelle stimmt nicht mehr.
    Woran man das merkt: steigt der Anteil geblockter echter Rufe im
    Near-Miss-Log (``failed_on: min_rms``), ist die Schwelle zu hoch für die
    aktuelle Verstärkung.
    """
    n = len(samples)
    win = int(rate * window_ms / 1000)
    if n == 0:
        return 0.0
    f = samples.astype(np.float64)
    if n < win:
        # Kürzer als das Fenster: RMS über alles. Studio-Takes sind teils nur
        # 1,25 s lang — da greift dieser Ast, und er ist in Replay und Live
        # identisch, weil die Funktion hier steht.
        return float(np.sqrt(np.mean(f * f)))
    c = np.concatenate(([0.0], np.cumsum(f * f)))
    sums = c[win:] - c[: n - win + 1]
    return float(np.sqrt(float(sums.max()) / win))
