#include <BLEDevice.h>
#include <BLEServer.h>
#include <BLEUtils.h>
#include <BLE2902.h>

#define DEVICE_NAME  "ESP32_IMG_BRIDGE"
#define SERVICE_UUID "12345678-1234-5678-1234-56789abcdef0"
#define TX_CHAR_UUID "12345678-1234-5678-1234-56789abcdef1" // ESP32 -> Laptop (Notify)
#define RX_CHAR_UUID "12345678-1234-5678-1234-56789abcdef2" // Laptop -> ESP32 (Write)

BLECharacteristic *pTxChar = nullptr;
bool deviceConnected = false;
volatile bool ackBleReceived = false;

class ServerCallbacks : public BLEServerCallbacks {
  void onConnect(BLEServer *pServer) override {
    deviceConnected = true;
    Serial.println("Laptop terhubung!");
  }
  void onDisconnect(BLEServer *pServer) override {
    deviceConnected = false;
    Serial.println("Laptop terputus, mulai advertising lagi...");
    pServer->startAdvertising();
  }
};

class RxCallbacks : public BLECharacteristicCallbacks {
  void onWrite(BLECharacteristic *pChar) override {
    String val = pChar->getValue();
    if (val.length() >= 4 && (uint8_t)val[0] == 0x12) {
      ackBleReceived = true;
      Serial.println("ACK BLE (0x12) diterima dari Laptop!");
    }
  }
};

void setup() {
  Serial.begin(115200);
  BLEDevice::init(DEVICE_NAME);
  BLEDevice::setMTU(517); // Wajib agar muat > 20 byte (target 192 byte)

  BLEServer *pServer = BLEDevice::createServer();
  pServer->setCallbacks(new ServerCallbacks());

  BLEService *pService = pServer->createService(SERVICE_UUID);

  pTxChar = pService->createCharacteristic(
    TX_CHAR_UUID,
    BLECharacteristic::PROPERTY_NOTIFY
  );
  pTxChar->addDescriptor(new BLE2902());

  BLECharacteristic *pRxChar = pService->createCharacteristic(
    RX_CHAR_UUID,
    BLECharacteristic::PROPERTY_WRITE | BLECharacteristic::PROPERTY_WRITE_NR
  );
  pRxChar->setCallbacks(new RxCallbacks());

  pService->start();
  pServer->getAdvertising()->start();
  Serial.println("BLE siap, menunggu koneksi Laptop...");
}

void loop() {
  if (!deviceConnected) {
    delay(200);
    return;
  }

  // Contoh kirim 3 paket = 192 byte
  uint8_t dummyData[192];
  static uint8_t counter = 0;
  for (int i = 0; i < 192; i++) {
    dummyData[i] = counter;
  }

  ackBleReceived = false;
  pTxChar->setValue(dummyData, sizeof(dummyData));
  pTxChar->notify();
  Serial.printf("Kirim 192 byte (counter=%u), menunggu ACK BLE...\n", counter);

  // Tunggu ACK BLE (0x12) sebelum kirim berikutnya (timeout 2 detik)
  unsigned long start = millis();
  while (!ackBleReceived && deviceConnected && (millis() - start < 2000)) {
    delay(1);
  }

  counter++;
  delay(1000); // Jeda 1 detik agar mudah diamati di Serial Monitor
}
