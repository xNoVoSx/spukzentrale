// Spukzentrale – Licht-ESP (#1)
//
// Aufgaben:
//  - 5 (max. 6) COB-Zonen per PWM über die MOSFET-Module ansteuern
//  - Effekte lokal berechnen (100 Bilder/s), Befehle kommen vom Server
//  - Lautstärke-Hüllkurve vom Sound-ESP per UART empfangen (Licht reagiert auf Ton)
//  - letzten Zustand im Flash merken -> läuft auch ohne Server weiter
//  - Updates über WLAN (ArduinoOTA)
//  - ab 1.3: hängt der ESP per USB am Raspberry Pi, läuft alles über das Kabel (USB-Modus): der Pi reicht die
//    Server-Nachrichten als JSON-Zeilen durch und schickt Hüllkurve, Spektrum und Aktionen. Das WLAN ist dann aus.
//    Schweigt der Pi 5 s lang, schaltet der ESP zurück auf WLAN (wie bis 1.2).
//
// Zwei Abläufe: die Render-Task (100 Hz, Kern 1) liest den UART, rechnet die Effekte und schreibt PWM – sie hängt
// nie am Netz. loop() macht WLAN, WebSocket, OTA und Speichern; hängt das WLAN, läuft das Licht trotzdem weiter.
// Die Effekt-Engine teilen sich beide unter einem Mutex (nur für Mikrosekunden gesperrt, nie während Netzwerk-I/O).
#include <Arduino.h>
#include <ArduinoJson.h>
#include <ArduinoOTA.h>
#include <Preferences.h>
#include <WebSocketsClient.h>
#include <WiFi.h>
#include <esp_mac.h>

#if __has_include("config.h")
#include "config.h"
#else
#warning "include/config.h fehlt - es wird config.example.h verwendet. Bitte kopieren und anpassen!"
#include "config.example.h"
#endif

#include "diagnose.h"
#include "effekte.h"
#include "protokoll.h"

#ifndef FW_VERSION
#define FW_VERSION "1.3.0"
#endif

using namespace spuk;

static const uint8_t PINS[] = ZONEN_PINS;
static constexpr int ANZ_ZONEN = sizeof(PINS) / sizeof(PINS[0]);
static_assert(ANZ_ZONEN >= 1 && ANZ_ZONEN <= MAX_ZONEN, "1 bis 6 Zonen-Pins angeben");

static constexpr uint8_t PWM_BITS = 12;
static constexpr uint32_t PWM_MAX = (1u << PWM_BITS) - 1;

static Lichtmaschine maschine(ANZ_ZONEN);
static WebSocketsClient ws;
static Preferences prefs;
static RahmenLeser uartLeser;
static HardwareSerial& SoundUart = Serial2;

static Diagnose diagnose;
static RahmenLeser usbLeser;
static QueueHandle_t usbZeilen = nullptr;     // JSON-Zeilen vom Pi (char*, mit malloc), Render-Task -> loop()
static volatile uint32_t letzteUsbMs = 0;      // letztes gültiges Zeichen vom Pi
static bool usbModus = false;
static SemaphoreHandle_t maschineSperre = nullptr;  // schützt maschine und gammaWert
static TaskHandle_t renderTask = nullptr;

static float gammaWert = 2.2f;
static bool wsVerbunden = false;
static uint32_t speichernAb = 0;  // 0 = nichts zu speichern
static String zuSpeichern;
static bool vorschau = false;  // Server will die aktuellen Zonenwerte sehen (Live-Vorschau)

// von der Render-Task geschrieben, von loop() gelesen
static volatile uint32_t letzteUartMs = 0;
static volatile uint8_t letzteFlags = 0;
static volatile float fps = 0;
static volatile uint32_t renderMaxUs = 0;
static volatile float letzteWerte[MAX_ZONEN] = {0};
static volatile float musikNorm = 0, fxNorm = 0;

struct Sperre {  // RAII: Effekt-Engine für die Dauer eines Blocks sperren
  Sperre() { xSemaphoreTake(maschineSperre, portMAX_DELAY); }
  ~Sperre() { xSemaphoreGive(maschineSperre); }
};

