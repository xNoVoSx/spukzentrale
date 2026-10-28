"""REST-API (für die Weboberfläche und Home Assistant) und WebSockets."""

import asyncio
import json
import logging
import shutil
import uuid
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Body, File, Form, HTTPException, Query, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field, ValidationError

from . import homeassistant, pfade
from .geraete import ROLLEN, Geraet
from .medien import ID_MUSTER, KonvertierFehler, fx_umwandeln, musik_umwandeln
from .modelle import (
    Ausloeser, Effekt, Ereignisse, Kategorie, LichtAktion, Musikstueck, Playlist, Stimmung, Szene, Zone, neue_id,
)
from .vorgaben import LICHT_JE_KATEGORIE
from .zentrale import Zentrale

log = logging.getLogger(__name__)
router = APIRouter()

MUSIK_MAX_MB = 300
FX_MAX_MB = 50


def zentrale(request: Request) -> Zentrale:
    return request.app.state.zentrale


def _nicht_gefunden(was: str) -> HTTPException:
    return HTTPException(404, f"{was} gibt es nicht")


class An(BaseModel):
    an: bool


class LichtAenderung(BaseModel):
    an: bool | None = None
    hell: int | None = Field(None, ge=0, le=100)
    musik_reaktiv: int | None = Field(None, ge=0, le=100)


class Stufe(BaseModel):
    stufe: int = Field(ge=1, le=3)


class Name(BaseModel):
    name: str = Field(min_length=1, max_length=60)


