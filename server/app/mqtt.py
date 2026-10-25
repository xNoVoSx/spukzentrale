"""MQTT mit Home-Assistant-Discovery.

Legt in Home Assistant automatisch ein Gerät „Spukzentrale“ an: Schalter für Show,
Ambiente und Ereignisse, Helligkeit, Lautstärke, Gruselstufe, Stimmung, einen Knopf pro
Szene, Gerätestatus und das laufende Ambiente-Stück.
Doku: https://www.home-assistant.io/integrations/mqtt/#mqtt-discovery
"""

import asyncio
import contextlib
import json
import logging
from typing import TYPE_CHECKING, Any

import aiomqtt

from . import VERSION

if TYPE_CHECKING:
    from .zentrale import Zentrale

log = logging.getLogger(__name__)
GERAET_ID = "spukzentrale"


class MqttBruecke:
    def __init__(self, z: "Zentrale") -> None:
        self.z = z
        self._task: asyncio.Task | None = None
        self._client: aiomqtt.Client | None = None
        self._aenderung = asyncio.Event()
        self._discovery_neu = asyncio.Event()
        self._veroeffentlicht: set[str] = set()  # Discovery-Topics, um gelöschte Szenen zu entfernen
        z.beobachter.append(self._geaendert)

    # ------------------------------------------------------------------
    def _geaendert(self, art: str) -> None:
        if art == "einst:mqtt":
            self.neu_starten()
            return
        if art == "daten" or art.startswith("einst:"):
            self._discovery_neu.set()
        self._aenderung.set()

    def _status(self, text: str) -> None:
        if text != self.z.mqtt_status:
            self.z.mqtt_status = text
            self.z._zustand_geaendert()

    def neu_starten(self) -> None:
        if self._task:
            self._task.cancel()
        self._task = asyncio.create_task(self._lauf(), name="mqtt")

    async def stoppen(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    @property
    def p(self) -> str:
        return self.z.einst.mqtt.praefix.rstrip("/")

    # ------------------------------------------------------------------
    async def _lauf(self) -> None:
        warte = 5
        while True:
            m = self.z.einst.mqtt
            if not (m.aktiv and m.host):
                self._status("aus")
                return
            try:
                will = aiomqtt.Will(f"{self.p}/verfuegbar", "offline", qos=1, retain=True)
                async with aiomqtt.Client(
                    m.host, m.port, username=m.benutzer or None, password=m.passwort or None,
                    identifier=f"spukzentrale-{VERSION}", will=will, keepalive=30,
                ) as c:
                    self._client = c
                    self._status("verbunden")
                    log.info("MQTT verbunden mit %s:%s", m.host, m.port)
                    warte = 5
                    await c.subscribe(f"{self.p}/+/set")
                    await c.subscribe(f"{self.p}/szene/+/start")
                    await c.subscribe(f"{self.p}/knopf/+")
                    await self._discovery(c)
                    await c.publish(f"{self.p}/verfuegbar", "online", qos=1, retain=True)
                    await self._zustaende(c)
                    sender = asyncio.create_task(self._sende_aenderungen(c))
                    try:
                        async for msg in c.messages:
                            try:
                                await self._befehl(str(msg.topic), (msg.payload or b"").decode(errors="replace"))
                            except Exception:
                                log.exception("MQTT-Befehl %s", msg.topic)
                    finally:
                        sender.cancel()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self._status(f"Fehler: {e}")
                log.warning("MQTT: %s – neuer Versuch in %s s", e, warte)
            finally:
                self._client = None
            await asyncio.sleep(warte)
            warte = min(warte * 2, 120)

    async def _sende_aenderungen(self, c: aiomqtt.Client) -> None:
        while True:
            await self._aenderung.wait()
            await asyncio.sleep(0.3)  # mehrere Änderungen zusammenfassen
            self._aenderung.clear()
            if self._discovery_neu.is_set():
                self._discovery_neu.clear()
                await self._discovery(c)
            await self._zustaende(c)

    # ------------------------------------------------------------------
    def _geraet(self) -> dict[str, Any]:
        return {"identifiers": [GERAET_ID], "name": "Spukzentrale", "model": "Spukzentrale", "sw_version": VERSION,
                "manufacturer": "Spukzentrale"}

    def _basis(self, objekt: str, name: str, icon: str | None = None) -> dict[str, Any]:
        d = {
            "name": name,
            "unique_id": f"{GERAET_ID}_{objekt}",
            "availability_topic": f"{self.p}/verfuegbar",
            "device": self._geraet(),
        }
        if icon:
            d["icon"] = icon
        return d

    def _entitaeten(self) -> list[tuple[str, str, dict[str, Any]]]:
        p = self.p
        z = self.z
        e: list[tuple[str, str, dict[str, Any]]] = []

        def schalter(obj: str, name: str, icon: str) -> None:
            e.append(("switch", obj, {**self._basis(obj, name, icon),
                                      "command_topic": f"{p}/{obj}/set", "state_topic": f"{p}/{obj}"}))

        schalter("show", "Show", "mdi:ghost")
        schalter("ambiente", "Ambiente", "mdi:music")
        schalter("ereignisse", "Zufallsereignisse", "mdi:dice-5")
        for obj, name, icon in (("hell", "Helligkeit", "mdi:brightness-6"), ("lautst", "Lautstärke", "mdi:volume-high")):
            e.append(("number", obj, {**self._basis(obj, name, icon), "command_topic": f"{p}/{obj}/set",
                                      "state_topic": f"{p}/{obj}", "min": 0, "max": 100, "step": 1,
                                      "unit_of_measurement": "%", "mode": "slider"}))
        e.append(("select", "grusel", {**self._basis("grusel", "Gruselstufe", "mdi:skull"),
                                       "command_topic": f"{p}/grusel/set", "state_topic": f"{p}/grusel",
                                       "options": ["1", "2", "3"]}))
        e.append(("select", "stimmung", {**self._basis("stimmung", "Stimmung", "mdi:lightbulb-group"),
                                         "command_topic": f"{p}/stimmung/set", "state_topic": f"{p}/stimmung",
                                         "options": ["–"] + [s.name for s in z.stimmungen.liste]}))
        for s in z.szenen.liste:
            obj = f"szene_{s.id.replace('-', '_')}"
            e.append(("button", obj, {**self._basis(obj, f"Szene {s.name}", "mdi:movie-open"),
                                      "command_topic": f"{p}/szene/{s.id}/start"}))
        e.append(("button", "stopp", {**self._basis("stopp", "Alles stoppen", "mdi:stop"),
                                      "command_topic": f"{p}/knopf/stopp"}))
        e.append(("button", "ausloeser", {**self._basis("ausloeser", "Klingel-Auslöser", "mdi:doorbell"),
                                          "command_topic": f"{p}/knopf/ausloeser"}))
        for rolle, name in (("licht", "Licht-ESP"), ("sound", "Sound-ESP")):
            obj = f"{rolle}_online"
            e.append(("binary_sensor", obj, {**self._basis(obj, name), "state_topic": f"{p}/{obj}",
                                             "device_class": "connectivity", "entity_category": "diagnostic"}))
        e.append(("sensor", "jetzt", {**self._basis("jetzt", "Ambiente-Stück", "mdi:music-note"),
                                      "state_topic": f"{p}/jetzt"}))
        e.append(("sensor", "szene", {**self._basis("szene", "Laufende Szene", "mdi:movie-play"),
                                      "state_topic": f"{p}/szene"}))
        return e

    async def _discovery(self, c: aiomqtt.Client) -> None:
        disc = self.z.einst.mqtt.discovery.rstrip("/")
        neu: set[str] = set()
        for komponente, obj, cfg in self._entitaeten():
            topic = f"{disc}/{komponente}/{GERAET_ID}/{obj}/config"
            cfg["default_entity_id"] = f"{komponente}.{GERAET_ID}_{obj}"
            neu.add(topic)
            await c.publish(topic, json.dumps(cfg), qos=1, retain=True)
        for topic in self._veroeffentlicht - neu:  # z. B. gelöschte Szene
            await c.publish(topic, b"", qos=1, retain=True)
        self._veroeffentlicht = neu

    async def _zustaende(self, c: aiomqtt.Client) -> None:
        z = self.z
        an = lambda b: "ON" if b else "OFF"  # noqa: E731
        stimmung = next((s.name for s in z.stimmungen.liste if s.id == z.zustand.stimmung_id), "–")
        werte = {
            "show": an(z.zustand.licht.an),
            "ambiente": an(z.zustand.ambiente_an),
            "ereignisse": an(z.zustand.ereignisse_an),
            "hell": str(z.zustand.licht.hell),
            "lautst": str(z.einst.ton.master),
            "grusel": str(z.zustand.gruselstufe),
            "stimmung": stimmung,
            "licht_online": an(z.geraete.verbunden("licht")),
            "sound_online": an(z.geraete.verbunden("sound")),
            "jetzt": z.ambiente.jetzt or "–",
            "szene": (z.spieler.laufend or {}).get("name", "–") if z.spieler else "–",
        }
        for k, v in werte.items():
            await c.publish(f"{self.p}/{k}", v, qos=0, retain=True)

    # ------------------------------------------------------------------
    async def _befehl(self, topic: str, wert: str) -> None:
        z = self.z
        teile = topic[len(self.p) + 1:].split("/")
        if len(teile) == 2 and teile[1] == "set":
            obj = teile[0]
            if obj == "show":
                await z.show(wert == "ON", quelle="Home Assistant")
            elif obj == "ambiente":
                await z.ambiente_schalten(wert == "ON")
            elif obj == "ereignisse":
                z.ereignisse_schalten(wert == "ON")
            elif obj == "hell":
                await z.licht_aendern(hell=int(float(wert)))
            elif obj == "lautst":
                await z.einstellungen_aendern("ton", {"master": int(float(wert))})
            elif obj == "grusel":
                z.gruselstufe(int(wert))
            elif obj == "stimmung":
                s = next((s for s in z.stimmungen.liste if s.name == wert), None)
                if s:
                    await z.stimmung_anwenden(s.id)
        elif len(teile) == 3 and teile[0] == "szene" and teile[2] == "start":
            with contextlib.suppress(KeyError):
                await z.szene_starten(teile[1], "Home Assistant")
        elif len(teile) == 2 and teile[0] == "knopf":
            if teile[1] == "stopp":
                await z.alles_stoppen()
            elif teile[1] == "ausloeser":
                await z.ausloeser("Home Assistant")
