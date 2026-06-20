"""CLI surface: every --panels value renders, invalid values exit non-zero, and
a custom --sample-include recipe produces a sheet."""
from __future__ import annotations

import os
import sys

import pytest

from pdf_fax_optimizer import optimize_pdf


def run_main(monkeypatch, argv) -> int:
    monkeypatch.setattr(sys, "argv", ["pdf-fax-optimizer", *argv])
    try:
        optimize_pdf.main()
        return 0
    except SystemExit as exc:
        code = exc.code
        if code is None:
            return 0
        if isinstance(code, int):
            return code
        return 1  # a string message means an error exit (process would print + exit 1)


@pytest.mark.parametrize("panels", ["1", "2", "4", "6", "8", "12", "20", "max"])
def test_panels_render(text_pdf, tmp_path, monkeypatch, panels):
    out = tmp_path / f"out_{panels}.pdf"
    code = run_main(monkeypatch, [
        text_pdf, "-o", str(out), "--sample", "1", "--panels", panels,
        "--no-sample-header",
    ])
    assert code == 0
    assert out.exists()
    assert os.path.exists(os.path.splitext(str(out))[0] + ".sample_p1.png")


def test_invalid_panels_exits_nonzero(text_pdf, tmp_path, monkeypatch):
    out = tmp_path / "bad.pdf"
    code = run_main(monkeypatch, [
        text_pdf, "-o", str(out), "--sample", "1", "--panels", "5",
    ])
    assert code != 0


def test_sample_include_recipe(text_pdf, tmp_path, monkeypatch):
    out = tmp_path / "inc.pdf"
    code = run_main(monkeypatch, [
        text_pdf, "-o", str(out), "--sample", "1",
        "--sample-include", "orig,gray,floyd,line", "--no-sample-header",
    ])
    assert code == 0
    assert os.path.exists(os.path.splitext(str(out))[0] + ".sample_p1.png")
