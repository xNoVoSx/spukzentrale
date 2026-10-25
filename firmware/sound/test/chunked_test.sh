#!/usr/bin/env bash
# Prüft den HTTP-Chunk-Dekoder der Sound-Firmware. Braucht g++, ffmpeg und python3.
set -euo pipefail
hier="$(cd "$(dirname "$0")" && pwd)"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

g++ -std=c++17 -O2 -Wall -Wextra -o "$tmp/vergleich" "$hier/chunked_vergleich.cpp"
ffmpeg -hide_banner -loglevel error -f lavfi -i "sine=f=220:d=5" -c:a libmp3lame -b:a 128k "$tmp/original.mp3"

# wie ein HTTP-Server verpacken: zufällige Chunk-Größen, Groß-/Kleinschreibung, eine Erweiterung
python3 - "$tmp/original.mp3" "$tmp/chunked.bin" <<'PY'
import random, sys
daten = open(sys.argv[1], "rb").read()
random.seed(7)
aus, pos, n = bytearray(), 0, 0
while pos < len(daten):
    groesse = random.choice([1, 2, 17, 255, 4096, 8192, random.randint(1, 20000)])
    stueck = daten[pos:pos + groesse]
    kopf = f"{len(stueck):x}" if n % 2 else f"{len(stueck):X}"
    if n == 3:
        kopf += ";name=wert"
    aus += kopf.encode() + b"\r\n" + stueck + b"\r\n"
    pos += len(stueck)
    n += 1
aus += b"0\r\n\r\n"
open(sys.argv[2], "wb").write(aus)
PY
"$tmp/vergleich" "$tmp/chunked.bin" "$tmp/original.mp3"
