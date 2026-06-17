#!/usr/bin/env python3
"""Fax conversion pipeline.

Converts PDF pages to fax-native 1-bit (bilevel) output and packs them into a
CCITT-G4 PDF (default) or a Class-F multipage TIFF. Implements the decisions
described in references/fax-optimization.md:

  - anisotropic rasterization at a fax-native resolution (clamped to 1728 px)
  - MRC-lite content segmentation (hard-threshold text, halftone photos),
    using the PDF's own embedded-image rectangles
  - background flatten / despeckle / deskew pre-cleans
  - selectable dithering (floyd, atkinson, ordered, clustered) with `auto`
  - stroke thickening to save hairlines/small fonts
  - lossless CCITT-G4 embedding (no re-encode) via img2pdf
  - per-page transmission-time estimate from the actual G4-encoded size

Designed to be importable (used by optimize_pdf.py) or run directly.
"""
from __future__ import annotations

import io
import os
import tempfile
from dataclasses import dataclass, field

import numpy as np
import cv2
import fitz  # PyMuPDF
from PIL import Image
import img2pdf

# Fax-native resolutions: (horizontal_dpi, vertical_dpi)
RESOLUTIONS = {
    "standard": (204, 98),
    "fine": (204, 196),
    "superfine": (204, 391),
}
MAX_SCANLINE_PX = 1728


@dataclass
class FaxOptions:
    resolution: str = "fine"
    dither: str = "auto"            # photo halftone schema (see HALFTONE_INFO)
    fax_heavy: bool = False
    segmentation: str = "embedded"  # embedded|variance|none
    thicken: bool = False
    flatten_bg: bool = True
    despeckle: bool = True
    deskew: bool = True
    fmt: str = "pdf"                # pdf|tiff
    max_scanline_px: int = MAX_SCANLINE_PX
    line_rate_bps: int = 14400
    page_overhead_s: float = 1.5
    min_font_px: int = 12           # below this stroke height -> warn / thicken
    # Document-optimization knobs (halftoning-schemas.md)
    text_binarize: str = "contrast"  # contrast|sauvola|niblack|wolf|bradley|otsu
    tone_curve: str = "auto"        # auto|none  (dot-gain pre-correction)
    sharpen: bool = False           # edge-aware unsharp on photo regions
    green_noise_coarseness: float = 4.0  # green-noise AM<->FM knob (~2..8)
    text_in_image: bool = True      # rescue text baked into photos (don't halftone it)
    robust_image_text: str = "auto"  # auto|on|off  recolor washout-prone colored text
    robust_text_stroke: float = 0.15  # contrasting-stroke thickness (× glyph height), dark bg only
    ocr_text: str = "off"            # off|auto|on  recognise baked-in text and re-typeset it
    ocr_conf_min: float = 0.6        # minimum OCR confidence to re-typeset a word


@dataclass
class PageReport:
    index: int
    encoded_bytes: int = 0
    est_transmission_s: float = 0.0
    photo_regions: int = 0
    photo_fraction: float = 0.0
    dither: str = ""
    text_binarize: str = ""
    already_bilevel: bool = False
    robust_text: dict = field(default_factory=dict)
    ocr_text: dict = field(default_factory=dict)
    warnings: list = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Rasterization                                                               #
# --------------------------------------------------------------------------- #
def _page_scale(page_w_pt: float, hdpi: int, vdpi: int, max_w: int):
    """Per-axis render scale (points→px). Anisotropic on purpose (fax pixels are
    non-square: hdpi≠vdpi). If the page would exceed the scanline limit `max_w`,
    scale BOTH axes by the same factor so the page keeps its aspect ratio —
    clamping the horizontal scale alone leaves the page full-height and stretches
    it vertically (a tall, squashed fax)."""
    sx, sy = hdpi / 72.0, vdpi / 72.0
    if page_w_pt * sx > max_w:
        factor = max_w / (page_w_pt * sx)
        sx *= factor
        sy *= factor
    return sx, sy


def render_page_gray(page: fitz.Page, hdpi: int, vdpi: int,
                     max_w: int) -> np.ndarray:
    """Render a page to a grayscale ndarray at anisotropic dpi.

    PyMuPDF's Matrix lets us scale x and y independently, so we render straight
    onto the fax pixel grid instead of resampling a square render (which would
    distort the page and risk moire).
    """
    sx, sy = _page_scale(page.rect.width, hdpi, vdpi, max_w)
    mat = fitz.Matrix(sx, sy)
    pix = page.get_pixmap(matrix=mat, colorspace=fitz.csGRAY, alpha=False)
    arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width)
    return arr.copy()


def render_page_color(page: fitz.Page, hdpi: int, vdpi: int,
                      max_w: int) -> np.ndarray:
    """Render a page to an RGB ndarray at the same grid as render_page_gray.

    Used only for the comparison contact sheet's reference panel, so a viewer can
    see the original color document next to its grayscale and halftoned versions.
    """
    sx, sy = _page_scale(page.rect.width, hdpi, vdpi, max_w)
    mat = fitz.Matrix(sx, sy)
    pix = page.get_pixmap(matrix=mat, colorspace=fitz.csRGB, alpha=False)
    arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(
        pix.height, pix.width, pix.n)
    return arr[:, :, :3].copy()


def photo_region_mask(page: fitz.Page, shape, hdpi, vdpi, max_w,
                      mode: str) -> np.ndarray:
    """Boolean mask (True = continuous-tone/photo) for MRC routing.

    `embedded` uses the PDF's embedded-image rectangles (robust, structural).
    `variance` uses a local-variance heuristic for flattened scans with no
    image structure. `none` returns an all-False mask (whole page thresholded).
    """
    h, w = shape
    mask = np.zeros((h, w), dtype=bool)
    if mode == "none":
        return mask

    if mode == "embedded":
        sx, sy = _page_scale(page.rect.width, hdpi, vdpi, max_w)
        try:
            imgs = page.get_images(full=True)
        except Exception:
            imgs = []
        for img in imgs:
            xref = img[0]
            try:
                rects = page.get_image_rects(xref)
            except Exception:
                rects = []
            for r in rects:
                x0 = max(0, int(r.x0 * sx)); y0 = max(0, int(r.y0 * sy))
                x1 = min(w, int(r.x1 * sx)); y1 = min(h, int(r.y1 * sy))
                if x1 > x0 and y1 > y0:
                    mask[y0:y1, x0:x1] = True
    # If 'embedded' found nothing, the caller (process_page) falls back to the
    # variance heuristic. This function only handles the structural path.
    return mask


def variance_photo_mask(gray: np.ndarray, block: int = 24,
                        var_lo: float = 80.0, var_hi: float = 4000.0,
                        min_area_frac: float = 0.015) -> np.ndarray:
    """Heuristic photo mask for flattened scans.

    Text/line-art blocks are bimodal (very high variance, sparse); flat
    background is near-zero variance; continuous-tone photo blocks fall in a
    mid band. We mark mid-variance blocks and clean up with morphology.

    Text protection: gray / anti-aliased text often lands in the mid band, and a
    naive CLOSE then welds those strokes into the photo blob — so the text gets
    halftoned and becomes hard to read (exactly what we must avoid). We therefore
    OPEN first (with a small kernel) to erase thin text strokes, CLOSE to
    consolidate the genuine photo interior, then keep only large connected
    components. Real photos are big contiguous regions; text-induced specks are
    small and get dropped, so halftoning stays on image sections only.
    """
    h, w = gray.shape
    gf = gray.astype(np.float32)
    mean = cv2.boxFilter(gf, -1, (block, block))
    sq = cv2.boxFilter(gf * gf, -1, (block, block))
    var = np.clip(sq - mean * mean, 0, None)
    band = ((var > var_lo) & (var < var_hi)).astype(np.uint8)
    k_open = cv2.getStructuringElement(
        cv2.MORPH_ELLIPSE, (max(3, block // 3), max(3, block // 3)))
    k_close = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (block, block))
    band = cv2.morphologyEx(band, cv2.MORPH_OPEN, k_open)
    band = cv2.morphologyEx(band, cv2.MORPH_CLOSE, k_close)
    n, labels, stats, _ = cv2.connectedComponentsWithStats(band, 8)
    out = np.zeros_like(band)
    min_area = min_area_frac * h * w
    for i in range(1, n):
        if stats[i, cv2.CC_STAT_AREA] >= min_area:
            out[labels == i] = 1
    return out.astype(bool)


def _fill_holes(mask: np.ndarray) -> np.ndarray:
    """Fill regions fully enclosed by `mask` (uint8 0/1). Flood the outer
    background inward from a corner; any background pixels it can't reach are
    interior holes, so OR them back into the mask. (For a full-page raster the
    corners are document margin, i.e. background — the precondition for the one
    caller, where the photo never reaches the page corners.)"""
    inv = (1 - mask).astype(np.uint8)
    ff = np.zeros((mask.shape[0] + 2, mask.shape[1] + 2), np.uint8)
    cv2.floodFill(inv, ff, (0, 0), 0)
    return mask | inv


def consolidate_photo_region(mask: np.ndarray, vdpi: int,
                             min_area_frac: float = 0.02) -> np.ndarray:
    """Turn a textured-photo mask into a solid photo *region*: close small gaps,
    fill interior holes, and keep only large components.

    This keeps flat areas that sit INSIDE a photo — a colored sign, a patch of sky
    — on the halftone path instead of binarizing them to stark white, while the
    page's own text-on-white areas (which lie outside the photo) stay excluded."""
    k = max(15, int(round(vdpi / 8)))
    m = cv2.morphologyEx(mask.astype(np.uint8), cv2.MORPH_CLOSE,
                         cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k)))
    m = _fill_holes(m)
    n, labels, stats, _ = cv2.connectedComponentsWithStats(m, 8)
    out = np.zeros_like(m)
    min_area = min_area_frac * mask.shape[0] * mask.shape[1]
    for i in range(1, n):
        if stats[i, cv2.CC_STAT_AREA] >= min_area:
            out[labels == i] = 1
    return out.astype(bool)


# --------------------------------------------------------------------------- #
# Pre-cleaning                                                                #
# --------------------------------------------------------------------------- #
def flatten_background(gray: np.ndarray, knee: int = 235) -> np.ndarray:
    """Push near-white pixels to pure white so the background is clean, WITHOUT
    brightening the midtones. The old version stretched everything below the knee
    toward white, which washed out gray / light-gray text (a legibility killer);
    the text binarizer below is responsible for pulling that text to solid black,
    so here we only clamp the near-white paper and leave real content intact."""
    out = gray.copy()
    out[out >= knee] = 255
    return out


