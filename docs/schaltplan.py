#!/usr/bin/env python3
"""Erzeugt docs/schaltplan.svg – den Verdrahtungsplan der Spukzentrale.

    python3 docs/schaltplan.py

Absichtlich ohne Abhängigkeiten: Das SVG wird direkt geschrieben. Pins und Zonenzahl
stehen oben, damit sich der Plan bei Änderungen leicht anpassen lässt.
"""

from pathlib import Path

ZONEN_PINS = [18, 19, 21, 22, 23]
TITEL = "Spukzentrale – Verdrahtung"

ROT, SCHWARZ, GRUEN, BLAU, ORANGE, LILA, GRAU = "#d62828", "#222222", "#2a9d3a", "#1f6fd1", "#e07a00", "#7b3fe4", "#777777"
W, MOD_X, MOD_W, MOD_H, ZEILE, Y0 = 1360, 470, 190, 110, 150, 190
BUS_P, BUS_M = 740, 770          # Sammelschienen +12 V / −
LED_X = 1010                     # Beginn der LED-Zonen
ESP_X, ESP_W = 40, 220
GND_SCHIENE = 340

teile: list[str] = []


def linie(punkte, farbe, breite=2.4, strich=None):
    p = " ".join(f"{x},{y}" for x, y in punkte)
    s = f' stroke-dasharray="{strich}"' if strich else ""
    teile.append(f'<polyline points="{p}" fill="none" stroke="{farbe}" stroke-width="{breite}" stroke-linejoin="round"{s}/>')


def punkt(x, y, farbe):
    teile.append(f'<circle cx="{x}" cy="{y}" r="4.2" fill="{farbe}"/>')


def sprung(x, y, farbe):
    """Kreuzung ohne Verbindung: kleiner Bogen über die senkrechte Leitung."""
    teile.append(f'<path d="M{x - 7},{y} a7,7 0 0 1 14,0" fill="white" stroke="white" stroke-width="5"/>')
    teile.append(f'<path d="M{x - 7},{y} a7,7 0 0 1 14,0" fill="none" stroke="{farbe}" stroke-width="2.4"/>')


def text(x, y, t, groesse=14, farbe=SCHWARZ, anker="start", fett=False, kursiv=False):
    f = ' font-weight="700"' if fett else ""
    k = ' font-style="italic"' if kursiv else ""
    t = t.replace("&", "&amp;").replace("<", "&lt;")
    teile.append(f'<text x="{x}" y="{y}" font-size="{groesse}" fill="{farbe}" text-anchor="{anker}"{f}{k}>{t}</text>')


def kasten(x, y, b, h, titel, fuellung="#f4f1f7", rand="#444", untertitel=""):
    teile.append(f'<rect x="{x}" y="{y}" width="{b}" height="{h}" rx="10" fill="{fuellung}" stroke="{rand}" stroke-width="2"/>')
    text(x + b / 2, y + 24, titel, 16, anker="middle", fett=True)
    if untertitel:
        text(x + b / 2, y + 42, untertitel, 12, GRAU, anker="middle")


def pin(x, y, name, rechts=True):
    teile.append(f'<circle cx="{x}" cy="{y}" r="5" fill="white" stroke="#444" stroke-width="1.6"/>')
    text(x - 10 if rechts else x + 10, y + 5, name, 12, anker="end" if rechts else "start")


def klemme(x, y, name):
    teile.append(f'<rect x="{x - 9}" y="{y - 9}" width="18" height="18" rx="3" fill="#ddd" stroke="#444" stroke-width="1.4"/>')
    teile.append(f'<circle cx="{x}" cy="{y}" r="4" fill="#888"/>')
    text(x - 14, y + 5, name, 12, anker="end", fett=True)


n = len(ZONEN_PINS)
H = Y0 + n * ZEILE + 520
teile.append(f'<rect width="{W}" height="{H}" fill="white"/>')
text(W / 2, 40, TITEL, 26, anker="middle", fett=True)
text(W / 2, 64, "Licht: 5 Zonen über LR7843-MOSFET-Module · Sound: I2S-DAC an AUX · beide ESP32 per USB versorgt", 14, GRAU, anker="middle")

