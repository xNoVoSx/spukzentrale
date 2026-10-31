"""Musik: doppelte Stücke erkennen, Stille an den Enden finden, passende Reihenfolge für die Zufalls-Playlist.

Doppelt: gleicher Name oder gleicher Klang-Fingerabdruck (übersteht anderes Format, andere Lautstärke, loudnorm und
abgeschnittene Ränder). Gemessen an den 15 Stücken vom 31.10.: dasselbe Stück 10 dB leiser, 3 s kürzer und umkodiert
weicht in 4–6 % der Bits ab, verschiedene Stücke in mindestens 47 %. Ein Abdruck aus dem Lautstärkeverlauf taugte
nicht: loudnorm verbiegt ihn je nach Ausgangspegel, und verschiedene Stücke ähnelten sich darin bis 0,98.

Reihenfolge: jede Runde spielt jedes Stück genau einmal (alle kommen dran), innerhalb der Runde folgt auf ein Stück
bevorzugt eines mit ähnlichem Tempo, gleicher Stimmung und passender Energie am Übergang.
"""

import logging
import math
import random
import re
import subprocess
from pathlib import Path

import numpy as np

from .modelle import Musikstueck

log = logging.getLogger(__name__)

RATE = 8000
FENSTER = 1024
HOP = RATE // 8                    # Achtelsekunde
BAENDER = np.geomspace(300, 3000, 18)   # 17 Bänder -> 16 Bit je Schritt
MAX_VERSATZ = 80                   # bis 10 s gegeneinander verschoben (abgeschnittene Stille, anderes Intro)
GLEICH_BIS = 0.25                  # Anteil abweichender Bits: gleiches Stück ≤ 0,06, verschiedene ≥ 0,47 (gemessen)
STILLE_DB = -55
STILLE_MIN_S = 0.5


def abdruck(pfad: Path) -> np.ndarray:
    """Klang-Fingerabdruck (wie bei Musikerkennung): je Achtelsekunde 16 Bit – steigt der Energieunterschied zweier
    Nachbarbänder gegenüber dem vorigen Schritt? Unabhängig von Lautstärke, Kompression und Format.
    Blockiert (~0,5 s pro Stück) – im Executor aufrufen. Ergebnis: (Schritte, 2) uint8 (gepackte Bits)."""
    roh = subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-i", str(pfad), "-ac", "1", "-ar", str(RATE),
                          "-f", "s16le", "-"], capture_output=True, check=True, timeout=120).stdout
    x = np.frombuffer(roh, np.int16).astype(np.float32) / 32768
    n = (len(x) - FENSTER) // HOP
    if n < 20:
        return np.zeros((0, 2), np.uint8)
    rahmen = np.lib.stride_tricks.sliding_window_view(x, FENSTER)[::HOP][:n] * np.hanning(FENSTER).astype(np.float32)
    spektrum = np.abs(np.fft.rfft(rahmen, axis=1)) ** 2
    f = np.fft.rfftfreq(FENSTER, 1 / RATE)
    e = np.log(np.stack([spektrum[:, (f >= a) & (f < b)].sum(axis=1) for a, b in zip(BAENDER[:-1], BAENDER[1:])],
                        axis=1) + 1e-9)
    d = e[:, :-1] - e[:, 1:]
    return np.packbits((d[1:] - d[:-1]) > 0, axis=1)


def abdruck_von(mp3: Path) -> np.ndarray:
    """Abdruck eines gespeicherten Stücks, zwischengespeichert neben der MP3 (<id>.abdruck.npy)."""
    cache = mp3.with_suffix(".abdruck.npy")
    try:
        return np.load(cache)
    except (OSError, ValueError):
        pass
    a = abdruck(mp3)
    try:
        np.save(cache, a)
    except OSError as e:
        log.warning("Abdruck nicht gespeichert: %s", e)
    return a


