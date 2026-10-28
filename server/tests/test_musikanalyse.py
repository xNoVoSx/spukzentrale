import collections

import numpy as np

from app import musikanalyse as ma
from app.ambiente import Ambiente
from app.medien import MedienVerwaltung


def _klicks(bpm: float, dauer: float, leise_von: float, leise_bis: float) -> np.ndarray:
    """Stereo 16 kHz: Ton-Teppich + Schläge; zwischen leise_von und leise_bis 15 dB leiser."""
    n = int(dauer * ma.RATE)
    t = np.arange(n) / ma.RATE
    x = 0.1 * np.sin(2 * np.pi * 220 * t)
    periode = 60 / bpm
    for k in range(int(dauer / periode)):
        i = int((0.2 + k * periode) * ma.RATE)
        x[i:i + 400] += np.random.default_rng(k).normal(0, 0.5, len(x[i:i + 400])) * np.exp(-np.arange(len(x[i:i + 400])) / 80)
    x[(t >= leise_von) & (t < leise_bis)] *= 10 ** (-15 / 20)
    return np.column_stack((x, x)).astype(np.float32)


def test_takt_und_ruhige_stelle():
    a = ma.analysieren_pcm(_klicks(120, 40, 20, 26))
    assert a.bpm is not None and abs(a.bpm - 120) < 3
    periode = 60 / a.bpm
    phase = (a.beat0_s - 0.2) / periode
    assert abs(phase - round(phase)) < 0.1, a.beat0_s           # Schlag-Phase stimmt
    assert any(19 <= v <= 21 and 25 <= b <= 27 for v, b in a.ruhig), a.ruhig
    assert len(a.pegel) == 40
    assert a.gewichte                                             # Stimmung hat Gewichte


def test_abschnitte_schliessen_luecken_und_verwerfen_kurzes():
    m = np.zeros(40, bool)
    m[4:8] = True      # 2,0–4,0 s
    m[12:14] = True    # 6,0–7,0 s  (Lücke 2 s -> zusammen)
    m[30] = True       # 15,0–15,5 s einzeln
    assert ma.abschnitte(m, 0.5, 4.0, 0.0, 1.5, 20.0) == [(2.0, 7.0)]
    assert ma.abschnitte(m, 0.5, 4.0, 1.0, 1.5, 20.0) == [(1.0, 8.0), (14.0, 16.5)]


def test_mitte_behaelt_center_und_entfernt_seiten():
    t = np.arange(ma.RATE * 2) / ma.RATE
    center = np.sin(2 * np.pi * 440 * t)
    links = np.sin(2 * np.pi * 1300 * t)                      # nur links -> soll weg
    st = np.column_stack((0.3 * center + 0.3 * links, 0.3 * center)).astype(np.float32)
    m = ma.mitte(st)[2000:-2000]
    spek = np.abs(np.fft.rfft(m))
    f = np.fft.rfftfreq(len(m), 1 / ma.RATE)
    assert spek[np.argmin(abs(f - 440))] > 10 * spek[np.argmin(abs(f - 1300))]


def test_position_folgt_der_encoder_uhr():
    a = Ambiente(MedienVerwaltung())
    a._task = type("T", (), {"done": lambda self: False})()       # Stream "läuft"
    a._spielplan = collections.deque([(100.0, "eins"), (160.0, "zwei")], maxlen=8)
    assert a.position(90.0) is None
    assert a.position(130.0) == ("eins", 30.0)
    assert a.position(170.5) == ("zwei", 10.5)


def test_yamnet_reine_musik_ist_kein_gesang():
    """Mit echtem Modell (CI lädt es): Akkorde + Schläge dürfen nicht als Gesang gelten."""
    import pytest
    if not ma.YAMNET.bereit():
        pytest.skip("YAMNet-Modell fehlt (python werkzeuge/modell_laden.py)")
    t = np.arange(ma.RATE * 20) / ma.RATE
    akkord = sum(np.sin(2 * np.pi * f * t) for f in (220, 277.2, 329.6)) * 0.08
    links = akkord + 0.05 * np.sin(2 * np.pi * 110 * t)
    st = np.column_stack((links, akkord)).astype(np.float32)
    st += _klicks(100, 20, 30, 30) * 0.5
    w = ma.YAMNET.werte(st.mean(axis=1))
    assert w.shape[1] == 521 and len(w) >= 35
    a = ma.analysieren_pcm(st)
    assert a.modell and a.stimme == [], a.stimme
