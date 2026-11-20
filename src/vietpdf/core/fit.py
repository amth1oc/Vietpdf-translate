"""Bộ dàn chữ: ngắt dòng, căn lề, và chuỗi cho vừa khung.

Thứ tự thử (đã chốt trong docs/DANH_GIA_KE_HOACH.md, mục 2):
  1. Giảm dãn dòng (không thấp hơn 1.12 vì tiếng Việt có dấu chồng)
  2. Nới khung: ngang (đoạn ngắn như tiêu đề, chú thích) rồi xuống phần trống bên dưới
  3. Co cỡ chữ từng bước 5% tới sàn `min_scale` (mặc định 0.7)
  4. (pipeline) Xin LLM dịch gọn hơn cho các đoạn còn tràn
  5. Co tiếp tới `hard_min_scale` và đánh dấu đoạn để người dùng xem lại

Lõi `fit_items` làm việc trên danh sách "từ" trừu tượng (chỉ cần độ rộng ở cỡ 1.0), nên dùng được cho
từ thuần chữ, từ có công thức trong dòng và từ có đoạn đậm/nghiêng. Module thuần, không phụ thuộc PDF.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

Measure = Callable[[str, float], float]
FitStatus = Literal["ok", "spacing", "expanded", "shrunk", "overflow"]


@dataclass(frozen=True)
class Box:
    x0: float
    y0: float
    x1: float
    y1: float

    @property
    def width(self) -> float:
        return self.x1 - self.x0

    @property
    def height(self) -> float:
        return self.y1 - self.y0


@dataclass
class Placement:
    """Kết quả dàn: mỗi dòng là danh sách (x, chỉ số từ)."""

    size: float
    line_height: float
    box: Box
    lines: list[list[tuple[float, int]]] = field(default_factory=list)
    baselines: list[float] = field(default_factory=list)
    status: FitStatus = "ok"
    scale: float = 1.0
    height_used: float = 0.0

    @property
    def needs_review(self) -> bool:
        return self.status == "overflow"


@dataclass
class PlacedLine:
    baseline: float
    words: list[tuple[float, str]]


@dataclass
class Layout:
    size: float
    line_height: float
    lines: list[PlacedLine] = field(default_factory=list)
    status: FitStatus = "ok"
    scale: float = 1.0
    height_used: float = 0.0

    @property
    def needs_review(self) -> bool:
        return self.status == "overflow"


def _wrap(widths: list[float], space: float, max_w: float) -> list[list[int]]:
    lines: list[list[int]] = []
    cur: list[int] = []
    cur_w = 0.0
    for i, w in enumerate(widths):
        add = w if not cur else space + w
        if cur and cur_w + add > max_w + 1e-6:
            lines.append(cur)
            cur, cur_w = [i], w
        else:
            cur.append(i)
            cur_w += add
    if cur:
        lines.append(cur)
    return lines


def fit_items(
    widths1: list[float],
    space1: float,
    box: Box,
    *,
    ascender: float,
    descender: float,
    base_size: float,
    line_height: float = 1.2,
    align: str = "left",
    expand_to_y: float | None = None,
    wide_box: Box | None = None,
    min_scale: float = 0.7,
    hard_min_scale: float = 0.4,
    step: float = 0.05,
    min_line_height: float = 1.12,
) -> Placement:
    if not widths1 or box.width <= 1:
        return Placement(size=base_size, line_height=line_height, box=box)
    glyph_h = max(ascender - descender, 1.0)

    def attempt(size: float, lh: float, b: Box) -> list[list[int]] | None:
        lines = _wrap([w * size for w in widths1], space1 * size, b.width)
        needed = (len(lines) - 1) * size * lh + glyph_h * size
        return lines if needed <= b.height + 0.5 else None

    lh_min = min(max(1.0, line_height), min_line_height)
    lh0 = max(lh_min, line_height)
    spacing_steps = [lh0] + [x for x in (1.2, 1.15, lh_min) if lh_min <= x < lh0]
    wide = wide_box if wide_box is not None and wide_box.width > box.width + 1 else box
    bottom = max(box.y1, expand_to_y or box.y1)
    tall = Box(wide.x0, box.y0, wide.x1, bottom)

    plan: list[tuple[float, float, Box, FitStatus]] = []
    for i, lh in enumerate(spacing_steps):
        plan.append((1.0, lh, box, "ok" if i == 0 else "spacing"))
    if wide is not box:
        plan.extend((1.0, lh, wide, "expanded") for lh in spacing_steps)
    if bottom > box.y1 + 0.5:
        plan.extend((1.0, lh, tall, "expanded") for lh in spacing_steps)
    scale = round(1.0 - step, 4)
    while scale >= hard_min_scale - 1e-9:
        plan.append((scale, lh_min, tall, "shrunk" if scale >= min_scale - 1e-9 else "overflow"))
        scale = round(scale - step, 4)

    chosen: tuple[float, float, Box, list[list[int]], FitStatus] | None = None
    for sc, lh, b, status in plan:
        lines = attempt(base_size * sc, lh, b)
        if lines is not None:
            chosen = (sc, lh, b, lines, status)
            break
    if chosen is None:  # không vừa kể cả ở cỡ nhỏ nhất: vẫn vẽ hết chữ, không cắt nội dung
        sc, lh = hard_min_scale, lh_min
        fallback: FitStatus = "overflow"
        wrapped = _wrap([w * base_size * sc for w in widths1], space1 * base_size * sc, tall.width)
        chosen = (sc, lh, tall, wrapped, fallback)

    sc, lh, b, chosen_lines, status = chosen
    size = base_size * sc
    space = space1 * size
    out_lines: list[list[tuple[float, int]]] = []
    baselines: list[float] = []
    baseline = b.y0 + ascender * size
    for li, idxs in enumerate(chosen_lines):
        ws = [widths1[i] * size for i in idxs]
        natural = sum(ws) + space * (len(ws) - 1)
        is_last = li == len(chosen_lines) - 1
        gap = space
        if align == "justify" and not is_last and len(ws) > 1:
            extra = (b.width - natural) / (len(ws) - 1)
            gap = space + extra if extra < space * 4 else space  # tránh khoảng trắng quá lớn
            x = b.x0
        elif align == "center":
            x = b.x0 + (b.width - natural) / 2
        elif align == "right":
            x = b.x1 - natural
        else:
            x = b.x0
        row: list[tuple[float, int]] = []
        for i, width in zip(idxs, ws, strict=True):
            row.append((x, i))
            x += width + gap
        out_lines.append(row)
        baselines.append(baseline)
        baseline += size * lh
    height = (len(chosen_lines) - 1) * size * lh + glyph_h * size
    return Placement(
        size=size,
        line_height=lh,
        box=b,
        lines=out_lines,
        baselines=baselines,
        status=status,
        scale=sc,
        height_used=height,
    )


def split_long_word(word: str, measure: Measure, max_w1: float) -> list[str]:
    """Từ dài hơn cả dòng (URL...): cắt theo ký tự. Tiếng Việt bình thường không gặp."""
    parts: list[str] = []
    cur = ""
    for ch in word:
        if cur and measure(cur + ch, 1.0) > max_w1:
            parts.append(cur)
            cur = ch
        else:
            cur += ch
    if cur:
        parts.append(cur)
    return parts


def layout_text(
    text: str,
    box: Box,
    *,
    measure: Measure,
    ascender: float,
    descender: float,
    base_size: float,
    line_height: float = 1.2,
    align: str = "left",
    expand_to_y: float | None = None,
    wide_box: Box | None = None,
    min_scale: float = 0.7,
    hard_min_scale: float = 0.4,
    step: float = 0.05,
    min_line_height: float = 1.12,
) -> Layout:
    """Dàn văn bản thuần (một font)."""
    raw = text.split()
    if not raw:
        return Layout(size=base_size, line_height=line_height)
    max_w1 = box.width / max(base_size * hard_min_scale, 0.5)
    words: list[str] = []
    for w in raw:
        words.extend(split_long_word(w, measure, max_w1) if measure(w, 1.0) > max_w1 else [w])
    p = fit_items(
        [measure(w, 1.0) for w in words],
        measure(" ", 1.0) or 0.25,
        box,
        ascender=ascender,
        descender=descender,
        base_size=base_size,
        line_height=line_height,
        align=align,
        expand_to_y=expand_to_y,
        wide_box=wide_box,
        min_scale=min_scale,
        hard_min_scale=hard_min_scale,
        step=step,
        min_line_height=min_line_height,
    )
    lines = [PlacedLine(bl, [(x, words[i]) for x, i in row]) for row, bl in zip(p.lines, p.baselines, strict=True)]
    return Layout(
        size=p.size, line_height=p.line_height, lines=lines, status=p.status, scale=p.scale, height_used=p.height_used
    )