class EffektAenderung(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    kategorie: Kategorie
    vol: int = Field(100, ge=0, le=100)
    licht: LichtAktion | None = None


class ZonenTest(BaseModel):
    zone: int = Field(ge=-1, le=5)  # -1 = alle


class Abspielen(BaseModel):
    vol: int = Field(100, ge=0, le=100)


class StimmeKorrektur(BaseModel):
    stellen: list[tuple[float, float]] | None = Field(None, max_length=200)


# ---------------------------------------------------------------------------
# Status & Steuerung
# ---------------------------------------------------------------------------
@router.get("/api/status")
async def status(request: Request):
    return zentrale(request).alles()


@router.post("/api/show")
async def show(request: Request, d: An):
    await zentrale(request).show(d.an, quelle="Hand")
    return {"ok": True}


@router.post("/api/licht")
async def licht(request: Request, d: LichtAenderung):
    await zentrale(request).licht_aendern(an=d.an, hell=d.hell, musik_reaktiv=d.musik_reaktiv)
    return {"ok": True}


@router.put("/api/zonen/{i}")
async def zone(request: Request, i: int, d: Zone):
    try:
        await zentrale(request).zone_setzen(i, d)
    except ValueError as e:
        raise HTTPException(404, str(e)) from e
    return {"ok": True}


@router.post("/api/zonen/alle")
async def zonen_alle(request: Request, d: Zone):
    z = zentrale(request)
    await z.licht_aendern(zonen=[d.model_copy() for _ in z.zustand.licht.zonen])
    return {"ok": True}


@router.post("/api/zonen/test")
async def zonen_test(request: Request, d: ZonenTest):
    z = zentrale(request)
    zonen = [] if d.zone < 0 else [d.zone]
    await z.aktion(LichtAktion(name="test", zonen=zonen, dauer_ms=3000, staerke=100))
    return {"ok": True}


@router.post("/api/aktion")
async def aktion(request: Request, d: LichtAktion):
    await zentrale(request).aktion(d, quelle="Hand")
    return {"ok": True}


@router.post("/api/stopp")
async def stopp(request: Request):
    await zentrale(request).alles_stoppen()
    return {"ok": True}


@router.post("/api/ambiente")
async def ambiente(request: Request, d: An):
    await zentrale(request).ambiente_schalten(d.an)
    return {"ok": True}


@router.post("/api/ambiente/weiter")
async def ambiente_weiter(request: Request):
    await zentrale(request).ambiente_weiter()
    return {"ok": True}


@router.post("/api/ambiente/spielen/{mid}")
async def ambiente_spielen(request: Request, mid: str):
    """Dieses Stück sofort spielen (Ambiente geht dafür an), danach geht die Playlist ab dort weiter."""
    try:
        await zentrale(request).ambiente_spielen(mid)
    except ValueError as e:
        raise _nicht_gefunden("Dieses Stück") from e
    return {"ok": True}


@router.post("/api/ereignisse")
async def ereignisse(request: Request, d: An):
    zentrale(request).ereignisse_schalten(d.an)
    return {"ok": True}


@router.put("/api/ereignisse/regeln")
async def regeln(request: Request, d: Ereignisse):
    zentrale(request).regeln_setzen(d)
    return {"ok": True}


@router.post("/api/gruselstufe")
async def gruselstufe(request: Request, d: Stufe):
    zentrale(request).gruselstufe(d.stufe)
    return {"ok": True}


@router.put("/api/ausloeser")
async def ausloeser_setzen(request: Request, d: list[Ausloeser]):
    zentrale(request).ausloeser_setzen(d)
    return zentrale(request).einst.ausloeser


@router.post("/api/ausloeser/{aid}")
async def ausloeser(request: Request, aid: str, test: bool = False):
    """Wie der Sensor (mit Sperrzeit, Gruselstufe, Show an) – oder mit ?test=true ohne Prüfungen."""
    try:
        return {"ausgeloest": await zentrale(request).ausloeser(aid, "Test" if test else "", pruefen=not test)}
    except KeyError as e:
        raise _nicht_gefunden("Diesen Auslöser") from e


# ---------------------------------------------------------------------------
# Stimmungen
# ---------------------------------------------------------------------------
@router.post("/api/stimmungen/{sid}/anwenden")
async def stimmung_anwenden(request: Request, sid: str):
    try:
        await zentrale(request).stimmung_anwenden(sid)
    except KeyError as e:
        raise _nicht_gefunden("Diese Stimmung") from e
    return {"ok": True}


@router.post("/api/stimmungen")
async def stimmung_speichern(request: Request, d: Name):
    """Aktuelle Zonen als neue Stimmung speichern."""
    z = zentrale(request)
    s = Stimmung(
        id=neue_id(d.name, {x.id for x in z.stimmungen.liste}), name=d.name,
        zonen=[zz.model_copy() for zz in z.zustand.licht.zonen], musik_reaktiv=z.zustand.licht.musik_reaktiv,
    )
    z.stimmungen.liste.append(s)
    z.zustand.stimmung_id = s.id
    z.speichern("stimmungen", "zustand")
    z.daten_geaendert()
    return s


@router.put("/api/stimmungen/{sid}")
async def stimmung_aendern(request: Request, sid: str, d: Stimmung):
    z = zentrale(request)
    for i, s in enumerate(z.stimmungen.liste):
        if s.id == sid:
            z.stimmungen.liste[i] = d.model_copy(update={"id": sid, "eingebaut": s.eingebaut})
            z.speichern("stimmungen")
            z.daten_geaendert()
            return z.stimmungen.liste[i]
    raise _nicht_gefunden("Diese Stimmung")


@router.delete("/api/stimmungen/{sid}")
async def stimmung_loeschen(request: Request, sid: str):
    z = zentrale(request)
    vorher = len(z.stimmungen.liste)
    z.stimmungen.liste = [s for s in z.stimmungen.liste if s.id != sid]
    if len(z.stimmungen.liste) == vorher:
        raise _nicht_gefunden("Diese Stimmung")
    if z.zustand.stimmung_id == sid:
        z.zustand.stimmung_id = None
    z.speichern("stimmungen", "zustand")
    z.daten_geaendert()
    return {"ok": True}


# ---------------------------------------------------------------------------
# Szenen
# ---------------------------------------------------------------------------
@router.post("/api/szenen/stopp")
async def szene_stopp(request: Request):
    z = zentrale(request)
    if z.spieler:
        await z.spieler.stoppen()
    return {"ok": True}


@router.post("/api/szenen/{sid}/start")
async def szene_start(request: Request, sid: str):
    try:
        await zentrale(request).szene_starten(sid, "Hand")
    except KeyError as e:
        raise _nicht_gefunden("Diese Szene") from e
    return {"ok": True}


@router.post("/api/szenen")
async def szene_neu(request: Request, d: Szene):
    z = zentrale(request)
    s = d.model_copy(update={"id": neue_id(d.name, {x.id for x in z.szenen.liste}), "eingebaut": False})
    z.szenen.liste.append(s)
    z.speichern("szenen")
    z.daten_geaendert()
    return s


@router.put("/api/szenen/{sid}")
async def szene_aendern(request: Request, sid: str, d: Szene):
    z = zentrale(request)
    for i, s in enumerate(z.szenen.liste):
        if s.id == sid:
            z.szenen.liste[i] = d.model_copy(update={"id": sid, "eingebaut": s.eingebaut})
            z.speichern("szenen")
            z.daten_geaendert()
            return z.szenen.liste[i]
    raise _nicht_gefunden("Diese Szene")


@router.delete("/api/szenen/{sid}")
async def szene_loeschen(request: Request, sid: str):
    z = zentrale(request)
    vorher = len(z.szenen.liste)
    z.szenen.liste = [s for s in z.szenen.liste if s.id != sid]
    if len(z.szenen.liste) == vorher:
        raise _nicht_gefunden("Diese Szene")
    z.speichern("szenen")
    z.daten_geaendert()
    return {"ok": True}


# ---------------------------------------------------------------------------
# Musik (Ambiente)
# ---------------------------------------------------------------------------
async def _hochladen(datei: UploadFile, max_mb: int) -> Path:
    ziel = pfade.TMP / f"upload-{uuid.uuid4().hex}"
    groesse = 0
    with open(ziel, "wb") as f:
        while block := await datei.read(1024 * 1024):
            groesse += len(block)
            if groesse > max_mb * 1024 * 1024:
                f.close()
                ziel.unlink(missing_ok=True)
                raise HTTPException(413, f"Datei ist größer als {max_mb} MB")
            f.write(block)
    return ziel


def _anzeigename(dateiname: str | None) -> str:
    name = Path(dateiname or "Unbenannt").stem.replace("_", " ").strip()
    return (name or "Unbenannt")[:60]


@router.post("/api/musik")
async def musik_hochladen(request: Request, datei: Annotated[UploadFile, File()]):
    z = zentrale(request)
    roh = await _hochladen(datei, MUSIK_MAX_MB)
    try:
        mid, dauer, groesse = await musik_umwandeln(roh)
    except KonvertierFehler as e:
        raise HTTPException(400, f"Umwandlung fehlgeschlagen: {e}") from e
    finally:
        roh.unlink(missing_ok=True)
    s = z.medien.musik_hinzu(Musikstueck(id=mid, name=_anzeigename(datei.filename), dauer_s=round(dauer, 1), groesse=groesse))
    z.protokollieren(f"Musik hochgeladen: {s.name}", "info")
    z.daten_geaendert()
    if s.analyse is None:
        asyncio.create_task(z.musik_analysieren(s.id))
    return s


@router.post("/api/musik/{mid}/analyse")
async def musik_analyse(request: Request, mid: str):
    """Stück neu analysieren (läuft im Hintergrund, Ergebnis kommt über die Oberfläche)."""
    z = zentrale(request)
    if not z.medien.musik(mid):
        raise _nicht_gefunden("Dieses Stück")
    asyncio.create_task(z.musik_analysieren(mid))
    return {"ok": True}


@router.put("/api/musik/{mid}/stimme")
async def musik_stimme(request: Request, mid: str, d: StimmeKorrektur):
    """Gesangsstellen von Hand setzen (Liste von [von_s, bis_s]); stellen = null: wieder die Erkennung nehmen."""
    z = zentrale(request)
    s = z.medien.musik(mid)
    if not s:
        raise _nicht_gefunden("Dieses Stück")
    if d.stellen is not None:
        ende = s.dauer_s or 1e9
        stellen = sorted((max(0.0, min(a, ende)), max(0.0, min(b, ende))) for a, b in d.stellen)
        s.stimme_manuell = [(a, b) for a, b in stellen if b > a]
    else:
        s.stimme_manuell = None
    z.medien.speichern()
    z.daten_geaendert()
    return s


@router.put("/api/musik/{mid}")
async def musik_umbenennen(request: Request, mid: str, d: Name):
    z = zentrale(request)
    s = z.medien.musik(mid)
    if not s:
        raise _nicht_gefunden("Dieses Stück")
    s.name = d.name
    z.medien.speichern()
    z.daten_geaendert()
    return s


@router.delete("/api/musik/{mid}")
async def musik_loeschen(request: Request, mid: str):
    z = zentrale(request)
    if not z.medien.musik_loeschen(mid):
        raise _nicht_gefunden("Dieses Stück")
    z.daten_geaendert()
    return {"ok": True}


@router.get("/api/musik/{mid}/datei")
async def musik_datei(request: Request, mid: str):
    z = zentrale(request)
    if not ID_MUSTER.match(mid) or not z.medien.musik(mid):
        raise _nicht_gefunden("Dieses Stück")
    return FileResponse(z.medien.musik_pfad(mid), media_type="audio/mpeg")


@router.put("/api/playlist")
async def playlist(request: Request, d: Playlist):
    z = zentrale(request)
    ids = {m.id for m in z.medien.daten.musik}
    reihenfolge = [i for i in d.reihenfolge if i in ids]
    reihenfolge += [i for i in ids if i not in reihenfolge]  # nichts geht verloren
    z.medien.daten.playlist = d.model_copy(update={"reihenfolge": list(dict.fromkeys(reihenfolge))})
    z.medien.speichern()
    z.daten_geaendert()
    return z.medien.daten.playlist


@router.get("/stream/ambiente")
async def stream(request: Request, puffer_s: float = Query(0, ge=0, le=60)):
    return StreamingResponse(
        zentrale(request).ambiente.hoeren(puffer_s), media_type="audio/mpeg",
        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
    )


# ---------------------------------------------------------------------------
# Effekt-Sounds
# ---------------------------------------------------------------------------
def _fx_oder_404(z: Zentrale, fid: str) -> Effekt:
    f = z.medien.fx(fid)
    if not f:
        raise _nicht_gefunden("Diesen Effekt")
    return f


@router.post("/api/fx")
async def fx_hochladen(
    request: Request,
    datei: Annotated[UploadFile, File()],
    kategorie: Annotated[Kategorie, Form()] = "sonstiges",
    name: Annotated[str, Form()] = "",
):
    z = zentrale(request)
    roh = await _hochladen(datei, FX_MAX_MB)
    try:
        fid, dauer_ms, groesse = await fx_umwandeln(roh)
    except KonvertierFehler as e:
        raise HTTPException(400, f"Umwandlung fehlgeschlagen: {e}") from e
    finally:
        roh.unlink(missing_ok=True)
    if not z.medien.fx(fid) and z.medien.fx_belegt_kb() + groesse // 1024 + 4 > z.fx_kapazitaet_kb():
        z.medien.fx_pfad(fid).unlink(missing_ok=True)
        z.medien.fx_vorhoer_pfad(fid).unlink(missing_ok=True)
        raise HTTPException(507, "Der Speicher des Sound-ESP ist voll – lösche erst andere Effekte")
    licht = LICHT_JE_KATEGORIE.get(kategorie)
    f = z.medien.fx_hinzu(Effekt(
        id=fid, name=(name.strip() or _anzeigename(datei.filename))[:60], kategorie=kategorie,
        licht=licht.model_copy() if licht else None, dauer_ms=dauer_ms, groesse=groesse,
    ))
    z.protokollieren(f"Effekt hochgeladen: {f.name} ({dauer_ms / 1000:.1f} s)", "info")
    await z.fx_sync()
    z.daten_geaendert()
    return f


@router.put("/api/fx/{fid}")
async def fx_aendern(request: Request, fid: str, d: EffektAenderung):
    z = zentrale(request)
    f = _fx_oder_404(z, fid)
    f.name, f.kategorie, f.vol, f.licht = d.name, d.kategorie, d.vol, d.licht
    z.medien.speichern()
    z.daten_geaendert()
    return f


@router.delete("/api/fx/{fid}")
async def fx_loeschen(request: Request, fid: str):
    z = zentrale(request)
    if not z.medien.fx_loeschen(fid):
        raise _nicht_gefunden("Diesen Effekt")
    await z.fx_sync()
    z.daten_geaendert()
    return {"ok": True}


@router.post("/api/fx/{fid}/abspielen")
async def fx_abspielen(request: Request, fid: str, d: Abspielen = Body(default_factory=Abspielen)):
    z = zentrale(request)
    await z.fx_spielen(_fx_oder_404(z, fid), d.vol, quelle="Hand")
    return {"ok": True}


@router.post("/api/fx/kategorie/{kategorie}/abspielen")
async def fx_kategorie(request: Request, kategorie: Kategorie, d: Abspielen = Body(default_factory=Abspielen)):
    f = await zentrale(request).fx_kategorie(kategorie, d.vol, quelle="Hand")
    if not f:
        raise HTTPException(404, "In dieser Kategorie gibt es noch keine Sounds")
    return {"ok": True, "id": f.id}


def _fx_datei(z: Zentrale, fid: str, vorhoeren: bool) -> FileResponse:
    if not ID_MUSTER.match(fid):
        raise _nicht_gefunden("Diesen Effekt")
    pfad = z.medien.fx_vorhoer_pfad(fid) if vorhoeren else z.medien.fx_pfad(fid)
    if not pfad.exists():
        raise _nicht_gefunden("Diesen Effekt")
    return FileResponse(pfad, media_type="audio/mpeg" if vorhoeren else "audio/wav")


@router.get("/api/fx/{fid}/vorhoeren")
async def fx_vorhoeren(request: Request, fid: str):
    return _fx_datei(zentrale(request), fid, True)


@router.get("/api/geraet/fx/{fid}")
async def fx_fuer_geraet(request: Request, fid: str):
    return _fx_datei(zentrale(request), fid, False)


# ---------------------------------------------------------------------------
# Einstellungen & Geräte
# ---------------------------------------------------------------------------
@router.put("/api/einstellungen/{teil}")
async def einstellungen(request: Request, teil: str, d: dict = Body()):
    try:
        await zentrale(request).einstellungen_aendern(teil, d)
    except KeyError as e:
        raise _nicht_gefunden("Diesen Einstellungsbereich") from e
    except ValidationError as e:
        raise HTTPException(422, e.errors(include_url=False, include_context=False)) from e
    return zentrale(request).einstellungen_oeffentlich()[teil]


@router.get("/api/ha/sensoren")
async def ha_sensoren(request: Request):
    ha = zentrale(request).einst.homeassistant
    try:
        return await homeassistant.sensoren(ha.url, ha.token)
    except Exception as e:
        raise HTTPException(502, f"Home Assistant: {e}") from e


@router.post("/api/latenz/test")
async def latenz_test(request: Request):
    await zentrale(request).latenz_test()
    return {"ok": True}


@router.post("/api/geraete/{rolle}/neustart")
async def geraet_neustart(request: Request, rolle: str):
    if rolle not in ROLLEN:
        raise _nicht_gefunden("Dieses Gerät")
    if not await zentrale(request).geraet_neustart(rolle):
        raise HTTPException(409, "Gerät ist nicht verbunden")
    return {"ok": True}


# ---------------------------------------------------------------------------
# WebSockets
# ---------------------------------------------------------------------------
@router.websocket("/ws/ui")
async def ws_ui(ws: WebSocket):
    z: Zentrale = ws.app.state.zentrale
    await ws.accept()
    await z.ui_anmelden(ws)
    try:
        while True:
            await ws.receive_text()  # die Oberfläche schickt Befehle per REST
    except WebSocketDisconnect:
        pass
    finally:
        await z.ui_abmelden(ws)


@router.websocket("/ws/geraet")
async def ws_geraet(ws: WebSocket, rolle: str = Query(""), fw: str = Query("")):
    z: Zentrale = ws.app.state.zentrale
    if rolle not in ROLLEN:
        await ws.close(code=1008)
        return
    await ws.accept()
    g = Geraet(rolle, ws, fw)
    await z.geraet_verbunden(g)
    try:
        while True:
            text = await ws.receive_text()
            try:
                msg = json.loads(text)
            except ValueError:
                continue
            if isinstance(msg, dict):
                await z.geraet_nachricht(g, msg)
    except WebSocketDisconnect:
        pass
    finally:
        await z.geraet_getrennt(g)


def aufraeumen() -> None:
    """Reste abgebrochener Uploads entfernen."""
    shutil.rmtree(pfade.TMP, ignore_errors=True)
    pfade.TMP.mkdir(parents=True, exist_ok=True)
