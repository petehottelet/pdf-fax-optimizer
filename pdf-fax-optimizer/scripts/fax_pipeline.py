#!/usr/bin/env python3
"""Back-compat shim — the pipeline now lives in pdf_fax_optimizer/fax_pipeline.py.

Re-exports the public surface so legacy `import fax_pipeline` (with the skill
folder on sys.path) keeps working. Prefer
`from pdf_fax_optimizer import fax_pipeline`.
"""
import os
import sys

_SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SKILL_DIR not in sys.path:
    sys.path.insert(0, _SKILL_DIR)

from pdf_fax_optimizer.fax_pipeline import *  # noqa: E402,F401,F403
from pdf_fax_optimizer import fax_pipeline as _impl  # noqa: E402

# Bridge any non-underscore names `import *` skipped (e.g. dunder-free helpers
# the legacy callers reached for) without enumerating the whole module.
globals().update({k: v for k, v in vars(_impl).items()
                  if not k.startswith("__")})
