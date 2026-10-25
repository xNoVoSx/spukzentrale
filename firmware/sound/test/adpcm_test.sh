#!/usr/bin/env bash
# Vergleicht den ADPCM-Dekoder der Sound-Firmware mit ffmpeg.
# Braucht g++ und ffmpeg. Aufruf: bash firmware/sound/test/adpcm_test.sh
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
ffmpeg -hide_banner -loglevel error -i "$tmp/fx.wav" -f s16le -acodec pcm_s16le "$tmp/ref.raw"

"$tmp/vergleich" "$tmp/fx.wav" "$tmp/ref.raw"
