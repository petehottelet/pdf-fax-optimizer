#!/usr/bin/env python3
"""Check (and pip-install) the dependencies the optimizer needs.

Run this once before using the skill. Pip packages are installed with
--break-system-packages because the sandbox uses the system interpreter.
CLI tools (qpdf, optional ghostscript) can't be pip-installed; we just report
their presence so the caller knows what's available.
"""
import importlib
import shutil
import subprocess
import sys

PIP_PKGS = {
    "fitz": "pymupdf",
    "PIL": "Pillow",
    "numpy": "numpy",
    "cv2": "opencv-python-headless",
    "img2pdf": "img2pdf",
}

# Only needed to transmit via a cloud fax API (scripts/send_fax.py).
OPTIONAL_PKGS = {
    "requests": "requests",
}


def ensure_pip(mod, pkg):
    try:
        importlib.import_module(mod)
        return True, "present"
    except ImportError:
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "--break-system-packages",
             "-q", pkg],
            check=False,
        )
        try:
            importlib.import_module(mod)
            return True, "installed"
        except ImportError:
            return False, "FAILED to install"


def main():
    ok = True
    print("Python packages:")
    for mod, pkg in PIP_PKGS.items():
        good, status = ensure_pip(mod, pkg)
        ok = ok and good
        print(f"  {pkg:24s} {status}")

    print("Python packages (optional — for sending faxes):")
    for mod, pkg in OPTIONAL_PKGS.items():
        good, status = ensure_pip(mod, pkg)
        print(f"  {pkg:24s} {status if good else 'missing (optional)'}")

    # Fax mode embeds CCITT-G4 via img2pdf and does not require any CLI tool.
    # qpdf / ghostscript are optional (handy for separate PDF work) — reported
    # for convenience only. LibreOffice (soffice) is optional too, needed only
    # to convert Word/PowerPoint/Excel/OpenDocument input to PDF.
    print("CLI tools (optional):")
    for tool in ("qpdf", "gs"):
        path = shutil.which(tool)
        print(f"  {tool:24s} {'present' if path else 'missing'} (optional)")

    import to_pdf  # local module; finds soffice on PATH or common locations
    soffice = to_pdf.find_soffice()
    print(f"  {'libreoffice (soffice)':24s} "
          f"{soffice if soffice else 'missing'} "
          f"(optional — for Office/OpenDocument input)")

    if not ok:
        print("\nSome required pip packages are missing. They should self-install "
              "above; if a pip install failed, check network/proxy settings.")
        sys.exit(1)
    print("\nAll required dependencies satisfied.")


if __name__ == "__main__":
    main()
