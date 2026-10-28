#!/usr/bin/env python3
"""Lädt das YAMNet-Modell (Google MediaPipe, AudioSet, 521 Klassen) für die Musikanalyse und prüft die Prüfsumme.

    python werkzeuge/modell_laden.py [zielordner]      # Standard: server/modelle/

Wird beim Docker-Build aufgerufen; lokal einmal ausführen, sonst läuft die Analyse ohne Gesangserkennung.
"""

import hashlib
import sys
import urllib.request
from pathlib import Path

DATEIEN = {
    "yamnet.tflite": ("https://storage.googleapis.com/mediapipe-models/audio_classifier/yamnet/float32/1/yamnet.tflite",
                      "4d8b4a53282dc83ef04e3e7dbc4fbc98082e34e44ed798e16c3a0cdd4c584faf"),
    "yamnet_labels.txt": ("https://storage.googleapis.com/mediapipe-tasks/audio_classifier/yamnet_label_list.txt",
                          "8e1267a120c1932b7273c0d0e0c5529edbb9a35512b437b1c8982baa59047051"),
}


def main() -> int:
    ziel = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent / "modelle"
    ziel.mkdir(parents=True, exist_ok=True)
    for name, (url, sha) in DATEIEN.items():
        pfad = ziel / name
        if pfad.exists() and hashlib.sha256(pfad.read_bytes()).hexdigest() == sha:
            continue
        daten = urllib.request.urlopen(url, timeout=60).read()
        ist = hashlib.sha256(daten).hexdigest()
        if ist != sha:
            print(f"{name}: Prüfsumme stimmt nicht ({ist})", file=sys.stderr)
            return 1
        pfad.write_bytes(daten)
        print(f"{name}: {len(daten)} Bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
