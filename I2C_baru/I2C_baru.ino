#include <Wire.h>

// === PARAMETER WAJIB SESUAI AGENTS.MD ===
#define I2C_SLAVE_ADDR   0x42
#define I2C_SDA          21
#define I2C_SCL          22
#define I2C_FREQ         400000
#define UART_BAUD        500000

#define PACKET_SIZE      64
#define MAX_WAVE_PKTS    64
#define WAVE_BUF_SIZE    (MAX_WAVE_PKTS * PACKET_SIZE) // 4096 byte
#define I2C_DELAY_US     500   // Jeda wajib 500 us sebelum Wire.requestFrom

// Perintah I2C (ESP32 #1 -> ESP32 #2)
#define CMD_POLL_ACK     0x01  // Ambil antrean ACK Akhir (maks 16 sinyal = 64 byte)
#define CMD_CHECK_READY  0x02  // Tanya apakah BLE ESP32 #2 sudah tersambung

// Jenis Pesan Sinyal Balasan (4 Byte)
#define MSG_READY        0x67
#define MSG_ACK_FINAL    0x02
#define MSG_NACK_FINAL   0x03
#define MSG_ACK_I2C      0x10
#define MSG_NACK_I2C     0x11

uint8_t waveBuffer[WAVE_BUF_SIZE];
bool systemReady = false;
unsigned long lastPollMs = 0;

// Baca tepat N byte dari Laptop 1 (UART) ke waveBuffer
bool readSerialExact(uint8_t *buf, size_t len, unsigned long timeoutMs) {
  size_t received = 0;
  unsigned long lastByteTime = millis();

  while (received < len) {
    if (Serial.available() > 0) {
      buf[received++] = Serial.read();
      lastByteTime = millis();
    } else if (millis() - lastByteTime > timeoutMs) {
      return false;
    }
  }
  return true;
}

// Kirim 1 paket utuh (64 byte) ke ESP32 #2 (I2C) + verifikasi ACK I2C & nomor urut
bool sendPacketI2C(const uint8_t *pkt) {
  const int MAX_RETRY = 15;

  for (int attempt = 0; attempt < MAX_RETRY; attempt++) {
    Wire.beginTransmission(I2C_SLAVE_ADDR);
    Wire.write(pkt, PACKET_SIZE);
    uint8_t txErr = Wire.endTransmission();

    if (txErr == 0) {
      delayMicroseconds(I2C_DELAY_US);

      size_t got = Wire.requestFrom((uint8_t)I2C_SLAVE_ADDR, (size_t)4);
      if (got == 4) {
        uint8_t ack[4];
        for (int i = 0; i < 4; i++) {
          ack[i] = Wire.read();
        }

        // Cocokkan status ACK I2C (0x10) DAN nomor urut 2 byte
        if (ack[0] == MSG_ACK_I2C && ack[1] == pkt[0] && ack[2] == pkt[1]) {
          return true;
        }
      } else {
        while (Wire.available()) Wire.read();
      }
    }

    // Beri jeda 2 ms jika gagal/NACK agar ESP32 #2 sempat bernapas saat sibuk BLE
    delay(2);
  }

  return false;
}

// Jemput antrean ACK/NACK Akhir dari ESP32 #2 (0x01) -> teruskan ke Laptop 1 (UART)
void pollAckQueue() {
  for (int round = 0; round < 8; round++) {
    Wire.beginTransmission(I2C_SLAVE_ADDR);
    Wire.write((uint8_t)CMD_POLL_ACK);
    if (Wire.endTransmission() != 0) {
      break;
    }

    delayMicroseconds(I2C_DELAY_US);

    size_t got = Wire.requestFrom((uint8_t)I2C_SLAVE_ADDR, (size_t)64);
    if (got != 64) {
      while (Wire.available()) Wire.read();
      break;
    }

    uint8_t buf[64];
    for (int i = 0; i < 64; i++) {
      buf[i] = Wire.read();
    }

    int validCount = 0;
    for (int i = 0; i < 16; i++) {
      uint8_t *sig = &buf[i * 4];
      // Hanya teruskan ACK Akhir (0x02) atau NACK Akhir (0x03); buang slot 0x00 / noise 0xFF
      if (sig[0] == MSG_ACK_FINAL || sig[0] == MSG_NACK_FINAL) {
        Serial.write(sig, 4);
        validCount++;
      }
    }

    if (validCount < 16) {
      break;
    }
  }
}

// Tahap 0: Aktif tanya kesiapan BLE ke ESP32 #2 (0x02)
void checkReadyLoop() {
  Wire.beginTransmission(I2C_SLAVE_ADDR);
  Wire.write((uint8_t)CMD_CHECK_READY);
  if (Wire.endTransmission() == 0) {
    delayMicroseconds(I2C_DELAY_US);

    if (Wire.requestFrom((uint8_t)I2C_SLAVE_ADDR, (size_t)4) == 4) {
      uint8_t resp[4];
      for (int i = 0; i < 4; i++) {
        resp[i] = Wire.read();
      }
      if (resp[0] == MSG_READY) {
        Serial.write(resp, 4); // Teruskan [0x67, 0x00, 0x00, 0x00] ke Laptop 1
        systemReady = true;
        return;
      }
    } else {
      while (Wire.available()) Wire.read();
    }
  }
  delay(100);
}

void setup() {
  // Urutan wajib: perbesar buffer sebelum begin()
  Serial.setRxBufferSize(8192);
  Serial.begin(UART_BAUD);

  Wire.setBufferSize(256);
  Wire.begin(I2C_SDA, I2C_SCL);
  Wire.setClock(I2C_FREQ);
}

void loop() {
  if (!systemReady) {
    checkReadyLoop();
    return;
  }

  if (Serial.available() >= 1) {
    uint8_t numPackets = Serial.read();

    if (numPackets >= 1 && numPackets <= MAX_WAVE_PKTS) {
      size_t totalBytes = (size_t)numPackets * PACKET_SIZE;

      if (readSerialExact(waveBuffer, totalBytes, 2000)) {
        for (uint8_t i = 0; i < numPackets; i++) {
          sendPacketI2C(&waveBuffer[i * PACKET_SIZE]);
        }
      }
    }

    pollAckQueue();
  }

  if (millis() - lastPollMs >= 5) {
    lastPollMs = millis();
    pollAckQueue();
  }
}