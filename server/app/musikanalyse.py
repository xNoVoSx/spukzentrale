"""Musikanalyse: Was passiert wann im Ambiente-Stück? Grundlage für Zufalls-Effekte, die zum Stück passen.

Einmal pro Stück (beim Hochladen bzw. nach dem Start für alte Stücke), Ergebnis in medien.json:
  - Gesang/Sprache (Abschnitte) – YAMNet (Google, AudioSet-Klassen, TFLite über ai-edge-litert) auf dem Mono-Mix
    UND auf der isolierten Mitte des Stereobilds (Gesang liegt fast immer phasengleich in der Mitte, Instrumente
    breiter). Ohne Mitten-Isolierung überdeckt die Musik die Stimme: „This Is Halloween“ 16 % statt 29 % erkannt.
  - Stimmung des Stücks – YAMNet-Klassen „Scary/Sad/Tender/Exciting/Angry/Happy music“ + Dynamik
  - ruhige Stellen und Pegelkurve (RMS), Tempo und Schlag-Phase (spektraler Fluss + Autokorrelation)
Ohne Modell (Tests, fehlende Datei) gibt es Pegel, Ruhe und Takt, aber keinen Gesang (modell=False).
"""

import logging
import os
import subprocess
import threading
from pathlib import Path

import numpy as np

from .modelle import Abschnitt, MusikAnalyse

log = logging.getLogger(__name__)

ANALYSE_VERSION = 2          # 2: Ein-/Ausblenden am Stückrand zählt nicht als ruhige Stelle
RATE = 16000                     # YAMNet erwartet 16 kHz mono
YAM_FENSTER = 15600              # 0,975 s
HOP_S = 0.5
MODELL = Path(os.environ.get("SPUK_YAMNET", Path(__file__).resolve().parent.parent / "modelle" / "yamnet.tflite"))
LABELS = MODELL.with_name("yamnet_labels.txt")

STIMM_KLASSEN = ("Speech", "Singing", "Choir", "Male singing", "Female singing", "Child singing", "Vocal music",
                 "Narration, monologue", "Chant", "A capella", "Humming", "Rapping", "Synthetic singing")
STIMME_SCHWELLE = 0.08           # Summe der Stimmklassen (bei Musik im Mix liegen echte Stimmen bei 0,1–0,6)
STIMME_LUECKE_S = 4.0            # Gesang mit kürzeren Pausen gilt als ein Abschnitt
STIMME_RAND_S = 1.0              # Abschnitt um so viel vorn/hinten erweitern
STIMME_MIN_S = 1.5
RUHIG_DB = 6.0                   # so viel leiser als der Median
RUHIG_MIN_S = 2.5
RAND_S = 1.0                     # leise Stellen, die so nah am Anfang/Ende liegen, sind Ein-/Ausblenden (die Playlist
                                 # überblendet dort ohnehin ins nächste Stück) – keine Pause für Effekte

STIMMUNGS_KLASSEN = {"Scary music": "duester", "Sad music": "duester", "Tender music": "ruhig",
                     "Exciting music": "dramatisch", "Angry music": "dramatisch",
                     "Happy music": "verspielt", "Funny music": "verspielt"}
# Faktor für die Häufigkeit einer Kategorie je Stimmung (1 = wie in der Regel eingestellt)
GEWICHTE = {
    "duester":    {"wind": 1.5, "kette": 1.3, "stimme": 1.3, "herz": 1.2, "donner": 1.0, "schrei": 0.8,
                   "lachen": 0.8, "kettensaege": 0.6},
    "ruhig":      {"wind": 1.5, "herz": 1.3, "stimme": 1.2, "kette": 1.0, "donner": 0.7, "schrei": 0.6,
                   "lachen": 0.7, "kettensaege": 0.4},
    "dramatisch": {"donner": 1.6, "schrei": 1.3, "kette": 1.1, "kettensaege": 1.0, "lachen": 1.0, "wind": 0.9,
                   "herz": 0.9, "stimme": 0.8},
    "verspielt":  {"lachen": 1.6, "stimme": 1.2, "kette": 1.0, "schrei": 0.9, "donner": 0.8, "wind": 0.8,
                   "herz": 0.8, "kettensaege": 0.8},
}


