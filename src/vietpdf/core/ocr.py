"""OCR trang scan / ảnh chụp bằng RapidOCR (mô hình PP-OCR dạng ONNX, Apache-2.0).

Tuỳ chọn: `pip install vietpdf-translate[ocr]`. CHỈ chạy trong tiến trình con. Mô hình nằm sẵn trong gói
rapidocr (khoá hash trong uv.lock); tiến trình con bị chặn mạng nên thư viện không thể tự tải thêm gì.

Kết quả là trang IR với các đoạn vai trò "ocr": khung chữ, cỡ chữ ước lượng, màu chữ và màu nền
(để che chữ gốc trên ảnh trước khi viết chữ Việt).
"""

from __future__ import annotations

import functools
import itertools
import logging
import statistics
from typing import Any

MAX_SIDE_PX = 3500
DPI = 300
MIN_CONFIDENCE = 0.6


def available() -> bool:
    try:
        import rapidocr  # noqa: F401, PLC0415
    except ImportError:
        return False
    return True


@functools.cache
def _engine() -> Any:
    logging.getLogger("RapidOCR").setLevel(logging.ERROR)
    import onnxruntime  # noqa: PLC0415
    from rapidocr import RapidOCR  # noqa: PLC0415

    from vietpdf.core.layout_model import disable_ort_telemetry  # noqa: PLC0415

    disable_ort_telemetry(onnxruntime)

    return RapidOCR(params={"Global.log_level": "error"})


def _median_color(pixels: Any) -> int:
    import numpy as np  # noqa: PLC0415

    if pixels.size == 0:
        return 0xFFFFFF
    med = np.median(pixels.reshape(-1, 3), axis=0).astype(int)
    return int(med[0]) << 16 | int(med[1]) << 8 | int(med[2])


def _colors(img: Any, x0: int, y0: int, x1: int, y1: int) -> tuple[int, int]:
    """(màu chữ, màu nền): nền = trung vị viền ngoài khung; chữ = trung vị 10% điểm tối nhất trong khung."""
    import numpy as np  # noqa: PLC0415

    h, w = img.shape[:2]
    pad = 3
    X0, Y0, X1, Y1 = max(0, x0 - pad), max(0, y0 - pad), min(w, x1 + pad), min(h, y1 + pad)
    border = np.concatenate(
        [
            img[Y0:y0, X0:X1].reshape(-1, 3),
            img[y1:Y1, X0:X1].reshape(-1, 3),
            img[Y0:Y1, X0:x0].reshape(-1, 3),
            img[Y0:Y1, x1:X1].reshape(-1, 3),
        ]
    )
    bg = _median_color(border)
    inner = img[y0:y1, x0:x1].reshape(-1, 3)
    if inner.size == 0:
        return 0, bg
    lum = inner.sum(1)
    dark = inner[lum <= np.percentile(lum, 10)]
    return _median_color(dark), bg


def _group_lines(lines: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Gom dòng OCR thành đoạn: cùng lề trái (±1.5 lần chiều cao), khoảng cách dọc nhỏ, chiều cao tương tự."""
    lines.sort(key=lambda d: (d["bbox"][1], d["bbox"][0]))
    groups: list[list[dict[str, Any]]] = []
    for ln in lines:
        h = ln["bbox"][3] - ln["bbox"][1]
        placed = False
        for g in reversed(groups[-4:]):
            last = g[-1]
            lh = last["bbox"][3] - last["bbox"][1]
            gap = ln["bbox"][1] - last["bbox"][3]
            if (
                abs(ln["bbox"][0] - g[0]["bbox"][0]) < 1.5 * h
                and -0.3 * h < gap < 0.9 * h
                and 0.7 < h / max(lh, 1e-3) < 1.4
            ):
                g.append(ln)
                placed = True
                break
        if not placed:
            groups.append([ln])
    return groups


def ocr_pages(path: str, page_numbers: list[int], password: str | None = None) -> list[dict[str, Any]]:
    import numpy as np  # noqa: PLC0415
    import pymupdf  # noqa: PLC0415

    from vietpdf.core.extract import _open  # noqa: PLC0415

    doc = _open(path, password)
    engine = _engine()
    pages: list[dict[str, Any]] = []
    try:
        for n in page_numbers:
            page = doc[n]
            rect = page.rect
            zoom = min(DPI / 72, MAX_SIDE_PX / max(rect.width, rect.height))
            pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False, colorspace=pymupdf.csRGB)
            img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.h, pix.w, 3)
            res = engine(img[..., ::-1].copy())  # RapidOCR dùng thứ tự kênh BGR
            lines: list[dict[str, Any]] = []
            for box, text, score in zip(
                res.boxes if res.boxes is not None else [], res.txts or [], res.scores or [], strict=False
            ):
                xs = [p[0] for p in box]
                ys = [p[1] for p in box]
                x0, y0, x1, y1 = int(min(xs)), int(min(ys)), int(max(xs)), int(max(ys))
                if x1 - x0 < 3 or y1 - y0 < 3 or not str(text).strip():
                    continue
                color, bg = _colors(img, x0, y0, x1, y1)
                lines.append(
                    {
                        "bbox": (x0 / zoom, y0 / zoom, x1 / zoom, y1 / zoom),
                        "text": str(text)[:2000],
                        "score": float(score),
                        "color": color,
                        "bg": bg,
                    }
                )
            segments = []
            for i, g in enumerate(_group_lines(lines)):
                heights = [ln["bbox"][3] - ln["bbox"][1] for ln in g]
                size = max(4.0, statistics.median(heights) * 0.78)
                bbox = (
                    min(ln["bbox"][0] for ln in g),
                    min(ln["bbox"][1] for ln in g),
                    max(ln["bbox"][2] for ln in g),
                    max(ln["bbox"][3] for ln in g),
                )
                conf = min(ln["score"] for ln in g)
                baselines = [ln["bbox"][3] for ln in g]
                gaps = [b - a for a, b in itertools.pairwise(baselines) if b > a]
                segments.append(
                    {
                        "id": f"p{n}-o{i}",
                        "page": n,
                        "bbox": bbox,
                        "role": "ocr",
                        "source": " ".join(ln["text"] for ln in g)[:20000],
                        "style": {
                            "family": "serif",
                            "size": size,
                            "color": g[0]["color"],
                            "line_height": max(1.0, min(statistics.median(gaps) / size, 3.0)) if gaps else 1.25,
                        },
                        "ocr_bg": statistics.mode([ln["bg"] for ln in g]),
                        "confidence": max(0.0, min(conf, 1.0)),
                        "flags": [] if conf >= MIN_CONFIDENCE else ["ocr-low-confidence"],
                    }
                )
            pages.append(
                {
                    "number": n,
                    "width": rect.width,
                    "height": rect.height,
                    "is_scan": True,
                    "ocr": True,
                    "segments": segments[:3000],
                    "obstacles": [],
                    "regions": [],
                }
            )
    finally:
        doc.close()
    return pages
