#!/usr/bin/env python3
"""Generiert aus GET /voiceact/capabilities das LLM-JSON-Schema (response_format)
fuer den Aktuator. AKTIONEN-GETRIEBEN: Vokabular (ziel/aktion/einheit) kommt aus
den Daten, nicht aus fest kodierten Typen. Node-RED bleibt die autoritative
inhaltliche Validierung; das Schema erzwingt nur die geschlossene FORM.

Outputs (in OUTDIR):
  intent_schema.json      - das json_schema.schema fuer response_format
  request_template.json   - komplettes /v1/chat/completions-Body-Geruest (ohne messages)
  capabilities_digest.json- id->{namen,typ,aktionen,wert} fuer Prompt + client-Sanity
  version.txt             - aktuelle capabilities-version (Change-Detection)
"""
import json, os, sys, urllib.request, urllib.error

BASE = os.environ.get("VOICEACT_BASE", "http://<node-red-host>:1880/voiceact")  # per Env setzen
TOKEN_FILE = os.environ.get(
    "VOICEACT_TOKEN_FILE",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "voiceact-token.txt"),
)
OUTDIR = os.path.dirname(os.path.abspath(__file__))

def fetch_capabilities():
    tok = open(TOKEN_FILE).read().strip()
    req = urllib.request.Request(BASE + "/capabilities",
                                 headers={"X-Actuator-Token": tok})
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read().decode())

def build(caps):
    ziele = caps.get("ziele", [])
    ids      = sorted(z["id"] for z in ziele)
    verbs    = sorted({a for z in ziele for a in z.get("aktionen", [])})
    einheiten= sorted({z["wert"]["einheit"] for z in ziele if "wert" in z})

    schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["ist_kommando", "ziel", "aktion", "wert", "einheit"],
        "properties": {
            "ist_kommando": {"type": "boolean"},
            # "" = kein/unbekanntes Ziel (nur zusammen mit ist_kommando=false)
            "ziel":    {"enum": ids + [""]},
            "aktion":  {"enum": verbs + [None]},
            "wert":    {"type": ["integer", "null"]},
            "einheit": {"enum": einheiten + [None]},
        },
    }
    request_template = {
        "response_format": {"type": "json_schema",
                            "json_schema": {"name": "intent", "strict": True,
                                            "schema": schema}},
        "chat_template_kwargs": {"enable_thinking": False},
        "temperature": 0,
        "max_tokens": 200,
    }
    digest = {"version": caps.get("version"),
              "ziele": [{"id": z["id"], "namen": z.get("namen", []),
                         "typ": z.get("typ"), "aktionen": z.get("aktionen", []),
                         "wert": z.get("wert")} for z in ziele]}
    return schema, request_template, digest, ids, verbs, einheiten

def main():
    caps = fetch_capabilities()
    schema, req_t, digest, ids, verbs, einheiten = build(caps)
    for name, obj in [("intent_schema.json", schema),
                      ("request_template.json", req_t),
                      ("capabilities_digest.json", digest)]:
        with open(os.path.join(OUTDIR, name), "w") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)
    with open(os.path.join(OUTDIR, "version.txt"), "w") as f:
        f.write(caps.get("version", "") + "\n")

    print(f"version      : {caps.get('version')}")
    print(f"ziele        : {len(ids)}")
    print(f"verbs (aktion): {verbs}")
    print(f"einheiten    : {einheiten}")
    print(f"ziel-enum-Groesse (inkl. \"\"): {len(ids)+1}")
    print("\nGeschrieben nach:", OUTDIR)
    print("  intent_schema.json / request_template.json / capabilities_digest.json / version.txt")

if __name__ == "__main__":
    main()
