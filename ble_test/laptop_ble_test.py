import asyncio
from bleak import BleakClient, BleakScanner

DEVICE_NAME = "ESP32_IMG_BRIDGE"
TX_CHAR_UUID = "12345678-1234-5678-1234-56789abcdef1"  # ESP32 -> Laptop (Notify)
RX_CHAR_UUID = "12345678-1234-5678-1234-56789abcdef2"  # Laptop -> ESP32 (Write)
ACK_BLE = bytes([0x12, 0x00, 0x00, 0x00])


async def main():
    print(f"Mencari perangkat BLE '{DEVICE_NAME}'...")
    device = await BleakScanner.find_device_by_name(DEVICE_NAME, timeout=10.0)
    if not device:
        print("ESP32 tidak ditemukan. Pastikan ESP32 sudah menyala dan advertising.")
        return

    async with BleakClient(device) as client:
        print(f"Terhubung ke {device.name} ({device.address}), MTU={client.mtu_size}")

        async def on_notify(_, data: bytearray):
            print(f"Terima {len(data)} byte | byte pertama={data[0]}")
            await client.write_gatt_char(RX_CHAR_UUID, ACK_BLE, response=False)
            print("-> Balas ACK BLE (0x12)")

        await client.start_notify(TX_CHAR_UUID, on_notify)
        print("Mendengarkan data dari ESP32... Tekan Ctrl+C untuk berhenti.")
        while True:
            await asyncio.sleep(1)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nSelesai.")
