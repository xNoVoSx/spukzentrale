#!/usr/bin/env bash
# Abgleich der Licht-Engine: C++ (firmware/licht/src/effekte.cpp) gegen den JavaScript-Port
# (server/static/lichtmaschine.js). Braucht g++ und node. Exit 0 = gleich (Abweichung <= Grenze).
# Optional: bash vergleich_js.sh <saat> <mal>  (anderes Drehbuch bzw. gemischter Teil mal-fach so lang)
set -euo pipefail
hier="$(cd "$(dirname "$0")" && pwd)"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
g++ -std=c++17 -O2 -Wall -Wextra -I"$hier/../../common" -o "$tmp/vergleich" "$hier/vergleich_js.cpp" "$hier/../src/effekte.cpp"
node "$hier/vergleich_js.mjs" erzeuge "$tmp/drehbuch.txt" "${1:-12648430}" "${2:-1}"
"$tmp/vergleich" "$tmp/drehbuch.txt" > "$tmp/cpp.txt"
node "$hier/vergleich_js.mjs" pruefe "$tmp/drehbuch.txt" "$tmp/cpp.txt"
