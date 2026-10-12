"""The face: a pill battery and two lines of text, drawn by us onto nothing.

Same mechanism as the clock widget: the image is handed to Windows as a per-pixel alpha
layered window, so unpainted pixels are the desktop itself.  The pill and the text sit on a
one-step alpha block (invisible, but it lets the widget be dragged and right-clicked); every
other pixel passes clicks straight through.

Animation is cheap on purpose: everything that does not move (outline, halo, text, the fill's
shape) is drawn once, supersampled for clean edges, and cached.  A frame only cuts the fill to
its level and lays the shimmer and the bolt over it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

#: The design, in units; one unit is one CSS pixel at "Medium".
BASE_W, BASE_H = 275.0, 60.0
BODY = (8.0, 9.0, 100.0, 51.0)
BODY_RADIUS = 12.0
STROKE = 2.2
FILL_INSET = 4.2
FILL_RADIUS = 8.0
NUB = (102.2, 22.5, 106.4, 37.5)
TEXT_X = 118.0
PERCENT_SIZE = 28.0
SIGN_SIZE = 15.0
STATUS_SIZE = 11.5
TEXT_GAP = 5.0
HIT_PAD = 4.0

#: Supersampling for the cached layers: Pillow does not antialias shapes on its own.
SS = 4
HIT_ALPHA = 1
#: Text on the wallpaper is drawn at this opacity, so it blends in rather than glaring.
BLEND = 0.82

#: Fill colours.
#: Fill colours: muted, so the pill sits with the rest of the desktop instead of glowing.
COLOURS = {
    "ok": (122, 184, 136),
    "low": (214, 178, 102),
    "critical": (208, 106, 98),
    "charging": (112, 196, 150),
}
#: How solid the fill is: the wallpaper shows through a little.
FILL_ALPHA = 0.62

INK = {
    #: outline / text colour, halo colour
    "light": ((255, 255, 255), (0, 0, 0)),
    "dark": ((20, 24, 30), (255, 255, 255)),
}

PERCENT_FONTS = (("bahnschrift.ttf", "Light"), ("segoeuil.ttf", None), ("segoeui.ttf", None))
STATUS_FONTS = (("segoeui.ttf", None), ("arial.ttf", None))


@dataclass(frozen=True)
class Frame:
    """Everything one frame depends on."""

    width: int
    height: int
    unit: float
    ink: str
    #: The level as drawn (0-100, may be fractional while it eases).
    level: float
    colour: str
    percent_text: str
    status_text: str
    charging: bool
    #: 0-1 position of the shimmer band, or None for no shimmer.
    shimmer: float | None = None
    #: 0-1 opacity of the bolt.
    bolt: float = 0.0
    #: 0-1 white flash over the fill (the moment the charger goes in).
    flash: float = 0.0
    #: 0-1 opacity of the fill itself (the critical-level pulse).
    fill_opacity: float = 1.0


@dataclass(frozen=True)
class Render:
    #: Premultiplied BGRA, ready for UpdateLayeredWindow.
    pixels: bytes
    width: int
    height: int
    hit: tuple[int, int, int, int]

    def in_hit(self, x: int, y: int) -> bool:
        left, top, right, bottom = self.hit
        return left <= x < right and top <= y < bottom


def size_for(width_css: float, scale: float) -> tuple[int, int, float]:
    """(width, height, unit) in physical pixels for a widget this many CSS pixels wide."""
    unit = width_css / BASE_W * scale
    return int(round(BASE_W * unit)), int(round(BASE_H * unit)), unit


# ---------------------------------------------------------------- fonts ---


@lru_cache(maxsize=32)
def _font(candidates: tuple[tuple[str, str | None], ...], size: int) -> ImageFont.FreeTypeFont:
    for name, variation in candidates:
        path = Path(r"C:\Windows\Fonts") / name
        if not path.exists():
            continue
        try:
            font = ImageFont.truetype(str(path), size)
            if variation:
                try:
                    font.set_variation_by_name(variation)
                except (OSError, ValueError):
                    pass
            return font
        except OSError:
            continue
    return ImageFont.load_default(size)


# ---------------------------------------------------------- cached layers ---


def _box(box: tuple[float, float, float, float], unit: float, ss: int = 1) -> list[float]:
    return [value * unit * ss for value in box]


def _fill_box() -> tuple[float, float, float, float]:
    left, top, right, bottom = BODY
    return (left + FILL_INSET, top + FILL_INSET, right - FILL_INSET, bottom - FILL_INSET)


def _downsample(image: Image.Image, size: tuple[int, int]) -> Image.Image:
    return image.resize(size, Image.Resampling.LANCZOS)


@lru_cache(maxsize=8)
def _shell(width: int, height: int, unit: float, ink: str) -> Image.Image:
    """Outline, nub and their halo: everything about the pill that never changes."""
    colour, halo = INK[ink]
    big = (width * SS, height * SS)
    mask = Image.new("L", big, 0)
    draw = ImageDraw.Draw(mask)
    draw.rounded_rectangle(
        _box(BODY, unit, SS),
        radius=BODY_RADIUS * unit * SS,
        outline=255,
        width=max(1, int(round(STROKE * unit * SS))),
    )
    draw.rounded_rectangle(_box(NUB, unit, SS), radius=2.0 * unit * SS, fill=255)
    mask = _downsample(mask, (width, height))

    shadow = mask.filter(ImageFilter.GaussianBlur(max(1.0, 2.2 * unit)))
    image = Image.new("RGBA", (width, height), (*halo, 0))
    image.putalpha(shadow.point(lambda v: v * 120 // 255))
    line = Image.new("RGBA", (width, height), (*colour, 0))
    line.putalpha(mask.point(lambda v: v * 230 // 255))
    return Image.alpha_composite(image, line)


@lru_cache(maxsize=8)
def _fill_mask(width: int, height: int, unit: float) -> Image.Image:
    """The full fill's rounded shape, antialiased."""
    mask = Image.new("L", (width * SS, height * SS), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        _box(_fill_box(), unit, SS), radius=FILL_RADIUS * unit * SS, fill=255
    )
    return _downsample(mask, (width, height))


