"""Hörseite: feste Sätze („Ja?“, „Okay.“, Denk-Phrasen) je Stimme auswählen.

CosyVoice ist bei kurzen Texten unzuverlässig (services/stimmen.py: „Ja?“ roh
0,8–1,6 s, teils mit einer Sekunde Stille davor). Der Assistent rechnet je
Satz fünf Versuche und nimmt den zur erwarteten Länge passendsten — geschätzt,
nicht gehört. Hier entscheidet das Ohr: je Stimme und Satz die aktuelle
Fassung und neue Kandidaten, „nehmen“ schreibt den Kandidaten in den Cache
des Assistenten, ab dem nächsten Mal spielt er ihn (kein Neustart nötig).

Erste Runde 2026-11-09 (Jochen): bei den Denk-Phrasen sind die Unterschiede
nur Nuancen, das Anhören lohnt nicht — sie sind nur mit ``--alle`` dabei. Bei
„Ja?“ und „Okay.“ dagegen große Unterschiede in der Betonung (fragend,
fordernd), und ein „Okay.“-Kandidat war nur „äm“. Deshalb geht jeder Kandidat
durch Qwen-ASR (``stt_pruefer``), wer nicht als der Satz gehört wird, fällt raus.

    ow-venv/bin/python -m tools.feste_saetze_hoeren [--kandidaten 6] [--port 8766]
                                                    [--stimme gaston2 ...] [--satz "Ja?" ...] [--alle]

Läuft dort, wo der Assistent läuft (Profil, Lager, Cache von dort). Die Seite
ist nur im eigenen Netz erreichbar und gehört nicht als Artifact ins Netz:
sie spielt Stimmen des Haushalts ab. Vor dem Anhören das Mikro stumm
schalten, sonst triggern die Proben. Jede Wahl steht in
``<cache>/auswahl.jsonl``.
"""
from __future__ import annotations

import argparse
import html
import http.server
import json
import os
import shutil
import sys
import threading
import time
import urllib.parse

from voice_assistant.config import STIMMEN_CACHE_DIR, STIMMEN_DIR, load_profile
from voice_assistant.services.speaches import SpeachesState
from voice_assistant.services.stimmen import (
    AntwortTts, StimmenLager, _sprache_schneiden, feste_saetze, stt_pruefer, wav_dauer,
)
from voice_assistant.services.stt import LlamaCppAsrStt


