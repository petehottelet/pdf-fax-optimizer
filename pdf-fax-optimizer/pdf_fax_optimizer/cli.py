#!/usr/bin/env python3
"""Console-script entry point for the `pdf-fax-optimizer` command.

Thin dispatcher so the installed CLI has a stable home even if the underlying
module layout changes. Today it forwards straight to the optimizer's argument
parser; sending is its own `pdf-fax-send` entry point (mapping to
`pdf_fax_optimizer.send_fax:main`).
"""
from __future__ import annotations


def main(argv=None) -> int:
    from .optimize_pdf import main as _optimize_main
    result = _optimize_main()
    return 0 if result is None else int(result)


if __name__ == "__main__":
    raise SystemExit(main())
