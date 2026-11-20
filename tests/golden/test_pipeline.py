"""Kiểm thử đầu-cuối với translator giả (không gọi mạng)."""

from __future__ import annotations

import asyncio
import re
import unicodedata
from pathlib import Path

import pymupdf
import pytest

from vietpdf.core.ir import DocumentIR
from vietpdf.core.pipeline import PipelineOptions, PipelineResult, parse_page_ranges, run_pipeline
from vietpdf.translate.cache import TranslationCache
from vietpdf.translate.fake import FakeTranslator


def _run(src: Path, out: Path, **kw: object) -> PipelineResult:
    kw = {"auto_terms": False, "first_mention": False, **kw}  # test bố cục: tách khỏi thuật ngữ do LLM giả đề xuất
    opts = PipelineOptions(**kw)  # type: ignore[arg-type]
    return asyncio.run(run_pipeline(src, out, FakeTranslator(), opts))


def _norm(s: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFC", s))


def test_paper_translation_preserves_layout(samples: Path, tmp_path: Path) -> None:
    out = tmp_path / "paper.vi.pdf"
    res = _run(samples / "paper.pdf", out, dual=True)
    assert res.failed == 0 and res.translated >= 8
    src, dst = pymupdf.open(samples / "paper.pdf"), pymupdf.open(out)
    assert dst.page_count == src.page_count
    for i in range(src.page_count):
        assert dst[i].rect == src[i].rect
        # Hình/đường kẻ giữ nguyên
        assert [d["rect"] for d in dst[i].get_drawings()] == [d["rect"] for d in src[i].get_drawings()]
    ir = DocumentIR.model_validate_json(res.ir_path.read_text("utf-8"))  # type: ignore[union-attr]
    page_text = _norm("".join(p.get_text() for p in dst))
    for seg in ir.segments():
        if seg.status == "translated":
            # Kiểm tra dấu: chữ trích lại từ PDF đầu ra phải khớp 100% bản dịch
            assert _norm(seg.translation or "") in page_text, seg.id
            x0, _y0, _x1, y1 = seg.bbox
            assert x0 >= 0 and y1 <= dst[seg.page].rect.height
    # URL, email, trích dẫn còn nguyên
    for frag in ("https://example.org/code", "author@example.org", "[12]", "12.5"):
        assert frag in dst[0].get_text()
    # Không còn chữ tiếng Anh của thân bài
    assert "overfitting" not in dst[0].get_text()
    # Mục lục được dịch, bản song ngữ có gấp đôi số trang
    assert dst.get_toc()[0][1] != "Introduction"
    assert pymupdf.open(res.dual).page_count == 2 * src.page_count  # type: ignore[arg-type]
    # Font nhúng dạng subset
    assert all("+" in f[3] for f in dst.get_page_fonts(0) if "Noto" in f[3])


def test_javascript_and_attachments_are_scrubbed(samples: Path, tmp_path: Path) -> None:
    out = tmp_path / "js.vi.pdf"
    _run(samples / "js.pdf", out)
    raw = out.read_bytes()
    assert b"/JavaScript" not in raw and b"app.alert" not in raw
    assert pymupdf.open(out).embfile_count() == 0


def test_scan_page_is_detected_and_skipped(samples: Path, tmp_path: Path) -> None:
    res = _run(samples / "scan.pdf", tmp_path / "scan.vi.pdf")
    # có OCR: trang được OCR (ảnh trơn, không có chữ); không có OCR: báo trang scan chưa dịch
    assert (res.scan_pages == [1] or res.ocr_pages == [0]) and res.translated == 0


def test_page_selection_and_cache_resume(samples: Path, tmp_path: Path) -> None:
    cache = TranslationCache(tmp_path / "c.db")
    fake = FakeTranslator()
    opts = PipelineOptions(pages=[1], auto_terms=False)
    r1 = asyncio.run(run_pipeline(samples / "paper.pdf", tmp_path / "a.vi.pdf", fake, opts, cache=cache))
    assert r1.pages == 1
    calls = fake.calls
    r2 = asyncio.run(run_pipeline(samples / "paper.pdf", tmp_path / "b.vi.pdf", fake, opts, cache=cache))
    assert fake.calls == calls and r2.requests == 0 and r2.cached >= r1.translated  # chạy lại không tốn request


def test_corrupt_pdf_fails_cleanly(samples: Path, tmp_path: Path) -> None:
    data = (samples / "paper.pdf").read_bytes()
    bad = tmp_path / "truncated.pdf"
    bad.write_bytes(data[: len(data) // 3])
    try:
        res = _run(bad, tmp_path / "t.vi.pdf")
    except Exception as e:  # lỗi rõ ràng, không treo, không sập tiến trình chính
        assert type(e).__name__ in {"SandboxError", "SandboxCrashedError", "ValueError"}
    else:
        assert res.pages >= 0
    garbage = tmp_path / "garbage.pdf"
    garbage.write_bytes(b"%PDF-1.7\n" + bytes(range(256)) * 50)
    with pytest.raises(Exception):  # noqa: B017
        _run(garbage, tmp_path / "g.vi.pdf")


def test_output_cannot_overwrite_input(samples: Path) -> None:
    with pytest.raises(ValueError):
        _run(samples / "paper.pdf", samples / "paper.pdf")


def test_parse_page_ranges() -> None:
    assert parse_page_ranges("1-3,5", 10) == [0, 1, 2, 4]
    assert parse_page_ranges("8-", 10) == [7, 8, 9]
    with pytest.raises(ValueError):
        parse_page_ranges("0", 10)