class Werkstatt:
    def __init__(self, tts: AntwortTts, stimmen: list[str], saetze: list[str],
                 ordner: str, anzahl: int) -> None:
        self.tts, self.stimmen, self.saetze = tts, stimmen, saetze
        self.ordner, self.anzahl = ordner, anzahl
        self.kandidaten: dict[tuple[str, str], list[str]] = {}
        self.lock = threading.Lock()
        self.fertig = False

    def _zeile_ordner(self, stimme: str, satz: str) -> str:
        i = self.saetze.index(satz)
        return os.path.join(self.ordner, stimme, f"s{i}")

    def wuerfeln(self, stimme: str, satz: str, n: int) -> None:
        ziel = self._zeile_ordner(stimme, satz)
        os.makedirs(ziel, exist_ok=True)
        for _ in range(n):
            pcm, rate = self.tts._roh(satz, stimme)
            kern = _sprache_schneiden(pcm, rate)
            if len(kern) < 0.1 * rate:
                continue
            if self.tts.pruefen is not None and not self.tts.pruefen(kern, rate, satz):
                continue
            with self.lock:
                liste = self.kandidaten.setdefault((stimme, satz), [])
                pfad = os.path.join(ziel, f"k{len(liste)}.wav")
                liste.append(pfad)
            with open(pfad, "wb") as f:
                f.write(self.tts._wav_bytes(kern, rate))

    def alle(self) -> None:
        for stimme in self.stimmen:
            for satz in self.saetze:
                try:
                    self.wuerfeln(stimme, satz, self.anzahl)
                except Exception as e:  # noqa: BLE001 — Seite zeigt, was da ist
                    print(f"⚠️  {stimme} {satz!r}: {e}", flush=True)
        self.fertig = True
        print("alle Kandidaten gerechnet", flush=True)

    def aktuell(self, stimme: str, satz: str) -> str | None:
        p = self.tts._cache_pfad(satz, stimme)
        return p if p and os.path.exists(p) else None

    def nehmen(self, stimme: str, satz: str, pfad: str) -> str:
        ziel = self.tts._cache_pfad(satz, stimme)
        os.makedirs(os.path.dirname(ziel), exist_ok=True)
        tmp = ziel + ".tmp"
        shutil.copyfile(pfad, tmp)
        os.replace(tmp, ziel)
        with open(os.path.join(self.tts.cache, "auswahl.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps({"zeit": time.strftime("%Y-%m-%dT%H:%M:%S"), "stimme": stimme,
                                "satz": satz, "dienst_id": self.tts.lager.dienst_id(stimme),
                                "dauer_s": round(wav_dauer(ziel), 2)}, ensure_ascii=False) + "\n")
        return ziel

    # --- Seite ------------------------------------------------------------

    def seite(self) -> str:
        def audio(pfad: str) -> str:
            q = urllib.parse.quote(pfad)
            return f'<audio controls preload="none" src="/datei?p={q}"></audio>'

        teile = []
        for stimme in self.stimmen:
            teile.append(f"<h2>{html.escape(stimme)}</h2>")
            for satz in self.saetze:
                akt = self.aktuell(stimme, satz)
                zellen = [f'<div class="k akt"><b>aktuell</b> '
                          f'{wav_dauer(akt):.2f} s<br>{audio(akt)}</div>' if akt else
                          '<div class="k akt"><b>aktuell</b><br>keine (Piper)</div>']
                with self.lock:
                    liste = list(self.kandidaten.get((stimme, satz), []))
                for i, p in enumerate(liste):
                    if not os.path.exists(p):
                        continue
                    daten = json.dumps({"stimme": stimme, "satz": satz, "pfad": p})
                    zellen.append(
                        f'<div class="k">#{i + 1} {wav_dauer(p):.2f} s<br>{audio(p)}<br>'
                        f"<button onclick='los(\"/nehmen\", {html.escape(daten)}, this)'>nehmen</button></div>")
                mehr = json.dumps({"stimme": stimme, "satz": satz})
                teile.append(
                    f'<div class="z"><div class="t">{html.escape(satz)} '
                    f"<button onclick='los(\"/mehr\", {html.escape(mehr)}, this)'>mehr</button></div>"
                    f'<div class="ks">{"".join(zellen)}</div></div>')
        stand = "" if self.fertig else "<p class='hinweis'>Kandidaten werden noch gerechnet — Seite neu laden.</p>"
        return f"""<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Feste Sätze</title><style>
body{{font-family:sans-serif;max-width:80rem;margin:1rem auto;padding:0 1rem}}
.z{{border-top:1px solid #ccc;padding:.5rem 0}}.t{{font-weight:bold;margin-bottom:.3rem}}
.ks{{display:flex;flex-wrap:wrap;gap:.5rem}}.k{{padding:.3rem;border:1px solid #ddd;border-radius:4px}}
.akt{{background:#eef6ee}}audio{{width:12rem}}.hinweis{{color:#a60}}button{{margin-top:.2rem}}
</style><h1>Feste Sätze je Stimme</h1>
<p>„nehmen“ schreibt den Kandidaten in den Cache des Assistenten, ab dem nächsten Mal spielt er ihn.
„mehr“ rechnet weitere Kandidaten (dauert je Stimme einige Sekunden).</p>{stand}
{"".join(teile)}
<script>
async function los(pfad, daten, knopf) {{
  knopf.disabled = true; knopf.textContent = "…";
  const r = await fetch(pfad, {{method: "POST", body: JSON.stringify(daten)}});
  if (pfad === "/mehr" || !r.ok) {{ location.reload(); return; }}
  knopf.textContent = "genommen ✓";
  setTimeout(() => location.reload(), 600);
}}
</script>"""


