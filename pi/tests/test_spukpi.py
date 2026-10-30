"""Tests ohne Hardware: python -m pytest -q (im Ordner pi/)."""

import json
import struct
import time

import numpy as np
import pytest

from spukpi import protokoll
from spukpi.ambiente import RATE, Ambiente, PcmVorrat
from spukpi.analyse import Analyse
from spukpi.mischer import BLOCK, Mischer, begrenze


# ---------------------------------------------------------------------------- Protokoll
def test_rahmen_pruefsumme_wie_firmware():
    r = protokoll.huelle(100, 200, 3)
    assert r[:4] == bytes([0xA5, 0x5A, 1, 3])
    assert r[4:7] == bytes([100, 200, 3])
    assert r[7] == 1 ^ 3 ^ 100 ^ 200 ^ 3


def test_aktion_rahmen():
    r = protokoll.aktion({"name": "blitz", "zonen": [0, 2], "staerke": 80, "dauer_ms": 900, "verz_ms": 50}, 120)
    typ, maske, staerke, _res, dauer, param, verz = struct.unpack("<BBBBHHH", r[4:-1])
    assert (typ, maske, staerke, dauer, param, verz) == (1, 0b101, 80, 900, 0, 170)
    assert protokoll.aktion({"name": "keine"}) is None
    assert protokoll.aktion({"name": "gibtsnicht"}) is None


def test_zeilenleser_trennt_json_und_log():
    z = protokoll.ZeilenLeser()
    teil1 = b"[licht] Start\n\x1e{\"t\":\"hal"
    teil2 = b"lo\",\"fw\":\"1.3.0\"}\n\xff\xfe\x00\n"
    assert z.feed(teil1) == [("log", "[licht] Start")]
    assert z.feed(teil2) == [("json", {"t": "hallo", "fw": "1.3.0"})]


def test_json_zeile():
    z = protokoll.json_zeile({"t": "zustand", "hell": 50})
    assert z[0] == 0x1E and z.endswith(b"\n")
    assert json.loads(z[1:-1]) == {"t": "zustand", "hell": 50}


# ---------------------------------------------------------------------------- Analyse
def _kicks(sekunden: float, bpm: float) -> np.ndarray:
    n = int(sekunden * RATE)
    t = np.arange(n) / RATE
    x = 0.02 * np.sin(2 * np.pi * 440 * t)                    # leiser Teppich
    abstand = 60 / bpm
    for start in np.arange(0.5, sekunden, abstand):
        i = int(start * RATE)
        k = np.arange(min(4000, n - i)) / RATE
        x[i:i + len(k)] += 0.8 * np.sin(2 * np.pi * 60 * k) * np.exp(-k * 25)
    return np.stack([x, x], axis=1).astype(np.float32)


def test_schlagerkennung_findet_takt():
    a = Analyse()
    x = _kicks(10, 120)
    schlaege = []
    for i in range(0, len(x) - BLOCK, BLOCK):
        bass, mitte, hoehen, beat = a.verarbeiten(x[i:i + BLOCK])
        assert 0 <= bass <= 255 and 0 <= hoehen <= 255
        if beat:
            schlaege.append(i / RATE)
    # 120 BPM ab 0,5 s -> ~19 Schläge; die ersten 0,2 s lernt die Analyse noch
    assert 15 <= len(schlaege) <= 21
    abstaende = np.diff(schlaege[2:])
    assert abs(np.median(abstaende) - 0.5) < 0.03


def test_analyse_still():
    a = Analyse()
    assert a.verarbeiten(np.zeros((BLOCK, 2), np.float32)) == (0, 0, 0, 0)


# ---------------------------------------------------------------------------- Ambiente
def test_vorrat_liest_exakt():
    v = PcmVorrat(10)
    v.schreiben(np.ones((300, 2), np.float32))
    v.schreiben(np.full((300, 2), 2, np.float32))
    assert v.lesen(700) is None
    b = v.lesen(441)
    assert b.shape == (441, 2) and b[299, 0] == 1 and b[300, 0] == 2
    assert v.frames == 159


