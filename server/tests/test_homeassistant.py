"""Home-Assistant-Anbindung gegen einen nachgebauten HA-WebSocket-Server."""

import asyncio
import json

import websockets

from app.homeassistant import HomeAssistant, sensoren
from app.modelle import Einstellungen
from app.zentrale import Zentrale

TOKEN = "geheim"


class FakeHA:
    def __init__(self):
        self.abos: dict[str, int] = {}  # entity_id -> Abo-ID
        self.ws = None
        self.verbunden = asyncio.Event()

    async def handler(self, ws):
        await ws.send(json.dumps({"type": "auth_required", "ha_version": "2026.10.0"}))
        auth = json.loads(await ws.recv())
        if auth.get("access_token") != TOKEN:
            await ws.send(json.dumps({"type": "auth_invalid", "message": "falsch"}))
            return
        await ws.send(json.dumps({"type": "auth_ok", "ha_version": "2026.10.0"}))
        self.ws = ws
        async for roh in ws:
            m = json.loads(roh)
            if m["type"] == "subscribe_trigger":
                assert m["trigger"]["trigger"] == "state" and m["trigger"]["to"] == "on"
                self.abos[m["trigger"]["entity_id"]] = m["id"]
                await ws.send(json.dumps({"id": m["id"], "type": "result", "success": True, "result": None}))
                self.verbunden.set()
            elif m["type"] == "get_states":
                await ws.send(json.dumps({"id": m["id"], "type": "result", "success": True, "result": [
                    {"entity_id": "binary_sensor.klingel_person", "state": "off", "attributes": {"friendly_name": "Klingel Person"}},
                    {"entity_id": "binary_sensor.klingel_besucher", "state": "off", "attributes": {"friendly_name": "Klingel Besucher"}},
                    {"entity_id": "light.flur", "state": "on", "attributes": {}},
                ]}))

    async def ausloesen(self, entity_id: str):
        await self.ws.send(json.dumps({"id": self.abos[entity_id], "type": "event", "event": {"variables": {}}}))


async def test_ha_beobachtet_mehrere_sensoren_und_loest_aus():
    ha = FakeHA()
    async with websockets.serve(ha.handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        z = Zentrale()
        ausgeloest: list[str] = []

        async def ausloeser(aid, quelle="", pruefen=True):
            ausgeloest.append(aid)
            return True

        z.ausloeser = ausloeser
        z.einst.homeassistant.url = f"http://127.0.0.1:{port}"
        z.einst.homeassistant.token = TOKEN
        z.einst.ausloeser[0].aktiv = True
        z.einst.ausloeser[0].entity_id = "binary_sensor.klingel_besucher"
        z.einst.ausloeser[1].aktiv = True
        z.einst.ausloeser[1].entity_id = "binary_sensor.klingel_person"

        verbindung = HomeAssistant(z)
        verbindung.neu_starten()
        await asyncio.wait_for(ha.verbunden.wait(), 5)
        await asyncio.sleep(0.1)
        assert set(ha.abos) == {"binary_sensor.klingel_besucher", "binary_sensor.klingel_person"}
        assert z.ha_status == "verbunden"

        await ha.ausloesen("binary_sensor.klingel_person")
        await ha.ausloesen("binary_sensor.klingel_besucher")
        await asyncio.sleep(0.2)
        assert ausgeloest == ["person", "klingel"]

        liste = await sensoren(f"http://127.0.0.1:{port}", TOKEN)
        assert [s["entity_id"] for s in liste][0] == "binary_sensor.klingel_person"  # Personensensoren zuerst
        assert all(s["entity_id"].startswith("binary_sensor.") for s in liste)
        await verbindung.stoppen()


async def test_ha_verbindet_auch_ohne_aktiven_ausloeser():
    """Adresse + Token genügen: der Status zeigt sofort, ob die Zugangsdaten stimmen."""
    ha = FakeHA()
    async with websockets.serve(ha.handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        z = Zentrale()
        z.einst.homeassistant.url = f"http://127.0.0.1:{port}"
        z.einst.homeassistant.token = TOKEN
        assert not any(a.aktiv for a in z.einst.ausloeser)
        verbindung = HomeAssistant(z)
        verbindung.neu_starten()
        for _ in range(50):
            await asyncio.sleep(0.05)
            if z.ha_status != "aus":
                break
        assert z.ha_status == "verbunden – kein Auslöser aktiv" and ha.abos == {}
        await verbindung.stoppen()


async def test_ha_falsches_token_meldet_fehler():
    ha = FakeHA()
    async with websockets.serve(ha.handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        z = Zentrale()
        z.einst.homeassistant.url = f"http://127.0.0.1:{port}"
        z.einst.homeassistant.token = "falsch"
        z.einst.ausloeser[0].aktiv = True
        z.einst.ausloeser[0].entity_id = "binary_sensor.x"
        verbindung = HomeAssistant(z)
        verbindung.neu_starten()
        await asyncio.sleep(0.5)
        assert z.ha_status.startswith("Fehler") and "Token" in z.ha_status
        await verbindung.stoppen()


def test_alte_klingel_einstellung_wird_uebernommen():
    alt = {"klingel": {"aktiv": True, "entity_id": "binary_sensor.alt", "szene_id": "hexenauftritt",
                       "sperre_s": 99, "gruselstufe_min": 2, "nur_wenn_an": False}}
    e = Einstellungen.model_validate(alt)
    k = e.ausloeser[0]
    assert (k.id, k.aktiv, k.entity_id, k.ziel, k.sperre_s, k.gruselstufe_min, k.nur_wenn_an) == \
        ("klingel", True, "binary_sensor.alt", "hexenauftritt", 99, 2, False)
    assert e.ausloeser[1].id == "person"


def test_vorgaben_werden_bei_update_ergaenzt(datenordner):
    from app import pfade
    from app.modelle import Szene, Szenen
    from app.speicher import speichere
    # Stand von 1.0: eigene Szene + eingebaute, ohne vorgaben_version
    speichere(pfade.CONFIG / "szenen.json", Szenen(liste=[Szene(id="meine", name="Meine"), Szene(id="schreck", name="Schreck")]))
    z = Zentrale()
    ids = [s.id for s in z.szenen.liste]
    assert ids[:2] == ["meine", "schreck"] and "kettensaege" in ids and "begruessung" in ids
    assert "gewitterfront" not in ids  # 1.0-Szene, die der Nutzer gelöscht hat, kommt nicht zurück
