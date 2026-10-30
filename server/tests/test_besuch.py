"""Besuchs-Ablauf: wer wann angesprochen wird – und vor allem, wann nicht."""
import asyncio
import time

import pytest
from fastapi.testclient import TestClient

from app.besuch import ABSCHIED, LOCKEN, TUER
from app.ki import KiStimme
from app.main import app
from app.modelle import Ausloeser


@pytest.fixture
def z(monkeypatch):
    gesagt: list[str] = []

    async def ansprechen(self, kamera, vol=100, quelle="", lage="", reaktion=True, still_bei=()):
        if self.z._richtung in still_bei:
            self.z.geprueft += 1
            return "still"
        gesagt.append(lage)
        self.verlauf.appendleft({"satz": f"Satz {len(gesagt)}", "richtung": self.z._richtung, "ergebnis": "gesagt"})
        return "gesagt"

    monkeypatch.setattr(KiStimme, "ansprechen", ansprechen)
    with TestClient(app) as c:
        zentrale = app.state.zentrale
        zentrale._richtung = "kommt"
        zentrale.gesagt = gesagt
        zentrale.geprueft = 0
        zentrale.besuch.__init__(zentrale)        # sauberer Zustand
        yield zentrale


FERN = Ausloeser(id="f", name="fern", typ="ki", rolle="fern", ziel="camera.trackmix")
NAH = Ausloeser(id="n", name="nah", typ="ki", rolle="nah", ziel="camera.klingel")


def _vorspulen(b, s):
    """Zeit vergehen lassen: alle monotonic-Merker nach hinten schieben."""
    b.letzte_ansprache -= s
    b.zuletzt_jemand -= s
    b.klingel_zeit -= s
    b.seit -= s
    b.letzte_pruefung -= s


def test_normaler_besuch_bis_zum_klingeln_und_abschied(z):
    b = z.besuch
    asyncio.run(b.sensor(FERN, True, "fern"))
    assert z.gesagt == [LOCKEN] and b.zustand == "gelockt"
    asyncio.run(b.sensor(FERN, False, "fern"))
    asyncio.run(b.sensor(NAH, True, "nah"))
    assert z.gesagt[-1] == TUER and b.zustand == "tuer"
    asyncio.run(b.sensor(NAH, True, "nah"))                 # Sensor flattert: nicht nochmal
    assert len(z.gesagt) == 2
    b.klingel()
    assert b.zustand == "geklingelt" and z.ki.abbrechen
    asyncio.run(b.sensor(FERN, True, "fern"))               # gleich nach dem Klingeln: still (ihr redet)
    assert len(z.gesagt) == 2
    _vorspulen(b, z.einst.ki.neu_pruefen_s + 1)
    z._richtung = "geht"
    asyncio.run(b.sensor(NAH, False, "nah"))                # Tür wieder frei
    asyncio.run(b.sensor(FERN, True, "fern"))               # gehen wieder (Modell sieht Rücken): Abschied, einmal
    assert z.gesagt[-1] == LOCKEN and b.zustand == "geht" and b.abschied_gesagt
    asyncio.run(b.sensor(FERN, True, "fern"))
    assert len(z.gesagt) == 3                               # nie zurücklocken
    _vorspulen(b, z.einst.ki.neu_pruefen_s + 1)
    asyncio.run(b.sensor(FERN, True, "fern"))               # später: KI schaut hin, die gehen aber -> still
    assert len(z.gesagt) == 3 and z.geprueft == 1 and b.zustand == "geht"


def test_bleiben_stehen_hoechstens_einmal_nachfassen(z):
    b = z.besuch
    asyncio.run(b.sensor(FERN, True, "fern"))
    asyncio.run(b.takt(time.monotonic()))
    assert len(z.gesagt) == 1                               # zu früh
    _vorspulen(b, 31)
    asyncio.run(b.takt(time.monotonic()))
    assert len(z.gesagt) == 2 and "nicht näher gekommen" in z.gesagt[-1] and "Satz 1" in z.gesagt[-1]
    _vorspulen(b, 31)
    asyncio.run(b.takt(time.monotonic()))
    asyncio.run(b.sensor(FERN, True, "fern"))
    assert len(z.gesagt) == 2                               # locken_max = 2


def test_gehen_weg_werden_nicht_zurueckgelockt(z):
    b = z.besuch
    z._richtung = "geht"
    asyncio.run(b.sensor(FERN, True, "fern"))
    assert b.zustand == "geht" and b.abschied_gesagt         # Modell hat sich laut Vorgabe verabschiedet
    _vorspulen(b, 60)
    asyncio.run(b.sensor(FERN, True, "fern"))
    asyncio.run(b.takt(time.monotonic()))
    assert len(z.gesagt) == 1


