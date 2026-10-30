"""KI-Stimme: spricht Besucher passend zu dem an, was die Kamera sieht.

Ablauf (Auslöser vom Typ "ki"):
  1. Standbild der Kamera über Home Assistant (/api/camera_proxy, verkleinert).
  2. Bild-Sprachmodell über eine OpenAI-kompatible API (z. B. llama-server mit Gemma 3) schreibt einen Satz.
  3. Sprach-Dienst (ki/stimme.py, Chatterbox) macht daraus die Gruselstimme (WAV).
  4. Der Pi lädt die WAV über /api/geraet/sprache/<id> und spielt sie wie einen Effekt (mit Licht).

Geht etwas schief oder dauert es zu lange, spielt stattdessen ein Sound aus der Ersatz-Kategorie.
Das Standbild bleibt nur im Speicher und wird nirgends abgelegt.
"""

import asyncio
import base64
import collections
import json
import logging
import re
import secrets
import time
from collections.abc import Callable
from typing import TYPE_CHECKING

import httpx

from .vorgaben import LICHT_JE_KATEGORIE

if TYPE_CHECKING:
    from .zentrale import Zentrale

log = logging.getLogger(__name__)

STUFEN = {
    1: "Gruselstufe 1 von 3: freundlich-schaurig, eher lustig, auch für kleine Kinder.",
    2: "Gruselstufe 2 von 3: unheimlich und geheimnisvoll, aber nicht brutal.",
    3: "Gruselstufe 3 von 3: richtig gruselig und bedrohlich – aber ohne Gewalt, Blut oder Tod im Detail.",
}

SYSTEM = (
    "Du bist die Stimme eines Spukhauses an Halloween. Eine Kamera zeigt dir, wer gerade vor dem Haus steht. "
    "Sprich die Person oder Gruppe mit EINEM kurzen Satz auf Deutsch direkt an (höchstens 10 Wörter). "
    "Beziehe dich auf etwas deutlich Sichtbares: Kostüm, Verkleidung, Maske, Farbe, Gegenstand in der Hand, "
    "Süßigkeitentüte oder die Anzahl der Personen. Klinge wie ein Geist, der Besucher anlockt oder erschreckt. "
    "Regeln: kinderfreundlich, keine Beleidigungen, nichts über Körper, Gewicht, Hautfarbe, Alter oder Geschlecht, "
    "keine Namen. Schreibe nur den gesprochenen Satz: keine Emojis, keine Anführungszeichen, keine Regieanweisungen, "
    "keine Sternchen. Natürliche gesprochene Sprache, gern mit einer kurzen Pause (...). "
    "Sprich nur, wenn mindestens ein Mensch deutlich zu sehen ist – Fahrzeuge, Tiere, Gegenstände oder Deko "
    "allein zählen nicht. Ist das Bild schwarz-weiß (Nachtsicht), nenne keine Farben – nur Formen, Masken, "
    "Hüte, Umhänge, Gegenstände.\n"
    "Antworte als JSON: {\"mensch\": true/false, \"richtung\": kommt (aufs Haus bzw. die Kamera zu), geht (weg vom "
    "Haus, Rücken zur Kamera), steht oder unklar, \"satz\": der gesprochene Satz, leer wenn kein Mensch zu sehen ist, "
    "\"beschreibung\": sachliche Beschreibung in höchstens 25 Wörtern, was du siehst (Anzahl Personen, Kostüme bzw. "
    "Kleidung, Gegenstände, wo im Bild sie stehen – nur Äußeres der Verkleidung, keine Körpermerkmale)}\n"
    "Beispiele für den Stil:\n"
    "Hey, kleines Skelett! Komm her... ich hab eine Überraschung.\n"
    "Ihr drei Hexen... eure Besen gehören jetzt mir.\n"
    "Du mit der Kürbistüte... teilst du mit mir?\n"
    "Schöne Vampirzähne... meine sind spitzer.\n"
    "Du in der roten Jacke... ich hab auf dich gewartet."
)

