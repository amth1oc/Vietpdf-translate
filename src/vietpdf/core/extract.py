"""PDF -> IR ở mức ký tự. CHỈ chạy trong tiến trình con (xem core/sandbox.py).

Các bước cho mỗi trang:
1. Đọc ký tự bằng `get_text("rawdict")`: mã Unicode, khung, đường cơ sở, font, cỡ, cờ, màu.
2. Đánh dấu ký tự toán (font toán, ký tự Unicode toán, chỉ số trên/dưới) và gom thành công thức trong dòng.
3. Gom dòng thành đoạn; đánh dấu đoạn chữ đậm/nghiêng khác kiểu chính bằng thẻ ⟦B⟧/⟦I⟧.
4. Nhận diện vùng bố cục (bảng, hình) bằng heuristic + mô hình ONNX tuỳ chọn; gán vai trò cho đoạn.
"""

from __future__ import annotations

import itertools
import re
import statistics
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from vietpdf.core.fonts import classify_family, is_bold, is_italic
from vietpdf.security.limits import DEFAULT_LIMITS

MATH_FONT_RE = re.compile(
    r"(cmmi|cmsy|cmex|cmbsy|cmmib|msam|msbm|eufm|eusm|rsfs|stmary|wasy|math|mtextra|mt extra|stix|xits|"
    r"lmmath|latinmodernmath|asana|txsy|txex|pxsy|pxex|esint|bbold|dsrom|mathpazo|euler|symbolmt|^symbol$)",
    re.IGNORECASE,
)
CAPTION_RE = re.compile(r"^\s*(figure|fig\.|table|tab\.|algorithm|listing|scheme|chart|hình|bảng)\s*[\dIVXA-Z]", re.I)
_BRIDGE_CHARS = set("=+-\u2212()[]{},.;:<>|/*'^_!0123456789 ")
_BAD_CODEPOINTS = ("\ufffd",)


def is_math_char(c: str) -> bool:
    o = ord(c)
    return (
        0x0370 <= o <= 0x03FF
        or 0x2200 <= o <= 0x22FF
        or 0x2A00 <= o <= 0x2AFF
        or 0x27C0 <= o <= 0x27EF
        or 0x2980 <= o <= 0x29FF
        or 0x1D400 <= o <= 0x1D7FF
        or 0x2070 <= o <= 0x209F
        or c in "±×÷√′″‴"
    )


def _is_bad_char(c: str) -> bool:
    return c in _BAD_CODEPOINTS or unicodedata.category(c) == "Co"


class EncryptedPdfError(Exception):
    pass


def _open(path: str, password: str | None = None) -> Any:
    import pymupdf  # noqa: PLC0415

    doc = pymupdf.open(path, filetype="pdf")
    if doc.needs_pass and not (password and doc.authenticate(password)):
        doc.close()
        raise EncryptedPdfError("PDF được mã hoá và cần mật khẩu")
    return doc


def doc_info(path: str, password: str | None = None) -> dict[str, Any]:
    doc = _open(path, password)
    try:
        if doc.page_count > DEFAULT_LIMITS.max_pages:
            raise ValueError(f"PDF có {doc.page_count} trang, vượt giới hạn {DEFAULT_LIMITS.max_pages}")
        toc = [
            {"level": int(lvl), "title": str(title)[:2000], "page": int(pg)}
            for lvl, title, pg, *_ in doc.get_toc(simple=True)
        ][:20000]
        meta = doc.metadata or {}
        return {"page_count": doc.page_count, "toc": toc, "title": str(meta.get("title") or "")[:2000]}
    finally:
        doc.close()


# --------------------------------------------------------------------------- ký tự & dòng


@dataclass
class Ch:
    c: str
    bbox: tuple[float, float, float, float]
    oy: float
    size: float
    font: str
    flags: int
    color: int
    math: bool = False

    @property
    def bold(self) -> bool:
        return is_bold(self.font, self.flags)

    @property
    def italic(self) -> bool:
        return is_italic(self.font, self.flags)


@dataclass
class Line:
    chars: list[Ch]
    bbox: tuple[float, float, float, float]
    baseline: float = 0.0
    size: float = 0.0
    items: list[Any] = field(default_factory=list)  # Ch hoặc dict công thức
    marks: set[str] = field(default_factory=set)  # faint|ocr|hidden|parthidden|overlap|unchecked (2.1.2)


