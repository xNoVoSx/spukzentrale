// Spukzentrale – Sound-ESP (#2)
//
// Aufgaben:
//  - Ambiente-Endlos-Stream (MP3) vom Server abspielen
//  - Effekt-Sounds (Donner, Gelächter, ...) sofort aus dem Flash drüberlegen
//  - Ausgabe über I2S-DAC (PCM5102A) an den AUX-Eingang der Box
//  - Licht-ESP per UART mit Hüllkurve und Licht-Aktionen versorgen (synchron zum Ton)
//  - Effekt-Dateien mit dem Server abgleichen (fx_sync)
#include <Arduino.h>
#include <ArduinoJson.h>
#include <HTTPClient.h>
#include <LittleFS.h>
#include <WebSocketsClient.h>
#include <WiFi.h>

#if __has_include("config.h")
#include "config.h"
#else
#warning "include/config.h fehlt - es wird config.example.h verwendet. Bitte kopieren und anpassen!"
#include "config.example.h"
#endif

#include "audio.h"
#include "diagnose.h"
#include "protokoll.h"

#ifndef FW_VERSION
#define FW_VERSION "1.2.1"
#endif

using namespace spuk;

static WebSocketsClient ws;
static RahmenLeser uartLeser;
static HardwareSerial& LichtUart = Serial2;

static Diagnose diagnose;
static bool wsVerbunden = false;
static uint32_t letzteUartMs = 0;

// Abgleich der Effekt-Dateien läuft in einem eigenen Task, damit der WebSocket
// währenddessen weiter bedient wird.
struct SyncAuftrag {
  String basis;
  JsonDocument liste;
};
static volatile bool syncLaeuft = false;
static volatile bool syncFertig = false;
static int syncGeladen = 0, syncGeloescht = 0, syncFehler = 0;
static String syncMeldung;

static uint32_t fxFreiKb() { return (LittleFS.totalBytes() - LittleFS.usedBytes()) / 1024; }

// ---------------------------------------------------------------------------
// Effekt-Dateien abgleichen
// ---------------------------------------------------------------------------
// Datei per HTTP vom Server laden (wie in 1.1.0). fehler: was schiefging (für die Meldung an den Server)
static bool ladeDatei(const String& pfad, const char* zielPfad, uint32_t groesse, String& fehler) {
  HTTPClient http;
  http.setTimeout(8000);
  String url = String("http://") + SERVER_HOST + ":" + SERVER_PORT + pfad;
  if (!http.begin(url)) {
    fehler = "URL ungültig";
    return false;
  }
  int code = http.GET();
  if (code != 200) {
    fehler = String("HTTP ") + code + " (" + http.errorToString(code) + ")";
    http.end();
    return false;
  }
  String tmp = String(zielPfad) + ".tmp";
  File f = LittleFS.open(tmp, "w");
  if (!f) {
    fehler = "Datei " + tmp + " lässt sich nicht anlegen";
    http.end();
    return false;
  }
  WiFiClient* s = http.getStreamPtr();
  static uint8_t buf[1024];
  uint32_t geschrieben = 0;
  uint32_t letzteDaten = millis();
  while (geschrieben < groesse && millis() - letzteDaten < 8000) {
    int n = s->read(buf, min((uint32_t)sizeof(buf), groesse - geschrieben));
    if (n > 0) {
      if (f.write(buf, n) != (size_t)n) break;
      geschrieben += n;
      letzteDaten = millis();
    } else {
      delay(2);
    }
  }
  f.close();
  http.end();
  if (geschrieben != groesse) {
    fehler = String("nur ") + geschrieben + " von " + groesse + " Byte";
    LittleFS.remove(tmp);
    return false;
  }
  LittleFS.remove(zielPfad);
  if (!LittleFS.rename(tmp, zielPfad)) {
    fehler = "Umbenennen fehlgeschlagen";
    return false;
  }
  return true;
}

