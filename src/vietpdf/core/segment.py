"""Thứ tự đọc, lọc đoạn cần dịch, đầu/chân trang, nối đoạn qua cột/trang, và vùng trống để nới khung."""

from __future__ import annotations

import itertools
import re
import unicodedata
from collections import defaultdict

from vietpdf.core.ir import MARKER_RE, PageIR, Segment

TRANSLATABLE_ROLES = {"body", "title", "caption", "header", "footer", "footnote", "ocr"}
_END_PUNCT = tuple('.!?:;"\u201d)]\u3002\uff01\uff1f')


def _letters(text: str) -> int:
    return sum(1 for ch in text if unicodedata.category(ch).startswith("L"))


def should_translate(seg: Segment, translate_tables: bool = False) -> bool:
    """Bỏ qua: đoạn quá ngắn, chỉ có số/ký hiệu, lỗi mã hoá font, công thức, chữ trong bảng/hình
    (trừ khi bật dịch cả ô bảng), OCR độ tin cậy thấp."""
    if "bad-encoding" in seg.flags or "ocr-low-confidence" in seg.flags:
        return False
    roles = TRANSLATABLE_ROLES | ({"in-table"} if translate_tables else set())
    if seg.role not in roles:
        return False
    text = MARKER_RE.sub("", seg.source).strip()
    return len(text) >= 2 and _letters(text) >= 2


def _norm_repeat(text: str) -> str:
    return re.sub(r"\d+", "#", MARKER_RE.sub("", text)).strip().lower()


def mark_headers_footers(pages: list[PageIR], min_pages: int = 3, ratio: float = 0.4) -> None:
    """Chữ lặp lại cùng vị trí (8% trên/dưới trang) trên nhiều trang => đầu/chân trang."""
    if len(pages) < min_pages:
        return
    groups: dict[tuple[str, str, int], list[Segment]] = defaultdict(list)
    for p in pages:
        for s in p.segments:
            if s.role not in ("body", "title"):
                continue
            if s.bbox[3] < p.height * 0.08:
                zone = "header"
            elif s.bbox[1] > p.height * 0.92:
                zone = "footer"
            else:
                continue
            groups[(zone, _norm_repeat(s.source), round(s.bbox[0] / 10))].append(s)
    need = max(min_pages, int(len(pages) * ratio))
    for (zone, _text, _x), segs in groups.items():
        if len({s.page for s in segs}) >= need:
            for s in segs:
                s.role = zone  # type: ignore[assignment]


def link_continuations(pages: list[PageIR]) -> list[list[Segment]]:
    """Đoạn bị cắt qua cột/trang: dòng cuối không có dấu kết câu và đoạn sau bắt đầu bằng chữ thường.
    Trả về các chuỗi đoạn cần dịch chung (mỗi chuỗi >= 2 đoạn)."""
    body = [s for p in pages for s in sorted(p.segments, key=lambda x: x.order) if s.role == "body"]
    chains: list[list[Segment]] = []
    for a, b in itertools.pairwise(body):
        ta = MARKER_RE.sub("", a.source).rstrip()
        tb = MARKER_RE.sub("", b.source).lstrip()
        if not ta or not tb or ta.endswith(_END_PUNCT) or not tb[0].islower():
            continue
        if a.style.family != b.style.family or abs(a.style.size - b.style.size) > 0.6:
            continue
        if b.page not in (a.page, a.page + 1):
            continue
        a.continues = b.id
        if chains and chains[-1][-1] is a:
            chains[-1].append(b)
        else:
            chains.append([a, b])
    return chains


def split_translation(text: str, weights: list[int]) -> list[str]:
    """Chia bản dịch của một chuỗi đoạn theo tỉ lệ độ dài bản gốc. Ưu tiên cắt ở ranh giới câu gần điểm chia
    (trong khoảng ±25% độ dài phần đó) để mỗi mảnh là câu trọn vẹn; không có thì cắt tại khoảng trắng."""
    if len(weights) == 1:
        return [text]
    words = text.split(" ")
    total = sum(weights) or 1
    lens = [len(w) + 1 for w in words]
    full = sum(lens)
    cum = [0]
    for ln in lens:
        cum.append(cum[-1] + ln)
    parts: list[str] = []
    start = 0
    acc_target = 0.0
    for wgt in weights[:-1]:
        acc_target += full * wgt / total
        window = full * wgt / total * 0.25
        # vị trí cắt "sau từ i" (i = start..len-2), chọn gần acc_target nhất; ưu tiên từ kết thúc câu
        cands = range(start + 1, len(words))
        if not cands:
            parts.append("")
            continue
        best = min(cands, key=lambda i: abs(cum[i] - acc_target))
        sentence_ends = [
            i for i in cands if words[i - 1].endswith((".", "!", "?", ";", ":")) and abs(cum[i] - acc_target) <= window
        ]
        if sentence_ends:
            best = min(sentence_ends, key=lambda i: abs(cum[i] - acc_target))
        parts.append(" ".join(words[start:best]))
        start = best
    parts.append(" ".join(words[start:]))
    return parts