def deskew_gray(gray: np.ndarray, companions=None) -> tuple:
    """Estimate small skew from dark-pixel orientation and rotate to correct it.

    `companions` is an optional list of boolean masks rotated by the *same*
    transform (nearest-neighbour) so caller-side region masks stay aligned with
    the deskewed page. Returns (rotated_gray, angle, rotated_companions)."""
    companions = companions or []
    inv = 255 - gray
    coords = np.column_stack(np.where(inv > 64))
    if coords.shape[0] < 50:
        return gray, 0.0, companions
    angle = cv2.minAreaRect(coords[:, ::-1].astype(np.float32))[-1]
    if angle < -45:
        angle += 90
    if abs(angle) < 0.2 or abs(angle) > 15:
        return gray, 0.0, companions
    h, w = gray.shape
    M = cv2.getRotationMatrix2D((w / 2, h / 2), angle, 1.0)
    rot = cv2.warpAffine(gray, M, (w, h), flags=cv2.INTER_LINEAR,
                         borderMode=cv2.BORDER_CONSTANT, borderValue=255)
    comp_out = [cv2.warpAffine(c.astype(np.uint8), M, (w, h),
                               flags=cv2.INTER_NEAREST,
                               borderMode=cv2.BORDER_CONSTANT,
                               borderValue=0).astype(bool)
                for c in companions]
    return rot, float(angle), comp_out


def despeckle_bw(bw: np.ndarray, min_area: int = 2) -> np.ndarray:
    """Remove isolated black specks (connected black components <= min_area px).
    bw: uint8 0/255 where 0 = black."""
    black = (bw == 0).astype(np.uint8)
    n, lbl, stats, _ = cv2.connectedComponentsWithStats(black, connectivity=8)
    out = bw.copy()
    for i in range(1, n):
        if stats[i, cv2.CC_STAT_AREA] <= min_area:
            out[lbl == i] = 255
    return out


def thicken_bw(bw: np.ndarray) -> np.ndarray:
    """Dilate black features by one pixel so hairlines survive transmission."""
    black = (bw == 0).astype(np.uint8)
    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
    black = cv2.dilate(black, k, iterations=1)
    return np.where(black > 0, 0, 255).astype(np.uint8)


# --------------------------------------------------------------------------- #
# Halftoning / thresholding                                                   #
# --------------------------------------------------------------------------- #
def threshold_otsu(gray: np.ndarray) -> np.ndarray:
    _, bw = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    return bw


# --- Adaptive text binarization (document-imaging, not dithering) ------------ #
# These produce crisp 1-bit *text*, holding up under uneven illumination, dark
# header bars and reverse (white-on-black) type far better than a single global
# Otsu cut. They run before any halftone so text pixels never reach a screen.
def _local_mean_std(gray: np.ndarray, r: int):
    """Windowed mean and std via integral images: O(1) per pixel."""
    g = gray.astype(np.float64)
    k = 2 * r + 1
    ii = cv2.integral(g)                 # (h+1, w+1)
    ii2 = cv2.integral(g * g)
    h, w = g.shape

    def _winsum(I):
        y0 = np.clip(np.arange(h) - r, 0, h)
        y1 = np.clip(np.arange(h) + r + 1, 0, h)
        x0 = np.clip(np.arange(w) - r, 0, w)
        x1 = np.clip(np.arange(w) + r + 1, 0, w)
        A = I[np.ix_(y0, x0)]; B = I[np.ix_(y0, x1)]
        C = I[np.ix_(y1, x0)]; D = I[np.ix_(y1, x1)]
        return D - B - C + A

    cnt = (np.clip(np.arange(h) + r + 1, 0, h) - np.clip(np.arange(h) - r, 0, h))[:, None] * \
          (np.clip(np.arange(w) + r + 1, 0, w) - np.clip(np.arange(w) - r, 0, w))[None, :]
    cnt = np.maximum(cnt, 1)
    mean = _winsum(ii) / cnt
    var = np.maximum(_winsum(ii2) / cnt - mean * mean, 0.0)
    return mean, np.sqrt(var)


def binarize_text(gray: np.ndarray, method: str, vdpi: int) -> np.ndarray:
    """Return a 0/255 bilevel image (0=black) for text/line content.

    The whole point of a fax is to be READ, so text is never halftoned — it is
    thresholded to maximize contrast. The default ('contrast') is tuned for
    legibility: it marks black wherever a pixel is darker than its local paper by
    a small margin, so gray / light-gray text on a white background is pulled to
    SOLID BLACK instead of being dropped (the failure mode of a conservative
    adaptive cut). Smooth, text-free areas stay clean white (no speckle)."""
    method = (method or "contrast").lower()
    if method == "otsu":
        return threshold_otsu(gray)
    r = max(8, int(round(vdpi / 12)))    # window ~ half a body-text x-height
    g = gray.astype(np.float64)
    mean, std = _local_mean_std(gray, r)
    if method == "contrast":
        # Additive local-paper threshold (high recall on light text). Near-white
        # paper has mean ~250, so anything more than ~5% below it goes black; in
        # truly flat areas pixel ~= mean so nothing flips -> no background noise.
        thr = mean - 13.0
    elif method == "bradley":
        thr = mean * (1.0 - 0.15)
    elif method == "niblack":
        thr = mean - 0.2 * std
    elif method == "wolf":
        R = std.max() if std.max() > 1e-6 else 1.0
        M = float(g.min())
        thr = mean - 0.5 * (1.0 - std / R) * (mean - M)
    else:  # sauvola
        R, k = 128.0, 0.34
        thr = mean * (1.0 + k * (std / R - 1.0))
    black = g <= thr
    black |= _solid_fill_mask(g, mean)
    return np.where(black, 0, 255).astype(np.uint8)


def text_in_image_mask(gray: np.ndarray, vdpi: int) -> np.ndarray:
    """Detect text *baked into a photo* (captions, signs, screenshots, labels, a
    whole page scanned as one image) so it can be kept legible instead of being
    halftoned into mush.

    Text strokes are high-contrast and arranged in roughly horizontal runs. A
    top-hat / black-hat (morphological) response isolates light/dark structures
    while suppressing smooth photo gradients; we keep strong responses, group them
    into horizontal lines, and accept components whose geometry looks like a text
    line (wide, short, reasonably dense). The response is taken **multi-scale**
    (several stroke widths): body text is thin, but signage and display type have
    thick strokes whose interiors a single small kernel can't fill — it would catch
    only the edges, leaving the letters too sparse to pass the line-density gate
    (the failure that let big sign text fall through to the halftone). Still
    conservative: it would rather miss faint text than carve harsh blobs out of
    ordinary photo detail."""
    base = max(2, int(round(vdpi / 90)))          # ~ thinnest stroke scale (px)
    scales = sorted({base, base * 2, base * 4})   # thin body text … thick signage
    resp = np.zeros(gray.shape, np.float32)
    for sw in scales:
        k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * sw + 1, 2 * sw + 1))
        r = np.maximum(cv2.morphologyEx(gray, cv2.MORPH_BLACKHAT, k),   # dark text
                       cv2.morphologyEx(gray, cv2.MORPH_TOPHAT, k))     # light text
        resp = np.maximum(resp, r.astype(np.float32))
    strokes = (resp >= 45).astype(np.uint8)       # strong-contrast strokes only
    return _text_line_filter(strokes, vdpi, base)


def _text_line_filter(strokes: np.ndarray, vdpi: int, sw: int) -> np.ndarray:
    """Group a binary stroke map into accepted *text lines* and return the dilated
    glyph-body mask. Shared by the luminance detector (text_in_image_mask) and the
    chroma detector (washout_text_mask) so both apply the *same* conservative
    geometry gate: a component is kept only if it is wide, short and reasonably
    dense — the signature of a row of glyphs, not a photo edge or a color blob."""
    kw = max(6, int(round(vdpi / 14)))
    lines = cv2.morphologyEx(
        strokes, cv2.MORPH_CLOSE,
        cv2.getStructuringElement(cv2.MORPH_RECT, (kw, 1)))
    n, labels, stats, _ = cv2.connectedComponentsWithStats(lines, 8)
    out = np.zeros_like(lines)
    min_w = max(int(vdpi / 6), 16)
    max_h = int(vdpi * 0.5)
    for i in range(1, n):
        x, y, w, h, area = stats[i]
        if w < min_w or h < 3 or h > max_h:
            continue
        if w / float(h) < 2.2:                     # text lines are wide, not blobby
            continue
        if area / float(w * h) < 0.30:
            continue
        out[labels == i] = 1
    if out.any():                                  # expand from edges to glyph bodies
        out = cv2.dilate(
            out, cv2.getStructuringElement(cv2.MORPH_RECT, (3 * sw, 2 * sw)))
    return out.astype(bool)


# --------------------------------------------------------------------------- #
# Robust image text — rescue colored / low-contrast text through the fax channel #
# --------------------------------------------------------------------------- #
# A grayscale fax pipeline silently loses text that differs from its background
# mainly in HUE (e.g. yellow letters on a cyan field): both map to similar
# luminance, so after desaturation the glyphs vanish or merge, and a 1-bit
# threshold finishes the job. The signal that separates such text from its
# background lives in CHROMA — which grayscale throws away. This pass runs on the
# COLOR render, finds text whose chroma contrast is high but luminance contrast
# is low, segments the glyph pixels in LAB, and rewrites them into the gray
# buffer as solid black on a white matte. Everything downstream then inherits
# trivially legible, high-contrast text and needs no changes. It is deliberately
# conservative (same text-line geometry gate as text_in_image_mask) so ordinary
# black-on-white text — already high luminance contrast — is left untouched.

def _lab(rgb: np.ndarray):
    """Return (L, a, b) float planes; a/b centered on 0 (OpenCV stores +128)."""
    lab = cv2.cvtColor(rgb, cv2.COLOR_RGB2LAB)
    return (lab[:, :, 0].astype(np.float32),
            lab[:, :, 1].astype(np.float32) - 128.0,
            lab[:, :, 2].astype(np.float32) - 128.0)


