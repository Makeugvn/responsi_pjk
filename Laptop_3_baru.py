"""
Laptop_3_baru.py - HTTP Server Laptop 3 (self-contained, little-endian, 64B paket).

Endpoint:
  POST /batch   body: 1..64 paket x 64 byte (raw binary, application/octet-stream)
                200 {"status": "batch_ok"}
                400 {"status": "batch_error", "failed": [seq, ...]}
  GET  /        halaman web Live Image Receiver (progress bar + preview gambar)
  GET  /preview gambar preview JPEG terbaru
  GET  /health  pemeriksaan server hidup
  GET  /status  progres transfer
  POST /reset   kosongkan sesi aktif (hanya dari localhost)
"""

import argparse
import math
import re
import socket
import sys
import threading
import time
from pathlib import Path
from flask import Flask, Response, jsonify, request

if "/usr/lib/python3/dist-packages" not in sys.path:
    sys.path.append("/usr/lib/python3/dist-packages")

try:
    import cv2
    import numpy as np
    HAS_CV2 = True
except Exception:
    HAS_CV2 = False

PACKET_SIZE = 64
PAYLOAD_MAX = 60
OPEN_SEQ = 0xFFFF
BYTE_ORDER = "little"
MAX_PACKETS_PER_BATCH = 64
RECEIVED_DIR = Path(__file__).resolve().parent / "received_images"
RECEIVED_DIR.mkdir(parents=True, exist_ok=True)

_CRC8_TABLE = [0] * 256
for _i in range(256):
    _c = _i
    for _ in range(8):
        _c = ((_c >> 1) ^ 0x8C) if (_c & 1) else (_c >> 1)
    _CRC8_TABLE[_i] = _c


def crc8_maxim(data: bytes) -> int:
    crc = 0
    for b in data:
        crc = _CRC8_TABLE[crc ^ b]
    return crc & 0xFF


def safe_filename(name: str) -> str:
    name = re.sub(r"[^A-Za-z0-9._-]", "_", Path(name).name)
    return name[:80] or "received_image.bin"


class PacketStore:
    def __init__(self, output_dir: Path):
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self.reset()

    def reset(self):
        with self.lock:
            self.active = False
            self.complete = False
            self.total_size = 0
            self.filename = ""
            self.packet_count = 0
            self.packets = {}
            self.started_at = 0.0
            self.updated_at = 0.0
            self.last_preview = 0.0
            self.preview_jpeg = b""

    def status(self):
        with self.lock:
            return {
                "active": self.active,
                "complete": self.complete,
                "filename": self.filename,
                "received": len(self.packets),
                "total": self.packet_count,
                "updated_at": self.updated_at,
                "byte_order": BYTE_ORDER,
            }

    def _build_partial_bytes(self) -> bytes:
        if not self.packets:
            return b""
        max_seq = max(self.packets.keys())
        chunks = []
        for i in range(max_seq + 1):
            if i in self.packets:
                chunks.append(self.packets[i])
            else:
                break
        raw = b"".join(chunks)[:self.total_size]
        if not raw.endswith(b"\xff\xd9"):
            raw += b"\xff\xd9"
        return raw

    def update_live_preview(self):
        with self.lock:
            if not self.active or self.complete or self.packet_count <= 0 or not self.packets:
                return
            now = time.monotonic()
            if now - self.last_preview < 0.5:
                return
            self.last_preview = now
            ratio = min(1.0, len(self.packets) / self.packet_count)
            raw = self._build_partial_bytes()
            received_count = len(self.packets)
            total_count = self.packet_count

        if HAS_CV2 and len(raw) > 4:
            arr = np.frombuffer(raw, dtype=np.uint8)
            img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
            if img is not None:
                h, w = img.shape[:2]
                fill_h = max(1, int(h * ratio))
                if received_count < total_count and fill_h < h:
                    img[fill_h:, :] = 35
                    cv2.line(img, (0, fill_h), (w, fill_h), (0, 220, 255), 2)
                ok, enc = cv2.imencode(".jpg", img)
                if ok:
                    with self.lock:
                        self.preview_jpeg = enc.tobytes()
                        self.updated_at = time.monotonic()
                    return

        with self.lock:
            self.preview_jpeg = raw
            self.updated_at = time.monotonic()

    def apply_packet(self, pkt: bytes):
        seq = int.from_bytes(pkt[0:2], BYTE_ORDER)
        length = pkt[2]
        if pkt[63] != crc8_maxim(pkt[:63]) or length > PAYLOAD_MAX:
            return False, seq

        payload = pkt[3:3 + length]
        with self.lock:
            if seq == OPEN_SEQ:
                if length < 4:
                    return False, seq
                total_size = int.from_bytes(payload[:4], BYTE_ORDER)
                if total_size <= 0:
                    return False, seq
                packet_count = math.ceil(total_size / PAYLOAD_MAX)
                if packet_count >= OPEN_SEQ:
                    return False, seq
                self.active = True
                self.complete = False
                self.total_size = total_size
                self.filename = safe_filename(payload[4:].decode("utf-8", errors="replace"))
                self.packet_count = packet_count
                self.packets = {}
                self.started_at = time.monotonic()
                self.updated_at = self.started_at
                self.last_preview = 0.0
                self.preview_jpeg = b""
                print(f"\n=== NEW TRANSFER: {self.filename} ({total_size} B, {packet_count} paket) ===")
                return True, seq

            if not self.active or seq >= self.packet_count:
                return False, seq

            expected_len = (
                self.total_size - seq * PAYLOAD_MAX
                if seq == self.packet_count - 1
                else PAYLOAD_MAX
            )
            if length != expected_len:
                return False, seq

            self.packets[seq] = payload
            self.updated_at = time.monotonic()
            if len(self.packets) == self.packet_count:
                image = b"".join(self.packets[i] for i in range(self.packet_count))[:self.total_size]
                out = self.output_dir / self.filename
                out.write_bytes(image)
                elapsed = time.monotonic() - self.started_at
                self.active = False
                self.complete = True
                self.preview_jpeg = image
                self.updated_at = time.monotonic()
                print(f"=== COMPLETE: {out} ({len(image)} B in {elapsed:.2f}s) ===")
            return True, seq


