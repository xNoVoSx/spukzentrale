"""Rahmen für die Leitung zum Licht-ESP – Gegenstück zu firmware/common/protokoll.h.

Rahmen: 0xA5 0x5A <typ> <len> <payload> <prüfsumme>, Prüfsumme = XOR über typ, len und Payload.
JSON-Zeilen (Server-Nachrichten) in beide Richtungen: 0x1E <json> '\\n'.
"""

import json
import struct

SYNC1, SYNC2 = 0xA5, 0x5A
JSON_START = 0x1E
USB_BAUD = 115200   # wie protokoll.h: höher verliert der Pi 3 USB-Pakete des CH340
MAX_PAYLOAD = 32

MSG_HUELLE = 1
MSG_HERZSCHLAG = 2
MSG_AKTION = 3
MSG_SPEKTRUM = 4

FLAG_MUSIK_AKTIV = 1 << 0
FLAG_FX_AKTIV = 1 << 1
FLAG_SERVER = 1 << 2

# Reihenfolge wie enum Aktion in protokoll.h
AKTIONEN = ["keine", "blitz", "env_blitz", "schreck", "blackout", "welle", "alle_an", "puls", "test", "flackern",
            "env_puls"]


def rahmen(typ: int, payload: bytes = b"") -> bytes:
    if len(payload) > MAX_PAYLOAD:
        raise ValueError("Payload zu groß")
    chk = typ ^ len(payload)
    for b in payload:
        chk ^= b
    return bytes([SYNC1, SYNC2, typ, len(payload)]) + payload + bytes([chk])


def _byte(x: float) -> int:
    return max(0, min(255, int(x)))


def huelle(musik: float, fx: float, flags: int) -> bytes:
    return rahmen(MSG_HUELLE, bytes([_byte(musik), _byte(fx), flags & 0xFF]))


def spektrum(bass: float, mitte: float, hoehen: float, beat: float) -> bytes:
    return rahmen(MSG_SPEKTRUM, bytes([_byte(bass), _byte(mitte), _byte(hoehen), _byte(beat)]))


def herzschlag() -> bytes:
    return rahmen(MSG_HERZSCHLAG)


def zonen_maske(zonen) -> int:
    m = 0
    for z in zonen or []:
        try:
            z = int(z)
        except (TypeError, ValueError):
            continue
        if 0 <= z < 8:
            m |= 1 << z
    return m


def aktion(licht: dict, zusatz_verz_ms: int = 0) -> bytes | None:
    """Licht-Aktion eines Effekts (Feld "licht" der fx-Nachricht) als Rahmen; None ohne Licht."""
    name = str(licht.get("name", "keine"))
    if name not in AKTIONEN or name == "keine":
        return None

    def zahl(feld, std, lo, hi):
        try:
            return max(lo, min(hi, int(licht.get(feld, std))))
        except (TypeError, ValueError):
            return std

    payload = struct.pack(
        "<BBBBHHH",
        AKTIONEN.index(name),
        zonen_maske(licht.get("zonen")),
        zahl("staerke", 100, 0, 100),
        0,
        zahl("dauer_ms", 1000, 50, 60000),
        zahl("param", 0, 0, 65535),
        min(65535, zahl("verz_ms", 0, 0, 10000) + max(0, int(zusatz_verz_ms))),
    )
    return rahmen(MSG_AKTION, payload)


def json_zeile(nachricht: dict) -> bytes:
    return bytes([JSON_START]) + json.dumps(nachricht, separators=(",", ":"), ensure_ascii=False).encode() + b"\n"


class ZeilenLeser:
    """Zerlegt, was der Licht-ESP schreibt, in JSON-Nachrichten und Log-Zeilen."""

    def __init__(self) -> None:
        self._puffer = bytearray()

    def feed(self, daten: bytes) -> list[tuple[str, object]]:
        """Liefert [("json", dict) | ("log", str)]."""
        self._puffer += daten
        aus: list[tuple[str, object]] = []
        while True:
            i = self._puffer.find(b"\n")
            if i < 0:
                if len(self._puffer) > 65536:  # Müll ohne Zeilenende
                    self._puffer.clear()
                return aus
            zeile = bytes(self._puffer[:i]).rstrip(b"\r")
            del self._puffer[: i + 1]
            j = zeile.find(bytes([JSON_START]))
            if j >= 0:
                vorher, zeile = zeile[:j], zeile[j + 1:]
                if vorher.strip():
                    aus.append(("log", vorher.decode("utf-8", "replace").strip()))
                try:
                    obj = json.loads(zeile.decode("utf-8", "replace"))
                except ValueError:
                    continue
                if isinstance(obj, dict):
                    aus.append(("json", obj))
            elif zeile.strip():
                text = zeile.decode("utf-8", "replace").strip()
                # Boot-Rauschen des ROM (115200 Baud) wegfiltern: nur lesbare Zeilen
                if sum(c.isprintable() for c in text) >= 0.9 * len(text):
                    aus.append(("log", text))
