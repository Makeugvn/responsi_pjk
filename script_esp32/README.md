# Dokumentasi Program ESP32 (`script_esp32/`)

Folder ini berisi 2 *sketch* Arduino untuk kedua pasang ESP32:

1. `esp32_i2c_master/esp32_i2c_master.ino` — **ESP32 #1A & #1B** (Jembatan USB UART → I2C Master)
2. `esp32_ble_slave/esp32_ble_slave.ino` — **ESP32 #2A & #2B** (Jembatan I2C Slave → NimBLE)

---

## 1. `esp32_i2c_master/esp32_i2c_master.ino` (ESP32 #1 — Sub-Tim I2C)

### Fungsi Utama
Menerima 1 gelombang paket dari Laptop 1 (`transmitter.py`) lewat USB Serial (`500000` baud), menyimpannya sementara ke RAM (`waveBuffer[4096]`), mengirimkannya per `1×64 byte` lewat I2C (`400000 Hz`) ke ESP32 #2, serta mengambil antrean sinyal `ACK`/`NACK`/`READY` pulang ke Laptop 1.

### Konfigurasi Pin & Buffer
- `BAUD_RATE`: `500000` dengan `Serial.setRxBufferSize(8192)` (2× ukuran gelombang `4096 byte`) dipanggil sebelum `Serial.begin(...)`.
- `I2C_ADDR`: `0x42`, `I2C_FREQ`: `400000` Hz, `SDA_PIN`: `21`, `SCL_PIN`: `22` (ubah jika memakai ESP32-S3).
- `Wire.setBufferSize(256)` dipanggil sebelum `Wire.begin(...)` agar paket `64 byte` utuh dalam 1× transaksi.

### Rincian Alur Kerja
- **`setup()` — Cek `READY` (`0x02`):**
  - Mengirim 1 byte `CMD_CHECK_READY` (`0x02`) ke `0x42`, menunggu `500 µs`, lalu membaca `4 byte`. Jika byte pertama `0x67`, sinyal diteruskan ke `Serial.write` dan masuk ke `loop()`.
- **`loop()` — Terima Gelombang & Kirim I2C:**
  - Jika belum ada data USB masuk, memanggil `pollAcksAndForward()` untuk meneruskan sisa sinyal ACK dari ESP32 #2.
  - Saat ada byte masuk: membaca `1 byte count` (`1..64`), lalu `Serial.readBytes(waveBuffer, count * 64)`.
  - Untuk setiap paket `0..count-1`: mengirim `64 byte` ke `0x42`, menunggu `delayMicroseconds(500)` agar ESP32 #2 sempat menyiapkan `Wire.slaveWrite`, lalu membaca `4 byte`. Jika `[0] == 0x10` dan nomor urut `ack[1..2]` cocok dengan `pkt[0..1]`, lanjut ke paket berikutnya; jika tidak, ulangi dari `waveBuffer`.
- **Ambil Antrean ACK Akhir (`0x01`):**
  - Mengirim `CMD_POLL_ACK` (`0x01`), menunggu `500 µs`, membaca `64 byte` (maks 16 sinyal × 4 byte), dan meneruskan slot yang bukan `0x00`/`0xFF` ke `Serial.write`.

---

## 2. `esp32_ble_slave/esp32_ble_slave.ino` (ESP32 #2 — Sub-Tim BLE)

### Fungsi Utama
Bertindak sebagai **I2C Slave (`0x42`)** sekaligus **BLE Server (`NimBLE-Arduino` v2.x)**. Memvalidasi `CRC-8` setiap paket yang masuk dari I2C, memancarkannya lewat BLE sesuai kapasitas MTU (hingga 8 paket = 512 byte), dan menampung balasan ACK/NACK dari Laptop 2.

### Library Wajib
- **`NimBLE-Arduino`** (versi 2.x, instal lewat Arduino IDE *Library Manager*).

### Pengaturan Nama Perangkat (Dual-Lane)
- **ESP32 #2A:** `#define DEVICE_NAME "ESP32_IMG_BRIDGE_A"`
- **ESP32 #2B:** `#define DEVICE_NAME "ESP32_IMG_BRIDGE_B"` (sesuaikan nilai `DEVICE_NAME` di `.ino` sebelum upload ke masing-masing board).
- `SERVICE_UUID`, `TX_CHAR_UUID`, dan `RX_CHAR_UUID` tetap sama di kedua board.

### Rincian Alur Kerja
- **I2C Callback (`onI2CReceive`):**
  - `len == 1`:
    - `0x01`: menyalin hingga 16 sinyal (`64 byte`) dari `ackQueue` (slot kosong `0x00`) lalu memanggil `Wire.slaveWrite(resp, 64)`.
    - `0x02`: membalas `[0x67, 0, 0, 0]` jika `deviceConnected == true` (atau `[0, 0, 0, 0]` jika belum) via `Wire.slaveWrite`.
  - `len == 64`: menghitung `crc8_maxim(pkt, 63)`. Jika cocok dengan `pkt[63]` dan `bleQueue` (kapasitas `128` paket) belum penuh, paket dimasukkan ke `bleQueue` dan membalas `ACK I2C` (`[0x10, seqL, seqH, 0x00]`); jika rusak/penuh membalas `NACK I2C` (`[0x11, seqL, seqH, 0x01]`).
- **BLE Callbacks & `loop()`:**
  - `onConnect`: menyimpan `currentMtu`, lalu meminta interval koneksi cepat `7.5–15 ms` (`updateConnParams(..., 6, 12, 0, 400)`).
  - `onMTUChange`: memperbarui `currentMtu` sesuai negosiasi aktual.
  - `loop()`: menghitung `maxPkts = min(8, (currentMtu - 3) / 64)`, mengambil paket dari `bleQueue`, mengirim lewat `pTxChar->notify()` (mengulang jika `notify()` mengembalikan `false`), lalu menunggu `ackBleReceived` (`0x12`) dengan batas waktu `500 ms`.
  - `onWrite` (`RX_CHAR`): jika byte pertama `0x12`, menandai `ackBleReceived = true`; selain itu memisahkan setiap 4 byte (`0x02`/`0x03`) ke `ackQueue` (kapasitas `256` sinyal).
