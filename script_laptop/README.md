# Dokumentasi Program Laptop (`script_laptop/`)

Folder ini berisi 3 program Python yang berjalan di sisi laptop:

1. `transmitter.py` — **Laptop 1** (Pengirim Gambar via USB UART)
2. `laptop2_ble_http.py` — **Laptop 2** (Jembatan BLE → HTTP)
3. `receiver.py` — **Laptop 3** (Server HTTP & Penyusun Gambar + Live Preview)

---

## 1. `transmitter.py` (Laptop 1 — Sub-Tim UART)

### Fungsi Utama
Memotong file gambar menjadi paket berukuran tetap **64 byte**, mengirimkannya secara paralel lewat **1 atau 2 port USB Serial** (`500000` baud) ke ESP32 #1 (`esp32_i2c_master.ino`), menunggu sinyal `ACK`/`NACK`, dan mengirim ulang paket yang gagal.

### Rincian Alur Kerja & Fungsi
- **`crc8_maxim(data)`:** Menghitung nilai `CRC-8 Maxim` (polinomial `0x8C` reflected) dari 63 byte pertama amplop paket, lalu menaruh hasilnya pada byte ke-63.
- **`make_frame(sequence, payload)`:** Membungkus data ke amplop `64 byte`:
  - `frame[0:2]`: `sequence` (`uint16` little-endian, `0xFFFF` untuk Paket Pembuka).
  - `frame[2]`: `len(payload)` (`1..60` byte).
  - `frame[3:3+len]`: isi data (atau `4B ukuran + nama file` untuk `0xFFFF`).
  - `frame[63]`: `CRC-8 Maxim`.
- **Inisialisasi & `wait_ready(ser)`:**
  - Membuka setiap port pada `--ports` (`500000` baud), mengirim pulsa `RTS` (`dtr=False, rts=True -> 0.1s -> rts=False`) agar ESP32 #1 reset bersih, lalu memindai byte demi byte hingga menemukan pola `[0x67, 0x00, 0x00, 0x00]` (`READY`) sehingga teks boot ROM ESP32 terabaikan.
- **`send_wave(ser, frames, sequences)`:**
  - Mengirim `1 byte header jumlah (1..64)` diikuti `N × 64 byte` paket.
  - Membaca sinyal balasan 4 byte (`0x02` = `ACK`, `0x03` = `NACK`) hingga seluruh paket gelombang mendapat `ACK` atau melewati `ACK_TIMEOUT = 15.0` detik. Paket yang belum mendapat `ACK` otomatis dikirim ulang.
- **Mode Dual-Lane (Multi-Thread):**
  - Paket Pembuka (`0xFFFF`) dikirim lewat port pertama (`sers[0]`) hingga mendapat `ACK`.
  - Seluruh gelombang data dimasukkan ke `queue.Queue()`, lalu dikerjakan paralel oleh `threading.Thread` (1 thread per port USB).

### Cara Menjalankan
- **1 jalur:** `.venv/bin/python script_laptop/transmitter.py samples/img.jpeg --ports /dev/ttyUSB1`
- **2 jalur paralel:** `.venv/bin/python script_laptop/transmitter.py samples/image.jpeg --ports /dev/ttyUSB0 /dev/ttyUSB1`

---

## 2. `laptop2_ble_http.py` (Laptop 2 — Sub-Tim HTTP)

### Fungsi Utama
Menjembatani komunikasi nirkabel **BLE (`ESP32 #2`) ↔ Wi-Fi HTTP (`Laptop 3`)**. Mendukung **1 atau 2 ESP32 #2** secara paralel menggunakan `asyncio` dan `bleak`.

### Rincian Alur Kerja & Fungsi
- **`discover_targets()`:**
  - Jika `--devices` tidak diisi, program memindai perangkat BLE selama 5 detik dan mengambil semua perangkat yang cocok dengan nama `ESP32_IMG_BRIDGE_A`/`_B` **atau** memancarkan `SERVICE_UUID` (`12345678-1234-5678-1234-56789abcdef0`).
- **`run_worker(device_name, url, scan_lock)`:**
  - Berjalan dalam *loop* `while True` dengan *auto-reconnect* jika koneksi BLE sempat terputus.
  - **`on_notify`:** Begitu menerima pancaran BLE (maks 8 paket = 512 byte), langsung membalas `ACK BLE` (`[0x12, 0x00, 0x00, 0x00]`) dengan `asyncio.Lock` (`write_lock`) *sebelum* memproses HTTP, lalu memasukkan potongan `64 byte` ke `pkt_queue`.
  - **Pengumpulan Batch & `post_batch_sync`:** Mengumpulkan paket hingga `BATCH_MAX = 64` atau jeda `BATCH_WAIT_SEC = 0.04` (`40 ms`), lalu mengirimkannya dalam `1× HTTP POST /batch` (`application/octet-stream`) ke `receiver.py`.
  - **Pembuatan Sinyal ACK/NACK:** Membaca status HTTP (`200` atau `400` dengan `failed: [...]`), menyusun deretan sinyal 4 byte (`0x02` atau `0x03`) per paket, dan mengirimkannya balik ke `RX_CHAR_UUID` (`max_write` disesuaikan MTU, berjeda `5 ms` antar potongan jika dipecah).

### Cara Menjalankan
- **1 Laptop (testing lokal):** `.venv/bin/python script_laptop/laptop2_ble_http.py`
- **3 Laptop terpisah:** `.venv/bin/python script_laptop/laptop2_ble_http.py --url http://<IP_LAPTOP_3>:5000/batch`
- **Pilih perangkat manual:** tambahkan `--devices ESP32_IMG_BRIDGE_A ESP32_IMG_BRIDGE_B` (atau alamat MAC).

---

## 3. `receiver.py` (Laptop 3 — Sub-Tim HTTP)

### Fungsi Utama
Server Flask di port `5000` yang menerima batch paket 64 byte, memvalidasi `CRC-8`, menyusun kembali file gambar utuh ke `../received_images/`, dan menampilkan **Live Preview** progres gambar secara real-time.

### Rincian Alur Kerja & Endpoint
- **`process_frame(frame)`:**
  - Mengecek `CRC-8 Maxim` pada `frame[:63]` vs `frame[63]`. Jika tidak cocok, mengembalikan status `400`.
  - Jika `sequence == 0xFFFF`, menginisialisasi transfer baru (`total_size`, `filename` yang disanitasi, dan `packet_count`).
  - Memvalidasi panjang payload paket data dan menyimpannya ke `transfer["packets"][sequence]` (aman untuk duplikat/retry maupun kedatangan *out-of-order* dari 2 jalur).
  - Saat `len(packets) == packet_count`, seluruh payload disatukan berurutan `0..packet_count-1` dan ditulis ke `received_images/<filename>`.
- **`POST /batch`:** Memproses seluruh paket dalam 1 batch. Paket yang valid tetap disimpan; nomor urut yang gagal dikumpulkan di list `failed` dan dikembalikan dengan status `400` (`{"status": "batch_error", "failed": [...]}`).
- **Live Preview (`/` web & jendela OpenCV):**
  - `update_live_preview()` mendekode potongan JPEG yang sudah masuk (`cv2.imdecode`) dan menutup bagian bawah yang belum tiba sesuai persentase paket, dibatasi maksimal 1× setiap `0.5` detik agar tidak memperlambat HTTP.
  - Dapat dipantau lewat jendela popup OpenCV **`Live Receiver Preview`** maupun browser di `http://localhost:5000` (`/status` & `/preview`).

### Cara Menjalankan
```bash
.venv/bin/python script_laptop/receiver.py
```
