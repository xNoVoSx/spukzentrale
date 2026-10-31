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
        return self.z._regie_wahl, "passt", "Test"

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
    assert "fx_d1" in aus and "keiner" in aus and "licht_blitz" in aus and "Donner" in aus["fx_d1"]
    assert "fx_k1" not in aus and "fx_s1" not in aus and "fx_l1" not in aus     # zu lang / Stimme / Stufe 3
    z.zustand.gruselstufe = 3
    assert "fx_l1" in z.regie.optionen()


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
    asyncio.run(z.regie.takt(time.monotonic() + 999))       # niemand mehr im Bild: nichts
    assert z.gefragt == []
    z.besuch.an["fern"] = True
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
        self.z.besuch.klingel()                  # während der Denkzeit geklingelt
        return "donner", "Spannung", "Test"

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


def test_claude_entscheidet_und_gemma_springt_ein(z, monkeypatch):
    """Ohne Schlüssel: lokal. Mit Schlüssel: Claude bekommt nur Text; Fehler/Ablehnung -> lokales Modell."""
    monkeypatch.undo()                     # echtes _fragen
    z.medien.fx_der_kategorie = lambda k: [Effekt(id="d1", name="Donner", kategorie="donner", dauer_ms=6000)] if k == "donner" else []
    lokal, claude = [], []

    async def lokal_fragen(self, text, schema, aus):
        lokal.append(text)
        return "licht_blitz", "lokal"

    class Antwort:
        def __init__(self, text, stop="end_turn"):
            self.stop_reason = stop
            self.content = [type("B", (), {"type": "text", "text": text})()]

    class Nachrichten:
        def __init__(self):
            self.antwort = Antwort('{"effekt": "donner", "grund": "Spannung"}')

        async def create(self, **kw):
            claude.append(kw)
            if isinstance(self.antwort, Exception):
                raise self.antwort
            return self.antwort

    nachrichten = Nachrichten()
    monkeypatch.setattr(Regie, "_lokal_fragen", lokal_fragen)
    z.regie._claude = type("C", (), {"beta": type("B", (), {"messages": nachrichten})()})()
    aus = {"keiner": "-", "donner": "Donner", "licht_blitz": "Blitz"}

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert asyncio.run(z.regie._fragen("Pause", "", aus))[2] == "Gemma (lokal)" and not claude
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    e, grund, modell = asyncio.run(z.regie._fragen("Pause", "Komm näher", aus))
    assert (e, modell) == ("donner", "Claude Opus 5.5")
    kw = claude[-1]
    assert kw["model"] == "claude-opus-5-5" and kw["output_config"]["effort"] == "low"
    assert kw["output_config"]["format"]["schema"]["properties"]["effekt"]["enum"] == list(aus)
    inhalt = kw["messages"][0]["content"]
    assert isinstance(inhalt, str) and "Komm näher" in inhalt          # nur Text, kein Bild
    nachrichten.antwort = Antwort("", stop="refusal")
    assert asyncio.run(z.regie._fragen("Pause", "", aus))[2] == "Gemma (lokal)"
    nachrichten.antwort = TimeoutError()
    assert asyncio.run(z.regie._fragen("Pause", "", aus))[:2] == ("licht_blitz", "lokal")
    z.regie._claude = None


def test_feste_regeln_schreck_und_musik(z):
    z.zustand.ambiente_an = True
    z.besuch.zustand = "gelockt"
    z.medien.fx_der_kategorie = lambda k: [Effekt(id=k, name=k, kategorie=k, dauer_ms=3000)]
    z.medien.fx = lambda fid: Effekt(id=fid, name=fid, kategorie=fid, dauer_ms=3000)
    z.ereignisse.regeln.append(Regel(id="s", typ="kategorie", ziel="schrei"))
    z.zustand.gruselstufe = 3
    assert "fx_schrei" in z.regie.optionen() and "musik_stille" in z.regie.optionen()
    z._regie_wahl = "fx_schrei"
    asyncio.run(z.regie.entscheiden("Pause"))
    assert z.gespielt[-1] == "schrei" and z.regie.verlauf[0]["name"] == "Schrei: schrei"
    assert "fx_schrei" not in z.regie.optionen()              # nach dem Schreck erst mal nichts Heftiges
    assert "fx_donner" in z.regie.optionen()
    z.regie.belegt_bis = 0
    z._regie_wahl = "musik_stille"
    asyncio.run(z.regie.entscheiden("Pause"))
    asyncio.run(z.musik_normal(sofort=True))
    assert "musik_stille" not in z.regie.optionen()           # einmal pro Besuch
    z.besuch._neu()
    assert "musik_stille" in z.regie.optionen()
    z.zustand.ambiente_an = False


def test_an_der_tuer_erst_spannung_dann_klingel(z):
    """Vor dem Klingeln an der Tür nichts Heftiges (der Schreck gehört der Klingel) – außer sie zögern lange."""
    z.medien.fx_der_kategorie = lambda k: [Effekt(id=k, name=k, kategorie=k, dauer_ms=3000)]
    z.medien.fx = lambda fid: Effekt(id=fid, name=fid, kategorie=fid, dauer_ms=3000)
    z.ereignisse.regeln.append(Regel(id="s", typ="kategorie", ziel="schrei"))
    z.zustand.gruselstufe = 3
    z.besuch.zustand, z.besuch.seit = "tuer", time.time()
    aus = z.regie.optionen()
    assert "fx_schrei" not in aus and "fx_donner" in aus and "licht_blackout" not in aus
    z.besuch.seit = time.time() - 25
    assert "fx_schrei" in z.regie.optionen()


def test_regie_macht_nach_dem_klingeln_weiter(z, monkeypatch):
    """Novos 31.10.: die KI-Stimme schweigt nach dem Klingeln, der Regisseur überrascht weiter – erst nach dem
    Klingel-Schreck und anfangs nichts Heftiges."""
    import time
    from app import regie as regie_mod
    anlaesse = []

    async def entscheiden(self, anlass, satz=""):
        anlaesse.append(anlass)
        return "keiner"

    monkeypatch.setattr(Regie, "entscheiden", entscheiden)
    b, r = z.besuch, z.regie
    z.zustand.regie_an = z.zustand.ki_an = True
    b.klingel()
    b.an["nah"] = True
    asyncio.run(r.takt(time.monotonic()))
    assert anlaesse == []                                      # Klingel-Schreck wirkt allein
    assert not any(r._heftig(e) for e in r.optionen())         # anfangs nichts Heftiges
    b.klingel_zeit -= regie_mod.KLINGEL_RUHE_S + 1
    r.letzte_aktion = r.ki_ende = 0.0
    asyncio.run(r.takt(time.monotonic()))
    assert len(anlaesse) == 1 and anlaesse[0].startswith("Nach dem Klingeln")