# ---------------------------------------------------------------------------------------------------------------------
# YAMNet
# ---------------------------------------------------------------------------------------------------------------------
class _Yamnet:
    """Lädt das Modell beim ersten Gebrauch. Ein Interpreter, nur unter der Sperre benutzen."""

    def __init__(self) -> None:
        self._sperre = threading.Lock()
        self._it = None
        self._fehlt = False
        self.labels: list[str] = []

    def bereit(self) -> bool:
        if self._it is not None:
            return True
        if self._fehlt:
            return False
        try:
            from ai_edge_litert.interpreter import Interpreter
            it = Interpreter(model_path=str(MODELL))
            it.allocate_tensors()
            self.labels = [z.strip() for z in LABELS.read_text().splitlines() if z.strip()]
            self._it = it
            return True
        except Exception as e:                       # Modell/Paket fehlt: Analyse ohne Gesang
            log.warning("YAMNet nicht verfügbar (%s) – Musikanalyse ohne Gesangserkennung", e)
            self._fehlt = True
            return False

    def werte(self, x: np.ndarray) -> np.ndarray:
        """Klassenwerte (n_fenster, 521) für mono 16 kHz, Fenster 0,975 s, Schritt HOP_S."""
        hop = int(HOP_S * RATE)
        with self._sperre:
            it = self._it
            ein, aus = it.get_input_details()[0], it.get_output_details()[0]
            out = []
            for i in range(0, max(1, len(x) - YAM_FENSTER + 1), hop):
                teil = x[i:i + YAM_FENSTER]
                if len(teil) < YAM_FENSTER:
                    teil = np.pad(teil, (0, YAM_FENSTER - len(teil)))
                it.set_tensor(ein["index"], teil.astype(np.float32).reshape(ein["shape"]))
                it.invoke()
                out.append(it.get_tensor(aus["index"]).reshape(-1).copy())
        return np.array(out, np.float32)

    def index(self, namen) -> list[int]:
        return [self.labels.index(n) for n in namen if n in self.labels]


YAMNET = _Yamnet()


# ---------------------------------------------------------------------------------------------------------------------
# Signal
# ---------------------------------------------------------------------------------------------------------------------
def dekodieren(pfad: Path) -> np.ndarray:
    """Stereo float32 (n, 2) mit 16 kHz."""
    r = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-i", str(pfad),
                        "-f", "s16le", "-ac", "2", "-ar", str(RATE), "pipe:1"], capture_output=True, check=True)
    return np.frombuffer(r.stdout, np.int16).reshape(-1, 2).astype(np.float32) / 32768.0


def mitte(st: np.ndarray, n: int = 1024) -> np.ndarray:
    """Center-Extraktion: je STFT-Bin nur der Anteil, der links und rechts gleich ist (Gesang)."""
    if len(st) < n:
        return st.mean(axis=1)
    win = np.hanning(n).astype(np.float32)
    hop = n // 4
    aus = np.zeros(len(st), np.float32)
    rahmen_l = np.lib.stride_tricks.sliding_window_view(st[:, 0], n)[::hop]
    rahmen_r = np.lib.stride_tricks.sliding_window_view(st[:, 1], n)[::hop]
    for a in range(0, len(rahmen_l), 2048):                        # in Stücken: Speicher bleibt klein
        fl = np.fft.rfft(rahmen_l[a:a + 2048] * win)
        fr = np.fft.rfft(rahmen_r[a:a + 2048] * win)
        aehnl = 2 * np.abs(fl * np.conj(fr)) / (np.abs(fl) ** 2 + np.abs(fr) ** 2 + 1e-12)
        y = np.fft.irfft((fl + fr) / 2 * aehnl ** 6, n).astype(np.float32) * win
        for k in range(len(y)):
            s = (a + k) * hop
            aus[s:s + n] += y[k]
    return aus / 1.5                                               # Hann² mit 75 % Überlappung summiert zu 1,5


def abschnitte(maske: np.ndarray, schritt_s: float, luecke_s: float, rand_s: float, min_s: float,
               ende_s: float) -> list[Abschnitt]:
    """Zusammenhängende True-Bereiche → (von, bis) in s; kurze Lücken schließen, Rand erweitern, Kurzes weg."""
    roh: list[list[float]] = []
    for i, an in enumerate(maske):
        if not an:
            continue
        t0, t1 = i * schritt_s, (i + 1) * schritt_s
        if roh and t0 - roh[-1][1] <= luecke_s:
            roh[-1][1] = t1
        else:
            roh.append([t0, t1])
    out: list[Abschnitt] = []
    for a, b in roh:
        a, b = max(0.0, a - rand_s), min(ende_s, b + rand_s)
        if b - a < min_s:
            continue
        if out and a <= out[-1][1]:
            out[-1] = (out[-1][0], b)
        else:
            out.append((round(a, 2), round(b, 2)))
    return out


