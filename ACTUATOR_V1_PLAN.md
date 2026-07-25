# Voice-Aktuator v1 — Geschichte & Bauplan

*Stand 2026-07-25. Dieses Dokument ist der Einstieg für die neue Session, die den
schnellen lokalen Schalt-Aktuator in den Voice-Assistant einbaut. Ergänzt
`CLAUDE.md` (Package-Struktur). Der globale CLAUDE.md-Hinweis gilt: relevante
MemPalace-Wings/Rooms durchsuchen — hier v.a. Wing `clawdpi1-home-pi-openclaw-voice-assist`
(room `technical`/`decisions`) und `noderedpi4-home-pi/technical`.*

---

## 1. Die Geschichte (warum wir hier sind)

- **Umzug** clawdpi1 (Raspberry Pi) → **gastonllm** (eigener GPU-Host, RTX 3060 Ti
  8 GB, IP <voice-host>) am 2026-07-24. STT dabei von `small` auf
  **`faster-whisper-medium`** (int8) gehoben — Grund für „small" war nur die VRAM-Enge
  auf dem alten geteilten Server, hier nicht mehr.
- **Vision:** Ein **schneller lokaler Schalt-Aktuator** (kleines LLM) fängt Kommandos
  wie „Schalte das Küchenlicht ein" ab und schaltet **sofort**, statt den langsamen
  Remote-Brain zu bemühen. Muster: **„schneller Aktuator + langsamer Überwacher"** —
  der große Brain bekommt dasselbe (über STT) und kontrolliert nach.