static void syncTask(void* arg) {
  SyncAuftrag* auftrag = static_cast<SyncAuftrag*>(arg);
  syncGeladen = syncGeloescht = syncFehler = 0;
  syncMeldung = "";

  fxSperren(true);
  delay(100);  // Audio-Task schließt offene Dateien

  JsonArrayConst liste = auftrag->liste.as<JsonArrayConst>();

  // 1. überzählige oder veränderte Dateien löschen
  File ordner = LittleFS.open(FX_ORDNER);
  std::vector<String> weg;
  for (File f = ordner.openNextFile(); f; f = ordner.openNextFile()) {
    String name = f.name();
    uint32_t groesse = f.size();
    f.close();
    bool behalten = false;
    for (JsonObjectConst e : liste) {
      String soll = String(e["id"] | "") + ".wav";
      if (name == soll && groesse == (e["groesse"] | 0u)) {
        behalten = true;
        break;
      }
    }
    if (!behalten) weg.push_back(String(FX_ORDNER) + "/" + name);
  }
  ordner.close();
  for (auto& p : weg)
    if (LittleFS.remove(p)) syncGeloescht++;

  // 2. fehlende Dateien laden
  for (JsonObjectConst e : liste) {
    const char* id = e["id"] | "";
    uint32_t groesse = e["groesse"] | 0u;
    if (!*id || !groesse) continue;
    char pfad[48];
    fxDateiPfad(id, pfad, sizeof(pfad));
    if (LittleFS.exists(pfad)) continue;
    if (groesse + 8192 > LittleFS.totalBytes() - LittleFS.usedBytes()) {
      syncFehler++;
      syncMeldung = "Flash voll";
      continue;
    }
    String fehler;
    uint32_t t0 = millis();
    if (ladeDatei(auftrag->basis + id, pfad, groesse, fehler)) {
      syncGeladen++;
    } else {
      syncFehler++;
      Serial.printf("[sound] Effekt %s: %s (%u ms)\n", id, fehler.c_str(), millis() - t0);
      fernlog("Effekt %s: %s (%lu ms)", id, fehler.c_str(), (unsigned long)(millis() - t0));
      if (!syncMeldung.length()) syncMeldung = String(id) + ": " + fehler + " (" + (millis() - t0) + " ms)";
    }
  }

  fxSperren(false);
  delete auftrag;
  syncFertig = true;
  syncLaeuft = false;
  vTaskDelete(nullptr);
}

// ---------------------------------------------------------------------------
// Server-Nachrichten
// ---------------------------------------------------------------------------
static void sende(JsonDocument& d) {
  String s;
  serializeJson(d, s);
  ws.sendTXT(s);
}

static void sendeHallo() {
  JsonDocument d;
  d["t"] = "hallo";
  d["rolle"] = "sound";
  d["fw"] = FW_VERSION;
  d["mac"] = WiFi.macAddress();
  d["ip"] = WiFi.localIP().toString();
  d["fx_frei_kb"] = fxFreiKb();
  d["fx_gesamt_kb"] = LittleFS.totalBytes() / 1024;
  d["max_stimmen"] = MAX_STIMMEN;
  diagnose.hallo(d);
  sende(d);
}

static void sendeStatus() {
  AudioStatus a = audioStatus();
  JsonDocument d;
  d["t"] = "status";
  d["rssi"] = WiFi.RSSI();
  d["uptime"] = millis() / 1000;
  d["heap"] = ESP.getFreeHeap();
  d["heap_min"] = ESP.getMinFreeHeap();
  d["musik"] = a.musikSoll;
  d["musik_verbunden"] = a.musikVerbunden;
  d["musik_spielt"] = a.musikSpielt;
  d["format_fehler"] = a.formatFehler;
  d["puffer"] = a.puffer;
  d["stimmen"] = a.stimmen;
  d["unterlaeufe"] = a.unterlaeufe;
  d["pcm_teilbloecke"] = a.pcmTeilbloecke;
  d["pcm_leerbloecke"] = a.pcmLeerbloecke;
  d["mp3_neupufferungen"] = a.mp3Neupufferungen;
  d["pcm_min_bytes"] = a.pcmMinVerfuegbar;
  d["fx_frei_kb"] = fxFreiKb();
  d["fx_gesamt_kb"] = LittleFS.totalBytes() / 1024;
  d["uart"] = (millis() - letzteUartMs) < 3000;
  d["sync"] = (bool)syncLaeuft;
  diagnose.status(d);
  sende(d);
}

