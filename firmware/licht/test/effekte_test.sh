#!/usr/bin/env bash
# Testet die Effekt-Engine des Licht-ESP am PC. Braucht g++.
set -euo pipefail
hier="$(cd "$(dirname "$0")" && pwd)"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
g++ -std=c++17 -O2 -Wall -Wextra -I"$hier/../../common" -o "$tmp/test" "$hier/effekte_test.cpp" "$hier/../src/effekte.cpp"
"$tmp/test"
