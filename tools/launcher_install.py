"""Install app images into M5Launcher over USB serial (no SD card or Wi-Fi needed).

    .venv\\Scripts\\python -I tools\\launcher_install.py --port COM3 Factory-ADV=dist\\sd\\cardputer-adv-factory.bin m5v1=dist\\sd\\m5v1.bin

Uses Launcher's serial console (`flash firmware <name> <size>`, src/serial_console.cpp):
the device answers READY, then takes the image in 2 KB chunks, ACKing each one, writes it
into a new app partition and reboots into it. Every image gets its own partition, so they
all stay selectable from the Launcher menu. The last one installed boots by default.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import serial

CHUNK = 2048
BANNER = "Press the button to enter the Launcher"


def open_port(port: str) -> serial.Serial:
    ser = serial.Serial()
    ser.port = port
    ser.baudrate = 115200
    ser.timeout = 0.2
    ser.dtr = False
    ser.rts = False
    ser.open()
    return ser


def read_line(ser: serial.Serial, deadline: float) -> str | None:
    while time.time() < deadline:
        raw = ser.readline()
        if raw:
            return raw.decode("utf-8", "replace").strip()
    return None


def wait_for(ser: serial.Serial, prefixes: tuple[str, ...], timeout: float) -> str:
    deadline = time.time() + timeout
    while True:
        line = read_line(ser, deadline)
        if line is None:
            raise SystemExit(f"timeout waiting for {prefixes}")
        if line.startswith(prefixes):
            return line


def open_launcher(port: str, timeout: float = 30.0) -> serial.Serial:
    """Open the port and get to the Launcher menu.

    At power-on the Launcher shows its banner for a few seconds and then starts the last
    installed app, which may not expose the USB console at all. The person enters the menu
    by pressing Enter on the banner; this only waits until the banner has stopped repeating.
    No key presses are sent: a remote SelPress meant for the banner can land in the menu
    and launch the highlighted app instead.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            ser = open_port(port)
        except serial.SerialException:
            time.sleep(0.2)
            continue
        try:
            while time.time() < deadline:
                ser.write(b"partitions\n")
                answered = False
                line = read_line(ser, time.time() + 0.4)
                while line is not None:
                    if line.startswith("Flash size"):
                        answered = True
                    line = read_line(ser, time.time() + 0.1)
                if not answered:
                    continue
                # The banner repeats every ~500 ms while the boot countdown runs; only once it
                # has been quiet for a while are we really in the menu (and safe to flash).
                quiet_until = time.time() + 1.5
                in_menu = True
                while time.time() < quiet_until:
                    line = read_line(ser, quiet_until)
                    if line and BANNER in line:
                        in_menu = False
                        break
                if in_menu:
                    ser.reset_input_buffer()
                    return ser
        except serial.SerialException:
            ser.close()
            time.sleep(0.2)
            continue
        ser.close()
    raise SystemExit("Launcher console did not answer: power-cycle and press Enter on the Launcher banner")


def app_partitions(ser: serial.Serial) -> list[str]:
    ser.reset_input_buffer()
    ser.write(b"partitions\n")
    labels = []
    deadline = time.time() + 5
    while (line := read_line(ser, deadline)) is not None:
        if line.startswith("Flash size"):
            break
        fields = line.split()
        if len(fields) > 2 and fields[1] == "type=app" and "subtype=ota" in line:
            labels.append(fields[0])
    return labels


def delete_previous(ser: serial.Serial, name: str) -> None:
    """Remove an earlier install of the same app (Launcher labels are a shortened, lowercased name)."""
    for label in app_partitions(ser):
        if name.lower().startswith(label.lower()):
            ser.write(f"partition delete {label}\n".encode())
            reply = wait_for(ser, ("OK", "ERR"), 10)
            print(f"  removed old '{label}': {reply}")


def install(ser: serial.Serial, name: str, image: bytes) -> None:
    if " " in name:
        raise SystemExit("names must not contain spaces")
    if image[0] != 0xE9:
        raise SystemExit(f"{name}: not an ESP32 app image")
    ser.reset_input_buffer()
    ser.write(f"flash firmware {name} {len(image)}\n".encode())
    reply = wait_for(ser, ("READY", "ERR"), 20)
    if reply.startswith("ERR"):
        raise SystemExit(f"{name}: {reply}")

    sent = 0
    started = time.time()
    while sent < len(image):
        chunk = image[sent : sent + CHUNK]
        ser.write(chunk)
        ack = wait_for(ser, ("ACK", "ERR"), 15)
        if ack.startswith("ERR"):
            raise SystemExit(f"{name}: {ack}")
        prev_pct = sent * 10 // len(image)
        sent += len(chunk)
        if sent * 10 // len(image) != prev_pct:
            print(f"  {name}: {sent * 100 // len(image)}%  {sent // 1024} / {len(image) // 1024} KB", flush=True)
    print(f"  sent in {time.time() - started:.0f}s")
    try:
        result = wait_for(ser, ("OK", "ERR"), 60)
    except serial.SerialException:
        # Launcher reboots into the new app right after "OK flashed"; the port often
        # disappears before that line is read, which after a full upload means success.
        print("  device rebooted (installed)")
        return
    if result.startswith("ERR"):
        raise SystemExit(f"{name}: {result}")
    print(f"  {result}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", default="COM3")
    parser.add_argument("--wait", type=float, default=30, help="seconds to wait for the Launcher console")
    parser.add_argument("--replace", action="store_true", help="delete an earlier install of the same name first")
    parser.add_argument(
        "--cmd", action="append", default=[], metavar="LINE",
        help='Launcher console command to run first, e.g. "partition delete m5v100" (repeatable)',
    )
    parser.add_argument("images", nargs="*", metavar="NAME=FILE")
    args = parser.parse_args()

    jobs = []
    for item in args.images:
        name, _, path = item.partition("=")
        if not path:
            sys.exit(f"expected NAME=FILE, got {item}")
        jobs.append((name, Path(path).read_bytes()))

    if args.cmd:
        print("waiting for Launcher...")
        ser = open_launcher(args.port, args.wait)
        try:
            for line in args.cmd:
                ser.write((line + "\n").encode())
                print(f"  {line}: {wait_for(ser, ('OK', 'ERR'), 15)}")
            for label in app_partitions(ser):
                print(f"  app partition: {label}")
        finally:
            ser.close()

    for name, image in jobs:
        print(f"{name}: waiting for Launcher...")
        ser = open_launcher(args.port, args.wait)
        try:
            if args.replace:
                delete_previous(ser, name)
            install(ser, name, image)
        finally:
            ser.close()
        time.sleep(1)  # device reboots after each install

    print("done")


if __name__ == "__main__":
    main()
