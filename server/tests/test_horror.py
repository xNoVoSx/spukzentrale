"""1.8: Licht-Szenen, Musik dämpfen, neue Kategorien, Regisseur mit Szenen/Musik, Besuchs-Simulation."""
import asyncio
import time

from fastapi.testclient import TestClient

from app import simulation, vorgaben
from app.ki import KiStimme
from app.main import app
from app.modelle import Ausloeser, Effekt, Ereignisse, Musikstueck, MusikAnalyse, Stimmungen, Szenen
from app.zentrale import Zentrale


def test_vorgaben_kommen_bei_bestehender_installation_dazu(tmp_path, monkeypatch):
    monkeypatch.setattr("app.pfade.CONFIG", tmp_path)
    z = Zentrale.__new__(Zentrale)
    # Stand vor 1.8: eigene Listen, ohne Versionsnummer
    z.stimmungen = Stimmungen(liste=[vorgaben.STIMMUNGEN[0][1].model_copy()])
    z.szenen = Szenen(liste=[vorgaben.SZENEN[0][1].model_copy()], vorgaben_version=2)
    regel = vorgaben.REGELN[0][1].model_copy(update={"aktiv": False})          # Novos hat sie abgeschaltet
    z.ereignisse = Ereignisse(regeln=[regel])
    z._vorgaben_ergaenzen()
    ids = {s.id for s in z.stimmungen.liste}
    assert {"schwarz", "ein-geist", "strobo"} <= ids and "geister" not in ids     # nur Neues, nichts Altes zurück
    assert {"stromausfall", "totenstille", "werwolf"} <= {s.id for s in z.szenen.liste}
    rids = {r.id for r in z.ereignisse.regeln}
    assert {"sz-stromausfall", "werwolf", "klopfen"} <= rids and "lachen" not in rids
    assert z.ereignisse.regeln[0].aktiv is False                                    # Eigenes bleibt
    # zweiter Start: nichts doppelt
    n = len(z.ereignisse.regeln)
    z._vorgaben_ergaenzen()
    assert len(z.ereignisse.regeln) == n


def test_licht_szenen_sind_kurz_und_gueltig():
    with TestClient(app):
        z = app.state.zentrale
        st = {s.id for s in z.stimmungen.liste}
        for _, s in vorgaben.SZENEN:
            for sch in s.schritte:
                assert sch.typ != "stimmung" or sch.stimmung_id in st, (s.id, sch.stimmung_id)
        for sid in ("stromausfall", "totenstille", "geist-zieht-vorbei", "pingpong-geist", "strobo-salve"):
            s = next(x for x in z.szenen.liste if x.id == sid)
            assert 0 < z.spieler.dauer_ms(s) <= 15000


def test_musik_daempfen_und_von_selbst_zurueck():
    with TestClient(app):
        z = app.state.zentrale
        normal = z.ton_effektiv().musik_vol

        async def ablauf():
            await z.musik_daempfen("stille", 150)
            assert z.ton_effektiv().musik_vol == 0
            await asyncio.sleep(0.3)
            assert z.musik_daempfung is None and z.ton_effektiv().musik_vol == normal
            await z.musik_daempfen("leise", 5000)
            assert z.ton_effektiv().musik_vol == round(normal * 0.3)
            await z.musik_normal()
        asyncio.run(ablauf())
        assert z.ton_effektiv().musik_vol == normal


def test_szene_mit_musik_schritt_stellt_musik_wieder_her():
    with TestClient(app):
        z = app.state.zentrale
        s = next(x for x in z.szenen.liste if x.id == "totenstille")

        async def ablauf():
            await z.spieler.starten(s, "Test")
            await asyncio.sleep(0.1)
            assert z.musik_daempfung and z.musik_daempfung[0] == "stille"
            assert z.spieler.laufend["quelle"] == "Test" and z.spieler.rest_s() > 5
            await z.spieler.stoppen()              # abgebrochen: Musik trotzdem zurück
            assert z.musik_daempfung is None
        asyncio.run(ablauf())


