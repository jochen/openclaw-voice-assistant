# Lizenz der Aussprache-Listen

Die Dateien in diesem Verzeichnis stehen unter
[CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/deed.de),
weil sie aus dem deutschen Wiktionary abgeleitet sind.

- `de_wiktionary.tsv`: Aussprachen (IPA) aus dem
  [deutschen Wiktionary](https://de.wiktionary.org/) (Autoren: siehe
  Versionsgeschichte der jeweiligen Seite), maschinenlesbar über
  [kaikki.org](https://kaikki.org/dewiktionary/) / Wiktextract
  (Tatu Ylonen, LREC 2022). Ausgewählt und in Piper-Schreibweise umgesetzt von
  `tools/aussprache_grundstock.py`: nur Wörter, bei denen espeak-ng (Regeln
  „de") deutlich anders spricht.
- `de_ergaenzt.tsv`: Ergänzungen, von einem LLM vorgeschlagen und per
  STT-Rückprobe geprüft (`tools/aussprache_ergaenzen.py`), mit Wiktionary-
  Einträgen als Beispielen.

Format: `wort<TAB>phoneme[<TAB>…]`; die Phoneme sind espeak-ng-IPA, wie sie
Piper in `[[ … ]]` liest. Der Code des Projekts bleibt unter seiner eigenen
Lizenz.
