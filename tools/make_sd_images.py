"""Prepare app images for M5Launcher's SD card.

    .venv\\Scripts\\python -I tools\\make_sd_images.py backups\\cardputer-<date>.bin

Writes to dist\\sd\\:
  cardputer-adv-factory.bin  the original M5Stack firmware, cut out of a full-flash backup
  m5v1.bin                   this project's firmware (firmware\\.pio\\build\\...\\firmware.bin)

Copy both to the SD card root and install them from Launcher (SD menu).
"""

from __future__ import annotations

import shutil
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "dist" / "sd"
M5V1_BIN = ROOT / "firmware" / ".pio" / "build" / "cardputer-adv" / "firmware.bin"

ESP_IMAGE_MAGIC = 0xE9


def partitions(flash: bytes) -> list[tuple[str, int, int, int, int]]:
    out = []
    for off in range(0x8000, 0x8C00, 32):
        entry = flash[off : off + 32]
        if entry[:2] != b"\xaa\x50":
            break
        ptype, subtype = entry[2], entry[3]
        start, size = struct.unpack("<II", entry[4:12])
        name = entry[12:28].rstrip(b"\0").decode(errors="replace")
        out.append((name, ptype, subtype, start, size))
    return out


def image_length(data: bytes, start: int) -> int:
    """Length of an ESP32 app image: header, segments, checksum padding, optional SHA-256."""
    if data[start] != ESP_IMAGE_MAGIC:
        raise ValueError(f"no app image at {start:#x}")
    segments = data[start + 1]
    hash_appended = data[start + 23] == 1
    pos = start + 24
    for _ in range(segments):
        _addr, length = struct.unpack("<II", data[pos : pos + 8])
        pos += 8 + length
    pos += 1  # checksum byte
    pos = (pos + 15) & ~15  # padded to 16 bytes
    if hash_appended:
        pos += 32
    return pos - start


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    flash = Path(sys.argv[1]).read_bytes()
    OUT.mkdir(parents=True, exist_ok=True)

    apps = [p for p in partitions(flash) if p[1] == 0]
    if not apps:
        sys.exit("no app partition found in backup")
    name, _t, _s, start, size = apps[0]
    length = image_length(flash, start)
    if length > size:
        sys.exit(f"image length {length} exceeds partition {name} ({size})")
    factory = OUT / "cardputer-adv-factory.bin"
    factory.write_bytes(flash[start : start + length])
    print(f"{factory.name}: {length} bytes from partition '{name}' at {start:#x}")

    if M5V1_BIN.exists():
        shutil.copyfile(M5V1_BIN, OUT / "m5v1.bin")
        print(f"m5v1.bin: {M5V1_BIN.stat().st_size} bytes")
    else:
        print("firmware not built yet: run `pio run` in firmware\\ first")


if __name__ == "__main__":
    main()
