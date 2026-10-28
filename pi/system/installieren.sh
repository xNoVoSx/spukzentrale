#!/usr/bin/env bash
# spukpi auf einem frischen Raspberry Pi OS Lite (64 bit, Trixie) einrichten bzw. aktualisieren.
#   bash pi/system/installieren.sh        (im Repo, auf dem Pi)
# Idempotent: lässt eine vorhandene /etc/spukpi/config.toml in Ruhe.
set -euo pipefail
hier="$(cd "$(dirname "$0")/.." && pwd)"
benutzer="${SUDO_USER:-$(id -un)}"

echo "== Pakete"
sudo apt-get update -qq
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
  python3-venv python3-numpy python3-alsaaudio python3-serial python3-websockets ffmpeg alsa-utils >/dev/null

echo "== Programm nach /opt/spukpi"
sudo mkdir -p /opt/spukpi/src
sudo rsync -a --delete "$hier/spukpi/" /opt/spukpi/src/spukpi/
if [ ! -x /opt/spukpi/venv/bin/python ]; then
  sudo python3 -m venv --system-site-packages /opt/spukpi/venv
fi
# esptool von PyPI: Debians Paket hat keinen Flasher-Stub
sudo /opt/spukpi/venv/bin/pip install -q esptool==4.8.1

echo "== Konfiguration"
sudo mkdir -p /etc/spukpi
if [ ! -f /etc/spukpi/config.toml ]; then
  sudo cp "$hier/system/config.example.toml" /etc/spukpi/config.toml
  echo "   /etc/spukpi/config.toml angelegt – server_host eintragen!"
fi

echo "== I2S für den PCM5102A"
cfg=/boot/firmware/config.txt
if ! grep -q '^dtoverlay=hifiberry-dac' "$cfg"; then
  printf '\n# Spukzentrale: PCM5102A an I2S (BCK GPIO18, LRCK GPIO19, DIN GPIO21)\ndtoverlay=hifiberry-dac\n' | sudo tee -a "$cfg" >/dev/null
  echo "   Overlay eingetragen – Neustart nötig"
fi

echo "== Dienst"
sed "s/^User=novos/User=$benutzer/" "$hier/system/spukpi.service" | sudo tee /etc/systemd/system/spukpi.service >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable spukpi >/dev/null
sudo systemctl restart spukpi
echo "fertig: journalctl -u spukpi -f"
