"""Licht-Choreografie zur Hintergrundmusik: Zonen gehen passend zur Musik aus und an, statt dass alles gleichmäßig
mitpulsiert (Novos: „wir machen zu wenig außer aufblitzen“; die Figuren hängen direkt am Netz, das Licht muss also
nicht ständig überall an sein).

Läuft nur, wenn die Grundbeleuchtung zur Musik gehört (Zonen-Effekte musik/spektrum/beat), die Show und das Ambiente
an sind, keine Szene/Effekt-Stimmung läuft und einstellbar (LichtEinst.choreografie). Die Muster wechseln auf
Phrasengrenzen (16 Schläge, sonst ~10 s), an ruhigen Stellen wird es fast dunkel, im Gesang ruhiger, sonst je nach
Stimmung des Stücks. Helligkeit/Tempo kommen aus der Grundbeleuchtung; grund_max hält alles unter den Effekten.
"""

import asyncio
import logging
import random
import time
from typing import TYPE_CHECKING

from .modelle import Stimmung, Zone

if TYPE_CHECKING:
    from .zentrale import Zentrale

log = logging.getLogger(__name__)

MUSIK_EFFEKTE = {"musik", "spektrum", "beat"}
SCHLAEGE_PRO_PHRASE = 16
PHRASE_OHNE_TAKT_S = 10.0
# Gewichte je Stimmung des Stücks (siehe musikanalyse.STIMMUNGS_KLASSEN)
GEWICHTE = {
    "duester":    {"alle": 1, "aussen_aus": 2, "mitte": 3, "wechsel": 2, "spektrum": 1, "beat": 1, "geist": 3},
    "ruhig":      {"alle": 1, "aussen_aus": 2, "mitte": 3, "wechsel": 1, "spektrum": 1, "beat": 0, "geist": 3},
    "dramatisch": {"alle": 2, "aussen_aus": 2, "mitte": 1, "wechsel": 2, "spektrum": 3, "beat": 3, "geist": 1},
    "verspielt":  {"alle": 2, "aussen_aus": 1, "mitte": 1, "wechsel": 3, "spektrum": 2, "beat": 2, "geist": 1},
}
NAMEN = {"alle": "alle zur Musik", "aussen_aus": "außen dunkel", "mitte": "nur die Mitte", "wechsel": "jede zweite Zone",
         "spektrum": "Spektrum", "beat": "Schlag-Blitze", "geist": "ein Geist wandert", "ruhig": "ruhige Stelle",
         "gesang": "Gesang"}


def muster(name: str, n: int, vorbild: Zone) -> list[Zone]:
    """Zonen-Muster aus der Grundbeleuchtung (Tempo, Stärke, Basis der ersten Zone)."""
    an = lambda e="musik", **k: Zone(**{"e": e, "tempo": vorbild.tempo, "staerke": vorbild.staerke,  # noqa: E731
                                        "basis": vorbild.basis, **k})
    aus = Zone(e="aus", tempo=50, staerke=0, basis=0)
    mitte = {n // 2} if n % 2 else {n // 2 - 1, n // 2}
    if name == "alle":
        return [an() for _ in range(n)]
    if name == "aussen_aus":
        return [aus.model_copy() if i in (0, n - 1) and n > 2 else an() for i in range(n)]
    if name == "mitte":
        return [an() if i in mitte else aus.model_copy() for i in range(n)]
    if name == "wechsel":
        return [an() if i % 2 == 0 else aus.model_copy() for i in range(n)]
    if name == "spektrum":
        return [an("spektrum") for _ in range(n)]
    if name == "beat":
        return [an("beat") if i % 2 else aus.model_copy() for i in range(n)]
    if name == "geist":
        return [an("wandern", tempo=25, basis=0) for _ in range(n)]
    if name == "ruhig":                         # ruhige Stelle: fast alles dunkel, eine Zone atmet
        k = random.randrange(n)
        return [an("atmen", tempo=20, staerke=max(20, vorbild.staerke // 2), basis=0) if i == k else aus.model_copy()
                for i in range(n)]
    if name == "gesang":                        # Gesang: ruhiges Glühen in der Mitte, außen dunkel
        return [an() if i in mitte or (n > 3 and i in (n // 2 - 1, n // 2 + 1)) else aus.model_copy()
                for i in range(n)]
    return [an() for _ in range(n)]


class Choreografie:
    def __init__(self, z: "Zentrale") -> None:
        self.z = z
        self.aktuell = ""
        self.naechster_wechsel = 0.0      # monotonic

    def aktiv(self) -> bool:
        z = self.z
        zonen = z.zustand.licht.zonen
        return (z.einst.licht.choreografie and z.zustand.licht.an and z.zustand.ambiente_an and bool(zonen)
                and all(zz.e in MUSIK_EFFEKTE for zz in zonen))

    async def lauf(self) -> None:
        while True:
            await asyncio.sleep(0.5)
            try:
                await self.takt(time.monotonic())
            except Exception:  # noqa: BLE001
                log.exception("Choreografie")

    async def takt(self, jetzt: float) -> None:
        z = self.z
        if not self.aktiv():
            if z.choreo is not None:
                z.choreo, self.aktuell = None, ""
                await z._licht_senden()
            return
        if jetzt < self.naechster_wechsel:
            return
        stueck, pos = z.musik_lage()
        name, bis_s = self._waehlen(stueck, pos)
        self.naechster_wechsel = jetzt + bis_s
        if name == self.aktuell:
            return
        self.aktuell = name
        n = z.einst.licht.zonen
        z.choreo = Stimmung(id="choreo", name=NAMEN.get(name, name), zonen=muster(name, n, z.zustand.licht.zonen[0]))
        log.debug("Choreografie: %s (nächster Wechsel in %.1f s)", name, bis_s)
        if z.overlay is None:
            await z._licht_senden()

    def _waehlen(self, stueck, pos) -> tuple[str, float]:
        """(Muster, Sekunden bis zum nächsten Wechsel) passend zur Stelle im Stück."""
        a = stueck.analyse if stueck else None
        if a is None or pos is None:
            return self._zufall("duester"), PHRASE_OHNE_TAKT_S
        # Länge einer Phrase und Zeit bis zur nächsten Phrasengrenze (auf dem Schlag)
        if a.bpm:
            phrase = SCHLAEGE_PRO_PHRASE * 60.0 / a.bpm
            while phrase > 16:
                phrase /= 2
            while phrase < 6:
                phrase *= 2
            rest = phrase - ((pos - a.beat0_s) % phrase)
            bis = rest if rest > 2 else rest + phrase
        else:
            bis = PHRASE_OHNE_TAKT_S
        for b0, b1 in a.ruhig:
            if b0 <= pos < b1:
                return "ruhig", max(1.0, min(bis, b1 - pos))
            if pos < b0 < pos + bis:                       # ruhige Stelle beginnt vor dem nächsten Wechsel
                bis = b0 - pos
        for g0, g1 in stueck.stimme:
            if g0 <= pos < g1:
                return "gesang", max(1.0, min(bis, g1 - pos))
            if pos < g0 < pos + bis:
                bis = g0 - pos
        return self._zufall(a.stimmung), max(1.0, bis)

    def _zufall(self, stimmung: str) -> str:
        g = {k: v for k, v in GEWICHTE.get(stimmung, GEWICHTE["duester"]).items() if v and k != self.aktuell}
        return random.choices(list(g), weights=list(g.values()))[0]
