#!/usr/bin/env python3
"""Aussprache-Liste ergaenzen: gesammelte Faelle per LLM, geprueft per STT.

Aufruf (Projekt-venv wird selbst gesucht):
    ow-venv/bin/python -m tools.aussprache_ergaenzen            # nur ab min_faelle
    ow-venv/bin/python -m tools.aussprache_ergaenzen --erzwingen
    ow-venv/bin/python -m tools.aussprache_ergaenzen --trocken   # nichts schreiben

Laeuft per Timer (aussprache-ergaenzen.timer) und tut nichts, solange weniger
als ``aussprache.min_faelle`` offene Faelle da sind. Faelle sammelt der
Assistent selbst (services/aussprache.py): gesprochene Woerter, die weder in
der Liste noch im Wiktionary-Wortschatz stehen — vor allem Namen und neue
Anglizismen.

Ablauf je Wort
--------------
1. Das LLM (``aussprache.llm_url``/``llm_model``, irgendein OpenAI-kompatibler
   Endpunkt) bekommt das Wort mit seinem Satz und liefert zwei Kandidaten: eine
   UMSCHREIBUNG in deutscher Schreibung ("Sohße") und eine IPA. Beispiele im
   Prompt kommen aus dem Wiktionary-Grundstock — so lernt es die deutsche
   Konvention fuer Fremdwoerter mit. Dazu: ist das Wort oeffentlich (Fremdwort,
   Marke, bekannte Person, Ort) oder privat (Name aus dem Haushalt)?
2. STT-Rueckprobe: "Das Wort heißt X." einmal wie heute und einmal mit jedem
   Kandidaten synthetisieren, durch die STT (Speaches, Messmodell) zurueck.
   - Bringt schon die heutige Aussprache das Wort zurueck: kein Eintrag
     ("espeak reicht") — ein Eintrag waere hier nur ein Risiko.
   - Sonst gilt der erste Kandidat, der das Wort zurueckbringt; die
     Umschreibung zuerst, denn sie bleibt im Lautvorrat der Stimme.
   - Besteht keiner: kein Eintrag, das Wort bleibt "versucht".
   Dass die Rueckprobe traegt, zeigte der 2026-10-06: die STT hoerte die
   heutigen Fassungen als "Sauke", "Hierzit", "André Fiaxung", die
   korrigierten als "Sauce", "Headset", "Andrew Jackson".
3. Uebernommen wird OHNE Anhoeren (Jochen, 2026-10-06). Die Liste der
   Uebernahmen geht zum Nachlesen an die Gruppe des Ueberwachers.

Ausgabe: ``<workspace>/voice/aussprache/<sprache>_ergaenzt.tsv`` (privat).
Was davon ins Repo darf, entscheidet tools/aussprache_veroeffentlichen.py
anhand der Markierung "oeffentlich".
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import time
import unicodedata
import urllib.request
import uuid

_VENV = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "ow-venv", "bin", "python")
if os.path.exists(_VENV) and os.path.realpath(sys.executable) != os.path.realpath(_VENV):
    os.execv(_VENV, [_VENV, "-m", "tools.aussprache_ergaenzen", *sys.argv[1:]])

from voice_assistant.config import load_profile  # noqa: E402
from voice_assistant.services import aussprache, telegram  # noqa: E402

_STAPEL = 25
_TRAEGER = "Das Wort heißt {}."


def _norm(s: str) -> str:
    """Vergleichsform: die STT schreibt "fünf", gesprochen war "fuenf"."""
    s = unicodedata.normalize("NFC", s.lower())
    for a, b in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("ß", "ss")):
        s = s.replace(a, b)
    return re.sub(r"[^a-z]", "", s)


class Rueckprobe:
    """Synthese + STT ueber Speaches, wie im Betrieb (Stimme aus dem Profil)."""

    def __init__(self, base: str, tts_model: str, tts_voice: str, stt_model: str) -> None:
        self.base, self.tts_model, self.tts_voice, self.stt_model = base.rstrip("/"), tts_model, tts_voice, stt_model

    def _synth(self, text: str) -> bytes:
        body = json.dumps({"model": self.tts_model, "voice": self.tts_voice,
                           "input": text, "response_format": "wav"}).encode()
        req = urllib.request.Request(f"{self.base}/v1/audio/speech", data=body,
                                     headers={"Content-Type": "application/json"})
        return urllib.request.urlopen(req, timeout=60).read()

    def _stt(self, wav: bytes) -> str:
        g = uuid.uuid4().hex
        teile = [f'--{g}\r\nContent-Disposition: form-data; name="model"\r\n\r\n{self.stt_model}\r\n',
                 f'--{g}\r\nContent-Disposition: form-data; name="language"\r\n\r\nde\r\n',
                 f'--{g}\r\nContent-Disposition: form-data; name="file"; filename="a.wav"\r\n'
                 'Content-Type: audio/wav\r\n\r\n']
        body = "".join(teile).encode() + wav + f"\r\n--{g}--\r\n".encode()
        req = urllib.request.Request(f"{self.base}/v1/audio/transcriptions", data=body,
                                     headers={"Content-Type": f"multipart/form-data; boundary={g}"})
        return json.loads(urllib.request.urlopen(req, timeout=60).read()).get("text", "")

    def hoert(self, wort: str, phoneme: str | None) -> tuple[bool, str]:
        """Kommt genau das Wort zurueck? Verglichen wird nur, was NACH dem
        Traegersatz steht, und als Ganzes: ein Teilstring-Vergleich hielt
        "The" fuer erkannt, weil "Das Wort heißt" angeglichen "…wortheisst"
        enthaelt (2026-10-06)."""
        text = _TRAEGER.format(f"[[{phoneme}]]" if phoneme else wort)
        gehoert = self._stt(self._synth(text))
        traeger = _norm(_TRAEGER.format(""))
        rest = _norm(gehoert)
        if rest.startswith(traeger):
            rest = rest[len(traeger):]
        return rest == _norm(wort), gehoert


def _prompt(beispiele: list[tuple[str, str]]) -> str:
    bsp = "\n".join(f"- {w}: {ipa}" for w, ipa in beispiele)
    return (
        "Du bestimmst, wie ein deutscher Sprecher Woerter in einem deutschen Satz ausspricht — "
        "vor allem Fremdwoerter, Anglizismen und Namen. Massstab ist die im Deutschen UEBLICHE "
        "Aussprache, nicht die der Herkunftssprache (\"Sauce\" ist [ˈzoːsə], nicht [sos]). "
        "Bei Namen und Begriffen, die man im Deutschen in der Originalsprache spricht, nimm diese, "
        "mit deutschen Lauten angenaehert.\n\n"
        "Gib je Wort zurueck:\n"
        "- umschreibung: die Aussprache in DEUTSCHER Schreibung, so dass ein Deutscher sie "
        "richtig vorliest (\"Sohße\", \"Hätt-ßett\", \"Ändru Dschäckßn\", \"Wehk on Länn\"). "
        "Achtung: ei liest Deutsch als ai, eu als oi, sp/st am Wortanfang als schp/scht.\n"
        "- ipa: dieselbe Aussprache in IPA.\n"
        "- oeffentlich: true fuer Fremdwoerter, Marken, Produkte, Orte und oeffentlich bekannte "
        "Personen; false fuer Namen, die nach Privatperson, Familie oder Haushalt aussehen, "
        "oder wenn unsicher.\n\n"
        f"Beispiele aus dem deutschen Wiktionary (IPA):\n{bsp}\n\n"
        "Antworte NUR mit JSON: {\"woerter\": [{\"wort\": …, \"umschreibung\": …, \"ipa\": …, "
        "\"oeffentlich\": …}, …]} in derselben Reihenfolge."
    )


def _llm(cfg, system: str, faelle: list[dict]) -> list[dict]:
    nutzer = "\n".join(f"{i + 1}. {f['wort']} — Satz: {f['satz']}" for i, f in enumerate(faelle))
    body = {"model": cfg.llm_model, "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": nutzer}]}
    kopf = {"Content-Type": "application/json"}
    if cfg.llm_api_key:
        kopf["Authorization"] = f"Bearer {cfg.llm_api_key}"
    url = cfg.llm_url.rstrip("/")
    if not url.endswith("/chat/completions"):
        url += "/v1/chat/completions"
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=kopf)
    antwort = json.loads(urllib.request.urlopen(req, timeout=600).read())
    inhalt = antwort["choices"][0]["message"]["content"] or "{}"
    inhalt = inhalt[inhalt.find("{"): inhalt.rfind("}") + 1]
    return json.loads(inhalt).get("woerter", [])


def _lies_jsonl(pfad: str) -> list[dict]:
    try:
        return [json.loads(z) for z in open(pfad, encoding="utf-8") if z.strip()]
    except FileNotFoundError:
        return []


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--erzwingen", action="store_true", help="auch unter min_faelle")
    ap.add_argument("--trocken", action="store_true", help="nichts schreiben, nichts melden")
    ap.add_argument("--max", type=int, default=200, help="hoechstens so viele Woerter je Lauf")
    a = ap.parse_args()

    profil = load_profile()
    cfg = profil.aussprache
    p = aussprache.pfade(cfg.sprache)
    versucht_pfad = p["faelle"].replace("_faelle.jsonl", "_versucht.jsonl")
    lex = aussprache.Aussprache(**p)

    versucht = {z["wort"] for z in _lies_jsonl(versucht_pfad)}
    offen: dict[str, dict] = {}
    for f in _lies_jsonl(p["faelle"]):
        w = f["wort"]
        if w not in versucht and w not in offen and not lex.nachschlagen(w):
            offen[w] = f
    print(f"{len(offen)} offene Faelle (Schwelle {cfg.min_faelle})")
    if len(offen) < cfg.min_faelle and not a.erzwingen:
        return 0
    if not cfg.llm_url or not cfg.llm_model:
        print("aussprache.llm_url / llm_model nicht gesetzt — nichts zu tun")
        return 1

    probe = Rueckprobe(profil.speaches_base, profil.speaches_tts_model,
                       profil.speaches_tts_voice, profil.speaches_stt_model)
    grund = [z.split("\t") for z in open(p["grundstock"][0], encoding="utf-8") if not z.startswith("#")]
    random.seed(len(offen))
    beispiele = [(w, ph) for w, ph, *_ in random.sample(grund, min(20, len(grund)))]
    beispiele += [("Sauce", "zˈoːsə"), ("Headset", "hˈɛdsˌɛt"), ("Currywurst", "kˈœrivˌʊɾst")]
    system = _prompt(beispiele)

    faelle = list(offen.values())[: a.max]
    uebernommen, verworfen, espeak_reicht = [], [], []
    protokoll = []
    for i in range(0, len(faelle), _STAPEL):
        stapel = faelle[i:i + _STAPEL]
        try:
            vorschlaege = {v.get("wort"): v for v in _llm(cfg, system, stapel)}
        except Exception as e:
            print(f"LLM-Fehler im Stapel {i // _STAPEL + 1}: {type(e).__name__}: {e}")
            continue
        for f in stapel:
            w = f["wort"]
            v = vorschlaege.get(w) or {}
            heute_ok, heute = probe.hoert(w, None)
            eintrag = {"wort": w, "ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "heute": heute,
                       "umschreibung": v.get("umschreibung"), "ipa": v.get("ipa"),
                       "oeffentlich": bool(v.get("oeffentlich"))}
            if heute_ok:
                eintrag["ergebnis"] = "espeak_reicht"
                espeak_reicht.append(w)
            else:
                kandidaten = []
                if v.get("umschreibung"):
                    kandidaten.append(("umschreibung", aussprache.aus_umschreibung(v["umschreibung"])))
                if v.get("ipa"):
                    kandidaten.append(("ipa", aussprache.piper_phoneme(v["ipa"])))
                heutig = aussprache.espeak_de(w)
                for art, ph in kandidaten:
                    # Gleiche Phoneme wie heute sind keine Verbesserung — ein
                    # "Bestehen" waere nur die Streuung der TTS ("Gross",
                    # 2026-10-06: heute "Kraus", dieselben Phoneme "gross").
                    if not ph or ph == heutig:
                        continue
                    ok, gehoert = probe.hoert(w, ph)
                    eintrag[f"gehoert_{art}"] = gehoert
                    if ok:
                        eintrag.update(ergebnis="uebernommen", art=art, phoneme=ph)
                        uebernommen.append(eintrag)
                        break
                else:
                    eintrag["ergebnis"] = "verworfen"
                    verworfen.append(w)
            protokoll.append(eintrag)
            print(f"  {eintrag['ergebnis']:13s} {w:18s} {(eintrag.get('umschreibung') or '')[:18]:18s} "
                  f"{'oeff' if eintrag['oeffentlich'] else 'priv'}  heute: {heute[:38]!r:40s} "
                  f"neu: {(eintrag.get('gehoert_umschreibung') or eintrag.get('gehoert_ipa') or '')[:38]!r}")

    print(f"\n{len(uebernommen)} uebernommen, {len(espeak_reicht)} espeak reicht, {len(verworfen)} verworfen")
    if a.trocken:
        return 0
    os.makedirs(os.path.dirname(p["ergaenzt"]), exist_ok=True)
    neu_datei = not os.path.exists(p["ergaenzt"])
    with open(p["ergaenzt"], "a", encoding="utf-8") as f:
        if neu_datei:
            f.write("# Vom LLM ergaenzt, per STT-Rueckprobe geprueft (tools/aussprache_ergaenzen.py)\n")
            f.write("# wort\tphoneme\toeffentlich\tumschreibung\tdatum\n")
        for e in uebernommen:
            f.write(f"{e['wort']}\t{e['phoneme']}\t{int(e['oeffentlich'])}\t"
                    f"{e.get('umschreibung') or ''}\t{e['ts'][:10]}\n")
    with open(versucht_pfad, "a", encoding="utf-8") as f:
        for e in protokoll:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")

    wt = profil.watcher
    if wt.chat_id and protokoll:
        zeilen = [f"• {e['wort']} → {e.get('umschreibung') or e['phoneme']}" for e in uebernommen]
        text = (f"🗣️ Aussprache ergänzt: {len(uebernommen)} übernommen, {len(espeak_reicht)} brauchten "
                f"keinen Eintrag, {len(verworfen)} bestanden die Hörprobe nicht.")
        if zeilen:
            text += "\n\n" + "\n".join(zeilen[:60])
        text += "\n\nFalsches korrigieren: dem Brain sagen, wie es richtig klingt."
        telegram.send(wt.bot_token or profil.telegram_bot_token, wt.chat_id, text, leise=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
