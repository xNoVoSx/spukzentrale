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
import logging
import re
import secrets
import time
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
    "Sprich die Person oder Gruppe mit EINEM kurzen Satz auf Deutsch direkt an (höchstens 16 Wörter). "
    "Beziehe dich auf etwas deutlich Sichtbares: Kostüm, Verkleidung, Maske, Farbe, Gegenstand in der Hand, "
    "Süßigkeitentüte oder die Anzahl der Personen. Klinge wie ein Geist, der Besucher anlockt oder erschreckt. "
    "Regeln: kinderfreundlich, keine Beleidigungen, nichts über Körper, Gewicht, Hautfarbe, Alter oder Geschlecht, "
    "keine Namen. Schreibe nur den gesprochenen Satz: keine Emojis, keine Anführungszeichen, keine Regieanweisungen, "
    "keine Sternchen. Natürliche gesprochene Sprache, gern mit einer kurzen Pause (...). "
    "Ist auf dem Bild keine Person zu erkennen, antworte nur mit einem einzelnen Minus: -\n"
    "Beispiele für den Stil:\n"
    "Hey, kleines Skelett! Komm her... ich hab eine Überraschung für dich.\n"
    "Ihr drei Hexen da... eure Besen gehören jetzt mir.\n"
    "Na, du mit der Kürbistüte... teilst du mit einem hungrigen Geist?\n"
    "Schöne Vampirzähne... aber meine sind spitzer.\n"
    "Du in der roten Jacke... ich habe schon so lange auf dich gewartet."
)

LETZTE = 6          # so viele Sätze merkt sie sich, um sich nicht zu wiederholen
SPRACHE_HALTEN = 8  # so viele fertige WAVs hält der Server für den Pi bereit


class KiFehler(Exception):
    pass


def satz_bereinigen(text: str) -> str:
    """Antwort des Modells auf einen sprechbaren Satz kürzen."""
    t = re.sub(r"\*[^*]*\*|\(.*?\)|\[.*?\]", "", text or "")       # Regieanweisungen
    t = re.sub(r"[*_#\"„“”«»]", "", t).strip()
    t = t.splitlines()[0].strip() if t else ""
    t = t.lstrip(".… ").strip()
    return t[:200]


class KiStimme:
    def __init__(self, z: "Zentrale") -> None:
        self.z = z
        self.letzte: collections.deque[str] = collections.deque(maxlen=LETZTE)
        self.sprache: collections.OrderedDict[str, bytes] = collections.OrderedDict()
        self.laeuft = False

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
    async def _standbild(self, c: httpx.AsyncClient, kamera: str) -> bytes:
        ha = self.z.einst.homeassistant
        if not (ha.url and ha.token):
            raise KiFehler("Home Assistant ist nicht eingerichtet")
        r = await c.get(f"{ha.url.rstrip('/')}/api/camera_proxy/{kamera}",
                        params={"width": self.z.einst.ki.bildbreite},
                        headers={"Authorization": f"Bearer {ha.token}"})
        if r.status_code != 200:
            raise KiFehler(f"Standbild {kamera}: HTTP {r.status_code}")
        return r.content

    async def _satz(self, c: httpx.AsyncClient, bild: bytes, lage: str = "") -> str:
        k = self.z.einst.ki
        hinweis = [STUFEN.get(self.z.zustand.gruselstufe, STUFEN[2])]
        if lage.strip():
            hinweis.append(lage.strip())
        if k.zusatz.strip():
            hinweis.append(k.zusatz.strip())
        if self.letzte:
            hinweis.append("Sag etwas anderes als: " + " | ".join(self.letzte))
        hinweis.append("Dein Satz:")
        anfrage = {
            "messages": [
                {"role": "system", "content": SYSTEM},
                {"role": "user", "content": [
                    {"type": "image_url",
                     "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(bild).decode()}},
                    {"type": "text", "text": "\n".join(hinweis)},
                ]},
            ],
            "max_tokens": 60,
            "temperature": 0.9,
        }
        if k.modell:
            anfrage["model"] = k.modell
        r = await c.post(f"{k.llm_url.rstrip('/')}/v1/chat/completions", json=anfrage)
        if r.status_code != 200:
            raise KiFehler(f"Sprachmodell: HTTP {r.status_code}")
        return satz_bereinigen(r.json()["choices"][0]["message"]["content"])

    async def _sprechen(self, c: httpx.AsyncClient, text: str) -> bytes:
        k = self.z.einst.ki
        r = await c.post(f"{k.tts_url.rstrip('/')}/sprechen", json={"text": text, "stimme": k.stimme})
        if r.status_code != 200:
            raise KiFehler(f"Sprach-Dienst: HTTP {r.status_code} {r.text[:100]}")
        return r.content

    # ------------------------------------------------------------------
    async def ansprechen(self, kamera: str, vol: int = 100, quelle: str = "KI-Stimme", lage: str = "") -> bool:
        """Kamera ansehen, Satz sprechen lassen. False = nichts gesagt (Ersatz-Sound läuft ggf.)."""
        k = self.z.einst.ki
        if self.laeuft:
            return False
        self.laeuft = True
        t0 = time.monotonic()
        zeiten: list[str] = []
        try:
            if not (k.llm_url and k.tts_url):
                raise KiFehler("Adressen für Sprachmodell/Sprach-Dienst fehlen")
            async with httpx.AsyncClient(timeout=k.zeitlimit_s) as c:
                async with asyncio.timeout(k.zeitlimit_s):
                    bild = await self._standbild(c, kamera)
                    zeiten.append(f"Bild {time.monotonic() - t0:.1f}")
                    satz = await self._satz(c, bild, lage)
                    zeiten.append(f"Text {time.monotonic() - t0:.1f}")
                    if not satz or satz == "-" or len(satz) < 4:
                        self.z.protokollieren(f"{quelle}: niemand zu erkennen", "info")
                        return False
                    wav = await self._sprechen(c, satz)
                    zeiten.append(f"Stimme {time.monotonic() - t0:.1f}")
            self.letzte.append(satz)
            await self._abspielen(wav, vol)
            self.z.protokollieren(f"{quelle}: „{satz}“ ({', '.join(zeiten)} s)", "fx")
            log.info("KI-Stimme %s: %s", ", ".join(zeiten), satz)
            return True
        except Exception as e:  # noqa: BLE001 – jeder Fehler -> Ersatz-Sound, Show läuft weiter
            grund = "zu langsam" if isinstance(e, TimeoutError) else str(e) or type(e).__name__
            self.z.protokollieren(f"{quelle}: KI nicht erreichbar ({grund}) – Ersatz-Sound", "fehler")
            log.warning("KI-Stimme: %s", grund)
            if k.ersatz_kategorie:
                await self.z.fx_kategorie(k.ersatz_kategorie, vol, quelle=f"{quelle} (Ersatz)")
            return False
        finally:
            self.laeuft = False

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
