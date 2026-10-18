"""One-time download of the Real-ESRGAN AI engine (about 50 MB) into ./engine.

Source: https://github.com/xinntao/Real-ESRGAN (BSD-3-Clause license).
Run:  python setup_engine.py
"""

import hashlib
import io
import os
import platform
import stat
import sys
import urllib.request
import zipfile
from pathlib import Path

BASE = "https://github.com/xinntao/Real-ESRGAN/releases/download/v0.2.5.0/"
PACKAGES = {
    "Windows": ("realesrgan-ncnn-vulkan-20220424-windows.zip",
                "abc02804e17982a3be33675e4d471e91ea374e65b70167abc09e31acb412802d"),
    "Darwin": ("realesrgan-ncnn-vulkan-20220424-macos.zip",
               "e0ad05580abfeb25f8d8fb55aaf7bedf552c375b5b4d9bd3c8d59764d2cc333a"),
    "Linux": ("realesrgan-ncnn-vulkan-20220424-ubuntu.zip",
              "e5aa6eb131234b87c0c51f82b89390f5e3e642b7b70f2b9bbe95b6a285a40c96"),
}
SKIP = {"input.jpg", "input2.jpg", "onepiece_demo.mp4"}
ENGINE_DIR = Path(__file__).resolve().parent / "engine"


def download(url):
    print(f"Downloading {url}")
    with urllib.request.urlopen(url) as resp:
        total = int(resp.headers.get("Content-Length", 0))
        buf = io.BytesIO()
        while chunk := resp.read(1 << 20):
            buf.write(chunk)
            if total:
                print(f"\r  {buf.tell() * 100 // total}%", end="", flush=True)
    print()
    return buf.getvalue()


def main():
    system = platform.system()
    if system not in PACKAGES:
        sys.exit(f"Unsupported OS: {system}")
    filename, sha256 = PACKAGES[system]
    data = download(BASE + filename)
    if hashlib.sha256(data).hexdigest() != sha256:
        sys.exit("Checksum mismatch. Download may be corrupted; try again.")

    ENGINE_DIR.mkdir(exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        for member in zf.infolist():
            name = Path(member.filename)
            if member.is_dir() or name.name in SKIP or name.suffix == ".md":
                continue
            if ".." in name.parts or name.is_absolute():
                continue
            dest = ENGINE_DIR / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(zf.read(member))

    binary = ENGINE_DIR / ("realesrgan-ncnn-vulkan.exe" if system == "Windows" else "realesrgan-ncnn-vulkan")
    if system != "Windows":
        binary.chmod(binary.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    if system == "Darwin":
        # Clear Gatekeeper's quarantine flag if it got set.
        os.system(f'xattr -dr com.apple.quarantine "{ENGINE_DIR}" 2>/dev/null')
    print(f"AI engine installed in {ENGINE_DIR}")


if __name__ == "__main__":
    main()