def washout_text_mask(rgb: np.ndarray, vdpi: int,
                      aggressive: bool = False) -> np.ndarray:
    """Boolean mask of text whose ink/background differ strongly in COLOR but only
    weakly in LUMINANCE — the case a grayscale pipeline cannot see.

    For each candidate stroke we require a strong *chroma* top-hat/black-hat
    response AND a weak *luminance* one (high luma contrast means ordinary text
    the existing path already handles — skip it), then pass the result through the
    shared text-line geometry gate so only rows of glyphs survive."""
    L, a, b = _lab(rgb)
    L8 = np.clip(L, 0, 255).astype(np.uint8)
    a8 = np.clip(a + 128.0, 0, 255).astype(np.uint8)
    b8 = np.clip(b + 128.0, 0, 255).astype(np.uint8)
    # Multi-scale top-hat/black-hat: body text has ~2px strokes, but signage and
    # display type (the washout case) has thick strokes a small kernel only sees
    # the edges of. Take the max response across a few stroke widths so both thin
    # and bold colored text register.
    base = max(2, int(round(vdpi / 90)))
    scales = sorted({base, base * 2, base * 4})

    def hat(plane8, sw):
        k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * sw + 1, 2 * sw + 1))
        return np.maximum(cv2.morphologyEx(plane8, cv2.MORPH_BLACKHAT, k),
                          cv2.morphologyEx(plane8, cv2.MORPH_TOPHAT, k))

    chroma_resp = np.zeros(L.shape, np.float32)
    luma_resp = np.full(L.shape, 255.0, np.float32)   # min luma contrast across scales
    for sw in scales:
        chroma_resp = np.maximum(chroma_resp,
                                 np.maximum(hat(a8, sw), hat(b8, sw)).astype(np.float32))
        luma_resp = np.minimum(luma_resp, hat(L8, sw).astype(np.float32))
    chroma_thr = 12.0 if aggressive else 16.0      # min hue contrast to qualify
    luma_weak = 60.0 if aggressive else 50.0       # luma contrast above this = ordinary text, skip
    strokes = ((chroma_resp >= chroma_thr) &
               (luma_resp <= luma_weak)).astype(np.uint8)
    return _text_line_filter(strokes, vdpi, base)


def _segment_ink(rgb_bbox: np.ndarray, stroke_bbox: np.ndarray):
    """Within one (generously padded) text region, split pixels into ink vs
    background by k=2 LAB clustering and return a boolean ink mask.

    The ink cluster is the **minority** one: on a sign or label the glyphs always
    cover less area than the field they sit on, and this holds whether the text is
    darker OR lighter than the field, and whether the *text* or the *background* is
    the saturated colour. (A stroke-concentration vote fails for light text on a
    coloured field — e.g. cream letters on gold — because the chroma response lands
    on the field side of the edge, not the near-neutral letters.) When the two
    clusters are close in size the result is ambiguous, so fall back to the
    stroke-overlap vote as a tiebreaker. Clustering captures anti-aliased glyph
    edges a luminance threshold would drop."""
    L, a, b = _lab(rgb_bbox)
    feats = np.stack([L.ravel() * 0.6, a.ravel(), b.ravel()], axis=1).astype(np.float32)
    crit = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 10, 1.0)
    try:
        _, labels, _ = cv2.kmeans(feats, 2, None, crit, 3, cv2.KMEANS_PP_CENTERS)
    except cv2.error:
        return None
    labels = labels.reshape(L.shape)
    s = stroke_bbox.astype(bool)
    if not s.any():
        return None
    counts = [int((labels == 0).sum()), int((labels == 1).sum())]
    if max(counts) >= 1.5 * max(1, min(counts)):
        ink_label = int(np.argmin(counts))            # minority cluster = the glyphs
    else:                                             # sizes too close → tiebreak
        score = [(s & (labels == c)).sum() / max(1, counts[c]) for c in (0, 1)]
        ink_label = int(np.argmax(score))
    return labels == ink_label


def _ellipse(r: int):
    return cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * r + 1, 2 * r + 1))


# Target luminance the local field around rescued glyphs is lifted to, so it screens
# to only sparse dots (≈8% at L=224) and black text keeps clean breathing room. WCAG
# contrast on a *uniform* fill is met far darker than this (black on L=145 is already
# 6.7:1), but a halftone is NOT uniform — its dots are the same ink as the glyphs —
# so the binding constraint is dot density, which is why the field must be lifted
# this light even though its nominal contrast was already "compliant".
ROBUST_FIELD_LUMA = 224
# ...but lift the field ONLY when it would otherwise screen too dense for black text.
# A field already light enough (a cyan sign ~163 → ~24% dots) carries solid black on
# its natural halftone and is left untouched — lifting it would just paint an ugly
# bright halo. Only a denser field (a gold sign ~145 → ~30% dots) is lifted.
ROBUST_MAX_FIELD_DARKNESS = 0.27
# Above this predicted darkness the field is too dark for any solid-black treatment
# to read, and — because genuine light-on-dark text keeps high luminance contrast and
# so is never detected here — such detections are almost always photo-chroma noise.
# Reject them rather than lighten a patch of the photo.
ROBUST_REJECT_DARKNESS = 0.45


def _halftone_darkness(luma: float, gamma: float = 0.64) -> float:
    """Predicted black-dot fraction of a flat tone after the screen — the tone
    curve lightens mid-tones to fight dot gain, then the screen lays dots ∝ how
    dark it still is. Used to reject fields too dark to carry text."""
    return 1.0 - (max(0.0, luma) / 255.0) ** gamma


