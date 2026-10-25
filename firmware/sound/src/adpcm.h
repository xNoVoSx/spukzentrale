// Spukzentrale – IMA-ADPCM-Dekoder für WAV-Dateien (Format 0x11, mono)
//
// Die Effekt-Sounds erzeugt der Server mit ffmpeg (-c:a adpcm_ima_wav). Pro Block:
// 4 Byte Kopf (Startwert int16, Schrittindex u8, reserviert u8), danach 4-Bit-Werte,
// unteres Nibble zuerst. Der Kopf-Startwert ist das erste Sample des Blocks.
//
// Frei von Arduino-Abhängigkeiten, damit er am PC gegen ffmpeg getestet werden kann
// (test/test_adpcm).
#pragma once
#include <stddef.h>
#include <stdint.h>
#include <string.h>

namespace spuk {

struct WavInfo {
  uint32_t abtastrate = 0;
  uint16_t blockAlign = 0;       // Bytes pro Block
  uint16_t samplesProBlock = 0;  // inkl. Kopf-Sample
  uint32_t samples = 0;          // Gesamtzahl laut fact-Chunk
  uint32_t datenStart = 0;       // Offset des data-Chunks in der Datei
  uint32_t datenLaenge = 0;
};

inline uint16_t le16(const uint8_t* p) { return (uint16_t)(p[0] | (p[1] << 8)); }
inline uint32_t le32(const uint8_t* p) { return (uint32_t)p[0] | ((uint32_t)p[1] << 8) | ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24); }

// Liest den WAV-Kopf. lesen(offset, ziel, laenge) muss die Datei lesen und die Anzahl
// gelesener Bytes liefern. Gibt false zurück, wenn das Format nicht passt.
template <typename Leser>
bool leseWavKopf(Leser lesen, uint32_t dateiGroesse, WavInfo& info) {
  uint8_t kopf[12];
  if (lesen(0, kopf, 12) != 12) return false;
  if (memcmp(kopf, "RIFF", 4) != 0 || memcmp(kopf + 8, "WAVE", 4) != 0) return false;
  uint32_t pos = 12;
  bool fmtOk = false;
  while (pos + 8 <= dateiGroesse) {
    uint8_t ch[8];
    if (lesen(pos, ch, 8) != 8) return false;
    uint32_t laenge = le32(ch + 4);
    if (memcmp(ch, "fmt ", 4) == 0) {
      uint8_t f[20] = {0};
      uint32_t n = laenge < sizeof(f) ? laenge : sizeof(f);
      if (lesen(pos + 8, f, n) != n || n < 16) return false;
      if (le16(f) != 0x11 || le16(f + 2) != 1 || le16(f + 14) != 4) return false;  // IMA, mono, 4 Bit
      info.abtastrate = le32(f + 4);
      info.blockAlign = le16(f + 12);
      info.samplesProBlock = n >= 20 ? le16(f + 18) : (uint16_t)((info.blockAlign - 4) * 2 + 1);
      fmtOk = true;
    } else if (memcmp(ch, "fact", 4) == 0) {
      uint8_t f[4];
      if (lesen(pos + 8, f, 4) != 4) return false;
      info.samples = le32(f);
    } else if (memcmp(ch, "data", 4) == 0) {
      info.datenStart = pos + 8;
      info.datenLaenge = laenge;
      if (info.datenStart + info.datenLaenge > dateiGroesse) info.datenLaenge = dateiGroesse - info.datenStart;
      break;
    }
    pos += 8 + laenge + (laenge & 1);
  }
  if (!fmtOk || info.datenStart == 0 || info.blockAlign < 5 || info.abtastrate == 0) return false;
  if (info.samplesProBlock != (info.blockAlign - 4) * 2 + 1) return false;
  if (info.samples == 0) info.samples = (info.datenLaenge / info.blockAlign) * info.samplesProBlock;
  return true;
}

// Dekodiert einen Block (blockAlign Bytes) nach pcm (samplesProBlock Werte).
// Gibt die Anzahl dekodierter Samples zurück (0 bei kaputtem Kopf).
inline size_t dekodiereImaBlock(const uint8_t* block, size_t laenge, int16_t* pcm) {
  static const int16_t SCHRITTE[89] = {
      7,     8,     9,     10,    11,    12,    13,    14,    16,    17,    19,    21,    23,
      25,    28,    31,    34,    37,    41,    45,    50,    55,    60,    66,    73,    80,
      88,    97,    107,   118,   130,   143,   157,   173,   190,   209,   230,   253,   279,
      307,   337,   371,   408,   449,   494,   544,   598,   658,   724,   796,   876,   963,
      1060,  1166,  1282,  1411,  1552,  1707,  1878,  2066,  2272,  2499,  2749,  3024,  3327,
      3660,  4026,  4428,  4871,  5358,  5894,  6484,  7132,  7845,  8630,  9493,  10442, 11487,
      12635, 13899, 15289, 16818, 18500, 20350, 22385, 24623, 27086, 29794, 32767};
  static const int8_t INDEX_TABELLE[16] = {-1, -1, -1, -1, 2, 4, 6, 8, -1, -1, -1, -1, 2, 4, 6, 8};

  if (laenge < 4) return 0;
  int32_t wert = (int16_t)le16(block);
  int index = block[2];
  if (index > 88) return 0;
  size_t n = 0;
  pcm[n++] = (int16_t)wert;
  for (size_t i = 4; i < laenge; i++) {
    for (int haelfte = 0; haelfte < 2; haelfte++) {
      uint8_t nib = haelfte == 0 ? (block[i] & 0x0F) : (block[i] >> 4);
      int32_t schritt = SCHRITTE[index];
      int32_t diff = schritt >> 3;
      if (nib & 1) diff += schritt >> 2;
      if (nib & 2) diff += schritt >> 1;
      if (nib & 4) diff += schritt;
      if (nib & 8)
        wert -= diff;
      else
        wert += diff;
      if (wert > 32767) wert = 32767;
      if (wert < -32768) wert = -32768;
      index += INDEX_TABELLE[nib];
      if (index < 0) index = 0;
      if (index > 88) index = 88;
      pcm[n++] = (int16_t)wert;
    }
  }
  return n;
}

}  // namespace spuk