# --------------------------------------------------------------------------- chữ không nhìn thấy (L1-010, R1-005)
#
# 2.1.2: quyết định chữ có NHÌN THẤY được hay không bằng cách SO ẢNH RENDER, không đoán theo màu:
#   ảnh A = trang đầy đủ; ảnh B = cùng trang đã xoá riêng CHỮ (redaction chỉ xoá chữ, giữ ảnh và đồ hoạ).
#   Ký tự mà vùng của nó trên A và B không khác nhau => người đọc không thấy (bị phủ bởi hình/ảnh vẽ SAU chữ, cùng màu
#   nền, độ mờ ~0, chế độ vẽ vô hình, bị cắt...). Bắt mọi kiểu che mà không bỏ nhầm chữ trắng trên nền tối.
# Ba mức cho mỗi ký tự:
#   "v" nhìn thấy rõ -> dịch bình thường
#   "f" mờ (có khác biệt nhưng rất nhỏ, vd. #F5F5F5 trên xanh rất nhạt) -> VẪN dịch (không mất nội dung), nhưng đoạn
#       bị gắn `security:low-contrast`, dịch riêng và liệt kê trong báo cáo
#   ẩn -> KHÔNG gửi tới LLM theo mặc định, nhưng liệt kê trong báo cáo/GUI (trang, vị trí, 80 ký tự đầu) — không lặng
#       lẽ bỏ; người dùng có thể bật "dịch cả chữ không nhìn thấy".
# Cỡ chữ: theo CHIỀU CAO GLYPH trong không gian thiết bị (khung ký tự sau mọi phép biến đổi), không theo cỡ danh nghĩa.

