import sys
if "/usr/lib/python3/dist-packages" not in sys.path:
    sys.path.append("/usr/lib/python3/dist-packages")

from flask import Flask, request, jsonify
from pathlib import Path
import threading
import math
import re
import time

try:
    import cv2
    import numpy as np
    HAS_CV2 = True
except Exception:
    HAS_CV2 = False

app = Flask(__name__)

FRAME_SIZE = 244
PAYLOAD_SIZE = 240
START_SEQ = 0xFFFF

OUTPUT_DIR = Path(__file__).resolve().parent.parent / "received_images"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

transfer_lock = threading.Lock()
transfer = {
    "active": False,
    "complete": False,
    "total_size": 0,
    "filename": "",
    "packet_count": 0,
    "packets": {},
    "started_at": 0,
    "updated_at": 0,
    "preview_jpeg": b"",
}

_CRC8_TABLE = [0] * 256
for _i in range(256):
    _crc = _i
    for _ in range(8):
        _crc = ((_crc >> 1) ^ 0x8C) if (_crc & 1) else (_crc >> 1)
    _CRC8_TABLE[_i] = _crc


def crc8_maxim(data: bytes) -> int:
    crc = 0
    for byte in data:
        crc = _CRC8_TABLE[crc ^ byte]
    return crc & 0xFF


def safe_filename(name: str) -> str:
    name = Path(name).name
    name = re.sub(r"[^A-Za-z0-9._-]", "_", name)
    return name[:80] or "received_image.bin"


def build_partial_bytes() -> bytes:
    if not transfer["packets"]:
        return b""
    max_seq = max(transfer["packets"].keys())
    chunks = []
    for i in range(max_seq + 1):
        if i in transfer["packets"]:
            chunks.append(transfer["packets"][i])
        else:
            break
    raw = b"".join(chunks)[:transfer["total_size"]]
    if not raw.endswith(b"\xff\xd9"):
        raw += b"\xff\xd9"
    return raw


