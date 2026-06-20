"""The JSON report contract: required keys, types, value bounds, and the new
chosen_dpi / chosen_dpi_reason audit fields."""
from __future__ import annotations

from pdf_fax_optimizer import FaxOptions, convert_pdf, fax_pipeline


TOP_LEVEL = {"mode", "input", "output", "input_bytes", "output_bytes",
             "pages", "total_est_transmission_s", "warnings"}
PER_PAGE = {"index", "encoded_bytes", "est_transmission_s", "photo_regions",
            "photo_fraction", "dither", "text_binarize", "chosen_dpi",
            "chosen_dpi_reason"}
DPI_REASONS = {"vector_preset", "raster_native", "mixed_preset_floor",
               "native_raster_upshift", "clamped_to_max"}


def test_report_shape_text(text_pdf, tmp_path):
    out = tmp_path / "out.pdf"
    rep = convert_pdf(text_pdf, str(out), FaxOptions())

    assert TOP_LEVEL <= set(rep)
    assert isinstance(rep["pages"], list) and rep["pages"]
    assert rep["total_est_transmission_s"] > 0

    page = rep["pages"][0]
    assert PER_PAGE <= set(page)
    assert page["index"] == 1
    assert page["est_transmission_s"] > 0
    assert page["photo_regions"] >= 0
    assert 0.0 <= page["photo_fraction"] <= 1.0
    assert page["dither"] in fax_pipeline.SCREENS or page["dither"] == "none"
    assert page["text_binarize"] == FaxOptions().text_binarize
    assert page["chosen_dpi"] > 0
    assert page["chosen_dpi_reason"] in DPI_REASONS


def test_photo_features_present_for_photo(photo_cover_pdf, tmp_path):
    out = tmp_path / "out.pdf"
    rep = convert_pdf(photo_cover_pdf, str(out), FaxOptions())
    page = rep["pages"][0]
    assert page["photo_fraction"] > 0.0
    assert page["photo_features"], "photo_features should be populated"
    assert "mean_luma" in page["photo_features"]
