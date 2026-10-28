"""USB-Brücke zum Licht-ESP (CH340, /dev/ttyUSB0).

- Pi -> ESP: Rahmen (Hüllkurve, Spektrum, Aktionen, Herzschlag) und Server-Nachrichten als JSON-Zeilen.
- ESP -> Pi: JSON-Zeilen (hallo, status, werte …) für den Server, alles andere ist Log.

Nur wenn die Brücke „aktiv“ ist, schickt der Pi Herzschläge – dann schaltet der ESP in den USB-Modus (WLAN aus).
Passiv liest der Pi nur das Log mit; der ESP bleibt im WLAN wie bisher.

Wichtig beim Öffnen: DTR/RTS bleiben beide gesetzt (pyserial-Standard). Wer sie einzeln umschaltet, zieht über
die Auto-Reset-Schaltung des Boards EN auf Low und startet den ESP neu.
"""

import logging
import threading
import time
from collections.abc import Callable

import serial

from . import protokoll

log = logging.getLogger(__name__)


class LichtBruecke:
    def __init__(self, port: str, aktiv: bool) -> None:
        self.port = port
        self.aktiv = aktiv
        self.bei_json: Callable[[dict], None] = lambda _m: None
        self.bei_log: Callable[[str], None] = lambda _t: None
        self._ser: serial.Serial | None = None
        self._schreibsperre = threading.Lock()
        self._laeuft = False
        self.letzte_antwort = 0.0       # letzte JSON-Zeile vom ESP
        self.geoeffnet = False
        self.fehler_schreiben = 0
        self.pausiert = False           # beim Flashen: Port freigeben

    @property
    def esp_da(self) -> bool:
        return time.monotonic() - self.letzte_antwort < 6.0

    def start(self) -> None:
        self._laeuft = True
        threading.Thread(target=self._lesen, name="licht-usb", daemon=True).start()
        threading.Thread(target=self._herzschlag, name="licht-herz", daemon=True).start()

    def stop(self) -> None:
        self._laeuft = False

    # ------------------------------------------------------------------
    def _oeffnen(self) -> bool:
        try:
            s = serial.Serial(self.port, protokoll.USB_BAUD, timeout=0.2, write_timeout=0.5)
        except (serial.SerialException, OSError) as e:
            if self.geoeffnet or not hasattr(self, "_gemeldet"):
                log.warning("Licht-ESP: %s nicht verfügbar (%s)", self.port, e)
                self._gemeldet = True
            self.geoeffnet = False
            return False
        self._ser = s
        self.geoeffnet = True
        log.info("Licht-ESP: %s geöffnet (%d Baud)", self.port, protokoll.USB_BAUD)
        return True

    def _schliessen(self) -> None:
        s, self._ser = self._ser, None
        self.geoeffnet = False
        if s:
            try:
                s.close()
            except Exception:
                pass

    def _lesen(self) -> None:
        leser = protokoll.ZeilenLeser()
        while self._laeuft:
            if self.pausiert:
                self._schliessen()
                time.sleep(0.5)
                continue
            if self._ser is None and not self._oeffnen():
                time.sleep(3)
                continue
            try:
                daten = self._ser.read(4096)
            except (serial.SerialException, OSError) as e:
                log.warning("Licht-ESP: Lesefehler (%s) – Port wird neu geöffnet", e)
                self._schliessen()
                time.sleep(1)
                continue
            if not daten:
                continue
            for art, inhalt in leser.feed(daten):
                if art == "json":
                    self.letzte_antwort = time.monotonic()
                    try:
                        self.bei_json(inhalt)
                    except Exception:
                        log.exception("Licht-ESP: Nachricht nicht verarbeitet")
                else:
                    self.bei_log(inhalt)

    def _herzschlag(self) -> None:
        while self._laeuft:
            if self.aktiv:
                self.schreiben(protokoll.herzschlag())
            time.sleep(1.0)

    # ------------------------------------------------------------------
    def schreiben(self, daten: bytes) -> bool:
        s = self._ser
        if s is None or self.pausiert:
            return False
        try:
            with self._schreibsperre:
                s.write(daten)
            return True
        except (serial.SerialException, OSError):
            self.fehler_schreiben += 1
            return False

    def json_senden(self, nachricht: dict) -> bool:
        return self.schreiben(protokoll.json_zeile(nachricht))
