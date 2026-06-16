#!/usr/bin/env python3
"""Normalize an input document to PDF so the fax pipeline can consume it.

The optimizer works on PDF pages. This module accepts the formats people
actually try to fax — Word, PowerPoint, Excel, OpenDocument, plain text, and
loose images — and converts them to a faithful PDF first, leaving the rest of
the pipeline (rasterize -> MRC-lite -> binarize/halftone -> G4) unchanged.

Two converters:
  * images  -> wrapped straight into a one-image-per-page PDF via img2pdf
    (already a hard dependency; no extra tools).
  * Office / OpenDocument / text -> rendered by **LibreOffice headless**
    (`soffice --headless --convert-to pdf`). LibreOffice reproduces the document
    layout far more reliably than ad-hoc parsers and is the same engine most
    server-side "convert to PDF" stacks use. Install it once; it needs no GUI.

`ensure_pdf()` is a no-op for input that is already a PDF.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile

PDF_EXTS = {".pdf"}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".gif", ".webp"}
# Anything LibreOffice can open and render to PDF.
OFFICE_EXTS = {
    ".doc", ".docx", ".docm", ".rtf", ".odt", ".txt",
    ".ppt", ".pptx", ".pptm", ".odp",
    ".xls", ".xlsx", ".xlsm", ".ods", ".csv",
}

# Common Windows install locations checked when soffice isn't on PATH.
_SOFFICE_FALLBACKS = (
    r"C:\Program Files\LibreOffice\program\soffice.exe",
    r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
    "/Applications/LibreOffice.app/Contents/MacOS/soffice",
    "/usr/bin/soffice",
    "/usr/bin/libreoffice",
)


def find_soffice() -> str | None:
    for name in ("soffice", "libreoffice"):
        p = shutil.which(name)
        if p:
            return p
    for p in _SOFFICE_FALLBACKS:
        if os.path.isfile(p):
            return p
    return None


def _image_to_pdf(src: str, dest: str) -> str:
    import img2pdf
    from PIL import Image
    # Normalize odd modes (palette/alpha) so img2pdf accepts the bytes.
    with Image.open(src) as im:
        if im.mode in ("RGBA", "P", "LA"):
            im = im.convert("RGB")
            tmp = dest + ".src.png"
            im.save(tmp)
            payload = tmp
        else:
            payload = src
    with open(dest, "wb") as f:
        f.write(img2pdf.convert(payload))
    if payload != src:
        try:
            os.remove(payload)
        except OSError:
            pass
    return dest


def _office_to_pdf(src: str, dest: str) -> str:
    soffice = find_soffice()
    if not soffice:
        raise RuntimeError(
            "LibreOffice (soffice) is required to convert Office/OpenDocument "
            "files to PDF but was not found. Install LibreOffice "
            "(https://www.libreoffice.org/download/) — it runs headless, no GUI "
            "needed — or export the document to PDF yourself and pass that.")
    outdir = os.path.dirname(os.path.abspath(dest))
    # A dedicated user profile dir avoids clashing with a running LibreOffice.
    profile = tempfile.mkdtemp(prefix="lo_profile_")
    try:
        proc = subprocess.run(
            [soffice, "-env:UserInstallation=file:///" +
             profile.replace("\\", "/").lstrip("/"),
             "--headless", "--norestore", "--convert-to", "pdf",
             "--outdir", outdir, os.path.abspath(src)],
            capture_output=True, text=True, timeout=180,
        )
    finally:
        shutil.rmtree(profile, ignore_errors=True)
    produced = os.path.join(
        outdir, os.path.splitext(os.path.basename(src))[0] + ".pdf")
    if not os.path.isfile(produced):
        raise RuntimeError(
            "LibreOffice failed to convert "
            f"{src!r} to PDF.\nstdout: {proc.stdout}\nstderr: {proc.stderr}")
    if os.path.abspath(produced) != os.path.abspath(dest):
        shutil.move(produced, dest)
    return dest


def ensure_pdf(input_path: str, out_dir: str | None = None) -> tuple[str, bool]:
    """Return (pdf_path, is_temp).

    If `input_path` is already a PDF, returns it unchanged with is_temp=False.
    Otherwise converts it to PDF. When `out_dir` is given the PDF is written
    there persistently (is_temp=False); otherwise it goes to a temp dir
    (is_temp=True) for the caller to clean up.
    """
    ext = os.path.splitext(input_path)[1].lower()
    if ext in PDF_EXTS:
        return input_path, False
    if ext not in IMAGE_EXTS and ext not in OFFICE_EXTS:
        raise RuntimeError(
            f"unsupported input type {ext!r}. Supported: PDF, images "
            f"({', '.join(sorted(IMAGE_EXTS))}), and Office/OpenDocument/text "
            f"({', '.join(sorted(OFFICE_EXTS))}).")

    base = os.path.splitext(os.path.basename(input_path))[0]
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
        dest, is_temp = os.path.join(out_dir, base + ".pdf"), False
    else:
        dest = os.path.join(tempfile.mkdtemp(prefix="faxin_"), base + ".pdf")
        is_temp = True

    if ext in IMAGE_EXTS:
        _image_to_pdf(input_path, dest)
    else:
        _office_to_pdf(input_path, dest)
    return dest, is_temp


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        sys.exit("usage: to_pdf.py INPUT [OUT_DIR]")
    out = sys.argv[2] if len(sys.argv) > 2 else None
    path, tmp = ensure_pdf(sys.argv[1], out)
    print(path + ("  (temp)" if tmp else ""))
