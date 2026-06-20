"""Input normalization: image wrapping preserves pixel dimensions; Office
conversion is cleanly skipped when LibreOffice is absent."""
from __future__ import annotations

import pytest

import fitz
from PIL import Image

from pdf_fax_optimizer import to_pdf


def test_image_wrap_preserves_dimensions(image_file, tmp_path):
    with Image.open(image_file) as im:
        src_w, src_h = im.size

    pdf_path, is_temp = to_pdf.ensure_pdf(image_file, str(tmp_path))
    assert pdf_path.endswith(".pdf") and not is_temp

    doc = fitz.open(pdf_path)
    try:
        assert doc.page_count == 1
        xref = doc[0].get_images(full=True)[0][0]
        info = doc.extract_image(xref)
    finally:
        doc.close()
    assert (info["width"], info["height"]) == (src_w, src_h)


def test_pdf_passthrough_is_noop(text_pdf):
    path, is_temp = to_pdf.ensure_pdf(text_pdf)
    assert path == text_pdf and is_temp is False


def test_office_conversion_requires_soffice(tmp_path):
    if to_pdf.find_soffice():
        pytest.skip("LibreOffice present; conversion path is exercised elsewhere")
    docx = tmp_path / "memo.docx"
    docx.write_bytes(b"not really a docx, but routed to the office converter")
    with pytest.raises(RuntimeError, match="LibreOffice"):
        to_pdf.ensure_pdf(str(docx), str(tmp_path))