static uint32_t zonenMaske(JsonArrayConst zonen) {
  uint32_t m = 0;
  for (JsonVariantConst v : zonen) {
    int z = v.as<int>();
    if (z >= 0 && z < 8) m |= 1u << z;
  }
  return m;
}

static void verarbeite(uint8_t* payload, size_t len) {
  JsonDocument doc;
  if (deserializeJson(doc, payload, len)) return;
  const char* t = doc["t"] | "";

  if (strcmp(t, "config") == 0) {
    AudioConfig c;
    c.master = constrain(doc["master"] | 80, 0, 100);
    c.musikVol = constrain(doc["musik_vol"] | 70, 0, 100);
    c.fxVol = constrain(doc["fx_vol"] | 100, 0, 100);
    c.duck = constrain(doc["duck"] | 40, 0, 100);
    c.latenzMs = constrain(doc["latenz_ms"] | 60, 0, 600);
    audioConfig(c);
  } else if (strcmp(t, "musik") == 0) {
    const char* aktion = doc["aktion"] | "";
    if (strcmp(aktion, "play") == 0)
      musikPlay(SERVER_HOST, SERVER_PORT, doc["pfad"] | "/stream/ambiente");
    else
      musikStop();
  } else if (strcmp(t, "fx") == 0) {
    const char* id = doc["id"] | "";
    char pfad[48];
    fxDateiPfad(id, pfad, sizeof(pfad));
    if (syncLaeuft || !LittleFS.exists(pfad)) {
      JsonDocument d;
      d["t"] = "fx_fehlt";
      d["id"] = id;
      sende(d);
      return;
    }
    JsonObjectConst l = doc["licht"].as<JsonObjectConst>();
    LichtAktion a = {};
    bool mitLicht = !l.isNull() && aktionVonName(l["name"] | "keine") != A_KEINE;
    if (mitLicht) {
      a.typ = aktionVonName(l["name"] | "keine");
      a.maske = (uint8_t)zonenMaske(l["zonen"].as<JsonArrayConst>());
      a.staerke = constrain(l["staerke"] | 100, 0, 100);
      a.dauerMs = constrain(l["dauer_ms"] | 1000, 50, 60000);
      a.param = constrain(l["param"] | 0, 0, 65535);
      a.verzMs = constrain(l["verz_ms"] | 0, 0, 10000);
    }
    fxStart(id, constrain(doc["vol"] | 100, 0, 100), mitLicht ? &a : nullptr);
  } else if (strcmp(t, "fx_stop") == 0) {
    fxStopAlle();
  } else if (strcmp(t, "fx_sync") == 0) {
    if (syncLaeuft) return;
    SyncAuftrag* auftrag = new SyncAuftrag();
    auftrag->basis = doc["basis"] | "/api/geraet/fx/";
    auftrag->liste.set(doc["liste"]);
    syncLaeuft = true;
    syncFertig = false;
    if (xTaskCreatePinnedToCore(syncTask, "fxsync", 8192, auftrag, 3, nullptr, 0) != pdPASS) {
      delete auftrag;
      syncLaeuft = false;
    }
  } else if (strcmp(t, "neustart") == 0) {
    delay(200);
    ESP.restart();
  }
}

static void wsEreignis(WStype_t typ, uint8_t* payload, size_t len) {
  switch (typ) {
    case WStype_CONNECTED:
      wsVerbunden = true;
      Serial.println(F("[sound] mit Server verbunden"));
      sendeHallo();
      break;
    case WStype_DISCONNECTED:
      if (wsVerbunden) Serial.println(F("[sound] Server-Verbindung getrennt"));
      wsVerbunden = false;
      break;
    case WStype_TEXT:
      verarbeite(payload, len);
      break;
    default:
      break;
  }
}

