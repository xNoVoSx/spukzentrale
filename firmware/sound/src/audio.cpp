// Spukzentrale – Audio-Pipeline des Sound-ESP (siehe audio.h)
#include "audio.h"

#include <Arduino.h>
#include <LittleFS.h>
#include <MP3DecoderHelix.h>
#include <WiFi.h>
#include <driver/i2s_std.h>
#include <freertos/queue.h>
#include <freertos/stream_buffer.h>
#include <math.h>

#include "adpcm.h"
#include "chunked.h"

namespace spuk {

namespace {

constexpr uint32_t RATE = 44100;
constexpr int BLOCK = 441;                    // 10 ms pro Mischblock
constexpr int DMA_BLOECKE = 6;                // I2S-DMA hält 60 ms vor
constexpr size_t MP3_PUFFER = 32 * 1024;      // ca. 2 s bei 128 kbit/s
constexpr size_t MP3_VORLAUF = 12 * 1024;     // erst ab diesem Füllstand dekodieren
constexpr size_t PCM_PUFFER = 8 * 1024;       // ca. 45 ms dekodiertes Stereo-PCM
constexpr int MAX_BLOCK_BYTES = 1024;         // größtes unterstütztes ADPCM-Blockformat
constexpr int MAX_BLOCK_SAMPLES = (MAX_BLOCK_BYTES - 4) * 2 + 1;
constexpr int HUELLE_VERZ = 64;               // max. 640 ms Verzögerung

// ---------------------------------------------------------------------------
// Gemeinsamer Zustand
// ---------------------------------------------------------------------------
portMUX_TYPE sperre = portMUX_INITIALIZER_UNLOCKED;
AudioConfig cfg;  // nur unter `sperre` lesen/schreiben

struct MusikZiel {
  char host[64] = "";
  uint16_t port = 0;
  char pfad[128] = "";
};
MusikZiel ziel;                          // unter `sperre`
volatile bool musikSoll = false;         // Netz + Decoder arbeiten
volatile bool musikStopAnfrage = false;  // Audio-Task blendet aus, dann musikSoll = false
volatile uint32_t musikGen = 0;          // ändert sich bei jedem Start/Stopp
volatile bool netzVerbunden = false;
volatile bool formatFehler = false;
volatile bool musikSpielt = false;
volatile uint32_t unterlaeufe = 0;
volatile uint32_t pcmTeilbloecke = 0;
volatile uint32_t pcmLeerbloecke = 0;
volatile uint32_t mp3Neupufferungen = 0;
volatile uint32_t pcmMinVerfuegbar = PCM_PUFFER;
volatile uint8_t aktiveStimmen = 0;
volatile bool fxGesperrt = false;

i2s_chan_handle_t tx = nullptr;
HardwareSerial* uart = nullptr;
StreamBufferHandle_t mp3Puffer = nullptr;
StreamBufferHandle_t pcmPuffer = nullptr;
QueueHandle_t befehle = nullptr;

struct Befehl {
  enum Typ : uint8_t { START, STOPP_ALLE } typ;
  char id[24];
  uint8_t vol;
  bool mitAktion;
  LichtAktion aktion;
};

// ---------------------------------------------------------------------------
// Netz: HTTP-Stream lesen (normal oder "chunked")
// ---------------------------------------------------------------------------
bool leseKopf(WiFiClient& c, bool& chunked) {
  String zeile;
  bool erste = true, ok = false;
  chunked = false;
  uint32_t bis = millis() + 4000;
  while (millis() < bis) {
    if (!c.connected() && !c.available()) return false;
    int b = c.read();
    if (b < 0) {
      vTaskDelay(pdMS_TO_TICKS(5));
      continue;
    }
    if (b == '\r') continue;
    if (b != '\n') {
      if (zeile.length() < 200) zeile += (char)b;
      continue;
    }
    if (erste) {
      ok = zeile.indexOf(" 200") > 0;
      erste = false;
    } else if (zeile.length() == 0) {
      return ok;  // Leerzeile = Ende des Kopfes
    } else {
      zeile.toLowerCase();
      if (zeile.startsWith("transfer-encoding:") && zeile.indexOf("chunked") > 0) chunked = true;
    }
    zeile = "";
  }
  return false;
}

void netzTask(void*) {
  static uint8_t buf[1460];
  WiFiClient c;
  ChunkDekoder chunks;
  uint32_t gen = 0;
  for (;;) {
    if (!musikSoll || WiFi.status() != WL_CONNECTED) {
      if (c.connected()) c.stop();
      netzVerbunden = false;
      vTaskDelay(pdMS_TO_TICKS(50));
      continue;
    }
    if (!netzVerbunden || gen != musikGen || !c.connected()) {
      c.stop();
      netzVerbunden = false;
      gen = musikGen;
      MusikZiel z;
      portENTER_CRITICAL(&sperre);
      z = ziel;
      portEXIT_CRITICAL(&sperre);
      if (!c.connect(z.host, z.port, 3000)) {
        vTaskDelay(pdMS_TO_TICKS(1500));
        continue;
      }
      c.setNoDelay(true);
      c.printf("GET %s HTTP/1.1\r\nHost: %s\r\nConnection: close\r\nUser-Agent: spukzentrale-sound/" FW_VERSION
               "\r\n\r\n",
               z.pfad, z.host);
      bool chunked = false;
      if (!leseKopf(c, chunked)) {
        c.stop();
        vTaskDelay(pdMS_TO_TICKS(1500));
        continue;
      }
      chunks.start(chunked);
      netzVerbunden = true;
    }
    int roh = c.read(buf, sizeof(buf));
    if (roh <= 0) {
      vTaskDelay(pdMS_TO_TICKS(5));
      continue;
    }
    size_t n = chunks.verarbeite(buf, roh, buf);
    if (chunks.fehler() || chunks.ende()) {  // kaputter Stream oder Server beendet -> neu verbinden
      c.stop();
      netzVerbunden = false;
      vTaskDelay(pdMS_TO_TICKS(500));
      continue;
    }
    // Puffer voll -> warten (TCP bremst den Server dann von selbst)
    size_t gesendet = 0;
    while (gesendet < n && musikSoll && gen == musikGen)
      gesendet += xStreamBufferSend(mp3Puffer, buf + gesendet, n - gesendet, pdMS_TO_TICKS(200));
  }
}

// ---------------------------------------------------------------------------
// Decoder: MP3 -> PCM (Stereo, 44,1 kHz)
// ---------------------------------------------------------------------------
void sendePcm(const int16_t* daten, size_t bytes, uint32_t gen) {
  size_t gesendet = 0;
  while (gesendet < bytes && musikSoll && gen == musikGen)
    gesendet += xStreamBufferSend(pcmPuffer, (const uint8_t*)daten + gesendet, bytes - gesendet, pdMS_TO_TICKS(100));
}

void pcmFertig(MP3FrameInfo& info, short* pcm, size_t len, void*) {
  if (info.samprate != (int)RATE || info.nChans < 1 || info.nChans > 2) {
    formatFehler = true;
    return;
  }
  formatFehler = false;
  uint32_t gen = musikGen;
  if (info.nChans == 2) {
    sendePcm(pcm, len * sizeof(int16_t), gen);
    return;
  }
  // Mono -> Stereo
  static int16_t stereo[512];
  for (size_t i = 0; i < len;) {
    size_t n = 0;
    while (i < len && n + 2 <= 512) {
      stereo[n++] = pcm[i];
      stereo[n++] = pcm[i];
      i++;
    }
    sendePcm(stereo, n * sizeof(int16_t), gen);
  }
}

libhelix::MP3DecoderHelix mp3(pcmFertig);

void decoderTask(void*) {
  static uint8_t buf[512];
  uint32_t gen = musikGen;
  bool vorlauf = true;
  mp3.begin();
  for (;;) {
    if (gen != musikGen || !musikSoll) {
      while (xStreamBufferReceive(mp3Puffer, buf, sizeof(buf), 0) > 0) {
      }
      if (gen != musikGen) {
        mp3.end();
        mp3.begin();
        gen = musikGen;
        vorlauf = true;
      }
      if (!musikSoll) {
        vTaskDelay(pdMS_TO_TICKS(20));
        continue;
      }
    }
    if (vorlauf) {
      if (xStreamBufferBytesAvailable(mp3Puffer) < MP3_VORLAUF) {
        vTaskDelay(pdMS_TO_TICKS(20));
        continue;
      }
      vorlauf = false;
    }
    size_t n = xStreamBufferReceive(mp3Puffer, buf, sizeof(buf), pdMS_TO_TICKS(150));
    if (n == 0) {
      mp3Neupufferungen++;
      vorlauf = true;  // Puffer leergelaufen -> neu vorpuffern statt stottern
      continue;
    }
    mp3.write(buf, n);
  }
}

// ---------------------------------------------------------------------------
// Effekt-Stimmen (IMA-ADPCM, beliebige Abtastrate -> 44,1 kHz linear interpoliert)
// ---------------------------------------------------------------------------
struct Stimme {
  bool aktiv = false;
  File datei;
  WavInfo info;
  uint8_t block[MAX_BLOCK_BYTES];
  int16_t pcm[MAX_BLOCK_SAMPLES];
  size_t pcmN = 0, pcmI = 0;
  uint32_t gelesen = 0;  // bereits ausgegebene Samples der Datei
  uint32_t schritt = 0;  // 16.16: Dateisamples pro Ausgabesample
  uint32_t phase = 0;
  int16_t a = 0, b = 0;
  bool ende = false;
  float vol = 1;
  float einblenden = 0;
  bool mitAktion = false;
  LichtAktion aktion;
  uint32_t start = 0;
};
Stimme stimmen[MAX_STIMMEN];

bool naechstesSample(Stimme& s, int16_t& wert) {
  if (s.pcmI >= s.pcmN) {
    if (s.gelesen >= s.info.samples) return false;
    int n = s.datei.read(s.block, s.info.blockAlign);
    if (n != s.info.blockAlign) return false;
    s.pcmN = dekodiereImaBlock(s.block, n, s.pcm);
    s.pcmI = 0;
    uint32_t rest = s.info.samples - s.gelesen;
    if (s.pcmN > rest) s.pcmN = rest;
    if (s.pcmN == 0) return false;
  }
  wert = s.pcm[s.pcmI++];
  s.gelesen++;
  return true;
}

void stimmeBeenden(Stimme& s) {
  if (s.datei) s.datei.close();
  s.aktiv = false;
}

void stimmeStarten(const Befehl& b) {
  if (fxGesperrt) return;
  char pfad[48];
  fxDateiPfad(b.id, pfad, sizeof(pfad));
  // freie Stimme suchen, sonst die älteste ersetzen
  Stimme* s = nullptr;
  for (auto& st : stimmen)
    if (!st.aktiv) {
      s = &st;
      break;
    }
  if (!s) {
    s = &stimmen[0];
    for (auto& st : stimmen)
      if (st.start < s->start) s = &st;
    stimmeBeenden(*s);
  }
  s->datei = LittleFS.open(pfad, "r");
  if (!s->datei) return;
  File& f = s->datei;
  auto lesen = [&f](uint32_t off, uint8_t* ziel, uint32_t n) -> uint32_t {
    if (!f.seek(off)) return 0;
    return f.read(ziel, n);
  };
  s->info = WavInfo();
  if (!leseWavKopf(lesen, f.size(), s->info) || s->info.blockAlign > MAX_BLOCK_BYTES || !f.seek(s->info.datenStart)) {
    log_e("Effekt %s: ungültige Datei", b.id);
    stimmeBeenden(*s);
    return;
  }
  s->pcmN = s->pcmI = 0;
  s->gelesen = 0;
  s->schritt = (uint32_t)(((uint64_t)s->info.abtastrate << 16) / RATE);
  s->phase = 0;
  s->ende = false;
  int16_t w0 = 0, w1 = 0;
  if (!naechstesSample(*s, w0)) {
    stimmeBeenden(*s);
    return;
  }
  if (!naechstesSample(*s, w1)) w1 = w0;
  s->a = w0;
  s->b = w1;
  s->vol = b.vol / 100.0f;
  s->einblenden = 0;
  s->mitAktion = b.mitAktion;
  s->aktion = b.aktion;
  s->start = millis();
  s->aktiv = true;
}

// Mischt alle Stimmen in fx[] (mono, Gleitkomma -1..1). Gibt den Spitzenwert zurück.
float mischeStimmen(float* fx, uint16_t latenzMs) {
  for (int i = 0; i < BLOCK; i++) fx[i] = 0;
  float spitze = 0;
  uint8_t aktiv = 0;
  for (auto& s : stimmen) {
    if (!s.aktiv) continue;
    if (s.mitAktion && uart) {
      // Licht-Aktion zeitgleich mit dem ersten Ton (Ton kommt nach latenzMs aus dem DAC)
      LichtAktion a = s.aktion;
      a.verzMs = (uint16_t)(a.verzMs + latenzMs);
      uint8_t rahmen[MAX_PAYLOAD + 5];
      size_t n = baueRahmen(MSG_AKTION, &a, sizeof(a), rahmen);
      uart->write(rahmen, n);
      s.mitAktion = false;
    }
    const float g = s.vol / 32768.0f;
    for (int i = 0; i < BLOCK && !s.ende; i++) {
      float frac = (s.phase & 0xFFFF) / 65536.0f;
      float v = (s.a + (s.b - s.a) * frac) * g;
      if (s.einblenden < 1.0f) {  // 2 ms Einblenden gegen Knackser
        s.einblenden += 1.0f / 88.0f;
        v *= s.einblenden;
      }
      fx[i] += v;
      s.phase += s.schritt;
      while (s.phase >= 0x10000) {
        s.phase -= 0x10000;
        s.a = s.b;
        int16_t w;
        if (!naechstesSample(s, w)) {
          s.ende = true;
          break;
        }
        s.b = w;
      }
    }
    if (s.ende)
      stimmeBeenden(s);
    else
      aktiv++;
  }
  for (int i = 0; i < BLOCK; i++) {
    float a = fabsf(fx[i]);
    if (a > spitze) spitze = a;
  }
  aktiveStimmen = aktiv;
  return spitze;
}

// Weicher Limiter: unter 0,8 linear, darüber sanft gegen 1,0
inline float begrenze(float x) {
  const float s = 0.8f;
  float a = fabsf(x);
  if (a <= s) return x;
  float y = s + (1.0f - s) * tanhf((a - s) / (1.0f - s));
  return x < 0 ? -y : y;
}

void audioTask(void*) {
  static int16_t musik[BLOCK * 2];
  static int16_t aus[BLOCK * 2];
  static float fx[BLOCK];
  static uint8_t verzMusik[HUELLE_VERZ], verzFx[HUELLE_VERZ];
  uint8_t verzPos = 0;
  float duckIst = 1.0f;
  float musikFade = 0.0f;  // weiches Ein-/Ausblenden des Ambientes
  bool hatteMusik = false;

  for (;;) {
    Befehl b;
    while (xQueueReceive(befehle, &b, 0) == pdTRUE) {
      if (b.typ == Befehl::START)
        stimmeStarten(b);
      else
        for (auto& s : stimmen) stimmeBeenden(s);
    }

    AudioConfig c;
    portENTER_CRITICAL(&sperre);
    c = cfg;
    portEXIT_CRITICAL(&sperre);

    // --- Ambiente ---
    size_t bytes = 0;
    size_t pcmVerfuegbar = musikSoll ? xStreamBufferBytesAvailable(pcmPuffer) : 0;
    if (musikSoll && pcmVerfuegbar < pcmMinVerfuegbar) pcmMinVerfuegbar = pcmVerfuegbar;
    // Nur vollständige 10-ms-Blöcke entnehmen. Teilblöcke bleiben im StreamBuffer,
    // damit Decoder-Jitter nicht mitten in einem Audioblock in Stille umgewandelt wird.
    if (musikSoll && pcmVerfuegbar >= sizeof(musik))
      bytes = xStreamBufferReceive(pcmPuffer, musik, sizeof(musik), 0);
    if (musikSoll && bytes < sizeof(musik)) {
      if (pcmVerfuegbar > 0) pcmTeilbloecke++;
      else pcmLeerbloecke++;
      if (hatteMusik && !musikStopAnfrage) unterlaeufe++;
    }
    if (bytes < sizeof(musik)) memset((uint8_t*)musik + bytes, 0, sizeof(musik) - bytes);
    hatteMusik = bytes == sizeof(musik);
    musikSpielt = bytes == sizeof(musik);

    float fadeZiel = (musikSoll && !musikStopAnfrage) ? 1.0f : 0.0f;
    float fadeSchritt = fadeZiel > musikFade ? 0.01f : 0.04f;  // 1 s ein, 250 ms aus
    if (musikStopAnfrage && musikFade <= 0.0f) {
      musikStopAnfrage = false;
      musikSoll = false;
      musikGen = musikGen + 1;
    }

    // --- Effekte ---
    float fxSpitze = mischeStimmen(fx, c.latenzMs) * (c.fxVol / 100.0f);

    // --- Ducking: Ambiente leiser, solange Effekte laufen ---
    float duckZiel = aktiveStimmen ? 1.0f - c.duck / 100.0f : 1.0f;
    duckIst += (duckZiel - duckIst) * (duckZiel < duckIst ? 0.30f : 0.015f);  // ~30 ms runter, ~700 ms hoch

    // --- Mischen ---
    const float master = c.master / 100.0f;
    const float fxG = c.fxVol / 100.0f;
    float summeQ = 0;
    for (int i = 0; i < BLOCK; i++) {
      if (musikFade != fadeZiel) {
        musikFade += fadeZiel > musikFade ? fadeSchritt / BLOCK : -fadeSchritt / BLOCK;
        if ((fadeZiel > 0 && musikFade > 1) || (fadeZiel == 0 && musikFade < 0)) musikFade = fadeZiel;
      }
      float mG = (c.musikVol / 100.0f) * duckIst * musikFade / 32768.0f;
      float l = musik[2 * i] * mG;
      float r = musik[2 * i + 1] * mG;
      float m = (l + r) * 0.5f;
      summeQ += m * m;
      float f = fx[i] * fxG;
      aus[2 * i] = (int16_t)(begrenze((l + f) * master) * 32767.0f);
      aus[2 * i + 1] = (int16_t)(begrenze((r + f) * master) * 32767.0f);
    }

    // --- Hüllkurve, um die Ausgabe-Latenz verzögert an den Licht-ESP ---
    float rms = sqrtf(summeQ / BLOCK);
    verzMusik[verzPos] = (uint8_t)fminf(rms * 2.0f * 255.0f, 255.0f);
    verzFx[verzPos] = (uint8_t)fminf(fxSpitze * 255.0f, 255.0f);
    int verz = c.latenzMs / 10;
    if (verz >= HUELLE_VERZ) verz = HUELLE_VERZ - 1;
    uint8_t idx = (uint8_t)((verzPos + HUELLE_VERZ - verz) % HUELLE_VERZ);
    verzPos = (verzPos + 1) % HUELLE_VERZ;
    if (uart) {
      Huelle h;
      h.musik = verzMusik[idx];
      h.fx = verzFx[idx];
      h.flags = (musikSpielt ? FLAG_MUSIK_AKTIV : 0) | (aktiveStimmen ? FLAG_FX_AKTIV : 0) |
                (WiFi.status() == WL_CONNECTED ? FLAG_SERVER : 0);
      uint8_t rahmen[MAX_PAYLOAD + 5];
      size_t n = baueRahmen(MSG_HUELLE, &h, sizeof(h), rahmen);
      uart->write(rahmen, n);
    }

    // blockiert, bis im DMA Platz ist -> gibt den 10-ms-Takt vor
    size_t geschrieben = 0;
    i2s_channel_write(tx, aus, sizeof(aus), &geschrieben, portMAX_DELAY);
  }
}

}  // namespace

// ---------------------------------------------------------------------------
// Öffentliche Schnittstelle
// ---------------------------------------------------------------------------
void fxDateiPfad(const char* id, char* zielPfad, size_t laenge) { snprintf(zielPfad, laenge, "%s/%s.wav", FX_ORDNER, id); }

void audioStart(int bck, int lck, int din, HardwareSerial* u) {
  uart = u;
  mp3Puffer = xStreamBufferCreate(MP3_PUFFER, 1);
  pcmPuffer = xStreamBufferCreate(PCM_PUFFER, 1);
  befehle = xQueueCreate(8, sizeof(Befehl));

  i2s_chan_config_t kanal = I2S_CHANNEL_DEFAULT_CONFIG(I2S_NUM_0, I2S_ROLE_MASTER);
  kanal.dma_desc_num = DMA_BLOECKE;
  kanal.dma_frame_num = BLOCK;
  kanal.auto_clear = true;  // bei Aussetzern Stille statt Brummen
  ESP_ERROR_CHECK(i2s_new_channel(&kanal, &tx, nullptr));
  i2s_std_config_t std = {
      .clk_cfg = I2S_STD_CLK_DEFAULT_CONFIG(RATE),
      .slot_cfg = I2S_STD_PHILIPS_SLOT_DEFAULT_CONFIG(I2S_DATA_BIT_WIDTH_16BIT, I2S_SLOT_MODE_STEREO),
      .gpio_cfg =
          {
              .mclk = I2S_GPIO_UNUSED,
              .bclk = (gpio_num_t)bck,
              .ws = (gpio_num_t)lck,
              .dout = (gpio_num_t)din,
              .din = I2S_GPIO_UNUSED,
              .invert_flags = {.mclk_inv = false, .bclk_inv = false, .ws_inv = false},
          },
  };
  ESP_ERROR_CHECK(i2s_channel_init_std_mode(tx, &std));
  ESP_ERROR_CHECK(i2s_channel_enable(tx));

  // Audio auf Kern 1 mit hoher Priorität, Netz/Decoder auf Kern 0 neben dem WLAN
  xTaskCreatePinnedToCore(audioTask, "audio", 6144, nullptr, 20, nullptr, 1);
  xTaskCreatePinnedToCore(decoderTask, "mp3", 8192, nullptr, 10, nullptr, 0);
  xTaskCreatePinnedToCore(netzTask, "netz", 4096, nullptr, 5, nullptr, 0);
}

void audioConfig(const AudioConfig& c) {
  portENTER_CRITICAL(&sperre);
  cfg = c;
  portEXIT_CRITICAL(&sperre);
}

void musikPlay(const char* host, uint16_t port, const char* pfad) {
  portENTER_CRITICAL(&sperre);
  strlcpy(ziel.host, host, sizeof(ziel.host));
  ziel.port = port;
  strlcpy(ziel.pfad, pfad, sizeof(ziel.pfad));
  portEXIT_CRITICAL(&sperre);
  musikStopAnfrage = false;
  musikGen = musikGen + 1;
  musikSoll = true;
}

void musikStop() {
  if (musikSoll) musikStopAnfrage = true;
}

void fxStart(const char* id, uint8_t vol, const LichtAktion* aktion) {
  Befehl b = {};
  b.typ = Befehl::START;
  strlcpy(b.id, id, sizeof(b.id));
  b.vol = vol > 100 ? 100 : vol;
  b.mitAktion = aktion != nullptr;
  if (aktion) b.aktion = *aktion;
  xQueueSend(befehle, &b, 0);
}

void fxStopAlle() {
  Befehl b = {};
  b.typ = Befehl::STOPP_ALLE;
  xQueueSend(befehle, &b, pdMS_TO_TICKS(50));
}

void fxSperren(bool gesperrt) {
  fxGesperrt = gesperrt;
  if (gesperrt) fxStopAlle();
}

AudioStatus audioStatus() {
  AudioStatus s;
  s.musikSoll = musikSoll && !musikStopAnfrage;
  s.musikVerbunden = netzVerbunden;
  s.musikSpielt = musikSpielt;
  s.formatFehler = formatFehler;
  s.puffer = (uint8_t)(xStreamBufferBytesAvailable(mp3Puffer) * 100 / MP3_PUFFER);
  s.stimmen = aktiveStimmen;
  s.unterlaeufe = unterlaeufe;
  s.pcmTeilbloecke = pcmTeilbloecke;
  s.pcmLeerbloecke = pcmLeerbloecke;
  s.mp3Neupufferungen = mp3Neupufferungen;
  s.pcmMinVerfuegbar = pcmMinVerfuegbar;
  return s;
}

}  // namespace spuk
