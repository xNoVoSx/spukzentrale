// Spukzentrale – Diagnose für beide ESPs: Neustart-Grund, WLAN-Trennungen, Hänger der Hauptschleife.
// Die Werte gehen mit "hallo" und "status" an den Server und stehen in der Oberfläche unter „Geräte“.
#pragma once
#include <Arduino.h>
#include <ArduinoJson.h>
#include <WiFi.h>
#include <esp_system.h>

namespace spuk {

inline const char* neustartGrund() {
  switch (esp_reset_reason()) {
    case ESP_RST_POWERON: return "Strom an";
    case ESP_RST_EXT: return "Reset-Taste";
    case ESP_RST_SW: return "Neustart";
    case ESP_RST_PANIC: return "Absturz";
    case ESP_RST_INT_WDT: return "Watchdog (Interrupt)";
    case ESP_RST_TASK_WDT: return "Watchdog (Task)";
    case ESP_RST_WDT: return "Watchdog";
    case ESP_RST_BROWNOUT: return "Unterspannung";
    case ESP_RST_DEEPSLEEP: return "Tiefschlaf";
    default: return "unbekannt";
  }
}

// ein normaler Start (Strom an, Taste, gewollter Neustart) – alles andere ist ein Fehler
inline bool neustartNormal() {
  esp_reset_reason_t r = esp_reset_reason();
  return r == ESP_RST_POWERON || r == ESP_RST_EXT || r == ESP_RST_SW;
}

class Diagnose {
 public:
  void start() {
    WiFi.onEvent([this](WiFiEvent_t, WiFiEventInfo_t info) {
      wlanTrennungen_ = wlanTrennungen_ + 1;
      letzterWlanGrund_ = info.wifi_sta_disconnected.reason;
    }, ARDUINO_EVENT_WIFI_STA_DISCONNECTED);
  }

  // am Anfang und Ende jedes loop()-Durchlaufs
  void schleifeStart() { schleifeT0_ = millis(); }
  void schleifeEnde() {
    uint32_t d = millis() - schleifeT0_;
    if (d > schleifeMax_) schleifeMax_ = d;
    if (d > 1000) haengerUeber1s_++;
  }

  void hallo(JsonDocument& d) const {
    d["neustart_grund"] = neustartGrund();
    d["neustart_normal"] = neustartNormal();
  }

  // Werte seit dem letzten Status (Maximum wird danach zurückgesetzt)
  void status(JsonDocument& d) {
    d["neustart_grund"] = neustartGrund();
    d["wlan_trennungen"] = wlanTrennungen_;
    d["wlan_grund"] = letzterWlanGrund_;
    d["schleife_max_ms"] = schleifeMax_;
    d["haenger_1s"] = haengerUeber1s_;
    schleifeMax_ = 0;
  }

 private:
  volatile uint32_t wlanTrennungen_ = 0;
  volatile uint8_t letzterWlanGrund_ = 0;
  uint32_t schleifeT0_ = 0, schleifeMax_ = 0, haengerUeber1s_ = 0;
};

}  // namespace spuk
