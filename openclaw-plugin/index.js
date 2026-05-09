/**
 * OpenClaw Voice Enrolment Plugin
 *
 * Registriert drei Tools, die das LLM auf Sprachbefehl aufrufen kann:
 *   - voice_enroll_speaker(name)  ← bei "lerne meine Stimme, ich bin X"
 *   - voice_list_speakers()       ← Listet bekannte Sprecher
 *   - voice_remove_speaker(name)  ← Entfernt eine Stimm-Referenz
 *
 * Die Tools sprechen einen lokalen HTTP-Server an, den der voice_assistant
 * auf 127.0.0.1:18791 startet (siehe voice_assistant/services/enroll_server.py).
 * Der Server kopiert dabei die jeweils letzte Aufnahme (last_recording.wav)
 * in den Speaker-Workspace.
 */
// @ts-ignore - resolved by openclaw runtime
import { definePluginEntry } from "openclaw/plugin-sdk/plugin-entry";

const ENROLL_BASE = "http://127.0.0.1:18791";

/** Wirft bei nicht-OK-Status mit Server-Body als Message. */
async function callEnrollServer(method, path, body) {
  const init = { method, headers: {} };
  if (body !== undefined) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(body);
  }
  const res = await fetch(`${ENROLL_BASE}${path}`, init);
  const text = await res.text();
  let parsed;
  try {
    parsed = text ? JSON.parse(text) : null;
  } catch {
    parsed = { raw: text };
  }
  if (!res.ok) {
    const detail = parsed?.error ?? text ?? `HTTP ${res.status}`;
    throw new Error(`Enrolment-Server ${method} ${path}: ${detail}`);
  }
  return parsed;
}

function textResult(text, details = {}) {
  return { content: [{ type: "text", text }], details };
}

export default definePluginEntry({
  id: "voice-enrol",
  name: "Voice Enrolment",
  description:
    "Tools zum Speichern, Auflisten und Löschen von Stimm-Referenzen für den Voice Assistant.",
  register(api) {
    api.registerTool({
      name: "voice_enroll_speaker",
      label: "Stimme anlernen",
      description:
        "Speichert die zuletzt aufgenommene Stimme als Stimm-Referenz für die Person <name>. " +
        "Aufrufen, wenn der Nutzer eine Stimm-Lernen-Anfrage stellt wie 'lerne meine Stimme, ich bin Jochen' " +
        "oder 'merk dir, ich heisse Katrin'. Die letzte Aufnahme (= der Satz, mit dem der Nutzer das gerade " +
        "verlangt hat) wird als Referenz benutzt.",
      parameters: {
        type: "object",
        additionalProperties: false,
        required: ["name"],
        properties: {
          name: {
            type: "string",
            description: "Name der Person (Vorname genügt). Wird normalisiert (lower-case, ohne Sonderzeichen).",
          },
        },
      },
      async execute(_toolCallId, params) {
        const name = String(params?.name ?? "").trim();
        if (!name) {
          return textResult("Fehler: Kein Name angegeben.", { ok: false });
        }
        try {
          const res = await callEnrollServer("POST", "/enroll", { name });
          return textResult(`Stimme von ${res.name} gespeichert.`, {
            ok: true,
            name: res.name,
            saved: res.saved,
            original: res.original,
          });
        } catch (err) {
          return textResult(`Konnte Stimme nicht speichern: ${err.message}`, { ok: false });
        }
      },
    });

    api.registerTool({
      name: "voice_list_speakers",
      label: "Bekannte Stimmen auflisten",
      description: "Liefert die Liste aller bisher angelernten Stimm-Referenzen.",
      parameters: { type: "object", additionalProperties: false, properties: {} },
      async execute() {
        try {
          const res = await callEnrollServer("GET", "/speakers");
          const speakers = res?.speakers ?? [];
          const text = speakers.length
            ? `Bekannte Stimmen: ${speakers.join(", ")}.`
            : "Es sind noch keine Stimmen angelernt.";
          return textResult(text, { ok: true, speakers });
        } catch (err) {
          return textResult(`Konnte Stimmen nicht auflisten: ${err.message}`, { ok: false });
        }
      },
    });

    api.registerTool({
      name: "voice_remove_speaker",
      label: "Stimme löschen",
      description:
        "Entfernt eine Stimm-Referenz. Aufrufen, wenn der Nutzer sagt 'vergiss meine Stimme' " +
        "oder 'lösche die Stimme von <name>'.",
      parameters: {
        type: "object",
        additionalProperties: false,
        required: ["name"],
        properties: {
          name: { type: "string", description: "Name der zu löschenden Person." },
        },
      },
      async execute(_toolCallId, params) {
        const name = String(params?.name ?? "").trim();
        if (!name) {
          return textResult("Fehler: Kein Name angegeben.", { ok: false });
        }
        try {
          const res = await callEnrollServer("DELETE", `/speakers/${encodeURIComponent(name)}`);
          return textResult(`Stimme von ${res.removed} entfernt.`, { ok: true, removed: res.removed });
        } catch (err) {
          return textResult(`Konnte Stimme nicht entfernen: ${err.message}`, { ok: false });
        }
      },
    });
  },
});
