"""Wakeword-Studio — geführte Werkzeuge rund um eigene Wakeword-Bundles.

Aktueller Umfang (wächst mit der Spec in Wakeword_Studio_Spec.md):

  python -m wakeword_studio record --speaker jochen   # Phase A: echte Aufnahmen
  python -m wakeword_studio score                     # Test-Set gegen Modell scoren

Die Aufnahmen landen in models/wakewords/<bundle>/samples/<sprecher>/ und
bilden das dauerhafte Test-Set des Bundles sowie später (Meilenstein 3) die
Trainingsbasis für den sprecherspezifischen Verifier.
"""
