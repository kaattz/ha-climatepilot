"""Tests for the brand assets and the generator that produces them.

Brand images are the one deliverable nobody reviews by reading a diff. These
tests encode the two things that were actually wrong before, so they cannot
silently regress:

* **Coverage.** Home Assistant's ``brands`` component accepts eight image names
  and, when one is missing, falls back through ``IMAGE_FALLBACKS`` to a
  *light* counterpart. Shipping only the light set therefore does not degrade to
  "no image" - it renders the light image on a dark surface. That is exactly
  what happened: the light icon carries an opaque white plate, so dark mode
  showed a glaring white tile.
* **Legibility at 24 px.** That is the size of an entity/integration list row.
  An earlier revision drew three hairline waves that merged into a smudge at
  that size. "Looks fine at 512 px" is not evidence, so the check measures the
  downscaled render.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
BRAND = ROOT / "custom_components" / "climatepilot" / "brand"
MANIFEST = ROOT / "custom_components" / "climatepilot" / "manifest.json"

#: The exact set Home Assistant may request
#: (homeassistant/components/brands/const.py :: ALLOWED_IMAGES).
ALLOWED_IMAGES = (
    "icon.png",
    "logo.png",
    "icon@2x.png",
    "logo@2x.png",
    "dark_icon.png",
    "dark_logo.png",
    "dark_icon@2x.png",
    "dark_logo@2x.png",
)

#: The fallback chain, copied from that same module. A missing entry resolves to
#: the first existing candidate, which is how a missing dark image silently
#: becomes the light one.
IMAGE_FALLBACKS: dict[str, tuple[str, ...]] = {
    "logo.png": ("icon.png",),
    "icon@2x.png": ("icon.png",),
    "logo@2x.png": ("logo.png", "icon.png"),
    "dark_icon.png": ("icon.png",),
    "dark_logo.png": ("dark_icon.png", "logo.png", "icon.png"),
    "dark_icon@2x.png": ("icon@2x.png", "icon.png"),
    "dark_logo@2x.png": ("dark_icon@2x.png", "logo@2x.png", "logo.png", "icon.png"),
}

#: Home Assistant's list-row icon size, and the size the redesign was judged at.
LIST_ICON_PX = 24


@pytest.mark.parametrize("name", ALLOWED_IMAGES)
def test_every_allowed_brand_image_is_shipped(name):
    """All eight names must exist, so no request falls back to the wrong theme."""
    path = BRAND / name
    assert path.is_file(), (
        f"{name} is missing; HA would fall back to "
        f"{IMAGE_FALLBACKS.get(name, ())} and render the light image in dark mode"
    )
    assert path.stat().st_size > 0


@pytest.mark.parametrize("name", ALLOWED_IMAGES)
def test_brand_images_are_valid_pngs(name):
    with Image.open(BRAND / name) as image:
        assert image.format == "PNG"
        assert image.mode in ("RGBA", "RGB")


def test_icon_is_square_and_two_x_is_double():
    """HA serves @2x to high-DPI screens; the sizes must line up."""
    for base, retina in (("icon.png", "icon@2x.png"), ("logo.png", "logo@2x.png")):
        with Image.open(BRAND / base) as small, Image.open(BRAND / retina) as big:
            assert small.size[0] == big.size[0] // 2
            assert small.size[1] == big.size[1] // 2


def test_icon_is_square_and_logo_is_not():
    """Icons are square; logos are rectangular. HA uses them in different slots."""
    for name in ("icon.png", "icon@2x.png", "dark_icon.png", "dark_icon@2x.png"):
        with Image.open(BRAND / name) as image:
            assert image.size[0] == image.size[1], name

    for name in ("logo.png", "logo@2x.png", "dark_logo.png", "dark_logo@2x.png"):
        with Image.open(BRAND / name) as image:
            assert image.size[0] > image.size[1], name


def test_dark_variants_are_actually_dark():
    """The whole point of shipping dark images is that they differ.

    A dark variant that is a byte-for-byte copy of the light one would satisfy a
    filename check while still showing a white tile on a dark theme, so this
    compares the rendered pixels rather than the names.
    """
    for light, dark in (
        ("icon.png", "dark_icon.png"),
        ("icon@2x.png", "dark_icon@2x.png"),
    ):
        with Image.open(BRAND / light) as a, Image.open(BRAND / dark) as b:
            assert a.convert("RGBA").tobytes() != b.convert("RGBA").tobytes(), (
                f"{dark} is identical to {light}; dark mode would show the light asset"
            )

        # The plate (a corner pixel, outside the rounded corner radius) must be
        # light in the light variant and dark in the dark one.
        with Image.open(BRAND / light) as a:
            light_corner = a.convert("RGBA").getpixel((a.size[0] // 2, 2))
        with Image.open(BRAND / dark) as b:
            dark_corner = b.convert("RGBA").getpixel((b.size[0] // 2, 2))

        assert sum(light_corner[:3]) > 600, f"{light} plate should be light"
        assert sum(dark_corner[:3]) < 250, f"{dark} plate should be dark"


def test_icon_survives_downscaling_to_the_list_size():
    """The icon must still carry ink when HA shrinks it into a list row.

    This is the regression the redesign fixed: three hairline waves averaged
    away to a pale smudge at 24 px. The assertions are deliberately loose enough
    to allow any reasonable redraw but tight enough to catch "too thin to see".
    """
    with Image.open(BRAND / "icon@2x.png") as source:
        small = source.convert("RGBA").resize((LIST_ICON_PX, LIST_ICON_PX), Image.LANCZOS)

    pixels = list(small.getdata())
    total = len(pixels)

    # Structure: the slate outline must claim a real share of the icon.
    ink = [
        p
        for p in pixels
        if p[3] > 128 and sum(p[:3]) < 300  # opaque and dark
    ]
    assert len(ink) / total > 0.10, (
        f"only {len(ink) / total:.1%} of the {LIST_ICON_PX}px icon is house outline; "
        "the stroke is too thin to read at list size"
    )

    # Heat: the warm gradient must survive as warm pixels, not grey mush.
    warm = [
        p
        for p in pixels
        if p[3] > 128 and p[0] > 150 and p[0] > p[2] + 60  # red-dominant
    ]
    assert len(warm) / total > 0.05, (
        f"only {len(warm) / total:.1%} of the {LIST_ICON_PX}px icon is warm colour; "
        "the waves are too thin to read at list size"
    )


def test_icon_does_not_fill_the_whole_canvas():
    """A rounded plate needs breathing room or HA's list rows look cramped."""
    with Image.open(BRAND / "icon@2x.png") as image:
        rgba = image.convert("RGBA")
        width, height = rgba.size
        # The four extreme corners must be transparent (outside the rounding).
        for xy in ((0, 0), (width - 1, 0), (0, height - 1), (width - 1, height - 1)):
            assert rgba.getpixel(xy)[3] == 0, f"corner {xy} is opaque; plate too large"


def test_manifest_domain_matches_the_brand_directory():
    """The brand directory is resolved by integration domain, not by folder name."""
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    assert manifest["domain"] == BRAND.parent.name
    assert (BRAND.parent / "brand").is_dir(), (
        "HA's loader sets has_branding only when a top-level 'brand' entry exists"
    )


def test_repo_preview_logo_exists_for_github():
    """The README/GitHub preview is separate from the HA-served assets."""
    preview = ROOT / "logo.png"
    assert preview.is_file()
    with Image.open(preview) as image:
        assert image.size[0] > image.size[1]


def test_generator_is_reproducible():
    """Running the generator twice must produce identical bytes.

    Without this, a redraw could silently leave half the set stale - for
    instance light icons regenerated but dark ones forgotten.
    """
    import subprocess
    import sys

    before = {name: (BRAND / name).read_bytes() for name in ALLOWED_IMAGES}

    subprocess.run(
        [sys.executable, str(ROOT / "tools" / "make_brand_assets.py")],
        check=True,
        capture_output=True,
    )

    for name, original in before.items():
        assert (BRAND / name).read_bytes() == original, (
            f"{name} changed when the generator was re-run; the checked-in asset "
            "is not the one the generator produces"
        )
