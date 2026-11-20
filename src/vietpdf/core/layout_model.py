"""Nhận diện bố cục bằng mô hình DocLayout-YOLO dạng ONNX (tuỳ chọn, `pip install vietpdf-translate[layout]`).

Chạy trong tiến trình con. Mô hình KHÔNG được tải lúc chạy: người dùng/người đóng gói tải bằng
`scripts/fetch_models.py` (ghim SHA-256). File ONNX là đồ thị tính toán, không chứa mã Python (khác pickle);
ta không đăng ký custom op nào, nên nạp mô hình không thể thực thi mã tuỳ ý.

Hỗ trợ 2 định dạng đầu ra phổ biến:
- YOLOv10 (không cần NMS): [1, N, 6] = x1, y1, x2, y2, điểm, lớp
- YOLOv8: [1, 4 + số_lớp, N] = cx, cy, w, h, điểm từng lớp (cần NMS)
"""

from __future__ import annotations

import contextlib
import functools
import hmac
import json
from pathlib import Path
from typing import Any

from vietpdf.security.integrity import ASSETS_DIR, sha256_file

LABELS = [
    "title",
    "plain text",
    "abandon",
    "figure",
    "figure_caption",
    "table",
    "table_caption",
    "table_footnote",
    "isolate_formula",
    "formula_caption",
]
KIND = {
    "title": "title",
    "plain text": "text",
    "abandon": "abandon",
    "figure": "figure",
    "figure_caption": "caption",
    "table": "table",
    "table_caption": "caption",
    "table_footnote": "caption",
    "isolate_formula": "formula",
    "formula_caption": "caption",
}
LOCK_FILE = ASSETS_DIR / "models.lock.json"


class ModelIntegrityError(Exception):
    pass


def _user_lock_file() -> Path:
    from vietpdf.config import config_dir  # noqa: PLC0415

    return config_dir() / "models.lock.json"


def model_read_files(model_path: str | None) -> list[str]:
    """File tiến trình con cần ĐỌC để nạp mô hình (cho Landlock): file mô hình + models.lock.json của người dùng."""
    if not model_path:
        return []
    return [str(Path(model_path).resolve()), str(_user_lock_file())]


def pinned_sha256(model_path: str) -> str | None:
    """SHA-256 được ghim cho file mô hình, tra theo TÊN file trong:
    1. `assets/models.lock.json` (đi kèm ứng dụng, được kiểm toàn vẹn), rồi
    2. `models.lock.json` trong thư mục cấu hình của người dùng (người dùng tự ghim sau khi kiểm nguồn).
    KHÔNG tin file `<model>.sha256` nằm cạnh mô hình: ai phát tán mô hình cũng phát tán được file đó (L1-016)."""
    name = Path(model_path).name
    for lock_path in (LOCK_FILE, _user_lock_file()):
        try:
            lock = json.loads(lock_path.read_text("utf-8")) if lock_path.is_file() else {}
        except (OSError, ValueError):
            continue
        for entry in (lock.get("models", {}) if isinstance(lock, dict) else {}).values():
            if isinstance(entry, dict) and entry.get("file") == name and entry.get("sha256"):
                return str(entry["sha256"]).lower()
    return None


def disable_ort_telemetry(ort: Any) -> None:
    """Tắt telemetry ETW của onnxruntime (bản Windows). Bản >= 1.29 còn có telemetry OneDS (gửi qua mạng) không tắt
    được bằng API này => pyproject ghim onnxruntime < 1.29 và tiến trình con bị chặn mạng ở mức OS (L1-007)."""
    with contextlib.suppress(Exception):
        ort.disable_telemetry_events()


@functools.cache
def _session(model_path: str) -> Any:
    import onnxruntime as ort  # noqa: PLC0415

    disable_ort_telemetry(ort)
    expected = pinned_sha256(model_path)
    if not expected or len(expected) != 64:
        raise ModelIntegrityError(
            f"Chưa ghim SHA-256 cho {Path(model_path).name}. Sau khi kiểm tra nguồn tải, thêm mục "
            '{"models": {"<tên>": {"file": "<tên file>", "sha256": "<64 ký tự hex>"}}} vào '
            f"{_user_lock_file()} (không đặt cạnh file mô hình), hoặc dùng scripts/fetch_models.py."
        )
    if not hmac.compare_digest(sha256_file(Path(model_path)), expected):
        raise ModelIntegrityError(f"Sai checksum mô hình {Path(model_path).name}")
    opts = ort.SessionOptions()
    opts.intra_op_num_threads = 2
    return ort.InferenceSession(model_path, sess_options=opts, providers=["CPUExecutionProvider"])


