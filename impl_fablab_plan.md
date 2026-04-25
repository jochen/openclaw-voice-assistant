# Fablab Voice Assistant — Setup-Protokoll

## Ziel

Den OpenClaw Voice Assistant auf dem Fablab-Pi (`openclawpi`, 192.168.111.156)
aufsetzen und mit einem ReSpeaker XVF3800 + XIAO ESP32-S3 ausstatten.

---

## Erledigter Stand (2026-04-25)

| Was | Ergebnis |
|---|---|
| `git pull` auf Fablab-Pi | Kompletter aktueller Code-Stand (Package + Follow-up + i18n) |
| ow-venv Pakete | PyYAML, requests, aioesphomeapi nachinstalliert |
| `config.yaml` migriert | Profile `fablab` (local) + `fablab_rs` (respeaker), Hostname-Map `openclawpi → fablab` |
| Systemd-Service | `/etc/systemd/system/voice-assistant.service` auf `python -m voice_assistant` umgestellt, `daemon-reload` done |
| `esphome/secrets.yaml` | Auf Fablab-Pi angelegt mit `fablab_wifi_ssid: fablab`, `fablab_wifi_password: material`, `fablab_ota_password: respeaker` |
| `esphome/respeaker-fablab.yaml` | Im Repo (gepusht), device_name: `respeaker-fablab`, WiFi-Keys `fablab_*` |
| `esphome-venv` | Auf Fablab-Pi angelegt, ESPHome 2026.4 installiert |
| Altes `voice_assistant.py` im Homedir | Umbenannt zu `voice_assistant_legacy_homedir.py` |

---

## Noch offen — vor Ort zu erledigen

### 1. ALSA-Playback-Device prüfen

Der Logitech Speakerphone P710e ist in `config.yaml` als
`playback_device: "plughw:CARD=P710e,DEV=0"` eingetragen. Stimmt der
ALSA-Kartename auf dem Fablab-Pi?

```bash
aplay -L | grep -A1 P710e
```

Falls abweichend → `config.yaml` anpassen (nur `local_audio.playback_device`).

### 2. Test-Start im Vordergrund

```bash
ssh pi@192.168.111.156
cd /home/pi/openclaw_voice_assist
source ow-venv/bin/activate
python -m voice_assistant
```

Prüfen: Profil `fablab` gewählt, Speaches erreichbar, Wakeword-Modell lädt.

### 3. Service aktivieren (wenn Test ok)

```bash
sudo systemctl enable --now voice-assistant.service
sudo systemctl status voice-assistant.service
```

---

## ReSpeaker Flashen

### Voraussetzungen

- ReSpeaker per USB-C an den Fablab-Pi
- Fablab-Pi im WLAN (oder per Ethernet) — braucht Internet für ESPHome-Kompilierung
- Gerät erscheint als `/dev/ttyACM0` (ggf. `/dev/ttyACM1` prüfen)

### Erstes Flashen (USB)

```bash
ssh pi@192.168.111.156
cd /home/pi/openclaw_voice_assist
esphome-venv/bin/esphome run esphome/respeaker-fablab.yaml --device /dev/ttyACM0
```

ESPHome kompiliert, flasht via USB, startet den ESP. Danach ist der ESP im
Fablab-WLAN (SSID: `fablab`) erreichbar unter `respeaker-fablab.local`.

### OTA (alle weiteren Updates)

```bash
esphome-venv/bin/esphome run esphome/respeaker-fablab.yaml --device respeaker-fablab.local
```

### Config validieren (ohne Flashen)

```bash
esphome-venv/bin/esphome config esphome/respeaker-fablab.yaml
```

---

## Auf ReSpeaker-Modus umschalten

Sobald der ReSpeaker geflasht und im Netz ist:

1. `config.yaml` auf dem Fablab-Pi: Hostname-Map auf `fablab_rs` umstellen

```yaml
hostname_map:
  openclawpi: fablab_rs   # ← war: fablab
  fablab: fablab_rs
```

2. Neustart des Voice-Assistenten:

```bash
sudo systemctl restart voice-assistant.service
# oder im tmux: Ctrl+C, python -m voice_assistant neu starten
```

3. Prüfen: Log zeigt `Profile: fablab_rs (mode: respeaker)`, ESP-Verbindung
   aufgebaut, LED-Ring reagiert auf Boot-Sequenz.

---

## Profil-Übersicht Fablab-Pi

| Profil | Mode | Audio | Wakeword | LEDs |
|---|---|---|---|---|
| `fablab` | local | Logitech P710e (ALSA) | openwakeword auf Pi | WLED |
| `fablab_rs` | respeaker | ReSpeaker XVF3800 (ESP) | openwakeword auf Pi | RespeakerRing |

Credentials (OpenClaw-Token, Telegram) sind in beiden Profilen identisch.

---

## Netz-Infos

| Was | Wert |
|---|---|
| Fablab-Pi IP | 192.168.111.156 |
| Speaches (GPU) | http://192.168.111.126:8000 |
| ReSpeaker (nach Flash) | respeaker-fablab.local |
| Fablab-WiFi SSID | fablab |
| OpenClaw lokal | http://127.0.0.1:18789 |
