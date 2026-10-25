"""Licht-Ausgang als austauschbare Schicht.

1.0: EspLicht – der Licht-ESP mit den MOSFET-Zonen.
Später (Weihnachten): ein WLED-Ausgang mit derselben Schnittstelle.
"""

from abc import ABC, abstractmethod

from .geraete import Geraete
from .modelle import LichtAktion, LichtEinst, LichtZustand


class LichtAusgang(ABC):
    name = "?"

    @property
    @abstractmethod
    def verbunden(self) -> bool: ...

    @abstractmethod
    async def konfigurieren(self, einst: LichtEinst) -> None: ...

    @abstractmethod
    async def zustand(self, z: LichtZustand, temp: bool = False) -> None:
        """temp: nur kurz (Szene) – nicht als Notfall-Zustand merken."""

    @abstractmethod
    async def aktion(self, a: LichtAktion, verz_ms: int, dauer_ms: int) -> None: ...

    @abstractmethod
    async def stopp(self) -> None: ...

    @abstractmethod
    async def vorschau(self, an: bool) -> None: ...

    async def neustart(self) -> None:
        return None


class EspLicht(LichtAusgang):
    name = "Licht-ESP"

    def __init__(self, geraete: Geraete) -> None:
        self.geraete = geraete

    @property
    def verbunden(self) -> bool:
        return self.geraete.verbunden("licht")

    async def _senden(self, nachricht: dict) -> None:
        await self.geraete.senden("licht", nachricht)

    async def konfigurieren(self, einst: LichtEinst) -> None:
        await self._senden({
            "t": "config",
            "strobo_max_hz": einst.strobo_max_hz,
            "gamma": einst.gamma,
            "zone_max": einst.zone_max[: einst.zonen],
        })

    async def zustand(self, z: LichtZustand, temp: bool = False) -> None:
        nachricht = {"t": "zustand", **z.model_dump()}
        if temp:
            nachricht["temp"] = True
        await self._senden(nachricht)

    async def aktion(self, a: LichtAktion, verz_ms: int, dauer_ms: int) -> None:
        await self._senden({
            "t": "aktion", "name": a.name, "zonen": a.zonen, "verz_ms": verz_ms,
            "dauer_ms": dauer_ms, "staerke": a.staerke, "param": a.param,
        })

    async def stopp(self) -> None:
        await self._senden({"t": "stopp"})

    async def vorschau(self, an: bool) -> None:
        await self._senden({"t": "vorschau", "an": an})

    async def neustart(self) -> None:
        await self._senden({"t": "neustart"})
