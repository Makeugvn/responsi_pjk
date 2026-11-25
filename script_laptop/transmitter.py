import argparse
import math
import os
import sys
import time
import serial

FRAME_SIZE = 64
PAYLOAD_SIZE = 60
START_SEQ = 0xFFFF
BATCH_SIZE = 64
ACK_TIMEOUT = 15.0

MSG_ACK = 0x02
MSG_NACK = 0x03
MSG_READY = 0x67


def crc8_maxim(data: bytes) -> int:
    crc = 0
    for byte in data:
        cur = byte
        for _ in range(8):
            mix = (crc ^ cur) & 1
            crc >>= 1
            if mix:
                crc ^= 0x8C
            cur >>= 1
    return crc & 0xFF


def make_frame(sequence: int, payload: bytes) -> bytes:
    if len(payload) > PAYLOAD_SIZE:
        raise ValueError(f"Payload maksimal {PAYLOAD_SIZE} byte")
    frame = bytearray(FRAME_SIZE)
    frame[0:2] = sequence.to_bytes(2, "little")
    frame[2] = len(payload)
    frame[3:3 + len(payload)] = payload
    frame[63] = crc8_maxim(frame[:63])
    return bytes(frame)


def read_signal(ser: serial.Serial, deadline: float):
    while time.monotonic() < deadline:
        raw = ser.read(4)
        if len(raw) == 4:
            msg_type = raw[0]
            seq = int.from_bytes(raw[1:3], "little")
            err = raw[3]
            return msg_type, seq, err
    return None


def wait_ready(ser: serial.Serial, timeout: float = 60.0):
    print("Menunggu READY (0x67) dari ESP32 #1...")
    deadline = time.monotonic() + timeout
    win = bytearray()
    target = bytes([MSG_READY, 0x00, 0x00, 0x00])
    while time.monotonic() < deadline:
        b = ser.read(1)
        if not b:
            continue
        win.extend(b)
        if len(win) > 4:
            del win[0]
        if bytes(win) == target:
            print("ESP32 READY!")
            return
    raise TimeoutError("Tidak menerima READY (0x67)")


def send_wave(ser: serial.Serial, frames: dict, sequences: list):
    pending = set(sequences)
    while pending:
        batch = sorted(pending)[:BATCH_SIZE]
        # 1 byte header jumlah paket (1..64) + N x 64 byte paket
        outgoing = bytes([len(batch)]) + b"".join(frames[s] for s in batch)
        written = ser.write(outgoing)
        ser.flush()
        if written != len(outgoing):
            raise IOError(f"UART write tidak lengkap: {written}/{len(outgoing)}")

        expected = set(batch)
        acknowledged = set()
        deadline = time.monotonic() + ACK_TIMEOUT
        while acknowledged != expected and time.monotonic() < deadline:
            sig = read_signal(ser, deadline)
            if not sig:
                break
            msg_type, seq, err = sig
            if seq not in expected:
                continue
            if msg_type == MSG_ACK:
                acknowledged.add(seq)
            elif msg_type == MSG_NACK:
                print(f"[NACK] seq={seq} err={err}")

        pending -= acknowledged
        if pending:
            print(f"[RETRY] Mengirim ulang {len(pending)} paket: {sorted(pending)[:10]}...")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("image")
    parser.add_argument("--port", required=True)
    parser.add_argument("--baud", type=int, default=500000)
    args = parser.parse_args()

    if not os.path.isfile(args.image):
        sys.exit("File gambar tidak ditemukan")
    with open(args.image, "rb") as f:
        image = f.read()
    if not image:
        sys.exit("Gambar kosong")

    packet_count = math.ceil(len(image) / PAYLOAD_SIZE)
    if packet_count >= START_SEQ:
        sys.exit("Gambar terlalu besar")

    filename = os.path.basename(args.image).encode("utf-8")[:56]
    start_payload = len(image).to_bytes(4, "little") + filename
    frames = {START_SEQ: make_frame(START_SEQ, start_payload)}
    for seq in range(packet_count):
        frames[seq] = make_frame(seq, image[seq * PAYLOAD_SIZE:(seq + 1) * PAYLOAD_SIZE])

    print(f"Kirim {args.image} ({len(image)} B, {packet_count} paket) via {args.port} @ {args.baud}")
    with serial.Serial(args.port, args.baud, timeout=0.05, write_timeout=10) as ser:
        ser.dtr = False
        ser.rts = True
        time.sleep(0.1)
        ser.reset_input_buffer()
        ser.rts = False
        wait_ready(ser)

        print("Mengirim Paket Pembuka (0xFFFF)...")
        send_wave(ser, frames, [START_SEQ])

        start_time = time.monotonic()
        for first in range(0, packet_count, BATCH_SIZE):
            seqs = list(range(first, min(first + BATCH_SIZE, packet_count)))
            send_wave(ser, frames, seqs)
            done = min(first + BATCH_SIZE, packet_count)
            print(f"Progress: {done}/{packet_count} ({done * 100 // packet_count}%)")

        elapsed = time.monotonic() - start_time
        print(f"SELESAI dalam {elapsed:.2f}s ({len(image) / elapsed / 1024:.1f} KB/s)")


if __name__ == "__main__":
    main()
