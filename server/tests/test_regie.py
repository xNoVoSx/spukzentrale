"""KI-Regisseur: wählt nur während eines Besuchs, nur aus erlaubten Effekten, und nie über etwas anderes drüber."""
import asyncio
import time

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.modelle import Effekt, Regel
from app.regie import Regie


@pytest.fixture
def z(monkeypatch):
    gespielt: list[str] = []
    gefragt: list[tuple[str, list[str]]] = []

    async def fragen(self, anlass, satz, aus):
        gefragt.append((anlass, list(aus)))
        return self.z._regie_wahl, "passt"

    async def fx_spielen(self, f, vol=100, licht="auto", quelle=""):
        gespielt.append(f.kategorie)

    async def aktion(self, a, verz_ms=0, dauer_ms=None, quelle=""):
        gespielt.append("licht:" + a.name)

    fx = {"donner": [Effekt(id="d1", name="Donner", kategorie="donner", dauer_ms=6000)],
          "kettensaege": [Effekt(id="k1", name="Säge", kategorie="kettensaege", dauer_ms=15800)],
          "lachen": [Effekt(id="l1", name="Lachen", kategorie="lachen", dauer_ms=4500)],
          "stimme": [Effekt(id="s1", name="Stimme", kategorie="stimme", dauer_ms=3000)]}

    monkeypatch.setattr(Regie, "_fragen", fragen)
    with TestClient(app):
        zentrale = app.state.zentrale
        monkeypatch.setattr(type(zentrale), "fx_spielen", fx_spielen)
        monkeypatch.setattr(type(zentrale), "aktion", aktion)
        monkeypatch.setattr(zentrale.medien, "fx_der_kategorie", lambda k: fx.get(k, []))
        zentrale.ereignisse.regeln = [
            Regel(id="a", typ="kategorie", ziel="donner"),
            Regel(id="b", typ="kategorie", ziel="kettensaege"),           # zu lang
            Regel(id="c", typ="kategorie", ziel="lachen", gruselstufe_min=3),
            Regel(id="d", typ="kategorie", ziel="stimme"),                # Stimmen nie
            Regel(id="e", typ="kategorie", ziel="herz", aktiv=False),
        ]
        zentrale.besuch.__init__(zentrale)
        zentrale.regie.__init__(zentrale)
        zentrale.zustand.licht.an = True
        zentrale.zustand.gruselstufe = 2
        zentrale.einst.ki.llm_url = "http://ki:8091"
        zentrale.zustand.regie_an = True
        zentrale._regie_wahl = "donner"
        zentrale.gespielt, zentrale.gefragt = gespielt, gefragt
        yield zentrale
        zentrale.zustand.regie_an = False


def test_auswahl_folgt_regeln_und_gruselstufe(z):
    aus = z.regie.optionen()
    assert "donner" in aus and "keiner" in aus and "licht_blitz" in aus
    assert "kettensaege" not in aus and "stimme" not in aus and "herz" not in aus and "lachen" not in aus
    z.zustand.gruselstufe = 3
    assert "lachen" in z.regie.optionen()


def test_standard_ist_aus_und_ohne_besuch_passiert_nichts(z):
    z.zustand.regie_an = False
    z.besuch.zustand = "gelockt"
    asyncio.run(z.regie.takt(time.monotonic() + 999))
    assert z.gefragt == []
    z.zustand.regie_an = True
    z.besuch.zustand = "leer"
    asyncio.run(z.regie.takt(time.monotonic() + 999))
    assert z.gefragt == []


def test_pause_im_besuch_spielt_effekt_und_belegt(z):
    z.besuch.zustand = "gelockt"
    asyncio.run(z.regie.takt(time.monotonic() + 999))
    assert z.gespielt == ["donner"] and z.regie.frei_in() > 5
    assert z.regie.verlauf[0]["effekt"] == "donner"
    asyncio.run(z.regie.takt(time.monotonic() + 999))       # läuft noch: nicht nochmal
    assert len(z.gefragt) == 1
    z.regie.belegt_bis = 0
    asyncio.run(z.regie.takt(time.monotonic()))             # Abstand zu kurz
    assert len(z.gefragt) == 1


def test_still_bei_klingel_szene_und_ki(z):
    z.besuch.zustand = "geklingelt"
    asyncio.run(z.regie.takt(time.monotonic() + 999))
    z.besuch.zustand = "tuer"
    z.ki.laeuft = True
    asyncio.run(z.regie.takt(time.monotonic() + 999))
    z.ki.laeuft = False
    z.spieler.laufend = {"id": "schreck"}
    asyncio.run(z.regie.takt(time.monotonic() + 999))
    z.spieler.laufend = None
    assert z.gefragt == [] and z.gespielt == []


def test_nach_satz_pointe_und_licht(z):
    z.besuch.zustand = "tuer"
    z._regie_wahl = "licht_blitz"
    asyncio.run(z.regie.nach_satz("Klingel, wenn du dich traust!", 10))
    assert z.gespielt == ["licht:blitz"] and "direkt nach" in z.gefragt[0][0]
    z._regie_wahl = "keiner"
    z.regie.belegt_bis = 0
    asyncio.run(z.regie.nach_satz("Noch was", 10))
    assert z.gespielt == ["licht:blitz"]


def test_entscheidung_verworfen_wenn_inzwischen_geklingelt(z, monkeypatch):
    z.besuch.zustand = "tuer"

    async def fragen(self, anlass, satz, aus):
        self.z.besuch.zustand = "geklingelt"
        return "donner", "Spannung"

    monkeypatch.setattr(Regie, "_fragen", fragen)
    assert asyncio.run(z.regie.entscheiden("Pause")) == "keiner"
    assert z.gespielt == [] and "verworfen" in z.regie.verlauf[0]["grund"]


def test_fehler_bedeutet_kein_effekt(z, monkeypatch):
    async def fragen(self, anlass, satz, aus):
        raise RuntimeError("Sprachmodell: HTTP 500")

    monkeypatch.setattr(Regie, "_fragen", fragen)
    z.besuch.zustand = "gelockt"
    assert asyncio.run(z.regie.entscheiden("Pause")) == ""
    assert z.gespielt == [] and z.regie.verlauf[0]["grund"].startswith("Fehler")


def test_api_schalter():
    with TestClient(app) as c:
        assert c.post("/api/regie", json={"an": True}).status_code == 200
        assert c.get("/api/status").json()["zustand"]["regie_an"] is True
        assert "regie" in c.get("/api/ki/status").json()
        c.post("/api/regie", json={"an": False})
        assert c.get("/api/status").json()["zustand"]["regie_an"] is False
