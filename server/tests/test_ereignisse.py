import random

from app.ereignisse import Generator
from app.modelle import Effekt, Regel
from app.zentrale import Zentrale


class FakeZentrale(Zentrale):
    def __init__(self):
        super().__init__()
        self.gespielt: list[str] = []

    async def fx_kategorie(self, kategorie, vol=100, quelle=""):
        self.gespielt.append(kategorie)
        return Effekt(id="x", name="x", kategorie=kategorie)

    def fx_waehlen(self, kategorie):
        return Effekt(id="x", name="x", kategorie=kategorie, dauer_ms=3000)

    async def fx_spielen(self, f, vol=100, licht="auto", quelle=""):
        self.gespielt.append(f.kategorie)

    async def szene_starten(self, sid, quelle=""):
        self.gespielt.append("szene:" + sid)


async def test_generator_haelt_abstand_und_gruselstufe():
    random.seed(1)
    z = FakeZentrale()
    z.zustand.ereignisse_an = True
    z.zustand.gruselstufe = 1
    z.ereignisse.min_abstand_s = 30
    z.ereignisse.regeln = [
        Regel(id="a", ziel="donner", min_s=10, max_s=20, gruselstufe_min=1),
        Regel(id="b", ziel="schrei", min_s=10, max_s=20, gruselstufe_min=3),
    ]
    g = Generator(z)
    zeitpunkte = []
    for t in range(0, 600):
        vorher = len(z.gespielt)
        await g.tick(float(t))
        if len(z.gespielt) > vorher:
            zeitpunkte.append(t)
    assert "schrei" not in z.gespielt  # Gruselstufe zu niedrig
    assert z.gespielt.count("donner") >= 10
    assert all(b - a >= 30 for a, b in zip(zeitpunkte, zeitpunkte[1:]))


async def test_generator_still_wenn_aus():
    z = FakeZentrale()
    z.zustand.ereignisse_an = False
    g = Generator(z)
    for t in range(0, 2000, 5):
        await g.tick(float(t))
    assert z.gespielt == []


# ---------------------------------------------------------------------------- Abstimmung auf die Musik
from app import abstimmung  # noqa: E402
from app.modelle import MusikAbstimmung, MusikAnalyse, Musikstueck  # noqa: E402


def _stueck(**kw):
    a = MusikAnalyse(version=1, modell=True, stimme=[(10.0, 30.0)], ruhig=[(40.0, 45.0)], bpm=120.0, beat0_s=0.25,
                     gewichte={"wind": 1.5, "kettensaege": 0.4})
    return Musikstueck(id="m", name="Lied", dauer_s=60, analyse=a, **kw)


def test_kein_effekt_in_gesang_ausser_erlaubten():
    e = MusikAbstimmung()
    p = abstimmung.planen(_stueck(), 12.0, "schrei", 2.0, e)
    assert not p.jetzt and p.grund == "Gesang" and 18 <= p.warten_s <= 19.5      # bis nach dem Gesang
    p = abstimmung.planen(_stueck(), 8.5, "schrei", 2.0, e)                     # würde in den Gesang hineinragen
    assert not p.jetzt and p.grund == "Gesang"
    assert abstimmung.planen(_stueck(), 12.0, "donner", 2.0, e).jetzt           # Donner darf über Gesang
    assert abstimmung.planen(_stueck(), 12.0, "wind", 2.0, e).jetzt
    # von Hand korrigiert: kein Gesang -> passt
    assert abstimmung.planen(_stueck(stimme_manuell=[]), 12.0, "schrei", 2.0, e).jetzt


def test_wartet_einmal_auf_ruhige_stelle_und_legt_auf_den_schlag():
    e = MusikAbstimmung()
    p = abstimmung.planen(_stueck(), 33.0, "kette", 1.0, e)
    assert not p.jetzt and p.grund == "ruhige Stelle" and 6 <= p.warten_s <= 7
    p = abstimmung.planen(_stueck(), 33.0, "kette", 1.0, e, pause_gewartet=True)
    assert p.jetzt                                                             # nicht ewig warten
    p = abstimmung.planen(_stueck(), 41.0, "kette", 1.0, e)                    # mitten in der ruhigen Stelle
    assert p.jetzt
    start = 41.0 + abstimmung.VORLAUF_S + p.verzoegerung_s
    assert abs(((start - 0.25) / 0.5) - round((start - 0.25) / 0.5)) < 1e-6    # genau auf einem Schlag (120 BPM)
    assert p.verzoegerung_s < 0.5


def test_aus_oder_ohne_musik_sofort_und_gewichte():
    assert abstimmung.planen(_stueck(), 12.0, "schrei", 2.0, MusikAbstimmung(aktiv=False)).jetzt
    assert abstimmung.planen(None, None, "schrei", 2.0, MusikAbstimmung()).jetzt
    e = MusikAbstimmung()
    assert abstimmung.gewicht(_stueck(), "wind", e) == 1.5
    assert abstimmung.gewicht(_stueck(), "kettensaege", e) == 0.5             # begrenzt
    assert abstimmung.gewicht(_stueck(), "wind", MusikAbstimmung(stimmung=False)) == 1.0


async def test_generator_wartet_auf_das_ende_des_gesangs():
    z = FakeZentrale()
    z.zustand.ereignisse_an = True
    z.ereignisse.min_abstand_s = 0
    z.ereignisse.regeln = [Regel(id="a", ziel="schrei", min_s=5, max_s=5)]
    z.ereignisse.schreck_nur_bei_besuch = False       # hier geht es um die Abstimmung auf den Gesang
    pos = {"t": 0.0}
    z.musik_lage = lambda: (_stueck(), pos["t"])
    g = Generator(z)
    g.naechstes["a"] = 12.0
    for t in range(0, 60):
        pos["t"] = float(t)
        vorher = len(z.gespielt)
        await g.tick(float(t))
        if len(z.gespielt) > vorher:
            break
    assert z.gespielt == ["schrei"] and t >= 30          # erst nach dem Gesang (10–30 s)



def test_schreck_ohne_besucher_nicht_per_zufall():
    from app.ereignisse import NUR_BEI_BESUCH
    assert {"schrei", "kettensaege", "spieluhr", "schreck", "werwolf"} <= NUR_BEI_BESUCH
    assert not {"wind", "kraehe", "fluestern", "glocke", "donner"} & NUR_BEI_BESUCH