- **Empirische Entscheidungen** („messen, nicht raten"):
  - **Path B** (STT → kleines Text-LLM → JSON-Intent) statt „Audio-direkt". Gemessen:
    STT ~330 ms + Intent ~300–450 ms = **~600–800 ms end-to-end**.
  - Modell: **Gemma-4-E2B (Q4)** über **llama.cpp CUDA-Container** (nicht Gemma 3n;
    llama.cpp-Audio ist bleeding-edge, brauchen wir für Path B nicht).
  - **`enable_thinking:false`** (Gemma 4 ist ein Reasoning-Modell → Denken = langsam
    UND falsch) + **`response_format json_schema`** (handgeschriebenes GBNF scheiterte
    am Parser). Schema erzwingt nur die **Form**; für die **Bedeutung** braucht es ein
    **digest-getriebenes Prompt** (Ziel-Liste + Aliase + Few-Shot).
- **Node-RED auf noderedpi4 = einziger Ausführungspunkt UND Vokabular-Quelle.** Der
  Aktuator ruft nur einen Endpunkt; Node-RED entfaltet Gruppen/Szenen/Routinen und
  validiert inhaltlich. Endpunkte gebaut & gemergt (MR #14, #15).
- **Design-Prinzipien** (alle im MemPalace ausführlich):
  1. **Sicherheit liegt im Harness**, nicht im Modell (Grammatik-Zwang + Node-RED-Validierung).
  2. **Ein Ziel pro Intent** (Variante A); Node-RED entfaltet Gruppen/Szenen/Routinen.
     Das LLM benennt nur ein Ziel — kognitive Last wächst nicht mit Automations-Komplexität.
  3. **Aktionskosten × Konfidenz-Gate**: `kosten=hoch` ohne Konfidenz → Rückfrage.
  4. **Überwacher ist ein separater Konsument**; **hauswacht bleibt unverändert**
     (Monitor-Wesen). Register/Weltmodell wird geteilt, nicht dupliziert.
- **Auto-Discovery bewiesen:** `/capabilities` ändert sich → Generator neu laufen lassen
  → neue Ziele sofort sprachsteuerbar (validiert beim Sprung 60 → 62 Ziele, 5/5).

## 2. Was schon existiert

**noderedpi4 (Ausführungsseite, live & gemergt):**
- `GET  http://<hausautomation>:1880/voiceact/capabilities` — 62 Ziele, aktuell Version `e3d6af78`.
- `POST http://<hausautomation>:1880/voiceact/intent` — Validierung, Gate, Handshake, Dedup, `gesprochen`, MQTT-Echo.
- `GET  http://<hausautomation>:1880/voiceact/registry` — 78 Geräte (für den späteren Überwacher).
- Auth: Header **`X-Actuator-Token`**, Token in **`/home/jochen/openclaw_voice_assist/voiceact-token.txt`** (gitignored).
- MQTT-Broker `<hausautomation>:1883`: `voiceact/capabilities_changed` (retained `{version}`),
  `voiceact/executed` (Echo = kompletter Request verbatim + status + timestamp),
  `voiceact/registry_changed`.

**gastonllm (Sprachseite, in Isolation validiert — NOCH NICHT im Loop):**
- STT medium ✓. Gemma-4-E2B-Container ✓ (koexistiert mit STT: ~5,3 GB / 8 GB, ~2,5 GB frei).
- **Prototyp in `actuator_prototype/`** (aus dem Scratchpad gerettet):
  - `capabilities_to_schema.py` — zieht `/capabilities` → `intent_schema.json` +
    `request_template.json` + `capabilities_digest.json` + `version.txt`. Aktionen-getrieben.
  - `test_grammar.py`, `test_new.py` — die Validierungsläufe (10/10, dann 5/5 auf 62 Ziele).
  - Die `.json` sind ein Snapshot zu `e3d6af78` (regenerierbar).

## 3. Was v1 ist (Bauplan) — ENTSCHEIDUNG: v1, sequenziell, Handshake drin

1. **Aktuator-Modul** `voice_assistant/services/actuator.py`:
   STT-Text → Gemma-Intent (Schema+Prompt aus `/capabilities`) → `POST /intent`
   → liefert `{status, gesprochen}`. Lädt/regeneriert Schema+Digest beim Start und
   **lauscht auf `voiceact/capabilities_changed`** (regeneriert dann). Token aus der Datei.
2. **In die State-Machine** (`assistant.py`) einhängen: nach STT den Aktuator fragen.
   - `ist_kommando:false` → **nicht** POSTen, weiter zum Brain wie heute.
   - `ist_kommando:true` → `POST /intent`, `gesprochen` vorlesen, **Brain überspringen**.
3. **Handshake** bei `status:"zurueckgestellt"` (z. B. „alle Rollos", „Nachtruhe"):
   `gesprochen` (= Rückfrage) sprechen → ja/nein hören → Re-POST **gleiche `request_id`
   + `bestaetigt:true`**. (In v1 drin, weil sonst hohe-Kosten-Ziele ins Leere laufen.)
4. **Gemma als Dienst** — beim Go-Live in `compose.gastonllm.yml`
   (Repo `openclaw-voice-stack`) statt handgestartetem Wegwerf-Container.
5. **Live-Test am ReSpeaker.**

**Getroffene Entscheidungen:**
- **Sequenziell** (Aktuator zuerst; ~400 ms Steuer-Latenz vor dem Brain bei Nicht-Kommandos). Nicht parallel (sonst liefe der Brain jedes Mal mit).
- **Handshake in v1.**
- **Kein `konfidenz` mitschicken** (sicherer Default: `kosten=hoch` fragt dann immer nach).
- Erst **Code auf einem Branch** gegen den *jetzigen* (manuellen) Container; Gemma erst
  produktiv machen, wenn der Code steht.

## 4. Technische Referenz fürs Bauen

**llama-Container (aktuell manuell, Port 8090):**
```
podman run --rm -d --name llamacpp-gemma \
  --device nvidia.com/gpu=0 --security-opt label=disable -p 8090:8090 \
  -v /home/jochen/.cache/huggingface:/root/.cache/huggingface \
  ghcr.io/ggml-org/llama.cpp:server-cuda \
  -hf unsloth/gemma-4-E2B-it-GGUF:Q4_K_M -ngl 99 --host 0.0.0.0 --port 8090 --jinja -c 4096
```
**Intent-Request** an `http://localhost:8090/v1/chat/completions`: `request_template.json`
(= `response_format json_schema(strict)` + `chat_template_kwargs{enable_thinking:false}`
+ `temperature:0` + `max_tokens:200`) plus `messages:[system(=digest-Prompt), user(=STT-Text)]`.
Das Prompt entsteht aus dem Digest: Verb-Regeln + Ziel-Liste (`id: namen  (aktionen)  [range]`)
+ 3–4 Few-Shot-Beispiele. Bauweise siehe `test_grammar.py`.

**`/intent`-Body:** `{ist_kommando, ziel, aktion, wert, einheit, quelle:"aktuator",
request_id:<uuid>, konfidenz?, sprecher?, bestaetigt?}`
**`/intent`-Response:** `{status: ausgefuehrt|abgelehnt|zurueckgestellt|unbekanntes_ziel,
request_id, ausgefuehrt{ziel,aktion,wert,einheit}, grund, gesprochen}`
**Client-Verhalten:** fachliche Ergebnisse immer HTTP 200 + `status`; 401/400/500 nur echte
Transport-/Envelope-Fehler. Timeout ~1,5 s, **max. 1 Retry** nur bei Transportfehler
(idempotent via `request_id`, TTL 60 s Node-RED-seitig).

**Service:** `systemctl --user restart openclaw-voice-assist.service`; Logs IMMER
`journalctl _SYSTEMD_USER_UNIT=openclaw-voice-assist.service` (nie `--user`).
Projekt-venv: `ow-venv/bin/python`. Aktives Profil: `gastonllm` in `config.yaml`.

## 5. Gelernte Fallstricke

- Handgeschriebenes GBNF scheitert am llama.cpp-Parser → **`json_schema`** nehmen.
- Gemma 4 = Reasoning-Modell → **`enable_thinking:false` Pflicht** (sonst leeres `content`/langsam).
- Schema = nur Form; **Few-Shot nötig** für korrekte Semantik.
- **Deutsche Zahlwörter fragil** in STT („23" → „3 und 20"). Node-RED plausibilisiert
  (min/max fängt „3 statt 23"). Echte Normalisierung STT-seitig = **Punkt 3, NICHT v1**.
- **`sensibel` (Registry/Überwacher) ≠ `kosten`-Gate (Aktuator).** `regenwasser_weiche`
  und `fussbodenheizung_badoben` sind `sensibel:true`, aber `kosten` mittel/niedrig →
  der Fast-Path schaltet sie **ohne** Rückfrage (bewusst von Jochen so gesetzt); Absicherung
  ist Sache des Überwachers.

## 6. Nach v1 (nicht jetzt)

- **Überwacher-Startpaket:** `/registry` ⊕ meine Alexa-Liste (Descriptions/Capabilities)
  ⊕ hauswachts IEEE-verankerte Quirks (Bürorollo lügt über Position, Kugellicht=Status,
  Garage=PIN, …). Ankerung auf **Node-RED `unique_id`/IEEE**, nicht HA-entity_id.
- Zahl-Normalisierung STT (Punkt 3). unique_id-Nachrüstung (noderedpi4, eigenes Projekt).
- Offen bei Jochen: Garage-Entscheidung, erster physischer `regenwasser_weiche`- und
  `alle_rollos`-Sprachtest.
