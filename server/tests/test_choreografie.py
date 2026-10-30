"""Licht-Choreografie zur Musik, Grundlicht unter den Effekten, Spieluhr als eigene Musik."""
import asyncio
import time

from fastapi.testclient import TestClient

from app import choreografie
from app.main import app
from app.modelle import Effekt, Musikstueck, MusikAnalyse, Zone


def _hell(zonen):
    return [z.staerke >= 30 for z in zonen]


def test_muster():
    v = Zone(e="musik", tempo=50, staerke=90, basis=8)
    assert _hell(choreografie.muster("aussen_aus", 5, v)) == [False, True, True, True, False]
    assert _hell(choreografie.muster("mitte", 5, v)) == [False, True, True, True, False]       # 3 von 5
    assert _hell(choreografie.muster("wechsel", 5, v)) == [True, False, True, False, True]
    assert sum(_hell(choreografie.muster("ruhig", 5, v))) == 1
    for name in ("aussen_aus", "mitte", "wechsel", "ruhig", "gesang", "beat"):
        assert all(z.e != "aus" and z.basis > 0 for z in choreografie.muster(name, 5, v))    # nie ganz schwarz


def test_grundlicht_bleibt_unter_effekten():
    with TestClient(app):
        z = app.state.zentrale
        z.einst.licht.grund_max = 50
        z.zustand.licht.zonen = [Zone(e="musik", tempo=50, staerke=90, basis=10) for _ in range(5)]
        eff = z.licht_effektiv()
        assert eff.zonen[0].staerke == 45 and eff.zonen[0].basis == 5
        z.overlay = next(s for s in z.stimmungen.liste if s.id == "strobo")      # Szenen-Licht: volle Stärke
        assert z.licht_effektiv().zonen[0].staerke == 100
        z.overlay = None


def test_choreografie_folgt_der_musik(monkeypatch):
    with TestClient(app):
        z = app.state.zentrale
        c = z.choreografie
        z.zustand.licht.an, z.zustand.ambiente_an, z.einst.licht.choreografie = True, True, True
        z.zustand.licht.zonen = [Zone(e="musik", tempo=50, staerke=90, basis=8) for _ in range(5)]
        pegel = [50] * 200
        pegel[150:160] = [40, 40, 40, 40, 40, 70, 75, 75, 75, 75]                 # Drop bei 155 s
        stueck = Musikstueck(id="m", name="M", dauer_s=200, analyse=MusikAnalyse(
            bpm=120, beat0_s=0.0, stimmung="dramatisch", ruhig=[(50.0, 60.0)], stimme=[(100.0, 120.0)], pegel=pegel))
        lage = {"pos": 10.0}
        monkeypatch.setattr(type(z), "musik_lage", lambda self: (stueck, lage["pos"]))
        asyncio.run(c.takt(time.monotonic()))
        assert z.choreo is not None and c.aktuell not in ("ruhig", "gesang")
        assert 1 <= c.naechster_wechsel - time.monotonic() <= 8.1          # 16 Schläge bei 120 BPM = 8 s, auf dem Takt
        lage["pos"] = 52.0
        asyncio.run(c.takt(c.naechster_wechsel))
        assert c.aktuell == "ruhig" and sum(_hell(z.choreo.zonen)) <= 1
        lage["pos"] = 105.0
        asyncio.run(c.takt(c.naechster_wechsel))
        assert c.aktuell == "gesang"
        lage["pos"] = 155.5                                                     # Drop: sofort, ohne Phrasengrenze
        asyncio.run(c.takt(time.monotonic()))
        assert c.aktuell == "drop" and all(_hell(z.choreo.zonen))
        z.zustand.licht.zonen = [Zone(e="flackern") for _ in range(5)]      # Grundbeleuchtung nicht zur Musik: aus
        asyncio.run(c.takt(time.monotonic() + 999))
        assert z.choreo is None


def test_spieluhr_ist_eigene_musik():
    with TestClient(app):
        z = app.state.zentrale
        f = Effekt(id="sp", name="Spieluhr", kategorie="spieluhr", dauer_ms=400)

        async def ablauf():
            await z.fx_spielen(f)
            assert z.musik_daempfung and z.musik_daempfung[0] == "stille"
            assert z.overlay and z.overlay.id == "spieluhr" and z.spieluhr_bis > time.monotonic()
            await asyncio.sleep(0.9)
            assert z.overlay is None and z.musik_daempfung is None
        asyncio.run(ablauf())


def test_energie_bestimmt_die_stufe():
    pegel = [50] * 60 + [30] * 20 + [60] * 20
    a = MusikAnalyse(bpm=120, pegel=pegel)
    E = choreografie.Choreografie._energie
    assert E(a, 10, 8) == "mittel" and E(a, 62, 8) == "leise" and E(a, 80, 8) == "laut"
