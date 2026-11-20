"""Sinh PDF mẫu cho test (tự tạo => giấy phép rõ ràng). Chạy: python tests/fixtures/make_samples.py out_dir"""

from __future__ import annotations

import sys
from pathlib import Path

import pymupdf

LOREM = (
    "Machine learning models are prone to overfitting when the training set is small. "
    "In this work we leverage a pretrained encoder and show that the error decreases by 12.5% "
    "on the benchmark described in [12]. The approach does not require labeled data. "
    "Code is available at https://example.org/code and questions can be sent to author@example.org."
)
LOREM2 = (
    "We evaluate the neural network on three datasets. The results indicate that the "
    "proposed method improves accuracy by 3.2 points over the strongest baseline, while the "
    "inference time remains under 40 ms per image. The attention mechanism never attends to padding tokens."
)


def make_paper(path: Path) -> None:
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)  # A4
    page.insert_textbox(
        pymupdf.Rect(60, 50, 535, 90),
        "Efficient Learning with Small Training Sets",
        fontname="Times-Bold",
        fontsize=18,
        align=pymupdf.TEXT_ALIGN_CENTER,
    )
    page.insert_textbox(
        pymupdf.Rect(60, 95, 535, 115),
        "Anonymous Authors",
        fontname="Times-Italic",
        fontsize=11,
        align=pymupdf.TEXT_ALIGN_CENTER,
    )
    col_w = 230
    for col, text in enumerate([LOREM, LOREM2]):
        x0 = 60 + col * (col_w + 15)
        page.insert_textbox(
            pymupdf.Rect(x0, 130, x0 + col_w, 330),
            text,
            fontname="Times-Roman",
            fontsize=10,
            align=pymupdf.TEXT_ALIGN_JUSTIFY,
        )
    # "Hình": khung + đường kẻ + chữ trong hình (phải được giữ nguyên)
    page.draw_rect(pymupdf.Rect(60, 360, 535, 520), color=(0.1, 0.3, 0.8), width=1.2)
    page.draw_line((80, 500), (515, 380), color=(0.8, 0.1, 0.1), width=2)
    page.insert_textbox(
        pymupdf.Rect(60, 525, 535, 545),
        "Figure 1: Accuracy as a function of training set size.",
        fontname="Helvetica",
        fontsize=9,
        align=pymupdf.TEXT_ALIGN_LEFT,
    )
    page.insert_textbox(
        pymupdf.Rect(60, 570, 535, 700),
        LOREM2 + " " + LOREM2,
        fontname="Helvetica",
        fontsize=10,
        align=pymupdf.TEXT_ALIGN_JUSTIFY,
    )
    page.insert_text((290, 820), "1", fontname="Times-Roman", fontsize=9)

    p2 = doc.new_page(width=595, height=842)
    p2.insert_textbox(pymupdf.Rect(60, 60, 535, 90), "2 Related Work", fontname="Helvetica-Bold", fontsize=14)
    p2.insert_textbox(
        pymupdf.Rect(60, 95, 535, 260),
        LOREM + " " + LOREM2,
        fontname="Times-Roman",
        fontsize=11,
        align=pymupdf.TEXT_ALIGN_JUSTIFY,
    )
    p2.insert_textbox(pymupdf.Rect(60, 270, 535, 290), "for i in range(10): print(i)", fontname="Courier", fontsize=10)
    p2.insert_text((290, 820), "2", fontname="Times-Roman", fontsize=9)
    doc.set_toc([[1, "Introduction", 1], [1, "Related Work", 2]])
    doc.set_metadata({"title": "Efficient Learning with Small Training Sets"})
    doc.save(str(path))
    doc.close()


def make_with_javascript(path: Path) -> None:
    """PDF có JavaScript chạy khi mở (OpenAction) — file đầu ra phải bị xoá JS."""
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), "This document contains an embedded script.", fontname="Helvetica", fontsize=12)
    js = doc.get_new_xref()
    doc.update_object(js, "<< /Type /Action /S /JavaScript /JS (app.alert\\('hello'\\);) >>")
    doc.xref_set_key(doc.pdf_catalog(), "OpenAction", f"{js} 0 R")
    doc.embfile_add("payload.txt", b"embedded attachment")
    doc.save(str(path))
    doc.close()


def make_scan_like(path: Path) -> None:
    doc = pymupdf.open()
    page = doc.new_page()
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 400, 500), False)
    pix.set_rect(pix.irect, (240, 240, 230))
    page.insert_image(page.rect, pixmap=pix)
    doc.save(str(path))
    doc.close()


def _line(page: pymupdf.Page, x: float, y: float, pieces: list[tuple[str, str, float, float]]) -> float:
    """Viết một dòng gồm nhiều mảnh (chữ, font, cỡ, độ lệch đường cơ sở); trả về x cuối."""
    for text, font, size, dy in pieces:
        page.insert_text((x, y + dy), text, fontname=font, fontsize=size)
        x += pymupdf.get_text_length(text, fontname=font, fontsize=size)
    return x


