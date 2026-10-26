"""Unit tests for the K/A/Kn line parser (apex_tracker.parse_kak).

Runs under pytest, or standalone with the project venv:
``.venv\\Scripts\\python.exe test_parse_kak.py`` (importing apex_tracker needs its
OCR deps). Covers clean reads, the fused-slash misread that silently dropped a
ranked win on 2026-10-25 ('7 / 7 / 7' OCR'd as '71717'), and the ambiguous fused
reads that must stay unread so the watch loop retries instead of guessing.
"""

from apex_tracker import parse_kak


# --------------------------------------------------------------------------- #
# clean reads are unchanged
# --------------------------------------------------------------------------- #
def test_clean_read():
    assert parse_kak("8/6/10") == (8, 6, 10)


def test_clean_read_with_spaces_and_noise():
    assert parse_kak(" 2 / 5 / 2 ") == (2, 5, 2)


def test_three_groups_never_resplit():
    # A '1' that is a real stat must not be treated as a slash.
    assert parse_kak("1/11/1") == (1, 11, 1)


# --------------------------------------------------------------------------- #
# fused-slash recovery
# --------------------------------------------------------------------------- #
def test_fully_fused_live_case():
    assert parse_kak("71717") == (7, 7, 7)


def test_fully_fused_two_digit_stat():
    assert parse_kak("101213") == (10, 2, 3)


def test_fully_fused_all_ones():
    assert parse_kak("11111") == (1, 1, 1)


def test_partly_fused():
    assert parse_kak("7/717") == (7, 7, 7)


# --------------------------------------------------------------------------- #
# unrecoverable reads stay unread (caller retries, nothing guessed)
# --------------------------------------------------------------------------- #
def test_ambiguous_fused_stays_unread():
    k, a, kn = parse_kak("111111")  # 11/1/1, 1/11/1 or 1/1/11
    assert a is None and kn is None


def test_too_short_to_split_stays_unread():
    assert parse_kak("717") == (717, None, None)


def test_half_rendered_stays_unread():
    assert parse_kak("2/5") == (2, 5, None)


def test_empty():
    assert parse_kak("") == (None, None, None)


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