# --- Netzteil und Sicherung -------------------------------------------------
NT_X, NT_Y = 640, 84
kasten(NT_X, NT_Y, 180, 62, "Netzteil 12 V", untertitel="200 W")
text(NT_X + 196, NT_Y + 30, "◄ 230 V ~", 14, ROT, fett=True)
y_bus_start = NT_Y + 62
linie([(BUS_P, y_bus_start), (BUS_P, y_bus_start + 10)], ROT)
teile.append(f'<rect x="{BUS_P - 9}" y="{y_bus_start + 10}" width="18" height="26" rx="3" fill="#ffe8a3" stroke="#8a6d00" stroke-width="1.6"/>')
text(BUS_P - 14, y_bus_start + 28, "Sicherung 15 A", 12, "#8a6d00", anker="end", fett=True)
y_letzte = Y0 + (n - 1) * ZEILE
linie([(BUS_P, y_bus_start + 36), (BUS_P, y_letzte + 25)], ROT, 3.2)
linie([(BUS_M, y_bus_start), (BUS_M, y_letzte + 85)], SCHWARZ, 3.2)
text(BUS_P - 14, y_bus_start + 44, "+12 V", 12, ROT, anker="end", fett=True)
text(BUS_M + 8, y_bus_start + 20, "− (Masse)", 12, SCHWARZ, fett=True)

# --- ESP#1 (Licht) ------------------------------------------------------------
esp1_y, esp1_h = Y0 - 30, n * ZEILE + 40
kasten(ESP_X, esp1_y, ESP_W, esp1_h, "ESP#1 LICHT", "#e8f1fd", BLAU, "ESP32-WROOM-32")
text(ESP_X + ESP_W / 2, esp1_y + 62, "Strom: USB-Netzteil", 12, GRAU, anker="middle", kursiv=True)
gnd_y = esp1_y + esp1_h - 46
pin(ESP_X + ESP_W, gnd_y, "GND")
linie([(ESP_X + ESP_W, gnd_y), (GND_SCHIENE, gnd_y), (GND_SCHIENE, Y0 + 75)], BLAU)

