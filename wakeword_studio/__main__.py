"""Entry point: `python -m wakeword_studio` (venv-Re-Exec wie voice_assistant)."""

import os
import sys

_PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_VENV_PY = os.path.join(_PROJECT_DIR, "ow-venv", "bin", "python")

if sys.executable != _VENV_PY and os.path.exists(_VENV_PY):
    os.execv(_VENV_PY, [_VENV_PY, "-u", "-m", "wakeword_studio"] + sys.argv[1:])


def main() -> int:
    import argparse
    import logging

    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    parser = argparse.ArgumentParser(
        prog="python -m wakeword_studio",
        description="Wakeword-Studio: eigene Wakeword-Bundles aufnehmen und prüfen",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    rec = sub.add_parser(
        "record",
        help="Phase A: geführte echte Wakeword-Aufnahmen (Test-Set + Verifier-Basis)",
    )
    rec.add_argument("--bundle", default="gaston", help="Bundle unter models/wakewords/ (Default: gaston)")
    rec.add_argument("--speaker", required=True, help="Name der aufnehmenden Person (z.B. jochen)")
    rec.add_argument("--takes", type=int, default=12, help="Anzahl Aufnahmen (Default: 12)")
    rec.add_argument(
        "--keep-service",
        action="store_true",
        help="Assistant-Service nicht stoppen (nur sinnvoll im local-Modus)",
    )
    rec.add_argument(
        "--no-play",
        action="store_true",
        help="Takes nach der Aufnahme nicht automatisch vorspielen",
    )
    rec.add_argument(
        "--min-rms",
        type=float,
        default=None,
        help="RMS-Sprach-Schwelle fest vorgeben (Default: 2.5× gemessener Raum-Grundpegel)",
    )

    sco = sub.add_parser("score", help="WAV-Samples gegen das Bundle-Modell scoren (Live-Trigger-Semantik)")
    sco.add_argument("--bundle", default="gaston", help="Bundle unter models/wakewords/ (Default: gaston)")
    sco.add_argument("--threshold", type=float, default=None, help="Threshold-Override (Default: manifest.yaml)")
    sco.add_argument("paths", nargs="*", help="WAV-Dateien/Verzeichnisse (Default: samples/ des Bundles)")

    args = parser.parse_args()

    from wakeword_studio.recorder import run_record, run_score

    if args.cmd == "record":
        return run_record(args)
    return run_score(args)


if __name__ == "__main__":
    sys.exit(main())
