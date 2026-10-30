"""REST-API (für die Weboberfläche und Home Assistant) und WebSockets."""

import asyncio
import json
import re
import logging
import shutil
import time
import uuid
from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, Body, File, Form, HTTPException, Query, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, Response, StreamingResponse
from pydantic import BaseModel, Field, ValidationError

from . import VERSION, homeassistant, pfade
from .geraete import ROLLEN, Geraet
from .medien import ID_MUSTER, KonvertierFehler, fx_umwandeln, musik_umwandeln
from .modelle import (
    Ausloeser, Effekt, Ereignisse, Kategorie, LichtAktion, Musikstueck, Playlist, Stimmung, Szene, Zone, neue_id,
)
from .vorgaben import LICHT_JE_KATEGORIE
from . import fxanalyse
from .ki import bild_typ
from .zentrale import Zentrale

log = logging.getLogger(__name__)
router = APIRouter()

MUSIK_MAX_MB = 300
FX_MAX_MB = 50


def zentrale(request: Request) -> Zentrale:
    return request.app.state.zentrale


def _nicht_gefunden(was: str) -> HTTPException:
    return HTTPException(404, f"{was} gibt es nicht")


class KiPause(BaseModel):
    minuten: float = Field(10, ge=0, le=240)


class KiTest(BaseModel):
    kamera: str = Field(min_length=8, max_length=120, pattern=r"^camera\.[a-z0-9_]+$")
    hinweis: str = Field("", max_length=300)


class An(BaseModel):
    an: bool


class TestSensor(BaseModel):
    rolle: Literal["fern", "nah", "klingel"]
    an: bool = True


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
    beschreibung: str = Field("", max_length=300)


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


