"""Licht-ESP über den Pi flashen (ersetzt USB am PC und OTA übers WLAN).

    sudo /opt/spukpi/venv/bin/python -m spukpi.flash firmware.bin

firmware.bin aus firmware/licht/.pio/build/licht/ (pio run) bzw. aus den CI-Artefakten. Der Dienst gibt dafür kurz
den USB-Port frei. otadata wird gelöscht: Hatte ein früheres OTA-Update auf die zweite App-Partition umgeschaltet,
startet der ESP sonst weiter die alte Firmware. NVS (gespeicherter Lichtzustand) bleibt erhalten.
"""

import argparse
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import esptool

from .konfig import laden

APP_ADRESSE = "0x10000"
OTADATA_ADRESSE, OTADATA_GROESSE = "0xe000", 0x2000


def dienst(aktion: str) -> None:
    subprocess.run(["systemctl", aktion, "spukpi"], check=False)


def main() -> int:
    p = argparse.ArgumentParser(prog="python -m spukpi.flash", description=__doc__.split("\n\n")[0])
    p.add_argument("firmware", type=Path, help="firmware.bin des Licht-ESP")
    p.add_argument("--port", help="USB-Port (Standard aus config.toml)")
    a = p.parse_args()
    if not a.firmware.is_file() or a.firmware.stat().st_size < 100_000:
        print(f"{a.firmware}: keine Firmware-Datei", file=sys.stderr)
        return 2
    port = a.port or laden().usb_port
    lief = subprocess.run(["systemctl", "is-active", "--quiet", "spukpi"]).returncode == 0
    if lief:
        dienst("stop")
        time.sleep(1)
    try:
        # Ein einziger Aufruf (Reset in den Bootloader, schreiben, Reset in die App). otadata als „gelöscht“ (0xFF)
        # mitschreiben – zwei getrennte Aufrufe ließen den ESP nach dem Reset im Bootloader hängen.
        with tempfile.NamedTemporaryFile(suffix=".bin") as leer:
            leer.write(b"\xff" * OTADATA_GROESSE)
            leer.flush()
            esptool.main(["--chip", "esp32", "--port", port, "--baud", "460800", "--before", "default_reset",
                          "--after", "hard_reset", "write_flash", "-z",
                          OTADATA_ADRESSE, leer.name, APP_ADRESSE, str(a.firmware)])
        print("Licht-ESP geflasht.")
        return 0
    except Exception as e:
        print(f"Flashen fehlgeschlagen: {e}", file=sys.stderr)
        return 1
    finally:
        if lief:
            dienst("start")


if __name__ == "__main__":
    sys.exit(main())
