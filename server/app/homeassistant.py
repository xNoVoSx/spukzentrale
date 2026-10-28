"""Home Assistant über die offizielle WebSocket-API.

Beobachtet die Sensoren der Auslöser (z. B. Klingeltaste und Personenerkennung der
Reolink-Klingel) per `subscribe_trigger` und löst bei "on" den jeweiligen Sound bzw. die
Szene aus.
Doku: https://contributors.home-assistant.io/core/internals/websocket-api/
"""

import asyncio
import contextlib
import itertools
import json
import logging
from typing import TYPE_CHECKING, Any

import websockets

if TYPE_CHECKING:
    from .zentrale import Zentrale

log = logging.getLogger(__name__)


class HaFehler(Exception):
    pass


def ws_url(url: str) -> str:
    url = url.strip().rstrip("/")
    if url.startswith("https://"):
        url = "wss://" + url[len("https://"):]
    elif url.startswith("http://"):
        url = "ws://" + url[len("http://"):]
    elif not url.startswith(("ws://", "wss://")):
        url = "ws://" + url
    return url + "/api/websocket"


async def _anmelden(url: str, token: str):
    ws = await websockets.connect(ws_url(url), open_timeout=10, max_size=16 * 1024 * 1024)
    try:
        hallo = json.loads(await asyncio.wait_for(ws.recv(), 10))
        if hallo.get("type") != "auth_required":
            raise HaFehler("unerwartete Antwort von Home Assistant")
        await ws.send(json.dumps({"type": "auth", "access_token": token}))
        antwort = json.loads(await asyncio.wait_for(ws.recv(), 10))
        if antwort.get("type") != "auth_ok":
            raise HaFehler("Token ungültig")
        return ws
    except BaseException:
        await ws.close()
        raise


async def sensoren(url: str, token: str) -> list[dict[str, Any]]:
    """Alle binary_sensor-Entitäten (für die Auswahl in der Oberfläche)."""
    if not url or not token:
        raise HaFehler("Home Assistant ist nicht eingerichtet")
    ws = await _anmelden(url, token)
    try:
        await ws.send(json.dumps({"id": 1, "type": "get_states"}))
        while True:
            msg = json.loads(await asyncio.wait_for(ws.recv(), 15))
            if msg.get("id") == 1 and msg.get("type") == "result":
                break
        if not msg.get("success"):
            raise HaFehler(msg.get("error", {}).get("message", "get_states fehlgeschlagen"))
        liste = [
            {
                "entity_id": s["entity_id"],
                "name": s.get("attributes", {}).get("friendly_name", s["entity_id"]),
                "zustand": s.get("state"),
            }
            for s in msg["result"]
            if s["entity_id"].startswith("binary_sensor.")
        ]
        # Personen-/Bewegungssensoren nach oben
        liste.sort(key=lambda s: (not any(w in s["entity_id"] for w in ("person", "motion", "bewegung")), s["name"].lower()))
        return liste
    finally:
        await ws.close()


class HomeAssistant:
    def __init__(self, z: "Zentrale") -> None:
        self.z = z
        self._task: asyncio.Task | None = None
        z.beobachter.append(self._geaendert)

    def _geaendert(self, art: str) -> None:
        if art in ("einst:homeassistant", "einst:ausloeser"):
            self.neu_starten()

    def _status(self, text: str) -> None:
        if text != self.z.ha_status:
            self.z.ha_status = text
            self.z._zustand_geaendert()

    def neu_starten(self) -> None:
        if self._task:
            self._task.cancel()
        self._task = asyncio.create_task(self._lauf(), name="homeassistant")

    async def stoppen(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    async def _lauf(self) -> None:
        warte = 5
        while True:
            ha = self.z.einst.homeassistant
            beobachten = {a.id: a.entity_id for a in self.z.einst.ausloeser if a.aktiv and a.entity_id}
            if not (ha.url and ha.token):
                self._status("aus")
                return
            # auch ohne aktiven Auslöser verbinden: zeigt sofort, ob Adresse und Token stimmen
            try:
                await self._verbinden(ha.url, ha.token, beobachten)
                warte = 5
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self._status(f"Fehler: {e}")
                log.warning("Home Assistant: %s – neuer Versuch in %s s", e, warte)
            await asyncio.sleep(warte)
            warte = min(warte * 2, 120)

    async def _verbinden(self, url: str, token: str, beobachten: dict[str, str]) -> None:
        """beobachten: Auslöser-ID -> entity_id"""
        ws = await _anmelden(url, token)
        ids = itertools.count(1)
        try:
            abos: dict[int, str] = {}  # Abo-ID -> Auslöser-ID
            for aid, entity_id in beobachten.items():
                nr = next(ids)
                abos[nr] = aid
                await ws.send(json.dumps({
                    "id": nr,
                    "type": "subscribe_trigger",
                    "trigger": {"trigger": "state", "entity_id": entity_id, "to": "on"},
                }))
            self._status("verbunden" if beobachten else "verbunden – kein Auslöser aktiv")
            log.info("Home Assistant verbunden, beobachte %s", ", ".join(beobachten.values()) or "nichts")
            while True:
                try:
                    roh = await asyncio.wait_for(ws.recv(), 60)
                except TimeoutError:
                    await ws.send(json.dumps({"id": next(ids), "type": "ping"}))
                    continue
                msg = json.loads(roh)
                aid = abos.get(msg.get("id"))
                if not aid:
                    continue
                if msg.get("type") == "result" and not msg.get("success"):
                    raise HaFehler(f"{beobachten[aid]}: " + msg.get("error", {}).get("message", "Abo fehlgeschlagen"))
                if msg.get("type") == "event":
                    try:
                        if not await self.z.ausloeser(aid):
                            log.info("Auslöser %s gesperrt (Sperrzeit/Gruselstufe/Show aus)", aid)
                    except KeyError:
                        log.warning("Auslöser %s gibt es nicht mehr", aid)
        finally:
            await ws.close()