// ---------------------------------------------------------------------------
// Zustand anwenden (vom Server oder aus dem Flash)
// ---------------------------------------------------------------------------
static void wendeZustandAn(JsonObjectConst z) {
  GlobalParam& g = maschine.global();
  if (z["an"].is<bool>()) g.an = z["an"].as<bool>();
  if (z["hell"].is<int>()) g.hell = constrain(z["hell"].as<int>(), 0, 100);
  if (z["musik_reaktiv"].is<int>()) g.musikReaktiv = constrain(z["musik_reaktiv"].as<int>(), 0, 100);

  JsonArrayConst zonen = z["zonen"].as<JsonArrayConst>();
  int i = 0;
  for (JsonObjectConst zo : zonen) {
    if (i >= ANZ_ZONEN) break;
    ZonenParam p = maschine.zone(i);
    if (zo["e"].is<const char*>()) p.effekt = effektVonName(zo["e"].as<const char*>());
    if (zo["tempo"].is<int>()) p.tempo = constrain(zo["tempo"].as<int>(), 0, 100);
    if (zo["staerke"].is<int>()) p.staerke = constrain(zo["staerke"].as<int>(), 0, 100);
    if (zo["basis"].is<int>()) p.basis = constrain(zo["basis"].as<int>(), 0, 100);
    maschine.setzeZone(i, p);
    i++;
  }
}

static void wendeConfigAn(JsonObjectConst c) {
  GlobalParam& g = maschine.global();
  if (c["strobo_max_hz"].is<float>()) g.stroboMaxHz = constrain(c["strobo_max_hz"].as<float>(), 0.5f, 15.0f);
  if (c["gamma"].is<float>()) gammaWert = constrain(c["gamma"].as<float>(), 1.0f, 3.0f);
  JsonArrayConst mx = c["zone_max"].as<JsonArrayConst>();
  int i = 0;
  for (JsonVariantConst v : mx) {
    if (i >= ANZ_ZONEN) break;
    g.zoneMax[i++] = constrain(v.as<int>(), 0, 100);
  }
}

static void zustandAnwenden(JsonObjectConst z) {
  Sperre s;
  wendeZustandAn(z);
}

static void configAnwenden(JsonObjectConst c) {
  Sperre s;
  wendeConfigAn(c);
}

static void merkeZumSpeichern(const char* json, size_t len) {
  zuSpeichern = String(json, len);
  speichernAb = millis() + 5000;  // Flash schonen: erst nach 5 s Ruhe
}

static void ladeGespeichertenZustand() {
  prefs.begin("spuk", true);
  String z = prefs.getString("zustand", "");
  String c = prefs.getString("config", "");
  prefs.end();
  JsonDocument doc;
  if (c.length() && !deserializeJson(doc, c)) configAnwenden(doc.as<JsonObjectConst>());
  if (z.length() && !deserializeJson(doc, z)) {
    zustandAnwenden(doc.as<JsonObjectConst>());
    Serial.println(F("[licht] gespeicherter Zustand geladen"));
  } else {
    // Werkseinstellung: alle Zonen flackern ruhig
    Sperre s;
    for (int i = 0; i < ANZ_ZONEN; i++) {
      ZonenParam p;
      p.effekt = E_FLACKERN;
      maschine.setzeZone(i, p);
    }
  }
}

// ---------------------------------------------------------------------------
// Server-Nachrichten
// ---------------------------------------------------------------------------
// an den Server: im USB-Modus als JSON-Zeile an den Pi (in einem Stück, damit kein Log dazwischenrutscht)
static void sende(JsonDocument& d) {
  String s;
  serializeJson(d, s);
  if (usbModus) {
    String zeile;
    zeile.reserve(s.length() + 2);
    zeile += (char)JSON_START;
    zeile += s;
    zeile += '\n';
    Serial.write((const uint8_t*)zeile.c_str(), zeile.length());
  } else {
    ws.sendTXT(s);
  }
}

