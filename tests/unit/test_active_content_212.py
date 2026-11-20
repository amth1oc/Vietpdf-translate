"""R1-004 (2.1.2): PDF hợp lệ có annotation ngoài allowlist (Sound/Movie/FileAttachment) hoặc GoTo kèm /JS vô hại
không còn làm MẤT cả file đầu ra sau khi đã dịch: đối tượng bị xoá, báo cáo ghi số đã xoá; chốt kiểm cuối báo lỗi thì
làm sạch lại một lần mà KHÔNG gọi lại API. Test qua pipeline thật (tiến trình con thật)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pymupdf
import pytest
import sandbox_helpers as h

from vietpdf.core import render
from vietpdf.core.pipeline import PipelineOptions, run_pipeline
from vietpdf.translate.fake import FakeTranslator


class CountingFake(FakeTranslator):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    async def translate_batch(self, *a: Any, **k: Any) -> Any:
        self.calls += 1
        return await super().translate_batch(*a, **k)


def _legit_with_annot(path: Path, subtype: str, extra: str) -> None:
    d = pymupdf.open()
    p = d.new_page()
    p.insert_text((72, 72), f"Lecture notes with a legacy {subtype} annotation. The error rate decreased.")
    x = d.get_new_xref()
    d.update_object(x, f"<< /Type /Annot /Subtype /{subtype} /Rect [72 100 92 120] {extra} >>")
    d.xref_set_key(d.page_xref(0), "Annots", f"[{x} 0 R]")
    d.save(str(path))


def _goto_with_js(path: Path) -> None:
    d = pymupdf.open()
    d.new_page()
    d.new_page()
    d[0].insert_text((72, 72), "Lecture notes with an internal link to page two.")
    d[1].insert_text((72, 72), "Page two.")
    x = d.get_new_xref()
    action = f"<< /S /GoTo /D [{d.page_xref(1)} 0 R /Fit] /JS (void 0) >>"
    d.update_object(x, f"<< /Type /Annot /Subtype /Link /Rect [72 60 300 80] /A {action} >>")
    d.xref_set_key(d.page_xref(0), "Annots", f"[{x} 0 R]")
    d.save(str(path))


def _no_active(path: Path) -> None:
    render.assert_no_active_content(str(path))


@pytest.mark.parametrize(
    ("subtype", "extra"),
    [("Sound", "/Sound 1 0 R"), ("Movie", "/Movie << /F (clip.mov) >>"), ("FileAttachment", "/FS << /F (a.csv) >>")],
)
def test_r1004_legit_legacy_annotation_does_not_lose_output(tmp_path: Path, subtype: str, extra: str) -> None:
    src = tmp_path / "lecture.pdf"
    _legit_with_annot(src, subtype, extra)
    res = asyncio.run(run_pipeline(src, None, FakeTranslator(), PipelineOptions(auto_terms=False, dual=True)))
    assert res.output is not None and res.output.is_file() and res.dual is not None and res.dual.is_file()
    _no_active(res.output)
    _no_active(res.dual)
    report = res.report_path.read_text("utf-8")
    assert f"annotation {subtype}" in report and "Đã bỏ khi dựng PDF" in report


def test_r1004_goto_link_with_harmless_js_key_keeps_link(tmp_path: Path) -> None:
    src = tmp_path / "goto.pdf"
    _goto_with_js(src)
    res = asyncio.run(run_pipeline(src, None, FakeTranslator(), PipelineOptions(auto_terms=False)))
    _no_active(res.output)
    with pymupdf.open(res.output) as d:
        links = d[0].get_links()
    assert any(lk.get("kind") == pymupdf.LINK_GOTO and lk.get("page") == 1 for lk in links)


def test_r1004_final_gate_failure_rescrubs_once_without_api(
    tmp_path: Path, samples: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Chốt kiểm cuối (tiến trình con chỉ đọc) thấy JavaScript: làm sạch lại một lần, công bố file sạch; số lần gọi
    bộ dịch bằng đúng lần chạy bình thường (không dịch lại). Dựng lại từ IR (`vietpdf render`, không có bộ dịch)
    cũng đi qua cùng đường làm sạch lại."""
    from vietpdf.core.pipeline import rerender

    src = tmp_path / "paper.pdf"
    src.write_bytes((samples / "paper.pdf").read_bytes())
    base = CountingFake()
    asyncio.run(run_pipeline(src, tmp_path / "a.vi.pdf", base, PipelineOptions(auto_terms=False)))

    monkeypatch.setattr(render, "render_document", h.render_then_inject_js)  # hook chỉ có trong test
    t = CountingFake()
    res = asyncio.run(run_pipeline(src, tmp_path / "b.vi.pdf", t, PipelineOptions(auto_terms=False)))
    assert t.calls == base.calls, "bước làm sạch lại không được gọi API"
    _no_active(res.output)
    assert "làm sạch lại sau chốt kiểm cuối" in res.report_path.read_text("utf-8")
    out = asyncio.run(rerender(src, res.ir_path, tmp_path / "r.vi.pdf"))
    _no_active(out)
