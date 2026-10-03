#!/usr/bin/env bash
# Eine GETESTETE ESPHome-Firmware aufheben — mit Zeitstempel, ausserhalb des Repos.
#
#   esphome/firmware_sichern.sh respeaker.yaml "Ansage, Barge-in, Echo-Test 0,07 ok"
#
# Regel (Jochen, 2026-10-02): Wenn eine Firmware erfolgreich getestet wurde,
# wird sie aufgehoben. Anlass: der erste Build mit ESPHome 2026.9 hat das
# Build-Verzeichnis neu erzeugt und dabei die einzige Kopie der laufenden
# April-Firmware geloescht — ab da gab es keinen Rueckweg mehr ausser einer
# USB-Sicherung vom Geraet selbst.
#
# Warum NICHT im Repo: eine gebaute Firmware enthaelt WLAN-Passwort,
# OTA-Passwort und API-Schluessel aus secrets.yaml. Das Repo ist oeffentlich.
#
# Gesichert wird der LETZTE BUILD dieser YAML, also das, was gerade geflasht
# und getestet wurde. Nach einem neuen compile ist es eine andere Firmware —
# dann erst testen, dann sichern.
#
# Ziel: ~/esphome-firmware/<device_name>/<JJJJ-MM-TT_HHMM>_esphome-<ver>_<git>/
#   firmware.factory.bin  — fuer USB (esptool write_flash 0x0 …)
#   firmware.ota.bin      — fuer OTA (esphome upload --file … bzw. Web-OTA)
#   <yaml>                — die Konfiguration genau dieses Builds
#   NOTIZ.txt             — was getestet wurde, ESPHome-Version, Git-Stand

set -euo pipefail

yaml="${1:?Aufruf: firmware_sichern.sh <yaml in esphome/> \"was getestet wurde\"}"
notiz="${2:?Bitte angeben, was getestet wurde — ungetestete Firmware wird nicht gesichert}"

hier="$(cd "$(dirname "$0")" && pwd)"
repo="$(cd "$hier/.." && pwd)"
yaml_pfad="$hier/$(basename "$yaml")"
[ -f "$yaml_pfad" ] || { echo "YAML nicht gefunden: $yaml_pfad"; exit 1; }

geraet="$(awk '/^  device_name:/ {print $2; exit}' "$yaml_pfad")"
[ -n "$geraet" ] || { echo "device_name nicht in den substitutions von $yaml_pfad"; exit 1; }

build="$hier/.esphome/build/$geraet/build"
for f in firmware.factory.bin firmware.ota.bin; do
  [ -f "$build/$f" ] || { echo "Kein Build gefunden: $build/$f"; exit 1; }
done

esphome_ver="$("$repo/esphome-venv/bin/esphome" version | awk '{print $2}')"
# Letzter Commit DIESER YAML, nicht HEAD: danach geaenderte Werkzeuge oder
# Doku aendern die Firmware nicht.
git_stand="$(git -C "$repo" log -1 --format=%h -- "$yaml_pfad")"
git -C "$repo" diff --quiet -- "$yaml_pfad" || git_stand="${git_stand}-geaendert"
stempel="$(date -r "$build/firmware.ota.bin" +%Y-%m-%d_%H%M)"

ziel="$HOME/esphome-firmware/$geraet/${stempel}_esphome-${esphome_ver}_${git_stand}"
if [ -e "$ziel" ]; then
  echo "Schon gesichert: $ziel"
  exit 0
fi
mkdir -p "$ziel"
cp -p "$build/firmware.factory.bin" "$build/firmware.ota.bin" "$ziel/"
cp -p "$yaml_pfad" "$ziel/"
{
  echo "Geraet:        $geraet"
  echo "Gebaut:        $(date -r "$build/firmware.ota.bin" '+%Y-%m-%d %H:%M:%S')"
  echo "Gesichert:     $(date '+%Y-%m-%d %H:%M:%S')"
  echo "ESPHome:       $esphome_ver"
  echo "Git-Stand:     $git_stand"
  echo "Getestet:      $notiz"
  echo
  echo "sha256:"
  (cd "$ziel" && sha256sum firmware.factory.bin firmware.ota.bin)
} > "$ziel/NOTIZ.txt"
chmod -R go-rwx "$HOME/esphome-firmware"
echo "Gesichert: $ziel"