static void sendeHallo() {
  JsonDocument d;
  d["t"] = "hallo";
  d["rolle"] = "licht";
  d["fw"] = FW_VERSION;
  d["zonen"] = ANZ_ZONEN;
  // im USB-Modus ist das WLAN aus – dann liefert WiFi.macAddress() Nullen; die MAC steht aber im eFuse
  uint8_t mac[6];
  esp_read_mac(mac, ESP_MAC_WIFI_STA);
  char macText[18];
  snprintf(macText, sizeof(macText), "%02X:%02X:%02X:%02X:%02X:%02X", mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]);
  d["mac"] = macText;
  d["ip"] = usbModus ? String("USB") : WiFi.localIP().toString();
  d["verbindung"] = usbModus ? "usb" : "wlan";
  JsonArray effekte = d["effekte"].to<JsonArray>();
  for (uint8_t e = 0; e < E_ANZAHL; e++) effekte.add(effektName(e));
  JsonArray aktionen = d["aktionen"].to<JsonArray>();
  for (uint8_t a = 1; a < A_ANZAHL; a++) aktionen.add(aktionName(a));
  diagnose.hallo(d);
  sende(d);
}

static void sendeStatus() {
  JsonDocument d;
  d["t"] = "status";
  d["verbindung"] = usbModus ? "usb" : "wlan";
  if (!usbModus) d["rssi"] = WiFi.RSSI();
  d["uptime"] = millis() / 1000;
  d["heap"] = ESP.getFreeHeap();
  d["fps"] = (int)fps;
  // „Kabel ok“: im USB-Modus die Leitung zum Pi, sonst der UART zum Sound-ESP
  d["uart"] = usbModus ? (millis() - letzteUsbMs) < 1500 : (millis() - letzteUartMs) < 1500;
  d["sound_musik"] = (letzteFlags & FLAG_MUSIK_AKTIV) != 0;
  d["musik"] = (int)(musikNorm * 100);
  d["fx"] = (int)(fxNorm * 100);
  d["render_max_us"] = renderMaxUs;
  renderMaxUs = 0;
  diagnose.status(d);
  sende(d);
}

// Aktuelle Ausgabe (nach Master, vor Gamma) in Prozent – für die Live-Vorschau
static void sendeWerte() {
  JsonDocument d;
  d["t"] = "werte";
  JsonArray v = d["v"].to<JsonArray>();
  for (int i = 0; i < ANZ_ZONEN; i++) v.add((int)(letzteWerte[i] * 100 + 0.5f));
  sende(d);
}

static void verarbeite(uint8_t* payload, size_t len) {
  JsonDocument doc;
  if (deserializeJson(doc, payload, len)) return;
  const char* t = doc["t"] | "";

  if (strcmp(t, "zustand") == 0) {
    zustandAnwenden(doc.as<JsonObjectConst>());
    // "temp": kurzzeitige Stimmung aus einer Szene -> nicht als Notfall-Zustand merken
    if (!(doc["temp"] | false)) merkeZumSpeichern((const char*)payload, len);
  } else if (strcmp(t, "config") == 0) {
    configAnwenden(doc.as<JsonObjectConst>());
    prefs.begin("spuk", false);
    prefs.putString("config", String((const char*)payload, len));
    prefs.end();
  } else if (strcmp(t, "aktion") == 0) {
    uint8_t typ = aktionVonName(doc["name"] | "keine");
    uint32_t maske = 0;
    JsonArrayConst zonen = doc["zonen"].as<JsonArrayConst>();
    for (JsonVariantConst v : zonen) {
      int z = v.as<int>();
      if (z >= 0 && z < ANZ_ZONEN) maske |= 1u << z;
    }
    Sperre s;
    maschine.aktion(typ, millis(), doc["verz_ms"] | 0, doc["dauer_ms"] | 800, maske,
                    doc["staerke"] | 100, doc["param"] | 0);
  } else if (strcmp(t, "stopp") == 0) {
    Sperre s;
    maschine.aktionenStoppen();
  } else if (strcmp(t, "vorschau") == 0) {
    vorschau = doc["an"] | false;
  } else if (strcmp(t, "neustart") == 0) {
    delay(200);
    ESP.restart();
  } else if (strcmp(t, "pi_hallo") == 0) {
    // der Pi hat (neu) mit dem Server verbunden und will unser Hallo weiterreichen
    sendeHallo();
  }
}

