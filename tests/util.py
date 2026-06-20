"""Tiny assertion helpers shared across the test modules."""
from __future__ import annotations

import numpy as np

import fitz
from PIL import Image


def first_page_bpc(pdf_path: str) -> int:
    """Bits-per-component of the first embedded image in a PDF. A CCITT-G4 fax
    page is 1 bpc (bilevel)."""
    doc = fitz.open(pdf_path)
    try:
        imgs = doc[0].get_images(full=True)
        assert imgs, "no embedded image on page 1"
        return int(doc.extract_image(imgs[0][0]).get("bpc", 0))
    finally:
        doc.close()


def assert_bilevel_pdf(pdf_path: str) -> None:
    """The output is a true 1-bit page: either the embedded image is 1 bpc, or
    (belt and braces) the rendered page contains only black and white pixels."""
    if first_page_bpc(pdf_path) == 1:
        return
    doc = fitz.open(pdf_path)
    try:
        pix = doc[0].get_pixmap(colorspace=fitz.csGRAY, alpha=False)
        arr = np.frombuffer(pix.samples, np.uint8)
    finally:
        doc.close()
    near_bw = np.isin(arr, (0, 255)).mean()
    assert near_bw > 0.99, f"page is not bilevel ({near_bw:.3f} pure b/w)"


def black_pixels_in_region(pdf_path: str, frac_box=(0.0, 0.0, 1.0, 1.0)) -> int:
    """Count black pixels inside a fractional bounding box of the page's actual
    1-bit bitmap (the embedded image, not a downscaled render — thin strokes
    survive at full resolution)."""
    arr = load_page_image(pdf_path)
    h, w = arr.shape[:2]
    x0, y0, x1, y1 = frac_box
    sub = arr[int(y0 * h):int(y1 * h), int(x0 * w):int(x1 * w)]
    return int((sub < 64).sum())


def load_page_image(pdf_path: str) -> np.ndarray:
    """Return the first embedded image of a PDF as a uint8 grayscale array
    (0 = black, 255 = white)."""
    doc = fitz.open(pdf_path)
    try:
        xref = doc[0].get_images(full=True)[0][0]
        raw = doc.extract_image(xref)
    finally:
        doc.close()
    import io

    with Image.open(io.BytesIO(raw["image"])) as im:
        return np.asarray(im.convert("L"), dtype=np.uint8)
