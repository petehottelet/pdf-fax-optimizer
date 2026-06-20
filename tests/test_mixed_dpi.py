"""Regression guard for the mixed-page _effective_dpi bug.

Before the fix, any embedded raster forced the WHOLE page to that raster's native
DPI, so a 72-DPI image dragged live vector body text down to 72 PPI even when the
user asked for `fine` (196). The fix makes the preset a floor on mixed pages
while leaving pure raster scans at their native grid.
"""
from __future__ import annotations

import fitz

from pdf_fax_optimizer import fax_pipeline as fx


FINE = 196
SUPERFINE = 391


def _page(pdf_path):
    return fitz.open(pdf_path)[0]


def test_mixed_page_honors_preset_floor(mixed_page_pdf):
    dpi, reason = fx._effective_dpi_and_reason(_page(mixed_page_pdf), FINE)
    assert round(dpi) == FINE          # was 72 before the fix
    assert reason == "mixed_preset_floor"


def test_mixed_page_clamped_at_ceiling(mixed_page_pdf):
    dpi, reason = fx._effective_dpi_and_reason(_page(mixed_page_pdf), SUPERFINE)
    assert round(dpi) == fx.MAX_RENDER_DPI
    assert reason == "clamped_to_max"


def test_raster_only_lowdpi_stays_native(raster_lowdpi_pdf):
    dpi, reason = fx._effective_dpi_and_reason(_page(raster_lowdpi_pdf), FINE)
    assert round(dpi) == 72            # baked-in raster text must not be upscaled
    assert reason == "raster_native"


def test_raster_only_highdpi_clamps(raster_highdpi_pdf):
    dpi, reason = fx._effective_dpi_and_reason(_page(raster_highdpi_pdf), FINE)
    assert round(dpi) == fx.MAX_RENDER_DPI
    assert reason == "clamped_to_max"


def test_vector_only_uses_preset(text_pdf):
    dpi, reason = fx._effective_dpi_and_reason(_page(text_pdf), FINE)
    assert round(dpi) == FINE
    assert reason == "vector_preset"