def make_hard(path: Path, pages: int = 3) -> None:
    """PDF khó: công thức trong dòng, phân số có gạch, bảng, biểu đồ có chữ, đậm/nghiêng giữa câu,
    đoạn nối qua cột, đầu/chân trang lặp lại."""
    doc = pymupdf.open()
    tr, it, bd, sy = "Times-Roman", "Times-Italic", "Times-Bold", "Symbol"
    for n in range(pages):
        page = doc.new_page(width=595, height=842)
        page.insert_text((60, 40), "Journal of Testing, Vol. 3", fontname="Helvetica", fontsize=8)
        page.insert_text((290, 815), str(n + 1), fontname="Helvetica", fontsize=8)
        if n == 0:
            page.insert_textbox(
                pymupdf.Rect(60, 60, 535, 90),
                "Robust Estimation under Distribution Shift",
                fontname="Times-Bold",
                fontsize=16,
                align=pymupdf.TEXT_ALIGN_CENTER,
            )
            y = 120
            # dòng có công thức: "the loss  L(q) = a + b2  is minimized when  q  is small."
            x = _line(
                page,
                60,
                y,
                [
                    ("We define the loss ", tr, 10, 0),
                    ("L", it, 10, 0),
                    ("(", tr, 10, 0),
                    ("q", sy, 10, 0),
                    (") = ", tr, 10, 0),
                    ("a", sy, 10, 0),
                    (" + ", tr, 10, 0),
                    ("b", sy, 10, 0),
                    ("2", tr, 6.5, -3.5),
                    (" which is", tr, 10, 0),
                ],
            )
            _line(
                page,
                60,
                y + 13,
                [
                    ("minimized when the ", tr, 10, 0),
                    ("learning rate", bd, 10, 0),
                    (" is small and the model is ", tr, 10, 0),
                    ("not", it, 10, 0),
                    (" overfitting the data.", tr, 10, 0),
                ],
            )
            # phân số trong dòng có gạch ngang (nét vẽ thuộc công thức)
            x = _line(page, 60, y + 32, [("The ratio ", tr, 10, 0), ("a", sy, 7, -4)])
            page.draw_line((x - 6, y + 29.5), (x + 1, y + 29.5), width=0.5)
            _line(page, x - 5.5, y + 35, [("b", sy, 7, 0)])
            _line(page, x + 4, y + 32, [(" controls the trade-off between bias and variance in training.", tr, 10, 0)])
            # hai cột, đoạn cột trái bị cắt giữa câu và tiếp tục ở cột phải
            left = (
                "Deep learning models trained on large datasets often fail when the test distribution "
                "differs from the training distribution, and in this paper we study how the choice of the"
            )
            right = (
                "loss function affects robustness. We show that a simple reweighting scheme improves accuracy "
                "by 4.1 points without additional data."
            )
            page.insert_textbox(
                pymupdf.Rect(60, 175, 290, 260), left, fontname=tr, fontsize=10, align=pymupdf.TEXT_ALIGN_JUSTIFY
            )
            page.insert_textbox(
                pymupdf.Rect(305, 175, 535, 260), right, fontname=tr, fontsize=10, align=pymupdf.TEXT_ALIGN_JUSTIFY
            )
            # bảng có kẻ ô: chữ trong bảng phải giữ nguyên
            x0, y0 = 100, 300
            rows = [["Method", "Accuracy", "Loss"], ["Baseline", "81.2", "0.45"], ["Ours", "85.3", "0.38"]]
            for i in range(4):
                page.draw_line((x0, y0 + i * 20), (x0 + 360, y0 + i * 20), width=0.6)
            for j in range(4):
                page.draw_line((x0 + j * 120, y0), (x0 + j * 120, y0 + 60), width=0.6)
            for i, row in enumerate(rows):
                for j, cell in enumerate(row):
                    page.insert_text((x0 + 6 + j * 120, y0 + 14 + i * 20), cell, fontname=tr, fontsize=9)
            page.insert_textbox(
                pymupdf.Rect(60, 368, 535, 384),
                "Table 1: Comparison of methods on the test set.",
                fontname=tr,
                fontsize=9,
            )
            # biểu đồ vector có nhãn chữ: giữ nguyên
            page.draw_rect(pymupdf.Rect(120, 410, 470, 600), width=0.8)
            for k in range(6):
                page.draw_line((140 + k * 60, 590), (140 + k * 60, 595))
            page.draw_polyline(
                [(140, 580), (200, 540), (260, 520), (320, 470), (380, 455), (440, 430)],
                color=(0.8, 0.1, 0.1),
                width=1.5,
            )
            page.insert_text((270, 615), "Training epochs", fontname="Helvetica", fontsize=8)
            page.insert_text((125, 405), "Validation accuracy", fontname="Helvetica", fontsize=8)
            page.insert_textbox(
                pymupdf.Rect(60, 625, 535, 641),
                "Figure 1: Validation accuracy increases steadily during training.",
                fontname=tr,
                fontsize=9,
            )
        else:
            page.insert_textbox(
                pymupdf.Rect(60, 70, 535, 300),
                (LOREM + " ") * 2,
                fontname=tr,
                fontsize=11,
                align=pymupdf.TEXT_ALIGN_JUSTIFY,
            )
    doc.set_toc([[1, "Introduction", 1], [1, "Experiments", 2]])
    doc.save(str(path))
    doc.close()


def make_scanned_text(path: Path) -> None:
    """Trang 'scan' có chữ thật trong ảnh (để test OCR)."""
    src = pymupdf.open()
    p = src.new_page(width=595, height=842)
    p.draw_rect(p.rect, color=None, fill=(0.96, 0.94, 0.88))
    p.insert_textbox(
        pymupdf.Rect(72, 100, 520, 300),
        "Machine learning models are prone to overfitting when the training set is small. "
        "The approach does not require labeled data.",
        fontname="Times-Roman",
        fontsize=14,
    )
    pix = p.get_pixmap(dpi=150)
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_image(page.rect, pixmap=pix)
    doc.save(str(path))
    doc.close()


if __name__ == "__main__":
    out = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
    out.mkdir(parents=True, exist_ok=True)
    make_paper(out / "paper.pdf")
    make_with_javascript(out / "js.pdf")
    make_scan_like(out / "scan.pdf")
    make_hard(out / "hard.pdf")
    make_scanned_text(out / "scantext.pdf")
    print("ok")
