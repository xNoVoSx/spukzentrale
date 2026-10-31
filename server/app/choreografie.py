"""Licht-Choreografie zur Hintergrundmusik: Zonen gehen passend zur Musik aus und an, statt dass alles gleichmäßig
mitpulsiert (Novos: „wir machen zu wenig außer aufblitzen“; die Figuren hängen direkt am Netz, das Licht muss also
nicht ständig überall an sein).

Läuft nur, wenn die Grundbeleuchtung zur Musik gehört (Zonen-Effekte musik/spektrum/beat), die Show und das Ambiente
an sind, keine Szene/Effekt-Stimmung läuft und einstellbar (LichtEinst.choreografie).

Energie statt Zufall (Prüfbericht 31.10.: Helligkeit folgte der Lautstärke kaum, Drops lösten nichts aus): Die
Pegelkurve aus der Musikanalyse (0..100 je Sekunde) bestimmt die Energiestufe der kommenden Phrase gegen den Median
des Stücks – leise: 1–3 Zonen glimmen, mittel: wechselnde Teil-Muster, laut: alle/Spektrum/Schlag. Ein Drop (Pegel
springt in 2 s um ≥ 12) schaltet sofort alles an, ohne auf die Phrasengrenze zu warten. Wechsel sonst auf
Phrasengrenzen (16 Schläge, sonst ~10 s). Nie ganz schwarz: auch die ruhigste Stufe lässt alle Zonen schwach glühen.
Helligkeit/Tempo kommen aus der Grundbeleuchtung; grund_max hält alles unter den Effekten.
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
DROP_DB = 12                    # Pegelsprung (Analyse-Skala 0..100 ≈ 40 dB) innerhalb von 2 s = Drop
GLIMM_STAERKE, GLIMM_BASIS = 40, 25    # „aus“-Zonen vor grund_max (siehe muster)
LEISE, LAUT = -8, 3             # Phrasen-Pegel gegen den Median des Stücks
# Muster je Energiestufe, gewichtet nach der Stimmung des Stücks (siehe musikanalyse.STIMMUNGS_KLASSEN)
STUFEN = {
    "leise":  {"ruhig": 3, "mitte": 2, "geist": 2},
    "mittel": {"aussen_aus": 3, "wechsel": 2, "mitte": 1, "geist": 1, "spektrum": 1},
    "laut":   {"alle": 3, "spektrum": 2, "beat": 2, "aussen_aus": 1},
}
STIMMUNGS_BONUS = {"duester": {"geist": 2, "mitte": 1}, "ruhig": {"geist": 2, "ruhig": 1},
                   "dramatisch": {"beat": 2, "spektrum": 1}, "verspielt": {"wechsel": 2, "beat": 1}}
NAMEN = {"alle": "alle zur Musik", "aussen_aus": "außen dunkel", "mitte": "nur die Mitte", "wechsel": "jede zweite Zone",
         "spektrum": "Spektrum", "beat": "Schlag-Blitze", "geist": "ein Geist wandert", "ruhig": "ruhige Stelle",
         "gesang": "Gesang", "drop": "Drop – alles an"}


def muster(name: str, n: int, vorbild: Zone) -> list[Zone]:
    """Zonen-Muster aus der Grundbeleuchtung (Tempo, Stärke, Basis der ersten Zone)."""
    an = lambda e="musik", **k: Zone(**{"e": e, "tempo": vorbild.tempo, "staerke": vorbild.staerke,  # noqa: E731
                                        "basis": vorbild.basis, **k})
    # „aus“ heißt hier: schwach glimmen statt schwarz – Dunkelheit ohne Folge wirkt kaputt (Novos). Die Werte sind so
    # hoch, weil grund_max sie noch kürzt (55 % -> 22/14) und der ESP mit Gamma rechnet: 4 % wären an der Wand 0
    # (Prüfbericht 31.10. nachts), 14–22 % glühen bei Gamma 3,0 sichtbar, bleiben aber weit unter den Musik-Zonen.
    aus = Zone(e="atmen", tempo=15, staerke=GLIMM_STAERKE, basis=GLIMM_BASIS)
    mitte = set(range(max(0, n // 2 - 1), min(n, n // 2 + 2))) if n >= 5 else ({n // 2} if n % 2 else {n // 2 - 1, n // 2})
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
    if name == "ruhig":                         # ruhige Stelle: fast dunkel, eine Zone atmet deutlich
        k = random.randrange(n)
        return [an("atmen", tempo=20, staerke=max(60, vorbild.staerke * 3 // 4), basis=GLIMM_BASIS) if i == k
                else aus.model_copy()
                for i in range(n)]
    if name == "gesang":                        # Gesang: ruhiges Glühen in der Mitte, außen glimmend
        return [an() if i in mitte else aus.model_copy() for i in range(n)]
    if name == "drop":                          # Drop: alles auf einmal, mit Schlag
        return [an("beat" if i % 2 else "musik") for i in range(n)]
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
        stueck, pos = z.musik_lage()
        drop = self._drop(stueck, pos)
        if jetzt < self.naechster_wechsel and not (drop and self.aktuell != "drop"):
            return
        if drop and self.aktuell != "drop":
            name, bis_s = "drop", self._bis_phrase(stueck.analyse, pos)
        else:
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

    @staticmethod
    def _phrase(a) -> float:
        if not a or not a.bpm:
            return PHRASE_OHNE_TAKT_S
        phrase = SCHLAEGE_PRO_PHRASE * 60.0 / a.bpm
        while phrase > 16:
            phrase /= 2
        while phrase < 6:
            phrase *= 2
        return phrase

    def _bis_phrase(self, a, pos) -> float:
        """Sekunden bis zur nächsten Phrasengrenze (auf dem Schlag)."""
        phrase = self._phrase(a)
        if not a or not a.bpm or pos is None:
            return phrase
        rest = phrase - ((pos - a.beat0_s) % phrase)
        return rest if rest > 2 else rest + phrase

    @staticmethod
    def _drop(stueck, pos) -> bool:
        a = stueck.analyse if stueck else None
        if not a or not a.pegel or pos is None:
            return False
        i = int(pos)
        if i < 2 or i >= len(a.pegel):
            return False
        return a.pegel[i] - min(a.pegel[i - 2:i]) >= DROP_DB

    @staticmethod
    def _energie(a, pos: float, dauer: float) -> str:
        """leise/mittel/laut: Pegel der kommenden Phrase gegen den Median des Stücks (bzw. Anstieg zur Vorphrase)."""
        if not a.pegel:
            return "mittel"
        i0, i1 = int(pos), max(int(pos) + 1, int(pos + dauer))
        teil = a.pegel[i0:i1] or a.pegel[-1:]
        vorher = a.pegel[max(0, i0 - int(dauer)):i0] or teil
        median = sorted(a.pegel)[len(a.pegel) // 2]
        jetzt = sum(teil) / len(teil)
        if jetzt - median <= LEISE:
            return "leise"
        if jetzt - median >= LAUT or jetzt - sum(vorher) / len(vorher) >= 10:
            return "laut"
        return "mittel"

    def _waehlen(self, stueck, pos) -> tuple[str, float]:
        """(Muster, Sekunden bis zum nächsten Wechsel) passend zur Stelle im Stück."""
        a = stueck.analyse if stueck else None
        if a is None or pos is None:
            return self._zufall("mittel", "duester"), PHRASE_OHNE_TAKT_S
        bis = self._bis_phrase(a, pos)
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
        return self._zufall(self._energie(a, pos, bis), a.stimmung), max(1.0, bis)

    def _zufall(self, stufe: str, stimmung: str) -> str:
        g = dict(STUFEN[stufe])
        for k, v in STIMMUNGS_BONUS.get(stimmung, {}).items():
            if k in g:
                g[k] += v
        g = {k: v for k, v in g.items() if k != self.aktuell} or g
        return random.choices(list(g), weights=list(g.values()))[0]