def tempo(mono: np.ndarray) -> tuple[float | None, float]:
    """(BPM oder None, Zeit des ersten Schlags). Spektraler Fluss + Autokorrelation 60–180 BPM."""
    n, hop = 1024, 256
    fps = RATE / hop
    if len(mono) < RATE * 8:
        return None, 0.0
    rahmen = np.lib.stride_tricks.sliding_window_view(mono, n)[::hop]
    win = np.hanning(n).astype(np.float32)
    fluss = []
    vorher = None
    for a in range(0, len(rahmen), 4096):
        spek = np.log1p(100 * np.abs(np.fft.rfft(rahmen[a:a + 4096] * win))[:, 1:200])   # bis ~3 kHz
        d = np.diff(np.vstack([spek[:1] if vorher is None else vorher, spek]), axis=0)
        vorher = spek[-1:]
        fluss.append(np.maximum(d, 0).sum(axis=1))
    f = np.concatenate(fluss)
    f = f - np.convolve(f, np.ones(int(fps)) / int(fps), mode="same")             # gleitenden Mittelwert weg
    f = np.maximum(f, 0)
    if not f.any():
        return None, 0.0
    lag_min, lag_max = int(fps * 60 / 180), int(fps * 60 / 60)
    ak = np.array([np.dot(f[:-lag], f[lag:]) for lag in range(lag_min, lag_max + 1)])
    lags = np.arange(lag_min, lag_max + 1)
    bpm_lag = 60 * fps / lags
    gewicht = np.exp(-0.5 * (np.log2(bpm_lag / 110) / 0.9) ** 2)                    # leichte Vorliebe um 110 BPM
    k = int(np.argmax(ak * gewicht))
    sicherheit = ak[k] / (np.median(ak) + 1e-9)
    if sicherheit < 1.6:
        return None, 0.0
    periode = lags[k]
    phasen = [f[p::periode].sum() for p in range(periode)]
    beat0 = int(np.argmax(phasen)) / fps
    return round(float(60 * fps / periode), 1), round(beat0, 3)


# ---------------------------------------------------------------------------------------------------------------------
# Gesamt
# ---------------------------------------------------------------------------------------------------------------------
def analysieren_pcm(st: np.ndarray) -> MusikAnalyse:
    dauer = len(st) / RATE
    mono = st.mean(axis=1)
    hop = int(HOP_S * RATE)
    n_fenster = max(1, len(mono) // hop)

    # Pegel je 0,5 s (dB) → ruhige Stellen und Kurve je Sekunde
    teile = mono[: n_fenster * hop].reshape(n_fenster, hop) if len(mono) >= hop else mono.reshape(1, -1)
    db = 10 * np.log10(np.mean(teile ** 2, axis=1) + 1e-10)
    ruhig = [(a, b) for a, b in abschnitte(db < np.median(db) - RUHIG_DB, HOP_S, 1.0, 0.0, RUHIG_MIN_S, dauer)
             if a > RAND_S and b < dauer - RAND_S]
    spitze = np.percentile(db, 98)
    pegel_s = [int(np.clip((np.max(db[i:i + 2]) - spitze + 40) / 40 * 100, 0, 100)) for i in range(0, len(db), 2)]

    bpm, beat0 = tempo(mono)

    a = MusikAnalyse(version=ANALYSE_VERSION, ruhig=ruhig, bpm=bpm, beat0_s=beat0, pegel=pegel_s)
    if YAMNET.bereit():
        w_mono = YAMNET.werte(mono)
        w_mitte = YAMNET.werte(mitte(st))
        idx = YAMNET.index(STIMM_KLASSEN)
        stimme = np.maximum(w_mono[:, idx].sum(axis=1), w_mitte[:, idx].sum(axis=1))
        a.modell = True
        a.stimme = abschnitte(stimme > STIMME_SCHWELLE, HOP_S, STIMME_LUECKE_S, STIMME_RAND_S, STIMME_MIN_S, dauer)
        a.stimme_kurve = [int(np.clip(np.max(stimme[i:i + 2]) * 250, 0, 100)) for i in range(0, len(stimme), 2)]
        # Stimmung: Mittel der Stimmungsklassen; Dynamik hebt „dramatisch“
        summen = {s: 0.0 for s in GEWICHTE}
        for name, art in STIMMUNGS_KLASSEN.items():
            i = YAMNET.index([name])
            if i:
                summen[art] += float(w_mono[:, i[0]].mean())
        dynamik = float(np.percentile(db, 90) - np.percentile(db, 10))
        summen["dramatisch"] += max(0.0, dynamik - 12) * 0.01
        a.stimmung = max(summen, key=summen.get) if max(summen.values()) > 0.005 else "duester"
    a.gewichte = GEWICHTE[a.stimmung]
    return a


def analysieren(pfad: Path) -> MusikAnalyse:
    """Blockiert (ffmpeg + Rechnen, ~1 s pro Minute Musik) – im Executor aufrufen."""
    return analysieren_pcm(dekodieren(pfad))
