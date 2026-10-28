"""Musik-Analyse für das Licht: Pegel in drei Bändern und Schlagerkennung, 100x pro Sekunde.

Läuft im Mischer auf jedem 10-ms-Block der Ambiente-Musik (so, wie sie aus der Box kommt). Die Werte sind
automatisch normiert (Spitze merken, langsam abklingen lassen) – leise und laute Stücke füllen beide die
Helligkeit aus. Schläge: „spektrale Zunahme“ im Bass gegen einen gleitenden Median (robust gegen Dauerbass).
"""

import collections

import numpy as np

RATE = 44100
FENSTER = 2048                 # ~46 ms – genug Auflösung für den Bass (21,5 Hz pro Bin)
BIN_HZ = RATE / FENSTER
BAENDER = ((40, 160), (160, 2000), (2000, 9000))
SCHLAG_BAND = (40, 250)
SCHLAG_PAUSE_S = 0.22          # kürzester Abstand zweier Schläge (~270 BPM)
STILLE = 1e-4                  # darunter (RMS 0..1) gilt die Musik als still


def _bins(lo: float, hi: float) -> slice:
    return slice(max(1, int(lo / BIN_HZ)), max(2, int(hi / BIN_HZ)))


class Analyse:
    def __init__(self, block: int = 441) -> None:
        self.block = block
        self.puffer = np.zeros(FENSTER, np.float32)     # Ringpuffer, self.pos = ältester Wert
        self.pos = 0
        self.fenster = np.hanning(FENSTER).astype(np.float32)
        grenzen = [_bins(lo, hi) for lo, hi in BAENDER]
        self.band_start = grenzen[0].start
        self.band_idx = np.array([b.start for b in grenzen] + [grenzen[-1].stop]) - self.band_start
        self.schlag_bins = _bins(*SCHLAG_BAND)
        self.spitze = np.full(3, 1e-3, np.float32)
        self.vorher = None
        self.fluss: collections.deque[float] = collections.deque(maxlen=50)    # 1 s Verlauf (50 Analysen/s)
        self.seit_schlag = 1.0
        self.schlaege = 0
        self.schwelle = None
        self._zaehler = 0
        self._takt = 0
        self._letzte = (0, 0, 0)

    def still(self) -> tuple[int, int, int, int]:
        """Kein Ton (Ambiente aus): wie ein stiller Block, ohne zu rechnen."""
        self.vorher = None
        self._letzte = (0, 0, 0)
        return 0, 0, 0, 0

    def verarbeiten(self, musik: np.ndarray) -> tuple[int, int, int, int]:
        """musik: (n, 2) float32 −1..1. Liefert (bass, mitte, hoehen, beat) je 0..255."""
        mono = musik.mean(axis=1) if musik.ndim == 2 else musik
        n = len(mono)
        ende = self.pos + n
        if ende <= FENSTER:
            self.puffer[self.pos:ende] = mono
        else:
            k = FENSTER - self.pos
            self.puffer[self.pos:] = mono[:k]
            self.puffer[:n - k] = mono[k:]
        self.pos = ende % FENSTER
        self.seit_schlag += n / RATE

        rms = float(np.sqrt(np.dot(mono, mono) / n)) if n else 0.0
        if rms < STILLE:
            self.vorher = None
            self.spitze *= 0.999
            self._letzte = (0, 0, 0)
            return 0, 0, 0, 0
        # FFT nur jeden zweiten Block (50 Hz reichen fürs Licht, spart die Hälfte der Rechenzeit auf dem Pi 3)
        self._takt += 1
        if self._takt % 2:
            return (*self._letzte, 0)

        fenster = np.concatenate((self.puffer[self.pos:], self.puffer[:self.pos]))
        spek = np.abs(np.fft.rfft(fenster * self.fenster))
        leistung = spek[self.band_start:self.band_start + self.band_idx[-1]] ** 2
        werte = np.sqrt(np.add.reduceat(leistung, self.band_idx[:-1])).astype(np.float32)
        self.spitze = np.maximum(self.spitze * 0.997, werte)       # ~4,6 s Halbwertszeit bei 50 Analysen/s
        norm = np.clip(werte / np.maximum(self.spitze, 1e-6), 0, 1)

        tief = np.log1p(spek[self.schlag_bins])
        beat = 0
        if self.vorher is not None:
            fluss = float(np.sum(np.maximum(tief - self.vorher, 0)))
            self._zaehler += 1
            if len(self.fluss) >= 10 and (self.schwelle is None or self._zaehler % 5 == 0):
                self.schwelle = float(np.median(self.fluss)) * 1.6 + 0.05     # ändert sich langsam: alle 100 ms
            if self.schwelle is not None and len(self.fluss) >= 10:
                schwelle = self.schwelle
                if fluss > schwelle and self.seit_schlag >= SCHLAG_PAUSE_S:
                    staerke = min(1.0, (fluss / schwelle - 1.0) * 1.5 + 0.3)
                    beat = max(1, int(staerke * 255))
                    self.seit_schlag = 0.0
                    self.schlaege += 1
            self.fluss.append(fluss)
        self.vorher = tief
        self._letzte = (int(norm[0] * 255), int(norm[1] * 255), int(norm[2] * 255))
        return (*self._letzte, beat)
