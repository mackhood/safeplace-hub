#!/usr/bin/env python3
"""
SafePlace — Emparejamiento (bonding) de la Xiaomi Smart Band 9 Active.

Sin bonding, TODA operación GATT sobre la Band falla con "Unlikely Error" (0x0E).
Hay que correr esto una sola vez por Raspberry; el bond queda guardado en BlueZ.

Correr con el python del venv (el que tiene bleak):
    /home/safeplace/safeplace-hub/venv/bin/python tools/pair_band9.py

Importante: una vez bondeada, la Band DEJA DE ADVERTIR, así que un scan por MAC ya
no la encuentra. A partir de ahí se conecta con `bluetoothctl connect <MAC>`.
"""

import argparse
import asyncio
import os
import sys

from bleak import BleakClient, BleakScanner

MAC = os.getenv("BAND_MAC", "")
ADAPTER = os.getenv("BAND_ADAPTER", "hci0")


async def main(mac: str, timeout: int) -> int:
    print(f"Buscando {mac} en {ADAPTER} (hasta {timeout}s)...")
    print("Poné la Band en modo visible: Ajustes → Bluetooth / o quitá el bond previo "
          "desde la app Mi Fitness.")

    device = await BleakScanner.find_device_by_address(
        mac, timeout=timeout, bluez={"adapter": ADAPTER})
    if device is None:
        print(f"\nNo apareció {mac}.")
        print("Si ya estaba bondeada no advierte: borrá el bond con")
        print(f"    bluetoothctl remove {mac}")
        print("y volvé a intentar.")
        return 1

    print(f"Encontrada: {device.name or '(sin nombre)'} — emparejando...")
    async with BleakClient(device, timeout=20) as client:
        ok = await client.pair()
        print("pair() ->", ok)
        try:
            batt = await client.read_gatt_char("00002a19-0000-1000-8000-00805f9b34fb")
            print(f"Batería: {batt[0]}%  (GATT responde, el bond quedó bien)")
        except Exception as e:
            print(f"No pude leer la batería: {e}")
            return 1

    print("\nListo. Verificá con:")
    print(f"    bluetoothctl info {mac}    # debe decir Paired/Bonded/Trusted: yes")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--mac", default=MAC, help="MAC de la Band (o BAND_MAC en el entorno)")
    ap.add_argument("--timeout", type=int, default=30)
    args = ap.parse_args()
    if not args.mac:
        ap.error("falta la MAC: pasá --mac o definí BAND_MAC")
    sys.exit(asyncio.run(main(args.mac, args.timeout)))
