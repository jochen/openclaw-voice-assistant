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
const SPEAK_BASE = "http://127.0.0.1:18792";

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
  name: "Voice Enrolment & Speak",
  description:
    "Tools zum Speichern, Auflisten und Löschen von Stimm-Referenzen sowie zum Vorlesen von Text über den lokalen Lautsprecher.",
  register(api) {
    api.registerTool({
      name: "voice_speak_text",
      label: "Text vorlesen",
      description:
        "Spricht einen Text über den Lautsprecher des Voice Assistants aus. " +
        "Der Text wird in eine Warteschlange eingereiht und sobald der Voice Assistant " +
        "nicht gerade aufnimmt oder antwortet (d.h. im Bereit-Zustand ist) vorgelesen. " +
        "Verwenden wenn OpenClaw aufgefordert wird, etwas anzusagen, eine Warnung auszusprechen " +
        "oder eine Lautsprecherausgabe zu machen. " +
        "Kein Markdown, keine Listen — reiner gesprochener Text.",
      parameters: {
        type: "object",
        additionalProperties: false,
        required: ["text"],
        properties: {
          text: {
            type: "string",
            description: "Der vorzulesende Text. Maximal 500 Zeichen, kein Markdown.",
          },
        },
      },
      async execute(_toolCallId, params) {
        const text = String(params?.text ?? "").trim();
        if (!text) {
          return textResult("Fehler: Kein Text angegeben.", { ok: false });
        }
        if (text.length > 500) {
          return textResult("Fehler: Text zu lang (max. 500 Zeichen).", { ok: false });
        }
        try {
          const res = await fetch(`${SPEAK_BASE}/speak`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ text }),
          });
          const data = await res.json().catch(() => ({}));
          if (!res.ok) {
            throw new Error(data?.error ?? `HTTP ${res.status}`);
          }
          return textResult(
            `Text wurde zur Sprachausgabe eingereiht (${text.length} Zeichen).`,
            { ok: true, queued: true }
          );
        } catch (err) {
          return textResult(`Lautsprecherausgabe fehlgeschlagen: ${err.message}`, { ok: false });
        }
      },
    });

    api.registerTool({
      name: "voice_enroll_speaker",
      label: "Stimme anlernen",
      description:
        "Speichert die zuletzt aufgenommene Stimme als Stimm-Referenz für die Person <name>. " +
        "WICHTIG: Rufe dieses Tool NICHT sofort auf wenn der Nutzer Stimm-Lernen anfragt. " +
        "Bitte ihn stattdessen zuerst, folgenden Trainingssatz laut vorzulesen — " +
        "erst im nächsten Follow-up-Turn (wenn er den Satz gesprochen hat) wird das Tool aufgerufen. " +
        "So wird die Trainingssatz-Aufnahme enrolled, nicht der kurze Trigger-Satz. " +
        "Trainingssatz (laut vorlesen lassen, dann Tool aufrufen): " +
        "'Ich bin [Name] — bitte lerne jetzt meine Stimme. " +
        "Über die grünen Felder und durch die tiefen Wälder reite ich gerne. " +
        "Die süßen Äpfel und die reifen Birnen schmecken köstlich. " +
        "Heute früh schien die Sonne, jetzt zieht Regen auf.' " +
        "Dieser Satz deckt alle deutschen Vokale, Umlaute, Diphthonge und typische Konsonanten ab " +
        "und liefert ein deutlich besseres Stimm-Embedding als kurze Alltagssätze.",
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
