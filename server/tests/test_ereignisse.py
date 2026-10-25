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
