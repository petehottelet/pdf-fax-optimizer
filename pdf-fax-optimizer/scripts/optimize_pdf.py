#!/usr/bin/env python3
"""Back-compat shim. The implementation moved into the importable
`pdf_fax_optimizer` package; this preserves the historical
`python scripts/optimize_pdf.py ...` invocation used by older skill
instructions and release zips.

Prefer `python -m pdf_fax_optimizer.optimize_pdf ...` or the installed
`pdf-fax-optimizer` console script.
"""
import os
import sys

_SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SKILL_DIR not in sys.path:
    sys.path.insert(0, _SKILL_DIR)

from pdf_fax_optimizer.optimize_pdf import main  # noqa: E402

if __name__ == "__main__":
    main()