def _handler(w: Werkstatt):
    class H(http.server.BaseHTTPRequestHandler):
        def _senden(self, code, daten: bytes, typ: str):
            self.send_response(code)
            self.send_header("Content-Type", typ)
            self.send_header("Content-Length", str(len(daten)))
            self.end_headers()
            self.wfile.write(daten)

        def do_GET(self):
            url = urllib.parse.urlparse(self.path)
            if url.path == "/":
                return self._senden(200, w.seite().encode(), "text/html; charset=utf-8")
            if url.path == "/datei":
                p = urllib.parse.parse_qs(url.query).get("p", [""])[0]
                erlaubt = (os.path.realpath(p).startswith(os.path.realpath(w.ordner))
                           or os.path.realpath(p).startswith(os.path.realpath(w.tts.cache)))
                if erlaubt and os.path.isfile(p):
                    with open(p, "rb") as f:
                        return self._senden(200, f.read(), "audio/wav")
            self._senden(404, b"weg", "text/plain")

        def do_POST(self):
            try:
                d = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
                stimme, satz = d["stimme"], d["satz"]
                if stimme not in w.stimmen or satz not in w.saetze:
                    raise ValueError("unbekannt")
                if self.path == "/nehmen":
                    p = d["pfad"]
                    if not os.path.realpath(p).startswith(os.path.realpath(w.ordner)):
                        raise ValueError("Pfad")
                    ziel = w.nehmen(stimme, satz, p)
                    print(f"genommen: {stimme} {satz!r} <- {p} -> {ziel}", flush=True)
                elif self.path == "/mehr":
                    w.wuerfeln(stimme, satz, 4)
                else:
                    raise ValueError("Pfad")
                self._senden(200, b"{}", "application/json")
            except Exception as e:  # noqa: BLE001
                self._senden(400, json.dumps({"error": str(e)}).encode(), "application/json")

        def log_message(self, *a):
            pass
    return H


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--kandidaten", type=int, default=6)
    ap.add_argument("--port", type=int, default=8766)
    ap.add_argument("--stimme", action="append", help="nur diese Stimme(n)")
    ap.add_argument("--satz", action="append", help="nur diese(n) Satz/Sätze")
    ap.add_argument("--alle", action="store_true", help="auch die Denk-Phrasen")
    ap.add_argument("--ordner", default=os.path.join(STIMMEN_CACHE_DIR, "_kandidaten"))
    a = ap.parse_args()

    p = load_profile()
    if not p.antwort_tts_url:
        print("Profil hat keinen Antwort-Dienst (antwort_tts_url)")
        return 1
    lager = StimmenLager(p.antwort_tts_stimmen or STIMMEN_DIR, p.antwort_tts_namensraum or p.name,
                         p.antwort_tts_url, p.antwort_tts_model, p.antwort_tts_voice, timeout=30)
    pruefen = (stt_pruefer(LlamaCppAsrStt(p.stt_llamacpp_url, timeout=30, sprache="German"))
               if p.stt_llamacpp_url else None)
    tts = AntwortTts(lager, SpeachesState(), timeout=30, cache=STIMMEN_CACHE_DIR, pruefen=pruefen)
    stimmen = a.stimme or lager.namen()
    denk = set(p.locale.thinking_phrases)
    saetze = a.satz or [t for t in feste_saetze(p) if a.alle or t not in denk]
    w = Werkstatt(tts, stimmen, saetze, a.ordner, a.kandidaten)
    threading.Thread(target=w.alle, daemon=True).start()
    srv = http.server.ThreadingHTTPServer(("0.0.0.0", a.port), _handler(w))
    print(f"Hörseite auf Port {a.port}: Stimmen {stimmen}, {len(saetze)} Sätze, "
          f"{a.kandidaten} Kandidaten je Satz", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
