"""Ereignis-Generator: legt in zufälligen Abständen Effekte und Szenen über das Ambiente.

Jede Regel hat ihren eigenen Zufallstakt (min_s..max_s). Zwischen zwei Ereignissen liegt
mindestens min_abstand_s, und während eine Szene läuft, passiert nichts anderes.
Effekt-Regeln richten sich nach dem laufenden Ambiente-Stück (abstimmung.py): nicht in Gesang (außer
erlaubten Kategorien), lieber in ruhigen Stellen, auf den Schlag, häufiger/seltener nach Stimmung.
"""

import asyncio
import logging
import random
import time
from typing import TYPE_CHECKING

from . import abstimmung
from .modelle import Effekt, Regel

# laut/schreckhaft: kommen ohne Besucher nicht per Zufall (Ereignisse.schreck_nur_bei_besuch)
NUR_BEI_BESUCH = {"schrei", "kettensaege", "zombie", "klopfen", "spieluhr", "schreck", "strobo-salve", "werwolf",
                  "herzschlag-licht", "donner-aus-dem-dunkel"}

if TYPE_CHECKING:
    from .zentrale import Zentrale

log = logging.getLogger(__name__)


class Generator:
    def __init__(self, z: "Zentrale") -> None:
        self.z = z
        self.naechstes: dict[str, float] = {}
        self.letztes = 0.0
        self.vorgemerkt: dict[str, Effekt] = {}       # Regel-ID -> gewählter Sound, der auf die Musik wartet
        self.pause_gewartet: set[str] = set()         # einmal auf eine ruhige Stelle gewartet

    def neu_planen(self) -> None:
        self.naechstes.clear()
        self.vorgemerkt.clear()
        self.pause_gewartet.clear()

    def _abstand(self, r: Regel) -> float:
        roh = random.uniform(r.min_s, max(r.min_s, r.max_s))
        if r.typ != "kategorie":
            return roh
        stueck, _ = self.z.musik_lage()
        return roh / abstimmung.gewicht(stueck, r.ziel, self.z.einst.musik)

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
            if z.zustand.gruselstufe < r.gruselstufe_min or (
                    z.ereignisse.schreck_nur_bei_besuch and r.ziel in NUR_BEI_BESUCH):
                self.naechstes[r.id] = jetzt + self._abstand(r)
                continue
            beschaeftigt = ((z.spieler and z.spieler.laufend) or jetzt - self.letztes < z.ereignisse.min_abstand_s
                            or z.besuch.aktiv        # Besucher werden angesprochen: kein Donner dazwischen
                            or jetzt < z.spieluhr_bis)  # Spieluhr ist eigene Musik: nichts drüber
            if beschaeftigt:
                self.naechstes[r.id] = jetzt + random.uniform(5, 20)
                continue
            ergebnis = await self._ausloesen(r)
            if isinstance(ergebnis, float):          # Musik passt noch nicht: so lange warten, dann neu fragen
                self.naechstes[r.id] = jetzt + ergebnis
                continue
            if ergebnis:
                self.letztes = jetzt
            self.naechstes[r.id] = jetzt + self._abstand(r)

    async def _ausloesen(self, r: Regel) -> bool:
        z = self.z
        if r.typ == "kategorie":
            return await self._effekt(r)
        try:
            await z.szene_starten(r.ziel, "Zufall")
            return True
        except KeyError:
            return False

    async def _effekt(self, r: Regel) -> bool | float:
        """Zufalls-Effekt der Regel, abgestimmt auf die Musik. True/False = ausgelöst/kein Sound vorhanden,
        float = so viele Sekunden warten und neu fragen."""
        z = self.z
        f = self.vorgemerkt.get(r.id) or z.fx_waehlen(r.ziel)
        if f is None:
            return False
        stueck, pos = z.musik_lage()
        plan = abstimmung.planen(stueck, pos, r.ziel, f.dauer_ms / 1000, z.einst.musik, r.id in self.pause_gewartet)
        if not plan.jetzt:
            self.vorgemerkt[r.id] = f
            if plan.grund == "ruhige Stelle":
                self.pause_gewartet.add(r.id)
            log.debug("Zufall %s wartet %.1f s (%s)", f.name, plan.warten_s, plan.grund)
            return float(plan.warten_s)
        self.vorgemerkt.pop(r.id, None)
        self.pause_gewartet.discard(r.id)
        if plan.verzoegerung_s > 0:
            await asyncio.sleep(plan.verzoegerung_s)      # höchstens 1,5 s (Takt) – der Generator-Takt ist 1 s
        if stueck is not None and pos is not None:
            log.info("Zufall %s bei %s %.1f s (%s)", f.name, stueck.name, pos + plan.verzoegerung_s, plan.grund)
        await z.fx_spielen(f, r.vol, quelle="Zufall")
        return True
