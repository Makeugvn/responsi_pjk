import argparse
from contextlib import ExitStack
import math
import os
import queue
import sys
import threading
import time
import serial

FRAME_SIZE = 244
PAYLOAD_SIZE = 240
START_SEQ = 0xFFFF
BATCH_SIZE = 32
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
    frame[FRAME_SIZE - 1] = crc8_maxim(frame[:FRAME_SIZE - 1])
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
    print(f"[{ser.port}] Menunggu READY (0x67)...")
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
            print(f"[{ser.port}] READY!")
            return
    raise TimeoutError(f"[{ser.port}] Tidak menerima READY (0x67)")


def send_wave(ser: serial.Serial, frames: dict, sequences: list):
    pending = set(sequences)
    while pending:
        batch = sorted(pending)[:BATCH_SIZE]
        outgoing = bytes([len(batch)]) + b"".join(frames[s] for s in batch)
        written = ser.write(outgoing)
        ser.flush()
        if written != len(outgoing):
            raise IOError(f"[{ser.port}] UART write tidak lengkap: {written}/{len(outgoing)}")

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
                print(f"[{ser.port}] [NACK] seq={seq} err={err}")

        pending -= acknowledged
        if pending:
            print(f"[{ser.port}] [RETRY] {len(pending)} paket: {sorted(pending)[:10]}...")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("image")
    parser.add_argument("--ports", "--port", nargs="+", required=True, dest="ports")
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

    print(f"Kirim {args.image} ({len(image)} B, {packet_count} paket) via {args.ports} @ {args.baud}")

    with ExitStack() as stack:
        sers = []
        for port in args.ports:
            ser = stack.enter_context(serial.Serial(port, args.baud, timeout=0.05, write_timeout=10))
            ser.dtr = False
            ser.rts = True
            time.sleep(0.1)
            ser.reset_input_buffer()
            ser.rts = False
            sers.append(ser)

        for ser in sers:
            wait_ready(ser)

        print(f"Mengirim Paket Pembuka (0xFFFF) via {sers[0].port}...")
        send_wave(sers[0], frames, [START_SEQ])

        wave_q = queue.Queue()
        for first in range(0, packet_count, BATCH_SIZE):
            wave_q.put(list(range(first, min(first + BATCH_SIZE, packet_count))))

        lock = threading.Lock()
        done_count = [0]
        errors = []

        def worker(ser):
            try:
                while True:
                    try:
                        seqs = wave_q.get_nowait()
                    except queue.Empty:
                        return
                    send_wave(ser, frames, seqs)
                    with lock:
                        done_count[0] += len(seqs)
                        print(f"[{ser.port}] Progress: {done_count[0]}/{packet_count} ({done_count[0] * 100 // packet_count}%)")
            except Exception as e:
                errors.append(e)

        start_time = time.monotonic()
        threads = [threading.Thread(target=worker, args=(ser,)) for ser in sers]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        if errors:
            sys.exit(f"TRANSFER GAGAL: {errors[0]}")

        elapsed = time.monotonic() - start_time
        print(f"SELESAI dalam {elapsed:.2f}s ({len(image) / elapsed / 1024:.1f} KB/s)")


if __name__ == "__main__":
    main()
