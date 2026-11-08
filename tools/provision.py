"""Send Wi-Fi credentials, the bridge token and server URLs to the Cardputer over USB.

    .venv\\Scripts\\python tools\\provision.py --port COM3 --ssid "MyWiFi"
    .venv\\Scripts\\python tools\\provision.py --port COM3 --remote-url https://m5.example.com
    .venv\\Scripts\\python tools\\provision.py --port COM3 --info

The Wi-Fi password is asked interactively and only travels over the USB cable; it is
stored in the device's NVS, never in this repository.
"""

from __future__ import annotations

import argparse
import getpass
import json
import sys
import time
import tomllib
from pathlib import Path

import serial

ROOT = Path(__file__).resolve().parent.parent
BRIDGE_CONFIG = ROOT / "bridge" / "config.toml"


def open_port(port: str) -> serial.Serial:
    ser = serial.Serial()
    ser.port = port
    ser.baudrate = 115200
    ser.timeout = 0.2
    # Keep DTR/RTS low so opening the port does not reset the ESP32-S3 into the bootloader.
    ser.dtr = False
    ser.rts = False
    ser.open()
    return ser


def command(ser: serial.Serial, line: str, expect: str, timeout: float = 5.0) -> str:
    ser.reset_input_buffer()
    ser.write((line + "\n").encode("utf-8"))
    deadline = time.time() + timeout
    while time.time() < deadline:
        raw = ser.readline().decode("utf-8", "replace").strip()
        if raw.startswith(expect):
            return raw
        if raw.startswith("CFG ERR"):
            raise SystemExit(f"device rejected config: {raw}")
    raise SystemExit(f"no '{expect}' reply from device (is the m5v1 firmware running?)")


def bridge_token() -> str:
    if not BRIDGE_CONFIG.exists():
        raise SystemExit(f"{BRIDGE_CONFIG} not found: start the bridge once to generate it")
    return tomllib.loads(BRIDGE_CONFIG.read_text(encoding="utf-8"))["server"]["token"]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", default="COM3")
    parser.add_argument("--ssid", help="Wi-Fi network to add (password is prompted)")
    parser.add_argument("--remote-url", help="https URL of the bridge reachable from outside (tunnel)")
    parser.add_argument("--lan-url", help="fixed LAN URL, e.g. http://192.168.1.10:8765 (skips discovery)")
    parser.add_argument("--clear-wifi", action="store_true", help="forget all saved networks first")
    parser.add_argument("--no-token", action="store_true", help="do not send the bridge token")
    parser.add_argument("--info", action="store_true", help="only print the device status")
    args = parser.parse_args()

    ser = open_port(args.port)
    try:
        print(command(ser, "PING", "PONG"))
        if args.info:
            print(command(ser, "INFO", "INFO"))
            return

        cfg: dict = {}
        if not args.no_token:
            cfg["token"] = bridge_token()
        if args.clear_wifi:
            cfg["clear_wifi"] = True
        if args.ssid:
            password = getpass.getpass(f"Wi-Fi password for {args.ssid} (empty for open network): ")
            cfg["wifi"] = [{"ssid": args.ssid, "pass": password}]
        if args.remote_url is not None:
            cfg["remote_url"] = args.remote_url
        if args.lan_url is not None:
            cfg["lan_url"] = args.lan_url

        print(command(ser, "CFG " + json.dumps(cfg, ensure_ascii=False), "CFG OK"))
        print(command(ser, "INFO", "INFO"))
    finally:
        ser.close()


if __name__ == "__main__":
    sys.exit(main())
