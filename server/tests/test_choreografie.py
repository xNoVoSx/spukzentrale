"""Licht-Choreografie zur Musik, Grundlicht unter den Effekten, Spieluhr als eigene Musik."""
import asyncio
import time

from fastapi.testclient import TestClient

from app import choreografie
from app.main import app
from app.modelle import Effekt, Musikstueck, MusikAnalyse, Zone


def test_muster():
    v = Zone(e="musik", tempo=50, staerke=90, basis=8)
    assert [z.e for z in choreografie.muster("aussen_aus", 5, v)] == ["aus", "musik", "musik", "musik", "aus"]
    assert [z.e for z in choreografie.muster("mitte", 5, v)] == ["aus", "aus", "musik", "aus", "aus"]
    assert [z.e for z in choreografie.muster("wechsel", 5, v)] == ["musik", "aus", "musik", "aus", "musik"]
    ruhig = choreografie.muster("ruhig", 5, v)
    assert sum(z.e != "aus" for z in ruhig) == 1


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
        stueck = Musikstueck(id="m", name="M", dauer_s=200, analyse=MusikAnalyse(
            bpm=120, beat0_s=0.0, stimmung="dramatisch", ruhig=[(50.0, 60.0)], stimme=[(100.0, 120.0)]))
        lage = {"pos": 10.0}
        monkeypatch.setattr(type(z), "musik_lage", lambda self: (stueck, lage["pos"]))
        asyncio.run(c.takt(time.monotonic()))
        assert z.choreo is not None and c.aktuell not in ("ruhig", "gesang")
        assert 1 <= c.naechster_wechsel - time.monotonic() <= 8.1          # 16 Schläge bei 120 BPM = 8 s, auf dem Takt
        lage["pos"] = 52.0
        asyncio.run(c.takt(c.naechster_wechsel))
        assert c.aktuell == "ruhig" and sum(zz.e != "aus" for zz in z.licht_effektiv().zonen) == 1
        lage["pos"] = 105.0
        asyncio.run(c.takt(c.naechster_wechsel))
        assert c.aktuell == "gesang"
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