def create_app(output_dir=None, store=None) -> Flask:
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = MAX_PACKETS_PER_BATCH * PACKET_SIZE
    store = store or PacketStore(Path(output_dir) if output_dir else RECEIVED_DIR)

    @app.get("/")
    def index():
        html = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Live Image Receiver</title>
<style>
  body { font-family: sans-serif; background: #18181b; color: #f4f4f5; text-align: center; padding: 20px; }
  .box { max-width: 720px; margin: 0 auto; background: #27272a; padding: 20px; border-radius: 12px; }
  .bar-bg { width: 100%; background: #3f3f46; height: 22px; border-radius: 11px; overflow: hidden; margin: 12px 0; }
  .bar-fill { height: 100%; background: #22c55e; width: 0%; transition: width 0.15s; }
  img { max-width: 100%; max-height: 75vh; border-radius: 8px; margin-top: 12px; border: 2px solid #52525b; }
</style></head><body>
<div class="box">
  <h2>Live Image Receiver</h2>
  <div id="info">Menunggu pengiriman gambar...</div>
  <div class="bar-bg"><div class="bar-fill" id="bar"></div></div>
  <img id="preview" src="" style="display:none;" />
</div>
<script>
  async function poll() {
    try {
      const st = await (await fetch('/status')).json();
      if (st.total > 0) {
        const pct = Math.floor((st.received * 100) / st.total);
        document.getElementById('bar').style.width = pct + '%';
        document.getElementById('info').innerText = st.filename + ' - ' + st.received + '/' + st.total + ' paket (' + pct + '%)';
        if (st.received > 0) {
          const img = document.getElementById('preview');
          img.style.display = 'inline-block';
          img.src = '/preview?t=' + st.updated_at;
        }
      }
    } catch (e) {}
    setTimeout(poll, 200);
  }
  poll();
</script></body></html>"""
        return Response(html, mimetype="text/html")

    @app.get("/preview")
    def preview():
        with store.lock:
            data = store.preview_jpeg
        if not data:
            return Response(status=204)
        return Response(data, mimetype="image/jpeg")

    @app.post("/batch")
    def receive_batch():
        if request.mimetype not in ("application/octet-stream", ""):
            return jsonify({"status": "request_error", "error": "unsupported_content_type"}), 400
        body = request.get_data(cache=False)
        if not body or len(body) % PACKET_SIZE != 0 or len(body) > MAX_PACKETS_PER_BATCH * PACKET_SIZE:
            return jsonify({"status": "request_error", "error": "invalid_batch_size"}), 400

        failed = []
        for i in range(0, len(body), PACKET_SIZE):
            ok, seq = store.apply_packet(body[i:i + PACKET_SIZE])
            if not ok:
                failed.append(seq)

        store.update_live_preview()

        if failed:
            return jsonify({"status": "batch_error", "failed": failed}), 400
        return jsonify({"status": "batch_ok"}), 200

    @app.get("/health")
    def health():
        return jsonify({"status": "ok", "packet_size": PACKET_SIZE, "byte_order": BYTE_ORDER})

    @app.get("/status")
    def status():
        return jsonify(store.status())

    @app.post("/reset")
    def reset():
        if request.remote_addr not in ("127.0.0.1", "::1"):
            return jsonify({"status": "forbidden"}), 403
        store.reset()
        return jsonify({"status": "reset_ok"})

    return app


def get_lan_ipv4_addresses() -> list:
    addrs = set()
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            addrs.add(s.getsockname()[0])
    except OSError:
        pass
    return sorted(ip for ip in addrs if not ip.startswith(("127.", "169.254.")))


def gui_loop(store: PacketStore):
    last_shown = 0.0
    window_opened = False
    while True:
        with store.lock:
            jpeg_data = store.preview_jpeg
            updated_at = store.updated_at
            received = len(store.packets)
            total = store.packet_count
            filename = store.filename
            complete = store.complete

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
                cv2.imshow("Live Receiver Preview", img)
                window_opened = True
                last_shown = updated_at

        if window_opened:
            cv2.waitKey(30)
        else:
            time.sleep(0.05)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="HTTP Server Laptop 3 (64B, little-endian)")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=5000)
    parser.add_argument("--output-dir", default=str(RECEIVED_DIR))
    parser.add_argument("--no-gui", action="store_true", help="Nonaktifkan jendela OpenCV")
    args = parser.parse_args()

    print(f"LAPTOP 3 SERVER -> http://127.0.0.1:{args.port} (Web Preview) | POST /batch (byte_order={BYTE_ORDER})")
    for ip in get_lan_ipv4_addresses():
        print(f"LAN URL -> http://{ip}:{args.port}/batch")
    store = PacketStore(Path(args.output_dir))
    app = create_app(args.output_dir, store=store)
    if HAS_CV2 and not args.no_gui:
        threading.Thread(
            target=lambda: app.run(host=args.host, port=args.port, threaded=True, debug=False, use_reloader=False),
            daemon=True,
        ).start()
        try:
            gui_loop(store)
        except Exception:
            while True:
                time.sleep(1)
    else:
        app.run(host=args.host, port=args.port, threaded=True, debug=False, use_reloader=False)
