#!/usr/bin/env python3
"""
Scan for nearby BLE devices and print their names.
Run this with the LEGO hub powered on to confirm its advertised name.

Usage:
    .venv/bin/python3 scan_ble.py
"""

import asyncio
from bleak import BleakScanner

LEGO_SERVICE = "00001623-1212-efde-1623-785feabcd123"

async def scan():
    print("Scanning for BLE devices (5 seconds)…")
    devices = await BleakScanner.discover(timeout=5, service_uuids=[LEGO_SERVICE])

    if not devices:
        print("No LEGO devices found — make sure hub is on and blinking.")
        print("Trying without service filter…")
        devices = await BleakScanner.discover(timeout=5)

    if not devices:
        print("No BLE devices found at all.")
        return

    print(f"\nFound {len(devices)} device(s):")
    for d in devices:
        print(f"  Name: {d.name!r:30s}  Address: {d.address}")

    print("\nUse the Name value as HUB_NAME in lego_audio_reactive.py")

asyncio.run(scan())
