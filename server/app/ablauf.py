"""Szenen abspielen: Schritte auf einer Zeitleiste (Sound, Licht-Aktion, Stimmung)."""

import asyncio
import contextlib
import logging
import time
from typing import TYPE_CHECKING, Any

from .modelle import Schritt, Szene

if TYPE_CHECKING:
    from .zentrale import Zentrale

log = logging.getLogger(__name__)


class SzenenSpieler:
    def __init__(self, z: "Zentrale") -> None:
        self.z = z
        self.task: asyncio.Task | None = None
        self.laufend: dict[str, Any] | None = None

    async def starten(self, s: Szene, quelle: str = "") -> None:
        await self.stoppen()
        self.laufend = {"id": s.id, "name": s.name, "start": time.time(), "quelle": quelle,
                        "ende": time.monotonic() + self._zeitleiste(s)[1] / 1000}
        self.task = asyncio.create_task(self._ablauf(s), name=f"szene-{s.id}")
        self.z.protokollieren(f"{quelle or 'Hand'}: Szene „{s.name}“", "szene")
        self.z._zustand_geaendert()

    async def stoppen(self) -> None:
        if self.task and not self.task.done():
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.task
        self.task = None

    def _zeitleiste(self, s: Szene) -> tuple[list[tuple[int, int, Schritt | None]], int]:
        """Ereignisse (Zeit, Reihenfolge, Schritt) – Schritt None = Stimmung beenden – und Gesamtdauer."""
        ereignisse: list[tuple[int, int, Schritt | None]] = []
        ende = 0
        for n, sch in enumerate(s.schritte):
            ereignisse.append((sch.t_ms, n, sch))
            if sch.typ == "stimmung":
                ereignisse.append((sch.t_ms + sch.dauer_ms, n, None))
                ende = max(ende, sch.t_ms + sch.dauer_ms)
            elif sch.typ == "musik":
                ende = max(ende, sch.t_ms + sch.dauer_ms)
            elif sch.typ == "aktion" and sch.aktion:
                ende = max(ende, sch.t_ms + (sch.aktion.dauer_ms or 1000))
            elif sch.typ == "fx":
                ende = max(ende, sch.t_ms + self._fx_dauer(sch))
        ereignisse.sort(key=lambda e: (e[0], e[2] is not None, e[1]))  # erst beenden, dann starten
        return ereignisse, ende

    def _fx_dauer(self, sch: Schritt) -> int:
        m = self.z.medien
        if sch.fx_id and (f := m.fx(sch.fx_id)):
            return f.dauer_ms
        if sch.kategorie:
            return max((f.dauer_ms for f in m.fx_der_kategorie(sch.kategorie)), default=0)
        return 0

    def dauer_ms(self, s: Szene) -> int:
        return self._zeitleiste(s)[1]

    def rest_s(self) -> float:
        """Sekunden, bis die laufende Szene vorbei ist (0 = keine)."""
        return max(0.0, self.laufend["ende"] - time.monotonic()) if self.laufend else 0.0

    async def _ablauf(self, s: Szene) -> None:
        z = self.z
        musik = False
        ereignisse, ende = self._zeitleiste(s)
        start = time.monotonic()
        try:
            for t_ms, _, sch in ereignisse:
                warten = start + t_ms / 1000 - time.monotonic()
                if warten > 0:
                    await asyncio.sleep(warten)
                try:
                    if sch is None:
                        await z.overlay_setzen(None)
                    else:
                        await self._schritt(sch)
                        musik = musik or sch.typ == "musik"
                except Exception:
                    log.exception("Szene %s: Schritt fehlgeschlagen", s.id)
            rest = start + ende / 1000 - time.monotonic()
            if rest > 0:
                await asyncio.sleep(rest)
        finally:
            if z.overlay is not None:
                await z.overlay_setzen(None)
            if musik:
                await z.musik_normal()    # auch wenn die Szene abgebrochen wurde
            self.laufend = None
            z._zustand_geaendert()

    async def _schritt(self, sch: Schritt) -> None:
        z = self.z
        if sch.typ == "fx":
            f = z.medien.fx(sch.fx_id) if sch.fx_id else None
            if not f and sch.kategorie:
                f = z.fx_waehlen(sch.kategorie)
            if not f:
                return  # Kategorie (noch) leer -> Schritt auslassen
            licht = sch.aktion if sch.licht == "eigen" else sch.licht
            await z.fx_spielen(f, sch.vol, licht)
        elif sch.typ == "aktion" and sch.aktion:
            # gleiche Zeitleiste wie der Ton: Licht um die Ausgabe-Latenz verzögern
            await z.aktion(sch.aktion, verz_ms=z.ton_effektiv().latenz_ms)
        elif sch.typ == "musik":
            await z.musik_daempfen(sch.musik, sch.dauer_ms)
        elif sch.typ == "stimmung" and sch.stimmung_id:
            st = next((x for x in z.stimmungen.liste if x.id == sch.stimmung_id), None)
            if st:
                await z.overlay_setzen(st)
