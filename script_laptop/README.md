# Dokumentasi Program Laptop (`script_laptop/` — Branch `main`)

Folder ini berisi 3 program Python versi stabil (`5/5` uji `img.jpeg` berhasil, memakai library BLE bawaan `BLEDevice.h` di ESP32 #2):

1. `transmitter.py` — **Laptop 1** (Pengirim Gambar via USB UART)
2. `laptop2_ble_http.py` — **Laptop 2** (Jembatan BLE → HTTP)
3. `receiver.py` — **Laptop 3** (Server HTTP & Penyusun Gambar + Live Preview)

---

## 1. `transmitter.py` (Laptop 1 — Sub-Tim UART)
- **Tugas:** Memotong file gambar menjadi paket **64 byte** (`60B` payload + `CRC-8 Maxim`), mengirimkannya per gelombang (**64 paket = 4096 byte**) lewat USB Serial (`500000` baud), dan mengirim ulang paket yang gagal.
- **Detail penting:**
  - Saat membuka port serial (`--port`), mengirim pulsa `RTS` untuk me-reset ESP32 #1, lalu memindai byte demi byte hingga menemukan `[0x67, 0x00, 0x00, 0x00]` (`READY`).
  - Mengirim Paket Pembuka (`0xFFFF`) berisi `4B` ukuran total + nama file, lalu gelombang data (`1 byte header jumlah 1..64` + `N × 64 byte` paket).
  - Menunggu `ACK (0x02)` / `NACK (0x03)` hingga `ACK_TIMEOUT = 15.0` detik; paket yang gagal dikirim ulang otomatis.
- **Jalankan:** `.venv/bin/python script_laptop/transmitter.py samples/img.jpeg --port /dev/ttyUSB1`

---

## 2. `laptop2_ble_http.py` (Laptop 2 — Sub-Tim HTTP)
- **Tugas:** Terhubung ke `ESP32_IMG_BRIDGE` via BLE (`bleak`), langsung membalas `ACK BLE (0x12)` setiap menerima pancaran, mengumpulkan gelombang (`64` paket atau jeda `150 ms`), mengirim `1× HTTP POST /batch` ke Laptop 3, dan menulis balik sinyal `ACK/NACK (0x02/0x03)` ke BLE.
- **Detail penting:**
  - Memakai `asyncio.Lock` (`write_lock`) agar `ACK BLE (0x12)` tidak bertabrakan dengan tulis balik ACK Akhir.
  - Memecah tulis balik ACK per `20 byte` (jika MTU terbaca `23`) dengan jeda `15 ms` antar potongan agar Bluedroid ESP32 #2 tidak kewalahan/putus.
  - Memiliki *auto-reconnect* otomatis jika koneksi BLE terputus.
- **Jalankan:** `.venv/bin/python script_laptop/laptop2_ble_http.py` *(atau tambah `--url http://<IP_LAPTOP_3>:5000/batch`)*.

---

## 3. `receiver.py` (Laptop 3 — Sub-Tim HTTP)
- **Tugas:** Server Flask port `5000` yang menerima `POST /batch`, memvalidasi `CRC-8` tiap paket, menyimpan gambar utuh ke `../received_images/`, dan menampilkan *Live Preview* (OpenCV + web `http://localhost:5000`, di-update maks tiap `0.5 detik`).
- **Partial Save pada HTTP `400`:** Paket yang valid tetap disimpan; nomor urut yang rusak dikembalikan pada JSON `{"status": "batch_error", "failed": [...]}`.
- **Jalankan:** `.venv/bin/python script_laptop/receiver.py`