def test_an_der_tuer_ohne_klingeln_wieder_weg(z):
    b = z.besuch
    asyncio.run(b.sensor(NAH, True, "nah"))
    assert z.gesagt == [TUER]
    asyncio.run(b.sensor(NAH, False, "nah"))
    asyncio.run(b.sensor(FERN, True, "fern"))
    assert z.gesagt[-1] == ABSCHIED and b.zustand == "geht"


def test_besuch_endet_und_naechster_beginnt(z):
    b = z.besuch
    asyncio.run(b.sensor(FERN, True, "fern"))
    asyncio.run(b.sensor(FERN, False, "fern"))
    assert z.besuch.aktiv
    _vorspulen(b, z.einst.ki.besuch_ende_s + 1)
    asyncio.run(b.takt(time.monotonic()))
    assert b.zustand == "nachlauf" and not b.aktiv
    z._richtung = "geht"
    asyncio.run(b.sensor(FERN, True, "fern"))               # Leute auf dem Rückweg: still
    asyncio.run(b.sensor(FERN, False, "fern"))
    assert len(z.gesagt) == 1
    z._richtung = "kommt"
    _vorspulen(b, z.einst.ki.nachlauf_s + 1)
    asyncio.run(b.takt(time.monotonic()))
    assert b.zustand == "leer"
    asyncio.run(b.sensor(FERN, True, "fern"))               # neue Gruppe
    assert z.gesagt[-1] == LOCKEN and b.gelockt == 1


def test_pause_und_szene_halten_die_ki_still(z):
    b = z.besuch
    b.pause(10)
    asyncio.run(b.sensor(FERN, True, "fern"))
    assert z.gesagt == []
    b.pause(0)
    z.spieler.laufend = {"id": "schreck"}
    asyncio.run(b.sensor(NAH, True, "nah"))
    assert z.gesagt == []
    z.spieler.laufend = None


def test_zufallsereignisse_pausieren_waehrend_besuch(z):
    b = z.besuch
    assert not b.aktiv
    asyncio.run(b.sensor(FERN, True, "fern"))
    assert b.aktiv


def test_api_status_und_pause():
    with TestClient(app) as c:
        r = c.post("/api/ki/pause", json={"minuten": 5})
        assert r.json()["pause_bis"] > time.time()
        assert c.post("/api/ki/pause", json={"minuten": 0}).json()["pause_bis"] == 0


def test_ausloeser_mit_rolle_laufen_ueber_den_besuch(z):
    z.ausloeser_setzen([FERN.model_copy(update={"aktiv": True, "sperre_s": 600}),
                        NAH.model_copy(update={"aktiv": True}),
                        Ausloeser(id="k", name="Klingel", aktiv=True, typ="szene", ziel="schreck", rolle="klingel")])
    z.zustand.licht.an = True

    async def ablauf():
        assert await z.ausloeser("f") is True
        await asyncio.gather(*z._ki_tasks)
        assert await z.ausloeser("f") is True                 # keine Sperrzeit – der Besuch entscheidet
        await asyncio.gather(*z._ki_tasks)
        z.ausloeser_aus("f")
        await asyncio.gather(*z._ki_tasks)
        assert await z.ausloeser("k") is True                 # Klingel: Szene wie bisher + Besuch merkt es
        await z.spieler.stoppen()

    asyncio.run(ablauf())
    assert z.gesagt == [LOCKEN] and not z.besuch.an["fern"] and z.besuch.zustand == "geklingelt"
    z.zustand.ki_an = False                                     # KI aus: wieder ganz normaler Auslöser
    assert asyncio.run(z.ausloeser("f")) in (True, False) and z.gesagt == [LOCKEN]
    z.zustand.ki_an = True


