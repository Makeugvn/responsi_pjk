# Dokumentasi Program ESP32 (`script_esp32/` — Branch `main`)

1. **`esp32_i2c_master/esp32_i2c_master.ino` (ESP32 #1 — Sub-Tim I2C):**
   - `Serial.setRxBufferSize(8192)` pada `500000` baud; `Wire.setBufferSize(256)` pada `400000 Hz` (`SDA=21`, `SCL=22`).
   - Saat `setup()`, mengirim perintah `0x02` (`CMD_CHECK_READY`) setiap `100 ms` + jeda `500 µs` sebelum membaca `4 byte` `READY (0x67)`.
   - Menampung gelombang ke `waveBuffer[4096]`, mengirim tiap paket `1×64 byte` via I2C ke `0x42`, jeda `500 µs`, lalu membaca `4 byte` `ACK I2C (0x10)` dan mencocokkan nomor urutnya.
   - Mengambil antrean ACK Akhir dengan mengirim `0x01` + jeda `500 µs` + membaca `64 byte` (maks 16 sinyal).

2. **`esp32_ble_slave/esp32_ble_slave.ino` (ESP32 #2 — Sub-Tim BLE):**
   - Menggunakan library BLE bawaan ESP32 (`BLEDevice.h`, tanpa install library tambahan).
   - `Wire.setBufferSize(256)` pada `0x42` (`SDA=21`, `SCL=22`, `400000 Hz`).
   - Memanggil `BLEDevice::setMTU(517)`, memvalidasi `CRC-8` paket I2C (`0x10`/`0x11`), memancarkan maks **3 paket (`192 byte`)** per pancaran BLE, dan menunggu `ACK BLE (0x12)` sebelum lanjut ke pancaran berikutnya.