def _nms(boxes: Any, scores: Any, thr: float = 0.5) -> list[int]:
    import numpy as np  # noqa: PLC0415

    order = scores.argsort()[::-1]
    keep: list[int] = []
    while order.size:
        i = int(order[0])
        keep.append(i)
        xx1 = np.maximum(boxes[i, 0], boxes[order[1:], 0])
        yy1 = np.maximum(boxes[i, 1], boxes[order[1:], 1])
        xx2 = np.minimum(boxes[i, 2], boxes[order[1:], 2])
        yy2 = np.minimum(boxes[i, 3], boxes[order[1:], 3])
        inter = np.clip(xx2 - xx1, 0, None) * np.clip(yy2 - yy1, 0, None)
        area = (boxes[:, 2] - boxes[:, 0]) * (boxes[:, 3] - boxes[:, 1])
        iou = inter / (area[i] + area[order[1:]] - inter + 1e-6)
        order = order[1:][iou < thr]
    return keep


def parse_output(out: Any, conf: float = 0.25) -> list[tuple[float, float, float, float, float, int]]:
    """Chuẩn hoá đầu ra mô hình thành danh sách (x1, y1, x2, y2, điểm, lớp) theo pixel ảnh đầu vào."""
    import numpy as np  # noqa: PLC0415

    a = np.asarray(out)
    if a.ndim == 3:
        a = a[0]
    if a.ndim != 2:
        return []
    if a.shape[1] == 6:  # YOLOv10
        rows = a[a[:, 4] >= conf]
        return [(float(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), int(r[5])) for r in rows]
    if a.shape[0] == 4 + len(LABELS):  # YOLOv8: [4+nc, N]
        a = a.T
        cls = a[:, 4:].argmax(1)
        sc = a[:, 4:].max(1)
        m = sc >= conf
        a, cls, sc = a[m], cls[m], sc[m]
        boxes = np.stack(
            [a[:, 0] - a[:, 2] / 2, a[:, 1] - a[:, 3] / 2, a[:, 0] + a[:, 2] / 2, a[:, 1] + a[:, 3] / 2], 1
        )
        keep = _nms(boxes, sc) if len(sc) else []
        return [(*map(float, boxes[i]), float(sc[i]), int(cls[i])) for i in keep]  # type: ignore[misc]
    return []


def detect_regions(page: Any, model_path: str, imgsz: int = 1024, conf: float = 0.25) -> list[dict[str, Any]]:
    import numpy as np  # noqa: PLC0415
    import pymupdf  # noqa: PLC0415

    sess = _session(model_path)
    rect = page.rect
    zoom = imgsz / max(rect.width, rect.height)
    pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False, colorspace=pymupdf.csRGB)
    img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.h, pix.w, 3)
    canvas = np.full((imgsz, imgsz, 3), 114, dtype=np.uint8)
    canvas[: pix.h, : pix.w] = img[:imgsz, :imgsz]
    x = canvas.astype(np.float32).transpose(2, 0, 1)[None] / 255.0
    out = sess.run(None, {sess.get_inputs()[0].name: x})[0]
    regions: list[dict[str, Any]] = []
    for x1, y1, x2, y2, score, cls in parse_output(out, conf):
        if not 0 <= cls < len(LABELS):
            continue
        bbox = (max(0.0, x1 / zoom), max(0.0, y1 / zoom), min(rect.width, x2 / zoom), min(rect.height, y2 / zoom))
        if bbox[2] - bbox[0] < 2 or bbox[3] - bbox[1] < 2:
            continue
        regions.append({"kind": KIND[LABELS[cls]], "bbox": bbox, "score": min(1.0, score), "source": "model"})
    return regions[:500]