def test_ambiente_blendet_auf_ersatz_und_zurueck(tmp_path):
    a = Ambiente("http://test", tmp_path, 15)
    a.soll = True                       # ohne Netz-Thread: Vorrat von Hand füllen
    a.letzte_daten = time.monotonic()
    a.vorrat.schreiben(np.full((int(1.2 * RATE), 2), 0.5, np.float32))
    assert a.lesen(BLOCK) is not None and a.quelle == "stream"
    # Stream stockt: keine frischen Daten, Ersatz steht bereit
    a.letzte_daten = 0
    a._ersatz_starten = lambda: True
    a._ersatz_proc = object()
    a.ersatz.schreiben(np.full((5 * RATE, 2), -0.5, np.float32))
    while a.vorrat.sekunden >= 0.4:
        a.lesen(BLOCK)
    blk = a.lesen(BLOCK)
    assert a.quelle == "ersatz" and blk is not None
    for _ in range(60):                 # Überblendung (0,4 s) läuft durch, ohne Lücke
        assert a.lesen(BLOCK) is not None
    assert np.allclose(a.lesen(BLOCK), -0.5)
    assert a.unterlaeufe == 0
    # Stream wieder da mit genug Vorrat -> zurück
    a._ersatz_stoppen = lambda: None
    a.letzte_daten = time.monotonic()
    a.vorrat.schreiben(np.full((4 * RATE, 2), 0.5, np.float32))
    a.lesen(BLOCK)
    assert a.quelle == "stream"


def test_ambiente_unterlauf_ohne_ersatz(tmp_path):
    a = Ambiente("http://test", tmp_path, 15)
    a.soll = True
    a.letzte_daten = time.monotonic()
    a.vorrat.schreiben(np.zeros((RATE, 2), np.float32))
    while a.lesen(BLOCK) is not None:
        pass
    assert a.unterlaeufe == 1 and a.quelle == "puffert"


def test_ambiente_sprung_blendet_aus_verwirft_vorrat_und_blendet_ein(tmp_path, monkeypatch):
    a = Ambiente("http://test", tmp_path, 15)
    monkeypatch.setattr(a, "_netz", lambda gen: None)        # kein echtes Netz
    a.soll = True
    a.letzte_daten = time.monotonic()
    a.vorrat.schreiben(np.full((10 * RATE, 2), 0.5, np.float32))   # 10 s altes Stück im Vorrat
    a.lesen(BLOCK)
    assert a.quelle == "stream"
    a.sprung()
    pegel = []
    while (blk := a.lesen(BLOCK)) is not None:
        pegel.append(float(np.abs(blk).max()))
    assert 30 <= len(pegel) <= 45, "altes Stück klingt in ~0,4 s aus"
    assert pegel[0] > 0.45 and pegel[-1] < 0.05 and all(x >= y - 1e-6 for x, y in zip(pegel, pegel[1:]))
    assert a.quelle == "puffert" and a._alt is None
    # Stream stockt kurz nach dem Sprung: trotzdem kein Ersatz (das wäre das alte Stück)
    a.letzte_daten = 0
    a._ersatz_starten = lambda: pytest.fail("Ersatz nach Sprung")
    assert a.lesen(BLOCK) is None
    # neues Stück kommt im neuen Vorrat an -> weich eingeblendet
    a.vorrat.schreiben(np.full((2 * RATE, 2), 0.8, np.float32))
    erste = a.lesen(BLOCK)
    assert a.quelle == "stream" and float(np.abs(erste).max()) < 0.1
    for _ in range(40):
        blk = a.lesen(BLOCK)
    assert np.allclose(blk, 0.8)
    assert a.unterlaeufe == 0


# ---------------------------------------------------------------------------- Mischer
class _Licht:
    aktiv = True

    def __init__(self):
        self.daten = []

    def schreiben(self, d):
        self.daten.append(d)
        return True


