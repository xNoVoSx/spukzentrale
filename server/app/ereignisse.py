"""Ereignis-Generator: legt in zufälligen Abständen Effekte und Szenen über das Ambiente.

Jede Regel hat ihren eigenen Zufallstakt (min_s..max_s). Zwischen zwei Ereignissen liegt
mindestens min_abstand_s, und während eine Szene läuft, passiert nichts anderes.
"""

import asyncio
import logging
import random
import time
from typing import TYPE_CHECKING

from .modelle import Regel

if TYPE_CHECKING:
    from .zentrale import Zentrale

log = logging.getLogger(__name__)


class Generator:
    def __init__(self, z: "Zentrale") -> None:
        self.z = z
        self.naechstes: dict[str, float] = {}
        self.letztes = 0.0

    def neu_planen(self) -> None:
        self.naechstes.clear()

    @staticmethod
    def _abstand(r: Regel) -> float:
        return random.uniform(r.min_s, max(r.min_s, r.max_s))

    async def lauf(self) -> None:
        while True:
            await asyncio.sleep(1)
            try:
                await self.tick(time.monotonic())
            except Exception:
                log.exception("Ereignis-Generator")

    async def tick(self, jetzt: float) -> None:
        z = self.z
        if not z.zustand.ereignisse_an or not z.zustand.licht.an:
            self.naechstes.clear()
            return
        for r in z.ereignisse.regeln:
            if not r.aktiv:
                continue
            if r.id not in self.naechstes:
                # erste Runde gestaffelt, damit nicht alles gleichzeitig kommt
                self.naechstes[r.id] = jetzt + self._abstand(r) * random.uniform(0.2, 1.0)
                continue
            if jetzt < self.naechstes[r.id]:
                continue
            if z.zustand.gruselstufe < r.gruselstufe_min:
                self.naechstes[r.id] = jetzt + self._abstand(r)
                continue
            beschaeftigt = (z.spieler and z.spieler.laufend) or jetzt - self.letztes < z.ereignisse.min_abstand_s
            if beschaeftigt:
                self.naechstes[r.id] = jetzt + random.uniform(5, 20)
                continue
            if await self._ausloesen(r):
                self.letztes = jetzt
            self.naechstes[r.id] = jetzt + self._abstand(r)

    async def _ausloesen(self, r: Regel) -> bool:
        z = self.z
        if r.typ == "kategorie":
            return await z.fx_kategorie(r.ziel, r.vol, quelle="Zufall") is not None
        try:
            await z.szene_starten(r.ziel, "Zufall")
            return True
        except KeyError:
            return False