def apply_robust_image_text(rgb: np.ndarray, vdpi: int,
                            mode: str, text_binarize: str, stroke: float = 0.15,
                            skip_mask: np.ndarray = None):
    """Identify washout-prone / baked-in image text on the COLOUR render and recolor
    it SOLID BLACK or SOLID WHITE *in colour, before the grayscale conversion*, then
    leave the rest of the image to halftone normally. Four steps that mirror a
    contrast-accessibility check: (1) **identify the text** on the colour image as
    the union of a chroma detector (light/low-luminance coloured text — e.g. cream
    "VILLA" on teal) and a luminance detector (darker text — e.g. "SOL"), because on
    a glossy sign one word reads by colour and the next by brightness; (2) **identify
    its field** (the local background tone around the glyphs); (3) **fix the
    contrast** — pick a polarity from the field luminance and paint the glyphs into
    the colour buffer: SOLID BLACK on a light/mid field (lifting a too-dense mid
    field light so it screens sparse) or SOLID WHITE on a genuinely dark field
    (knockout, over a uniformly darkened field); (4) leave the rest to **halftone
    normally**. Returns (rgb_out, keep_mask, info) — the caller derives grayscale
    from rgb_out, so the recolour provably precedes the grayscale conversion.

    Two field treatments back the black path's lift and the white path's knockout. A
    halftone of a mid-tone field is a dense field of black dots — the *same ink* as
    black glyphs — so even though the field's nominal WCAG contrast is fine, the
    screen crowds the strokes; lifting the local field light enough to screen sparse
    restores breathing room while keeping it on the halftone path (a light,
    lightly-textured sign, not a stark white plate). A genuinely dark field can't be
    lifted without punching a bright hole in the photo, so there the polarity flips
    to white-on-dark knockout over a uniformly darkened field instead. Text already
    on a near-white field is skipped (the binarizer renders document text crisp).

    A region is committed only if its segmentation looks like text (ink coverage in
    a sane band) and the result verifies as legible in the actual 1-bit output;
    regions that fail are reverted. (`stroke` is retained for call compatibility
    and currently unused.)"""
    h0, w0 = rgb.shape[:2]
    info = {"mode": mode, "regions_detected": 0, "regions_recovered": 0,
            "regions_unrecovered": 0, "regions_rejected": 0, "details": []}
    if mode == "off":
        return rgb, np.zeros((h0, w0), bool), info

    # Luminance of the COLOUR render — used for the luminance text detector and for
    # measuring each region's field tone. The chroma detector reads `rgb` directly.
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    # Candidate text = the union of the CHROMA detector (light/low-luminance
    # coloured text — e.g. cream "VILLA" on teal) and the LUMINANCE detector
    # (darker baked-in text — e.g. "SOL", "PRESTIGE ESTATES"). The two are
    # complementary: on a glossy sign one word can read by colour and the next by
    # brightness, so neither alone covers the whole line.
    wmask = (washout_text_mask(rgb, vdpi, aggressive=(mode == "on"))
             | text_in_image_mask(gray, vdpi))
    if skip_mask is not None and skip_mask.any():
        # Text already recovered by OCR (re-typeset crisply) — don't re-detect and
        # re-process it here. Dilate a little so glyph fringes are excluded too.
        wmask &= ~cv2.dilate(skip_mask.astype(np.uint8),
                             _ellipse(max(2, vdpi // 40))).astype(bool)
    keep = np.zeros((h0, w0), bool)
    if not wmask.any():
        return rgb, keep, info

    n, labels, stats, _ = cv2.connectedComponentsWithStats(
        wmask.astype(np.uint8), 8)

    def _ink_frac(buf, sel=None):
        bw = binarize_text(buf, text_binarize, vdpi)
        s = bw if sel is None else bw[sel]
        return float((s == 0).mean()) if s.size else 0.0

    # Pass 1: validate each candidate and decide its polarity + field treatment.
    H, W = gray.shape
    # (x0,y0,x1,y1, ink, label_r, polarity, field_op, vmarg, cover, before, bg_luma)
    #   polarity: 'black' | 'white'   field_op: 'none' | 'lift' | 'darken'
    candidates = []
    for i in range(1, n):
        x, y, w, h, _ = stats[i]
        # Pad generously (≈ half the line height) so the crop contains real
        # background around the glyphs — k=2 clustering needs both classes present
        # to separate letters from their field rather than letter-edge from body.
        pad = max(int(vdpi / 60), h // 2)
        y0, y1 = max(0, y - pad), min(H, y + h + pad)
        x0, x1 = max(0, x - pad), min(W, x + w + pad)
        comp = (labels[y0:y1, x0:x1] == i)
        ink = _segment_ink(rgb[y0:y1, x0:x1], comp)
        if ink is None:
            continue
        # Reject regions whose segmentation isn't text-like: real glyph rows cover
        # a modest fraction of their plate. Too little = noise; too much = a solid
        # color patch, not letters. This filters photo false positives.
        cover = float(ink.mean())
        if not (0.04 <= cover <= 0.55):
            info["regions_rejected"] += 1
            continue
        gcrop = gray[y0:y1, x0:x1]
        ink_u8 = ink.astype(np.uint8)
        # Background tone right around the glyphs (excluding the glyphs).
        near = cv2.dilate(ink_u8, _ellipse(max(3, h // 2))).astype(bool) & ~ink
        bg_vals = gcrop[near] if near.any() else gcrop[~ink]
        bg_luma = float(np.median(bg_vals)) if bg_vals.size else 255.0
        if bg_luma >= 228:
            # Text already on a (near-)white field: the binarizer renders it crisp
            # black-on-white on its own and won't halftone it. Robust-text adds
            # nothing here (this is ordinary document text), so skip it.
            continue
        darkness = _halftone_darkness(bg_luma)
        if darkness > ROBUST_REJECT_DARKNESS:
            # Genuinely dark field. The signal (chroma/luminance) detectors cannot
            # tell dark-SIGN text from dark-PHOTO noise (car windows, shadows): both
            # span the same luma/texture range, so flipping these to white knockout
            # paints black slabs over the photo. White-on-dark recovery is therefore
            # left to the OCR path, where actual word recognition gates it. Reject
            # here rather than punch a hole in the photo.
            info["regions_rejected"] += 1
            continue
        # Radius of the lifted field carried around the glyphs, scaled to glyph
        # height so every letter gets clean breathing room.
        label_r = max(3, int(round(h * 0.45)), int(round(vdpi * 0.02)))
        # Verify over the glyphs plus a generous margin so dense/bold text isn't
        # mis-scored as "too much ink" (a tight zone over-counts the strokes).
        vmarg = cv2.dilate(ink_u8, _ellipse(max(3, int(round(h * 0.6))))).astype(bool)
        before = _ink_frac(gcrop, vmarg)
        # Solid black glyphs; lift the field light ONLY if it would otherwise screen
        # too dense for black text (a mid-dense field), so the screen can't crowd
        # the strokes. (White-on-dark knockout lives on the OCR path; see above.)
        field_op = "lift" if darkness > ROBUST_MAX_FIELD_DARKNESS else "none"
        candidates.append((x0, y0, x1, y1, ink, label_r, "black", field_op,
                           vmarg, cover, before, bg_luma))

    if not candidates:
        return rgb, keep, info

    def _composite(selected):
        """Paint each region into a copy of the COLOUR image: glyphs to solid black
        or solid white, and the local field lifted light (so a mid-dense field
        screens to sparse dots and black text keeps breathing room) or darkened (so
        white knockout reads). Returns (rgb_out, keep_mask). keep_mask = glyphs
        (always) + any darkened field, so the halftone step skips them; a *lifted*
        field stays on the halftone path and reads as a light, textured sign."""
        black_ink = np.zeros((H, W), bool)
        white_ink = np.zeros((H, W), bool)
        light_field = np.zeros((H, W), bool)
        dark_field = np.zeros((H, W), bool)
        for x0, y0, x1, y1, ink, label_r, polarity, field_op, *_ in selected:
            sub = np.zeros((H, W), bool)
            sub[y0:y1, x0:x1] = ink
            (white_ink if polarity == "white" else black_ink)[sub] = True
            if field_op in ("lift", "darken"):
                field = (cv2.dilate(sub.astype(np.uint8),
                                    _ellipse(label_r)).astype(bool) & ~sub)
                (light_field if field_op == "lift" else dark_field)[field] = True
        glyphs = black_ink | white_ink
        light_field &= ~glyphs
        dark_field &= ~glyphs & ~light_field
        o = rgb.copy()
        o[light_field] = np.maximum(o[light_field], ROBUST_FIELD_LUMA)  # lift mid field
        o[dark_field] = np.minimum(o[dark_field], 28)                   # darken for knockout
        o[black_ink] = 0                                                # solid-black glyphs
        o[white_ink] = 255                                              # solid-white knockout
        return o, glyphs | dark_field   # glyphs + solid dark field skip the halftone

    # Composite, verify with the actual 1-bit result, and revert any region whose
    # glyphs don't come out legible (so the self-check gates the change).
    out, keep = _composite(candidates)
    passed, results = [], []
    for cand in candidates:
        x0, y0, x1, y1, ink, label_r, polarity, field_op, vmarg, cover, before, bg_luma = cand
        after = _ink_frac(cv2.cvtColor(out[y0:y1, x0:x1], cv2.COLOR_RGB2GRAY), vmarg)
        # Committed regions are legible by construction. The ink check binarizes the
        # surround, so it over-counts; use it only as a gross-failure guard:
        #  - black: revert if the region collapsed to a near-solid black slab.
        #  - white: revert only if nothing white survived (region went fully black).
        legible = (after <= 0.9) if polarity == "black" else (after <= 0.985)
        if legible:
            passed.append(cand)
        results.append((x0, y0, x1, y1, cover, before, after, bg_luma, polarity, legible))

    if len(passed) != len(candidates):
        out, keep = _composite(passed)        # recomposite without the failed ones

    for x0, y0, x1, y1, cover, before, after, bg_luma, polarity, legible in results:
        if legible:
            info["regions_recovered"] += 1
        else:
            info["regions_unrecovered"] += 1   # detected but reverted (left untouched)
        info["details"].append({
            "bbox": [int(x0), int(y0), int(x1), int(y1)],
            "ink_before": round(before, 3), "ink_after": round(after, 3),
            "cover": round(cover, 3), "bg_luma": round(bg_luma, 1),
            "polarity": polarity, "legible": bool(legible)})
    info["regions_detected"] = len(results)
    return out, keep, info


# --------------------------------------------------------------------------- #
# OCR text recovery — recognise baked-in text, recolor its ORIGINAL glyphs      #
# --------------------------------------------------------------------------- #
def _rel_luminance(rgb):
    """WCAG relative luminance of an sRGB colour (array last-axis = RGB, 0–255)."""
    c = np.asarray(rgb, np.float64) / 255.0
    c = np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)
    return 0.2126 * c[..., 0] + 0.7152 * c[..., 1] + 0.0722 * c[..., 2]


def _wcag_contrast(rgb1, rgb2) -> float:
    """WCAG 2.x contrast ratio between two sRGB colours (1.0 … 21.0)."""
    l1, l2 = float(_rel_luminance(rgb1)), float(_rel_luminance(rgb2))
    hi, lo = max(l1, l2), min(l1, l2)
    return (hi + 0.05) / (lo + 0.05)


def _segment_word(rgb: np.ndarray, gray: np.ndarray, quad: np.ndarray):
    """Use the OCR box only to LOCATE a word, then segment its ORIGINAL glyph
    pixels (preserving the real letterforms). Returns (ink_full, bbox, field_gray,
    field_rgb, contrast) or None — *no* recoloring, so the caller can apply ONE
    consistent treatment across all the words on a sign.

    The OCR box's PADDING is, by construction, field: take the field colour from
    that border ring and call any pixel inside the box that differs from it (in full
    LAB) a glyph. This is robust to the glyph/field area ratio, unlike a 2-means
    split which can invert on a tight box."""
    H, W = gray.shape
    q = np.asarray(quad, np.float32)
    bh = max(q[:, 1].max() - q[:, 1].min(), 1.0)
    pad = max(4, int(bh * 0.4))
    y0 = max(0, int(q[:, 1].min()) - pad); y1 = min(H, int(q[:, 1].max()) + pad)
    x0 = max(0, int(q[:, 0].min()) - pad); x1 = min(W, int(q[:, 0].max()) + pad)
    if y1 - y0 < 6 or x1 - x0 < 10:
        return None
    crgb = rgb[y0:y1, x0:x1]
    cgray = gray[y0:y1, x0:x1]
    hc, wc = cgray.shape
    lab = cv2.cvtColor(crgb, cv2.COLOR_RGB2LAB).astype(np.float32)
    b = max(2, pad // 2)
    ring = np.zeros((hc, wc), bool)
    ring[:b, :] = ring[-b:, :] = ring[:, :b] = ring[:, -b:] = True
    field_lab = np.median(lab[ring].reshape(-1, 3), axis=0)
    dist = np.linalg.norm(lab - field_lab, axis=2)
    box = np.zeros((hc, wc), np.uint8)
    cv2.fillConvexPoly(box, (q - [x0, y0]).astype(np.int32), 1)
    box = box.astype(bool)
    inbox = dist[box]
    if inbox.size == 0:
        return None
    thr = max(9.0, float(np.percentile(inbox, 60)))   # glyphs are the high-distance tail
    ink = (dist > thr) & box
    ink = cv2.morphologyEx(ink.astype(np.uint8), cv2.MORPH_OPEN,
                           _ellipse(1)).astype(bool)
    cover = float(ink[box].mean()) if box.any() else 0.0
    if ink.sum() < 12 or not (0.03 <= cover <= 0.7):
        return None
    field_gray = float(np.median(cgray[ring]))
    field_rgb = crgb[ring].reshape(-1, 3).mean(0)
    contrast = _wcag_contrast(crgb[ink].reshape(-1, 3).mean(0), field_rgb)
    ink_full = np.zeros((H, W), bool)
    ink_full[y0:y1, x0:x1] = ink
    return ink_full, (x0, y0, x1, y1), field_gray, round(contrast, 2)


def apply_ocr_text(rgb: np.ndarray, vdpi: int,
                   region_mask: np.ndarray, conf_min: float = 0.5):
    """Recognise text inside `region_mask` (the photo/image area) and recolor each
    word's ORIGINAL glyph pixels for contrast — recovering text the signal path
    can't (a sign word in a specular highlight has near-zero contrast in grayscale,
    but still reads to a recogniser on the colour image, and its glyphs are still
    separable *by colour*, so the real letterforms can be recoloured, not retyped).

    The recolor is decided **per sign, not per word**: words are grouped into signs
    by proximity, the field tone is taken once across the whole group, and ALL its
    glyphs get the same polarity (solid black on a light/mid field, solid white on a
    dark one) and the same field treatment (one uniform lift if the field would
    screen too dense — never a separate blob per line). Identification and recolor
    happen on the COLOUR image, before the grayscale conversion.

    Scoped to image regions (never crisp vector document text) and to coloured
    fields (not text on white). Returns (rgb_out, fixed_mask, word_region, info);
    `info['words']` lists each string with its confidence and measured WCAG
    contrast, so the result can be verified (OCR can misread)."""
    import ocr_text
    info = {"engine": ocr_text.engine_name(), "words": []}
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)     # luminance of the COLOUR render
    fixed = np.zeros(gray.shape, bool)
    region = np.zeros(gray.shape, np.uint8)          # whole word areas (for robust skip)
    if region_mask is None or not region_mask.any():
        return rgb, fixed, region.astype(bool), info
    ys, xs = np.where(region_mask)
    y0, y1, x0, x1 = int(ys.min()), int(ys.max()) + 1, int(xs.min()), int(xs.max()) + 1
    crop = rgb[y0:y1, x0:x1]
    # Upscale for OCR: a fax-DPI render makes signage text small, and the recogniser
    # drops word spaces at small sizes; 1.5–2× restores cleaner boxes.
    hc, wc = crop.shape[:2]
    scale = max(1.0, min(2.0, 2600.0 / max(hc, wc, 1)))
    if scale > 1.01:
        crop = cv2.resize(crop, None, fx=scale, fy=scale,
                          interpolation=cv2.INTER_CUBIC)
    words = ocr_text.recognize(crop, conf_min)

    # Pass 1: validate + segment each word (no recolor yet).
    cands = []   # (text, conf, ink_full, bbox, field_gray)
    for text, quad, conf in words:
        quad = quad / scale + np.array([x0, y0], np.float32)   # → full-image coords
        iy, ix = int(round(quad[:, 1].mean())), int(round(quad[:, 0].mean()))
        if not (0 <= iy < gray.shape[0] and 0 <= ix < gray.shape[1]):
            continue
        if not region_mask[iy, ix]:                  # centre must be in a photo
            continue
        # Only touch text on a COLOURED/photo field — crisp document text on a
        # near-white field is already rendered perfectly by the binarizer.
        qb = quad.astype(int)
        p = gray[max(0, qb[:, 1].min()):qb[:, 1].max() + 1,
                 max(0, qb[:, 0].min()):qb[:, 0].max() + 1]
        if p.size and float(np.median(p)) >= 222:
            continue
        seg = _segment_word(rgb, gray, quad)
        if seg is None:
            continue
        ink_full, bbox, field_gray, contrast = seg
        cands.append((text, conf, contrast, ink_full, bbox, field_gray))

    if not cands:
        return rgb, fixed, region.astype(bool), info

    # Group words into signs by proximity (so the same background gets one
    # treatment). Dilate the word boxes by ~a line height and connect them.
    H, W = gray.shape
    bhs = [c[4][3] - c[4][1] for c in cands]
    link = max(8, int(np.median(bhs) * 0.9))
    gm = np.zeros((H, W), np.uint8)
    for *_x, bbox, _f in cands:
        bx0, by0, bx1, by1 = bbox
        gm[by0:by1, bx0:bx1] = 1
    gm = cv2.dilate(gm, _ellipse(link))
    n, lbl = cv2.connectedComponents(gm, 8)
    groups = {}
    for c in cands:
        bx0, by0, bx1, by1 = c[4]
        groups.setdefault(int(lbl[(by0 + by1) // 2, (bx0 + bx1) // 2]), []).append(c)

    out = rgb.copy()       # recolor IN COLOUR; caller derives grayscale afterwards
    for grp in groups.values():
        sign_field = float(np.median([c[5] for c in grp]))   # one field tone per sign
        black = sign_field >= 110                            # light/mid → black, else white
        dense = _halftone_darkness(sign_field) > ROBUST_MAX_FIELD_DARKNESS
        allink = np.zeros((H, W), bool)
        for c in grp:
            allink |= c[3]
        # ONE consistent field treatment for the whole sign.
        if black:
            if dense:    # uniform light lift of the sign's text field (not per-line blobs)
                lr = max(3, int(np.median([c[4][3] - c[4][1] for c in grp]) * 0.45))
                fz = cv2.dilate(allink.astype(np.uint8),
                                _ellipse(lr)).astype(bool) & ~allink
                out[fz] = np.maximum(out[fz], ROBUST_FIELD_LUMA)
            out[allink] = 0
        else:            # dark sign → white text on a uniformly darkened field
            lr = max(3, int(np.median([c[4][3] - c[4][1] for c in grp]) * 0.45))
            fz = cv2.dilate(allink.astype(np.uint8),
                            _ellipse(lr)).astype(bool) & ~allink
            out[fz] = np.minimum(out[fz], 28)
            fixed |= fz
            out[allink] = 255
        fixed |= allink
        for text, conf, contrast, _ink, bbox, _f in grp:
            info["words"].append({"text": text, "conf": round(conf, 3),
                                  "wcag_contrast": contrast})
            bx0, by0, bx1, by1 = bbox
            region[by0:by1, bx0:bx1] = 1
    return out, fixed, region.astype(bool), info  # out is the recoloured COLOUR image


def _solid_fill_mask(g: np.ndarray, mean: np.ndarray,
                     dark_level: float = 115.0,
                     bright: float = 160.0) -> np.ndarray:
    """Carry solid color-fill areas across as solid black.

    Adaptive binarizers (Sauvola/Niblack/...) misfire on large dark fills — e.g.
    a reverse-type header bar: inside the fill the local contrast term drives the
    threshold below the fill level, so the fill flips to white and only the glyph
    edges survive as a stroke. Where the *local mean* is dark (a filled region —
    using the mean rather than the per-pixel value rides out rasterization noise
    that a flat-std test trips over), keep the area solid black; only genuinely
    bright pixels (reverse/knockout text, `g >= bright`) are left white. Body text
    on a white page is unaffected: its local mean stays light."""
    return (mean < dark_level) & (g < bright)


# --- Photo pre-conditioning (applied before the halftone, photo region only) - #
# Effective dot gain on a 1-bit channel is large: without correction midtones
# plug to solid black and a photo arrives as a silhouette. Screens gain more
# than error diffusion, so the correction is calibrated per family. gamma<1
# lifts midtones (counteracts the darkening).
_TONE_GAMMA = {
    "clustered": 0.62, "ordered": 0.66, "blue-noise": 0.70, "green-noise": 0.66,
    "floyd": 0.85, "atkinson": 0.82, "jarvis": 0.85, "stucki": 0.85,
    "sierra": 0.85, "edd": 0.82, "line": 0.90,
}


def apply_tone_curve(gray: np.ndarray, dither_name: str, mode: str) -> np.ndarray:
    """Per-family dot-gain pre-correction. mode: 'auto' | 'none'."""
    if mode == "none":
        return gray
    gamma = _TONE_GAMMA.get(dither_name)
    if not gamma:
        return gray
    lut = (((np.arange(256) / 255.0) ** gamma) * 255.0).astype(np.uint8)
    return lut[gray]


def pre_sharpen(gray: np.ndarray, vdpi: int, amount: float = 0.7) -> np.ndarray:
    """Edge-aware unsharp mask to counter the channel's MTF rolloff."""
    blur = cv2.GaussianBlur(gray, (0, 0), sigmaX=1.0)
    out = gray.astype(np.float32) + amount * (gray.astype(np.float32)
                                              - blur.astype(np.float32))
    return np.clip(out, 0, 255).astype(np.uint8)


def _aniso_tile(tile: np.ndarray, hdpi: int, vdpi: int) -> np.ndarray:
    """Resample a screen/threshold tile to the device pixel aspect so a dot is
    round *on paper* (pixels are non-square: standard 2:1, fine 1:1, superfine
    ~1:2). Vertical scale = vdpi/hdpi."""
    scale = vdpi / float(hdpi)
    th = max(2, int(round(tile.shape[0] * scale)))
    if th == tile.shape[0]:
        return tile
    return cv2.resize(tile, (tile.shape[1], th), interpolation=cv2.INTER_NEAREST)


def _tile_to(thr: np.ndarray, shape) -> np.ndarray:
    th, tw = thr.shape
    tiled = np.tile(thr, (shape[0] // th + 1, shape[1] // tw + 1))
    return tiled[:shape[0], :shape[1]]


def dither_floyd(gray: np.ndarray) -> np.ndarray:
    """Floyd-Steinberg via Pillow's fast C implementation."""
    return np.asarray(Image.fromarray(gray, "L").convert("1")).astype(np.uint8) * 255


# Error-diffusion kernels as (divisor, [(dy, dx, weight), ...]). Atkinson
# deliberately diffuses only 6/8 of the error (cleaner whites, less speckle).
ED_KERNELS = {
    "atkinson": (8, [(0, 1, 1), (0, 2, 1),
                     (1, -1, 1), (1, 0, 1), (1, 1, 1),
                     (2, 0, 1)]),
    "jarvis": (48, [(0, 1, 7), (0, 2, 5),
                    (1, -2, 3), (1, -1, 5), (1, 0, 7), (1, 1, 5), (1, 2, 3),
                    (2, -2, 1), (2, -1, 3), (2, 0, 5), (2, 1, 3), (2, 2, 1)]),
    "stucki": (42, [(0, 1, 8), (0, 2, 4),
                    (1, -2, 2), (1, -1, 4), (1, 0, 8), (1, 1, 4), (1, 2, 2),
                    (2, -2, 1), (2, -1, 2), (2, 0, 4), (2, 1, 2), (2, 2, 1)]),
    "sierra": (32, [(0, 1, 5), (0, 2, 3),
                    (1, -2, 2), (1, -1, 4), (1, 0, 5), (1, 1, 4), (1, 2, 2),
                    (2, -1, 2), (2, 0, 3), (2, 1, 2)]),
}


def error_diffuse(gray: np.ndarray, kernel: str) -> np.ndarray:
    """Generic serpentine error diffusion for the named kernel (see ED_KERNELS).

    Serpentine (boustrophedon) scanning alternates row direction, which breaks
    up the directional "worm" artifacts that a one-way raster produces.
    """
    div, taps = ED_KERNELS[kernel]
    img = gray.astype(np.float32)
    h, w = img.shape
    for y in range(h):
        if y % 2 == 0:
            xs, flip = range(w), 1
        else:
            xs, flip = range(w - 1, -1, -1), -1
        for x in xs:
            old = img[y, x]
            new = 255.0 if old >= 128 else 0.0
            err = (old - new) / div
            img[y, x] = new
            for dy, dx, wt in taps:
                ny, nx = y + dy, x + dx * flip
                if 0 <= ny < h and 0 <= nx < w:
                    img[ny, nx] += err * wt
    return (img >= 128).astype(np.uint8) * 255


def dither_atkinson(gray: np.ndarray) -> np.ndarray:
    return error_diffuse(gray, "atkinson")


def dither_ordered(gray: np.ndarray, n: int = 8, hdpi: int = 204,
                   vdpi: int = 196) -> np.ndarray:
    """Bayer ordered dithering (dispersed-dot), anisotropy-corrected."""
    base = np.array([[0, 2], [3, 1]], dtype=np.float32)
    m = base
    while m.shape[0] < n:
        m = np.block([[4 * m, 4 * m + 2], [4 * m + 3, 4 * m + 1]])
    m = m[:n, :n]
    thr = (m + 0.5) / (m.size) * 255.0
    thr = _aniso_tile(thr, hdpi, vdpi)
    tiled = _tile_to(thr, gray.shape)
    return (gray.astype(np.float32) > tiled).astype(np.uint8) * 255


def dither_clustered(gray: np.ndarray, cell: int = 6, hdpi: int = 204,
                     vdpi: int = 196) -> np.ndarray:
    """Clustered-dot (AM) screening. Dots grow in a cluster, producing long runs
    that compress far better and survive a noisy line. `cell` is scaled from the
    fax dpi by the caller so the screen doesn't collapse after re-thresholding,
    and the tile is resampled to the device aspect so dots stay round on paper."""
    # spiral-ordered threshold within a cell -> growth from center outward
    yy, xx = np.mgrid[0:cell, 0:cell]
    cx = cy = (cell - 1) / 2.0
    dist = np.sqrt((xx - cx) ** 2 + (yy - cy) ** 2)
    order = dist.argsort(axis=None).argsort().reshape(cell, cell)
    thr = (order + 0.5) / (cell * cell) * 255.0
    thr = _aniso_tile(thr.astype(np.float32), hdpi, vdpi)
    tiled = _tile_to(thr, gray.shape)
    return (gray.astype(np.float32) > tiled).astype(np.uint8) * 255


# --- Blue-noise (void-and-cluster) FM screening -------------------------------
# Ulichney's void-and-cluster method builds an isotropic ("blue noise") threshold
# matrix: no directional structure, no clustered low-frequency blotches. We
# generate one 64x64 tile once and cache it as an asset (numpy-only, no scipy).
_BLUE_NOISE = None


def _gaussian_toroidal(arr: np.ndarray, sigma: float) -> np.ndarray:
    """Wrap-around (tileable) Gaussian filter via FFT, matching scipy's
    fourier_gaussian semantics so void/cluster ranking is stable."""
    fy = np.fft.fftfreq(arr.shape[0])[:, None]
    fx = np.fft.fftfreq(arr.shape[1])[None, :]
    g = np.exp(-2.0 * (np.pi ** 2) * (sigma ** 2) * (fx ** 2 + fy ** 2))
    return np.fft.ifft2(np.fft.fft2(arr) * g).real


def _largest_void(binary: np.ndarray, sigma: float) -> int:
    bp = binary if (np.count_nonzero(binary) * 2 < binary.size) else ~binary
    f = _gaussian_toroidal(np.where(bp, 1.0, 0.0), sigma)
    return int(np.argmin(np.where(bp, 2.0, f)))


def _tightest_cluster(binary: np.ndarray, sigma: float) -> int:
    bp = binary if (np.count_nonzero(binary) * 2 < binary.size) else ~binary
    f = _gaussian_toroidal(np.where(bp, 1.0, 0.0), sigma)
    return int(np.argmax(np.where(bp, f, -1.0)))


def generate_blue_noise(shape=(64, 64), sigma=1.9, seed=0, frac=0.1) -> np.ndarray:
    """Void-and-cluster blue-noise dither array (Ulichney 1993). Returns an int
    array containing each rank 0..N-1 exactly once."""
    rng = np.random.default_rng(seed)
    n = int(np.prod(shape))
    n_one = max(1, min(int((n - 1) / 2), int(n * frac)))
    binary = np.zeros(shape, dtype=bool)
    binary.flat[rng.permutation(n)[:n_one]] = True
    while True:
        c = _tightest_cluster(binary, sigma)
        binary.flat[c] = False
        v = _largest_void(binary, sigma)
        if v == c:
            binary.flat[c] = True
            break
        binary.flat[v] = True
    dither = np.zeros(shape, dtype=np.int64)
    bp = binary.copy()
    for rank in range(n_one - 1, -1, -1):          # phase 1
        c = _tightest_cluster(bp, sigma)
        bp.flat[c] = False
        dither.flat[c] = rank
    bp = binary.copy()
    for rank in range(n_one, (n + 1) // 2):        # phase 2
        v = _largest_void(bp, sigma)
        bp.flat[v] = True
        dither.flat[v] = rank
    for rank in range((n + 1) // 2, n):            # phase 3
        c = _tightest_cluster(bp, sigma)
        bp.flat[c] = True
        dither.flat[c] = rank
    return dither


# Cache of generated void-and-cluster matrices, keyed by sigma. Blue noise uses
# a small sigma (isotropic, max detail); larger sigma clumps the minority pixels
# into mid-size aggregates -> "green noise" (the AM<->FM hybrid of section 1.1).
_VC_CACHE = {}


def _vc_matrix(sigma: float, tag: str) -> np.ndarray:
    key = round(sigma, 2)
    if key in _VC_CACHE:
        return _VC_CACHE[key]
    assets = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "..", "assets")
    asset = os.path.join(assets, f"{tag}_64_s{key}.npy")
    try:
        m = np.load(asset)
    except Exception:
        m = generate_blue_noise((64, 64), sigma=sigma, seed=0)
        try:
            os.makedirs(assets, exist_ok=True)
            np.save(asset, m)
        except Exception:
            pass
    _VC_CACHE[key] = m
    return m


def _blue_noise_matrix() -> np.ndarray:
    global _BLUE_NOISE
    if _BLUE_NOISE is not None:
        return _BLUE_NOISE
    legacy = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "..", "assets", "bluenoise_64.npy")
    try:
        _BLUE_NOISE = np.load(legacy)
    except Exception:
        _BLUE_NOISE = _vc_matrix(1.9, "bluenoise")
    return _BLUE_NOISE


def _screen_from_matrix(gray, m, hdpi, vdpi) -> np.ndarray:
    thr = (m.astype(np.float32) + 0.5) / m.size * 255.0
    thr = _aniso_tile(thr, hdpi, vdpi)
    tiled = _tile_to(thr, gray.shape)
    return (gray.astype(np.float32) > tiled).astype(np.uint8) * 255


def dither_blue_noise(gray: np.ndarray, hdpi: int = 204,
                      vdpi: int = 196) -> np.ndarray:
    return _screen_from_matrix(gray, _blue_noise_matrix(), hdpi, vdpi)


def dither_green_noise(gray: np.ndarray, coarseness: float = 4.0,
                       hdpi: int = 204, vdpi: int = 196) -> np.ndarray:
    """Green-noise / hybrid AM-FM screening (section 1.1). Built by running
    void-and-cluster with a larger Gaussian sigma so minority pixels coalesce
    into mid-size clusters: a tunable point between blue-noise detail and
    clustered-dot run-length/robustness. `coarseness` ~2 (near blue) .. 8 (near
    clustered)."""
    sigma = max(2.0, min(8.0, coarseness))
    return _screen_from_matrix(gray, _vc_matrix(sigma, "greennoise"), hdpi, vdpi)


def dither_edd(gray: np.ndarray, lam: float = 0.4, vdpi: int = 196) -> np.ndarray:
    """Edge-enhancing error diffusion (Eschbach, section 2.3): add a high-pass
    term before diffusing so glyph/edge structure survives while tone still
    diffuses. Good for text sitting over a photographic background."""
    sharp = pre_sharpen(gray, vdpi, amount=lam * 2.0)
    return error_diffuse(sharp, "atkinson")


def dither_line(gray: np.ndarray, hdpi: int = 204, vdpi: int = 196,
                period: int = 0) -> np.ndarray:
    """Horizontal line-screen halftone (woodcut / engraving look).

    Tone is rendered as horizontal stripes whose *thickness* grows with
    darkness: a triangular threshold profile across one vertical period is high
    at the line center (so even highlights keep a hairline) and low at the
    edges (so only deep shadows fill the gap). Because the lines run *along the
    scanline*, the result is almost entirely long horizontal black/white runs —
    the single most G4-friendly way to carry a continuous-tone image over a fax
    line, while reading as a clean engraving rather than mud. The period is
    derived from the vertical DPI (so stripe pitch is constant on paper)."""
    if period <= 0:
        # ~18 lines/inch: a bold, unmistakable woodcut pitch that survives both
        # the fax channel and being downscaled into a preview.
        period = max(4, int(round(vdpi / 18.0)))
    # Triangular threshold profile across one period. A high peak (250) keeps a
    # hairline even in highlights; a low edge (12) lets only deep shadows fill
    # the gap to near-solid. Black where gray <= thr, so the dark stripe THICKENS
    # smoothly as the local tone darkens — a line-conversion / engraving screen.
    y = np.arange(period, dtype=np.float32)
    center = (period - 1) / 2.0
    d = np.abs(y - center) / max(center, 1e-6)      # 0 at center .. 1 at edge
    peak, edge = 250.0, 12.0
    thr = peak - (peak - edge) * d
    tiled = _tile_to(thr.reshape(period, 1), gray.shape)
    return (gray.astype(np.float32) > tiled).astype(np.uint8) * 255


# The curated halftone technologies offered in the comparison preview, spanning
# the design space: AM screening, AM-FM hybrid, FM/blue-noise, two error-diffusion
# variants, and a horizontal line screen (woodcut). 6-up so the user can pick by
# eye. (ordered/edd/jarvis/stucki/sierra are also selectable via --dither.)
COMPARE_METHODS = ["clustered", "green-noise", "blue-noise", "atkinson",
                   "floyd", "line"]
# When both reference panels (original color + true grayscale) are shown first,
# use four halftone options so the sheet stays a clean 6-up: an AM screen, the
# AM-FM hybrid, an FM/blue-noise stipple, and the woodcut line screen.
COMPARE4_METHODS = ["clustered", "green-noise", "blue-noise", "line"]
# Backwards-compatible alias.
COMPARE5_METHODS = COMPARE4_METHODS
# Backwards-compatible alias for older callers.
TOP5_METHODS = COMPARE_METHODS

HALFTONE_INFO = {
    "clustered": "Clustered-dot AM screening — longest runs, best G4 compression, "
                 "most robust over a noisy line; lowest apparent resolution.",
    "green-noise": "Green-noise hybrid AM-FM — mid-size dot clusters: blue-noise "
                   "detail with clustered-dot run-length/robustness. Tunable via "
                   "coarseness; a strong default for a photo that must survive a "
                   "bad line.",
    "blue-noise": "Void-and-cluster blue noise (FM) — isotropic organic stipple, "
                  "great perceived detail, no directional worms; mid compression.",
    "atkinson": "Atkinson error diffusion — clean whites, crisp thin features; "
                "good detail, looser compression than screening.",
    "floyd": "Floyd-Steinberg error diffusion — classic, maximum detail; "
             "directional speckle is the worst case for G4 size and line noise.",
    "ordered": "Bayer ordered dithering — fast, predictable crosshatch; "
               "middling on both detail and compression.",
    "line": "Horizontal line screen (woodcut/engraving) — tone as horizontal "
            "stripes thickened by darkness; runs along the scanline so G4 size "
            "is excellent and it reads cleanly, high-contrast, never muddy.",
    "edd": "Edge-enhancing error diffusion — high-pass term sharpens edges while "
           "diffusing tone; for text over a photographic background.",
    "jarvis": "Jarvis-Judice-Ninke error diffusion — wide 12-tap kernel, very "
              "smooth tone; heavy speckle, large G4 size.",
    "stucki": "Stucki error diffusion — 12-tap, sharp and smooth for print; "
              "heavy speckle, large G4 size.",
    "sierra": "Sierra error diffusion — Jarvis-like smoothness, a little cheaper.",
    "none": "Hard threshold — no halftone; correct for pure text, line art, "
            "and barcodes/QR codes.",
}

# Accepted --dither aliases mapped to canonical names.
DITHER_ALIASES = {"threshold": "none", "bayer": "ordered", "blue": "blue-noise",
                  "green": "green-noise", "woodcut": "line", "engraving": "line",
                  "lines": "line"}


def recommend_dither(photo_fraction: float, fax_heavy: bool) -> tuple:
    """Suggest the OPTIMAL halftone for this page; returns (method, reason)."""
    if photo_fraction < 0.03:
        return "none", ("Page is essentially text / line art (photo area "
                        f"{photo_fraction * 100:.0f}%); a hard threshold is the "
                        "sharpest and smallest — halftoning would only add noise.")
    if fax_heavy or photo_fraction > 0.45:
        return "clustered", (f"Large photo area ({photo_fraction * 100:.0f}%) or "
                             "fax-heavy mode: clustered-dot keeps runs long, so it "
                             "compresses best and survives a noisy line.")
    return "green-noise", (f"Moderate photo area ({photo_fraction * 100:.0f}%): "
                           "green-noise hybrid keeps blue-noise-level detail but "
                           "clusters dots into longer runs, so it compresses and "
                           "survives the line far better than Floyd/Atkinson. Drop "
                           "to atkinson for the crispest whites on a clean line.")


def choose_dither(name: str, fax_heavy: bool, photo_fraction: float) -> str:
    if name and name != "auto":
        return DITHER_ALIASES.get(name, name)
    return recommend_dither(photo_fraction, fax_heavy)[0]


def halftone(gray: np.ndarray, name: str, hdpi: int, vdpi: int,
             coarseness: float = 4.0) -> np.ndarray:
    name = DITHER_ALIASES.get(name, name)
    if name == "none":
        return threshold_otsu(gray)
    if name == "floyd":
        return dither_floyd(gray)
    if name == "ordered":
        return dither_ordered(gray, hdpi=hdpi, vdpi=vdpi)
    if name == "blue-noise":
        return dither_blue_noise(gray, hdpi=hdpi, vdpi=vdpi)
    if name == "green-noise":
        return dither_green_noise(gray, coarseness=coarseness,
                                  hdpi=hdpi, vdpi=vdpi)
    if name == "edd":
        return dither_edd(gray, vdpi=vdpi)
    if name == "line":
        return dither_line(gray, hdpi=hdpi, vdpi=vdpi)
    if name == "clustered":
        cell = max(4, min(10, round(vdpi / 32)))  # scale screen to dpi
        return dither_clustered(gray, cell=cell, hdpi=hdpi, vdpi=vdpi)
    if name in ED_KERNELS:
        return error_diffuse(gray, name)
    raise ValueError(f"unknown dither: {name}")


# --------------------------------------------------------------------------- #
# Page assembly                                                               #
# --------------------------------------------------------------------------- #
def is_already_bilevel(page: fitz.Page) -> bool:
    """True if the page is a single full-page image that is already 1-bit."""
    try:
        imgs = page.get_images(full=True)
    except Exception:
        return False
    if len(imgs) != 1:
        return False
    xref = imgs[0][0]
    try:
        info = page.parent.extract_image(xref)
    except Exception:
        return False
    return info.get("bpc") == 1


def detect_washout_colors(page: fitz.Page) -> list:
    """Flag wash-out-prone colors present as text fills (yellow/light-blue/etc.)."""
    warns = set()
    try:
        d = page.get_text("dict")
    except Exception:
        return []
    for blk in d.get("blocks", []):
        for line in blk.get("lines", []):
            for span in line.get("spans", []):
                c = span.get("color", 0)
                r, g, b = (c >> 16) & 255, (c >> 8) & 255, c & 255
                lum = 0.299 * r + 0.587 * g + 0.114 * b
                if lum > 180 and (r > 180 and g > 180 and b < 120):
                    warns.add("wash_out_color:yellow")
                elif lum > 190:
                    warns.add("wash_out_color:light")
    return sorted(warns)


def _compute_photo_region(page, gray, opt, hdpi, vdpi):
    """The continuous-tone (photo) region to halftone, before any text-keep masks
    are subtracted. Shared by OCR/robust-text scoping and the final mask."""
    if opt.segmentation == "none":
        return np.zeros_like(gray, dtype=bool)
    if opt.segmentation == "variance":
        return variance_photo_mask(gray)
    mask = photo_region_mask(page, gray.shape, hdpi, vdpi,
                             opt.max_scanline_px, "embedded")
    if not mask.any():
        return variance_photo_mask(gray)
    if mask.mean() > 0.8:
        # A single image covering (nearly) the whole page is a full-page raster (a
        # scan or exported cover sheet), not a photo. Halftoning all of it would
        # dither the document's own text into mush. Find the genuine photo via the
        # variance heuristic and *consolidate* it into a solid region so flat areas
        # inside the photo (a colored sign, sky) keep halftoning while the document's
        # text-on-white areas, outside the photo, are binarized crisp. (Empty result
        # = all text/line art → binarize everything, correct for a text-only scan.)
        vmask = variance_photo_mask(gray)
        if vmask.any():
            vmask = consolidate_photo_region(vmask, vdpi)
        mask = mask & vmask
    return mask


def _prepare_page(page: fitz.Page, opt: FaxOptions):
    """Rasterize + pre-clean a page and compute its photo mask, once.

    Returns (gray, mask, photo_fraction, warnings, already_bilevel, rtext, otext).
    The result is reused both by the real conversion and by the multi-method
    comparison so every halftone option is rendered from an identical start.
    """
    hdpi, vdpi = RESOLUTIONS[opt.resolution]
    warnings: list = []
    rtext: dict = {}
    otext: dict = {}

    if is_already_bilevel(page):
        gray = render_page_gray(page, hdpi, vdpi, opt.max_scanline_px)
        return gray, np.zeros_like(gray, dtype=bool), 0.0, warnings, True, rtext, otext

    # When text recolouring is on (the default), render in COLOUR first and derive
    # the working grayscale from the *recoloured* colour image AFTER identifying and
    # recolouring the text — so the recolour provably precedes the grayscale
    # conversion. Only when both text passes are off do we render grayscale directly.
    color_path = (opt.robust_image_text != "off" or opt.ocr_text != "off")
    if color_path:
        rgb = render_page_color(page, hdpi, vdpi, opt.max_scanline_px)
        base_gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    else:
        rgb = None
        base_gray = render_page_gray(page, hdpi, vdpi, opt.max_scanline_px)

    # The photo region scopes OCR and the final halftone mask. Compute it on a
    # flattened copy (near-white → white) so the document's text-on-white areas
    # read as flat and stay OUT of the photo region — otherwise OCR would see, and
    # re-typeset, the page's own crisp form text. It rides the deskew alongside the
    # text-keep masks. Computed on the pre-recolour luminance (the original page).
    photo = _compute_photo_region(
        page, flatten_background(base_gray) if opt.flatten_bg else base_gray,
        opt, hdpi, vdpi)

    fixed_ocr = np.zeros(base_gray.shape, bool)
    ocr_region = np.zeros(base_gray.shape, bool)
    fixed = np.zeros(base_gray.shape, bool)

    if color_path:
        # (1) Identify text on the COLOUR image and recolour it IN COLOUR.
        # OCR text recovery (optional, FIRST): recognise text baked into the photo
        # and recolour its original glyphs — recovering words the signal path can't
        # (e.g. a sign word in a specular highlight). What OCR recolours is excluded
        # from the robust-text pass below so it isn't reprocessed.
        if opt.ocr_text != "off":
            rgb, fixed_ocr, ocr_region, otext = apply_ocr_text(
                rgb, vdpi, photo, opt.ocr_conf_min)
            if otext.get("words"):
                warnings.append(f"ocr_text:recognized:{len(otext['words'])}")
        # Robust image text: recolour washout-prone coloured text to solid black or
        # solid white (polarity by field tone) directly in the colour buffer.
        if opt.robust_image_text != "off":
            rgb, fixed, rtext = apply_robust_image_text(
                rgb, vdpi, opt.robust_image_text, opt.text_binarize,
                opt.robust_text_stroke, skip_mask=ocr_region)
            if rtext.get("regions_recovered"):
                warnings.append(f"robust_text:recovered:{rtext['regions_recovered']}")
        # (2) NOW convert to grayscale, from the recoloured colour image.
        gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    else:
        gray = base_gray

    if opt.deskew:
        gray, ang, (fixed, fixed_ocr, photo) = deskew_gray(
            gray, [fixed, fixed_ocr, photo])
        if ang:
            warnings.append(f"deskew:{ang:.1f}deg")
    if opt.flatten_bg:
        gray = flatten_background(gray)

    mask = photo
    # Recovered/recolored text is crisp now: route it to the text binarizer, never
    # the halftone, so it can't be screened back into mush.
    keep = fixed | fixed_ocr
    if keep.any():
        mask = mask & ~keep

    photo_fraction = float(mask.mean()) if mask.size else 0.0
    return gray, mask, photo_fraction, warnings, False, rtext, otext


def _apply_dither(gray, mask, dither_name, opt, hdpi, vdpi) -> np.ndarray:
    """Binarize text/line content with the chosen adaptive binarizer, then
    overlay the chosen halftone inside the photo mask only (bounded to its bbox).
    Photo regions get dot-gain pre-correction and optional edge sharpening first,
    so the channel renders the document well rather than just 'fax-ifying' it."""
    text_bw = binarize_text(gray, opt.text_binarize, vdpi)
    dither_name = DITHER_ALIASES.get(dither_name, dither_name)
    if not mask.any() or dither_name == "none":
        return text_bw
    ys, xs = np.where(mask)
    y0, y1, x0, x1 = ys.min(), ys.max() + 1, xs.min(), xs.max() + 1
    sub = gray[y0:y1, x0:x1]
    if opt.sharpen:
        sub = pre_sharpen(sub, vdpi)
    sub = apply_tone_curve(sub, dither_name, opt.tone_curve)
    sub_ht = halftone(sub, dither_name, hdpi, vdpi, opt.green_noise_coarseness)
    if opt.text_in_image:
        # Rescue text baked into the photo: where strokes form text lines, keep
        # the legible binarization instead of the halftone so it stays readable.
        tmask = text_in_image_mask(gray[y0:y1, x0:x1], vdpi)
        if tmask.any():
            sub_ht = np.where(tmask, text_bw[y0:y1, x0:x1], sub_ht)
    bw = text_bw.copy()
    sub_mask = mask[y0:y1, x0:x1]
    region = bw[y0:y1, x0:x1]
    region[sub_mask] = sub_ht[sub_mask]
    bw[y0:y1, x0:x1] = region
    return bw


def process_page(page: fitz.Page, idx: int, opt: FaxOptions) -> tuple[Image.Image, PageReport]:
    rep = PageReport(index=idx)
    hdpi, vdpi = RESOLUTIONS[opt.resolution]
    gray, mask, photo_fraction, warnings, already, rtext, otext = _prepare_page(page, opt)
    rep.warnings.extend(warnings)
    rep.photo_fraction = round(photo_fraction, 4)
    rep.text_binarize = opt.text_binarize
    if rtext.get("regions_detected"):
        rep.robust_text = rtext
    if otext.get("words"):
        rep.ocr_text = otext

    if already:
        rep.already_bilevel = True
        rep.dither = "none"
        return _finalize(threshold_otsu(gray), rep, opt), rep

    rep.photo_regions = int(mask.any())
    dname = choose_dither(opt.dither, opt.fax_heavy, photo_fraction)
    rep.dither = dname
    bw = _apply_dither(gray, mask, dname, opt, hdpi, vdpi)

    if (bw == 0).mean() > 0.45:
        rep.warnings.append("inverted_or_heavy_black")
    rep.warnings.extend(detect_washout_colors(page))
    return _finalize(bw.astype(np.uint8), rep, opt), rep


def _postclean(bw: np.ndarray, opt: FaxOptions) -> np.ndarray:
    if opt.despeckle:
        bw = despeckle_bw(bw)
    if opt.thicken:
        bw = thicken_bw(bw)
    return bw


def _finalize(bw: np.ndarray, rep: PageReport, opt: FaxOptions) -> Image.Image:
    # Pillow 1-bit: 0=black,255=white -> mode '1'
    return Image.fromarray(_postclean(bw, opt)).convert("1")


# --------------------------------------------------------------------------- #
# Encoding / packing                                                          #
# --------------------------------------------------------------------------- #
def encode_g4_tiff(img: Image.Image, path: str) -> int:
    img.save(path, format="TIFF", compression="group4")
    return os.path.getsize(path)


def convert_pdf(in_pdf: str, out_path: str, opt: FaxOptions) -> dict:
    """Convert every page and write a G4 PDF or a Class-F multipage TIFF.
    Returns the report dict."""
    doc = fitz.open(in_pdf)
    tmpdir = tempfile.mkdtemp(prefix="faxopt_")
    tiff_paths, pages = [], []

    for i, page in enumerate(doc, start=1):
        img, rep = process_page(page, i, opt)
        tp = os.path.join(tmpdir, f"p{i:04d}.tif")
        nbytes = encode_g4_tiff(img, tp)
        rep.encoded_bytes = nbytes
        rep.est_transmission_s = round(
            nbytes * 8 / opt.line_rate_bps + opt.page_overhead_s, 1)
        tiff_paths.append(tp)
        pages.append(rep)

    if opt.fmt == "tiff":
        _save_multipage_tiff(tiff_paths, out_path)
    else:
        # img2pdf embeds CCITT G4 losslessly (PDF carries CCITTFaxDecode)
        with open(out_path, "wb") as f:
            f.write(img2pdf.convert(tiff_paths))

    report = {
        "mode": "fax",
        "input": in_pdf,
        "output": out_path,
        "input_bytes": os.path.getsize(in_pdf),
        "output_bytes": os.path.getsize(out_path),
        "pages": [vars(p) for p in pages],
        "total_est_transmission_s": round(
            sum(p.est_transmission_s for p in pages), 1),
        "warnings": sorted({w for p in pages for w in p.warnings}),
    }
    return report


def _save_multipage_tiff(tiff_paths, out_path):
    frames = [Image.open(p) for p in tiff_paths]
    first, rest = frames[0], frames[1:]
    first.save(out_path, format="TIFF", compression="group4",
               save_all=True, append_images=rest)


def render_preview(in_pdf: str, page_no: int, out_png: str, opt: FaxOptions):
    """Render exactly the bilevel output for one page as a PNG, for inspection."""
    doc = fitz.open(in_pdf)
    page = doc[page_no - 1]
    img, _ = process_page(page, page_no, opt)
    img.convert("L").save(out_png)
    return out_png


def render_robust_text_preview(in_pdf: str, page_no: int, out_png: str,
                               opt: FaxOptions) -> dict:
    """Side-by-side proof that robust-image-text helped: the exact bilevel page as
    it would fax WITHOUT the recolor (left) vs WITH it (right), so a human can
    confirm the rescued text is now legible. Returns the per-region self-check."""
    import dataclasses
    off = dataclasses.replace(opt, robust_image_text="off")
    before, _ = process_page(fitz.open(in_pdf)[page_no - 1], page_no, off)
    after, rep = process_page(fitz.open(in_pdf)[page_no - 1], page_no, opt)
    rt = rep.robust_text or {}
    rec = rt.get("regions_recovered", 0)
    det = rt.get("regions_detected", 0)
    metrics = {
        "before": {"original": True, "label": "WITHOUT robust text",
                   "note": "colored/low-contrast text as a plain grayscale fax"},
        "after": {"original": True, "label": "WITH robust text",
                  "note": f"{rec}/{det} region(s) recolored & verified legible"},
    }
    _compose_contact_sheet([("before", before), ("after", after)],
                           metrics, recommended="after").save(out_png)
    return {"page": page_no, "output": out_png, "robust_text": rt}


# --------------------------------------------------------------------------- #
# Multi-method comparison preview ("spend your eye tokens here")              #
# --------------------------------------------------------------------------- #
def _load_font(size: int):
    from PIL import ImageFont
    for name in ("arial.ttf", "DejaVuSans.ttf", "LiberationSans-Regular.ttf",
                 "Helvetica.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except Exception:
            continue
    return ImageFont.load_default()


def _oswald_font(size: int, weight: str = "Bold"):
    """Condensed Oswald display face (matches the project logo) for headings.

    Prefers the bundled Oswald variable font; falls back to a condensed/bold
    system face, then to the generic loader, so it still renders anywhere."""
    from PIL import ImageFont
    bundled = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "..", "assets", "Oswald.ttf")
    try:
        f = ImageFont.truetype(bundled, size)
        try:
            f.set_variation_by_name(weight)
        except Exception:
            pass
        return f
    except Exception:
        pass
    for name in ("Oswald-Bold.ttf", "BebasNeue-Regular.ttf", "impact.ttf",
                 "arialbd.ttf", "ariblk.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except Exception:
            continue
    return _load_font(size)


def _compose_contact_sheet(panels, metrics, recommended, cell_w=480):
    """panels: list of (method_name, PIL '1' image). Returns an RGB contact
    sheet with a labeled, metric-annotated panel per method."""
    from PIL import ImageDraw
    rendered = []
    for name, img in panels:
        rgb = img.convert("RGB")          # keep color references in color
        ch = max(1, int(rgb.height * (cell_w / rgb.width)))
        rendered.append((name, rgb.resize((cell_w, ch), Image.LANCZOS)))

    cap, pad, title_h = 58, 18, 18
    cols = 3 if len(rendered) > 4 else max(1, len(rendered))
    rows = (len(rendered) + cols - 1) // cols
    cell_h = max(r.height for _, r in rendered)
    W = pad + cols * (cell_w + pad)
    H = title_h + rows * (cell_h + cap + pad) + pad
    canvas = Image.new("RGB", (W, H), (255, 255, 255))
    d = ImageDraw.Draw(canvas)
    lf, sf = _oswald_font(22, "SemiBold"), _load_font(15)

    for i, (name, rgb) in enumerate(rendered):
        r, c = divmod(i, cols)
        x = pad + c * (cell_w + pad)
        y = title_h + r * (cell_h + cap + pad)
        m = metrics.get(name, {})
        is_orig = bool(m.get("original"))
        is_rec = (name == recommended) and not is_orig
        if is_orig:
            header = (210, 222, 240)        # neutral blue for the source panel
        elif is_rec:
            header = (206, 234, 206)
        else:
            header = (228, 228, 228)
        d.rectangle([x, y, x + cell_w, y + cap], fill=header)
        label = (m.get("label", name.upper()) if is_orig
                 else name.upper() + ("   >> RECOMMENDED <<" if is_rec else ""))
        d.text((x + 10, y + 6), label, font=lf, fill=(10, 10, 10))
        if is_orig:
            d.text((x + 10, y + 34), m.get("note", "source \u00b7 not a fax"),
                   font=sf, fill=(70, 70, 70))
        else:
            d.text((x + 10, y + 34),
                   f"{m.get('encoded_bytes', 0) / 1024:.0f} KB  \u00b7  "
                   f"~{m.get('est_transmission_s', 0):.0f}s / page",
                   font=sf, fill=(70, 70, 70))
        canvas.paste(rgb, (x, y + cap))
        d.rectangle([x, y, x + cell_w, y + cap + rgb.height],
                    outline=(38, 148, 38) if is_rec else (178, 178, 178),
                    width=3 if is_rec else 1)
    return canvas


def render_comparison(in_pdf: str, page_no: int, out_png: str, opt: FaxOptions,
                      methods=None, include_original: bool = False) -> dict:
    """Render one page through several halftone methods into a single contact
    sheet, annotated with per-method G4 size + transmission estimate and a
    recommended pick, so a human can choose the optimal one by eye ("eye
    tokens"). Returns a dict of metrics + recommendation.

    With include_original, two reference panels are shown first — the original in
    color (#1) and a true grayscale of it (#2) — and the default method set drops
    to four halftones so the sheet stays a clean 6-up. The references are not
    themselves faxes; they show the continuous-tone input each halftone (and the
    1-bit channel) has to approximate."""
    if methods is None:
        methods = COMPARE4_METHODS if include_original else COMPARE_METHODS
    hdpi, vdpi = RESOLUTIONS[opt.resolution]
    doc = fitz.open(in_pdf)
    page = doc[page_no - 1]
    gray, mask, photo_fraction, _warn, already, _rtext, _otext = _prepare_page(page, opt)

    tmpdir = tempfile.mkdtemp(prefix="faxcmp_")
    panels, metrics = [], {}
    if include_original:
        color = render_page_color(page, hdpi, vdpi, opt.max_scanline_px)
        panels.append(("original", Image.fromarray(color, "RGB")))
        metrics["original"] = {
            "encoded_bytes": 0, "est_transmission_s": 0.0, "original": True,
            "label": "ORIGINAL (color)", "note": "color source \u00b7 not a fax",
            "info": "The original document in color, for reference.",
        }
        gray_ref = render_page_gray(page, hdpi, vdpi, opt.max_scanline_px)
        panels.append(("grayscale", Image.fromarray(gray_ref).convert("L")))
        metrics["grayscale"] = {
            "encoded_bytes": 0, "est_transmission_s": 0.0, "original": True,
            "label": "TRUE GRAYSCALE", "note": "grayscale source \u00b7 not a fax",
            "info": "True grayscale (desaturated) source — the continuous-tone "
                    "input each halftone below approximates.",
        }
    for m in methods:
        bw = _postclean(_apply_dither(gray, mask, m, opt, hdpi, vdpi), opt)
        img = Image.fromarray(bw.astype(np.uint8)).convert("1")
        tp = os.path.join(tmpdir, f"{m}.tif")
        nbytes = encode_g4_tiff(img, tp)
        metrics[m] = {
            "encoded_bytes": nbytes,
            "est_transmission_s": round(
                nbytes * 8 / opt.line_rate_bps + opt.page_overhead_s, 1),
            "info": HALFTONE_INFO.get(DITHER_ALIASES.get(m, m), ""),
        }
        panels.append((m, img))

    rec, reason = recommend_dither(photo_fraction, opt.fax_heavy)
    if rec not in methods:  # ensure the recommended panel is shown/highlighted
        rec = min(methods, key=lambda k: metrics[k]["encoded_bytes"])
    _compose_contact_sheet(panels, metrics, rec).save(out_png)
    smallest = min(methods, key=lambda k: metrics[k]["encoded_bytes"])
    return {
        "page": page_no,
        "already_bilevel": already,
        "photo_fraction": round(photo_fraction, 4),
        "methods": metrics,
        "recommended": rec,
        "reason": reason,
        "smallest": smallest,
        "output": out_png,
    }
