#!/usr/bin/env python3
"""Check (and optionally install) the dependencies the optimizer needs.

By default this only *reports* what is present and prints the right pip command
for anything missing — it does not modify your environment. Auto-installation is
opt-in via `--auto-install`, and the aggressive `--break-system-packages` is a
separate explicit flag on top of that (only needed in a managed system
interpreter, e.g. a locked-down CI sandbox). The recommended install path for a
normal user is simply:

    pip install pdf-fax-optimizer            # core
    pip install "pdf-fax-optimizer[ocr,send]"  # + OCR + cloud-fax sending

CLI tools (qpdf, optional ghostscript, LibreOffice) can't be pip-installed; we
just report their presence so the caller knows what's available.
"""
from __future__ import annotations

import argparse
import importlib
import shutil
import subprocess
import sys

# Required runtime packages: import name -> pip distribution name.
PIP_PKGS = {
    "fitz": "pymupdf",
    "PIL": "Pillow",
    "numpy": "numpy",
    "cv2": "opencv-python-headless",
    "img2pdf": "img2pdf",
}

# Optional features (import name -> pip extra that provides it):
#  - requests: transmit via a cloud fax API (the `send` extra)
#  - rapidocr_onnxruntime: the OCR-driven #808080 polarity passes used by
#    --recover-text / --ocr-text (the `ocr` extra). Self-contained ONNX models,
#    no system binary.
OPTIONAL_PKGS = {
    "requests": ("requests", "send"),
}
OPTIONAL_REPORT_ONLY = {
    "rapidocr_onnxruntime": ("rapidocr-onnxruntime", "ocr"),
}


def _present(mod: str) -> bool:
    try:
        importlib.import_module(mod)
        return True
    except Exception:
        return False


def _pip_install(pkg: str, break_system: bool) -> None:
    cmd = [sys.executable, "-m", "pip", "install", "-q"]
    if break_system:
        cmd.append("--break-system-packages")
    cmd.append(pkg)
    subprocess.run(cmd, check=False)


def ensure_pip(mod: str, pkg: str, *, auto_install: bool,
               break_system: bool) -> tuple[bool, str]:
    if _present(mod):
        return True, "present"
    if not auto_install:
        return False, "MISSING"
    _pip_install(pkg, break_system)
    if _present(mod):
        return True, "installed"
    return False, "FAILED to install"


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description="Report (and optionally install) the optimizer's "
                    "dependencies.")
    p.add_argument(
        "--auto-install", action="store_true",
        help="pip-install any missing REQUIRED packages (default: report only).")
    p.add_argument(
        "--break-system-packages", action="store_true",
        help="pass --break-system-packages to pip; only with --auto-install and "
             "only when targeting a managed system interpreter (e.g. a sandbox).")
    args = p.parse_args(argv)

    auto = args.auto_install
    brk = args.break_system_packages

    ok = True
    missing_required = []
    print("Python packages (required):")
    for mod, pkg in PIP_PKGS.items():
        good, status = ensure_pip(mod, pkg, auto_install=auto, break_system=brk)
        ok = ok and good
        if not good:
            missing_required.append(pkg)
        print(f"  {pkg:28s} {status}")

    print("Python packages (optional):")
    for mod, (pkg, extra) in {**OPTIONAL_PKGS, **OPTIONAL_REPORT_ONLY}.items():
        present = _present(mod)
        note = "present" if present else f"missing (enable with the '{extra}' extra)"
        print(f"  {pkg:28s} {note}")

    # Fax mode embeds CCITT-G4 via img2pdf and does not require any CLI tool.
    # qpdf / ghostscript are optional (handy for separate PDF work) — reported
    # for convenience only. LibreOffice (soffice) is optional too, needed only
    # to convert Word/PowerPoint/Excel/OpenDocument input to PDF.
    print("CLI tools (optional):")
    for tool in ("qpdf", "gs"):
        path = shutil.which(tool)
        print(f"  {tool:28s} {'present' if path else 'missing'} (optional)")

    try:
        from . import to_pdf
    except ImportError:  # running as a loose script without the package context
        import to_pdf  # type: ignore
    soffice = to_pdf.find_soffice()
    print(f"  {'libreoffice (soffice)':28s} "
          f"{soffice if soffice else 'missing'} "
          f"(optional — for Office/OpenDocument input)")

    if not ok:
        joined = " ".join(missing_required)
        print("\nMissing required packages: " + joined)
        if auto:
            print("Auto-install failed for the above; check network/proxy "
                  "settings, or install manually:")
        else:
            print("Install them with:")
        print("  pip install pdf-fax-optimizer            # core only")
        print('  pip install "pdf-fax-optimizer[ocr,send]"  # + OCR + sending')
        print("Or, inside this checkout:  pip install -r requirements.txt")
        print("(Re-run with --auto-install to let this script pip-install them.)")
        return 1

    print("\nAll required dependencies satisfied.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
