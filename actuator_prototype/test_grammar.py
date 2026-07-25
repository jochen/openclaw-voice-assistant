#!/usr/bin/env python3
import json, time, urllib.request, os
D = os.path.dirname(os.path.abspath(__file__))
digest = json.load(open(f"{D}/capabilities_digest.json"))
req_t  = json.load(open(f"{D}/request_template.json"))
URL = "http://localhost:8090/v1/chat/completions"

# --- Prompt aus dem Digest bauen (Ziel-Vokabular kennt das Modell nur hierueber) ---
lines = []
for z in digest["ziele"]:
    al = " / ".join(z["namen"])
    rng = ""
    if z.get("wert"):
        w = z["wert"]; rng = f"  [{w['einheit']} {w['min']}-{w['max']}]"
    lines.append(f'- {z["id"]}: {al}  (aktionen: {",".join(z["aktionen"])}){rng}')
ziel_liste = "\n".join(lines)

SYS = f"""Du bist der lokale Schalt-Aktuator. Wandle den gesprochenen Satz in EIN JSON-Intent. Gib NUR das JSON aus.
aktion: ein/aus (Licht,Schalter), auf/zu (Rollo ganz oeffnen/schliessen; "hoch"=auf,"runter"=zu), setzen (Zahlenwert), aktivieren (Szene), starten (Routine).
wert(Zahl)+einheit nur bei setzen (prozent Rollo, grad Heizung), sonst null. Kein Steuerkommando -> ist_kommando=false, ziel="", rest null.
Waehle das passende ziel aus der Liste (id links). Aliase stehen rechts.

Beispiele:
Schalte das Flurlicht ein -> {{"ist_kommando":true,"aktion":"ein","ziel":"flurlicht","wert":null,"einheit":null}}
Stell die Felixheizung auf 22 Grad -> {{"ist_kommando":true,"aktion":"setzen","ziel":"felixheizung","wert":22,"einheit":"grad"}}
Mach alle Rollos zu -> {{"ist_kommando":true,"aktion":"zu","ziel":"alle_rollos","wert":null,"einheit":null}}
Erzaehl mir einen Witz -> {{"ist_kommando":false,"aktion":null,"ziel":"","wert":null,"einheit":null}}

Bekannte Ziele:
{ziel_liste}"""

TESTS = [
 ("Mach das Küchenlicht an",            {"ist_kommando":True,"aktion":"ein","ziel":"kuechenlicht"}),
 ("Schalte das Wohnzimmerlicht aus",    {"ist_kommando":True,"aktion":"aus","ziel":"wohnzimmerlicht"}),
 ("Stell die Büroheizung auf 20 Grad",  {"ist_kommando":True,"aktion":"setzen","ziel":"bueroheizung","wert":20,"einheit":"grad"}),
 ("Mach den Balkonrollo zu",            {"ist_kommando":True,"aktion":"zu","ziel":"balkonrollo"}),
 ("Wohnzimmerrollo auf 30 Prozent",     {"ist_kommando":True,"aktion":"setzen","ziel":"wohnzimmerrollo","wert":30,"einheit":"prozent"}),
 ("Alle Rollos hoch",                   {"ist_kommando":True,"aktion":"auf","ziel":"alle_rollos"}),
 ("Aktiviere die Nachtruhe",            {"ist_kommando":True,"aktion":"aktivieren","ziel":"nachtruhe"}),
 ("Stopp alle Rollos",                  {"ist_kommando":True,"aktion":"starten","ziel":"rollostop"}),
 ("Wie spät ist es",                    {"ist_kommando":False,"ziel":""}),
 ("Mach die Stehlampe von Maja an",     {"ist_kommando":True,"aktion":"ein","ziel":"maja_stehlampe"}),
]

def call(text):
    body = dict(req_t); body["messages"]=[{"role":"system","content":SYS},{"role":"user","content":text}]
    data = json.dumps(body).encode()
    r = urllib.request.Request(URL, data=data, headers={"Content-Type":"application/json"})
    t0=time.time(); out=urllib.request.urlopen(r,timeout=30).read().decode(); dt=(time.time()-t0)*1000
    c = json.loads(out)["choices"][0]["message"]["content"]
    return json.loads(c), dt

ok=0
for text, soll in TESTS:
    got, dt = call(text)
    # funktionaler Vergleich: nur die soll-Felder muessen stimmen; stray einheit bei nicht-setzen egal
    good = all(got.get(k)==v for k,v in soll.items())
    if got.get("aktion")!="setzen" and "einheit" not in soll:
        pass  # stray einheit toleriert
    mark = "✓" if good else "✗"
    if good: ok+=1
    print(f"{mark} {dt:5.0f}ms  {text}")
    print(f"     -> {json.dumps(got, ensure_ascii=False)}")
print(f"\n=== {ok}/{len(TESTS)} funktional korrekt ===")