def _xy_cut(segs: list[Segment], min_gap: float = 6.0, depth: int = 0) -> list[Segment]:
    """Thứ tự đọc theo XY-cut đệ quy: ưu tiên cắt DỌC (tách cột), không cắt được thì cắt NGANG (tách hàng).
    Đúng cho bố cục 2 cột xen kẽ bảng/hình/tiêu đề trải rộng."""
    if len(segs) <= 1 or depth > 40:
        return sorted(segs, key=lambda s: (s.bbox[1], s.bbox[0]))

    def cuts(axis: int) -> list[list[Segment]]:
        lo, hi = (0, 2) if axis == 0 else (1, 3)
        items = sorted(segs, key=lambda s: s.bbox[lo])
        groups: list[list[Segment]] = [[items[0]]]
        reach = items[0].bbox[hi]
        for s in items[1:]:
            if s.bbox[lo] - reach >= min_gap:
                groups.append([s])
            else:
                groups[-1].append(s)
            reach = max(reach, s.bbox[hi])
        return groups

    for axis in (0, 1):  # 0: cắt dọc (cột), 1: cắt ngang (hàng)
        groups = cuts(axis)
        if len(groups) > 1:
            return [x for g in groups for x in _xy_cut(g, min_gap, depth + 1)]
    return sorted(segs, key=lambda s: (s.bbox[1], s.bbox[0]))


def assign_reading_order(page: PageIR) -> None:
    """Đầu/chân trang (và chữ trong 5% mép trên/dưới) đứng riêng; phần thân xếp bằng XY-cut."""
    top = [s for s in page.segments if s.role == "header" or s.bbox[3] < page.height * 0.05]
    bottom = [s for s in page.segments if s not in top and (s.role == "footer" or s.bbox[1] > page.height * 0.95)]
    body = [s for s in page.segments if s not in top and s not in bottom]
    ordered = (
        sorted(top, key=lambda s: (s.bbox[1], s.bbox[0]))
        + _xy_cut(body)
        + sorted(bottom, key=lambda s: (s.bbox[1], s.bbox[0]))
    )
    for i, s in enumerate(ordered):
        s.order = i
    page.segments = ordered


def is_short(seg: Segment) -> bool:
    """Đoạn 1–2 dòng (tiêu đề, tên tác giả, chú thích hình)."""
    h = seg.bbox[3] - seg.bbox[1]
    return h < seg.style.size * seg.style.line_height * 2.6


def widened_bbox(page: PageIR, seg: Segment, gap: float = 6.0) -> tuple[float, float, float, float] | None:
    """Khung nới ngang cho đoạn ngắn, không đè khối khác cùng hàng; None nếu không áp dụng."""
    if not is_short(seg) or not page.segments:
        return None
    x0, y0, x1, y1 = seg.bbox
    left = max(min(s.bbox[0] for s in page.segments), page.width * 0.04)
    right = min(max(s.bbox[2] for s in page.segments), page.width * 0.96)
    others = [s.bbox for s in page.segments if s.id != seg.id] + list(page.obstacles)
    for ox0, oy0, ox1, oy1 in others:
        if oy1 <= y0 + 0.5 or oy0 >= y1 - 0.5:
            continue  # không cùng hàng
        if ox0 >= x1 - 0.5:
            right = min(right, ox0 - gap)
        elif ox1 <= x0 + 0.5:
            left = max(left, ox1 + gap)
        else:
            return None  # có vật chồng lên đoạn: không nới
    left, right = min(left, x0), max(right, x1)
    if seg.style.align == "center":
        room = min(x0 - left, right - x1)
        return (x0 - room, y0, x1 + room, y1)
    if seg.style.align == "right":
        return (left, y0, x1, y1)
    return (x0, y0, right, y1)


def free_space_below(
    page: PageIR, seg: Segment, margin: float = 2.0, x_range: tuple[float, float] | None = None
) -> float:
    """Toạ độ y lớn nhất mà khung của `seg` có thể nới xuống mà không đè khối/hình khác."""
    x0, _, x1, y1 = seg.bbox
    if x_range is not None:
        x0, x1 = x_range
    limit = page.height - max(18.0, page.height * 0.03)
    others = [s.bbox for s in page.segments if s.id != seg.id] + list(page.obstacles)
    for ox0, oy0, ox1, oy1 in others:
        if ox1 <= x0 + 0.5 or ox0 >= x1 - 0.5:
            continue  # không chồng theo chiều ngang
        if oy0 >= y1 - 0.5:
            limit = min(limit, oy0 - margin)
        elif oy1 > y1 + 0.5:
            # vật cản vắt qua cạnh dưới của đoạn (vd. khung viền bao quanh): không vượt cạnh dưới của nó
            limit = min(limit, oy1 - margin)
    return max(limit, y1)
