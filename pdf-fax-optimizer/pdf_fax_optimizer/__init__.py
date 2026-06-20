"""pdf-fax-optimizer — convert PDFs and office documents into a 1-bit CCITT-G4
fax that arrives legible after a noisy Group-3 transmission.

The public, stable programmatic API is exposed lazily so that simply importing
the package (e.g. for `python -m pdf_fax_optimizer.check_deps`) does not pull in
the heavy runtime dependencies (numpy / OpenCV / PyMuPDF / img2pdf). They are
imported only when you actually touch the pipeline objects.

    from pdf_fax_optimizer import FaxOptions, convert_pdf
    convert_pdf("in.pdf", "out.fax.pdf", FaxOptions())
"""
from __future__ import annotations

__version__ = "2.0.0"

# Single source of truth for the version: hatchling reads this attribute
# statically (see [tool.hatch.version] in pyproject.toml), so do not compute it.

_LAZY_EXPORTS = {
    "FaxOptions",
    "convert_pdf",
    "recommend_dither",
    "render_contact_sheet",
}

__all__ = ["__version__", *sorted(_LAZY_EXPORTS)]


def __getattr__(name: str):
    # PEP 562 lazy attribute access: keeps `import pdf_fax_optimizer` lightweight
    # while still exposing the pipeline objects as top-level package attributes.
    if name in _LAZY_EXPORTS:
        from . import fax_pipeline
        return getattr(fax_pipeline, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__():
    return sorted(set(globals()) | _LAZY_EXPORTS)