# --- MOSFET-Module und LED-Zonen ---------------------------------------------
for i, gpio in enumerate(ZONEN_PINS):
    y = Y0 + i * ZEILE
    # Modul
    teile.append(f'<rect x="{MOD_X}" y="{y}" width="{MOD_W}" height="{MOD_H}" rx="8" fill="#1d1d1d" stroke="#000"/>')
    text(MOD_X + MOD_W / 2, y + 62, f"MOSFET {i + 1}", 15, "white", anker="middle", fett=True)
    text(MOD_X + MOD_W / 2, y + 80, "LR7843", 11, "#bbb", anker="middle")
    for name, dy in (("PWM", 40), ("GND", 75)):
        teile.append(f'<circle cx="{MOD_X + 16}" cy="{y + dy}" r="6" fill="#d9b44a"/>')
        text(MOD_X + 28, y + dy + 4, name, 11, "white", fett=True)
    xk = MOD_X + MOD_W
    for name, dy in (("+", 25), ("LOAD", 55), ("−", 85)):
        teile.append(f'<rect x="{xk - 4}" y="{y + dy - 9}" width="18" height="18" rx="3" fill="#2a7fd4" stroke="#0d3f73"/>')
        text(xk - 8, y + dy + 4, name, 11, "white", anker="end", fett=True)

    # Steuerseite: GPIO -> PWM, GND-Schiene -> GND
    pin(ESP_X + ESP_W, y + 40, f"GPIO{gpio}")
    linie([(ESP_X + ESP_W, y + 40), (MOD_X + 10, y + 40)], GRUEN)
    if i > 0:
        sprung(GND_SCHIENE, y + 40, GRUEN)
    linie([(GND_SCHIENE, y + 75), (MOD_X + 10, y + 75)], BLAU)
    punkt(GND_SCHIENE, y + 75, BLAU)

    # Lastseite: + an +12 V, − an Masse, LOAD an LED −, LED + an +
    linie([(xk + 14, y + 25), (LED_X + 30, y + 25)], ROT)
    punkt(BUS_P, y + 25, ROT)
    linie([(xk + 14, y + 85), (BUS_M, y + 85)], SCHWARZ)
    punkt(BUS_M, y + 85, SCHWARZ)
    linie([(xk + 14, y + 55), (LED_X + 30, y + 55)], "#555")
    if i < n - 1:  # die +12-V-Schiene endet auf Höhe des letzten Moduls
        sprung(BUS_P, y + 55, "#555")
    sprung(BUS_M, y + 55, "#555")
    sprung(BUS_M, y + 25, ROT)

    # LED-Zone: 2 Streifen parallel
    zx = LED_X + 30
    teile.append(f'<rect x="{zx}" y="{y - 4}" width="300" height="{MOD_H + 8}" rx="10" fill="#fff7ec" stroke="{ORANGE}" stroke-dasharray="5 4"/>')
    text(zx + 150, y + 16, f"Zone {i + 1} · 2 Streifen parallel", 13, ORANGE, anker="middle", fett=True)
    for k, sy in enumerate((y + 40, y + 80)):
        linie([(zx + 10, y + 25), (zx + 10, sy - 5), (zx + 60, sy - 5)], ROT, 1.8)
        linie([(zx + 24, y + 55), (zx + 24, sy + 5), (zx + 60, sy + 5)], "#555", 1.8)
        teile.append(f'<rect x="{zx + 60}" y="{sy - 9}" width="226" height="18" rx="5" fill="#ffd9a8" stroke="{ORANGE}"/>')
        text(zx + 173, sy + 4, f"LED-Streifen {2 * i + k + 1}", 11, "#7a4300", anker="middle")
        text(zx + 52, sy - 8, "+", 11, ROT, anker="middle", fett=True)
        text(zx + 52, sy + 18, "−", 11, SCHWARZ, anker="middle", fett=True)
    punkt(zx + 10, y + 25, ROT)
    punkt(zx + 24, y + 55, "#555")

# --- ESP#2 (Sound), DAC, Box --------------------------------------------------
esp2_y = esp1_y + esp1_h + 90
esp2_h = 190
kasten(ESP_X, esp2_y, ESP_W, esp2_h, "", "#efe8fd", LILA)
text(ESP_X + ESP_W / 2, esp2_y + 50, "ESP#2 SOUND", 16, anker="middle", fett=True)
text(ESP_X + ESP_W / 2, esp2_y + 68, "ESP32-WROOM-32", 12, GRAU, anker="middle")
text(ESP_X + ESP_W / 2, esp2_y + esp2_h - 14, "Strom: USB-Netzteil", 12, GRAU, anker="middle", kursiv=True)
# --- UART zwischen den ESPs ---------------------------------------------------
for x_pin, oben, unten, farbe in ((90, "TX 17", "RX 16", ORANGE), (150, "RX 16", "TX 17", ORANGE), (210, "GND", "GND", BLAU)):
    teile.append(f'<circle cx="{x_pin}" cy="{esp1_y + esp1_h}" r="5" fill="white" stroke="#444" stroke-width="1.6"/>')
    teile.append(f'<circle cx="{x_pin}" cy="{esp2_y}" r="5" fill="white" stroke="#444" stroke-width="1.6"/>')
    text(x_pin, esp1_y + esp1_h - 10, oben, 11, anker="middle", fett=True)
    text(x_pin, esp2_y + 22, unten, 11, anker="middle", fett=True)
    linie([(x_pin, esp1_y + esp1_h + 5), (x_pin, esp2_y - 5)], farbe)
text(270, esp1_y + esp1_h + 50, "UART 115200 Baud: TX ↔ RX gekreuzt, GND gemeinsam (wichtig!)", 13, ORANGE, fett=True)

