#!/usr/bin/env bash
# Testet die Ambiente-Ausgabe der Sound-Firmware am PC (Vorpuffern, Unterlauf ohne Knacken). Braucht g++.
set -euo pipefail
hier="$(cd "$(dirname "$0")" && pwd)"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
g++ -std=c++17 -O2 -Wall -Wextra -o "$tmp/test" "$hier/musik_ausgabe_test.cpp"
"$tmp/test"
