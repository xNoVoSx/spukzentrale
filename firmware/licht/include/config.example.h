// Spukzentrale – Licht-ESP (#1) – Konfiguration
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
#define GERAETENAME "spuk-licht"   // Hostname im WLAN + Name für OTA
#define OTA_PASSWORT "spukota"      // Passwort für Updates über WLAN

// --- MOSFET-Module: ein Pin pro Zone (Zone 1, 2, 3 ...), max. 6 ---
#define ZONEN_PINS {18, 19, 21, 22, 23}

// 1 = Modul schaltet bei LOW ein (selten). Standard: 0
#define PWM_INVERTIERT 0

// PWM-Frequenz. Module mit Optokoppler (PC817) mögen niedrige Frequenzen.
#define PWM_FREQUENZ 1000

// --- UART-Verbindung zum Sound-ESP ---
#define UART_RX_PIN 16   // an TX (GPIO17) vom Sound-ESP
#define UART_TX_PIN 17   // an RX (GPIO16) vom Sound-ESP
