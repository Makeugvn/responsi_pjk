import argparse
import asyncio
import json
import time
import urllib.error
import urllib.request
from bleak import BleakClient, BleakScanner

DEVICE_NAME = "ESP32_IMG_BRIDGE"
SERVICE_UUID = "12345678-1234-5678-1234-56789abcdef0"
TX_CHAR_UUID = "12345678-1234-5678-1234-56789abcdef1"
RX_CHAR_UUID = "12345678-1234-5678-1234-56789abcdef2"

PKT_SIZE = 64
BATCH_MAX = 64
BATCH_WAIT_SEC = 0.15
ACK_BLE = bytes([0x12, 0x00, 0x00, 0x00])


def post_batch_sync(url: str, payload: bytes):
    req = urllib.request.Request(
        url, data=payload, method="POST",
        headers={"Content-Type": "application/octet-stream"},
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="ignore")
        try:
            return e.code, json.loads(body)
        except Exception:
            return e.code, {}


async def find_ble_device():
    found = await BleakScanner.discover(timeout=5.0, return_adv=True)
    for _, (d, adv) in found.items():
        if d.name == DEVICE_NAME or adv.local_name == DEVICE_NAME:
            return d
    for _, (d, adv) in found.items():
        if SERVICE_UUID in (adv.service_uuids or []):
            return d
    return None


async def run_bridge(url: str):
    while True:
        try:
            print(f"Mencari '{DEVICE_NAME}'...")
            device = await find_ble_device()
            if not device:
                print("ESP32 #2 tidak ditemukan, coba lagi...")
                await asyncio.sleep(1)
                continue

            pkt_queue = asyncio.Queue()
            write_lock = asyncio.Lock()

            async with BleakClient(device) as client:
                if hasattr(client, "_acquire_mtu"):
                    try:
                        await client._acquire_mtu()
                    except Exception:
                        pass
                print(f"Terhubung ke {device.name or device.address} (MTU={client.mtu_size}) -> {url}")

                async def on_notify(_, data: bytearray):
                    async with write_lock:
                        await client.write_gatt_char(RX_CHAR_UUID, ACK_BLE, response=False)
                    for i in range(0, len(data), PKT_SIZE):
                        if i + PKT_SIZE <= len(data):
                            await pkt_queue.put(bytes(data[i:i + PKT_SIZE]))

                await client.start_notify(TX_CHAR_UUID, on_notify)

                batch = []
                last_rx = time.monotonic()
                while client.is_connected:
                    try:
                        pkt = await asyncio.wait_for(pkt_queue.get(), timeout=0.005)
                        batch.append(pkt)
                        last_rx = time.monotonic()
                    except asyncio.TimeoutError:
                        pass

                    if not batch:
                        continue

                    if len(batch) >= BATCH_MAX or (time.monotonic() - last_rx >= BATCH_WAIT_SEC):
                        payload = b"".join(batch)
                        status, resp = await asyncio.to_thread(post_batch_sync, url, payload)
                        failed = set(resp.get("failed", [])) if status != 200 else set()

                        acks = bytearray()
                        for p in batch:
                            # little-endian: p[0]=seqLo, p[1]=seqHi
                            seq = int.from_bytes(p[0:2], "little")
                            is_fail = (status != 200 and (not failed or seq in failed))
                            acks.extend([0x03 if is_fail else 0x02, p[0], p[1], 0x02 if is_fail else 0x00])

                        max_write = max(4, ((client.mtu_size - 3) // 4) * 4)
                        async with write_lock:
                            for i in range(0, len(acks), max_write):
                                await client.write_gatt_char(RX_CHAR_UUID, bytes(acks[i:i + max_write]), response=False)
                                if i + max_write < len(acks):
                                    await asyncio.sleep(0.015)
                        batch.clear()
                print("BLE terputus, menyambung ulang...")
        except Exception as e:
            print(f"BLE error ({e}), menyambung ulang...")
            await asyncio.sleep(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:5000/batch")
    args = parser.parse_args()
    try:
        asyncio.run(run_bridge(args.url))
    except KeyboardInterrupt:
        print("\nBridge berhenti.")