DAC_X, DAC_W = MOD_X, MOD_W
kasten(DAC_X, esp2_y, DAC_W, esp2_h, "PCM5102A", "#efe8fd", LILA, "I2S-DAC")
dac_pins = [("3V3", "VIN", ROT), ("GND", "GND", BLAU), ("GPIO26", "BCK", LILA), ("GPIO25", "LCK", LILA), ("GPIO22", "DIN", LILA)]
for k, (esp_pin, dac_pin, farbe) in enumerate(dac_pins):
    py = esp2_y + 82 + k * 20
    pin(ESP_X + ESP_W, py, esp_pin)
    pin(DAC_X, py, dac_pin, rechts=False)
    linie([(ESP_X + ESP_W + 5, py), (DAC_X - 5, py)], farbe, 2)
py = esp2_y + 82 + len(dac_pins) * 20
pin(DAC_X, py, "SCK", rechts=False)
linie([(DAC_X - 5, py), (DAC_X - 40, py), (DAC_X - 40, esp2_y + 102)], BLAU, 1.6)
punkt(DAC_X - 40, esp2_y + 102, BLAU)
text(DAC_X - 46, py + 4, "SCK an GND", 11, BLAU, anker="end")
# Klinke -> Box
kl_y = esp2_y + 95
teile.append(f'<rect x="{DAC_X + DAC_W - 8}" y="{kl_y - 12}" width="24" height="24" rx="12" fill="#333"/>')
text(DAC_X + DAC_W + 24, kl_y - 18, "3,5 mm Klinke", 12, GRAU)
linie([(DAC_X + DAC_W + 16, kl_y), (BUS_M + 140, kl_y)], SCHWARZ, 4)
kasten(BUS_M + 140, kl_y - 50, 220, 100, "Lautsprecher-Box", "#f4f1f7", "#444", "AUX-Eingang")

# --- Hinweise und Legende ------------------------------------------------------
hy = esp2_y + esp2_h + 50
hinweise = [
    "PCM5102A-Lötbrücken auf der Rückseite: FLT, DEMP, FMT auf L – XSMT auf H (sonst stumm).",
    "MOSFET-Modul: Netzteil + an „+“, Netzteil − an „−“, LED + an „+“, LED − an „LOAD“ (Modul schaltet die Minusseite).",
    "ESP-GND an GND jedes Moduls. Bei Modulen ohne Optokoppler ist GND intern mit „−“ verbunden – das ist so gewollt.",
    "Leitungen: 0,75 mm² reichen bei 2 m LED pro Zone (≈ 1,7 A). Den Montageplan mit Längen berechnet die Weboberfläche.",
    "Pins meiden: GPIO 0, 2, 12, 15 (Boot) und 34–39 (nur Eingang). Eine 6. Zone ginge z. B. an GPIO 25 des Licht-ESP.",
]
text(ESP_X, hy, "Hinweise", 16, fett=True)
for k, h in enumerate(hinweise):
    text(ESP_X, hy + 26 + k * 22, "• " + h, 13)
ly = hy + 26 + len(hinweise) * 22 + 20
for k, (farbe, name) in enumerate(((ROT, "+12 V"), (SCHWARZ, "Masse 12 V"), ("#555", "LED − (geschaltet)"), (GRUEN, "PWM"),
                                   (BLAU, "GND Steuerung"), (ORANGE, "UART"), (LILA, "I2S"))):
    x = ESP_X + k * 180
    linie([(x, ly), (x + 34, ly)], farbe, 4)
    text(x + 42, ly + 5, name, 12)

svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {ly + 30}" width="{W}" height="{ly + 30}" '
       f'font-family="DejaVu Sans, Arial, sans-serif">\n' + "\n".join(teile) + "\n</svg>\n")
ziel = Path(__file__).with_name("schaltplan.svg")
ziel.write_text(svg, encoding="utf-8")
print(f"geschrieben: {ziel}")
