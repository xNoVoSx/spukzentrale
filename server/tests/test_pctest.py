"""PC-Test: Sensoren von Hand, Testbild statt Kamera, Browser als Gerät."""
import asyncio

import httpx
from fastapi.testclient import TestClient

from app.ki import KiStimme, bild_typ
from app.main import app

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
JPG = b"\xff\xd8\xff\xe0" + b"\x00" * 32


def test_sensor_ohne_auslöser_mit_rolle():
    with TestClient(app) as c:
        c.put("/api/ausloeser", json=[])
        r = c.post("/api/test/sensor", json={"rolle": "fern"})
        assert r.status_code == 404
        assert c.get("/api/test").json()["rollen"] == {"fern": None, "nah": None, "klingel": None}


def test_sensoren_laufen_ueber_den_besuch(monkeypatch):
    gesagt = []

    async def ansprechen(self, kamera, vol=100, quelle="", lage="", reaktion=True, still_bei=()):
        gesagt.append((kamera, quelle))
        return "gesagt"

    monkeypatch.setattr(KiStimme, "ansprechen", ansprechen)
    with TestClient(app) as c:
        z = app.state.zentrale
        z.besuch.__init__(z)
        c.put("/api/ausloeser", json=[
            {"name": "Weg", "aktiv": True, "entity_id": "binary_sensor.a", "typ": "ki", "ziel": "camera.weg", "rolle": "fern"},
            {"name": "Klingel", "aktiv": True, "entity_id": "binary_sensor.k", "typ": "szene", "ziel": "schreck",
             "rolle": "klingel", "nur_wenn_an": False},
        ])
        z.zustand.licht.an = True
        assert c.get("/api/test").json()["rollen"]["fern"] == "Weg"
        assert c.post("/api/test/sensor", json={"rolle": "fern"}).json()["ausgeloest"] is True

        async def warten():
            await asyncio.gather(*z._ki_tasks)
        asyncio.run(warten())
        assert gesagt == [("camera.weg", "Test: Weg")] and z.besuch.zustand == "gelockt"
        c.post("/api/test/sensor", json={"rolle": "fern", "an": False})
        asyncio.run(warten())
        assert z.besuch.an["fern"] is False
        c.post("/api/test/sensor", json={"rolle": "klingel"})
        assert z.besuch.zustand == "geklingelt"
        c.post("/api/szenen/stopp")
        c.put("/api/ausloeser", json=[])


def test_testbild_statt_kamera():
    with TestClient(app) as c:
        assert c.post("/api/test/bild", files={"datei": ("x.txt", b"hallo", "text/plain")}).status_code == 400
        assert c.post("/api/test/bild", files={"datei": ("k.png", PNG, "image/png")}).json()["testbild"] is True
        r = c.get("/api/test/bild")
        assert r.content == PNG and r.headers["content-type"] == "image/png"
        z = app.state.zentrale

        async def bild():
            async with httpx.AsyncClient() as h:
                return await z.ki._standbild(h, "camera.egal")
        assert asyncio.run(bild()) == PNG              # ohne Home Assistant: kommt direkt das Testbild
        c.delete("/api/test/bild")
        assert c.get("/api/test").json()["testbild"] is False
        assert c.get("/api/test/bild").status_code == 404


def test_bild_typ():
    assert bild_typ(PNG) == "image/png" and bild_typ(JPG) == "image/jpeg"


def test_browser_als_geraet():
    with TestClient(app) as c:
        with c.websocket_connect("/ws/geraet?rolle=sound&fw=pc-test-1") as ws:
            ws.send_json({"t": "hallo", "rolle": "sound", "fw": "pc-test-1", "plattform": "browser", "kann_sagen": True})
            for _ in range(10):                        # config, fx_sync, … – das Hallo ist verarbeitet
                if ws.receive_json().get("t") == "fx_sync":
                    break
            texte = [p["text"] for p in c.get("/api/status").json()["protokoll"]]
            assert any("PC-Test im Browser (Sound) verbunden" in t for t in texte)


def test_speicher_des_pi_gilt_weiter_wenn_er_aus_ist():
    with TestClient(app) as c:
        z = app.state.zentrale
        z.medien.daten.sound_plattform, z.medien.daten.sound_speicher_kb = "", 0
        assert z.fx_kapazitaet_kb() == 0                    # nie ein Gerät gesehen: nicht begrenzen
        for plattform, kb in (("pi", 50_000_000), ("browser", 10_000_000)):
            with c.websocket_connect("/ws/geraet?rolle=sound&fw=x") as ws:
                ws.send_json({"t": "hallo", "rolle": "sound", "fw": "x", "plattform": plattform, "fx_gesamt_kb": kb})
                for _ in range(10):
                    if ws.receive_json().get("t") == "fx_sync":
                        break
        # Pi und Browser-Test sind wieder weg: es gilt der Speicher des Pi (der Browser überschreibt ihn nicht)
        assert z.medien.daten.sound_plattform == "pi" and z.fx_kapazitaet_kb() == 50_000_000 - 64
        assert c.get("/api/status").json()["medien"]["sound_plattform"] == "pi"
