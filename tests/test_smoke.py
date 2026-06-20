"""Smoke tests: the package imports, exposes its API, and a basic conversion
runs end to end producing a bilevel PDF."""
from __future__ import annotations

import importlib

import pytest

from util import assert_bilevel_pdf


MODULES = [
    "pdf_fax_optimizer",
    "pdf_fax_optimizer.cli",
    "pdf_fax_optimizer.optimize_pdf",
    "pdf_fax_optimizer.send_fax",
    "pdf_fax_optimizer.to_pdf",
    "pdf_fax_optimizer.check_deps",
    "pdf_fax_optimizer.ocr_text",
    "pdf_fax_optimizer.fax_pipeline",
]


@pytest.mark.parametrize("name", MODULES)
def test_module_imports(name):
    assert importlib.import_module(name) is not None


def test_version_and_public_api():
    import pdf_fax_optimizer as p

    assert isinstance(p.__version__, str) and p.__version__
    for attr in ("FaxOptions", "convert_pdf", "recommend_dither",
                 "render_contact_sheet"):
        assert hasattr(p, attr), attr


def test_convert_text_pdf_end_to_end(text_pdf, tmp_path):
    from pdf_fax_optimizer import FaxOptions, convert_pdf

    out = tmp_path / "out.fax.pdf"
    report = convert_pdf(text_pdf, str(out), FaxOptions())
    assert out.exists() and out.stat().st_size > 0
    assert report["pages"] and report["output_bytes"] > 0
    assert_bilevel_pdf(str(out))
