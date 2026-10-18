import shutil
from pathlib import Path

import pytest
from PIL import Image

from upscaler import core


@pytest.fixture
def images(tmp_path):
    paths = []
    for i, mode in enumerate(["RGB", "RGBA", "RGB"]):
        p = tmp_path / f"img{i}.png"
        Image.new(mode, (40, 30), (200, 30, 30, 128) if mode == "RGBA" else (10, 120, 200)).save(p)
        paths.append(p)
    return paths


@pytest.mark.parametrize("scale", [2, 3, 4])
def test_basic_mode_sizes(images, tmp_path, scale):
    results = core.upscale_batch(images, tmp_path / "out", scale=scale)
    for src, dest, err in results:
        assert err is None
        assert Image.open(dest).size == (40 * scale, 30 * scale)


def test_batch_limit(images, tmp_path):
    with pytest.raises(ValueError):
        core.upscale_batch(images * 2, tmp_path)


def test_png_keeps_alpha_and_dpi(images, tmp_path):
    dest = core.upscale_one(images[1], tmp_path, scale=2, dpi=300)
    im = Image.open(dest)
    assert im.mode == "RGBA"
    assert round(im.info["dpi"][0]) == 300


def test_jpg_flattens(images, tmp_path):
    dest = core.upscale_one(images[1], tmp_path, scale=2, fmt="jpg")
    assert dest.suffix == ".jpg" and Image.open(dest).mode == "RGB"


def test_no_overwrite(images, tmp_path):
    a = core.upscale_one(images[0], tmp_path, scale=2)
    b = core.upscale_one(images[0], tmp_path, scale=2)
    assert a != b and a.exists() and b.exists()


def test_bad_file_does_not_sink_batch(images, tmp_path):
    bad = tmp_path / "broken.png"
    bad.write_text("not an image")
    results = core.upscale_batch([images[0], bad], tmp_path / "out", scale=2)
    assert results[0][2] is None and results[1][2] is not None


@pytest.mark.skipif(core.engine_binary() is None, reason="AI engine not installed")
@pytest.mark.parametrize("model", list(core.MODELS))
def test_ai_engine(images, tmp_path, model):
    dest = core.upscale_one(images[1], tmp_path, scale=2, model_key=model, binary=core.engine_binary())
    im = Image.open(dest)
    assert im.size == (80, 60) and im.mode == "RGBA"
