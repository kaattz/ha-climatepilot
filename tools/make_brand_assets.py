"""Generate the ClimatePilot brand assets.

Home Assistant serves brand images from a local ``brand/`` directory inside a
custom integration (see "Brand images" in the developer docs; the loader exposes
``Integration.has_branding`` for any integration whose top-level files include
``brand``). Local images take precedence over the brands CDN, which matters here:
ClimatePilot is a private project and is not in the brands repository.

What Home Assistant actually requests
-------------------------------------
``homeassistant/components/brands/const.py`` defines the allowed set:

    icon.png  logo.png  icon@2x.png  logo@2x.png
    dark_icon.png  dark_logo.png  dark_icon@2x.png  dark_logo@2x.png

and an ``IMAGE_FALLBACKS`` chain used when a requested image is absent. The dark
variants fall back to their **light** counterparts, so shipping only the light
set does not produce "no image" - it produces the light image in a dark UI. Our
light icon carries an opaque white plate, which is right on a light background
and glaring on a dark one. Hence all eight are generated.

Two design constraints drive the geometry
-----------------------------------------
1. **Legibility at 24 px.** That is the size Home Assistant uses in entity and
   integration lists. An earlier revision drew three hairline waves ~1 px thick
   at 24 px; they blurred into a smudge and the thin house outline faded. The
   mark is therefore drawn with a much heavier stroke, so it survives
   downscaling.
2. **No false meaning.** A still earlier draft put a plus sign inside the house
   and read unmistakably as a medical / red-cross badge. The theme is now
   carried by colour and by the rising waves, with no symbol that already means
   something else.

Drawn from primitives so the asset is reproducible and reviewable rather than an
opaque binary.

Run:  python tools/make_brand_assets.py
"""

from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
BRAND_DIR = ROOT / "custom_components" / "climatepilot" / "brand"

# HomeAxis palette: deep slate for structure, warm amber-to-ember for heat.
SLATE = (37, 47, 63, 255)
AMBER = (245, 158, 11, 255)
EMBER = (214, 69, 43, 255)
WHITE = (255, 255, 255, 255)

#: Dark-mode plate and ink. Not pure black: a dark grey separates the mark from
#: a near-black background the way Home Assistant's dark theme reads.
DARK_PLATE = (28, 33, 43, 255)
DARK_INK = (232, 238, 245, 255)

SUPERSAMPLE = 4

#: Geometry as a fraction of the canvas. Kept in one place so the light and dark
#: variants stay pixel-identical apart from colour.
#:
#: These values were chosen by rendering candidates and inspecting them at 24 px
#: (Home Assistant's list size), not by taste at full resolution. Two bold waves
#: beat three thin ones decisively: at 24 px three strokes merged into a single
#: orange smudge, whereas two read as distinct shapes. The house outline is
#: correspondingly heavier so it survives the same downscale.
STROKE = 0.100  # house outline
WAVE_STROKE = 0.100  # a single wave
ROOF_PEAK = (0.50, 0.115)
ROOF_LEFT = (0.075, 0.455)
ROOF_RIGHT = (0.925, 0.455)
BODY_LEFT = 0.165
BODY_RIGHT = 0.835
BODY_TOP = 0.435
BODY_BOTTOM = 0.875
WAVE_BASE_Y = 0.835
WAVE_HEIGHT = 0.360
WAVE_WIDTH = 0.140
WAVE_CENTRES = (0.390, 0.610)


def _lerp(a: tuple[int, int, int, int], b: tuple[int, int, int, int], t: float):
    return tuple(round(x + (y - x) * t) for x, y in zip(a, b, strict=True))


