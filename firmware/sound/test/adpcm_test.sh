#!/usr/bin/env bash
# Prüft den ADPCM-Dekoder der Sound-Firmware:
#  1. bit-genau gegen eine unabhängige Referenz nach der IMA-ADPCM-Spezifikation (Python)
#  2. gegen ffmpeg: Abweichung mind. 40 dB unter dem Signal (ältere Versionen runden bei IMA anders)
# Braucht g++, ffmpeg und python3. Aufruf: bash firmware/sound/test/adpcm_test.sh
set -euo pipefail
hier="$(cd "$(dirname "$0")" && pwd)"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

g++ -std=c++17 -O2 -Wall -Wextra -o "$tmp/vergleich" "$hier/adpcm_vergleich.cpp"

# Gleiche Einstellungen wie der Server (server/app/audio.py): 22,05 kHz mono, Block 512
ffmpeg -hide_banner -loglevel error -f lavfi \
  -i "anoisesrc=d=2.7:c=pink:a=0.6,aformat=sample_rates=22050" \
  -f lavfi -i "sine=f=330:d=2.7" -filter_complex "amix=inputs=2,volume=1.5" \
  -ac 1 -ar 22050 -c:a adpcm_ima_wav -block_size 512 "$tmp/fx.wav"
ffmpeg -hide_banner -loglevel error -i "$tmp/fx.wav" -f s16le -acodec pcm_s16le "$tmp/ffmpeg.raw"

python3 - "$tmp/fx.wav" "$tmp/spez.raw" <<'PY'
# Referenz-Dekoder nach "IMA ADPCM" (Microsoft WAVE, Format 0x11), bewusst unabhängig geschrieben
import struct, sys
SCHRITTE = [7, 8, 9, 10, 11, 12, 13, 14, 16, 17, 19, 21, 23, 25, 28, 31, 34, 37, 41, 45, 50, 55, 60, 66, 73, 80,
            88, 97, 107, 118, 130, 143, 157, 173, 190, 209, 230, 253, 279, 307, 337, 371, 408, 449, 494, 544, 598,
            658, 724, 796, 876, 963, 1060, 1166, 1282, 1411, 1552, 1707, 1878, 2066, 2272, 2499, 2749, 3024, 3327,
            3660, 4026, 4428, 4871, 5358, 5894, 6484, 7132, 7845, 8630, 9493, 10442, 11487, 12635, 13899, 15289,
            16818, 18500, 20350, 22385, 24623, 27086, 29794, 32767]
INDEX = [-1, -1, -1, -1, 2, 4, 6, 8]
d = open(sys.argv[1], "rb").read()
pos, chunks = 12, {}
while pos + 8 <= len(d):
    kennung, laenge = d[pos:pos + 4], struct.unpack("<I", d[pos + 4:pos + 8])[0]
    chunks[kennung] = d[pos + 8:pos + 8 + laenge]
    pos += 8 + laenge + (laenge & 1)
block = struct.unpack("<H", chunks[b"fmt "][12:14])[0]
anzahl = struct.unpack("<I", chunks[b"fact"][:4])[0]
aus = []
daten = chunks[b"data"]
for b in range(0, len(daten) - block + 1, block):
    bl = daten[b:b + block]
    wert, index = struct.unpack("<h", bl[:2])[0], bl[2]
    aus.append(wert)
    for byte in bl[4:]:
        for nib in (byte & 15, byte >> 4):
            s = SCHRITTE[index]
            diff = s >> 3
            if nib & 4: diff += s
            if nib & 2: diff += s >> 1
            if nib & 1: diff += s >> 2
            wert = max(-32768, min(32767, wert - diff if nib & 8 else wert + diff))
            index = max(0, min(88, index + INDEX[nib & 7]))
            aus.append(wert)
open(sys.argv[2], "wb").write(struct.pack(f"<{len(aus)}h", *aus))
PY

"$tmp/vergleich" "$tmp/fx.wav" "$tmp/spez.raw"
"$tmp/vergleich" "$tmp/fx.wav" "$tmp/ffmpeg.raw" 40
