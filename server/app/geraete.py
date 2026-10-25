"""Verbindungen zu den ESPs (WebSocket /ws/geraet?rolle=licht|sound)."""

import asyncio
import json
import logging
import time
from typing import Any

from fastapi import WebSocket

log = logging.getLogger(__name__)

ROLLEN = ("licht", "sound")


class Geraet:
    def __init__(self, rolle: str, ws: WebSocket, fw: str) -> None:
        self.rolle = rolle
        self.ws = ws
        self.fw = fw
        self.hallo: dict[str, Any] = {}
        self.status: dict[str, Any] = {}
        self.seit = time.time()
        self.letzte_nachricht = time.monotonic()
        self._sperre = asyncio.Lock()

    @property
    def ip(self) -> str:
        if self.hallo.get("ip"):
            return self.hallo["ip"]
        return self.ws.client.host if self.ws.client else ""

    async def senden(self, nachricht: dict[str, Any]) -> bool:
        try:
            async with self._sperre:
                await self.ws.send_text(json.dumps(nachricht, separators=(",", ":")))
            return True
        except Exception as e:  # Verbindung weg – wird vom Empfangs-Loop aufgeräumt
            log.debug("%s: senden fehlgeschlagen: %s", self.rolle, e)
            return False

    def info(self) -> dict[str, Any]:
        return {
            "verbunden": True,
            "fw": self.hallo.get("fw", self.fw),
            "ip": self.ip,
            "mac": self.hallo.get("mac", ""),
            "seit": self.seit,
            "status": self.status,
            "hallo": {k: v for k, v in self.hallo.items() if k not in ("t", "effekte", "aktionen")},
        }


class Geraete:
    def __init__(self) -> None:
        self.liste: dict[str, Geraet] = {}

    def get(self, rolle: str) -> Geraet | None:
        return self.liste.get(rolle)

    def verbunden(self, rolle: str) -> bool:
        return rolle in self.liste

    async def senden(self, rolle: str, nachricht: dict[str, Any]) -> bool:
        g = self.liste.get(rolle)
        return await g.senden(nachricht) if g else False

    def anmelden(self, g: Geraet) -> Geraet | None:
        """Gibt ein evtl. verdrängtes älteres Gerät derselben Rolle zurück."""
        alt = self.liste.get(g.rolle)
        self.liste[g.rolle] = g
        return alt

    def abmelden(self, g: Geraet) -> bool:
        if self.liste.get(g.rolle) is g:
            del self.liste[g.rolle]
            return True
        return False

    def info(self) -> dict[str, Any]:
        return {r: (self.liste[r].info() if r in self.liste else {"verbunden": False}) for r in ROLLEN}
