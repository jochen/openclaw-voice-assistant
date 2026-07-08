#!/bin/bash
# Schaltet den Voice-Assist zwischen dem ai-stack-Server (126) und der
# Testinstanz auf rouven (229) um: speaches (STT/TTS, Port 8000) und
# voice-analysis (Mood/SER, Port 8001). ser (8002) wird von voice-analysis
# intern angesprochen, dafür ist kein separater Eintrag nötig.
set -euo pipefail

CONFIG_YAML="$(dirname "$0")/config.yaml"
CONFIG_PY="$(dirname "$0")/voice_assistant/config.py"

case "${1:-}" in
  126)
    IP="<speaches-host>"
    ;;
  rouven)
    IP="<test-host>"
    ;;
  *)
    echo "Usage: $0 {126|rouven}" >&2
    echo "  126    = ai-stack Fablab-Server (<speaches-host>, Original)" >&2
    echo "  rouven = Testinstanz (<test-host>)" >&2
    exit 1
    ;;
esac

sed -i -E "s#(speaches_base: \")http://[0-9.]+(:8000\")#\1http://${IP}\2#g" "$CONFIG_YAML"
sed -i -E "s#(VOICE_ANALYSIS_BASE = \")http://[0-9.]+(:8001\")#\1http://${IP}\2#" "$CONFIG_PY"

echo "Umgeschaltet auf ${1} (${IP}):"
grep -n "speaches_base" "$CONFIG_YAML"
grep -n "VOICE_ANALYSIS_BASE" "$CONFIG_PY"

systemctl --user restart openclaw-voice-assist.service
sleep 2
systemctl --user is-active openclaw-voice-assist.service
