/*
  ESP32 #2  --  Jembatan I2C (slave) -> BLE (server)

  Alur:
    Laptop 1 --UART--> ESP32 #1 --I2C (1x64 byte)--> ESP32 #2 --BLE (maks 3x64 byte)--> Laptop 2

  Format paket 64 byte (dibuat transmitter.py):
    [0..1] seq (little endian)   [2] panjang payload   [3..62] payload   [63] CRC8-MAXIM(byte 0..62)
    seq 0xFFFF = paket pembuka (ukuran file + nama file)

  Sinyal 4 byte  [tipe, seqLo, seqHi, err]:
    0x67 READY            ESP32 #2 -> ESP32 #1 -> Laptop 1  (BLE sudah siap, MTU sudah dinegosiasi)
    0x10 / 0x11  ACK/NACK I2C       ESP32 #2 -> ESP32 #1    (paket diterima / CRC salah atau antrean penuh)
    0x12         ACK BLE            Laptop 2 -> ESP32 #2    (3 paket hasil notify sudah diterima)
    0x02 / 0x03  ACK/NACK Akhir     Laptop 2 -> ESP32 #2 -> (poll I2C) -> ESP32 #1 -> Laptop 1

  Gambar TIDAK disimpan di firmware ini. Gambar (jpeg/png/dll) dipecah jadi frame oleh transmitter.py,
  dikirim ESP32 #1 lewat I2C satu paket per transaksi, lalu diteruskan ke BLE 3 paket per notify.
*/

#include <Arduino.h>
#include <Wire.h>
#include <BLEDevice.h>
#include <BLEServer.h>
#include <BLEUtils.h>
#include <BLE2902.h>

// ---------------- I2C slave ----------------
#define I2C_ADDR 0x42
#define I2C_FREQ 400000
#define SDA_PIN 21
#define SCL_PIN 22

#define CMD_POLL_ACK 0x01      // master minta 64 byte (16 sinyal x 4 byte)
#define CMD_CHECK_READY 0x02   // master minta 4 byte status READY

// ---------------- Kode sinyal ----------------
#define MSG_ACK_FINAL 0x02
#define MSG_NACK_FINAL 0x03
#define MSG_ACK_I2C 0x10
#define MSG_NACK_I2C 0x11
#define MSG_ACK_BLE 0x12
#define MSG_READY 0x67

#define ERR_NONE 0x00
#define ERR_I2C_CRC_OR_FULL 0x01
#define ERR_BLE_NO_ACK 0x02

// ---------------- Ukuran & antrean ----------------
#define PKT_SIZE 64
#define PKTS_PER_NOTIFY 3
#define BLE_QUEUE_CAP 128
#define ACK_QUEUE_CAP 256
#define ACKS_PER_POLL 16

#define MTU_REQUEST 517
#define MTU_NEEDED (PKTS_PER_NOTIFY * PKT_SIZE + 3)   // 192 + 3 byte header ATT = 195
#define BLE_ACK_TIMEOUT_MS 1000
#define BLE_RETRY 3
#define READY_FALLBACK_MS 3000

// ---------------- BLE ----------------
#define DEVICE_NAME  "ESP32_IMG_BRIDGE"
#define SERVICE_UUID "12345678-1234-5678-1234-56789abcdef0"
#define TX_CHAR_UUID "12345678-1234-5678-1234-56789abcdef1"   // ESP32 #2 -> Laptop 2 (notify)
#define RX_CHAR_UUID "12345678-1234-5678-1234-56789abcdef2"   // Laptop 2 -> ESP32 #2 (write)

BLEServer *pServer = nullptr;
BLECharacteristic *pTxChar = nullptr;

volatile bool deviceConnected = false;
volatile bool bleReady = false;
volatile bool ackBleReceived = false;
volatile uint16_t negotiatedMtu = 23;
unsigned long connectTime = 0;

// Antrean paket (I2C -> BLE) dan antrean sinyal balasan (BLE -> I2C poll)
uint8_t bleQueue[BLE_QUEUE_CAP][PKT_SIZE];
volatile int bleHead = 0, bleTail = 0, bleCount = 0;

uint8_t ackQueue[ACK_QUEUE_CAP][4];
volatile int ackHead = 0, ackTail = 0, ackCount = 0;

portMUX_TYPE qMux = portMUX_INITIALIZER_UNLOCKED;

// Statistik untuk debug (dicetak di loop, bukan di callback)
volatile uint32_t statI2cOk = 0, statI2cBad = 0, statBleSent = 0, statBleFail = 0;

// =====================================================
uint8_t crc8_maxim(const uint8_t *data, size_t len) {
  uint8_t crc = 0;
  for (size_t i = 0; i < len; i++) {
    uint8_t cur = data[i];
    for (int b = 0; b < 8; b++) {
      uint8_t mix = (crc ^ cur) & 1;
      crc >>= 1;
      if (mix) crc ^= 0x8C;
      cur >>= 1;
    }
  }
  return crc;
}

