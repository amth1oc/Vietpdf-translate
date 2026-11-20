from __future__ import annotations

import unicodedata
from pathlib import Path

import pytest

from vietpdf.core.fit import Box, layout_text
from vietpdf.core.fonts import FONT_FILES, classify_family, default_fontbook, vietnamese_chars
from vietpdf.core.protect import PlaceholderError, protect, restore
from vietpdf.translate.glossary import Glossary, GlossaryEntry, load_csv
from vietpdf.translate.qa import check_meaning, sanitize_output


def test_protect_and_restore_roundtrip() -> None:
    src = (
        "See https://example.org/a?b=1, mail a.b@example.com, doi 10.1145/3292500.3330701 and [3, 5-7] with `x+1` α≤β."
    )
    pr = protect(src)
    for frag in ["https://example.org/a?b=1", "a.b@example.com", "10.1145/3292500.3330701", "[3, 5-7]", "`x+1`"]:
        assert frag not in pr.text
        assert frag in pr.mapping.values()
    vi = pr.text.replace("See", "Xem").replace("mail", "thư")
    assert restore(vi, pr).startswith("Xem https://example.org/a?b=1,")


def test_restore_detects_lost_or_extra_placeholder() -> None:
    pr = protect("Visit https://x.org now [1].")
    with pytest.raises(PlaceholderError):
        restore("Truy cập ngay.", pr)
    with pytest.raises(PlaceholderError):
        restore(pr.text + " ⟦F9⟧", pr)


def test_source_with_bracket_chars_is_safe() -> None:
    pr = protect("weird ⟦F1⟧ text")
    assert restore(pr.text, pr) == "weird ⟦F1⟧ text"


def test_vietnamese_charset_and_fonts() -> None:
    chars = vietnamese_chars()
    assert len(chars) == 134 and len(set(chars)) == 134
    assert all(unicodedata.is_normalized("NFC", c) for c in chars)
    fb = default_fontbook()
    for key in FONT_FILES:
        assert fb.missing_vietnamese(*key) == []


@pytest.mark.parametrize(
    ("name", "flags", "fam"),
    [
        ("ABCDEF+TimesNewRomanPSMT", 0, "serif"),
        ("Arial-BoldMT", 0, "sans"),
        ("Microsoft Sans Serif", 0, "sans"),
        ("CourierNew", 0, "mono"),
        ("XYZ", 8, "mono"),
        ("Unknown", 0, "serif"),
    ],
)
def test_font_classification(name: str, flags: int, fam: str) -> None:
    assert classify_family(name, flags) == fam


def _measure(text: str, size: float) -> float:
    return len(text) * size * 0.5


def test_fit_chain_order() -> None:
    box = Box(0, 0, 100, 24)  # ~2 dòng ở cỡ 10
    kw = dict(measure=_measure, ascender=0.9, descender=-0.25, base_size=10, line_height=1.3)
    short = layout_text("một hai ba", box, **kw)  # type: ignore[arg-type]
    assert short.status == "ok" and short.scale == 1.0
    long_text = " ".join(["từ"] * 40)
    expanded = layout_text(long_text, box, expand_to_y=200, **kw)  # type: ignore[arg-type]
    assert expanded.status == "expanded" and expanded.scale == 1.0
    shrunk = layout_text(" ".join(["từ"] * 25), box, **kw)  # type: ignore[arg-type]
    assert shrunk.status in ("shrunk", "overflow") and shrunk.scale < 1.0
    assert all(w for ln in shrunk.lines for _, w in ln.words)  # không mất chữ
    assert sum(len(ln.words) for ln in shrunk.lines) == 25


def test_fit_justify_fills_width() -> None:
    lay = layout_text(
        " ".join(["abc"] * 30),
        Box(0, 0, 100, 500),
        measure=_measure,
        ascender=0.9,
        descender=-0.25,
        base_size=10,
        align="justify",
    )
    first = lay.lines[0]
    last_x, last_w = first.words[-1]
    assert abs(last_x + _measure(last_w, lay.size) - 100) < 0.5


def test_glossary_longest_match_and_plural(tmp_path: Path) -> None:
    g = Glossary(
        [
            GlossaryEntry("neural network", "mạng nơ-ron"),
            GlossaryEntry("convolutional neural network", "mạng nơ-ron tích chập"),
            GlossaryEntry("API", "API", policy="keep"),
        ]
    )
    found = {e.en for e in g.match("Convolutional neural networks use an API.")}
    assert found == {"convolutional neural network", "API"}
    csv_path = tmp_path / "g.csv"
    csv_path.write_text("english,vietnamese,ghi_chu\nthread,luồng,CNTT\n,missing,\n", "utf-8")
    assert [e.vi for e in load_csv(csv_path)] == ["luồng"]


def test_meaning_checks() -> None:
    assert "qa:negation" in check_meaning("The method does not converge.", "Phương pháp hội tụ.")
    assert "qa:negation" not in check_meaning("The method does not converge.", "Phương pháp không hội tụ.")
    assert "qa:number" in check_meaning("Accuracy is 93.5%.", "Độ chính xác là 95%.")
    assert "qa:direction" in check_meaning("The error decreases.", "Sai số tăng lên.")
    assert check_meaning("The error decreases by 3%.", "Sai số giảm 3%.") == []


def test_sanitize_output() -> None:
    assert sanitize_output("a\u202eb\x00c", 10) == "abc"  # bỏ ký tự điều khiển/bidi
    assert sanitize_output("Tie\u0302\u0301ng", 10) == "Tiếng"  # NFD -> NFC
    assert sanitize_output("x" * 1000, 10) is None  # dài bất thường
    assert sanitize_output("   ", 10) is None