def update_live_preview():
    packet_count = transfer["packet_count"]
    received_count = len(transfer["packets"])
    if packet_count <= 0 or received_count == 0:
        return

    ratio = min(1.0, received_count / packet_count)
    raw = build_partial_bytes()

    if HAS_CV2 and len(raw) > 4:
        arr = np.frombuffer(raw, dtype=np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if img is not None:
            h, w = img.shape[:2]
            fill_h = max(1, int(h * ratio))
            if received_count < packet_count and fill_h < h:
                img[fill_h:, :] = 35
                cv2.line(img, (0, fill_h), (w, fill_h), (0, 220, 255), 2)
            ok, enc = cv2.imencode(".jpg", img)
            if ok:
                preview_bytes = enc.tobytes()
                transfer["preview_jpeg"] = preview_bytes
                if not transfer["complete"] and transfer["filename"]:
                    (OUTPUT_DIR / transfer["filename"]).write_bytes(preview_bytes)
                return

    transfer["preview_jpeg"] = raw
    if not transfer["complete"] and transfer["filename"]:
        (OUTPUT_DIR / transfer["filename"]).write_bytes(raw)


def process_frame(frame: bytes, update_preview_now: bool = True):
    global transfer

    sequence = int.from_bytes(frame[0:2], "little")
    length = frame[2]

    if frame[FRAME_SIZE - 1] != crc8_maxim(frame[:FRAME_SIZE - 1]):
        print(f"[CRC ERROR] SEQ={sequence}")
        return 400, {"error": "CRC mismatch", "sequence": sequence}

    if length > PAYLOAD_SIZE:
        return 400, {"error": "Invalid payload length", "sequence": sequence}

    payload = frame[3:3 + length]

    if sequence == START_SEQ:
        if length < 4:
            return 400, {"error": "Invalid START frame", "sequence": sequence}
        total_size = int.from_bytes(payload[:4], "little")
        if total_size <= 0:
            return 400, {"error": "Invalid image size", "sequence": sequence}
        packet_count = math.ceil(total_size / PAYLOAD_SIZE)
        if packet_count >= START_SEQ:
            return 400, {"error": "Image too large", "sequence": sequence}
        filename = safe_filename(payload[4:].decode("utf-8", errors="replace"))

        with transfer_lock:
            transfer = {
                "active": True,
                "complete": False,
                "total_size": total_size,
                "filename": filename,
                "packet_count": packet_count,
                "packets": {},
                "started_at": time.monotonic(),
                "updated_at": time.monotonic(),
                "preview_jpeg": b"",
            }
        print(f"\n=== NEW TRANSFER: {filename} ({total_size} B, {packet_count} paket) ===")
        return 200, {"status": "started", "sequence": sequence}

    if not transfer["active"]:
        return 409, {"error": "No active transfer", "sequence": sequence}

    if sequence >= transfer["packet_count"]:
        return 400, {"error": "Invalid sequence", "sequence": sequence}

    expected_length = (
        transfer["total_size"] - sequence * PAYLOAD_SIZE
        if sequence == transfer["packet_count"] - 1
        else PAYLOAD_SIZE
    )
    if length != expected_length:
        return 400, {"error": "Wrong payload length", "sequence": sequence}

    with transfer_lock:
        transfer["packets"][sequence] = payload
        transfer["updated_at"] = time.monotonic()
        received_count = len(transfer["packets"])

    if received_count == transfer["packet_count"]:
        with transfer_lock:
            image = b"".join(transfer["packets"][i] for i in range(transfer["packet_count"]))
            image = image[:transfer["total_size"]]
            output_path = OUTPUT_DIR / transfer["filename"]
            output_path.write_bytes(image)
            elapsed = time.monotonic() - transfer["started_at"]
            transfer["active"] = False
            transfer["complete"] = True
            transfer["preview_jpeg"] = image
            transfer["updated_at"] = time.monotonic()
        print(f"=== COMPLETE: {output_path} ({len(image)} B in {elapsed:.2f}s) ===")
        return 200, {"status": "complete", "sequence": sequence, "received": received_count, "total": transfer["packet_count"]}

    if update_preview_now:
        with transfer_lock:
            update_live_preview()

    return 200, {"status": "ok", "sequence": sequence, "received": received_count, "total": transfer["packet_count"]}


@app.route("/status", methods=["GET"])
def status():
    with transfer_lock:
        return jsonify({
            "active": transfer["active"],
            "complete": transfer["complete"],
            "filename": transfer["filename"],
            "received": len(transfer["packets"]),
            "total": transfer["packet_count"],
            "updated_at": transfer["updated_at"],
        })


@app.route("/batch", methods=["POST"])
def receive_batch():
    data = request.get_data(cache=False)
    if len(data) == 0 or len(data) % FRAME_SIZE != 0:
        return jsonify({"error": "Invalid batch size"}), 400

    frame_count = len(data) // FRAME_SIZE
    failed = []
    for i in range(frame_count):
        frame = data[i * FRAME_SIZE:(i + 1) * FRAME_SIZE]
        status_code, _ = process_frame(frame, update_preview_now=False)
        if status_code != 200:
            failed.append(int.from_bytes(frame[0:2], "little"))

    with transfer_lock:
        if transfer["active"] and not transfer["complete"]:
            now = time.monotonic()
            if now - transfer.get("last_preview", 0) >= 0.5:
                transfer["last_preview"] = now
                update_live_preview()

    if not failed:
        return jsonify({"status": "batch_ok", "count": frame_count}), 200
    return jsonify({"status": "batch_error", "count": frame_count, "failed": failed}), 400


def gui_loop():
    last_shown = 0
    win = "Live Receiver Preview"
    cv2.namedWindow(win, cv2.WINDOW_NORMAL)

    placeholder = np.full((320, 520, 3), 24, dtype=np.uint8)
    cv2.putText(
        placeholder,
        "Menunggu pengiriman gambar...",
        (45, 165),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.75,
        (200, 200, 200),
        2,
    )
    cv2.imshow(win, placeholder)
    cv2.waitKey(1)

    while True:
        with transfer_lock:
            jpeg_data = transfer["preview_jpeg"]
            updated_at = transfer["updated_at"]
            received = len(transfer["packets"])
            total = transfer["packet_count"]
            filename = transfer["filename"]
            complete = transfer["complete"]

        if jpeg_data and updated_at != last_shown:
            arr = np.frombuffer(jpeg_data, dtype=np.uint8)
            img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
            if img is not None:
                h, w = img.shape[:2]
                if max(h, w) > 720:
                    scale = 720 / max(h, w)
                    img = cv2.resize(img, (int(w * scale), int(h * scale)))
                pct = (received * 100 // total) if total > 0 else 0
                cv2.rectangle(img, (0, 0), (img.shape[1], 32), (20, 20, 20), -1)
                cv2.putText(
                    img,
                    f"{filename}: {received}/{total} ({pct}%)" + (" - SELESAI!" if complete else ""),
                    (10, 22),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.6,
                    (0, 255, 128) if complete else (0, 220, 255),
                    2,
                )
                cv2.imshow(win, img)
                last_shown = updated_at

        cv2.waitKey(30)


if __name__ == "__main__":
    print("HTTP RECEIVER (64B) STARTED -> http://0.0.0.0:5000/batch")
    if HAS_CV2:
        print("[GUI] Jendela preview OpenCV aktif.")
        threading.Thread(
            target=lambda: app.run(host="0.0.0.0", port=5000, debug=False, threaded=True, use_reloader=False),
            daemon=True,
        ).start()
        try:
            gui_loop()
        except Exception as e:
            print(f"[GUI ERROR] {e}")
            while True:
                time.sleep(1)
    else:
        print("[GUI] OpenCV (cv2) belum terpasang. Untuk jendela preview, jalankan: pip install opencv-python")
        app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
