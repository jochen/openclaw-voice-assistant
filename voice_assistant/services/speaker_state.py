"""Wer zuletzt per Stimme gesprochen hat — als Datei, nicht als Prompt-Hinweis.

Der Sprecher stand bis 2026-09-19 ausschliesslich im Prompt (`[Sprecher: …]`).
Damit war er ein *Hinweis an ein Sprachmodell*, keine Durchsetzung: ob eine
folgenreiche Aktion bei unbekanntem Sprecher unterbleibt, entschied allein der
Text in AGENTS.md — und dessen Fortsetzungs-Ausnahme hat am 2026-09-18 im
Fablab genau das erlaubt, was sie verhindern sollte (Rechner ausschalten,
nachdem die Erkennung ausgefallen war).

Diese Datei ist die Gegenmassnahme: ein Werkzeug, das etwas Folgenreiches tut,
liest sie SELBST und entscheidet selbst. Der Vertrag steht in SPEAKER_STATE.md;
er ist bewusst so klein, dass ihn ein Shell-Skript mit `jq` auswerten kann.

Geschrieben wird nach JEDEM Voice-Turn, auch bei Ausfall — eine fehlende oder
alte Datei darf nie als "ist schon in Ordnung" durchgehen.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from datetime import datetime

from voice_assistant.config import CURRENT_SPEAKER_PATH, VOICE_DIR
from voice_assistant.services.diarization import SpeakerVerdict


def write_current_speaker(
    verdict: SpeakerVerdict,
    wakeword: str | None = None,
    path: str = CURRENT_SPEAKER_PATH,
) -> None:
    """Das Urteil des laufenden Turns festhalten (atomar, best effort).

    Atomar per tmp+rename, weil ein Leser genau im Moment des Schreibens
    zugreifen kann und eine halbe JSON-Datei als "kein bekannter Sprecher"
    gelesen wuerde — das waere ein stiller Ausfall in die falsche Richtung.

    Fehler werden geschluckt und gemeldet: ein kaputter Schreibvorgang darf den
    Sprach-Turn nicht abbrechen. Fuer den Leser ist eine veraltete Datei
    ohnehin kein Freibrief — er prueft das Alter selbst.
    """
    payload = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "epoch": int(time.time()),
        "status": verdict.status,
        "name": verdict.name,
        "label": verdict.label,
        "wakeword": wakeword,
    }
    try:
        os.makedirs(os.path.dirname(path) or VOICE_DIR, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or VOICE_DIR, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        except BaseException:
            # Kein verwaistes .tmp im Workspace zuruecklassen.
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except Exception as e:  # noqa: BLE001 — Turn laeuft weiter, egal was hier schiefgeht
        print(f"⚠️  current_speaker.json nicht geschrieben: {e}")
