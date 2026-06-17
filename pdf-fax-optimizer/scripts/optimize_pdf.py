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
        text_binarize=pick(args.text_binarize, "text_binarize", "contrast"),
        tone_curve=pick(args.tone_curve, "tone_curve", "auto"),
        sharpen=args.sharpen or fc.get("sharpen", False),
        green_noise_coarseness=pick(args.green_noise_coarseness,
                                    "green_noise_coarseness", 4.0),
        text_in_image=pick(args.text_in_image, "text_in_image", True),
        robust_image_text=pick(args.robust_text, "robust_image_text", "auto"),
        robust_text_stroke=pick(args.robust_text_stroke, "robust_text_stroke", 0.15),
        ocr_text=pick(args.ocr_text, "ocr_text", "off"),
        ocr_conf_min=pick(args.ocr_conf, "ocr_conf_min", 0.6),
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
                            "sierra", "blue-noise", "green-noise", "green",
                            "edd", "line", "woodcut", "engraving"], default=None,
                   help="photo halftone schema (see references)")
    p.add_argument("--green-noise-coarseness", type=float, default=None,
                   dest="green_noise_coarseness",
                   help="green-noise AM<->FM knob ~2 (detail) .. 8 (robust)")
    p.add_argument("--text-binarize",
                   choices=["contrast", "sauvola", "niblack", "wolf", "bradley",
                            "otsu"],
                   default=None,
                   help="binarizer for text/line content; 'contrast' (default) "
                        "maximizes legibility by forcing gray/light text to "
                        "solid black")
    p.add_argument("--tone-curve", choices=["auto", "none"], default=None,
                   help="per-family dot-gain pre-correction for photos")
    p.add_argument("--sharpen", action="store_true",
                   help="edge-aware unsharp on photo regions before halftoning")
    p.add_argument("--no-text-in-image", dest="text_in_image",
                   action="store_false", default=None,
                   help="don't rescue text baked into photos; halftone the whole "
                        "image region (text-in-image rescue is on by default)")
    p.add_argument("--robust-text", choices=["auto", "on", "off"], default=None,
                   dest="robust_text",
                   help="rescue washout-prone COLORED text (e.g. yellow on cyan) "
                        "that grayscale loses: detect by chroma, recolor to solid "
                        "black-on-white, and self-verify legibility. 'auto' "
                        "(default) acts only when such text is found; 'on' scans "
                        "more aggressively; 'off' disables")
    p.add_argument("--robust-text-stroke", type=float, default=None,
                   dest="robust_text_stroke", metavar="MULT",
                   help="thickness of the contrasting stroke drawn behind rescued "
                        "text, as a multiple of glyph height (default 0.15). Only "
                        "applied where the background is too dark for solid-black "
                        "text on its own; light/mid backgrounds get no backing")
    p.add_argument("--robust-text-preview", type=int, default=None,
                   metavar="PAGE",
                   help="write a before/after contact sheet for PAGE showing the "
                        "page faxed WITHOUT vs WITH robust-text recolor")
    p.add_argument("--ocr-text", choices=["off", "auto", "on"], default=None,
                   dest="ocr_text",
                   help="RECOGNISE text baked into images (OCR) and re-typeset it "
                        "crisply, recovering words the signal path can't (e.g. a "
                        "sign word lost to near-zero contrast). Off by default; "
                        "needs an OCR engine (rapidocr-onnxruntime). OCR can misread "
                        "— verify recognised words in the report/preview")
    p.add_argument("--ocr-conf", type=float, default=None, dest="ocr_conf",
                   metavar="C",
                   help="minimum OCR confidence (0–1) to re-typeset a word "
                        "(default 0.5)")
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
                        "(default: the curated 6-up set)")
    p.add_argument("--compare-original", action="store_true",
                   help="lead the contact sheet with original-color (#1) and "
                        "true-grayscale (#2) references, then four halftones (6-up)")
    p.add_argument("--keep-converted-pdf", action="store_true",
                   help="when input is Office/image, keep the intermediate PDF "
                        "next to the output instead of deleting it")
    # ---- optional: send the optimized file via a cloud fax API ----
    s = p.add_argument_group("sending (optional)")
    s.add_argument("--send", choices=["mfax", "phaxio", "generic"], default=None,
                   help="after optimizing, transmit the output via this fax API")
    s.add_argument("--to", help="recipient fax number in E.164 (with --send)")
    s.add_argument("--api-key")
    s.add_argument("--api-secret")
    s.add_argument("--base-url")
    s.add_argument("--dry-run", action="store_true",
                   help="with --send: print the request instead of transmitting")
    s.add_argument("--cover-page", action="store_true")
    s.add_argument("--recipient-name")
    s.add_argument("--subject")
    s.add_argument("--notes")
    s.add_argument("--caller-id")
    s.add_argument("--url")
    s.add_argument("--auth-header")
    s.add_argument("--basic-user")
    s.add_argument("--basic-pass")
    s.add_argument("--to-field", default="to")
    s.add_argument("--file-field", default="file")
    s.add_argument("--field", action="append")
    args = p.parse_args()

    cfg = load_config(args.config)
    opt = build_fax_options(cfg, args)

    # Accept Word/PowerPoint/Excel/OpenDocument/text and loose images by
    # normalizing them to PDF first; the rest of the pipeline is unchanged.
    import to_pdf
    keep_dir = os.path.dirname(os.path.abspath(args.output)) \
        if args.keep_converted_pdf else None
    try:
        src_pdf, converted_is_temp = to_pdf.ensure_pdf(args.input, keep_dir)
    except RuntimeError as e:
        sys.exit(str(e))
    if src_pdf != args.input:
        print(f"converted {args.input} -> {src_pdf}")

    comparison = None
    if args.compare_page:
        methods = ([m.strip() for m in args.compare_methods.split(",")]
                   if args.compare_methods else None)
        png = (os.path.splitext(args.output)[0]
               + f".compare_p{args.compare_page}.png")
        comparison = fax.render_comparison(
            src_pdf, args.compare_page, png, opt, methods,
            include_original=args.compare_original)
        print(f"Comparison contact sheet: {png}")
        print(f"  recommended: {comparison['recommended']}  "
              f"(smallest: {comparison['smallest']})")
        print(f"  why: {comparison['reason']}")
        print("  spend your eye tokens \u2014 per-method G4 size / page:")
        for m, mm in comparison["methods"].items():
            if mm.get("original"):
                print(f"    {m:11s} {'  source':>9s}  "
                      f"({mm.get('note', 'reference, not faxed')})")
                continue
            star = "  <- recommended" if m == comparison["recommended"] else ""
            print(f"    {m:11s} {mm['encoded_bytes'] / 1024:6.0f} KB  "
                  f"~{mm['est_transmission_s']:.0f}s{star}")

    if args.preview_page:
        png = os.path.splitext(args.output)[0] + f".preview_p{args.preview_page}.png"
        fax.render_preview(src_pdf, args.preview_page, png, opt)
        print(f"Preview written: {png}")

    if args.robust_text_preview:
        png = (os.path.splitext(args.output)[0]
               + f".robusttext_p{args.robust_text_preview}.png")
        rtp = fax.render_robust_text_preview(
            src_pdf, args.robust_text_preview, png, opt)
        rt = rtp.get("robust_text") or {}
        print(f"Robust-text before/after: {png}")
        print(f"  regions detected: {rt.get('regions_detected', 0)}, "
              f"recovered: {rt.get('regions_recovered', 0)}, "
              f"unrecovered: {rt.get('regions_unrecovered', 0)}")

    report = fax.convert_pdf(src_pdf, args.output, opt)
    # Report against the original file the user handed us, not the intermediate.
    report["input"] = args.input
    report["input_bytes"] = os.path.getsize(args.input)
    if src_pdf != args.input:
        report["converted_pdf"] = src_pdf
    if comparison:
        report["comparison"] = comparison

    if converted_is_temp and src_pdf != args.input:
        try:
            import shutil
            shutil.rmtree(os.path.dirname(src_pdf), ignore_errors=True)
        except Exception:
            pass

    report_path = args.report or cfg.get("report")
    if report_path:
        with open(report_path, "w") as f:
            json.dump(report, f, indent=2)

    _print_summary(report)

    if args.send:
        if not args.to:
            sys.exit("--send requires --to (recipient fax number in E.164)")
        import send_fax
        print(f"\nsending {args.output} via {args.send} to {args.to}"
              + (" (dry run)" if args.dry_run else "") + " ...")
        result = send_fax.send(args.send, args.output, args.to, args)
        print(json.dumps(result, indent=2))
        if not result.get("ok"):
            sys.exit(1)


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
    binar = sorted({p.get("text_binarize", "") for p in report["pages"]} - {""})
    if binar:
        print(f"text binarizer: {', '.join(binar)}")
    rt_det = sum((p.get("robust_text") or {}).get("regions_detected", 0)
                 for p in report["pages"])
    if rt_det:
        rt_rec = sum((p.get("robust_text") or {}).get("regions_recovered", 0)
                     for p in report["pages"])
        rt_un = sum((p.get("robust_text") or {}).get("regions_unrecovered", 0)
                    for p in report["pages"])
        print(f"robust image text: {rt_det} colored region(s) detected, "
              f"{rt_rec} recolored for contrast"
              + (f", {rt_un} left as-is (no rescue needed)" if rt_un else ""))
    ocr_words = [w for p in report["pages"]
                 for w in (p.get("ocr_text") or {}).get("words", [])]
    if ocr_words:
        shown = ", ".join(f"“{w['text']}”({w['conf']:.2f})"
                          for w in ocr_words[:8])
        more = "" if len(ocr_words) <= 8 else f", +{len(ocr_words) - 8} more"
        print(f"OCR text recovered: {len(ocr_words)} word(s) re-typeset — "
              f"{shown}{more}  (verify these are correct)")
    print(f"est. transmission: {report['total_est_transmission_s']:.0f}s "
          f"(~{report['total_est_transmission_s'] / 60:.1f} min)")
    if report.get("comparison"):
        print(f"comparison sheet: {report['comparison']['output']}")
    if report["warnings"]:
        print("warnings: " + ", ".join(report["warnings"]))


if __name__ == "__main__":
    main()