static void wsEreignis(WStype_t typ, uint8_t* payload, size_t len) {
  switch (typ) {
    case WStype_CONNECTED:
      wsVerbunden = true;
      Serial.println(F("[licht] mit Server verbunden"));
      sendeHallo();
      break;
    case WStype_DISCONNECTED:
      if (wsVerbunden) Serial.println(F("[licht] Server-Verbindung getrennt"));
      wsVerbunden = false;
      vorschau = false;
      break;
    case WStype_TEXT:
      verarbeite(payload, len);
      break;
    default:
      break;
  }
}

// ---------------------------------------------------------------------------
// UART vom Sound-ESP (Render-Task, Sperre gehalten)
// ---------------------------------------------------------------------------
static void leseUart() {
  while (SoundUart.available()) {
    if (uartLeser.feed((uint8_t)SoundUart.read())) {
      letzteUartMs = millis();
      if (uartLeser.typ == MSG_HUELLE && uartLeser.len == sizeof(Huelle)) {
        Huelle h;
        memcpy(&h, uartLeser.payload, sizeof(h));
        letzteFlags = h.flags;
        maschine.huelle(h.musik, h.fx, millis());
      } else if (uartLeser.typ == MSG_AKTION && uartLeser.len == sizeof(LichtAktion)) {
        // vom Sound-ESP zeitgleich mit einem Effekt-Sound ausgelöst
        LichtAktion a;
        memcpy(&a, uartLeser.payload, sizeof(a));
        uint32_t maske = a.maske & ((1u << ANZ_ZONEN) - 1);
        maschine.aktion(a.typ, millis(), a.verzMs, a.dauerMs, maske, a.staerke, a.param);
      }
    }
  }
}

// ---------------------------------------------------------------------------
// USB vom Pi (Render-Task, Sperre gehalten): Rahmen sofort auswerten, JSON-Zeilen an loop() weiterreichen
// ---------------------------------------------------------------------------
static void leseUsb() {
  static char zeile[3072];
  static size_t pos = 0;
  static bool inJson = false;
  static bool zuLang = false;
  while (Serial.available()) {
    uint8_t b = (uint8_t)Serial.read();
    if (inJson) {
      if (b == '\n') {
        inJson = false;
        if (zuLang || !pos) continue;
        zeile[pos] = 0;
        char* kopie = (char*)malloc(pos + 1);
        if (!kopie) continue;
        memcpy(kopie, zeile, pos + 1);
        if (xQueueSend(usbZeilen, &kopie, 0) != pdTRUE) free(kopie);
        letzteUsbMs = millis();
      } else if (pos < sizeof(zeile) - 1) {
        zeile[pos++] = (char)b;
      } else {
        zuLang = true;
      }
      continue;
    }
    if (b == JSON_START && usbLeser.leer()) {
      inJson = true;
      zuLang = false;
      pos = 0;
      continue;
    }
    if (!usbLeser.feed(b)) continue;
    letzteUsbMs = millis();
    if (usbLeser.typ == MSG_HUELLE && usbLeser.len == sizeof(Huelle)) {
      Huelle h;
      memcpy(&h, usbLeser.payload, sizeof(h));
      letzteFlags = h.flags;
      letzteUartMs = millis();
      maschine.huelle(h.musik, h.fx, millis());
    } else if (usbLeser.typ == MSG_AKTION && usbLeser.len == sizeof(LichtAktion)) {
      LichtAktion a;
      memcpy(&a, usbLeser.payload, sizeof(a));
      uint32_t maske = a.maske & ((1u << ANZ_ZONEN) - 1);
      maschine.aktion(a.typ, millis(), a.verzMs, a.dauerMs, maske, a.staerke, a.param);
    } else if (usbLeser.typ == MSG_SPEKTRUM && usbLeser.len == sizeof(Spektrum)) {
      Spektrum sp;
      memcpy(&sp, usbLeser.payload, sizeof(sp));
      maschine.spektrum(sp.bass, sp.mitte, sp.hoehen, sp.beat, millis());
    }
  }
}

