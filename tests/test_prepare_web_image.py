"""Tests for scripts/prepare_web_image.py.

Regression guard for the 2026-09-11 bug: the ad-hoc `heif-convert -> 1600px`
step baked the EXIF rotation into pixels but left the STALE Orientation tag in
the output, so browsers rotated a second time -> sideways photos.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "prepare_web_image", REPO_ROOT / "scripts" / "prepare_web_image.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["prepare_web_image"] = mod
    spec.loader.exec_module(mod)
    return mod


pwi = _load_module()
Image = pytest.importorskip("PIL.Image")


def _write_src(path: Path, size=(400, 300), orientation: int = 6) -> Path:
    """Landscape image, top half white / bottom half black, tagged with an
    orientation so the correct display is a 90-degree rotation."""
    import numpy as np

    a = np.zeros((size[1], size[0], 3), dtype="uint8")
    a[: size[1] // 2] = 255
    im = Image.fromarray(a, "RGB")
    ex = Image.Exif()
    ex[274] = orientation
    im.save(path, format="JPEG", quality=95, exif=ex.tobytes())
    return path


def test_orientation_of_fixture_is_non_normal(tmp_path):
    src = _write_src(tmp_path / "in.jpg", orientation=6)
    assert pwi.read_orientation(src) == 6


def test_converter_does_not_emit_stale_tag(tmp_path):
    """THE regression: output must read Orientation 1 (absent == 1), not the
    source's 6. If this fails, browsers will double-rotate again."""
    src = _write_src(tmp_path / "in.jpg", orientation=6)
    out = pwi.to_web_jpeg(src, tmp_path / "out.jpg", max_dim=1600)
    assert pwi.read_orientation(out) == 1


def test_converter_bakes_rotation_into_pixels(tmp_path):
    """The white half must end up on the RIGHT (the display orientation),
    proving the rotation was applied to pixels exactly once."""
    import numpy as np

    src = _write_src(tmp_path / "in.jpg", orientation=6)
    out = pwi.to_web_jpeg(src, tmp_path / "out.jpg", max_dim=1600)
    arr = np.array(Image.open(out).convert("L"))
    left = arr[:, : arr.shape[1] // 2].mean()
    right = arr[:, arr.shape[1] // 2 :].mean()
    assert right > left + 50  # white mass on the right, not the top


def test_converter_downscales_but_never_upscales(tmp_path):
    big = _write_src(tmp_path / "big.jpg", size=(4000, 3000), orientation=1)
    out = pwi.to_web_jpeg(big, tmp_path / "big_out.jpg", max_dim=1600)
    assert max(Image.open(out).size) == 1600
    small = _write_src(tmp_path / "small.jpg", size=(400, 300), orientation=1)
    out2 = pwi.to_web_jpeg(small, tmp_path / "small_out.jpg", max_dim=1600)
    assert max(Image.open(out2).size) == 400  # unchanged, not upscaled


def test_heif_bakes_and_strips_tag(tmp_path):
    """End-to-end HEIC path: heif source with orientation 6 -> JPEG that is
    rotated once and carries NO stale tag."""
    pillow_heif = pytest.importorskip("pillow_heif")
    pillow_heif.register_heif_opener()
    heic = tmp_path / "in.heic"
    _write_src(heic, orientation=6)
    # re-save fixture as HEIC preserving the tag
    im = Image.open(heic)
    ex = Image.Exif()
    ex[274] = 6
    im.save(heic, format="HEIF", exif=ex.tobytes())
    out = pwi.to_web_jpeg(heic, tmp_path / "out.jpg", max_dim=1600)
    assert pwi.read_orientation(out) == 1


def test_check_flags_stale_and_passes_normal(tmp_path):
    stale = _write_src(tmp_path / "stale.jpg", orientation=8)
    normal = _write_src(tmp_path / "normal.jpg", orientation=1)
    assert pwi.main(["--check", str(stale)]) == 1
    assert pwi.main(["--check", str(normal)]) == 0
    assert pwi.main(["--check", str(normal), str(stale)]) == 1


def test_unsupported_extension_raises(tmp_path):
    bad = tmp_path / "x.gif"
    bad.write_bytes(b"GIF89a")
    with pytest.raises(ValueError):
        pwi.to_web_jpeg(bad, tmp_path / "x.jpg")
