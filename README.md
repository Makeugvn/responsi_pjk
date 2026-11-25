# Proyek Pengiriman Gambar Estafet (UART → I2C → BLE → HTTP) — `fast-ble`

Branch `fast-ble` adalah versi **1 jalur cepat** menggunakan **`NimBLE-Arduino`** (hingga 8 paket = `512 byte` per pancaran BLE, interval koneksi `7.5–15 ms`, `BATCH_WAIT_SEC = 40 ms`).

## Struktur Folder
- `script_laptop/` — `transmitter.py`, `laptop2_ble_http.py`, `receiver.py` (+ `README.md`)
- `script_esp32/` — `esp32_i2c_master/`, `esp32_ble_slave/` (+ `README.md`)
- `samples/` — `img.jpg`, `img.jpeg`
- `received_images/` — hasil gambar yang diterima `receiver.py`
- `AGENTS.md` — standar protokol proyek

## Cara Menjalankan
1. `.venv/bin/python script_laptop/receiver.py`
2. `.venv/bin/python script_laptop/laptop2_ble_http.py` *(atau tambah `--url http://<IP_LAPTOP_3>:5000/batch`)*
3. `.venv/bin/python script_laptop/transmitter.py samples/img.jpeg --port /dev/ttyUSB1`
