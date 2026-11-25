#include <Wire.h>
#include <NimBLEDevice.h>

#define I2C_ADDR 0x42
#define I2C_FREQ 400000
#define SDA_PIN 21
#define SCL_PIN 22

#define PKT_SIZE 64
#define BLE_QUEUE_CAP 128
#define ACK_QUEUE_CAP 256

#define DEVICE_NAME  "ESP32_IMG_BRIDGE_B" // Ganti jadi "ESP32_IMG_BRIDGE_B" untuk ESP32 #2B
#define SERVICE_UUID "12345678-1234-5678-1234-56789abcdef0"
#define TX_CHAR_UUID "12345678-1234-5678-1234-56789abcdef1"
#define RX_CHAR_UUID "12345678-1234-5678-1234-56789abcdef2"

uint8_t bleQueue[BLE_QUEUE_CAP][PKT_SIZE];
volatile int bleHead = 0, bleTail = 0, bleCount = 0;

uint8_t ackQueue[ACK_QUEUE_CAP][4];
volatile int ackHead = 0, ackTail = 0, ackCount = 0;

NimBLECharacteristic *pTxChar = nullptr;
volatile bool deviceConnected = false;
volatile bool ackBleReceived = false;
volatile uint16_t currentMtu = 23;
portMUX_TYPE qMux = portMUX_INITIALIZER_UNLOCKED;

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

void pushAck(const uint8_t *sig) {
  portENTER_CRITICAL(&qMux);
  if (ackCount < ACK_QUEUE_CAP) {
    memcpy(ackQueue[ackTail], sig, 4);
    ackTail = (ackTail + 1) % ACK_QUEUE_CAP;
    ackCount++;
  }
  portEXIT_CRITICAL(&qMux);
}

void onI2CReceive(int len) {
  if (len == 1) {
    uint8_t cmd = Wire.read();
    if (cmd == 0x01) {
      uint8_t resp[64] = {0};
      portENTER_CRITICAL_ISR(&qMux);
      for (int i = 0; i < 16 && ackCount > 0; i++) {
        memcpy(&resp[i * 4], ackQueue[ackHead], 4);
        ackHead = (ackHead + 1) % ACK_QUEUE_CAP;
        ackCount--;
      }
      portEXIT_CRITICAL_ISR(&qMux);
      Wire.slaveWrite(resp, 64);
    } else if (cmd == 0x02) {
      uint8_t readyResp[4] = {(uint8_t)(deviceConnected ? 0x67 : 0x00), 0x00, 0x00, 0x00};
      Wire.slaveWrite(readyResp, 4);
    }
    return;
  }

  if (len == PKT_SIZE) {
    uint8_t pkt[PKT_SIZE];
    for (int i = 0; i < PKT_SIZE; i++) pkt[i] = Wire.read();

    uint8_t ack[4] = {0x11, pkt[0], pkt[1], 0x01};
    if (crc8_maxim(pkt, 63) == pkt[63]) {
      portENTER_CRITICAL_ISR(&qMux);
      if (bleCount < BLE_QUEUE_CAP) {
        memcpy(bleQueue[bleTail], pkt, PKT_SIZE);
        bleTail = (bleTail + 1) % BLE_QUEUE_CAP;
        bleCount++;
        ack[0] = 0x10;
        ack[3] = 0x00;
      }
      portEXIT_CRITICAL_ISR(&qMux);
    }
    Wire.slaveWrite(ack, 4);
    return;
  }

  while (Wire.available()) Wire.read();
}

class ServerCallbacks : public NimBLEServerCallbacks {
  void onConnect(NimBLEServer *pServer, NimBLEConnInfo &connInfo) override {
    deviceConnected = true;
    currentMtu = connInfo.getMTU();
    // Minta interval koneksi tercepat 7.5-15ms
    pServer->updateConnParams(connInfo.getConnHandle(), 6, 12, 0, 400);
    Serial.printf("BLE Laptop 2 terhubung (MTU=%u)!\n", currentMtu);
  }
  void onMTUChange(uint16_t MTU, NimBLEConnInfo &connInfo) override {
    currentMtu = MTU;
    Serial.printf("MTU diperbarui: %u\n", currentMtu);
  }
  void onDisconnect(NimBLEServer *pServer, NimBLEConnInfo &connInfo, int reason) override {
    deviceConnected = false;
    currentMtu = 23;
    Serial.println("BLE terputus, advertising lagi...");
    NimBLEDevice::startAdvertising();
  }
};

class RxCallbacks : public NimBLECharacteristicCallbacks {
  void onWrite(NimBLECharacteristic *pChar, NimBLEConnInfo &connInfo) override {
    std::string val = pChar->getValue();
    size_t len = val.length();
    if (len >= 4 && (uint8_t)val[0] == 0x12) {
      ackBleReceived = true;
      return;
    }
    for (size_t i = 0; i + 4 <= len; i += 4) {
      pushAck((const uint8_t *)val.data() + i);
    }
  }
};

void setup() {
  Serial.begin(115200);
  Wire.setBufferSize(256);
  Wire.onReceive(onI2CReceive);
  Wire.begin((uint8_t)I2C_ADDR, SDA_PIN, SCL_PIN, I2C_FREQ);

  NimBLEDevice::init(DEVICE_NAME);
  NimBLEDevice::setMTU(517);

  NimBLEServer *pServer = NimBLEDevice::createServer();
  pServer->setCallbacks(new ServerCallbacks());
  NimBLEService *pService = pServer->createService(SERVICE_UUID);

  pTxChar = pService->createCharacteristic(TX_CHAR_UUID, NIMBLE_PROPERTY::NOTIFY);
  NimBLECharacteristic *pRxChar = pService->createCharacteristic(
    RX_CHAR_UUID,
    NIMBLE_PROPERTY::WRITE | NIMBLE_PROPERTY::WRITE_NR
  );
  pRxChar->setCallbacks(new RxCallbacks());

  pService->start();
  NimBLEAdvertising *pAdv = NimBLEDevice::getAdvertising();
  pAdv->addServiceUUID(SERVICE_UUID);
  pAdv->setName(DEVICE_NAME);
  pAdv->enableScanResponse(true);
  pAdv->start();
  Serial.println("ESP32 #2 NimBLE+I2C siap!");
}

void loop() {
  if (!deviceConnected) {
    delay(50);
    return;
  }

  int maxPkts = (currentMtu >= 67) ? ((currentMtu - 3) / PKT_SIZE) : 1;
  if (maxPkts > 8) maxPkts = 8;

  int sendPkts = 0;
  uint8_t txBuf[8 * PKT_SIZE];
  portENTER_CRITICAL(&qMux);
  while (sendPkts < maxPkts && bleCount > 0) {
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

  ackBleReceived = false;
  pTxChar->setValue(txBuf, sendPkts * PKT_SIZE);
  while (deviceConnected && !pTxChar->notify()) {
    delay(2);
  }

  unsigned long start = millis();
  while (!ackBleReceived && deviceConnected && (millis() - start < 500)) {
    delay(1);
  }
}
