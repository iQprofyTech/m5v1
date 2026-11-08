"""Collect the files for a GitHub release from the built firmware.

    cd firmware; ..\\.venv\\Scripts\\pio run; cd ..
    .venv\\Scripts\\python tools\\make_release.py

Writes dist\\release\\ (git-ignored):
  m5v1-cardputer-adv-<ver>.bin       the app image: for M5Launcher (SD card or tools/launcher_install.py)
  m5v1-cardputer-adv-<ver>-full.bin  bootloader + partitions + app: `esptool write-flash 0x0 <file>`
  SHA256SUMS.txt

The images contain no Wi-Fi passwords or tokens: those are stored in the device's NVS by
tools/provision.py or entered on the device, never compiled in.
"""

from __future__ import annotations

import hashlib
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BUILD = ROOT / "firmware" / ".pio" / "build" / "cardputer-adv"
OUT = ROOT / "dist" / "release"


def version() -> str:
    ini = (ROOT / "firmware" / "platformio.ini").read_text(encoding="utf-8")
    m = re.search(r'M5V1_VERSION=\\"([^"\\]+)\\"', ini)
    if not m:
        sys.exit("M5V1_VERSION not found in firmware/platformio.ini")
    return m.group(1)


def main() -> None:
    app = BUILD / "firmware.bin"
    full = BUILD / "firmware.factory.bin"
    for f in (app, full):
        if not f.exists():
            sys.exit(f"{f} not found: build first (cd firmware; pio run)")
    if app.stat().st_mtime < (ROOT / "firmware" / "platformio.ini").stat().st_mtime:
        sys.exit("firmware.bin is older than platformio.ini: rebuild (pio run)")

    ver = version()
    OUT.mkdir(parents=True, exist_ok=True)
    files = {
        f"m5v1-cardputer-adv-{ver}.bin": app,
        f"m5v1-cardputer-adv-{ver}-full.bin": full,
    }
    sums = []
    for name, src in files.items():
        dst = OUT / name
        shutil.copyfile(src, dst)
        data = dst.read_bytes()
        if data[:1] != b"\xe9":
            sys.exit(f"{name}: not an ESP32 image")
        sums.append(f"{hashlib.sha256(data).hexdigest()}  {name}")
        print(f"{name}: {len(data)} bytes")
    (OUT / "SHA256SUMS.txt").write_text("\n".join(sums) + "\n", encoding="utf-8")
    print(f"release files in {OUT}")


if __name__ == "__main__":
    main()
