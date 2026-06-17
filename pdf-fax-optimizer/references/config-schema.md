# Config Schema

Pass `--config config.json` instead of (or alongside) flags. Any flag given on
the command line overrides the matching config value. All keys are optional;
omitted keys fall back to the documented defaults.

## Schema

```jsonc
{
  "fax": {
    "resolution": "fine",         // "standard" | "fine" | "superfine"

    // ---- photo halftone schema (continuous-tone regions only) ----
    "dither": "auto",             // "auto" | "clustered" | "green-noise"
                                  //   | "blue-noise" | "atkinson" | "floyd"
                                  //   | "line" (aka "woodcut"/"engraving")
                                  //   | "ordered" | "edd" | "jarvis" | "stucki"
                                  //   | "sierra" | "none"
    "green_noise_coarseness": 4.0, // green-noise AM<->FM knob ~2 (detail)..8 (robust)
    "tone_curve": "auto",         // per-family dot-gain pre-correction | "none"
    "sharpen": false,             // edge-aware unsharp on photos before halftoning
    "fax_heavy": false,           // bias toward clustered (compresses, robust)

    // ---- text / line-art binarization (never halftoned) ----
    "text_binarize": "contrast",  // "contrast" (force gray/light text to solid
                                  //   black) | "sauvola" | "niblack" | "wolf"
                                  //   | "bradley" | "otsu"
    "text_in_image": true,        // rescue text baked into photos (captions,
                                  //   signs, scanned-as-one-image) so it stays
                                  //   legible instead of being halftoned
    "robust_image_text": "auto",  // rescue washout-prone COLORED text (e.g. yellow
                                  //   on cyan) that grayscale loses: detect by
                                  //   chroma, recolor to solid black, add a stroke
                                  //   only if the bg is too dark, and self-verify.
                                  //   "auto" acts only when found | "on" scans
                                  //   harder | "off" disables
    "robust_text_stroke": 0.15,   // thickness (× glyph height) of the contrasting
                                  //   stroke used only in the dark-background case
    "segmentation": "embedded",   // "embedded" (use PDF image rects)
                                  //   | "variance" (heuristic for flat scans)
                                  //   | "none" (whole page one strategy)
    "thicken": false,             // dilate thin strokes/small fonts so they survive
    "flatten_bg": true,           // push near-white to pure white before threshold
    "despeckle": true,            // remove isolated black pixels
    "deskew": true,               // straighten skewed scans
    "format": "pdf",              // "pdf" (CCITT G4 in PDF) | "tiff" (Class-F G4)
    "max_scanline_px": 1728,      // clamp page width
    "line_rate_bps": 14400,       // basis for transmission-time estimate
    "page_overhead_s": 1.5        // per-page fax handshake/overhead estimate
  },

  "report": "OUTPUT.report.json", // write machine-readable report here
  "preview_page": null            // 1-based page number to render as PNG, or null
}
```

> Note: `--compare-page N` and `--compare-methods a,b,c` (the multi-method
> "eye tokens" contact sheet) are CLI-only flags, not config keys.

## Annotated example — faxing scanned clinical paperwork with a photo

```jsonc
{
  "fax": {
    "resolution": "superfine",    // small print on intake forms needs the dpi
    "dither": "atkinson",         // one embedded photo, want detail, line is clean
    "segmentation": "embedded",   // forms are vector+image: route the photo only
    "thicken": true,              // faint signatures and small fonts
    "flatten_bg": true,           // scanner gave a gray cast; force white
    "despeckle": true,
    "deskew": true,
    "format": "pdf",
    "line_rate_bps": 14400
  },
  "report": "intake.report.json",
  "preview_page": 1               // eyeball page 1 before sending
}
```

## Report output shape

The `report` file (and `--report`) is written as JSON:

```jsonc
{
  "mode": "fax",
  "input": "intake.pdf",
  "output": "intake.fax.pdf",
  // (input_bytes/output_bytes are absolute file sizes, not a compression goal)
  "input_bytes": 4210233,
  "output_bytes": 188440,
  "pages": [
    {
      "index": 1,
      "encoded_bytes": 61220,
      "est_transmission_s": 35.5,
      "photo_regions": 1,
      "photo_fraction": 0.19,
      "dither": "green-noise",
      "text_binarize": "contrast",
      "already_bilevel": false,
      "robust_text": {              // present only when colored text was found
        "mode": "auto",
        "regions_detected": 2,
        "regions_recovered": 2,     // recolored & verified legible
        "regions_unrecovered": 0,   // reverted (left as the normal pipeline renders)
        "regions_rejected": 1,      // candidates that weren't text-like (skipped)
        "details": [ { "bbox": [x0,y0,x1,y1], "ink_before": 0.49,
                       "ink_after": 0.18, "cover": 0.12, "bg_luma": 163.0,
                       "backing": "none", "legible": true } ]
      },
      "warnings": ["small_text_below_min", "robust_text:recovered:2"]
    }
  ],
  "total_est_transmission_s": 142.0,
  "warnings": []
}
```

Always surface `total_est_transmission_s`, page count, and any `warnings` to the
user. Long per-page transmission times and `inverted_or_heavy_black` are the
signals to revisit knobs before sending. `robust_text:recovered:*` is
informational — colored text that would have washed out was recolored for contrast
and verified; per-region detail (incl. `bg_luma` and whether a stroke was needed)
is under `pages[].robust_text`.
