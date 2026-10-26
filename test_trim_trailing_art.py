"""Unit tests for apex_tracker.trim_trailing_art (the card-art phantom-digit fix).

Runs under pytest, or standalone with the project venv:
``.venv\\Scripts\\python.exe test_trim_trailing_art.py``. The array tests need only
numpy. The frame test re-creates the 2026-10-24 bug on a real summary (a bright
vertical art edge ~50px past the text OCR'd as a trailing '1': 2,616 -> 26161,
2/5/2 -> 2/5/21) and checks the trimmed read is clean; it is skipped when the local
sample frame is absent (it isn't committed: it shows squadmates' account ids).
"""

import os

import numpy as np

from apex_tracker import trim_trailing_art

GAP = 20
SAMPLE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                      "samples", "capture_20261025_195532.png")


# --------------------------------------------------------------------------- #
# builders
# --------------------------------------------------------------------------- #
def strip(*spans, width=150, height=30):
    """A dark crop with bright vertical blocks at the given (x0, x1) spans."""
    img = np.full((height, width, 3), 20, np.uint8)
    for x0, x1 in spans:
        img[5:25, x0:x1] = 240
    return img


# --------------------------------------------------------------------------- #
# array-level behaviour
# --------------------------------------------------------------------------- #
def test_art_after_wide_gap_is_cut():
    # "glyphs" at 2-40 with 6px gaps, art at 90-95 (50px gap)
    img = strip((2, 14), (20, 32), (38, 40), (90, 95))
    out = trim_trailing_art(img, GAP)
    assert 40 <= out.shape[1] < 90, out.shape
    assert not (out.max(axis=2) > 140).any(axis=0)[40:].any()  # no art columns left


def test_text_is_never_clipped():
    img = strip((2, 14), (20, 32), (38, 50), (90, 95))
    out = trim_trailing_art(img, GAP)
    assert np.array_equal(out[:, :50], img[:, :50])


def test_inner_gaps_below_threshold_kept():
    # 9px gaps (the widest measured inside a real value) must not trigger a cut
    img = strip((2, 12), (21, 31), (40, 50), (59, 69))
    out = trim_trailing_art(img, GAP)
    assert out.shape[1] >= 69


def test_no_wide_gap_returns_crop_unchanged():
    # art too close to the text (15px): fall back to the old, untrimmed crop
    img = strip((2, 40), (55, 60), width=70)
    assert trim_trailing_art(img, GAP) is img


def test_empty_crop_unchanged():
    img = strip()
    assert trim_trailing_art(img, GAP) is img


def test_open_trailing_space_is_trimmed_safely():
    img = strip((2, 40))
    out = trim_trailing_art(img, GAP)
    assert np.array_equal(out[:, :40], img[:, :40])


# --------------------------------------------------------------------------- #
# real frame: re-create the phantom digit, confirm the trimmed read is clean
# --------------------------------------------------------------------------- #
def test_painted_art_edge_on_real_summary():
    if not os.path.exists(SAMPLE):
        print("    (skipped: local sample frame not present)")
        return
    import cv2
    import apex_tracker as t
    frame = cv2.imread(SAMPLE)
    cfg = t.load_config()
    eff, _ = t.apply_profile(cfg, frame.shape[1], frame.shape[0], None)
    scale = t.scaler(eff, frame.shape[1], frame.shape[0])
    rows, vw = eff["rows"], eff.get("value_width", 150)
    for col in eff["columns"]:
        for rk in ("kak", "damage"):
            x0, y0, w, h = scale({"x": col["x"], "y": rows[rk]["y"], "w": vw, "h": rows[rk]["h"]})
            ink = np.flatnonzero((frame[y0:y0 + h, x0:x0 + w].max(axis=2) > 140).sum(axis=0) >= 2)
            bx = min(x0 + int(ink[-1]) + 50, x0 + w - 4)
            cv2.rectangle(frame, (bx, y0 + int(h * .15)), (bx + 2, y0 + int(h * .85)),
                          (245, 245, 245), -1)
    m = t.extract_match(frame, eff, scale)
    got = [(p["kills"], p["assists"], p["knocks"], p["damage"]) for p in m["players"]]
    assert got == [(7, 7, 7, 2616), (8, 6, 10, 2508), (2, 5, 2, 1477)], got


if __name__ == "__main__":
    import sys
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  FAIL  {t.__name__}  {e!r}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  ERROR {t.__name__}  {e!r}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed.")
    sys.exit(1 if failed else 0)
