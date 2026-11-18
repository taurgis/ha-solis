"""Brand images shipped beside the integration manifest.

Home Assistant 2026.3 and newer read ``custom_components/solis/brand/``.
icon.png and logo.png are the files already published for this domain in
home-assistant/brands ``custom_integrations/solis``. There are no @2x files:
the brands repository has no high-DPI originals, and its guidelines reject
images that are only upscaled.
"""

from __future__ import annotations

import json
from pathlib import Path
import struct

ROOT = Path(__file__).resolve().parents[1]
BRAND = ROOT / "custom_components" / "solis" / "brand"
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
# Color type 6 is RGBA. The published Solis images use transparency.
_RGBA = 6


def _png_header(path: Path) -> tuple[int, int, int]:
    """Return width, height, and color type from a PNG IHDR chunk."""
    data = path.read_bytes()
    assert data.startswith(PNG_SIGNATURE), path.name
    assert data[12:16] == b"IHDR", path.name
    width, height, _depth, color_type = struct.unpack(">IIBB", data[16:26])
    return width, height, color_type


def test_brand_images_match_the_hacs_sizes() -> None:
    """HACS asks for a square icon and accepts a landscape logo."""
    icon_w, icon_h, icon_color = _png_header(BRAND / "icon.png")
    logo_w, logo_h, logo_color = _png_header(BRAND / "logo.png")

    assert (icon_w, icon_h) == (256, 256)
    assert logo_h == 256
    assert logo_w > logo_h
    for color in (icon_color, logo_color):
        assert color == _RGBA


def test_no_upscaled_hidpi_images() -> None:
    """Upscaled @2x images are rejected by the brands guidelines, so none ship."""
    assert not (BRAND / "icon@2x.png").exists()
    assert not (BRAND / "logo@2x.png").exists()
    assert sorted(path.name for path in BRAND.glob("*.png")) == ["icon.png", "logo.png"]


def test_hacs_release_archive_includes_the_brand_folder() -> None:
    """Without zip_release, HACS installs the tag archive under custom_components.

    A separate release zip could omit ``brand/`` while the Git tree still
    passed the brands check. This integration does not use that option, and
    the release workflow does not filter files.
    """
    hacs = json.loads((ROOT / "hacs.json").read_text(encoding="utf-8"))
    assert "zip_release" not in hacs
    assert "filename" not in hacs
    manifest = json.loads(
        (ROOT / "custom_components" / "solis" / "manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["domain"] == "solis"
    assert manifest["issue_tracker"].startswith("https://")
    assert manifest["documentation"].startswith("https://")
    assert manifest["codeowners"]
    workflow = (ROOT / ".github" / "workflows" / "release.yaml").read_text(encoding="utf-8")
    assert "exclude" not in workflow
    assert "files:" not in workflow
