"""Score-Gate fürs Wakeword — die gemeinsam genutzte Entscheidung.

Warum eine eigene Datei: dieselbe Regel entscheidet an zwei Stellen, ob ein
beendeter Score-Streak triggern darf — im Leerlauf (``assistant.py``,
STATE_LISTENING) und während der Assistent selbst spricht (``bargein.py``,
der Abbruch "Stopp Gaston"). Zwei Kopien würden auseinanderlaufen, und dann
wäre der Abbruch schwerer oder leichter auszulösen als der Ruf selbst, ohne
dass es jemand bemerkt. Gleiche Überlegung wie bei ``wake_rms.py`` fürs
Pegel-Gate.

Die Messreihen, die die Schwellen begründen, stehen in den Docstrings unten
und in ``WAKEWORD_PROCESS.md``.
"""

from __future__ import annotations


def gate_passed(
    wake_hits: int,
    wake_peak: float,
    min_hits: int,
    min_peak: float,
    min_peak_short: float,
    min_peak_single: float,
) -> bool:
    """Entscheidet, ob ein beendeter Streak triggern darf.

    Zwei Wege, absichtlich als ODER: der bisherige Streak-Weg (min_hits Frames
    plus gestufte Peak-Anforderung) und — falls konfiguriert — ein einzelner
    sehr hoher Frame. Der zweite ist ADDITIV; min_hits bleibt unangetastet,
    damit die Gap-Toleranz im Streak-Zähler unverändert erst ab min_hits
    greift (sonst würde sie zwei unabhängige Rausch-Spitzen zusammennähen).

    Der 1-Frame-Weg existiert, weil 5 der 6 nachweislich echten, verlorenen
    Rufe vom 2026-07-26 als EINZELNE Spitze ankamen (Nachbar-Frames bei 0.01)
    und damit an min_hits scheiterten, nicht am Peak. Siehe
    WakewordHit.min_peak_single für die Datenbasis und tools/wake_triage.py
    für das Verfahren, mit dem echte Rufe von Rauschen getrennt wurden.
    """
    if wake_hits >= min_hits and wake_peak >= required_peak(
        wake_hits, min_peak, min_peak_short
    ):
        return True
    return wake_hits == 1 and min_peak_single > 0.0 and wake_peak >= min_peak_single


def required_peak(
    wake_hits: int, min_peak: float, min_peak_short: float, min_peak_single: float = 0.0
) -> float:
    """Peak-Anforderung abhängig von der Streak-Länge.

    Kurz-Streaks (< 3 Frames, also der min_hits-2-Pfad) müssen min_peak_short
    erreichen, längere Streaks min_peak. Datenbasis Live-Logs 2026-07-08..13:
    alle vier 2-Frame-Trigger waren False Positives (Peaks 0.70/0.77/0.83/0.92),
    der einzige echte 2-Frame-Ruf peakte 0.93 — ab 3 Frames tragen die
    zusätzlichen Hits die Evidenz, dort bleibt min_peak ausreichend.
    """
    if wake_hits == 1 and min_peak_single > 0.0:
        return min_peak_single
    return min_peak_short if wake_hits < 3 else min_peak
