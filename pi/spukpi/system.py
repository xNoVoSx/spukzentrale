"""Gesundheit des Pi: Temperatur, Unterspannung/Drosselung, WLAN, Last, Speicher."""

import os
import shutil
import socket
import subprocess
import time
from pathlib import Path

GEDROSSELT_BITS = {
    0: "Unterspannung", 1: "Frequenz begrenzt", 2: "gedrosselt", 3: "Temperaturgrenze",
    16: "Unterspannung seit Start", 17: "Frequenz begrenzt seit Start", 18: "gedrosselt seit Start",
    19: "Temperaturgrenze seit Start",
}


def temperatur() -> float | None:
    try:
        return int(Path("/sys/class/thermal/thermal_zone0/temp").read_text()) / 1000
    except (OSError, ValueError):
        return None


def gedrosselt() -> int | None:
    try:
        aus = subprocess.run(["vcgencmd", "get_throttled"], capture_output=True, text=True, timeout=2).stdout
        return int(aus.strip().split("=")[1], 16)
    except Exception:
        return None


def gedrosselt_text(wert: int | None) -> list[str]:
    if not wert:
        return []
    return [t for bit, t in GEDROSSELT_BITS.items() if wert & (1 << bit)]


def wlan() -> tuple[int | None, str]:
    """(RSSI in dBm, Schnittstelle) aus /proc/net/wireless."""
    try:
        for zeile in Path("/proc/net/wireless").read_text().splitlines()[2:]:
            teile = zeile.split()
            return int(float(teile[3].rstrip("."))), teile[0].rstrip(":")
    except (OSError, ValueError, IndexError):
        pass
    return None, ""


def ip_adresse() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 1))      # TEST-NET, es wird nichts gesendet
            return s.getsockname()[0]
    except OSError:
        return ""


def mac_adresse(schnittstelle: str = "wlan0") -> str:
    try:
        return Path(f"/sys/class/net/{schnittstelle}/address").read_text().strip().upper()
    except OSError:
        return ""


def speicher_frei_kb() -> int:
    try:
        for zeile in Path("/proc/meminfo").read_text().splitlines():
            if zeile.startswith("MemAvailable:"):
                return int(zeile.split()[1])
    except (OSError, ValueError):
        pass
    return 0


_START = time.monotonic()


def status() -> dict:
    rssi, _ = wlan()
    g = gedrosselt()
    return {
        "rssi": rssi,
        "uptime": int(time.monotonic() - _START),
        "heap": speicher_frei_kb() * 1024,       # Feldname wie beim ESP (Anzeige „Speicher frei“)
        "pi_temp": temperatur(),
        "pi_last": round(os.getloadavg()[0], 2),
        "pi_gedrosselt": g,
        "pi_gedrosselt_text": gedrosselt_text(g),
        "pi_sd_frei_mb": shutil.disk_usage("/").free // (1024 * 1024),
    }