@lru_cache(maxsize=8)
def _gloss(width: int, height: int, unit: float) -> Image.Image:
    """A soft highlight across the top of the fill, so it reads as a lit cell, not a flat bar."""
    _, top, _, bottom = _box(_fill_box(), unit)
    gloss = Image.new("L", (1, height), 0)
    middle = top + (bottom - top) * 0.55
    for y in range(height):
        if top <= y < middle:
            gloss.putpixel((0, y), int(40 * (1 - (y - top) / max(1.0, middle - top))))
    return gloss.resize((width, height))


@lru_cache(maxsize=8)
def _band(width: int, unit: float) -> Image.Image:
    """The shimmer: a horizontal band, bright in the middle, fading to nothing at both ends."""
    left, _, right, _ = _box(_fill_box(), unit)
    band_w = max(4, int((right - left) * 0.42))
    band = Image.new("L", (band_w, 1), 0)
    for x in range(band_w):
        band.putpixel((x, 0), int(100 * math.sin(math.pi * x / (band_w - 1)) ** 2))
    return band


@lru_cache(maxsize=8)
def _bolt(width: int, height: int, unit: float, ink: str) -> Image.Image:
    """The charging bolt, centred in the pill, white with a thin dark edge."""
    left, top, right, bottom = BODY
    cx, cy = (left + right) / 2, (top + bottom) / 2
    h = (bottom - top) * 0.62
    w = h * 0.62
    points = [
        (cx + w * 0.10, cy - h / 2),
        (cx - w * 0.50, cy + h * 0.06),
        (cx - w * 0.02, cy + h * 0.06),
        (cx - w * 0.12, cy + h / 2),
        (cx + w * 0.50, cy - h * 0.08),
        (cx + w * 0.02, cy - h * 0.08),
    ]
    big = [(x * unit * SS, y * unit * SS) for x, y in points]
    edge = Image.new("L", (width * SS, height * SS), 0)
    ImageDraw.Draw(edge).polygon(big, fill=255, outline=255, width=int(2.4 * unit * SS))
    body = Image.new("L", (width * SS, height * SS), 0)
    ImageDraw.Draw(body).polygon(big, fill=255)
    edge = _downsample(edge, (width, height))
    body = _downsample(body, (width, height))
    image = Image.new("RGBA", (width, height), (10, 40, 22, 0))
    image.putalpha(edge.point(lambda v: v * 150 // 255))
    white = Image.new("RGBA", (width, height), (255, 255, 255, 0))
    white.putalpha(body)
    return Image.alpha_composite(image, white)


@lru_cache(maxsize=16)
def _text(
    width: int, height: int, unit: float, ink: str, percent_text: str, status_text: str
) -> tuple[Image.Image, tuple[int, int, int, int]]:
    """Percentage and status line, with the halo that keeps them readable on any wallpaper."""
    colour, halo = INK[ink]
    big = _font(PERCENT_FONTS, max(1, int(round(PERCENT_SIZE * unit))))
    sign = _font(PERCENT_FONTS, max(1, int(round(SIGN_SIZE * unit))))
    small = _font(STATUS_FONTS, max(1, int(round(STATUS_SIZE * unit))))
    probe = ImageDraw.Draw(Image.new("L", (1, 1)))
    digits, has_sign = (percent_text[:-1], True) if percent_text.endswith("%") else (percent_text, False)
    digits_box = probe.textbbox((0, 0), digits, font=big, anchor="ls")
    status_box = probe.textbbox((0, 0), status_text, font=small, anchor="ls")
    cap = -digits_box[1]
    status_h = -status_box[1] + status_box[3]
    block = cap + TEXT_GAP * unit + status_h
    x = TEXT_X * unit
    baseline = (height - block) / 2 + cap
    status_base = baseline + TEXT_GAP * unit - status_box[1]

    layer = Image.new("L", (width, height), 0)
    draw = ImageDraw.Draw(layer)
    draw.text((x, baseline), digits, font=big, fill=255, anchor="ls")
    right = x + digits_box[2]
    if has_sign:
        draw.text((right + 1.5 * unit, baseline), "%", font=sign, fill=255, anchor="ls")
        right += 1.5 * unit + probe.textlength("%", font=sign)
    soft = Image.new("L", (width, height), 0)
    ImageDraw.Draw(soft).text((x + 1 * unit, status_base), status_text, font=small, fill=215, anchor="ls")
    right = max(right, x + unit + status_box[2])
    mask = ImageChops.lighter(layer, soft)

    image = Image.new("RGBA", (width, height), (*halo, 0))
    image.putalpha(mask.filter(ImageFilter.GaussianBlur(max(1.0, 2.0 * unit))).point(lambda v: v * 140 // 255))
    ink_layer = Image.new("RGBA", (width, height), (*colour, 0))
    ink_layer.putalpha(mask)
    box = (int(x), int(baseline - cap), int(math.ceil(right)), int(math.ceil(status_base + status_box[3])))
    return Image.alpha_composite(image, ink_layer), box


# ---------------------------------------------------------------- frame ---


def _solid(size: tuple[int, int], rgb: tuple[int, int, int], alpha: Image.Image) -> Image.Image:
    layer = Image.new("RGBA", size, (*rgb, 0))
    layer.putalpha(alpha)
    return layer


def _scaled(alpha: Image.Image, factor: float) -> Image.Image:
    factor = max(0.0, min(1.0, factor))
    if factor >= 0.999:
        return alpha
    return alpha.point(lambda v: int(v * factor))


def compose(frame: Frame) -> tuple[Image.Image, tuple[int, int, int, int]]:
    """The frame as straight RGBA plus its hit box (for previews and tests)."""
    size = (frame.width, frame.height)
    unit = frame.unit
    image = _shell(frame.width, frame.height, unit, frame.ink).copy()

    # The fill, cut at the level.  The cut column is partial, so the edge moves smoothly.
    left, _, right, _ = _box(_fill_box(), unit)
    level = max(0.0, min(100.0, frame.level))
    if level > 0:
        edge = left + (right - left) * level / 100.0
        edge = max(edge, left + 2.0 * unit)  # a sliver is still visible at 1%
        cut = Image.new("L", size, 0)
        whole = int(edge)
        if whole > 0:
            ImageDraw.Draw(cut).rectangle((0, 0, whole - 1, frame.height), fill=255)
        part = edge - whole
        if part > 0 and whole < frame.width:
            ImageDraw.Draw(cut).line((whole, 0, whole, frame.height), fill=int(255 * part))
        fill_alpha = ImageChops.multiply(_fill_mask(frame.width, frame.height, unit), cut)
        rgb = COLOURS["charging" if frame.charging else frame.colour]
        image = Image.alpha_composite(
            image, _solid(size, rgb, _scaled(fill_alpha, FILL_ALPHA * frame.fill_opacity))
        )
        image = Image.alpha_composite(
            image,
            _solid(size, (255, 255, 255), ImageChops.multiply(fill_alpha, _gloss(*size, unit))),
        )
        if frame.shimmer is not None:
            band = _band(frame.width, unit)
            travel = (edge - left) + band.width
            x = int(left - band.width + travel * frame.shimmer)
            strip = Image.new("L", size, 0)
            strip.paste(band.resize((band.width, frame.height)), (x, 0))
            image = Image.alpha_composite(
                image, _solid(size, (255, 255, 255), ImageChops.multiply(fill_alpha, strip))
            )
        if frame.flash > 0:
            image = Image.alpha_composite(
                image, _solid(size, (255, 255, 255), _scaled(fill_alpha, 0.75 * frame.flash))
            )

    if frame.bolt > 0:
        bolt = _bolt(frame.width, frame.height, unit, frame.ink)
        if frame.bolt < 0.999:
            bolt = bolt.copy()
            bolt.putalpha(_scaled(bolt.getchannel("A"), frame.bolt))
        image = Image.alpha_composite(image, bolt)

    text, text_box = _text(
        frame.width, frame.height, unit, frame.ink, frame.percent_text, frame.status_text
    )
    image = Image.alpha_composite(image, text)
    return image, hit_box(frame, text_box)


def hit_box(frame: Frame, text_box: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    body = _box(BODY, frame.unit)
    pad = HIT_PAD * frame.unit
    return (
        max(0, int(body[0] - pad)),
        max(0, int(min(body[1], text_box[1]) - pad)),
        min(frame.width, int(max(_box(NUB, frame.unit)[2], text_box[2]) + pad)),
        min(frame.height, int(max(body[3], text_box[3]) + pad)),
    )


def render(frame: Frame) -> Render:
    image, hit = compose(frame)
    image.putalpha(image.getchannel("A").point(lambda v: int(v * BLEND)))
    # The one-step block that makes the pill and the text draggable; zero everywhere else, so
    # clicks beside the widget reach the desktop.
    block = Image.new("L", image.size, 0)
    ImageDraw.Draw(block).rectangle((hit[0], hit[1], hit[2] - 1, hit[3] - 1), fill=HIT_ALPHA)
    image.putalpha(ImageChops.lighter(image.getchannel("A"), block))
    red, green, blue, alpha = image.convert("RGBa").split()
    bgra = Image.merge("RGBa", (blue, green, red, alpha))
    return Render(pixels=bgra.tobytes(), width=image.width, height=image.height, hit=hit)