MIN_GLYPH_HEIGHT = 3.0  # pt (thiết bị, 72 dpi): thấp hơn => không đọc được bằng mắt
_VIS_ZOOM = 1.0
# lượng tử hoá để đếm điểm ảnh khác nhau theo ngưỡng mà không cần numpy: mức khác >= ~bước => khác nhóm ở CẢ 2 bảng
_Q_STRONG = (bytes(v // 64 for v in range(256)), bytes((v + 32) // 64 for v in range(256)))
_Q_FAINT = (bytes(v // 8 for v in range(256)), bytes((v + 4) // 8 for v in range(256)))


def _xor_or_rows(a: bytes, b: bytes, rows: list[tuple[int, int]]) -> int:
    """OR theo cột của (a XOR b) trên các hàng — byte khác 0 ở cột nào có ít nhất một hàng khác nhau."""
    acc = 0
    for lo, hi in rows:
        x = a[lo:hi]
        y = b[lo:hi]
        if x != y:
            acc |= int.from_bytes(x, "little") ^ int.from_bytes(y, "little")
    return acc


def _nonzero_bytes(v: int, start: int, end: int) -> int:
    if v == 0 or end <= start:
        return 0
    chunk = ((v >> (8 * start)) & ((1 << (8 * (end - start))) - 1)).to_bytes(end - start, "little")
    return (end - start) - chunk.count(0)


class _Visibility:
    """So ảnh trang đầy đủ với ảnh trang đã xoá chữ (một lần mỗi trang)."""

    def __init__(self, page: Any, textless_page: Any) -> None:
        import pymupdf  # noqa: PLC0415

        m = pymupdf.Matrix(_VIS_ZOOM, _VIS_ZOOM)
        full = page.get_pixmap(matrix=m, alpha=False, colorspace=pymupdf.csRGB)
        textless_page.add_redact_annot(textless_page.rect, fill=False)
        textless_page.apply_redactions(
            images=pymupdf.PDF_REDACT_IMAGE_NONE,
            graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
            text=pymupdf.PDF_REDACT_TEXT_REMOVE,
        )
        empty = textless_page.get_pixmap(matrix=m, alpha=False, colorspace=pymupdf.csRGB)
        if (full.width, full.height) != (empty.width, empty.height):
            raise ValueError("kích thước ảnh render khác nhau")
        self.w, self.h, self.stride, self.n = full.width, full.height, full.stride, full.n
        fs, es = full.samples, empty.samples
        self.same = fs == es
        self.q = [(fs.translate(t), es.translate(t)) for t in (*_Q_STRONG, *_Q_FAINT)]

    def classify(self, chars: list[dict[str, Any]], page_rect: Any) -> list[str]:
        """Trả về mức cho từng ký tự: "v" | "f" | "h:tiny" | "h:offpage" | "h:noink"."""
        out: list[str] = []
        if not chars:
            return out
        z = _VIS_ZOOM
        ys = [c["bbox"][1] for c in chars] + [c["bbox"][3] for c in chars]
        py0 = max(0, int(min(ys) * z))
        py1 = min(self.h, int(max(ys) * z + 0.999))
        accs = [0, 0, 0, 0]
        if not self.same and py1 > py0:
            xs0 = min(c["bbox"][0] for c in chars)
            xs1 = max(c["bbox"][2] for c in chars)
            bx0 = max(0, int(xs0 * z)) * self.n
            bx1 = min(self.w, int(xs1 * z + 0.999)) * self.n
            rows = [(y * self.stride + bx0, y * self.stride + bx1) for y in range(py0, py1)]
            accs = [_xor_or_rows(a, b, rows) for a, b in self.q]
            base = bx0
        for c in chars:
            x0, y0, x1, y1 = c["bbox"]
            cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
            if not (page_rect.x0 <= cx <= page_rect.x1 and page_rect.y0 <= cy <= page_rect.y1):
                out.append("h:offpage")
                continue
            if y1 - y0 < MIN_GLYPH_HEIGHT:
                out.append("h:tiny")
                continue
            if self.same or py1 <= py0:
                out.append("h:noink")
                continue
            s0 = max(0, int(x0 * z)) * self.n - base
            s1 = min(self.w, max(int(x0 * z) + 1, int(x1 * z + 0.999))) * self.n - base
            strong = min(_nonzero_bytes(accs[0], s0, s1), _nonzero_bytes(accs[1], s0, s1))
            if strong:
                out.append("v")
                continue
            faint = min(_nonzero_bytes(accs[2], s0, s1), _nonzero_bytes(accs[3], s0, s1))
            out.append("f" if faint else "h:noink")
        return out


_REASON_VI = {
    "h:tiny": "chữ quá nhỏ",
    "h:offpage": "nằm ngoài trang",
    "h:noink": "không hiện trên trang (bị che, trùng màu nền, trong suốt hoặc vô hình)",
}


def _heuristic_level(sp: dict[str, Any], ch: dict[str, Any], page_rect: Any) -> str:
    """Dự phòng khi không render được trang: chỉ các dấu hiệu chắc chắn; còn lại coi là "mờ" (gắn cờ, không bỏ)."""
    x0, y0, x1, y1 = ch["bbox"]
    if not (page_rect.x0 <= (x0 + x1) / 2 <= page_rect.x1 and page_rect.y0 <= (y0 + y1) / 2 <= page_rect.y1):
        return "h:offpage"
    if y1 - y0 < MIN_GLYPH_HEIGHT:
        return "h:tiny"
    if sp.get("alpha", 255) == 0:
        return "h:noink"
    return "f"


def _read_lines(
    page: Any, textless_page: Any = None, include_hidden: bool = False
) -> tuple[list[list[Line]], list[list[float]], int, list[dict[str, Any]]]:
    import pymupdf  # noqa: PLC0415

    flags = pymupdf.TEXTFLAGS_RAWDICT & ~pymupdf.TEXT_PRESERVE_IMAGES
    data = page.get_text("rawdict", flags=flags)
    blocks: list[list[Line]] = []
    rotated: list[list[float]] = []
    page_rect = page.rect
    vis: _Visibility | None = None
    vis_error = False
    if textless_page is not None:
        try:
            vis = _Visibility(page, textless_page)
        except Exception:
            vis_error = True
    else:
        vis_error = True
    hidden = 0
    hidden_runs: list[dict[str, Any]] = []
    images = [pymupdf.Rect(i["bbox"]) & page_rect for i in page.get_image_info()]
    img_area = sum(r.width * r.height for r in images if not r.is_empty)
    image_page = img_area / max(page_rect.width * page_rect.height, 1.0) > 0.6

    def flush_run(run: list[dict[str, Any]], reason: str) -> None:
        if not run:
            return
        text = "".join(c["c"] for c in run).strip()
        if not text:
            return
        bs = [c["bbox"] for c in run]
        hidden_runs.append(
            {
                "bbox": [min(b[0] for b in bs), min(b[1] for b in bs), max(b[2] for b in bs), max(b[3] for b in bs)],
                "text": text[:200],
                "reason": reason,
            }
        )

    for block in data.get("blocks", []):
        if block.get("type") != 0:
            continue
        lines: list[Line] = []
        for ln in block.get("lines", []):
            if abs(ln["dir"][0] - 1) > 1e-3 or abs(ln["dir"][1]) > 1e-3:
                rotated.append(list(ln["bbox"]))  # chữ xoay: giữ nguyên, coi là vật cản
                continue
            chars: list[Ch] = []
            marks: set[str] = set()
            n_alnum = n_faint = n_hidden = 0
            for sp in ln["spans"]:
                raw = sp.get("chars", [])
                if vis is not None:
                    levels = vis.classify(raw, page_rect)
                else:
                    levels = [_heuristic_level(sp, ch, page_rect) for ch in raw]
                run: list[dict[str, Any]] = []
                run_reason = ""
                for ch, level in zip(raw, levels, strict=True):
                    lv = level
                    if ch["c"].isspace():
                        if run:
                            run.append(ch)
                        lv = "v"  # khoảng trắng đi theo chữ quanh nó
                    elif lv.startswith("h:"):
                        in_image = any(r.contains(pymupdf.Point(*ch["origin"])) for r in images)
                        if lv == "h:noink" and image_page and in_image:
                            lv = "o"  # lớp chữ OCR vô hình đặt trên ảnh scan: dùng lại, gắn cờ, liệt kê
                        else:
                            hidden += 1
                            n_hidden += 1
                            if run_reason not in ("", lv):
                                flush_run(run, _REASON_VI[run_reason])
                                run = []
                            run.append(ch)
                            run_reason = lv
                            if not include_hidden:
                                continue
                            marks.add("hidden")
                    if not lv.startswith("h:") and not ch["c"].isspace() and run:
                        flush_run(run, _REASON_VI[run_reason])
                        run, run_reason = [], ""
                    if ch["c"].isalnum():
                        n_alnum += 1
                        n_faint += lv == "f"
                    if lv == "o":
                        marks.add("ocr")
                    chars.append(
                        Ch(
                            ch["c"],
                            tuple(ch["bbox"]),
                            float(ch["origin"][1]),
                            float(sp["size"]),
                            str(sp["font"]),
                            int(sp["flags"]),
                            int(sp["color"]) & 0xFFFFFF,
                        )
                    )
                flush_run(run, _REASON_VI.get(run_reason, ""))
            while chars and chars[-1].c.isspace():
                chars.pop()
            # mờ: đủ nhiều chữ-số mờ (một dấu chấm nhỏ khử răng cưa nhạt không tính); bị che một phần: cả dòng đáng ngờ
            if n_faint and n_faint >= max(3, 0.3 * n_alnum):
                marks.add("faint")
            if n_hidden and any(not c.c.isspace() for c in chars) and not include_hidden:
                marks.add("parthidden")
            if chars and any(not c.c.isspace() for c in chars):
                bs = [c.bbox for c in chars if not c.c.isspace()]
                lb = (min(b[0] for b in bs), min(b[1] for b in bs), max(b[2] for b in bs), max(b[3] for b in bs))
                if vis_error:
                    marks.add("unchecked")
                lines.append(Line(chars, lb, marks=marks))
        if lines:
            blocks.append(lines)
    _mark_overlaps(blocks)
    return blocks, rotated, hidden, hidden_runs


def _mark_overlaps(blocks: list[list[Line]]) -> None:
    """Chữ KHÁC nhau chồng đúng lên nhau (giấu chữ dưới chữ nhìn thấy — so ảnh không tách được): gắn cờ. Chữ GIỐNG
    nhau chồng lên nhau (giả đậm) là bình thường."""
    cells: dict[tuple[int, int], list[tuple[Ch, Line]]] = {}
    for lines in blocks:
        for ln in lines:
            for c in ln.chars:
                if c.c.isspace():
                    continue
                key = (round(c.bbox[0]), round(c.oy))
                cells.setdefault(key, []).append((c, ln))
    for items in cells.values():
        if len(items) > 1 and len({c.c for c, _ in items}) > 1:
            for _, ln in items:
                ln.marks.add("overlap")


def _stacked(a: Line, b: Line) -> bool:
    h = min(a.bbox[3] - a.bbox[1], b.bbox[3] - b.bbox[1])
    v_overlap = min(a.bbox[3], b.bbox[3]) - max(a.bbox[1], b.bbox[1])
    size = max(c.size for c in a.chars + b.chars)
    h_gap = max(a.bbox[0], b.bbox[0]) - min(a.bbox[2], b.bbox[2])
    short = min(sum(not c.c.isspace() for c in a.chars), sum(not c.c.isspace() for c in b.chars))
    min_w = min(a.bbox[2] - a.bbox[0], b.bbox[2] - b.bbox[0])
    if h <= 0 or v_overlap <= 0.3 * h:
        return False
    # (1) một bên là mảnh rất ngắn (tử/mẫu số, chỉ số) nằm sát bên — không gộp nhầm hai cột chữ
    # (2) hai dòng lồng nhau một chút theo chiều ngang (phân số chen giữa câu) — dòng thường không như vậy
    return (short <= 8 and h_gap < 0.8 * size) or (h_gap < 0 and -h_gap < 0.3 * min_w and v_overlap > 0.4 * h)


def _merge_blocks(blocks: list[list[Line]], max_lines: int = 600) -> list[list[Line]]:
    """Gộp các khối có dòng nằm trên cùng một dòng thị giác (vd. tử số/mẫu số bị MuPDF tách thành khối riêng)."""
    if sum(len(b) for b in blocks) > max_lines:
        return blocks
    parent = list(range(len(blocks)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i, bi in enumerate(blocks):
        for j in range(i + 1, len(blocks)):
            if find(i) != find(j) and any(_stacked(a, b) for a in bi for b in blocks[j]):
                parent[find(j)] = find(i)
    merged: dict[int, list[Line]] = {}
    for i, b in enumerate(blocks):
        merged.setdefault(find(i), []).extend(b)
    return [sorted(ls, key=lambda ln: ((ln.bbox[1] + ln.bbox[3]) / 2, ln.bbox[0])) for ls in merged.values()]


def _merge_stacked(lines: list[Line]) -> list[Line]:
    """MuPDF tách phân số / chỉ số lệch nhiều thành dòng riêng. Gộp các dòng chồng nhau theo chiều dọc
    (> 30% chiều cao) và liền kề theo chiều ngang thành một dòng thị giác."""
    out: list[Line] = []
    for ln in lines:
        if out and _stacked(out[-1], ln):
            prev = out[-1]
            chars = sorted(prev.chars + ln.chars, key=lambda c: (round(c.bbox[0], 1), c.bbox[1]))
            bbox = (
                min(prev.bbox[0], ln.bbox[0]),
                min(prev.bbox[1], ln.bbox[1]),
                max(prev.bbox[2], ln.bbox[2]),
                max(prev.bbox[3], ln.bbox[3]),
            )
            out[-1] = Line(chars, bbox, marks=prev.marks | ln.marks)
            continue
        out.append(ln)
    return out


def _mark_math(line: Line) -> None:
    chars = line.chars
    weights: Counter[float] = Counter()
    sizes: Counter[float] = Counter()
    for c in chars:
        if not c.c.isspace():
            weights[round(c.oy, 1)] += 1
            sizes[round(c.size, 1)] += 1
    line.baseline = weights.most_common(1)[0][0] if weights else line.bbox[3]
    line.size = sizes.most_common(1)[0][0] if sizes else 10.0
    for c in chars:
        if c.c.isspace():
            continue
        script = c.size < line.size * 0.85 and abs(c.oy - line.baseline) > line.size * 0.12
        c.math = bool(MATH_FONT_RE.search(c.font)) or is_math_char(c.c) or script
    # nối các ký tự toán bị ngắt bởi dấu =, (, ), chữ số… ngắn
    idx = [i for i, c in enumerate(chars) if c.math]
    for a, b in itertools.pairwise(idx):
        gap = chars[a + 1 : b]
        core = [c for c in gap if not c.c.isspace()]
        if gap and len(core) <= 3 and all(c.c in _BRIDGE_CHARS for c in gap):
            for c in gap:
                c.math = True
    # khoảng trắng đầu/cuối không thuộc công thức
    for c in chars:
        if c.c.isspace():
            c.math = False


def _build_items(line: Line, page_no: int, counter: list[int]) -> None:
    """Thay mỗi chuỗi ký tự toán liên tiếp bằng một đối tượng công thức."""
    items: list[Any] = []
    run: list[Ch] = []

    def flush() -> None:
        if not run:
            return
        x0 = min(c.bbox[0] for c in run)
        y0 = min(c.bbox[1] for c in run)
        x1 = max(c.bbox[2] for c in run)
        y1 = max(c.bbox[3] for c in run)
        key = f"M{page_no}x{counter[0]}"
        counter[0] += 1
        items.append(
            {
                "key": key,
                "page": page_no,
                "bbox": (x0 - 0.3, y0 - 0.3, x1 + 0.3, y1 + 0.3),
                "baseline_offset": line.baseline - (y0 - 0.3),
                "text": "".join(c.c for c in run)[:1000],
            }
        )
        run.clear()

    for i, c in enumerate(line.chars):
        if c.math:
            run.append(c)
            continue
        # khoảng trắng nằm giữa hai ký tự toán: giữ trong công thức
        if c.c.isspace() and run and i + 1 < len(line.chars) and line.chars[i + 1].math:
            run.append(c)
            continue
        flush()
        items.append(c)
    flush()
    line.items = items


def _split_paragraphs(lines: list[Line]) -> list[list[Line]]:
    """Tách khối thành đoạn khi cỡ chữ đổi rõ rệt hoặc có khoảng hở dọc lớn."""
    groups: list[list[Line]] = []
    prev: Line | None = None
    for ln in lines:
        if prev is not None:
            gap = ln.bbox[1] - prev.bbox[3]
            if abs(ln.size - prev.size) > 1.0 or gap > max(ln.size, prev.size) * 0.9:
                groups.append([])
        if not groups:
            groups.append([])
        groups[-1].append(ln)
        prev = ln
    return [g for g in groups if g]


def _detect_align(lines: list[Line], bbox: tuple[float, float, float, float], page_w: float) -> str:
    center = (bbox[0] + bbox[2]) / 2
    if len(lines) < 3:
        if abs(center - page_w / 2) < page_w * 0.03 and (bbox[2] - bbox[0]) < page_w * 0.8:
            return "center"
        return "left"
    tol = 2.5
    x0s = [ln.bbox[0] for ln in lines[1:]]
    x1s = [ln.bbox[2] for ln in lines[:-1]]
    left_ok = sum(abs(x - bbox[0]) < tol for x in x0s) >= len(x0s) * 0.8
    right_ok = sum(abs(x - bbox[2]) < tol for x in x1s) >= len(x1s) * 0.8
    if left_ok and right_ok:
        return "justify"
    centers = [(ln.bbox[0] + ln.bbox[2]) / 2 for ln in lines]
    if all(abs(c - center) < tol for c in centers) and not left_ok:
        return "center"
    return "left"


def _dominant_style(lines: list[Line]) -> dict[str, Any]:
    weight: Counter[tuple[str, float, int, int]] = Counter()
    for ln in lines:
        for c in ln.chars:
            if not c.c.isspace() and not c.math:
                weight[(c.font, round(c.size, 1), c.flags, c.color)] += 1
    if not weight:
        for ln in lines:
            for c in ln.chars:
                weight[(c.font, round(c.size, 1), c.flags, c.color)] += 1
    (font, size, flags, color), _ = weight.most_common(1)[0]
    gaps = [b.baseline - a.baseline for a, b in itertools.pairwise(lines) if b.baseline > a.baseline]
    lh = statistics.median(gaps) / size if gaps and size > 0 else 1.2
    return {
        "family": classify_family(font, flags),
        "bold": is_bold(font, flags),
        "italic": is_italic(font, flags),
        "size": max(1.0, min(float(size), 400.0)),
        "color": int(color),
        "line_height": max(0.8, min(lh, 3.0)),
        "source_font": str(font)[:200],
    }


def _paragraph_text(
    lines: list[Line], base_bold: bool, base_italic: bool
) -> tuple[str, list[dict[str, Any]], int, int]:
    """Ghép dòng thành văn bản có thẻ kiểu chữ + ký hiệu công thức. Trả về (text, formulas, n_math, n_chars)."""
    seq: list[Any] = []
    for ln in lines:
        items = list(ln.items)
        while items and isinstance(items[-1], Ch) and items[-1].c.isspace():
            items.pop()
        while items and isinstance(items[0], Ch) and items[0].c.isspace():
            items.pop(0)
        if not items:
            continue
        if seq:
            last = seq[-1]
            first = items[0]
            hyphen = (
                isinstance(last, Ch)
                and last.c in "-\u00ad"
                and len(seq) > 1
                and isinstance(seq[-2], Ch)
                and seq[-2].c.isalpha()
                and isinstance(first, Ch)
                and first.c.islower()
            )
            if hyphen:
                seq.pop()  # "trans-" + "lation" -> "translation"
            else:
                seq.append(" ")
        seq.extend(items)

    out: list[str] = []
    formulas: list[dict[str, Any]] = []
    state = (False, False)
    n_math = sum(len(f["text"].replace(" ", "")) for f in (x for x in seq if isinstance(x, dict)))
    n_chars = n_math

    def set_state(new: tuple[bool, bool]) -> None:
        nonlocal state
        if new == state:
            return
        if state[1]:
            out.append("⟦/I⟧")
        if state[0]:
            out.append("⟦/B⟧")
        if new[0]:
            out.append("⟦B⟧")
        if new[1]:
            out.append("⟦I⟧")
        state = new

    for x in seq:
        if isinstance(x, dict):
            out.append(f"⟦{x['key']}⟧")
            formulas.append(x)
        elif isinstance(x, str) or x.c.isspace():
            out.append(" ")
        else:
            n_chars += 1
            if _is_bad_char(x.c):
                out.append("\ufffd")
                continue
            set_state((x.bold and not base_bold, x.italic and not base_italic))
            out.append(x.c)
    set_state((False, False))
    text = re.sub(r"[ \t\u00a0]+", " ", "".join(out)).strip()
    text = re.sub(r"⟦([BI])⟧(\s*)⟦/\1⟧", r"\2", text)  # thẻ rỗng
    # Thẻ quá ngắn (1 ký tự) gây nhiễu cho LLM: bỏ
    text = re.sub(r"⟦([BI])⟧([^⟦\s])⟦/\1⟧", r"\2", text)
    return text, formulas, n_math, n_chars


# --------------------------------------------------------------------------- vùng bố cục


def _overlap_ratio(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> float:
    """Tỉ lệ diện tích của a nằm trong b."""
    x0, y0 = max(a[0], b[0]), max(a[1], b[1])
    x1, y1 = min(a[2], b[2]), min(a[3], b[3])
    if x1 <= x0 or y1 <= y0:
        return 0.0
    area = max((a[2] - a[0]) * (a[3] - a[1]), 1e-6)
    return (x1 - x0) * (y1 - y0) / area


def _heuristic_regions(
    page: Any, drawings: list[Any], seg_boxes: list[tuple[float, float, float, float]]
) -> list[dict[str, Any]]:
    import pymupdf  # noqa: PLC0415

    rect = page.rect
    page_area = max(rect.width * rect.height, 1.0)
    regions: list[dict[str, Any]] = []
    try:
        for t in page.find_tables().tables:
            regions.append({"kind": "table", "bbox": tuple(t.bbox), "score": 0.8})
    except Exception:  # noqa: S110 - trang lạ: bỏ qua nhận diện bảng, không làm hỏng cả trang
        pass
    for info in page.get_image_info():
        b = pymupdf.Rect(info["bbox"]) & rect
        if not b.is_empty and b.width * b.height > page_area * 0.01:
            regions.append({"kind": "figure", "bbox": (b.x0, b.y0, b.x1, b.y1), "score": 0.9})
    try:
        clusters = page.cluster_drawings(drawings=drawings) if drawings else []
    except Exception:
        clusters = []
    for r in clusters:
        box = (r.x0, r.y0, r.x1, r.y1)
        if r.width * r.height < page_area * 0.015 or r.width < 30 or r.height < 20:
            continue
        if r.width * r.height > page_area * 0.9:
            continue  # nền phủ cả trang
        if any(_overlap_ratio(box, reg["bbox"]) > 0.5 for reg in regions):
            continue
        n_items = sum(1 for d in drawings if _overlap_ratio(tuple(d["rect"]), box) > 0.9)
        text_cover = sum((s[2] - s[0]) * (s[3] - s[1]) for s in seg_boxes if _overlap_ratio(s, box) > 0.8) / max(
            r.width * r.height, 1.0
        )
        if n_items <= 4 and text_cover > 0.25:
            continue  # khung viền bao quanh đoạn chữ (hộp ghi chú): vẫn là văn bản
        regions.append({"kind": "figure", "bbox": box, "score": 0.6})
    return regions


def _assign_role(
    text: str, bbox: tuple[float, float, float, float], regions: list[dict[str, Any]], n_math: int, n_chars: int
) -> str:
    plain = re.sub(r"⟦[^⟧]*⟧", "", text)
    if CAPTION_RE.match(plain):
        return "caption"
    if n_chars and n_math / n_chars > 0.6:
        return "formula"
    for kind, role in (("abandon", "abandon"), ("table", "in-table"), ("figure", "in-figure"), ("formula", "formula")):
        if any(r["kind"] == kind and _overlap_ratio(bbox, r["bbox"]) >= 0.6 for r in regions):
            return role
    # nhãn ngắn sát mép hình (tên trục, chú giải) thuộc về hình
    if len(plain) < 60:
        for r in regions:
            if r["kind"] == "figure":
                x0, y0, x1, y1 = r["bbox"]
                if _overlap_ratio(bbox, (x0 - 20, y0 - 20, x1 + 20, y1 + 20)) >= 0.6:
                    return "in-figure"
    return "body"


def _attach_strokes(formulas: list[dict[str, Any]], drawings: list[Any], page_area: float) -> None:
    """Gắn nét vẽ (gạch phân số, dấu căn, ngoặc lớn) vào công thức: nét nhỏ nằm phần lớn trong khung công thức
    đã nới 1.5pt thì khung được mở rộng để bao trọn nét đó (để xoá đúng chỗ cũ và vẽ lại đủ ở chỗ mới)."""
    for f in formulas:
        x0, y0, x1, y1 = f["bbox"]
        grown = (x0 - 1.5, y0 - 1.5, x1 + 1.5, y1 + 1.5)
        for d in drawings:
            r = d.get("rect")
            if r is None or r.width * r.height > page_area * 0.002 or r.width > (x1 - x0) * 3 + 20:
                continue
            box = (r.x0, r.y0, r.x1, r.y1) if r.height > 0.1 else (r.x0, r.y0 - 0.3, r.x1, r.y1 + 0.3)
            if _overlap_ratio(box, grown) >= 0.5:
                x0, y0 = min(x0, box[0] - 0.3), min(y0, box[1] - 0.3)
                x1, y1 = max(x1, box[2] + 0.3), max(y1, box[3] + 0.3)
        if (x0, y0, x1, y1) != tuple(f["bbox"]):
            f["baseline_offset"] += f["bbox"][1] - y0
            f["bbox"] = (x0, y0, x1, y1)


# --------------------------------------------------------------------------- trang


# dấu của dòng -> cờ của đoạn (2.1.2)
_MARK_FLAGS = {
    "faint": "security:low-contrast",
    "hidden": "security:hidden-text",
    "overlap": "security:overlapping-text",
    "unchecked": "security:visibility-unchecked",
    "parthidden": "security:partly-hidden",
    "ocr": "ocr-layer",
}


def _page_ir(
    page: Any, number: int, layout_model: str | None = None, textless_page: Any = None, include_hidden: bool = False
) -> dict[str, Any]:
    import pymupdf  # noqa: PLC0415

    rect = page.rect
    blocks, rotated, hidden_chars, hidden_runs = _read_lines(page, textless_page, include_hidden)
    blocks = _merge_blocks(blocks)
    counter = [0]
    paragraphs: list[tuple[list[Line], dict[str, Any]]] = []
    total_chars = 0
    bad_chars = 0
    for lines in blocks:
        lines = _merge_stacked(lines)  # noqa: PLW2901
        for ln in lines:
            _mark_math(ln)
            _build_items(ln, number, counter)
        for group in _split_paragraphs(lines):
            paragraphs.append((group, _dominant_style(group)))

    raw_segments: list[dict[str, Any]] = []
    for idx, (group, style) in enumerate(paragraphs):
        text, formulas, n_math, n_chars = _paragraph_text(group, style["bold"], style["italic"])
        if not text.strip():
            continue
        total_chars += n_chars
        bad = text.count("\ufffd")
        bad_chars += bad
        if total_chars > DEFAULT_LIMITS.max_text_chars_per_page:
            raise ValueError(f"Trang {number + 1} có lượng chữ bất thường")
        bbox = (
            min(ln.bbox[0] for ln in group),
            min(ln.bbox[1] for ln in group),
            max(ln.bbox[2] for ln in group),
            max(ln.bbox[3] for ln in group),
        )
        style["align"] = _detect_align(group, bbox, rect.width)
        flags = ["bad-encoding"] if bad and bad / max(n_chars, 1) > 0.3 else []
        flags += sorted({_MARK_FLAGS[m] for ln in group for m in ln.marks if m in _MARK_FLAGS})
        raw_segments.append(
            {
                "id": f"p{number}-s{idx}",
                "page": number,
                "bbox": bbox,
                "source": text[:20000],
                "style": style,
                "flags": flags,
                "formulas": formulas,
                "_n": (n_math, n_chars),
            }
        )
        if len(raw_segments) > DEFAULT_LIMITS.max_segments_per_page:
            raise ValueError(f"Trang {number + 1} có quá nhiều khối chữ")

    drawings = page.get_drawings()[:5000]
    page_area = max(rect.width * rect.height, 1.0)
    regions = _heuristic_regions(page, drawings, [s["bbox"] for s in raw_segments])
    if layout_model:
        from vietpdf.core.layout_model import detect_regions  # noqa: PLC0415

        regions += detect_regions(page, layout_model)

    _attach_strokes([f for seg in raw_segments for f in seg["formulas"]], drawings, page_area)
    obstacles: list[list[float]] = list(rotated)
    img_area = 0.0
    for info in page.get_image_info():
        b = pymupdf.Rect(info["bbox"]) & rect
        if not b.is_empty:
            img_area += b.width * b.height
            obstacles.append([b.x0, b.y0, b.x1, b.y1])
    for d in drawings:
        r = d.get("rect")
        if r is not None and r.width * r.height < page_area * 0.9:
            obstacles.append([r.x0, r.y0, r.x1, r.y1])
    obstacles.extend(list(r["bbox"]) for r in regions if r["kind"] in ("table", "figure"))

    for s in raw_segments:
        n_math, n_chars = s.pop("_n")
        s["role"] = _assign_role(s["source"], s["bbox"], regions, n_math, n_chars)

    # Trang scan, hoặc font không có bảng Unicode trên phần lớn trang -> cần OCR
    is_scan = (total_chars < 30 and img_area / page_area > 0.6) or (total_chars > 50 and bad_chars / total_chars > 0.3)
    return {
        "number": number,
        "width": rect.width,
        "height": rect.height,
        "is_scan": is_scan,
        "segments": [] if is_scan else raw_segments,
        "obstacles": obstacles[:20000],
        "regions": [{**r, "source": r.get("source", "heuristic")} for r in regions][:2000],
        "hidden_chars": hidden_chars,
        "hidden_spans": [{**h, "bbox": [max(-1e4, min(1e5, float(v))) for v in h["bbox"]]} for h in hidden_runs[:500]],
    }


def extract_pages(
    path: str,
    page_numbers: list[int],
    password: str | None = None,
    layout_model: str | None = None,
    include_hidden: bool = False,
) -> list[dict[str, Any]]:
    """`include_hidden`: người dùng chọn dịch cả chữ không nhìn thấy (đoạn vẫn bị gắn `security:hidden-text`)."""
    doc = _open(path, password)
    textless = _open(path, password)  # bản sao để xoá chữ khi so ảnh render (không ghi ra đĩa)
    try:
        return [
            _page_ir(doc[n], n, layout_model, textless[n], include_hidden)
            for n in page_numbers
            if 0 <= n < doc.page_count
        ]
    finally:
        textless.close()
        doc.close()


def info_and_pages(
    path: str,
    password: str | None = None,
    layout_model: str | None = None,
    include_hidden: bool = False,
    max_pages: int = 20,
) -> dict[str, Any]:
    """`doc_info` + trích xuất toàn bộ trang khi tài liệu có ≤ `max_pages` trang — MỘT tiến trình con thay vì hai
    (2.1.2: bù chi phí của tiến trình con quét nội dung chủ động mới thêm cho R1-001, giữ hiệu năng tài liệu ngắn)."""
    info = doc_info(path, password)
    n = int(info["page_count"])
    pages = extract_pages(path, list(range(n)), password, layout_model, include_hidden) if n <= max_pages else None
    return {"info": info, "pages": pages}
