#!/usr/bin/env python3
"""Simuliert Licht- und Sound-ESP – zum Testen des Servers ohne Hardware.

    python werkzeuge/sim_geraete.py --server http://localhost:8095 [--dauer 60]

Verbindet sich wie die echten ESPs mit /ws/geraet, meldet Status, lädt Effekt-Dateien
beim fx_sync herunter (und prüft das WAV-Format), hört den Ambiente-Stream mit und gibt
alles aus, was der Server schickt. Beim Licht-ESP werden "werte" für die Live-Vorschau
erzeugt.
"""

import argparse
import asyncio
import json
import math
import struct
import time
import urllib.request

import websockets


def pruefe_wav(daten: bytes) -> str:
    if daten[:4] != b"RIFF" or daten[8:12] != b"WAVE":
        return "kein WAV"
    pos = 12
    while pos + 8 <= len(daten):
        kennung, laenge = daten[pos:pos + 4], struct.unpack("<I", daten[pos + 4:pos + 8])[0]
        if kennung == b"fmt ":
            fmt, kanaele, rate = struct.unpack("<HHI", daten[pos + 8:pos + 16])
            bits = struct.unpack("<H", daten[pos + 22:pos + 24])[0]
            if (fmt, kanaele, bits) != (0x11, 1, 4):
                return f"falsches Format {fmt:#x}/{kanaele}ch/{bits}bit"
            return f"ok (IMA-ADPCM, {rate} Hz)"
        pos += 8 + laenge + (laenge & 1)
    return "fmt fehlt"


class Licht:
    def __init__(self, basis: str, log) -> None:
        self.basis, self.log = basis, log
        self.zustand: dict = {}
        self.aktionen: list[dict] = []
        self.vorschau = False

    async def lauf(self) -> None:
        url = self.basis.replace("http", "ws", 1) + "/ws/geraet?rolle=licht&fw=1.0.0"
        async with websockets.connect(url) as ws:
            await ws.send(json.dumps({"t": "hallo", "rolle": "licht", "fw": "1.0.0", "zonen": 5,
                                      "mac": "00:00:00:00:00:01", "ip": "127.0.0.1"}))
            empfang = asyncio.create_task(self._empfang(ws))
            t0 = time.monotonic()
            try:
                while True:
                    await ws.send(json.dumps({"t": "status", "rssi": -55, "uptime": int(time.monotonic() - t0),
                                              "heap": 200000, "fps": 100, "uart": True, "musik": 0, "fx": 0}))
                    for _ in range(20):
                        if self.vorschau:
                            t = time.monotonic()
                            v = [int(50 + 45 * math.sin(t * 2 + i)) for i in range(5)]
                            await ws.send(json.dumps({"t": "werte", "v": v}))
                        await asyncio.sleep(0.1)
            finally:
                empfang.cancel()

    async def _empfang(self, ws) -> None:
        async for roh in ws:
            m = json.loads(roh)
            if m["t"] == "zustand":
                self.zustand = m
            elif m["t"] == "aktion":
                self.aktionen.append(m)
            elif m["t"] == "vorschau":
                self.vorschau = m["an"]
            self.log("licht", m)


