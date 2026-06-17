"""Optional OCR backend for the fax optimizer.

`robust_image_text` rescues text baked into a photo at the *signal* level — it
preserves whatever contrast survives. But text in a sign's specular highlight can
have near-zero luminance AND chroma contrast in the rendered grayscale, so there
is nothing to rescue. The *colour* image often still holds enough signal for a
recogniser, so this module reads the words with OCR and the pipeline re-typesets
them crisply — recovering text the signal path cannot.

Engine: rapidocr-onnxruntime (self-contained ONNX models, no system binary). It is
an OPTIONAL dependency — `available()` returns False and `recognize()` returns []
when it isn't installed, so the skill degrades gracefully to signal-level rescue.
"""
from __future__ import annotations

import numpy as np

_ENGINE = None
_TRIED = False


def available() -> bool:
    """True if an OCR engine can be loaded."""
    return _load() is not None


def _load():
    global _ENGINE, _TRIED
    if _TRIED:
        return _ENGINE
    _TRIED = True
    try:
        from rapidocr_onnxruntime import RapidOCR
        _ENGINE = RapidOCR()
    except Exception:
        _ENGINE = None
    return _ENGINE


def recognize(rgb: np.ndarray, conf_min: float = 0.5):
    """Recognise text in an RGB image.

    Returns a list of (text, quad, conf): `quad` is a 4×2 float array of the word
    box corners (clockwise from top-left) in pixel coordinates, so the caller can
    re-typeset the word fitted to its real position and tilt. Only words at or
    above `conf_min` and with at least two characters are returned. Empty list if
    no engine is available or nothing is read."""
    engine = _load()
    if engine is None:
        return []
    try:
        result, _ = engine(rgb)
    except Exception:
        return []
    out = []
    for box, text, conf in (result or []):
        text = (text or "").strip()
        if conf is None or float(conf) < conf_min or len(text) < 2:
            continue
        if sum(c.isalnum() for c in text) < 2:        # not a word/number
            continue
        out.append((text, np.asarray(box, dtype=np.float32), float(conf)))
    return out


def engine_name() -> str:
    return "rapidocr-onnxruntime" if available() else "none"
