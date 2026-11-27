
import argparse
import os
import random
import sys
import time

import serial

# ---- Angka wajib sama (panduan bagian 8) ----
BAUD = 500000
PACKET_SIZE = 64
PAYLOAD_MAX = 60
WAVE_PACKETS = 64
OPEN_SEQ = 0xFFFF
ACK_TIMEOUT = 15.0         # detik, per gelombang (AGENTS.md terbaru: 15.000 ms)

# ---- Jenis sinyal balasan ----
READY, ACK, NACK = 0x67, 0x02, 0x03
KNOWN_TYPES = {0x67, 0x02, 0x03, 0x10, 0x11, 0x12}

# Panduan tidak menyebut urutan byte untuk Nomor Urut (2 byte) maupun ukuran
# file (4 byte). Dipakai big-endian; HARUS disepakati dengan Sub-Tim HTTP
# (Laptop 3) dan Sub-Tim I2C/BLE (ESP32 #2 yang membaca nomor urut ACK).
BYTE_ORDER = "little"


def crc8_maxim(data):
    """CRC-8/MAXIM: poly 0x31 (refleksi 0x8C), init 0x00, tanpa xorout."""
    crc = 0
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0x8C if crc & 1 else crc >> 1
    return crc


def build_packet(seq, payload):
    if len(payload) > PAYLOAD_MAX:
        raise ValueError("payload > 60 byte")
    pkt = bytearray(PACKET_SIZE)
    pkt[0:2] = seq.to_bytes(2, BYTE_ORDER)
    pkt[2] = len(payload)
    pkt[3:3 + len(payload)] = payload
    pkt[63] = crc8_maxim(pkt[:63])
    return bytes(pkt)


def build_opening_packet(size, filename):
    name = os.path.basename(filename).encode("utf-8")[:56]
    payload = size.to_bytes(4, BYTE_ORDER) + name
    return build_packet(OPEN_SEQ, payload)


def split_image(data):
    """Return dict {seq: paket 64 byte} untuk seq 0..N-1."""
    packets = {}
    for seq, off in enumerate(range(0, len(data), PAYLOAD_MAX)):
        if seq >= OPEN_SEQ:
            raise ValueError("file terlalu besar (nomor urut melebihi 0xFFFE)")
        packets[seq] = build_packet(seq, data[off:off + PAYLOAD_MAX])
    return packets


class ReplyReader:
    """Membaca Sinyal Balasan 4 byte dari serial."""

    def __init__(self, ser):
        self.ser = ser
        self.buf = bytearray()

    def poll(self, timeout):
        end = time.monotonic() + timeout
        while True:
            while len(self.buf) >= 4:
                if self.buf[0] in KNOWN_TYPES:
                    frame = bytes(self.buf[:4])
                    del self.buf[:4]
                    return frame
                del self.buf[0]  # buang byte sampah / resync
            if end - time.monotonic() <= 0:
                return None
            self.buf.extend(self.ser.read(self.ser.in_waiting or 1))


READY_FRAME = bytes([READY, 0, 0, 0])


def reset_esp32(ser):
    """Reset ESP32 #1 lewat RTS (EN rendah), DTR dibiarkan rendah (boot normal)."""
    ser.dtr = False
    ser.rts = True
    time.sleep(0.1)
    ser.rts = False
    time.sleep(0.05)


def wait_ready(rd, timeout):
    """Pindai per byte sampai ketemu persis [0x67, 0, 0, 0].

    Byte sebelum READY (teks boot ROM dsb.) dibuang. Byte setelah READY
    dibiarkan di buffer untuk dibaca sebagai sinyal balasan berikutnya.
    """
    print("Menunggu READY (0x67) ...")
    end = time.monotonic() + timeout
    while True:
        i = rd.buf.find(READY_FRAME)
        if i >= 0:
            del rd.buf[:i + 4]
            print("READY diterima.")
            return
        if len(rd.buf) > 3:
            del rd.buf[:-3]  # sisakan ekor 3 byte kalau-kalau READY terpotong
        if end - time.monotonic() <= 0:
            raise TimeoutError("READY tidak diterima")
        rd.buf.extend(rd.ser.read(rd.ser.in_waiting or 1))