static void leseUart() {
  while (LichtUart.available()) {
    if (uartLeser.feed((uint8_t)LichtUart.read()) && uartLeser.typ == MSG_HERZSCHLAG) letzteUartMs = millis();
  }
}

// ---------------------------------------------------------------------------
void setup() {
  Serial.begin(115200);
  delay(100);
  Serial.printf("\n[sound] Spukzentrale Sound-ESP v%s, Start: %s\n", FW_VERSION, neustartGrund());

  if (!LittleFS.begin(true)) Serial.println(F("[sound] LittleFS konnte nicht eingebunden werden!"));
  if (!LittleFS.exists(FX_ORDNER)) LittleFS.mkdir(FX_ORDNER);
  Serial.printf("[sound] Effekt-Speicher: %u KB frei von %u KB\n", fxFreiKb(), LittleFS.totalBytes() / 1024);

  LichtUart.begin(UART_BAUD, SERIAL_8N1, UART_RX_PIN, UART_TX_PIN);
  audioStart(I2S_BCK_PIN, I2S_LCK_PIN, I2S_DIN_PIN, &LichtUart);

  diagnose.start();
  WiFi.setHostname(GERAETENAME);  // vor WiFi.mode(), sonst meldet sich der ESP als „esp32-…“
  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);  // kein Bluetooth im Spiel: volle WLAN-Leistung für den Stream
  WiFi.setAutoReconnect(true);
  WiFi.begin(WLAN_SSID, WLAN_PASS);

  String pfad = String("/ws/geraet?rolle=sound&fw=") + FW_VERSION;
  ws.begin(SERVER_HOST, SERVER_PORT, pfad);
  ws.onEvent(wsEreignis);
  ws.setReconnectInterval(3000);
  ws.enableHeartbeat(15000, 3000, 2);
}

// Hängt ein Abschnitt der Schleife länger als 500 ms, ans Fern-Log melden (wo genau es klemmt)
static void abschnitt(const char* name, uint32_t t0) {
  uint32_t d = millis() - t0;
  if (d > 500) fernlog("Hänger in %s: %lu ms", name, (unsigned long)d);
}

void loop() {
  static uint32_t naechsterStatus = 0;
  static bool wlanGemeldet = false;
  diagnose.schleifeStart();

  uint32_t t0 = millis();
  leseUart();
  abschnitt("UART", t0);

  if (WiFi.status() == WL_CONNECTED) {
    if (!wlanGemeldet) {
      Serial.printf("[sound] WLAN ok, IP %s\n", WiFi.localIP().toString().c_str());
      wlanGemeldet = true;
    }
    t0 = millis();
    ws.loop();
    abschnitt("WebSocket", t0);
  } else {
    wlanGemeldet = false;
  }

  // Fern-Log abschicken
  if (wsVerbunden) {
    char text[128];
    for (int i = 0; i < 4 && fernlogHolen(text, sizeof(text)); i++) {
      JsonDocument d;
      d["t"] = "log";
      d["text"] = text;
      sende(d);
      Serial.printf("[sound] %s\n", text);
    }
  }

  if (syncFertig && wsVerbunden) {
    syncFertig = false;
    JsonDocument d;
    d["t"] = "fx_sync_fertig";
    d["geladen"] = syncGeladen;
    d["geloescht"] = syncGeloescht;
    d["fehler"] = syncFehler;
    d["meldung"] = syncMeldung;
    d["fx_frei_kb"] = fxFreiKb();
    sende(d);
    Serial.printf("[sound] Effekte abgeglichen: %d geladen, %d gelöscht, %d Fehler\n", syncGeladen, syncGeloescht,
                  syncFehler);
  }

  uint32_t jetzt = millis();
  if (wsVerbunden && (int32_t)(jetzt - naechsterStatus) >= 0) {
    naechsterStatus = jetzt + 2000;
    t0 = millis();
    sendeStatus();
    abschnitt("Status", t0);
  }

  diagnose.schleifeEnde();
  delay(2);
}
