// Spukzentrale – Sound-ESP (#2) – Konfiguration
//
// Diese Datei nach "config.h" kopieren und anpassen.
// config.h wird nicht ins Git eingecheckt (WLAN-Passwort!).
#pragma once

// --- WLAN ---
#define WLAN_SSID "MeinWLAN"
#define WLAN_PASS "geheim"

// --- Server (Docker-Container "spukzentrale") ---
#define SERVER_HOST "192.168.1.10"   // IP der Docker-VM mit dem Server
#define SERVER_PORT 8095

// --- Gerät ---
#define GERAETENAME "spuk-sound"     // Hostname im WLAN
#define OTA_PASSWORT "spukota"        // Passwort für Updates über WLAN

// --- I2S-DAC PCM5102A (Ausgang an AUX der Box) ---
#define I2S_BCK_PIN 26
#define I2S_LCK_PIN 25
#define I2S_DIN_PIN 22

// --- UART-Verbindung zum Licht-ESP ---
#define UART_RX_PIN 16   // an TX (GPIO17) vom Licht-ESP
#define UART_TX_PIN 17   // an RX (GPIO16) vom Licht-ESP
