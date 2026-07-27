# Ein-Satz-Kommando — Implementierungsplan

Stand: 2026-07-27, ausgearbeitet vor Code-Änderung.
„Gaston schalte das Tischlicht ein" — ohne Pause, in einem Satz.

## Problem

Beim Trigger feuert das Gate bei „Gaston". Dann:
1. `play_wav(ack)` blockiert ~0,6s („Ja?" wird gespielt)
2. `audio_source.flush()` verwirft gepuffertes Audio
3. `recorded_chunks = []` fängt leer an
4. Wer durchspricht („Gaston schalte...") verliert „schalte" — der Anfang
   des Kommandos ist im Puffer der geflusht wird, oder fällt in die 0,6s
   in denen „Ja?" spielt.

Die Wartegewohnheit der Familie (erst „Gaston", Pause, „Ja?", dann Kommando)
ist erzwungen, nicht Präferenz.

## Lösungsidee (aus HANDOFF 2026-07-26)

„Ja?" nicht sofort spielen. Direkt in RECORDING ohne Flush. Schwanz des
wake_ring voranstellen. ~400ms den VAD beobachten — wird weitergesprochen,
kommt gar kein „Ja?" (LED-Ring als Rückmeldung); bleibt es still, „Ja?"
spielen wie heute.

## Betroffene Code-Stellen (alle geprüft)

### Stelle 1: Normaler Trigger (assistant.py:719-731)
```
719  ack_path = ack_paths.get(...)
720  if os.path.exists(ack_path):
721      audio_sink.play_wav(ack_path)     ← BLOCKIERT ~0,6s
723  voice_controller.set_default_voice(...)
724  leds.set_phase(LED_RECORDING)          ← LED zu spät (nach ack!)
726  wakeword.reset()
727  audio_source.flush()                   ← verwirft gepuffertes Audio
728  state = STATE_RECORDING
730  recorded_chunks = []                    ← fängt FRISCH an
```

### Stelle 2: Timeout-Trigger (assistant.py:801-815)
Identischer Ablauf, zweite Code-Pfad. Gleiche Änderung nötig.

### Stelle 3: STATE_RECORDING (assistant.py:822-867)
```
822  recorded_chunks.append(audio_16.copy())
823  if _is_speech_chunk(vad, audio_16, _vad_rms_min):
824      speech_detected = True
833  stop = speech_detected and silence_counter >= _silence_limit
835  if stop or timeout:
854      if speech_detected and len(recorded_chunks) >= MIN_SPEECH_CHUNKS:
855          leds.set_phase(LED_STT)
856          state = STATE_PROCESSING
```

### Stelle 4: STATE_PAUSE (assistant.py:1144-1168)
```
1145  if now - state_start > 1.0:
1146      audio_source.flush()                ← flush nach 1s Pause
1147      wakeword.reset()
```

### Stelle 5: wake_ring (assistant.py:610-615)
Ringpuffer der letzten ~3s Audio. Enthält beim Trigger das Wakewort
und evtl. schon den Anfang des Durchgesprochenen. Wird beim Trigger
als `*_wake.wav` archiviert, dann gecleared (Zeile 717/799).

### Stelle 6: audio_source.flush() (respeaker.py:245-252)
Leert die Audio-Queue + internen Buffer. Alles was gepuffert war ist weg.

### Stelle 7: LED-Phasen (leds.py)
```
LED_IDLE       = 1  (blau, gedimmt)
LED_WAKEWORD   = 2  (hell rot)
LED_RECORDING  = 3  (hell rot)
LED_NEAR_MISS  = 12 (orange)
```
Aktuell: LED_WAKEWORD bei Erkennung (Zeile 683), dann LED_RECORDING
NACH ack (Zeile 724). Das heißt die LED kommt zu spät wenn ack blockiert.

## Geplanter Ablauf (neu)

### Neue Konstante
```python
ACK_DELAY_SEC = 0.4   # wie lange VAD beobachten bevor "Ja?" gespielt wird
```

### Trigger-Pfad (neu, beide Stellen)
```
1. wake_ring archivieren (*_wake.wav) — wie bisher
2. wake_ring NICHT clearen — wird als recorded_chunks vorangestellt
3. leds.set_phase(LED_RECORDING) — SOFORT, vor allem anderen
4. wakeword.reset()
5. KEIN audio_source.flush()
6. KEIN play_wav(ack) — noch nicht
7. state = STATE_RECORDING
8. recorded_chunks = list(wake_ring)  ← Schwanz voranstellen!
9. wake_ring.clear(); wake_ring_samples = 0
10. speech_detected = False
11. ack_pending = True  ← Flag: "Ja?" noch offen
12. ack_deadline = now + ACK_DELAY_SEC
```

### STATE_RECORDING (neu)
```
# Bestehende Logik:
recorded_chunks.append(audio_16)
if _is_speech_chunk(vad, audio_16, _vad_rms_min):
    speech_detected = True
    silence_counter = 0
elif speech_detected:
    silence_counter += 1

# NEU: ack-Entscheidung
if ack_pending and now >= ack_deadline:
    if speech_detected:
        # User spricht weiter → kein "Ja?", LED reicht
        ack_pending = False
        print("⚡ Ein-Satz erkannt — kein Ja?")
    else:
        # Still → "Ja?" spielen (wie bisher, aber verzögert)
        ack_pending = False
        if os.path.exists(ack_path):
            audio_sink.play_wav(ack_path)
        # ACHTUNG: play_wav blockiert. Währenddessen läuft Audio weiter
        # in den Puffer (read_chunk wird nicht gerufen). Nach play_wav
        # holt der nächste Loop-Durchlauf die gepufferten Chunks ab.
        # KEIN flush — die gepufferten Chunks enthalten evtl. den
        # Anfang des Kommandos falls der User doch weitergesprochen hat.
        # Risiko: TTS-Nachhall im Puffer. XVF3800 AEC sollte das abfangen
        # (ungeprüft — siehe Risiken).

# Stop/Timeout wie bisher
stop = speech_detected and silence_counter >= _silence_limit
```

### Was sich NICHT ändert
- STATE_PROCESSING, STATE_WAITING, STATE_PAUSE, STATE_FOLLOWUP — alle
  unverändert. Die Änderung ist NUR im Trigger-Pfad und STATE_RECORDING.
- wake_ring wird weiterhin archiviert (vor dem Clearen).
- STT bekommt recorded_chunks die evtl. den wake_ring-Schwanz enthalten.
  Das ist OK — die STT ignoriert „Gaston" am Anfang (oder transkribiert
  es, was nicht stört da der Aktuator das Kommando danach klassifiziert).

## Risiken

1. TTS-Nachhall: Wenn „Ja?" gespielt wird (Still-Fall) und der User
   in genau diesem Moment weiterzusprechen beginnt, mixt sich „Ja?"
   mit dem Kommando. Der XVF3800 hat AEC (Acoustic Echo Cancellation),
   aber das ist UNGEPRÜFT für diesen Fall. Test nötig.
   Mitigation: wenn das ein Problem wird, kann man play_wav in einen
   Thread auslagern und den Puffer nicht flushen.

2. wake_ring als recorded_chunks: Der Ring enthält 3s Audio inklusive
   „Gaston" und evtl. Rauschen vor dem Wakewort. Die STT bekommt das
   mit. Sollte nicht stören („Gaston" ist kein Schaltkommando), aber
   die Diarization könnte irritiert sein. Test nötig.

3. VAD-Fehler im 400ms-Fenster: Wenn der VAD in den ersten 400ms fälschlich
   Sprache erkennt (Nachhall, Knacken), wird „Ja?" unterdrückt obwohl
   der User gar nicht weitergesprochen hat. Der User bekommt kein
   akustisches Feedback, nur den LED-Ring. Das ist akzeptabel — der
   LED-Ring ist hell und sofort da.

4. Zwei Trigger-Pfade: Beide (Zeile 719 und 801) müssen identisch
   geändert werden. Vergessen = Inkonsistenz.

## LED-Ring (explizit, Jochens Anforderung)

Der LED-Ring muss SOFORT beim Trigger kommen — als optisches Feedback
„er hat mich gehört". Aktuell kommt LED_RECORDING erst NACK play_wav(ack)
(0,6s zu spät). Neu: LED_RECORDING kommt als ALLERERSTES, vor ack-Logik.
Das ist der wichtigste optische Gewinn.

## Offene Fragen

1. Was passiert wenn der User „Gaston" sagt und dann GAR NICHTS? Aktuell:
   400ms Still → „Ja?" → User sagt Kommando. Das ist der normale Fall und
   funktioniert wie bisher, nur 400ms später. Akzeptabel?

2. Soll die 400ms konfigurierbar sein (config.yaml) oder hardcoded?
   Empfehlung: hardcoded mit Konstante, kann später in config wandern.

3. Was passiert beim Aktuator-Handshake (zurueckgestellt)? Da wird
   speaker.speak() gerufen und dann audio_source.flush(). Das ist ein
   anderer Pfad (Zeile 1030) und NICHT betroffen — der Handshake kommt
   nach dem ersten Turn, nicht beim initialen Trigger.

4. Was passiert mit `near_miss_until`? Das wird beim Trigger auf 0.0
   gesetzt (Zeile 725). Das bleibt unverändert.

## Reihenfolge der Implementierung

1. Neue Konstante ACK_DELAY_SEC in assistant.py
2. Trigger-Pfad Stelle 1 (Zeile 719-731) umbauen
3. Trigger-Pfad Stelle 2 (Zeile 801-815) identisch umbauen
4. STATE_RECORDING um ack-Logik erweitern
5. ack_path als Instanzvariable (wird in STATE_RECORDING gebraucht,
   nicht nur im Trigger-Pfad)
6. Test: normales „Gaston" + Pause + Kommando (sollte „Ja?" nach 400ms)
7. Test: Ein-Satz „Gaston schalte das Tischlicht ein" (sollte kein „Ja?")
8. Test: „Gaston" + nichts (sollte „Ja?" nach 400ms + dann Still →
   Recording stoppt mit „No speech detected" → zurück zu LISTENING)
9. Test: TTS-Nachhall — ob „Ja?" sich selbst im Mikro hört (AEC-Test)
