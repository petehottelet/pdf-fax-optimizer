#!/usr/bin/env python3
"""Back-compat shim — see pdf_fax_optimizer/check_deps.py for the implementation.

Prefer `python -m pdf_fax_optimizer.check_deps`.
"""
import os
import sys

_SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SKILL_DIR not in sys.path:
    sys.path.insert(0, _SKILL_DIR)

from pdf_fax_optimizer.check_deps import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