def test_naechste_gruppe_kommt_waehrend_die_erste_geht(z):
    b = z.besuch
    asyncio.run(b.sensor(NAH, True, "nah"))
    asyncio.run(b.sensor(NAH, False, "nah"))
    asyncio.run(b.sensor(FERN, True, "fern"))
    assert b.zustand == "geht" and z.gesagt == [TUER, ABSCHIED]
    asyncio.run(b.sensor(NAH, True, "nah"))                 # zu früh nach dem Abschied: nicht mal hinschauen
    assert len(z.gesagt) == 2 and z.geprueft == 0
    asyncio.run(b.sensor(NAH, False, "nah"))
    # Sensor bleibt dauerhaft an (ständig Leute im Bild): der Takt schaut nach neu_pruefen_s hin
    _vorspulen(b, z.einst.ki.neu_pruefen_s + 1)
    z._richtung = "geht"
    asyncio.run(b.takt(time.monotonic()))
    assert z.geprueft == 1 and b.zustand == "geht"
    asyncio.run(b.takt(time.monotonic()))                   # gleich danach nicht nochmal
    assert z.geprueft == 1
    _vorspulen(b, z.einst.ki.neu_pruefen_s + 1)
    z._richtung = "kommt"
    asyncio.run(b.takt(time.monotonic()))                   # neue Gruppe kommt: neuer Besuch
    assert z.gesagt[-1] == LOCKEN and b.zustand == "gelockt" and b.gelockt == 1 and not b.abschied_gesagt


def test_neue_gruppe_an_der_tuer(z):
    b = z.besuch
    z._richtung = "geht"
    asyncio.run(b.sensor(FERN, True, "fern"))               # erste Gruppe geht schon
    assert b.zustand == "geht"
    _vorspulen(b, z.einst.ki.neu_pruefen_s + 1)
    z._richtung = "steht"
    asyncio.run(b.sensor(NAH, True, "nah"))
    assert z.gesagt[-1] == TUER and b.zustand == "tuer" and b.tuer_gesagt


def test_nach_dem_klingeln_kommt_schon_die_naechste_gruppe(z):
    b = z.besuch
    asyncio.run(b.sensor(NAH, True, "nah"))
    b.klingel()
    asyncio.run(b.sensor(NAH, False, "nah"))
    asyncio.run(b.sensor(FERN, True, "fern"))               # gleich nach dem Klingeln: still
    assert z.gesagt == [TUER]
    _vorspulen(b, z.einst.ki.neu_pruefen_s + 1)
    asyncio.run(b.takt(time.monotonic()))                   # Weg-Sensor bleibt an, neue Leute kommen
    assert z.gesagt[-1] == LOCKEN and b.zustand == "gelockt" and b.gelockt == 1 and not b.tuer_gesagt
    asyncio.run(b.sensor(NAH, True, "nah"))                 # die Neuen an der Tür: wieder Aufforderung
    assert z.gesagt[-1] == TUER and b.zustand == "tuer"


def test_genug_gelockt_dann_nur_noch_abschied_wenn_sie_gehen(z):
    b = z.besuch
    asyncio.run(b.sensor(FERN, True, "fern"))
    _vorspulen(b, 31)
    asyncio.run(b.takt(time.monotonic()))                   # einmal nachgefasst -> Maximum (2)
    assert b.gelockt == 2 and len(z.gesagt) == 2
    _vorspulen(b, z.einst.ki.neu_pruefen_s + 1)
    asyncio.run(b.sensor(FERN, True, "fern"))               # stehen noch da: still
    assert len(z.gesagt) == 2 and z.geprueft == 1 and b.zustand == "gelockt"
    _vorspulen(b, z.einst.ki.neu_pruefen_s + 1)
    z._richtung = "geht"
    asyncio.run(b.sensor(FERN, True, "fern"))               # drehen um: Abschied
    assert z.gesagt[-1] == ABSCHIED and b.zustand == "geht" and b.abschied_gesagt


def test_tuer_hat_vorrang(z):
    """Gruppe A steht an der Tür (noch nicht geklingelt), Gruppe B kommt den Weg herauf: niemand redet dazwischen."""
    b = z.besuch
    asyncio.run(b.sensor(NAH, True, "nah"))
    assert z.gesagt == [TUER]
    _vorspulen(b, 120)
    asyncio.run(b.sensor(FERN, True, "fern"))               # B auf dem Weg, A noch an der Tür: still
    asyncio.run(b.takt(time.monotonic()))
    assert z.gesagt == [TUER] and b.zustand == "tuer"
    b.klingel()                                             # beide klingeln zusammen: ein Besuch
    _vorspulen(b, z.einst.ki.neu_pruefen_s + 1)
    asyncio.run(b.takt(time.monotonic()))                   # du redest an der Tür, Weg-Sensor an: still
    assert z.gesagt == [TUER]
    asyncio.run(b.sensor(NAH, False, "nah"))                # Tür frei, auf dem Weg jemand (neue Leute)
    asyncio.run(b.takt(time.monotonic()))
    assert z.gesagt[-1] == LOCKEN and b.zustand == "gelockt"