class _Effekte:
    def __init__(self):
        self.k = np.full(RATE // 2, 0.9, np.float32)

    def hat(self, fid):
        return fid == "donner"

    def klang(self, fid):
        return self.k if fid == "donner" else None


def test_mischer_block_effekt_licht_und_ducking(tmp_path):
    amb = Ambiente("http://test", tmp_path, 15)
    licht = _Licht()
    m = Mischer("null", amb, _Effekte(), licht)
    m.latenz_ms = 80
    m.konfigurieren({"master": 100, "fx_vol": 100, "duck": 50, "latenz_ms": 20})
    assert not m.fx_start("fehlt", 100, None)
    assert m.fx_start("donner", 100, {"name": "blitz", "zonen": [1], "dauer_ms": 500})
    pcm = np.frombuffer(m._block(), np.int16)
    assert len(pcm) == BLOCK * 2 and pcm.max() > 20000          # Effekt ist zu hören (begrenzt < 32767)
    # Aktion sofort, mit Latenz 80 + 20 ms als Verzögerung
    aktion = [d for d in licht.daten if d[2] == protokoll.MSG_AKTION]
    assert len(aktion) == 1 and struct.unpack("<H", aktion[0][-3:-1])[0] == 100
    # Hüllkurve erst nach 100 ms (10 Blöcke Verzögerung)
    huellen = [d for d in licht.daten if d[2] == protokoll.MSG_HUELLE]
    assert not huellen
    for _ in range(11):
        m._block()
    assert any(d[2] == protokoll.MSG_HUELLE for d in licht.daten)
    assert m.duck_ist < 0.6


def test_musiklicht_sackt_beim_ducking_nicht_ab(tmp_path):
    amb = Ambiente("http://test", tmp_path, 15)
    amb.soll = True
    amb.letzte_daten = time.monotonic() + 3600              # Stream gilt als frisch
    t = np.arange(RATE * 3) / RATE
    ton = (0.3 * np.sin(2 * np.pi * 220 * t)).astype(np.float32)
    amb.vorrat.schreiben(np.column_stack((ton, ton)))
    licht = _Licht()
    m = Mischer("null", amb, _Effekte(), licht, analyse=False)
    m.konfigurieren({"master": 100, "musik_vol": 60, "duck": 60, "latenz_ms": 0})
    m.latenz_ms = 0
    m.musik_fade = 1.0
    musik = lambda: [d[4] for d in licht.daten if d[2] == protokoll.MSG_HUELLE][-1]   # noqa: E731
    for _ in range(30):
        m._block()
    ohne = musik()
    m.fx_start("donner", 100, None)
    for _ in range(30):
        m._block()
    assert m.duck_ist < 0.5                                   # Musik ist hörbar geduckt …
    assert ohne > 20 and abs(musik() - ohne) <= 2             # … das Musiklicht bleibt gleich


def test_limiter():
    x = np.array([0.5, 0.9, -2.0, 5.0], np.float32)
    y = begrenze(x)
    assert y[0] == 0.5 and 0.8 < y[1] < 0.95 and -1.0 <= y[2] < -0.95 and y[3] <= 1.0


if __name__ == "__main__":
    pytest.main([__file__, "-q"])


def test_mischer_spielt_ki_ansage_mit_licht(tmp_path):
    amb = Ambiente("http://test", tmp_path, 15)
    licht = _Licht()
    m = Mischer("null", amb, _Effekte(), licht)
    m.latenz_ms = 0
    m.konfigurieren({"master": 100, "fx_vol": 100, "latenz_ms": 0})
    ansage = np.full(RATE, 0.5, np.float32)
    m.klang_start("sag", ansage, 100, {"name": "env_puls", "dauer_ms": 1300, "staerke": 60})
    pcm = np.frombuffer(m._block(), np.int16)
    assert pcm.max() > 10000                                     # Ansage ist zu hören
    assert any(d[2] == protokoll.MSG_AKTION for d in licht.daten)  # Licht startet mit dem ersten Ton
    for _ in range(105):
        m._block()
    assert not m.stimmen                                         # nach 1 s fertig und weg


def test_ansage_dekodieren(tmp_path):
    import subprocess
    from spukpi.effekte import dekodieren
    wav = tmp_path / "a.wav"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=f=440:d=0.5", "-ar", "22050", str(wav)], check=True)
    k = dekodieren(str(wav))
    assert k.dtype == np.float32 and abs(len(k) - RATE // 2) < 200
