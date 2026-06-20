"""Shared pytest fixtures: synthetic document factories.

Every fixture builds its input on demand with fitz / numpy / Pillow so the repo
carries no large binary test assets. Pages are kept small (a few hundred px) so
the whole suite runs in well under a minute even on CI.
"""
from __future__ import annotations

import numpy as np
import pytest

import fitz
from PIL import Image


# --------------------------------------------------------------------------- #
# Low-level builders                                                          #
# --------------------------------------------------------------------------- #
def _png_bytes(rgb: np.ndarray) -> bytes:
    """Encode an HxWx3 uint8 RGB array to PNG bytes."""
    import io

    buf = io.BytesIO()
    Image.fromarray(rgb.astype(np.uint8), "RGB").save(buf, format="PNG")
    return buf.getvalue()


def _image_pdf(path, rgb: np.ndarray, dpi: float):
    """Wrap a raster image into a one-page PDF such that the page's native DPI
    (pixels / inch, as `_native_dpi` computes it) equals `dpi`. No live text."""
    h, w = rgb.shape[:2]
    rect_w = w * 72.0 / dpi
    rect_h = h * 72.0 / dpi
    doc = fitz.open()
    page = doc.new_page(width=rect_w, height=rect_h)
    page.insert_image(fitz.Rect(0, 0, rect_w, rect_h), stream=_png_bytes(rgb))
    doc.save(str(path))
    doc.close()
    return str(path)


def _gradient_rgb(w: int, h: int) -> np.ndarray:
    """A colourful, textured continuous-tone field (passes the chroma gate and
    carries real photo variance/edges)."""
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    r = 128 + 110 * np.sin(xx / 9.0)
    g = 128 + 110 * np.sin(yy / 7.0 + 1.0)
    b = 128 + 110 * np.sin((xx + yy) / 11.0 + 2.0)
    return np.clip(np.dstack([r, g, b]), 0, 255).astype(np.uint8)


# --------------------------------------------------------------------------- #
# Fixtures                                                                    #
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="session")
def text_pdf(tmp_path_factory):
    """A single page of plain black-on-white body text (vector, no raster)."""
    path = tmp_path_factory.mktemp("text") / "text.pdf"
    doc = fitz.open()
    page = doc.new_page(width=612, height=792)
    body = ("The quick brown fox jumps over the lazy dog. "
            "Pack my box with five dozen liquor jugs.\n") * 18
    page.insert_textbox(fitz.Rect(54, 54, 558, 738), body, fontsize=11,
                        fontname="cour")
    doc.save(str(path))
    doc.close()
    return str(path)


@pytest.fixture(scope="session")
def signature_scan_pdf(tmp_path_factory):
    """A 300-DPI grayscale 'scan' carrying a thin dark squiggle (no live text)."""
    path = tmp_path_factory.mktemp("sig") / "sig.pdf"
    w, h = 900, 300
    img = np.full((h, w, 3), 245, np.uint8)            # near-white paper
    xs = np.linspace(60, w - 60, 1400)
    ys = (h / 2 + 60 * np.sin(xs / 28.0)).astype(int)
    for dx in range(2):
        for dy in range(2):
            img[np.clip(ys + dy, 0, h - 1),
                np.clip(xs.astype(int) + dx, 0, w - 1)] = 20
    _image_pdf(path, img, dpi=300)
    return str(path)


@pytest.fixture(scope="session")
def photo_cover_pdf(tmp_path_factory):
    """A full-page colour continuous-tone image (~150 DPI, no live text)."""
    path = tmp_path_factory.mktemp("photo") / "photo.pdf"
    return _image_pdf(path, _gradient_rgb(750, 970), dpi=150)


@pytest.fixture(scope="session")
def reverse_text_pdf(tmp_path_factory):
    """A page with a black band carrying white vector text (polarity case)."""
    path = tmp_path_factory.mktemp("rev") / "rev.pdf"
    doc = fitz.open()
    page = doc.new_page(width=612, height=792)
    page.draw_rect(fitz.Rect(40, 40, 572, 160), color=(0, 0, 0), fill=(0, 0, 0))
    page.insert_text((70, 110), "WHITE ON BLACK HEADER", fontsize=26,
                     color=(1, 1, 1))
    page.insert_textbox(fitz.Rect(54, 200, 558, 740),
                        "Ordinary dark body text below the banner.\n" * 10,
                        fontsize=12)
    doc.save(str(path))
    doc.close()
    return str(path)


@pytest.fixture(scope="session")
def qr_pdf(tmp_path_factory):
    """A high-contrast black/white code-like bitmap (no colour, no live text):
    exercises the hard-threshold (`dither none`) path."""
    path = tmp_path_factory.mktemp("qr") / "qr.pdf"
    rng = np.random.default_rng(0)
    n = 33
    cells = (rng.random((n, n)) > 0.5).astype(np.uint8)
    # Stamp three finder squares so it reads structurally like a QR code.
    def finder(r, c):
        cells[r:r + 7, c:c + 7] = 1
        cells[r + 1:r + 6, c + 1:c + 6] = 0
        cells[r + 2:r + 5, c + 2:c + 5] = 1
    finder(0, 0); finder(0, n - 7); finder(n - 7, 0)
    scale = 9
    mono = (1 - np.kron(cells, np.ones((scale, scale), np.uint8))) * 255
    rgb = np.dstack([mono, mono, mono]).astype(np.uint8)
    return _image_pdf(path, rgb, dpi=150)


@pytest.fixture(scope="session")
def mixed_page_pdf(tmp_path_factory):
    """Live vector body text PLUS a small 72-DPI colour raster (~16% of area).
    This is the regression case for the mixed-page `_effective_dpi` bug."""
    path = tmp_path_factory.mktemp("mixed") / "mixed.pdf"
    doc = fitz.open()
    page = doc.new_page(width=612, height=792)
    page.insert_textbox(fitz.Rect(54, 54, 558, 360),
                        "Crisp vector body text that must stay sharp. " * 30,
                        fontsize=11)
    img = _gradient_rgb(200, 150)                       # 200x150 px
    # rect 200x150 pt -> native DPI 72 for this embedded image
    page.insert_image(fitz.Rect(60, 420, 260, 570), stream=_png_bytes(img))
    doc.save(str(path))
    doc.close()
    return str(path)


@pytest.fixture(scope="session")
def raster_lowdpi_pdf(tmp_path_factory):
    """A full-page 72-DPI raster scan with NO live text (native must be kept)."""
    path = tmp_path_factory.mktemp("low") / "low.pdf"
    return _image_pdf(path, _gradient_rgb(420, 560), dpi=72)


@pytest.fixture(scope="session")
def raster_highdpi_pdf(tmp_path_factory):
    """A 600-DPI raster scan with NO live text (must clamp to the 300 ceiling)."""
    path = tmp_path_factory.mktemp("high") / "high.pdf"
    return _image_pdf(path, _gradient_rgb(600, 600), dpi=600)


@pytest.fixture(scope="session")
def image_file(tmp_path_factory):
    """A loose PNG on disk for the to_pdf wrapping test."""
    path = tmp_path_factory.mktemp("img") / "pic.png"
    Image.fromarray(_gradient_rgb(320, 240), "RGB").save(str(path))
    return str(path)