def _draw_mark(size: int, *, plate: tuple[int, int, int, int] | None, ink) -> Image.Image:
    """Render the mark at ``size`` x ``size``.

    ``plate`` is the rounded-square background, or ``None`` for a transparent
    field. ``ink`` is the colour of the house outline; the waves always keep
    their warm gradient because that is what carries the "heating" meaning.
    """
    s = size * SUPERSAMPLE
    image = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    if plate is not None:
        draw.rounded_rectangle([0, 0, s - 1, s - 1], radius=int(s * 0.22), fill=plate)

    stroke = max(2, int(s * STROKE))

    # --- house silhouette ------------------------------------------------
    peak = (s * ROOF_PEAK[0], s * ROOF_PEAK[1])
    roof_left = (s * ROOF_LEFT[0], s * ROOF_LEFT[1])
    roof_right = (s * ROOF_RIGHT[0], s * ROOF_RIGHT[1])
    body_left = s * BODY_LEFT
    body_right = s * BODY_RIGHT
    body_top = s * BODY_TOP
    body_bottom = s * BODY_BOTTOM

    draw.line([roof_left, peak], fill=ink, width=stroke, joint="curve")
    draw.line([peak, roof_right], fill=ink, width=stroke, joint="curve")
    draw.line([(body_left, body_top), (body_left, body_bottom)], fill=ink, width=stroke)
    draw.line([(body_right, body_top), (body_right, body_bottom)], fill=ink, width=stroke)
    draw.line([(body_left, body_bottom), (body_right, body_bottom)], fill=ink, width=stroke)

    # --- rising heat waves -----------------------------------------------
    # Thicker strokes than the first revision: at 24 px three hairline waves
    # merged into a smudge, so legibility wins over decorative detail.
    wave_stroke = max(1, int(s * WAVE_STROKE))
    base_y = s * WAVE_BASE_Y
    height = s * WAVE_HEIGHT
    width = s * WAVE_WIDTH

    for centre_x in WAVE_CENTRES:
        steps = 44
        points: list[tuple[float, float]] = []
        for index in range(steps + 1):
            t = index / steps
            y = base_y - height * t
            x = s * centre_x + math.sin(t * math.pi * 2.0) * (width * 0.5)
            points.append((x, y))

        for index in range(len(points) - 1):
            t = index / (len(points) - 1)
            # Ember at the bottom (nearest the floor), amber at the top.
            colour = _lerp(EMBER, AMBER, t)
            draw.line(
                [points[index], points[index + 1]],
                fill=colour,
                width=wave_stroke,
                joint="curve",
            )

    return image.resize((size, size), Image.LANCZOS)


def draw_icon(size: int, *, dark: bool) -> Image.Image:
    """Square icon: mark on a rounded plate.

    The plate is what makes an icon look deliberate in Home Assistant's rounded
    list rows; the dark variant swaps it for a dark surface so it does not read
    as a bright white tile.
    """
    return _draw_mark(
        size,
        plate=DARK_PLATE if dark else WHITE,
        ink=DARK_INK if dark else SLATE,
    )


def draw_logo(width: int, height: int, *, dark: bool) -> Image.Image:
    """Rectangular logo: the mark on a transparent field.

    Logos appear on the integration page where the surrounding surface already
    provides contrast, so they stay transparent in both modes - but the outline
    still switches colour, otherwise the dark-mode logo would be dark ink on a
    dark background.
    """
    image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    mark_size = int(height * 0.90)
    mark = _draw_mark(mark_size, plate=None, ink=DARK_INK if dark else SLATE)
    image.alpha_composite(mark, (int((width - mark_size) / 2), int((height - mark_size) / 2)))
    return image


def main() -> int:
    BRAND_DIR.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    def save(image: Image.Image, path: Path) -> None:
        image.save(path)
        written.append(path)

    # All eight names Home Assistant may request. Shipping the dark set matters:
    # the fallback chain resolves a missing dark_* to its light counterpart, so
    # omitting them is not "no image" but "the light image on a dark surface".
    save(draw_icon(256, dark=False), BRAND_DIR / "icon.png")
    save(draw_icon(512, dark=False), BRAND_DIR / "icon@2x.png")
    save(draw_icon(256, dark=True), BRAND_DIR / "dark_icon.png")
    save(draw_icon(512, dark=True), BRAND_DIR / "dark_icon@2x.png")

    save(draw_logo(512, 256, dark=False), BRAND_DIR / "logo.png")
    save(draw_logo(1024, 512, dark=False), BRAND_DIR / "logo@2x.png")
    save(draw_logo(512, 256, dark=True), BRAND_DIR / "dark_logo.png")
    save(draw_logo(1024, 512, dark=True), BRAND_DIR / "dark_logo@2x.png")

    # Repository-level preview: what GitHub shows on the project page. Light
    # only, because GitHub renders one image regardless of theme.
    save(draw_logo(1280, 640, dark=False), ROOT / "logo.png")

    for path in written:
        print(f"wrote {path.relative_to(ROOT)} ({path.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
