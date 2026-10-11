"""Generate the ClimatePilot brand assets.

Home Assistant renders a brand image for each integration in the "Add
integration" dialog and on the integration card. The local override lives at
``custom_components/<domain>/brand/`` and takes precedence over the remote
brands CDN.

The mark: a house silhouette with three rising heat waves inside it, coloured on
an amber-to-ember gradient. It reads as "a home being warmed" without using any
symbol that already means something else - an early draft used a plus inside the
house and read unmistakably as a medical/red-cross badge, which is exactly the
kind of accidental meaning a brand mark must avoid.

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

SUPERSAMPLE = 4


def _lerp(
    a: tuple[int, int, int, int], b: tuple[int, int, int, int], t: float
) -> tuple[int, int, int, int]:
    return tuple(round(x + (y - x) * t) for x, y in zip(a, b, strict=True))  # type: ignore[return-value]


def _draw_wave(
    draw: ImageDraw.ImageDraw,
    origin_x: float,
    base_y: float,
    width: float,
    height: float,
    *,
    stroke: int,
    colour: tuple[int, int, int, int],
) -> None:
    """Draw one vertical heat wave (an S-curve) rising upward."""
    steps = 40
    points: list[tuple[float, float]] = []
    for index in range(steps + 1):
        t = index / steps
        y = base_y - height * t
        # A sine offset across the height gives the classic "heat rising" wave.
        x = origin_x + math.sin(t * math.pi * 2.0) * (width * 0.5)
        points.append((x, y))
    for index in range(len(points) - 1):
        # Fade from ember at the bottom to amber at the top.
        t = index / (len(points) - 1)
        draw.line(
            [points[index], points[index + 1]],
            fill=_lerp(colour, _lerp(EMBER, AMBER, 1.0), t),
            width=stroke,
            joint="curve",
        )


def draw_mark(size: int, *, with_background: bool) -> Image.Image:
    """Render the ClimatePilot mark at ``size`` x ``size`` pixels."""
    s = size * SUPERSAMPLE
    image = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    if with_background:
        draw.rounded_rectangle([0, 0, s - 1, s - 1], radius=int(s * 0.22), fill=WHITE)

    stroke = max(3, int(s * 0.055))

    # --- house silhouette --------------------------------------------------
    # A roof triangle over a body, with generous internal space for the waves.
    peak = (s * 0.50, s * 0.13)
    roof_left = (s * 0.09, s * 0.47)
    roof_right = (s * 0.91, s * 0.47)
    body_left = s * 0.19
    body_right = s * 0.81
    body_top = s * 0.45
    body_bottom = s * 0.89

    draw.line([roof_left, peak], fill=SLATE, width=stroke, joint="curve")
    draw.line([peak, roof_right], fill=SLATE, width=stroke, joint="curve")
    draw.line([(body_left, body_top), (body_left, body_bottom)], fill=SLATE, width=stroke)
    draw.line(
        [(body_right, body_top), (body_right, body_bottom)],
        fill=SLATE,
        width=stroke,
    )
    draw.line(
        [(body_left, body_bottom), (body_right, body_bottom)],
        fill=SLATE,
        width=stroke,
    )

    # --- three rising heat waves ------------------------------------------
    wave_stroke = max(2, int(s * 0.042))
    centre_y = s * 0.795
    wave_height = s * 0.26
    wave_width = s * 0.10
    for centre_x in (s * 0.35, s * 0.50, s * 0.65):
        _draw_wave(
            draw,
            centre_x,
            centre_y,
            wave_width,
            wave_height,
            stroke=wave_stroke,
            colour=EMBER,
        )

    return image.resize((size, size), Image.LANCZOS)


def draw_logo(width: int, height: int) -> Image.Image:
    """Render the wider logo: the mark on a transparent field."""
    image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    mark_size = int(height * 0.90)
    mark = draw_mark(mark_size, with_background=False)
    image.alpha_composite(mark, (int((width - mark_size) / 2), int((height - mark_size) / 2)))
    return image


def main() -> int:
    BRAND_DIR.mkdir(parents=True, exist_ok=True)

    # Home Assistant reads icon.png and icon@2x.png from the brand directory.
    draw_mark(256, with_background=True).save(BRAND_DIR / "icon.png")
    draw_mark(512, with_background=True).save(BRAND_DIR / "icon@2x.png")

    draw_logo(512, 256).save(BRAND_DIR / "logo.png")
    draw_logo(1024, 512).save(BRAND_DIR / "logo@2x.png")

    # Repository-level preview (what GitHub shows for the project).
    draw_logo(1280, 640).save(ROOT / "logo.png")

    for path in sorted(BRAND_DIR.iterdir()):
        print(f"wrote {path.relative_to(ROOT)} ({path.stat().st_size} bytes)")
    print(f"wrote logo.png ({(ROOT / 'logo.png').stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
