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
      name: "voice_list_voices",
      label: "TTS-Stimmen auflisten",
      description:
        "Listet alle verfügbaren TTS-Stimmen und die aktuell aktive. " +
        "Vorher aufrufen, um zu wissen welche Stimmen es gibt, bevor du mit voice_set_voice wechselst.",
      parameters: { type: "object", additionalProperties: false, properties: {} },
      async execute() {
        let data;
        try {
          const res = await fetch(`${SPEAK_BASE}/voices`);
          const text = await res.text();
          try {
            data = text ? JSON.parse(text) : null;
          } catch {
            data = { raw: text };
          }
          if (!res.ok) {
            const msg = data?.error ?? `HTTP ${res.status}`;
            return textResult(`Konnte Stimmen nicht abrufen: ${msg}`, { ok: false });
          }
        } catch (err) {
          return textResult(`Voice-Dienst nicht erreichbar: ${err.message}`, { ok: false });
        }

        const available = data?.available ?? [];
        const loaded = data?.loaded ?? [];
        const active = data?.active ?? {};
        const lastSpeaker = data?.last_speaker ?? null;

        const lines = [];
        if (available.length) {
          lines.push("Verfügbare Stimmen:");
          for (const v of available) {
            lines.push(`- ${v.voice} (Modell ${v.model}, Sprache ${v.language})`);
          }
        } else {
          lines.push("Keine Stimmen verfügbar.");
        }
        if (active?.voice) {
          lines.push(
            `Aktuell aktiv: ${active.voice} (Modell ${active.model}, Tempo ${active.speed ?? 1.0}).`
          );
        }
        if (lastSpeaker) {
          lines.push(`Zuletzt erkannter Sprecher: ${lastSpeaker}.`);
        }

        return textResult(lines.join("\n"), { ok: true, details: data });
      },
    });

    api.registerTool({
      name: "voice_set_voice",
      label: "TTS-Stimme wechseln",
      description:
        "Wechselt die Stimme, mit der der Voice-Assistant spricht (z.B. wenn der Nutzer eine andere/weibliche/lustige Stimme will). " +
        "voice ist der Stimmenname aus voice_list_voices; model optional für exakte Wahl. " +
        "speed optional (1.0 normal, >1 schneller). " +
        "for_speaker: wenn gesetzt, wird diese Stimme dauerhaft für diesen Sprecher gemerkt und künftig automatisch verwendet — nutze dafür den erkannten Sprechernamen.",
      parameters: {
        type: "object",
        additionalProperties: false,
        required: ["voice"],
        properties: {
          voice: { type: "string", description: "Stimmenname aus voice_list_voices (z.B. 'kerstin')." },
          model: { type: "string", description: "Optional: exakte Modell-ID, falls eine Stimme in mehreren Modellen existiert." },
          speed: { type: "number", description: "Optional: Sprechtempo (1.0 normal, >1 schneller, <1 langsamer)." },
          for_speaker: { type: "string", description: "Optional: erkannter Sprechername, für den diese Stimme dauerhaft gemerkt werden soll." },
        },
      },
      async execute(_toolCallId, params) {
        const voice = String(params?.voice ?? "").trim();
        if (!voice) {
          return textResult("Fehler: Keine Stimme angegeben.", { ok: false });
        }
        const payload = { voice };
        if (params?.model) payload.model = String(params.model).trim();
        if (params?.speed != null) payload.speed = Number(params.speed);
        if (params?.for_speaker) payload.for_speaker = String(params.for_speaker).trim();

        let data;
        try {
          const res = await fetch(`${SPEAK_BASE}/voice/set`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload),
          });
          const text = await res.text();
          try {
            data = text ? JSON.parse(text) : null;
          } catch {
            data = { raw: text };
          }
          if (!res.ok) {
            const msg = data?.error ?? `HTTP ${res.status}`;
            return textResult(`Konnte Stimme nicht wechseln: ${msg}`, { ok: false });
          }
        } catch (err) {
          return textResult(`Voice-Dienst nicht erreichbar: ${err.message}`, { ok: false });
        }

        const active = data?.active ?? {};
        const speedTxt = active.speed != null && active.speed !== 1.0 ? ` mit Tempo ${active.speed}` : "";
        const remembered = payload.for_speaker
          ? ` Ich merke mir das für ${payload.for_speaker}.`
          : "";
        return textResult(
          `Alles klar, ich spreche jetzt mit der Stimme ${active.voice ?? voice}${speedTxt}.${remembered}`,
          { ok: true, details: data }
        );
      },
    });

    api.registerTool({
      name: "voice_set_speed",
      label: "Sprechtempo ändern",
      description:
        "Ändert nur das Sprechtempo (1.0 normal, 1.3 zügiger, 0.8 langsamer).",
      parameters: {
        type: "object",
        additionalProperties: false,
        required: ["speed"],
        properties: {
          speed: { type: "number", description: "Sprechtempo (1.0 normal, 1.3 zügiger, 0.8 langsamer)." },
        },
      },
      async execute(_toolCallId, params) {
        const speed = Number(params?.speed);
        if (!Number.isFinite(speed)) {
          return textResult("Fehler: Ungültiges Tempo.", { ok: false });
        }
        let data;
        try {
          const res = await fetch(`${SPEAK_BASE}/voice/speed`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ speed }),
          });
          const text = await res.text();
          try {
            data = text ? JSON.parse(text) : null;
          } catch {
            data = { raw: text };
          }
          if (!res.ok) {
            const msg = data?.error ?? `HTTP ${res.status}`;
            return textResult(`Konnte Tempo nicht ändern: ${msg}`, { ok: false });
          }
        } catch (err) {
          return textResult(`Voice-Dienst nicht erreichbar: ${err.message}`, { ok: false });
        }
        const active = data?.active ?? {};
        return textResult(`Sprechtempo ist jetzt ${active.speed ?? speed}.`, { ok: true, details: data });
      },
    });

    api.registerTool({
      name: "voice_analyze_last_output",
      label: "Letzte Sprachausgabe analysieren",
      description:
        "Analysiert die zuletzt vom Voice-Assistant gesprochene Antwort akustisch: " +
        "Texttreue (kam der Text sauber rüber — WER/CER), Sprechtempo und Pausen, " +
        "sowie Tonfall/Emotion der eigenen Ausgabe (arousal/valence/dominance via Speech Emotion Recognition). " +
        "Verwenden, wenn der Nutzer fragt wie etwas klang ('klang das holprig?', 'war das gelangweilt?', " +
        "'wie hast du das gesagt?'), oder wenn OpenClaw selbst prüfen will, wie seine letzte Ausgabe ankam. " +
        "Analysiert NUR die zuletzt gesprochene Antwort — nicht die aktuelle Eingabe.",
      parameters: { type: "object", additionalProperties: false, properties: {} },
      async execute() {
        let data;
        try {
          const res = await fetch(`${SPEAK_BASE}/analyze-last`);
          const text = await res.text();
          try {
            data = text ? JSON.parse(text) : null;
          } catch {
            data = { raw: text };
          }
          if (!res.ok) {
            const msg = data?.error ?? `HTTP ${res.status}`;
            return textResult(`Keine analysierbare Aufnahme vorhanden: ${msg}`, { ok: false });
          }
        } catch (err) {
          return textResult(`Analyse-Dienst nicht erreichbar: ${err.message}`, { ok: false });
        }

        // Kompakter Report für das LLM
        const intended = data.intended ?? "";
        const observed = data.observed ?? "";
        const fidelity = data.text_fidelity ?? {};
        const timing = data.timing ?? {};
        const ser = data.ser ?? data.mood_proxy ?? {};

        const lines = [];

        // Texttreue
        if (intended && observed && intended !== observed) {
          lines.push(`Beabsichtigt: "${intended}"`);
          lines.push(`Erkannt (Transkript): "${observed}"`);
        } else if (observed) {
          lines.push(`Transkript: "${observed}"`);
        }
        if (fidelity.wer != null) {
          const match = fidelity.match ? "✓ sauber" : "✗ Abweichung";
          lines.push(`Texttreue: WER ${fidelity.wer}%, CER ${fidelity.cer ?? "?"}% — ${match}`);
        }

        // Tempo & Pausen
        if (timing.words_per_sec != null) {
          const wps = timing.words_per_sec.toFixed(2);
          const dur = timing.duration_s != null ? `, Dauer ${timing.duration_s.toFixed(1)} s` : "";
          const pauseCount = (timing.pauses ?? []).length;
          const pauseTotal = timing.pause_total_s != null ? `, Pausenzeit ${timing.pause_total_s.toFixed(1)} s` : "";
          lines.push(`Tempo: ${wps} Wörter/s${dur}${pauseCount > 0 ? `, ${pauseCount} Pausen${pauseTotal}` : ""}`);
        }

        // Tonfall / SER
        const serLabel = ser.label ?? ser.hint ?? "";
        if (ser.arousal != null) {
          lines.push(
            `Tonfall: ${serLabel} | arousal=${ser.arousal.toFixed(2)}, valence=${(ser.valence ?? 0).toFixed(2)}, dominance=${(ser.dominance ?? 0).toFixed(2)}`
          );
        } else if (serLabel) {
          lines.push(`Tonfall: ${serLabel}`);
        }

        const summary = lines.join("\n");
        return textResult(summary || "Analyse abgeschlossen (keine Details verfügbar).", {
          ok: true,
          details: data,
        });
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
