#!/usr/bin/env python3
"""PDF fax optimizer — convert a PDF into a fax-ready 1-bit CCITT-G4 PDF/TIFF.

The whole job is a document that arrives LEGIBLE over a Group-3 fax line. See
SKILL.md and references/ for the why behind each knob. Flags override any values
from --config.

Need to shrink a PDF for email/web instead? Use the companion skill:
https://github.com/petehottelet/pdf-email-optimizer
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fax_pipeline as fax  # noqa: E402


def load_config(path):
    if not path:
        return {}
    with open(path) as f:
        return json.load(f)


def build_fax_options(cfg, args) -> fax.FaxOptions:
    fc = cfg.get("fax", {})

    def pick(flag, key, default):
        return flag if flag is not None else fc.get(key, default)

    return fax.FaxOptions(
        resolution=pick(args.fax_resolution, "resolution", "fine"),
        dither=pick(args.dither, "dither", "auto"),
        fax_heavy=args.fax_heavy or fc.get("fax_heavy", False),
        segmentation=pick(args.segmentation, "segmentation", "embedded"),
        thicken=args.thicken or fc.get("thicken", False),
        flatten_bg=pick(args.flatten_bg, "flatten_bg", True),
        despeckle=pick(args.despeckle, "despeckle", True),
        deskew=pick(args.deskew, "deskew", True),
        fmt=pick(args.format, "format", "pdf"),
        line_rate_bps=pick(args.line_rate, "line_rate_bps", 14400),
    )


def main():
    p = argparse.ArgumentParser(description="Convert a PDF to a fax-ready "
                                            "1-bit CCITT-G4 PDF/TIFF")
    p.add_argument("input")
    p.add_argument("-o", "--output", required=True)
    # Kept for backward-compatible invocations; fax is the only mode.
    p.add_argument("--mode", choices=["fax"], default="fax")
    p.add_argument("--config")
    p.add_argument("--report")
    p.add_argument("--fax-resolution",
                   choices=["standard", "fine", "superfine"], default=None)
    p.add_argument("--dither",
                   choices=["auto", "none", "threshold", "ordered", "bayer",
                            "clustered", "floyd", "atkinson", "jarvis", "stucki",
                            "sierra", "blue-noise"], default=None)
    p.add_argument("--fax-heavy", action="store_true")
    p.add_argument("--segmentation",
                   choices=["embedded", "variance", "none"], default=None)
    p.add_argument("--thicken", action="store_true")
    p.add_argument("--no-flatten-bg", dest="flatten_bg", action="store_false",
                   default=None)
    p.add_argument("--no-despeckle", dest="despeckle", action="store_false",
                   default=None)
    p.add_argument("--no-deskew", dest="deskew", action="store_false",
                   default=None)
    p.add_argument("--format", choices=["pdf", "tiff"], default=None)
    p.add_argument("--line-rate", type=int, default=None)
    p.add_argument("--preview-page", type=int, default=None)
    p.add_argument("--compare-page", type=int, default=None,
                   help="render this page through several halftone methods into "
                        "one labeled contact sheet so you can pick by eye")
    p.add_argument("--compare-methods", default=None,
                   help="comma-separated halftone names for --compare-page "
                        "(default: the curated top 5)")
    args = p.parse_args()

    cfg = load_config(args.config)
    opt = build_fax_options(cfg, args)

    comparison = None
    if args.compare_page:
        methods = ([m.strip() for m in args.compare_methods.split(",")]
                   if args.compare_methods else None)
        png = (os.path.splitext(args.output)[0]
               + f".compare_p{args.compare_page}.png")
        comparison = fax.render_comparison(
            args.input, args.compare_page, png, opt, methods)
        print(f"Comparison contact sheet: {png}")
        print(f"  recommended: {comparison['recommended']}  "
              f"(smallest: {comparison['smallest']})")
        print(f"  why: {comparison['reason']}")
        print("  spend your eye tokens \u2014 per-method G4 size / page:")
        for m, mm in comparison["methods"].items():
            star = "  <- recommended" if m == comparison["recommended"] else ""
            print(f"    {m:11s} {mm['encoded_bytes'] / 1024:6.0f} KB  "
                  f"~{mm['est_transmission_s']:.0f}s{star}")

    if args.preview_page:
        png = os.path.splitext(args.output)[0] + f".preview_p{args.preview_page}.png"
        fax.render_preview(args.input, args.preview_page, png, opt)
        print(f"Preview written: {png}")

    report = fax.convert_pdf(args.input, args.output, opt)
    if comparison:
        report["comparison"] = comparison

    report_path = args.report or cfg.get("report")
    if report_path:
        with open(report_path, "w") as f:
            json.dump(report, f, indent=2)

    _print_summary(report)


def _print_summary(report):
    ib, ob = report["input_bytes"], report["output_bytes"]
    if ib:
        change = (ob - ib) / ib * 100
        size_note = (f"{abs(change):.1f}% smaller" if change <= 0
                     else f"{change:.1f}% larger")
    else:
        size_note = "n/a"
    print(f"mode: {report['mode']}")
    print(f"input:  {ib:,} bytes")
    print(f"output: {ob:,} bytes  ({size_note})")
    print(f"pages:  {len(report['pages'])}")
    dithers = sorted({p.get("dither", "") for p in report["pages"]} - {""})
    if dithers:
        print(f"halftone used: {', '.join(dithers)}")
    print(f"est. transmission: {report['total_est_transmission_s']:.0f}s "
          f"(~{report['total_est_transmission_s'] / 60:.1f} min)")
    if report.get("comparison"):
        print(f"comparison sheet: {report['comparison']['output']}")
    if report["warnings"]:
        print("warnings: " + ", ".join(report["warnings"]))


if __name__ == "__main__":
    main()
