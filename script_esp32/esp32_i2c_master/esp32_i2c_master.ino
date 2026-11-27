#include <Wire.h>

#define BAUD_RATE 500000
#define I2C_ADDR 0x42
#define I2C_FREQ 400000
#define SDA_PIN 21
#define SCL_PIN 22

#define PKT_SIZE 244
#define MAX_WAVE_PKTS 32
#define WAVE_BUF_SIZE (MAX_WAVE_PKTS * PKT_SIZE) // 7808 byte

#define CMD_POLL_ACK 0x01
#define CMD_CHECK_READY 0x02
#define MSG_ACK_I2C 0x10
#define MSG_READY 0x67

uint8_t waveBuffer[WAVE_BUF_SIZE];

bool pollAcksAndForward() {
  Wire.beginTransmission(I2C_ADDR);
  Wire.write(CMD_POLL_ACK);
  if (Wire.endTransmission() != 0) return false;

  delayMicroseconds(500);
  if (Wire.requestFrom(I2C_ADDR, 64) != 64) return false;

  bool anyForwarded = false;
  for (int i = 0; i < 64; i += 4) {
    uint8_t sig[4];
    for (int k = 0; k < 4; k++) sig[k] = Wire.read();
    if (sig[0] != 0x00 && sig[0] != 0xFF) {
      Serial.write(sig, 4);
      anyForwarded = true;
    }
  }
  return anyForwarded;
}

void setup() {
  Serial.setRxBufferSize(16384);
  Serial.begin(BAUD_RATE);
  Serial.setTimeout(2000);

  Wire.setBufferSize(512);
  Wire.begin(SDA_PIN, SCL_PIN, I2C_FREQ);

  // Tanya status READY (0x67) ke ESP32 #2 sampai BLE terhubung
  while (true) {
    Wire.beginTransmission(I2C_ADDR);
    Wire.write(CMD_CHECK_READY);
    if (Wire.endTransmission() == 0) {
      delayMicroseconds(500);
      if (Wire.requestFrom(I2C_ADDR, 4) == 4) {
        uint8_t sig[4];
        for (int i = 0; i < 4; i++) sig[i] = Wire.read();
        if (sig[0] == MSG_READY) {
          Serial.write(sig, 4);
          break;
        }
      }
    }
    delay(100);
  }
}

void loop() {
  if (Serial.available() < 1) {
    pollAcksAndForward();
    delay(5);
    return;
  }

  uint8_t count = (uint8_t)Serial.read();
  if (count == 0 || count > MAX_WAVE_PKTS) return;

  size_t totalBytes = (size_t)count * PKT_SIZE;
  size_t got = Serial.readBytes(waveBuffer, totalBytes);
  if (got != totalBytes) return;

  for (uint8_t i = 0; i < count; i++) {
    uint8_t *pkt = waveBuffer + (i * PKT_SIZE);
    while (true) {
      Wire.beginTransmission(I2C_ADDR);
      Wire.write(pkt, PKT_SIZE);
      if (Wire.endTransmission() != 0) {
        delay(2);
        continue;
      }

      delayMicroseconds(500);
      if (Wire.requestFrom(I2C_ADDR, 4) == 4) {
        uint8_t ack[4];
        for (int j = 0; j < 4; j++) ack[j] = Wire.read();
        if (ack[0] == MSG_ACK_I2C && ack[1] == pkt[0] && ack[2] == pkt[1]) {
          break;
        }
      }
      delay(2);
    }
  }

  // Ambil antrean ACK Akhir sampai semua count ACK terkirim (atau timeout 3s)
  int forwarded = 0;
  unsigned long start = millis();
  while (forwarded < count && (millis() - start < 3000)) {
    Wire.beginTransmission(I2C_ADDR);
    Wire.write(CMD_POLL_ACK);
    if (Wire.endTransmission() == 0) {
      delayMicroseconds(500);
      if (Wire.requestFrom(I2C_ADDR, 64) == 64) {
        for (int i = 0; i < 64; i += 4) {
          uint8_t sig[4];
          for (int k = 0; k < 4; k++) sig[k] = Wire.read();
          if (sig[0] != 0x00 && sig[0] != 0xFF) {
            Serial.write(sig, 4);
            forwarded++;
          }
        }
      }
    }
    if (Serial.available() > 0) break;
    delay(3);
  }
}
