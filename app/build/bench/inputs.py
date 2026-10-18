"""Benchmark inputs in each form a customer might send.

    drawing  clean raster drawing (PNG)
    sketch   the drawing with a hand-drawn wobble (PNG)
    pdf      the vector sheet as a PDF print
    scan     the clean sheet scanned at 300 dpi: grayscale, slightly skewed,
             noisy and soft, paper white clipped like a scanner (TIFF)
    photo    the sketch photographed by a phone: keystone perspective, uneven
             light, pixels stored rotated with EXIF Orientation=6 (JPEG)
    picture  a bare picture with no sizes (PNG; used by the no-scale case)

Live runs measure how well the agent copes with each form. Oracle and mutant
runs never read the inputs, so they are unaffected.
"""

from __future__ import annotations

import io
import random

from PIL import Image, ImageChops, ImageFilter, ImageOps

from app.build.sheet import Sheet, to_pdf, to_png

VARIANTS = ("drawing", "sketch", "pdf", "scan", "photo")
EXIF_ORIENTATION = 0x0112


def render_input(sheet: Sheet, variant: str, seed: int) -> tuple[str, bytes]:
    """File name and bytes of the input the agent receives."""
    if variant == "pdf":
        return "input.pdf", to_pdf(sheet)
    if variant == "scan":
        return "input.tiff", _scan(sheet, seed)
    if variant == "photo":
        return "input.jpg", _photo(sheet, seed)
    return "input.png", to_png(sheet, dpi=150, style="sketch" if variant == "sketch" else "clean", seed=seed)


def preview_png(sheet: Sheet, variant: str, data: bytes, max_edge: int = 1600) -> bytes:
    """A browser-viewable picture of the input for the report (pixels as stored)."""
    if variant == "pdf":
        data = to_png(sheet, dpi=100)
    img = Image.open(io.BytesIO(data))
    img.thumbnail((max_edge, max_edge))
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "PNG", optimize=True)
    return buf.getvalue()


def _scan(sheet: Sheet, seed: int) -> bytes:
    rng = random.Random(seed)
    img = Image.open(io.BytesIO(to_png(sheet, dpi=300))).convert("L")
    angle = rng.choice((-1, 1)) * rng.uniform(0.5, 1.2)
    img = img.rotate(angle, resample=Image.Resampling.BICUBIC, expand=True, fillcolor=255)
    img = ImageChops.add(img, Image.effect_noise(img.size, 6), scale=1.0, offset=-128)  # zero-mean grain
    img = img.filter(ImageFilter.GaussianBlur(0.6))
    img = img.point(lambda v: 255 if v > 236 else v)  # scanners clip paper white
    buf = io.BytesIO()
    img.save(buf, "TIFF", compression="tiff_lzw", dpi=(300, 300))
    return buf.getvalue()


def _photo(sheet: Sheet, seed: int) -> bytes:
    rng = random.Random(seed)
    img = Image.open(io.BytesIO(to_png(sheet, dpi=200, style="sketch", seed=seed))).convert("RGB")
    w, h = img.size

    def j(limit: float) -> float:
        return rng.uniform(0.01, limit)

    # Keystone: QUAD samples this source quadrilateral (UL, LL, LR, UR) into the
    # output rectangle; corners outside the sheet show the table it lies on.
    quad = (-j(0.05) * w, -j(0.04) * h, -j(0.02) * w, h * (1 + j(0.03)),
            w * (1 + j(0.03)), h * (1 + j(0.02)), w * (1 + j(0.05)), -j(0.03) * h)
    img = img.transform((w, h), Image.Transform.QUAD, quad, resample=Image.Resampling.BICUBIC, fillcolor=(118, 108, 92))
    light = Image.linear_gradient("L").rotate(90).resize((w, h)).point(lambda v: 200 + v * 55 // 255)
    img = ImageChops.multiply(img, Image.merge("RGB", (light, light, light)))
    # Phones store the sensor's orientation and tag it; 6 = turn 90° clockwise to view.
    stored = img.rotate(90, expand=True)
    exif = Image.Exif()
    exif[EXIF_ORIENTATION] = 6
    buf = io.BytesIO()
    stored.save(buf, "JPEG", quality=85, exif=exif.tobytes())
    return buf.getvalue()


def upright(data: bytes) -> Image.Image:
    """The photo as a viewer shows it (EXIF orientation applied)."""
    return ImageOps.exif_transpose(Image.open(io.BytesIO(data)))
