import json, urllib.request, os
D="/tmp/claude-1000/-home-jochen/59195269-ddef-4908-9f82-8e61c0663ba2/scratchpad/grammar_gen"
digest=json.load(open(f"{D}/capabilities_digest.json")); req_t=json.load(open(f"{D}/request_template.json"))
lines=[]
for z in digest["ziele"]:
    rng=f"  [{z['wert']['einheit']} {z['wert']['min']}-{z['wert']['max']}]" if z.get("wert") else ""
    lines.append(f"- {z['id']}: {' / '.join(z['namen'])}  (aktionen: {','.join(z['aktionen'])}){rng}")
SYS=("Du bist der lokale Schalt-Aktuator. Wandle den Satz in EIN JSON-Intent. Gib NUR das JSON aus.\n"
"aktion: ein/aus (Licht,Schalter), auf/zu (Rollo), setzen (Zahlenwert), aktivieren (Szene), starten (Routine).\n"
"wert+einheit nur bei setzen, sonst null. Kein Steuerkommando -> ist_kommando=false, ziel=\"\", rest null.\n"
"Waehle das ziel aus der Liste (id links), Aliase rechts.\n\nBekannte Ziele:\n"+"\n".join(lines))
TESTS=[("Schalt die Fußbodenheizung im Bad ein",{"aktion":"ein","ziel":"fussbodenheizung_badoben"}),
("Mach die Regenwasserweiche an",{"aktion":"ein","ziel":"regenwasser_weiche"}),
("Regenwasser aus",{"aktion":"aus","ziel":"regenwasser_weiche"}),
("Stell die Badobenheizung auf 22 Grad",{"aktion":"setzen","ziel":"badobenheizung","wert":22,"einheit":"grad"}),
("Schalte das Küchenlicht ein",{"aktion":"ein","ziel":"kuechenlicht"})]
ok=0
for text,soll in TESTS:
    body=dict(req_t); body["messages"]=[{"role":"system","content":SYS},{"role":"user","content":text}]
    r=urllib.request.Request("http://localhost:8090/v1/chat/completions",data=json.dumps(body).encode(),headers={"Content-Type":"application/json"})
    got=json.loads(json.loads(urllib.request.urlopen(r,timeout=30).read())["choices"][0]["message"]["content"])
    good=all(got.get(k)==v for k,v in soll.items()); ok+=good
    print(("✓" if good else "✗"),text,"->",json.dumps(got,ensure_ascii=False))
print(f"\n{ok}/{len(TESTS)} korrekt")
