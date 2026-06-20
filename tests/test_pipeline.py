"""Pipeline behaviour: dither auto-pick per content type, signature preservation,
and reverse-text polarity (OCR-gated)."""
from __future__ import annotations

import pytest

from pdf_fax_optimizer import FaxOptions, convert_pdf, ocr_text, recommend_dither
from util import assert_bilevel_pdf, black_pixels_in_region


SCREEN_FAMILY = {"clustered", "atkinson", "edd", "floyd", "jarvis",
                 "green-noise", "blue-noise", "stucki", "sierra"}


def test_recommend_dither_text_is_none():
    method, _reason = recommend_dither(0.0, False)
    assert method == "none"


def test_photo_page_picks_a_screen(photo_cover_pdf, tmp_path):
    out = tmp_path / "photo.fax.pdf"
    report = convert_pdf(photo_cover_pdf, str(out), FaxOptions())
    page = report["pages"][0]
    assert page["photo_fraction"] > 0.0
    assert page["dither"] in SCREEN_FAMILY
    assert_bilevel_pdf(str(out))


def test_qr_uses_hard_threshold(qr_pdf, tmp_path):
    out = tmp_path / "qr.fax.pdf"
    report = convert_pdf(qr_pdf, str(out), FaxOptions())
    # A colourless high-contrast code must not be halftoned.
    assert report["pages"][0]["dither"] == "none"
    assert_bilevel_pdf(str(out))


def test_signature_strokes_survive(signature_scan_pdf, tmp_path):
    out = tmp_path / "sig.fax.pdf"
    convert_pdf(signature_scan_pdf, str(out), FaxOptions())
    assert_bilevel_pdf(str(out))
    # The squiggle lives in the vertical middle band; it must leave ink behind.
    assert black_pixels_in_region(str(out), (0.0, 0.25, 1.0, 0.75)) > 200


@pytest.mark.ocr
def test_reverse_text_recolored_white(reverse_text_pdf, tmp_path):
    if not ocr_text.available():
        pytest.skip("OCR engine not installed")
    out = tmp_path / "rev.fax.pdf"
    opt = FaxOptions(recover_text="on", ocr_text="on")
    report = convert_pdf(reverse_text_pdf, str(out), opt)
    words = (report["pages"][0].get("ocr_text") or {}).get("words", [])
    assert any(w.get("polarity") == "white" for w in words)