// USB-Modus ein/aus: der Pi spricht mit uns -> WLAN aus; schweigt er 5 s -> zurück ins WLAN
static void usbModusPruefen() {
  bool piDa = letzteUsbMs && millis() - letzteUsbMs < 5000;
  if (piDa && !usbModus) {
    usbModus = true;
    Serial.println(F("[licht] Pi per USB erkannt – USB-Modus, WLAN aus"));
    WiFi.disconnect(true);
    WiFi.mode(WIFI_OFF);
    wsVerbunden = false;
    sendeHallo();
  } else if (!piDa && usbModus) {
    usbModus = false;
    vorschau = false;
    Serial.println(F("[licht] Pi schweigt – zurück ins WLAN"));
    WiFi.mode(WIFI_STA);
    WiFi.setSleep(false);
    WiFi.begin(WLAN_SSID, WLAN_PASS);
  }
}

// ---------------------------------------------------------------------------
// PWM-Ausgabe
// ---------------------------------------------------------------------------
static void schreibePwm(const float* werte, float gamma) {
  for (int i = 0; i < ANZ_ZONEN; i++) {
    float v = powf(werte[i], gamma);
    uint32_t duty = (uint32_t)(v * PWM_MAX + 0.5f);
    if (duty > PWM_MAX) duty = PWM_MAX;
#if PWM_INVERTIERT
    duty = PWM_MAX - duty;
#endif
    ledcWrite(PINS[i], duty);
  }
}

// ---------------------------------------------------------------------------
// Render-Task: 100 Bilder/s, unabhängig vom Netz
// ---------------------------------------------------------------------------
static void renderSchleife(void*) {
  TickType_t takt = xTaskGetTickCount();
  uint32_t bilder = 0, bilderZeit = millis(), naechsterHerzschlag = 0;
  for (;;) {
    vTaskDelayUntil(&takt, pdMS_TO_TICKS(10));
    uint32_t t0 = micros();
    float werte[MAX_ZONEN];
    float g;
    {
      Sperre s;
      leseUart();
      leseUsb();
      maschine.berechne(millis(), werte);
      g = gammaWert;
      musikNorm = maschine.musikNorm();
      fxNorm = maschine.fxNorm();
    }
    schreibePwm(werte, g);
    for (int i = 0; i < ANZ_ZONEN; i++) letzteWerte[i] = werte[i];
    bilder++;

    uint32_t jetzt = millis();
    if (jetzt - bilderZeit >= 1000) {
      fps = bilder * 1000.0f / (jetzt - bilderZeit);
      bilder = 0;
      bilderZeit = jetzt;
    }
    // Lebenszeichen an den Sound-ESP
    if ((int32_t)(jetzt - naechsterHerzschlag) >= 0) {
      naechsterHerzschlag = jetzt + 1000;
      uint8_t buf[8];
      size_t n = baueRahmen(MSG_HERZSCHLAG, nullptr, 0, buf);
      SoundUart.write(buf, n);
    }
    uint32_t d = micros() - t0;
    if (d > renderMaxUs) renderMaxUs = d;
  }
}