def send_until_acked(ser, rd, packets, seqs, drop_ack=0.0, max_attempts=10):
    """Kirim satu gelombang (<= 64 paket) sampai semua seq mendapat ACK."""
    pending = set(seqs)
    attempt = 0
    while pending:
        attempt += 1
        if attempt > max_attempts:
            raise RuntimeError(f"gagal setelah {max_attempts}x kirim, sisa seq: "
                               f"{sorted(pending)[:10]}")
        batch = sorted(pending)
        if len(batch) > WAVE_PACKETS:
            raise ValueError("gelombang > 64 paket")
        ser.write(bytes([len(batch)]) + b"".join(packets[s] for s in batch))
        ser.flush()

        outstanding = set(batch)
        deadline = time.monotonic() + ACK_TIMEOUT
        n_nack = 0
        while outstanding:
            sig = rd.poll(deadline - time.monotonic())
            if sig is None:
                break  # timeout 3000 ms
            typ = sig[0]
            seq = int.from_bytes(sig[1:3], BYTE_ORDER)
            if typ not in (ACK, NACK) or seq not in outstanding:
                continue  # sinyal basi / bukan ACK akhir
            if drop_ack and random.random() < drop_ack:
                continue  # simulasi ACK hilang
            outstanding.discard(seq)
            if typ == ACK:
                pending.discard(seq)
            else:
                n_nack += 1
        if pending:
            print(f"  percobaan {attempt}: {len(pending)} paket belum ACK "
                  f"({n_nack} NACK, {len(outstanding)} timeout) -> kirim ulang")


def send_image(ser, path, ready_timeout=60.0, drop_ack=0.0, max_attempts=10,
               do_reset=True):
    with open(path, "rb") as f:
        data = f.read()
    if not data:
        raise ValueError("file kosong")
    packets = split_image(data)
    total = len(packets)
    print(f"{path}: {len(data)} byte -> {total} paket, "
          f"{(total + WAVE_PACKETS - 1) // WAVE_PACKETS} gelombang")

    rd = ReplyReader(ser)
    ser.reset_input_buffer()
    if do_reset:
        reset_esp32(ser)
    wait_ready(rd, ready_timeout)

    # Tahap 1: Paket Pembuka (0xFFFF)
    packets[OPEN_SEQ] = build_opening_packet(len(data), path)
    send_until_acked(ser, rd, packets, [OPEN_SEQ], drop_ack, max_attempts)
    print("Paket Pembuka di-ACK.")

    # Tahap 2+: gelombang 64 paket
    t0 = time.monotonic()
    for start in range(0, total, WAVE_PACKETS):
        seqs = list(range(start, min(start + WAVE_PACKETS, total)))
        send_until_acked(ser, rd, packets, seqs, drop_ack, max_attempts)
        print(f"Gelombang {start // WAVE_PACKETS + 1} selesai "
              f"(paket {seqs[0]}..{seqs[-1]})")
    print(f"Selesai dalam {time.monotonic() - t0:.2f} detik.")


def main():
    ap = argparse.ArgumentParser(description="Laptop 1 - pengirim gambar via UART")
    ap.add_argument("port")
    ap.add_argument("image")
    ap.add_argument("--baud", type=int, default=BAUD)
    ap.add_argument("--ready-timeout", type=float, default=60.0)
    ap.add_argument("--max-attempts", type=int, default=10)
    ap.add_argument("--no-reset", action="store_true",
                    help="jangan reset ESP32 #1 lewat RTS saat mulai")
    ap.add_argument("--drop-ack", type=float, default=0.0,
                    help="peluang 0..1 membuang ACK yang diterima (uji retry)")
    args = ap.parse_args()

    if os.path.getsize(args.image) >= 2 * 1024 * 1024:
        sys.exit("Gambar harus < 2 MB")
    ser = serial.Serial()
    ser.port, ser.baudrate, ser.timeout = args.port, args.baud, 0.05
    ser.dtr = False   # cegah reset ganda saat port dibuka
    ser.rts = False
    ser.open()
    try:
        send_image(ser, args.image, args.ready_timeout, args.drop_ack,
                   args.max_attempts, do_reset=not args.no_reset)
    finally:
        ser.close()


if __name__ == "__main__":
    main()