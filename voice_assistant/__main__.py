"""Entry point: `python -m voice_assistant`.

Sorgt zuerst für das venv-Re-Exec (wenn nicht schon drin),
lädt dann den Assistant.
"""

import os
import sys

_PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_VENV_PY = os.path.join(_PROJECT_DIR, "ow-venv", "bin", "python")

if sys.executable != _VENV_PY and os.path.exists(_VENV_PY):
    os.execv(_VENV_PY, [_VENV_PY, "-m", "voice_assistant"] + sys.argv[1:])


def main() -> None:
    from voice_assistant.assistant import run

    run()


if __name__ == "__main__":
    main()