// ---------------------------------------------------------------------------
void setup() {
  Serial.setRxBufferSize(4096);  // JSON-Zeilen vom Pi (bis ~3 KB) kommen schneller, als die Render-Task (10 ms) liest
  // Sendepuffer: ohne ihn blockiert write(), bis eine lange Zeile bei 115200 Baud draußen ist (~20–35 ms), und die
  // Render-Task wartet so lange beim Lesen auf dieselbe UART-Sperre -> Ruckler im Licht
  Serial.setTxBufferSize(4096);
  Serial.begin(USB_BAUD);
  delay(100);
  Serial.printf("\n[licht] Spukzentrale Licht-ESP v%s, %d Zonen\n", FW_VERSION, ANZ_ZONEN);

  for (int i = 0; i < ANZ_ZONEN; i++) {
    if (!ledcAttach(PINS[i], PWM_FREQUENZ, PWM_BITS)) Serial.printf("[licht] PWM an GPIO%d fehlgeschlagen\n", PINS[i]);
    ledcWrite(PINS[i], PWM_INVERTIERT ? PWM_MAX : 0);
  }

  SoundUart.begin(UART_BAUD, SERIAL_8N1, UART_RX_PIN, UART_TX_PIN);

  maschineSperre = xSemaphoreCreateMutex();
  usbZeilen = xQueueCreate(8, sizeof(char*));
  ladeGespeichertenZustand();
  Serial.printf("[licht] Start: %s\n", neustartGrund());
  xTaskCreatePinnedToCore(renderSchleife, "licht", 4096, nullptr, 5, &renderTask, 1);

  diagnose.start();
  WiFi.setHostname(GERAETENAME);  // vor WiFi.mode(), sonst meldet sich der ESP als „esp32-…“
  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);  // kein Stromsparen: sonst verzögerte/verlorene Pakete bei schwachem Empfang
  WiFi.setAutoReconnect(true);
  WiFi.begin(WLAN_SSID, WLAN_PASS);

  ArduinoOTA.setHostname(GERAETENAME);
  ArduinoOTA.setPassword(OTA_PASSWORT);
  ArduinoOTA.setMdnsEnabled(false);  // wir sprechen die ESPs per IP an; mDNS verarbeitet sonst jeden Multicast im Netz
  ArduinoOTA.onStart([]() {
    // während des Updates alles dunkel
    vTaskSuspend(renderTask);
    for (int i = 0; i < ANZ_ZONEN; i++) ledcWrite(PINS[i], PWM_INVERTIERT ? PWM_MAX : 0);
  });
  ArduinoOTA.onError([](ota_error_t) { vTaskResume(renderTask); });
  ArduinoOTA.begin();

  String pfad = String("/ws/geraet?rolle=licht&fw=") + FW_VERSION;
  ws.begin(SERVER_HOST, SERVER_PORT, pfad);
  ws.onEvent(wsEreignis);
  ws.setReconnectInterval(3000);
  ws.enableHeartbeat(15000, 3000, 2);
}

void loop() {
  static uint32_t naechsterStatus = 0, naechsteWerte = 0;
  static bool otaBereit = false;
  diagnose.schleifeStart();

  // JSON-Zeilen vom Pi = Server-Nachrichten
  char* zeile;
  while (xQueueReceive(usbZeilen, &zeile, 0) == pdTRUE) {
    usbModusPruefen();
    verarbeite((uint8_t*)zeile, strlen(zeile));
    free(zeile);
  }
  usbModusPruefen();

  if (!usbModus && WiFi.status() == WL_CONNECTED) {
    if (!otaBereit) {
      Serial.printf("[licht] WLAN ok, IP %s\n", WiFi.localIP().toString().c_str());
      otaBereit = true;
    }
    ArduinoOTA.handle();
    ws.loop();
  } else {
    otaBereit = false;
  }

  uint32_t jetzt = millis();
  bool verbunden = usbModus || wsVerbunden;
  if (verbunden && vorschau && (int32_t)(jetzt - naechsteWerte) >= 0) {
    naechsteWerte = jetzt + 100;  // 10 Hz reichen für die Vorschau
    sendeWerte();
  }

  if (verbunden && (int32_t)(jetzt - naechsterStatus) >= 0) {
    naechsterStatus = jetzt + 2000;
    sendeStatus();
  }

  if (speichernAb && (int32_t)(jetzt - speichernAb) >= 0) {
    prefs.begin("spuk", false);
    prefs.putString("zustand", zuSpeichern);
    prefs.end();
    speichernAb = 0;
    zuSpeichern = String();
  }

  diagnose.schleifeEnde();
  delay(2);
}