LETZTE = 6          # so viele Sätze merkt sie sich, um sich nicht zu wiederholen
SPRACHE_HALTEN = 8  # so viele fertige WAVs hält der Server für den Pi bereit
VERLAUF = 20        # so viele Einträge zeigt die KI-Zentrale


class KiFehler(Exception):
    pass


def satz_bereinigen(text: str) -> str:
    """Antwort des Modells auf einen sprechbaren Satz kürzen."""
    t = re.sub(r"\*[^*]*\*|\(.*?\)|\[.*?\]", "", text or "")       # Regieanweisungen
    t = re.sub(r"[*_#\"„“”«»]", "", t).strip()
    t = t.splitlines()[0].strip() if t else ""
    t = t.lstrip(".… ").strip()
    return t[:200]


# Reihenfolge zählt: der Satz kommt vor der Beschreibung, damit die Stimme schon starten kann (Streaming)
ANTWORT_SCHEMA = {
    "type": "object",
    "properties": {
        "mensch": {"type": "boolean"},
        "richtung": {"type": "string", "enum": ["kommt", "geht", "steht", "unklar"]},
        "satz": {"type": "string"},
        "beschreibung": {"type": "string"},
    },
    "required": ["mensch", "richtung", "satz", "beschreibung"],
}
SATZ_FERTIG = re.compile(r'"mensch"\s*:\s*(true|false)\s*,\s*(?:"richtung"\s*:\s*"[^"]*"\s*,\s*)?'
                         r'"satz"\s*:\s*"((?:[^"\\]|\\.)*)"')


def satz_aus_teil(text: str) -> str | None:
    """Satz aus einer noch unvollständigen JSON-Antwort, sobald er komplett ist (sonst None)."""
    m = SATZ_FERTIG.search(text)
    if not m:
        return None
    if m.group(1) == "false":
        return ""
    try:
        satz = json.loads(f'"{m.group(2)}"')
    except ValueError:
        return None
    satz = satz_bereinigen(satz)
    return "" if satz == "-" else satz


def richtung_lesen(text: str) -> str:
    m = re.search(r'"richtung"\s*:\s*"(kommt|geht|steht)"', text or "")
    return m.group(1) if m else "unklar"


def antwort_lesen(text: str) -> tuple[str, str]:
    """JSON-Antwort des Modells -> (Beschreibung, Satz). Kommt kein JSON, gilt der Text als Satz."""
    try:
        d = json.loads(text)
    except (ValueError, TypeError):
        satz = satz_bereinigen(text)
        return "", "" if satz == "-" else satz
    if not isinstance(d, dict):
        return "", ""
    beschreibung = str(d.get("beschreibung") or "").strip()[:400]
    satz = satz_bereinigen(str(d.get("satz") or "")) if d.get("mensch", True) else ""
    return beschreibung, "" if satz == "-" else satz


