#pragma once

// ---------------------------------------------------------------------------
// Câblage — ESP32-S3 DevKitC-1 N16R8
// Les GPIO 35/36/37 sont pris par la PSRAM octale : ne pas les utiliser.
// ---------------------------------------------------------------------------

// Micro INMP441 (I2S, entrée). L/R relié à GND => canal gauche.
#define MIC_PIN_SCK 5   // BCLK
#define MIC_PIN_WS 6    // LRCLK
#define MIC_PIN_SD 4    // DATA

// Ampli MAX98357A (I2S, sortie). SD laissé en l'air ou à 3V3 => activé.
#define SPK_PIN_BCLK 15
#define SPK_PIN_LRC 16
#define SPK_PIN_DIN 7

// Bouton push-to-talk : BOOT intégré (GPIO0) ou un bouton externe vers GND.
#define PTT_PIN 0

// LED RGB intégrée (WS2812). 48 sur la plupart des N16R8, 38 sur DevKitC-1 v1.1.
#define LED_PIN 48
#define LED_BRIGHTNESS 40  // 0-255

// Yeux : 2 écrans ronds GC9A01 240x240, chacun sur son propre bus SPI
// (les deux bus tournent en DMA en parallèle). RST commun aux deux écrans.
// Broches des modules : VCC->3V3, GND, SCL->SCLK, SDA->MOSI, DC, CS, RST, BLK->3V3.
#define EYE_L_SCLK 12
#define EYE_L_MOSI 11
#define EYE_L_DC 10
#define EYE_L_CS 9
#define EYE_R_SCLK 2
#define EYE_R_MOSI 1
#define EYE_R_DC 42
#define EYE_R_CS 41
#define EYE_RST 14
#define EYE_SPI_HZ 80000000  // baisser à 40 MHz si image parasitée (fils longs)
#define EYE_INVERT true       // couleurs inversées ? passer à false
#define EYE_L_ROTATION 0      // 0-3 selon le montage des écrans
#define EYE_R_ROTATION 0

// ---------------------------------------------------------------------------
// Audio
// ---------------------------------------------------------------------------
#define MIC_RATE 16000
#define MIC_CHUNK_SAMPLES 512     // 32 ms par trame envoyée
#define MIC_GAIN_SHIFT 14         // 32 bits -> 16 bits ; plus petit = plus fort
#define MAX_RECORD_MS 20000

#define SPK_DEFAULT_RATE 24000
#define SPK_VOLUME 70             // 0-100
#define SPK_BUFFER_BYTES (2 * 1024 * 1024)  // ~43 s à 24 kHz, en PSRAM

// ---------------------------------------------------------------------------
// Serveur (pont Opal) — WiFi, domaine, port et jeton dans secrets.h
// ---------------------------------------------------------------------------
#define OPAL_SERVER_PATH "/opal"