def abweichung(a: np.ndarray, b: np.ndarray) -> float:
    """Kleinster Anteil abweichender Bits über alle Versätze bis MAX_VERSATZ (1 = nicht vergleichbar)."""
    if min(len(a), len(b)) < 20:
        return 1.0
    a, b = np.unpackbits(a, axis=1), np.unpackbits(b, axis=1)
    beste = 1.0
    for v in range(-MAX_VERSATZ, MAX_VERSATZ + 1):
        x, y = (a[v:], b) if v >= 0 else (a, b[-v:])
        m = min(len(x), len(y))
        if m < 0.8 * min(len(a), len(b)):
            continue
        beste = min(beste, float(np.mean(x[:m] != y[:m])))
    return beste


def _name(s: str) -> str:
    return re.sub(r"[^a-z0-9äöüß]+", " ", s.lower()).strip()


def doppelt(neu: Path, name: str, vorhandene: list[tuple[Musikstueck, Path]]) -> tuple[Musikstueck | None, np.ndarray]:
    """(schon vorhandenes gleiches Stück oder None, Abdruck des neuen). Blockiert – im Executor aufrufen."""
    a = abdruck(neu)
    for s, _ in vorhandene:
        if _name(s.name) == _name(name):
            return s, a
    for s, mp3 in vorhandene:
        b = abdruck_von(mp3)
        if abs(len(a) - len(b)) <= 2 * MAX_VERSATZ and abweichung(a, b) <= GLEICH_BIS:
            return s, a
    return None, a


def stille_grenzen(stderr: str, dauer: float) -> tuple[float, float | None]:
    """Aus der silencedetect-Ausgabe: (Anfang ohne Stille, Ende ohne Stille oder None = bis zum Schluss)."""
    starts = [float(x) for x in re.findall(r"silence_start:\s*(-?[\d.]+)", stderr)]
    enden = [float(x) for x in re.findall(r"silence_end:\s*(-?[\d.]+)", stderr)]
    anfang = enden[0] if starts and starts[0] <= 0.05 and enden else 0.0
    ende = None
    if starts and (len(enden) < len(starts) or enden[-1] >= dauer - 0.1) and starts[-1] > anfang:
        ende = starts[-1]
    return anfang, ende


# --- Reihenfolge -------------------------------------------------------------------------------------------------
def _energie(s: Musikstueck, ende: bool) -> float | None:
    p = s.analyse.pegel if s.analyse else []
    if len(p) < 30:
        return None
    teil = p[-20:] if ende else p[:20]
    return sum(teil) / len(teil)


def abstand(a: Musikstueck, b: Musikstueck) -> float:
    """Wie schlecht passt b nach a? 0 = ideal. Ohne Analyse neutral."""
    d = 0.0
    ta, tb = (a.analyse.bpm if a.analyse else None), (b.analyse.bpm if b.analyse else None)
    if ta and tb:
        # halbes/doppeltes Tempo zählt als passend; 10 % Unterschied ≈ 1,4
        d += 10 * min(abs(math.log2(ta / tb)), abs(math.log2(2 * ta / tb)), abs(math.log2(ta / (2 * tb))))
    else:
        d += 0.7
    if a.analyse and b.analyse:
        d += 0 if a.analyse.stimmung == b.analyse.stimmung else 1.0
    else:
        d += 0.5
    ea, eb = _energie(a, True), _energie(b, False)
    d += abs(ea - eb) / 20 if ea is not None and eb is not None else 0.5
    return d


def reihenfolge(stuecke: list[Musikstueck], vorher: Musikstueck | None = None) -> list[str]:
    """Eine Runde: jedes Stück einmal, nacheinander möglichst passend (unter den 3 passendsten zufällig, damit es
    nicht jede Runde gleich klingt). Das zuletzt gespielte Stück kommt nicht gleich wieder."""
    rest = list(stuecke)
    random.shuffle(rest)
    aus: list[str] = []
    letzt = vorher
    while rest:
        if letzt is None:
            wahl = rest[0]
        else:
            kandidaten = sorted((s for s in rest if s.id != letzt.id), key=lambda s: abstand(letzt, s))[:3] or rest
            wahl = random.choices(kandidaten, weights=[3, 2, 1][: len(kandidaten)])[0]
        rest.remove(wahl)
        aus.append(wahl.id)
        letzt = wahl
    return aus