class KiStimme:
    def __init__(self, z: "Zentrale") -> None:
        self.z = z
        self.letzte: collections.deque[str] = collections.deque(maxlen=LETZTE)
        self.sprache: collections.OrderedDict[str, bytes] = collections.OrderedDict()
        self.laeuft = False
        self.abbrechen = False   # z. B. geklingelt, während die KI noch nachdenkt -> nicht mehr sprechen
        self.verlauf: collections.deque[dict] = collections.deque(maxlen=VERLAUF)
        self.letztes_bild: bytes | None = None   # nur im Speicher, für die KI-Zentrale
        self.letztes_bild_zeit = 0.0

    # ------------------------------------------------------------------
    def wav(self, sid: str) -> bytes | None:
        return self.sprache.get(sid)

    def _ablegen(self, daten: bytes) -> str:
        sid = secrets.token_hex(6)
        self.sprache[sid] = daten
        while len(self.sprache) > SPRACHE_HALTEN:
            self.sprache.popitem(last=False)
        return sid

    # ------------------------------------------------------------------
    async def _standbild(self, c: httpx.AsyncClient, kamera: str, breite: int | None = None) -> bytes:
        ha = self.z.einst.homeassistant
        if not (ha.url and ha.token):
            raise KiFehler("Home Assistant ist nicht eingerichtet")
        r = await c.get(f"{ha.url.rstrip('/')}/api/camera_proxy/{kamera}",
                        params={"width": breite or self.z.einst.ki.bildbreite},
                        headers={"Authorization": f"Bearer {ha.token}"})
        if r.status_code != 200:
            raise KiFehler(f"Standbild {kamera}: HTTP {r.status_code}")
        return r.content

    async def _satz(self, c: httpx.AsyncClient, bild: bytes, lage: str = "",
                    bei_satz: Callable[[str, str], None] | None = None) -> tuple[str, str, str]:
        """(Beschreibung, Satz, Richtung) – Satz leer, wenn kein Mensch zu sehen ist.

        Liest die Antwort als Stream: bei_satz(satz, richtung) kommt, sobald der Satz fertig ist – die Beschreibung danach.
        """
        k = self.z.einst.ki
        hinweis = [STUFEN.get(self.z.zustand.gruselstufe, STUFEN[2])]
        if lage.strip():
            hinweis.append("Deine Aufgabe in diesem Moment (der Satz muss das erfüllen): " + lage.strip())
        if k.zusatz.strip():
            hinweis.append(k.zusatz.strip())
        if self.letzte:
            hinweis.append("Sag etwas anderes als: " + " | ".join(self.letzte))
        hinweis.append("Deine Antwort (JSON):")
        anfrage = {
            "messages": [
                {"role": "system", "content": SYSTEM},
                {"role": "user", "content": [
                    {"type": "image_url",
                     "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(bild).decode()}},
                    {"type": "text", "text": "\n".join(hinweis)},
                ]},
            ],
            "max_tokens": 200,
            "temperature": 0.9,
            "response_format": {"type": "json_schema", "json_schema": {"name": "antwort", "schema": ANTWORT_SCHEMA}},
            "stream": True,
        }
        if k.modell:
            anfrage["model"] = k.modell
        text, gemeldet = "", False
        # llama-server schließt nach einem Stream die Verbindung – nicht wiederverwenden
        async with c.stream("POST", f"{k.llm_url.rstrip('/')}/v1/chat/completions", json=anfrage,
                            headers={"Connection": "close"}) as r:
            if r.status_code != 200:
                raise KiFehler(f"Sprachmodell: HTTP {r.status_code}")
            async for zeile in r.aiter_lines():
                if not zeile.startswith("data: ") or zeile == "data: [DONE]":
                    continue
                try:
                    text += json.loads(zeile[6:])["choices"][0]["delta"].get("content") or ""
                except (ValueError, KeyError, IndexError):
                    continue
                if bei_satz and not gemeldet:
                    satz = satz_aus_teil(text)
                    if satz is not None:
                        gemeldet = True
                        bei_satz(satz, richtung_lesen(text))
        return (*antwort_lesen(text), richtung_lesen(text))

    async def _sprechen(self, c: httpx.AsyncClient, text: str) -> bytes:
        k = self.z.einst.ki
        r = await c.post(f"{k.tts_url.rstrip('/')}/sprechen", json={"text": text, "stimme": k.stimme})
        if r.status_code != 200:
            raise KiFehler(f"Sprach-Dienst: HTTP {r.status_code} {r.text[:100]}")
        return r.content

    # ------------------------------------------------------------------
    async def ansprechen(self, kamera: str, vol: int = 100, quelle: str = "KI-Stimme", lage: str = "",
                         reaktion: bool = True, still_bei: tuple[str, ...] = ()) -> str:
        """Kamera ansehen, Satz sprechen lassen.

        Ergebnis: "gesagt", "niemand" (kein Mensch im Bild), "still" (Richtung in still_bei, z. B. Leute gehen),
        "fehler" (KI weg, zu langsam …) oder "beschaeftigt".
        Den Ersatz-Sound bei "niemand"/"fehler" spielt der Aufrufer (je Auslöser einstellbar).
        """
        k = self.z.einst.ki
        if self.laeuft:
            return "beschaeftigt"
        self.laeuft = True
        self.abbrechen = False
        t0 = time.monotonic()
        eintrag: dict = {"zeit": time.time(), "quelle": quelle, "kamera": kamera, "satz": "", "beschreibung": "",
                         "richtung": "unklar", "zeiten": {},
                         "ergebnis": "fehler", "grund": ""}
        zeiten = eintrag["zeiten"]
        self._melden({"t": "ki", "laeuft": True, "quelle": quelle})
        if k.reaktion_kategorie and reaktion:
            # sofort ein kurzes Flüstern + Flackern: der Geist hat die Leute bemerkt, der Satz kommt gleich
            await self.z.fx_kategorie(k.reaktion_kategorie, vol)
        try:
            if not (k.llm_url and k.tts_url):
                raise KiFehler("Adressen für Sprachmodell/Sprach-Dienst fehlen")
            async with httpx.AsyncClient(timeout=k.zeitlimit_s) as c:
                async with asyncio.timeout(k.zeitlimit_s):
                    bild = await self._standbild(c, kamera)
                    zeiten["bild"] = round(time.monotonic() - t0, 1)
                    self.letztes_bild, self.letztes_bild_zeit = bild, eintrag["zeit"]
                    # Satz kommt per Stream vor der Beschreibung: Stimme startet, während das Modell weiterschreibt
                    satz_da: asyncio.Future[tuple[str, str]] = asyncio.get_running_loop().create_future()
                    denken = asyncio.create_task(self._satz(
                        c, bild, lage, lambda s, r: satz_da.done() or satz_da.set_result((s, r))))
                    try:
                        await asyncio.wait({satz_da, denken}, return_when=asyncio.FIRST_COMPLETED)
                        satz, richtung = satz_da.result() if satz_da.done() else denken.result()[1:]
                        zeiten["text"] = round(time.monotonic() - t0, 1)
                        if len(satz) >= 4 and richtung in still_bei:
                            eintrag["beschreibung"], _, eintrag["richtung"] = await denken
                            eintrag["ergebnis"], eintrag["grund"] = "still", f"Richtung „{richtung}“ – nicht ansprechen"
                            self.z.protokollieren(f"{quelle}: Leute gehen – bleibt still", "info")
                            return "still"
                        if len(satz) < 4:
                            eintrag["beschreibung"], _, eintrag["richtung"] = await denken
                            eintrag["ergebnis"] = "niemand"
                            self.z.protokollieren(f"{quelle}: kein Mensch zu erkennen", "info")
                            return "niemand"
                        eintrag["satz"] = satz
                        wav = await self._sprechen(c, satz)
                        zeiten["stimme"] = round(time.monotonic() - t0, 1)
                        eintrag["beschreibung"], _, eintrag["richtung"] = await denken
                    finally:
                        denken.cancel()
            self.letzte.append(satz)
            if self.abbrechen:
                eintrag["ergebnis"], eintrag["grund"] = "abgebrochen", "inzwischen wurde geklingelt"
                return "abgebrochen"
            await self._abspielen(wav, vol)
            eintrag["ergebnis"] = "gesagt"
            text = ", ".join(f"{n.capitalize()} {v}" for n, v in zeiten.items())
            self.z.protokollieren(f"{quelle}: „{satz}“ ({text} s)", "fx")
            log.info("KI-Stimme %s: %s", text, satz)
            return "gesagt"
        except Exception as e:  # noqa: BLE001 – jeder Fehler -> Ersatz, Show läuft weiter
            grund = "zu langsam" if isinstance(e, TimeoutError) else str(e) or type(e).__name__
            eintrag["grund"] = grund
            self.z.protokollieren(f"{quelle}: KI nicht erreichbar ({grund})", "fehler")
            log.warning("KI-Stimme: %s", grund)
            return "fehler"
        finally:
            self.laeuft = False
            self.verlauf.appendleft(eintrag)
            self._melden({"t": "ki", "laeuft": False, "eintrag": eintrag})

    def _melden(self, nachricht: dict) -> None:
        self.z.an_ui(nachricht)

    async def dienste(self) -> dict:
        """Erreichbarkeit von Sprachmodell und Sprach-Dienst (für die KI-Zentrale)."""
        k = self.z.einst.ki

        async def frage(url: str) -> dict:
            if not url:
                return {"ok": False, "text": "keine Adresse"}
            try:
                async with httpx.AsyncClient(timeout=2) as c:
                    r = await c.get(url)
                return {"ok": r.status_code == 200, "text": "erreichbar" if r.status_code == 200 else f"HTTP {r.status_code}",
                        "daten": r.json() if r.status_code == 200 else None}
            except Exception as e:  # noqa: BLE001
                return {"ok": False, "text": type(e).__name__}

        llm, tts = await asyncio.gather(frage(k.llm_url and f"{k.llm_url.rstrip('/')}/health"),
                                        frage(k.tts_url and f"{k.tts_url.rstrip('/')}/gesund"))
        tts["stimmen"] = (tts.pop("daten", None) or {}).get("stimmen", [])
        llm.pop("daten", None)
        return {"llm": llm, "tts": tts}

    async def live_bild(self, kamera: str, breite: int = 640) -> bytes:
        async with httpx.AsyncClient(timeout=5) as c:
            return await self._standbild(c, kamera, breite)

    async def _abspielen(self, wav: bytes, vol: int) -> None:
        sound = self.z.geraete.get("sound")
        if sound is None or self.z.stumm:
            raise KiFehler("kein Sound-Gerät verbunden")
        if not sound.hallo.get("kann_sagen"):
            raise KiFehler("Sound-Gerät kann noch keine KI-Stimme (Pi-Software aktualisieren)")
        sid = self._ablegen(wav)
        dauer_ms = wav_dauer_ms(wav)
        nachricht = {"t": "sag", "pfad": f"/api/geraet/sprache/{sid}",
                     "vol": round(self.z.einst.ki.vol * vol / 100)}
        licht = LICHT_JE_KATEGORIE.get("stimme")
        lg = self.z.geraete.get("licht")
        per_kabel = bool(sound.status.get("uart")) and bool(lg and lg.status.get("uart"))
        if licht and per_kabel:
            nachricht["licht"] = {**licht.model_dump(), "dauer_ms": dauer_ms + 300}
        await sound.senden(nachricht)
        if licht and not per_kabel:
            await self.z.licht.aktion(licht, self.z.ton_effektiv().latenz_ms, dauer_ms + 300)


def wav_dauer_ms(wav: bytes) -> int:
    """Dauer einer PCM-WAV aus dem Kopf (Sprach-Dienst liefert 16 bit)."""
    try:
        i = wav.index(b"fmt ")
        kanaele = int.from_bytes(wav[i + 10:i + 12], "little")
        rate = int.from_bytes(wav[i + 12:i + 16], "little")
        bits = int.from_bytes(wav[i + 22:i + 24], "little")
        j = wav.index(b"data")
        laenge = int.from_bytes(wav[j + 4:j + 8], "little")
        if laenge in (0, 0xFFFFFFFF):
            laenge = len(wav) - j - 8
        return int(laenge * 1000 / (rate * kanaele * bits // 8))
    except (ValueError, ZeroDivisionError):
        return 3000