@router.get("/api/diagnose")
async def diagnose(request: Request):
    """Alles für die Fehlersuche auf einen Blick: Geräte, Stream und Hörposition, Generator, HA/MQTT, Protokoll."""
    z = zentrale(request)
    jetzt = time.monotonic()
    geraete = {}
    for rolle in ROLLEN:
        g = z.geraete.get(rolle)
        geraete[rolle] = {"name": z.geraet_name(g, rolle), "verbunden": g is not None,
                          **({"fw": g.hallo.get("fw", g.fw), "ip": g.ip, "seit_s": round(time.time() - g.seit),
                              "letzte_nachricht_s": round(jetzt - g.letzte_nachricht, 1), "status": g.status}
                             if g else {})}
    stueck, pos = z.musik_lage()
    gesang = bool(stueck and pos is not None and any(a <= pos < b for a, b in stueck.stimme))
    gen = z.generator
    return {
        "version": VERSION,
        "zustand": {"show": z.zustand.licht.an, "ambiente": z.zustand.ambiente_an, "ereignisse": z.zustand.ereignisse_an,
                    "gruselstufe": z.zustand.gruselstufe, "ruhezeit": z.ruhezeit_aktiv},
        "geraete": geraete,
        "ambiente": {"laeuft": z.ambiente.laeuft, "zuhoerer": z.ambiente.zuhoerer, "vorlauf_s": z.ambiente.vorlauf_s,
                     "jetzt": z.ambiente.jetzt, "stueck_id": stueck.id if stueck else None,
                     "position_s": None if pos is None else round(pos, 1), "gesang_jetzt": gesang,
                     "analyse_laeuft": sorted(z.analyse_laeuft)},
        "generator": None if gen is None else {
            "naechstes_in_s": {rid: round(t - jetzt, 1) for rid, t in gen.naechstes.items()},
            "wartet_auf_musik": {rid: f.name for rid, f in gen.vorgemerkt.items()},
            "auf_ruhe_gewartet": sorted(gen.pause_gewartet)},
        "szene": (z.spieler.laufend or {}).get("name") if z.spieler else None,
        "homeassistant": z.ha_status, "mqtt": z.mqtt_status,
        "protokoll": list(z.protokoll)[:30],
    }


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
    ersetzen: Annotated[str, Form()] = "",
):
    z = zentrale(request)
    alt = z.medien.fx(ersetzen) if ersetzen else None
    if ersetzen and not alt:
        raise _nicht_gefunden("Den zu ersetzenden Effekt")
    # schon da (gleicher Name)? Dann nicht doppelt anlegen – der Inhalt allein erkennt das nicht mehr sicher,
    # seit Stille am Ende abgeschnitten wird (alter, ungekürzter Sound ≠ neu hochgeladener)
    anzeige = (name.strip() or _anzeigename(datei.filename))[:60]
    vorhanden = next((f for f in z.medien.daten.fx if f.name == anzeige), None)
    if vorhanden and not alt:
        return vorhanden
    roh = await _hochladen(datei, FX_MAX_MB)
    try:
        fid, dauer_ms, groesse = await fx_umwandeln(roh)
    except KonvertierFehler as e:
        raise HTTPException(400, f"Umwandlung fehlgeschlagen: {e}") from e
    finally:
        roh.unlink(missing_ok=True)
    kapazitaet = z.fx_kapazitaet_kb()
    if not z.medien.fx(fid) and kapazitaet and z.medien.fx_belegt_kb() + groesse // 1024 + 4 > kapazitaet:
        z.medien.fx_pfad(fid).unlink(missing_ok=True)
        z.medien.fx_vorhoer_pfad(fid).unlink(missing_ok=True)
        raise HTTPException(507, "Der Effekt-Speicher des Sound-Geräts ist voll – lösche erst andere Effekte")
    licht = LICHT_JE_KATEGORIE.get(kategorie)
    if alt:
        # neue Fassung eines vorhandenen Effekts: Name, Lautstärke, Beschreibung bleiben; Licht nur bei gleicher Kategorie
        neu = alt.model_copy(update={"id": fid, "kategorie": kategorie, "dauer_ms": dauer_ms, "groesse": groesse,
                                     "analyse": None, "licht": alt.licht if alt.kategorie == kategorie else
                                     (licht.model_copy() if licht else None)})
        await z.fx_ersetzen(alt.id, neu)
        z.protokollieren(f"Effekt ersetzt: {neu.name} ({alt.dauer_ms / 1000:.1f} s -> {dauer_ms / 1000:.1f} s)", "info")
        await z.fx_sync()
        z.daten_geaendert()
        return z.medien.fx(fid) or neu
    f = z.medien.fx_hinzu(Effekt(
        id=fid, name=anzeige, kategorie=kategorie,
        licht=licht.model_copy() if licht else None, dauer_ms=dauer_ms, groesse=groesse,
    ))
    z.protokollieren(f"Effekt hochgeladen: {f.name} ({dauer_ms / 1000:.1f} s)", "info")
    await z.fx_sync()
    await z.fx_analysieren(f.id)
    z.daten_geaendert()
    return z.medien.fx(f.id) or f


@router.put("/api/fx/{fid}")
async def fx_aendern(request: Request, fid: str, d: EffektAenderung):
    z = zentrale(request)
    f = _fx_oder_404(z, fid)
    f.name, f.kategorie, f.vol, f.licht, f.beschreibung = d.name, d.kategorie, d.vol, d.licht, d.beschreibung.strip()
    if f.analyse:
        f.analyse.vorschlag = fxanalyse.kategorie_vorschlag(f.name, f.kategorie, f.analyse.klassen)
    z.medien.speichern()
    z.daten_geaendert()
    return f