class Sound:
    def __init__(self, basis: str, log) -> None:
        self.basis, self.log = basis, log
        self.dateien: dict[str, int] = {}
        self.fx: list[dict] = []
        self.stream_bytes = 0
        self._stream: asyncio.Task | None = None

    async def lauf(self) -> None:
        url = self.basis.replace("http", "ws", 1) + "/ws/geraet?rolle=sound&fw=1.0.0"
        async with websockets.connect(url) as ws:
            self.ws = ws
            await ws.send(json.dumps({"t": "hallo", "rolle": "sound", "fw": "1.0.0", "mac": "00:00:00:00:00:02",
                                      "ip": "127.0.0.1", "fx_frei_kb": 2200, "fx_gesamt_kb": 2240}))
            empfang = asyncio.create_task(self._empfang(ws))
            try:
                while True:
                    await ws.send(json.dumps({"t": "status", "rssi": -60, "heap": 150000, "musik": bool(self._stream),
                                              "musik_spielt": self.stream_bytes > 0, "puffer": 60, "stimmen": 0,
                                              "unterlaeufe": 0, "fx_frei_kb": 2200 - sum(self.dateien.values()) // 1024,
                                              "fx_gesamt_kb": 2240, "uart": True, "sync": False}))
                    await asyncio.sleep(2)
            finally:
                empfang.cancel()
                if self._stream:
                    self._stream.cancel()

    async def _empfang(self, ws) -> None:
        async for roh in ws:
            m = json.loads(roh)
            self.log("sound", m)
            if m["t"] == "fx_sync":
                await self._sync(ws, m)
            elif m["t"] == "fx":
                self.fx.append(m)
                if m["id"] not in self.dateien:
                    await ws.send(json.dumps({"t": "fx_fehlt", "id": m["id"]}))
            elif m["t"] == "musik":
                if m["aktion"] == "play" and not self._stream:
                    self._stream = asyncio.create_task(self._hoeren(m["pfad"]))
                elif m["aktion"] == "stop" and self._stream:
                    self._stream.cancel()
                    self._stream = None

    async def _sync(self, ws, m: dict) -> None:
        soll = {e["id"]: e["groesse"] for e in m["liste"]}
        geloescht = [i for i in self.dateien if i not in soll]
        for i in geloescht:
            del self.dateien[i]
        geladen = fehler = 0
        for fid, groesse in soll.items():
            if self.dateien.get(fid) == groesse:
                continue
            daten = await asyncio.to_thread(lambda: urllib.request.urlopen(self.basis + m["basis"] + fid).read())
            pruefung = pruefe_wav(daten)
            if len(daten) == groesse and pruefung.startswith("ok"):
                self.dateien[fid] = groesse
                geladen += 1
            else:
                fehler += 1
            print(f"  [sound] {fid}: {len(daten)} Bytes, {pruefung}")
        await ws.send(json.dumps({"t": "fx_sync_fertig", "geladen": geladen, "geloescht": len(geloescht),
                                  "fehler": fehler, "meldung": "", "fx_frei_kb": 2000}))

    async def _hoeren(self, pfad: str) -> None:
        """Wie Pi und Firmware: Endet der Stream (Server-Neustart, Sprung zu einem Stück), sofort neu verbinden."""
        while True:
            try:
                await self._hoeren_einmal(pfad)
            except (OSError, asyncio.IncompleteReadError) as e:
                print(f"  [sound] Stream: {e}")
            await asyncio.sleep(1)

    async def _hoeren_einmal(self, pfad: str) -> None:
        # wie die Firmware: HTTP/1.1 mit "Connection: close", Antwort ggf. "chunked"
        host, port = self.basis.split("//")[1].split(":")
        r, w = await asyncio.open_connection(host, int(port))
        w.write(f"GET {pfad} HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n".encode())
        await w.drain()
        kopf = await r.readuntil(b"\r\n\r\n")
        chunked = b"transfer-encoding: chunked" in kopf.lower()
        print(f"  [sound] Stream: {kopf.splitlines()[0].decode()}{' (chunked)' if chunked else ''}")
        try:
            while True:
                if chunked:
                    laenge = int((await r.readline()).split(b";")[0].strip() or b"0", 16)
                    if laenge == 0:
                        break
                    daten = await r.readexactly(laenge)
                    await r.readexactly(2)
                else:
                    daten = await r.read(4096)
                    if not daten:
                        break
                if self.stream_bytes == 0 and daten[:1] not in (b"\xff", b"I"):
                    print(f"  [sound] Stream beginnt nicht mit MP3-Daten: {daten[:8]!r}")
                self.stream_bytes += len(daten)
        finally:
            w.close()


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--server", default="http://localhost:8095")
    ap.add_argument("--dauer", type=float, default=0, help="nach so vielen Sekunden beenden (0 = nie)")
    ap.add_argument("--leise", action="store_true", help="Nachrichten nicht ausgeben")
    a = ap.parse_args()

    def log(rolle: str, m: dict) -> None:
        if not a.leise:
            print(f"[{rolle}] <- {json.dumps(m, ensure_ascii=False)[:200]}")

    licht, sound = Licht(a.server, log), Sound(a.server, log)
    aufgaben = [asyncio.create_task(licht.lauf()), asyncio.create_task(sound.lauf())]
    try:
        if a.dauer:
            await asyncio.sleep(a.dauer)
        else:
            await asyncio.gather(*aufgaben)
    finally:
        for t in aufgaben:
            t.cancel()
        print(f"\nZusammenfassung: Effekte auf dem Sound-ESP: {len(sound.dateien)}, "
              f"FX-Befehle: {len(sound.fx)}, Licht-Aktionen: {len(licht.aktionen)}, "
              f"Stream empfangen: {sound.stream_bytes / 1024:.0f} KB")


if __name__ == "__main__":
    asyncio.run(main())
