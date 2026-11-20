"""v1.0 — PDF khó: công thức trong dòng, phân số có gạch, bảng/biểu đồ giữ nguyên, đậm/nghiêng, đoạn nối qua cột,
đầu/chân trang, OCR, mô hình bố cục ONNX, dựng lại từ IR, ước tính chi phí."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import make_samples
import pymupdf
import pytest

from vietpdf.core.ir import DocumentIR
from vietpdf.core.pipeline import PipelineOptions, rerender, run_pipeline
from vietpdf.translate.fake import FakeTranslator


@pytest.fixture(scope="module")
def hard(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path, DocumentIR]:
    d = tmp_path_factory.mktemp("hard")
    src = d / "hard.pdf"
    make_samples.make_hard(src)
    out = d / "hard.vi.pdf"
    res = asyncio.run(run_pipeline(src, out, FakeTranslator(), PipelineOptions(auto_terms=False, dual=True)))
    assert res.failed == 0
    ir = DocumentIR.model_validate_json(res.ir_path.read_text("utf-8"))  # type: ignore[union-attr]
    return src, out, ir


def _seg(ir: DocumentIR, startswith: str):  # type: ignore[no-untyped-def]
    return next(s for s in ir.segments() if s.plain_source().startswith(startswith))


def test_inline_formulas_detected_and_kept(hard: tuple[Path, Path, DocumentIR]) -> None:
    _, out, ir = hard
    loss = _seg(ir, "We define the loss")
    # chữ font Symbol (font toán) + chỉ số trên được gom thành MỘT công thức, kể cả dấu "=" "+" ở giữa
    assert len(loss.formulas) == 1 and "=" in loss.formulas[0].text and "+" in loss.formulas[0].text
    assert "⟦M" in (loss.translation or "")  # ký hiệu công thức còn trong bản dịch
    ratio = _seg(ir, "The ratio")
    assert len(ratio.formulas) == 1  # tử số + mẫu số + gạch phân số gộp thành MỘT công thức
    f = ratio.formulas[0]
    assert f.bbox[3] - f.bbox[1] > 8  # khung bao cả tử, mẫu và gạch
    # Gạch phân số cũ đã bị xoá: không còn đường kẻ ngang ngắn ở chỗ cũ trên trang dịch
    src_lines = [
        d["rect"] for d in pymupdf.open(hard[0])[0].get_drawings() if d["rect"].height < 1 and d["rect"].width < 10
    ]
    out_lines = [
        d["rect"] for d in pymupdf.open(out)[0].get_drawings() if d["rect"].height < 1 and d["rect"].width < 10
    ]
    assert src_lines and not any(r == src_lines[0] for r in out_lines)


def test_tables_and_figures_untouched(hard: tuple[Path, Path, DocumentIR]) -> None:
    _src, out, ir = hard
    roles = {s.plain_source(): s.role for s in ir.segments()}
    assert roles["Baseline 81.2 0.45"] == "in-table"
    assert roles["Validation accuracy"] == "in-figure"
    assert roles["Training epochs"] == "in-figure"
    assert roles["Table 1: Comparison of methods on the test set."] == "caption"
    text = pymupdf.open(out)[0].get_text()
    for kept in ("Method", "Accuracy", "Baseline", "81.2", "Validation accuracy", "Training epochs"):
        assert kept in text
    kinds = {r.kind for r in ir.pages[0].regions}
    assert {"table", "figure"} <= kinds


def test_style_tags_and_continuation(hard: tuple[Path, Path, DocumentIR]) -> None:
    _, _, ir = hard
    loss = _seg(ir, "We define the loss")
    assert "⟦B⟧learning rate" in loss.source and "⟦I⟧not" in loss.source
    left = _seg(ir, "Deep learning models")
    right = _seg(ir, "loss function affects")
    assert left.continues == right.id and "joined" in left.flags and "joined" in right.flags
    assert left.order < right.order  # XY-cut: cột trái trước cột phải


def test_headers_footers_and_reading_order(hard: tuple[Path, Path, DocumentIR]) -> None:
    _, _, ir = hard
    headers = [s for s in ir.segments() if s.role == "header"]
    assert len(headers) == 3  # "Journal of Testing" lặp lại trên 3 trang
    assert all(s.order == 0 for s in headers)


def test_rerender_from_edited_ir(hard: tuple[Path, Path, DocumentIR], tmp_path: Path) -> None:
    src, _out, ir = hard
    seg = _seg(ir, "Robust Estimation")
    seg.translation = "Ước lượng bền vững khi phân phối dịch chuyển"
    ir_path = tmp_path / "edited.ir.json"
    ir_path.write_text(ir.model_dump_json(), "utf-8")
    new = asyncio.run(rerender(src, ir_path, tmp_path / "edited.vi.pdf"))
    assert "Ước lượng bền vững" in pymupdf.open(new)[0].get_text()
    bad = tmp_path / "other.pdf"
    make_samples.make_paper(bad)
    with pytest.raises(ValueError, match="SHA-256"):
        asyncio.run(rerender(bad, ir_path, tmp_path / "x.pdf"))


def test_estimate_dry_run_makes_no_calls(hard: tuple[Path, Path, DocumentIR]) -> None:
    fake = FakeTranslator()
    res = asyncio.run(run_pipeline(hard[0], None, fake, PipelineOptions(dry_run=True)))
    assert fake.calls == 0 and res.output is None
    assert res.estimate["segments"] > 5 and res.estimate["tokens"] > 0


def test_translate_tables_option(tmp_path: Path) -> None:
    src = tmp_path / "h.pdf"
    make_samples.make_hard(src, pages=1)
    res = asyncio.run(
        run_pipeline(
            src, tmp_path / "h.vi.pdf", FakeTranslator(), PipelineOptions(auto_terms=False, translate_tables=True)
        )
    )
    ir = DocumentIR.model_validate_json(res.ir_path.read_text("utf-8"))  # type: ignore[union-attr]
    assert _seg(ir, "Baseline").status == "translated"


def test_report_written(hard: tuple[Path, Path, DocumentIR]) -> None:
    src = hard[0]
    md = src.with_name("hard.report.md")
    assert md.exists() and "Báo cáo dịch" in md.read_text("utf-8")
    data = json.loads(md.with_suffix(".json").read_text("utf-8"))
    assert "flags" in data


@pytest.mark.skipif(
    not __import__("vietpdf.core.ocr", fromlist=["available"]).available(), reason="cần vietpdf-translate[ocr]"
)
def test_ocr_scanned_page(tmp_path: Path) -> None:
    src = tmp_path / "scan.pdf"
    make_samples.make_scanned_text(src)
    opts = PipelineOptions(auto_terms=False, first_mention=False)
    res = asyncio.run(run_pipeline(src, tmp_path / "scan.vi.pdf", FakeTranslator(), opts))
    assert res.ocr_pages == [0] and res.translated >= 1
    ir = DocumentIR.model_validate_json(res.ir_path.read_text("utf-8"))  # type: ignore[union-attr]
    seg = ir.segments()[0]
    assert "overfitting" in seg.source and seg.role == "ocr" and (seg.confidence or 0) > 0.8
    assert seg.ocr_bg is not None and seg.ocr_bg != 0xFFFFFF  # nền giấy ngà, không phải trắng
    assert "overfitting" not in pymupdf.open(tmp_path / "scan.vi.pdf")[0].get_text()


def test_layout_model_integrity_and_parsing(tmp_path: Path) -> None:
    onnx = pytest.importorskip("onnx")
    np = pytest.importorskip("numpy")
    from onnx import TensorProto, helper

    from vietpdf.core.layout_model import ModelIntegrityError, detect_regions, parse_output

    # Mô hình giả: luôn trả về 1 hộp "table" ở giữa ảnh 1024x1024 (định dạng YOLOv10 [1, N, 6])
    boxes = np.array([[[200, 300, 800, 600, 0.9, 5]]], dtype=np.float32)
    const = helper.make_node(
        "Constant", [], ["out"], value=helper.make_tensor("v", TensorProto.FLOAT, boxes.shape, boxes.flatten().tolist())
    )
    graph = helper.make_graph(
        [const],
        "g",
        [helper.make_tensor_value_info("images", TensorProto.FLOAT, [1, 3, 1024, 1024])],
        [helper.make_tensor_value_info("out", TensorProto.FLOAT, list(boxes.shape))],
    )
    model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 13)])
    model.ir_version = 8
    path = tmp_path / "fake_layout.onnx"
    onnx.save(model, str(path))
    page = pymupdf.open()
    pg = page.new_page(width=512, height=512)
    with pytest.raises(ModelIntegrityError):  # chưa ghim SHA-256 -> từ chối nạp
        detect_regions(pg, str(path))
    from vietpdf.config import config_dir
    from vietpdf.core.layout_model import _session
    from vietpdf.security.integrity import sha256_file

    # file .sha256 đặt CẠNH mô hình không còn được tin (L1-016)
    path.with_suffix(".onnx.sha256").write_text(sha256_file(path))
    _session.cache_clear()
    with pytest.raises(ModelIntegrityError):
        detect_regions(pg, str(path))
    # ghim trong models.lock.json của người dùng (thư mục cấu hình) => nạp được
    config_dir().mkdir(parents=True, exist_ok=True)
    (config_dir() / "models.lock.json").write_text(
        json.dumps({"models": {"t": {"file": path.name, "sha256": sha256_file(path)}}})
    )
    _session.cache_clear()
    regs = detect_regions(pg, str(path))
    assert regs and regs[0]["kind"] == "table" and regs[0]["source"] == "model"
    assert regs[0]["bbox"][0] == pytest.approx(100, abs=1)  # quy đổi từ pixel 1024 về điểm PDF 512
    # Định dạng YOLOv8 [1, 4+nc, N] có NMS
    v8 = np.zeros((1, 14, 2), dtype=np.float32)
    v8[0, :4, 0] = [500, 500, 100, 100]
    v8[0, 4 + 3, 0] = 0.8
    v8[0, :4, 1] = [505, 505, 100, 100]
    v8[0, 4 + 3, 1] = 0.7
    assert len(parse_output(v8)) == 1