// Dipakai dari task BLE, task I2C, maupun loop() -> pakai versi SAFE
void pushAck(const uint8_t *sig) {
  portENTER_CRITICAL_SAFE(&qMux);
  if (ackCount < ACK_QUEUE_CAP) {
    memcpy(ackQueue[ackTail], sig, 4);
    ackTail = (ackTail + 1) % ACK_QUEUE_CAP;
    ackCount++;
  }
  portEXIT_CRITICAL_SAFE(&qMux);
}

// Ambil sampai maxN sinyal, tulis berurutan ke out. Return jumlah yang diambil.
int popAcks(uint8_t *out, int maxN) {
  int n = 0;
  portENTER_CRITICAL_SAFE(&qMux);
  while (n < maxN && ackCount > 0) {
    memcpy(out + n * 4, ackQueue[ackHead], 4);
    ackHead = (ackHead + 1) % ACK_QUEUE_CAP;
    ackCount--;
    n++;
  }
  portEXIT_CRITICAL_SAFE(&qMux);
  return n;
}

void clearQueues() {
  portENTER_CRITICAL_SAFE(&qMux);
  bleHead = bleTail = bleCount = 0;
  ackHead = ackTail = ackCount = 0;
  portEXIT_CRITICAL_SAFE(&qMux);
}

// =====================================================
//  I2C slave: dipanggil saat ESP32 #1 selesai menulis ke kita.
//  Jawaban untuk read berikutnya di-preload dengan Wire.slaveWrite().
//  JANGAN pakai Serial.print di sini (waktunya mepet: master hanya menunggu ~500 us).
// =====================================================
void onI2CReceive(int len) {
  // --- 1 paket utuh 64 byte dari ESP32 #1 ---
  if (len == PKT_SIZE) {
    uint8_t pkt[PKT_SIZE];
    for (int i = 0; i < PKT_SIZE; i++) pkt[i] = Wire.read();

    uint8_t ack[4] = {MSG_NACK_I2C, pkt[0], pkt[1], ERR_I2C_CRC_OR_FULL};
    if (crc8_maxim(pkt, 63) == pkt[63]) {
      portENTER_CRITICAL_SAFE(&qMux);
      if (bleCount < BLE_QUEUE_CAP) {
        memcpy(bleQueue[bleTail], pkt, PKT_SIZE);
        bleTail = (bleTail + 1) % BLE_QUEUE_CAP;
        bleCount++;
        ack[0] = MSG_ACK_I2C;
        ack[3] = ERR_NONE;
      }
      portEXIT_CRITICAL_SAFE(&qMux);
    }
    if (ack[0] == MSG_ACK_I2C) statI2cOk++; else statI2cBad++;
    Wire.slaveWrite(ack, 4);
    return;
  }

  // --- perintah 1 byte dari ESP32 #1 ---
  if (len == 1) {
    uint8_t cmd = Wire.read();

    if (cmd == CMD_CHECK_READY) {
      uint8_t r[4] = {0, 0, 0, 0};
      if (bleReady) r[0] = MSG_READY;          // [0x67, 0, 0, 0]
      Wire.slaveWrite(r, 4);
    } else if (cmd == CMD_POLL_ACK) {
      uint8_t out[ACKS_PER_POLL * 4];
      memset(out, 0, sizeof(out));             // sisa slot = 0x00 (diabaikan master)
      popAcks(out, ACKS_PER_POLL);
      Wire.slaveWrite(out, sizeof(out));
    }
    return;
  }

  while (Wire.available()) Wire.read();
}

// =====================================================
//  BLE callbacks
// =====================================================
class ServerCallbacks : public BLEServerCallbacks {
  void onConnect(BLEServer *s) override {
    deviceConnected = true;
    bleReady = false;
    negotiatedMtu = 23;
    connectTime = millis();
    Serial.println("BLE Laptop 2 terhubung! Menunggu negosiasi MTU...");
  }

  void onDisconnect(BLEServer *s) override {
    deviceConnected = false;
    bleReady = false;
    clearQueues();
    Serial.println("BLE terputus, advertising lagi...");
    s->startAdvertising();
  }

  // Tanpa "override" supaya tetap bisa dikompilasi walau versi core tidak mendeklarasikannya
  void onMtuChanged(BLEServer *s, esp_ble_gatts_cb_param_t *param) {
    negotiatedMtu = param->mtu.mtu;
    if (negotiatedMtu >= MTU_NEEDED) {
      bleReady = true;     // -> READY (0x67) akan dijawab ke ESP32 #1 lewat I2C
    }
    Serial.printf("MTU = %u %s\n", negotiatedMtu, bleReady ? "(READY)" : "(terlalu kecil!)");
  }
};

class RxCallbacks : public BLECharacteristicCallbacks {
  void onWrite(BLECharacteristic *pChar) override {
    auto val = pChar->getValue();
    size_t len = val.length();
    const uint8_t *d = (const uint8_t *)val.c_str();

    // Isi bisa 1 sinyal atau gabungan beberapa sinyal 4 byte
    for (size_t i = 0; i + 4 <= len; i += 4) {
      if (d[i] == MSG_ACK_BLE) {
        ackBleReceived = true;            // ACK BLE 0x12 untuk notify terakhir
      } else if (d[i] == MSG_ACK_FINAL || d[i] == MSG_NACK_FINAL) {
        pushAck(d + i);                   // ACK/NACK Akhir -> diteruskan ke Laptop 1
      }
    }
  }
};

