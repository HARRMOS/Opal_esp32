// ESP32-S3 transformé en adaptateur USB-série pour flasher l'ESP32-CAM (qui n'a pas d'USB).
//
// Tout ce qui arrive du PC par l'USB de l'S3 part vers la caméra (UART1), et inversement.
// Câblage : S3 GPIO17 -> U0R de la caméra, S3 GPIO18 <- U0T, 5V -> 5V, GND -> GND,
// et IO0 de la caméra relié à GND pendant le flash (puis appui sur RST de la caméra).
//
// Une fois la caméra flashée, reflasher l'S3 avec l'env opal-s3.

#include <Arduino.h>

#define CAM_TX_PIN 17  // vers U0R de la caméra
#define CAM_RX_PIN 18  // depuis U0T de la caméra
#define LED_PIN 48

static uint8_t buf[512];

void setup() {
  Serial.begin(115200);
  Serial1.setRxBufferSize(8192);
  Serial1.begin(115200, SERIAL_8N1, CAM_RX_PIN, CAM_TX_PIN);
  rgbLedWrite(LED_PIN, 0, 0, 30);  // bleu : passerelle prête
}

void loop() {
  bool busy = false;
  while (int n = Serial.available()) {
    n = Serial.read(buf, min((size_t)n, sizeof(buf)));
    Serial1.write(buf, n);
    busy = true;
  }
  while (int n = Serial1.available()) {
    n = Serial1.read(buf, min((size_t)n, sizeof(buf)));
    Serial.write(buf, n);
    busy = true;
  }
  rgbLedWrite(LED_PIN, 0, busy ? 40 : 0, 30);  // vert pendant les échanges
}