def test_regie_bekommt_szenen_und_musik(monkeypatch):
    with TestClient(app):
        z = app.state.zentrale
        z.zustand.gruselstufe = 2
        z.zustand.ambiente_an = True
        monkeypatch.setattr(z.medien, "fx_der_kategorie",
                            lambda k: [Effekt(id=k, name=k, kategorie=k, dauer_ms=4000)] if k in ("werwolf", "lachen") else [])
        monkeypatch.setattr(z.medien.daten, "musik", [Musikstueck(id="m1", name="Düster", analyse=MusikAnalyse(stimmung="duester"))])
        aus = z.regie.optionen()
        assert "szene_stromausfall" in aus and "szene_werwolf" in aus and "Stromausfall" in aus["szene_stromausfall"]
        assert "szene_strobo-salve" not in aus                 # Gruselstufe 3
        assert "szene_gewitterfront" not in aus                # 36 s – zu lang
        assert "szene_schreck" not in aus                      # Schrei-Sounds fehlen
        assert {"musik_stille", "musik_leise", "musik_duester"} <= set(aus)
        z.zustand.ambiente_an = False
        assert "musik_stille" not in z.regie.optionen()
        z.zustand.gruselstufe = 3
        monkeypatch.setattr(z.medien, "fx_der_kategorie", lambda k: [Effekt(id="s", name="S", kategorie=k, dauer_ms=3000)])
        assert "szene_schreck" in z.regie.optionen()
        z.ausloeser_setzen([Ausloeser(id="k", name="Klingel", aktiv=True, typ="szene", ziel="schreck", rolle="klingel")])
        assert "szene_schreck" not in z.regie.optionen()        # der Schreck gehört der Klingel
        z.ausloeser_setzen([])


def test_regie_szene_haelt_die_stimme_nicht_auf():
    with TestClient(app):
        z = app.state.zentrale
        z.besuch.__init__(z)
        z.spieler.laufend = {"id": "x", "quelle": "Regie", "ende": time.monotonic() + 5}
        assert z.besuch._darf_sprechen()
        z.spieler.laufend = {"id": "x", "quelle": "Zufall", "ende": time.monotonic() + 5}
        assert not z.besuch._darf_sprechen()
        z.spieler.laufend = None


def test_simulation_spielt_drehbuch(monkeypatch):
    gesagt = []

    async def ansprechen(self, kamera, vol=100, quelle="", lage="", reaktion=True, still_bei=()):
        gesagt.append((quelle, self.testszene))
        return "gesagt"

    monkeypatch.setattr(KiStimme, "ansprechen", ansprechen)
    monkeypatch.setitem(simulation.DREHBUECHER, "kurz", ("Kurz", [
        (0, "szene", "Weg: {g} kommen."), (0, "fern", True), (0.1, "fern", False),
        (0.15, "szene", "Tür: {g} stehen da."), (0.15, "nah", True), (0.3, "klingel", True)]))
    monkeypatch.setattr(simulation.asyncio, "sleep", _schnell(asyncio.sleep))
    with TestClient(app) as c:
        z = app.state.zentrale
        z.besuch.__init__(z)
        c.put("/api/ausloeser", json=[
            {"name": "Weg", "aktiv": True, "entity_id": "binary_sensor.a", "typ": "ki", "ziel": "camera.weg", "rolle": "fern"},
            {"name": "Tür", "aktiv": True, "entity_id": "binary_sensor.b", "typ": "ki", "ziel": "camera.tuer", "rolle": "nah"},
            {"name": "Klingel", "aktiv": True, "entity_id": "binary_sensor.k", "typ": "kategorie", "ziel": "schrei",
             "rolle": "klingel", "nur_wenn_an": False},
        ])
        z.zustand.licht.an = True
        assert "kurz" in c.get("/api/test/simulation").json()["drehbuecher"]
        assert c.post("/api/test/simulation", json={"drehbuch": "gibtsnicht"}).status_code == 404
        c.post("/api/test/simulation", json={"drehbuch": "kurz"})

        async def warten():
            while z.simulation.laeuft:
                await asyncio.sleep(0.05)
            await asyncio.gather(*z._ki_tasks)
        asyncio.run(warten())
        assert [q for q, _ in gesagt] == ["Test: Weg", "Test: Tür"]
        assert gesagt[0][1].startswith("Weg: ") and gesagt[1][1].startswith("Tür: ")
        assert z.besuch.zustand == "geklingelt" and z.ki.testszene is None
        assert not z.besuch.an["nah"]                               # am Ende alle Sensoren aus
        c.put("/api/ausloeser", json=[])


def _schnell(sleep):
    async def kurz(s):
        await sleep(min(s, 0.05))
    return kurz


def test_ki_liest_testszene_statt_bild():
    with TestClient(app):
        z = app.state.zentrale
        z.ki.testszene = "zwei Hexen auf dem Weg"

        async def bild():
            return await z.ki._standbild(None, "camera.egal")
        assert asyncio.run(bild()) == b""
        z.ki.testszene = None