// =====================================================
//  Kirim n paket (maks 3) lewat satu notify, tunggu ACK BLE 0x12
// =====================================================
bool sendOverBle(const uint8_t *buf, int n) {
  for (int attempt = 0; attempt < BLE_RETRY; attempt++) {
    if (!deviceConnected) return false;

    ackBleReceived = false;
    pTxChar->setValue((uint8_t *)buf, n * PKT_SIZE);
    pTxChar->notify();

    unsigned long start = millis();
    while (!ackBleReceived && deviceConnected && (millis() - start < BLE_ACK_TIMEOUT_MS)) {
      delay(1);
    }
    if (ackBleReceived) return true;
  }
  return false;
}

// =====================================================
void setup() {
  Serial.begin(115200);
  Serial.println();
  Serial.println("=== ESP32 #2 : I2C slave -> BLE server ===");

  // I2C slave
  Wire.setBufferSize(256);                       // harus sebelum begin(), muat 64 byte + sisa
  Wire.onReceive(onI2CReceive);
  Wire.begin((uint8_t)I2C_ADDR, SDA_PIN, SCL_PIN, I2C_FREQ);

  // BLE
  BLEDevice::init(DEVICE_NAME);
  BLEDevice::setMTU(MTU_REQUEST);                // supaya notify 3x64 = 192 byte muat

  pServer = BLEDevice::createServer();
  pServer->setCallbacks(new ServerCallbacks());

  BLEService *pService = pServer->createService(SERVICE_UUID);

  pTxChar = pService->createCharacteristic(
      TX_CHAR_UUID,
      BLECharacteristic::PROPERTY_READ | BLECharacteristic::PROPERTY_NOTIFY);
  pTxChar->addDescriptor(new BLE2902());

  BLECharacteristic *pRxChar = pService->createCharacteristic(
      RX_CHAR_UUID,
      BLECharacteristic::PROPERTY_WRITE | BLECharacteristic::PROPERTY_WRITE_NR);
  pRxChar->setCallbacks(new RxCallbacks());

  pService->start();

  BLEAdvertising *pAdv = BLEDevice::getAdvertising();
  pAdv->addServiceUUID(SERVICE_UUID);
  pAdv->setScanResponse(true);
  BLEDevice::startAdvertising();

  Serial.println("Advertising sebagai ESP32_IMG_BRIDGE, menunggu Laptop 2...");
}

void loop() {
  // Cadangan: kalau event MTU tidak pernah datang, tetap READY setelah beberapa detik
  if (deviceConnected && !bleReady && (millis() - connectTime > READY_FALLBACK_MS)) {
    bleReady = true;
    Serial.printf("PERINGATAN: event MTU tidak datang, lanjut READY (MTU=%u)\n", negotiatedMtu);
  }

  // Log status ringan tiap 1 detik jika ada perubahan
  static unsigned long lastLog = 0;
  static uint32_t lastSum = 0;
  if (millis() - lastLog > 1000) {
    lastLog = millis();
    uint32_t sum = statI2cOk + statI2cBad + statBleSent + statBleFail;
    if (sum != lastSum) {
      lastSum = sum;
      Serial.printf("I2C ok=%u bad=%u | BLE terkirim=%u gagal=%u | antrean=%d\n",
                    statI2cOk, statI2cBad, statBleSent, statBleFail, bleCount);
    }
  }

  if (!deviceConnected || !bleReady) {
    delay(10);
    return;
  }

  // Ambil sampai 3 paket dari antrean
  int sendPkts = 0;
  uint8_t txBuf[PKTS_PER_NOTIFY * PKT_SIZE];
  portENTER_CRITICAL(&qMux);
  while (sendPkts < PKTS_PER_NOTIFY && bleCount > 0) {
    memcpy(&txBuf[sendPkts * PKT_SIZE], bleQueue[bleHead], PKT_SIZE);
    bleHead = (bleHead + 1) % BLE_QUEUE_CAP;
    bleCount--;
    sendPkts++;
  }
  portEXIT_CRITICAL(&qMux);

  if (sendPkts == 0) {
    delay(1);
    return;
  }

  if (sendOverBle(txBuf, sendPkts)) {
    statBleSent += sendPkts;
  } else {
    // Laptop 2 tidak membalas ACK BLE -> beri tahu Laptop 1 supaya kirim ulang paket ini
    statBleFail += sendPkts;
    for (int i = 0; i < sendPkts; i++) {
      uint8_t nack[4] = {MSG_NACK_FINAL, txBuf[i * PKT_SIZE], txBuf[i * PKT_SIZE + 1], ERR_BLE_NO_ACK};
      pushAck(nack);
    }
  }
}