@router.post("/api/fx/pruefen")
async def fx_pruefen(request: Request, datei: Annotated[UploadFile, File()], name: Annotated[str, Form()] = ""):
    """Vor dem Hochladen: Kategorie aus Dateiname/Klang, schon vorhanden (gleicher Name) und gleich lang?
    Legt nichts ab."""
    z = zentrale(request)
    anzeige = (name.strip() or _anzeigename(datei.filename))[:60]
    roh = await _hochladen(datei, FX_MAX_MB)
    ordner = pfade.TMP / f"pruefen-{uuid.uuid4().hex}"
    ordner.mkdir(parents=True)
    try:
        fid, dauer_ms, _ = await fx_umwandeln(roh, ordner)
        a = await asyncio.get_running_loop().run_in_executor(
            None, fxanalyse.analysieren, ordner / f"{fid}.wav", anzeige, "sonstiges")
    except KonvertierFehler as e:
        raise HTTPException(400, f"Umwandlung fehlgeschlagen: {e}") from e
    finally:
        roh.unlink(missing_ok=True)
        shutil.rmtree(ordner, ignore_errors=True)
    kategorie, sicher, kandidaten = fxanalyse.kategorie_auto(anzeige, a.klassen)
    f = next((x for x in z.medien.daten.fx if x.name == anzeige), None)
    vorhanden = None
    if f:
        vorhanden = {"id": f.id, "name": f.name, "kategorie": f.kategorie, "dauer_ms": f.dauer_ms,
                     "gleich": min(abs(dauer_ms - f.dauer_ms), abs(dauer_ms - f.hoerbar_ms)) <= 400}
    return {"name": anzeige, "dauer_ms": dauer_ms, "klassen": a.klassen, "verlauf": a.verlauf,
            "kategorie": kategorie, "sicher": sicher, "kandidaten": kandidaten, "vorhanden": vorhanden}


@router.post("/api/fx/nachbearbeiten")
async def fx_nachbearbeiten(request: Request):
    """Stille am Ende vorhandener Effekte abschneiden (neue Uploads werden gleich gekürzt)."""
    return await zentrale(request).fx_nachbearbeiten()


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
async def ha_sensoren(request: Request, domain: Literal["binary_sensor", "camera"] = "binary_sensor"):
    ha = zentrale(request).einst.homeassistant
    try:
        return await homeassistant.sensoren(ha.url, ha.token, domain)
    except Exception as e:
        raise HTTPException(502, f"Home Assistant: {e}") from e


@router.post("/api/ki")
async def ki_schalten(request: Request, d: An):
    zentrale(request).ki_schalten(d.an)
    return {"ok": True}


@router.post("/api/regie")
async def regie_schalten(request: Request, d: An):
    zentrale(request).regie_schalten(d.an)
    return {"ok": True}


@router.post("/api/ki/test")
async def ki_test(request: Request, d: KiTest):
    """KI-Stimme sofort ausprobieren (ohne Sperrzeit), wartet auf das Ergebnis."""
    z = zentrale(request)
    if z.ki.laeuft:
        raise HTTPException(409, "Die KI-Stimme spricht gerade")
    return {"ergebnis": await z.ki.ansprechen(d.kamera, quelle="KI-Test", lage=d.hinweis)}


# ---------------------------------------------------------------------------
# PC-Test: Sensoren von Hand auslösen, Testbild statt Kamera (der Browser spielt dabei den Pi, siehe test.js)
# ---------------------------------------------------------------------------
@router.get("/api/test")
async def test_info(request: Request):
    z = zentrale(request)
    return {"testbild": z.ki.testbild is not None,
            "rollen": {r: next((a.name for a in z.einst.ausloeser if a.aktiv and a.rolle == r), None)
                       for r in ("fern", "nah", "klingel")}}


@router.post("/api/test/sensor")
async def test_sensor(request: Request, d: TestSensor):
    """Wie ein Personensensor bzw. die Klingel in Home Assistant: läuft genau den echten Weg (inkl. Sperren)."""
    try:
        return {"ausgeloest": await zentrale(request).test_sensor(d.rolle, d.an)}
    except KeyError:
        raise HTTPException(404, f"Kein aktiver Auslöser mit der Rolle „{d.rolle}“") from None


