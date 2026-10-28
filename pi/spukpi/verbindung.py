"""WebSocket zum Server (/ws/geraet?rolle=…) mit automatischem Neuverbinden."""

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable

import websockets

log = logging.getLogger(__name__)


class Verbindung:
    def __init__(self, rolle: str, url: Callable[[], str], bei_nachricht: Callable[[dict], Awaitable[None]],
                 bei_verbunden: Callable[[], Awaitable[None]] | None = None) -> None:
        self.rolle = rolle
        self.url = url
        self.bei_nachricht = bei_nachricht
        self.bei_verbunden = bei_verbunden
        self._ws = None
        self.trennungen = 0
        self.gewollt = True            # False: Verbindung beenden und nicht neu aufbauen

    @property
    def verbunden(self) -> bool:
        return self._ws is not None

    async def senden(self, nachricht: dict) -> bool:
        ws = self._ws
        if ws is None:
            return False
        try:
            await ws.send(json.dumps(nachricht, separators=(",", ":"), ensure_ascii=False))
            return True
        except Exception as e:
            log.debug("%s: senden fehlgeschlagen: %s", self.rolle, e)
            return False

    async def trennen(self) -> None:
        ws = self._ws
        if ws is not None:
            await ws.close()

    async def laufen(self, darf: Callable[[], bool] = lambda: True) -> None:
        """Hält die Verbindung, solange darf() True ist (sonst wird getrennt und gewartet)."""
        pause = 1.0
        while True:
            if not darf():
                await asyncio.sleep(1)
                continue
            try:
                async with websockets.connect(self.url(), ping_interval=15, ping_timeout=10, open_timeout=10,
                                              max_size=2**22) as ws:
                    self._ws = ws
                    pause = 1.0
                    log.info("%s: mit dem Server verbunden", self.rolle)
                    if self.bei_verbunden:
                        await self.bei_verbunden()
                    waechter = asyncio.create_task(self._waechter(ws, darf))
                    try:
                        async for text in ws:
                            try:
                                msg = json.loads(text)
                            except ValueError:
                                continue
                            if isinstance(msg, dict):
                                try:
                                    await self.bei_nachricht(msg)
                                except Exception:
                                    log.exception("%s: Nachricht %s nicht verarbeitet", self.rolle, msg.get("t"))
                    finally:
                        waechter.cancel()
            except (OSError, asyncio.TimeoutError, websockets.WebSocketException) as e:
                log.warning("%s: Server nicht erreichbar (%s)", self.rolle, e)
            finally:
                if self._ws is not None:
                    self.trennungen += 1
                    log.info("%s: Verbindung getrennt", self.rolle)
                self._ws = None
            await asyncio.sleep(pause)
            pause = min(pause * 2, 10.0)

    @staticmethod
    async def _waechter(ws, darf: Callable[[], bool]) -> None:
        while darf():
            await asyncio.sleep(1)
        await ws.close()
