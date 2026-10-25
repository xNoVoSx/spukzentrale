"""Einstieg: uvicorn app.main:app"""

import asyncio
import contextlib
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from . import VERSION
from .ablauf import SzenenSpieler
from .api import aufraeumen, router
from .ereignisse import Generator
from .homeassistant import HomeAssistant
from .medien import KonvertierFehler, fx_umwandeln, test_klick_erzeugen
from .modelle import Effekt
from .mqtt import MqttBruecke
from .zeitplan import Zeitplan
from .zentrale import Zentrale

logging.basicConfig(
    level=os.environ.get("SPUK_LOGLEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
log = logging.getLogger("spukzentrale")
STATIC = Path(__file__).resolve().parent.parent / "static"


async def _test_klick(z: Zentrale) -> None:
    """System-Sound für den Ton-Licht-Abgleich anlegen (einmalig)."""
    if any(f.kategorie == "system" for f in z.medien.daten.fx):
        return
    try:
        quelle = await test_klick_erzeugen()
        fid, dauer, groesse = await fx_umwandeln(quelle)
        quelle.unlink(missing_ok=True)
        z.medien.fx_hinzu(Effekt(id=fid, name="Test-Klick", kategorie="system", dauer_ms=dauer, groesse=groesse))
    except KonvertierFehler as e:
        log.error("Test-Klick konnte nicht erzeugt werden: %s", e)


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("Spukzentrale %s startet", VERSION)
    aufraeumen()
    z = Zentrale()
    z.spieler = SzenenSpieler(z)
    z.generator = Generator(z)
    app.state.zentrale = z
    await _test_klick(z)

    zeitplan = Zeitplan(z)
    ha = HomeAssistant(z)
    mqtt = MqttBruecke(z)
    tasks = [
        asyncio.create_task(z.generator.lauf(), name="ereignisse"),
        asyncio.create_task(zeitplan.lauf(), name="zeitplan"),
    ]
    ha.neu_starten()
    mqtt.neu_starten()
    try:
        yield
    finally:
        for t in tasks:
            t.cancel()
        await z.spieler.stoppen()
        await ha.stoppen()
        await mqtt.stoppen()
        await z.ambiente.stoppen()
        for t in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await t
        log.info("Spukzentrale beendet")


app = FastAPI(title="Spukzentrale", version=VERSION, lifespan=lifespan)
app.include_router(router)
app.mount("/", StaticFiles(directory=STATIC, html=True), name="oberflaeche")
