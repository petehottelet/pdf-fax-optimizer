#!/usr/bin/env python3
"""Back-compat shim — see pdf_fax_optimizer/ocr_text.py for the implementation.

Prefer `from pdf_fax_optimizer import ocr_text`.
"""
import os
import sys

_SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SKILL_DIR not in sys.path:
    sys.path.insert(0, _SKILL_DIR)

from pdf_fax_optimizer.ocr_text import *  # noqa: E402,F401,F403
from pdf_fax_optimizer.ocr_text import available, recognize, engine_name  # noqa: E402,F401
