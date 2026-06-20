#!/usr/bin/env python3
"""Back-compat shim — see pdf_fax_optimizer/to_pdf.py for the implementation.

Re-exports the public surface so legacy `import to_pdf` (with the skill folder on
sys.path) keeps working. Prefer `from pdf_fax_optimizer import to_pdf`.
"""
import os
import sys

_SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SKILL_DIR not in sys.path:
    sys.path.insert(0, _SKILL_DIR)

from pdf_fax_optimizer.to_pdf import *  # noqa: E402,F401,F403
from pdf_fax_optimizer.to_pdf import ensure_pdf, find_soffice  # noqa: E402,F401