class SimulationStart(BaseModel):
    drehbuch: str = Field(max_length=40)


@router.get("/api/test/simulation")
async def simulation_info(request: Request):
    return zentrale(request).simulation.info()


@router.post("/api/test/simulation")
async def simulation_start(request: Request, d: SimulationStart):
    try:
        await zentrale(request).simulation.starten(d.drehbuch)
    except KeyError:
        raise HTTPException(404, "Dieses Drehbuch gibt es nicht") from None
    return zentrale(request).simulation.info()


@router.delete("/api/test/simulation")
async def simulation_stopp(request: Request):
    await zentrale(request).simulation.stoppen()
    return zentrale(request).simulation.info()


@router.post("/api/test/bild")
async def test_bild(request: Request, datei: Annotated[UploadFile, File()]):
    daten = await datei.read(8 * 1024 * 1024 + 1)
    if len(daten) > 8 * 1024 * 1024:
        raise HTTPException(413, "Bild zu groß (höchstens 8 MB)")
    if not (daten[:3] == b"\xff\xd8\xff" or bild_typ(daten) != "image/jpeg"):
        raise HTTPException(400, "Bitte ein JPG-, PNG- oder WebP-Bild")
    z = zentrale(request)
    z.ki.testbild = daten
    z.protokollieren("PC-Test: KI sieht jetzt das Testbild statt der Kamera", "info")
    return {"testbild": True}


@router.delete("/api/test/bild")
async def test_bild_weg(request: Request):
    z = zentrale(request)
    if z.ki.testbild is not None:
        z.ki.testbild = None
        z.protokollieren("PC-Test: KI sieht wieder die Kamera", "info")
    return {"testbild": False}


@router.get("/api/test/bild")
async def test_bild_zeigen(request: Request):
    bild = zentrale(request).ki.testbild
    if bild is None:
        raise _nicht_gefunden("Ein Testbild")
    return Response(bild, media_type=bild_typ(bild), headers={"Cache-Control": "no-store"})


@router.get("/api/ki/status")
async def ki_status(request: Request):
    z = zentrale(request)
    return {"laeuft": z.ki.laeuft, "verlauf": list(z.ki.verlauf), "bild_zeit": z.ki.letztes_bild_zeit,
            "besuch": z.besuch.info(), "regie": z.regie.info(), **await z.ki.dienste()}


@router.post("/api/ki/pause")
async def ki_pause(request: Request, d: KiPause):
    """Besuchs-Ablauf pausieren (z. B. wenn ihr selbst rausgeht), 0 = Pause beenden."""
    zentrale(request).besuch.pause(d.minuten)
    return zentrale(request).besuch.info()


@router.get("/api/ki/bild")
async def ki_bild(request: Request):
    """Das Bild, das die KI zuletzt angesehen hat (nur im Speicher)."""
    bild = zentrale(request).ki.letztes_bild
    if bild is None:
        raise _nicht_gefunden("Ein Bild")
    return Response(bild, media_type=bild_typ(bild), headers={"Cache-Control": "no-store"})


@router.get("/api/ki/live/{kamera}")
async def ki_live(request: Request, kamera: str):
    """Aktuelles Standbild einer Kamera (die KI-Zentrale holt es alle paar Sekunden, solange sie offen ist)."""
    if not re.fullmatch(r"camera\.[a-z0-9_]+", kamera):
        raise _nicht_gefunden("Diese Kamera")
    try:
        bild = await zentrale(request).ki.live_bild(kamera)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"Standbild: {e}") from e
    return Response(bild, media_type=bild_typ(bild), headers={"Cache-Control": "no-store"})


@router.get("/api/geraet/sprache/{sid}")
async def sprache_fuer_geraet(request: Request, sid: str):
    wav = zentrale(request).ki.wav(sid)
    if wav is None:
        raise _nicht_gefunden("Diese Ansage")
    return Response(wav, media_type="audio/wav")


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
