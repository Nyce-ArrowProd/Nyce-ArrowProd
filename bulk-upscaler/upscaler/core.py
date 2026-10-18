"""Upscaling engine: wraps the Real-ESRGAN ncnn binary, with a basic fallback.

Pipeline for each image:
    1. Run the AI model at its native scale (4x for the x4plus models).
    2. Resize down to the scale the user asked for with Lanczos.
    3. Save as PNG or JPG, optionally stamped with 300 DPI for print.
"""

import os
import platform
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageFilter

MAX_BATCH = 5
SUPPORTED_EXTS = {".png", ".jpg", ".jpeg", ".webp"}
ENGINE_DIR = Path(__file__).resolve().parent.parent / "engine"


@dataclass(frozen=True)
class Model:
    label: str
    name: str
    native_scales: tuple  # scales the model can run directly


MODELS = {
    "photo": Model("Photo / realistic", "realesrgan-x4plus", (4,)),
    "graphic": Model("Graphic / illustration / logo", "realesrgan-x4plus-anime", (4,)),
    "fast": Model("Fast (lighter quality)", "realesr-animevideov3", (2, 3, 4)),
}


def engine_binary(engine_dir=ENGINE_DIR):
    """Return the path to the Real-ESRGAN executable, or None if not installed."""
    name = "realesrgan-ncnn-vulkan.exe" if platform.system() == "Windows" else "realesrgan-ncnn-vulkan"
    path = Path(engine_dir) / name
    return path if path.is_file() else None


def output_path(src, out_dir, scale, fmt):
    src = Path(src)
    ext = ".jpg" if fmt == "jpg" else ".png"
    candidate = Path(out_dir) / f"{src.stem}_upscaled_{scale}x{ext}"
    n = 2
    while candidate.exists():
        candidate = Path(out_dir) / f"{src.stem}_upscaled_{scale}x_{n}{ext}"
        n += 1
    return candidate


def _run_ai(binary, src, model, scale):
    """Run the AI model and return a PIL image at the model's native scale."""
    run_scale = scale if scale in model.native_scales else max(model.native_scales)
    with tempfile.TemporaryDirectory() as tmp:
        # The engine only reads png/jpg/webp, so normalize odd files to PNG first.
        tmp_in = Path(tmp) / "in.png"
        with Image.open(src) as im:
            im.save(tmp_in)
        tmp_out = Path(tmp) / "out.png"
        cmd = [
            str(binary), "-i", str(tmp_in), "-o", str(tmp_out),
            "-n", model.name, "-s", str(run_scale),
            "-m", str(Path(binary).parent / "models"),
        ]
        kwargs = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        result = subprocess.run(cmd, capture_output=True, text=True, **kwargs)
        if result.returncode != 0 or not tmp_out.exists():
            raise RuntimeError(f"AI engine failed: {(result.stderr or result.stdout).strip()[-400:]}")
        with Image.open(tmp_out) as out:
            return out.copy()


def _run_basic(src, scale):
    """Non-AI fallback: Lanczos resize plus light sharpening."""
    with Image.open(src) as im:
        im = im.copy()
    big = im.resize((im.width * scale, im.height * scale), Image.LANCZOS)
    return big.filter(ImageFilter.UnsharpMask(radius=2, percent=60, threshold=2))


def upscale_one(src, out_dir, scale=4, model_key="photo", fmt="png", dpi=300, binary=None):
    """Upscale a single image. Returns the saved output path."""
    model = MODELS[model_key]
    with Image.open(src) as probe:
        target = (probe.width * scale, probe.height * scale)

    if binary:
        img = _run_ai(binary, src, model, scale)
    else:
        img = _run_basic(src, scale)
    if img.size != target:
        img = img.resize(target, Image.LANCZOS)

    dest = output_path(src, out_dir, scale, fmt)
    os.makedirs(out_dir, exist_ok=True)
    save_kwargs = {"dpi": (dpi, dpi)} if dpi else {}
    if fmt == "jpg":
        if img.mode in ("RGBA", "LA", "P"):
            # JPG has no transparency; flatten onto white.
            rgba = img.convert("RGBA")
            bg = Image.new("RGB", rgba.size, (255, 255, 255))
            bg.paste(rgba, mask=rgba.split()[-1])
            img = bg
        img.convert("RGB").save(dest, "JPEG", quality=95, **save_kwargs)
    else:
        img.save(dest, "PNG", **save_kwargs)
    return dest


def upscale_batch(files, out_dir, scale=4, model_key="photo", fmt="png", dpi=300,
                  binary=None, on_progress=None):
    """Upscale up to MAX_BATCH files one after another (one GPU, so no parallel runs).

    on_progress(index, path, status, detail) is called with status
    "working", "done" or "error".
    """
    files = list(files)
    if len(files) > MAX_BATCH:
        raise ValueError(f"Batch limit is {MAX_BATCH} images, got {len(files)}.")
    results = []
    for i, f in enumerate(files):
        if on_progress:
            on_progress(i, f, "working", "")
        try:
            dest = upscale_one(f, out_dir, scale, model_key, fmt, dpi, binary)
            results.append((f, dest, None))
            if on_progress:
                on_progress(i, f, "done", str(dest))
        except Exception as e:  # keep going so one bad file doesn't sink the batch
            results.append((f, None, str(e)))
            if on_progress:
                on_progress(i, f, "error", str(e))
    return results


def is_supported(path):
    return Path(path).suffix.lower() in SUPPORTED_EXTS


def open_folder(path):
    if sys.platform == "win32":
        os.startfile(path)
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    elif shutil.which("xdg-open"):
        subprocess.Popen(["xdg-open", str(path)])
