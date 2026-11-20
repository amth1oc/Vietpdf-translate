"""Mô hình trung gian (IR): tách thông tin bố cục khỏi nội dung chữ.

IR là ranh giới tin cậy: dữ liệu do tiến trình con (đã đọc PDF không tin cậy) trả về luôn được
kiểm tra lại bằng pydantic trước khi dùng (kiểu dữ liệu, giới hạn độ dài, toạ độ hữu hạn).

Cú pháp đánh dấu trong `Segment.source` / `Segment.translation` (xem docs/KIEN_TRUC.md):
- `⟦M3x1⟧`  công thức trong dòng (giữ nguyên glyph gốc, dời nguyên khối khi dàn chữ)
- `⟦B⟧…⟦/B⟧`, `⟦I⟧…⟦/I⟧`  đoạn chữ đậm / nghiêng khác kiểu chính của đoạn
- `⟦F1⟧`, `⟦T1⟧`  ký hiệu giữ chỗ tạm thời khi gửi dịch (URL, thuật ngữ khoá) — không lưu trong IR
"""

from __future__ import annotations

import math
import re
from typing import Annotated, Any, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

IR_VERSION = 2
MAX_TEXT = 20_000

MARKER_RE = re.compile(r"⟦(?:/?[BI]|[FMT][0-9x]{1,12})⟧")
FORMULA_RE = re.compile(r"⟦(M[0-9x]{1,12})⟧")


def _finite_bbox(v: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
    if not all(math.isfinite(x) and abs(x) < 1e6 for x in v):
        raise ValueError("bbox không hợp lệ")
    x0, y0, x1, y1 = v
    return (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))


BBox = Annotated[tuple[float, float, float, float], AfterValidator(_finite_bbox)]
_FLAG_RE = re.compile(r"^[a-z0-9][a-z0-9:_\-]{0,39}$")


def _clean_flags(v: list[str]) -> list[str]:
    """Cờ là mã nội bộ (vd. `qa:number`): bỏ mọi giá trị khác (IR do người khác gửi, tiến trình con bị chiếm) — chúng
    được hiển thị trong GUI (tooltip) và báo cáo (L1-021)."""
    return [f for f in v if isinstance(f, str) and _FLAG_RE.match(f)]


Flags = Annotated[list[str], AfterValidator(_clean_flags)]
Family = Literal["serif", "sans", "mono"]
Align = Literal["left", "justify", "center", "right"]
Status = Literal["pending", "translated", "skipped", "failed"]
Role = Literal[
    "body", "title", "caption", "header", "footer", "footnote", "formula", "in-table", "in-figure", "ocr", "abandon"
]
RegionKind = Literal["table", "figure", "formula", "caption", "abandon", "title", "text"]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class Style(_Model):
    family: Family = "serif"
    bold: bool = False
    italic: bool = False
    size: float = Field(10.0, gt=0, lt=500)
    color: int = Field(0, ge=0, le=0xFFFFFF)
    align: Align = "left"
    line_height: float = Field(1.2, ge=0.8, le=3.0)  # khoảng cách đường cơ sở / cỡ chữ
    source_font: str = Field("", max_length=200)


class Formula(_Model):
    """Công thức trong dòng: vẽ lại bằng chính vùng của trang gốc (glyph + nét vẽ), không dịch."""

    key: str = Field(pattern=r"^M[0-9x]{1,12}$")
    page: int = Field(ge=0)
    bbox: BBox
    baseline_offset: float = Field(ge=-1e4, le=1e4)  # đường cơ sở - y0 của khung
    text: str = Field("", max_length=1000)


class Region(_Model):
    kind: RegionKind
    bbox: BBox
    score: float = Field(1.0, ge=0, le=1)
    source: Literal["heuristic", "model"] = "heuristic"


class Segment(_Model):
    id: str = Field(pattern=r"^[a-z0-9\-]{1,40}$")
    page: int = Field(ge=0)
    bbox: BBox
    source: str = Field(max_length=MAX_TEXT)
    style: Style = Field(default_factory=Style)
    role: Role = "body"
    order: int = 0
    status: Status = "pending"
    translation: str | None = Field(None, max_length=MAX_TEXT * 4)
    flags: Flags = Field(default_factory=list, max_length=50)
    formulas: list[Formula] = Field(default_factory=list, max_length=500)
    continues: str | None = Field(None, pattern=r"^[a-z0-9\-]{1,40}$")  # đoạn nối tiếp (qua cột/trang)
    ocr_bg: int | None = Field(None, ge=0, le=0xFFFFFF)  # màu nền để che chữ trên trang scan
    confidence: float | None = Field(None, ge=0, le=1)

    def plain_source(self) -> str:
        """Văn bản gốc dễ đọc: bỏ thẻ kiểu chữ, thay công thức bằng chữ của nó."""
        ftext = {f.key: f.text for f in self.formulas}
        s = FORMULA_RE.sub(lambda m: ftext.get(m.group(1), ""), self.source)
        return MARKER_RE.sub("", s)

    def plain_translation(self) -> str:
        ftext = {f.key: f.text for f in self.formulas}
        s = FORMULA_RE.sub(lambda m: ftext.get(m.group(1), ""), self.translation or "")
        return MARKER_RE.sub("", s)


class HiddenSpan(_Model):
    """Chữ có trong PDF nhưng người đọc không nhìn thấy (2.1.2): không dịch theo mặc định, liệt kê cho người dùng."""

    bbox: BBox
    text: str = Field("", max_length=200)
    reason: str = Field("", max_length=120)


class PageIR(_Model):
    number: int = Field(ge=0)
    width: float = Field(gt=0, lt=1e5)
    height: float = Field(gt=0, lt=1e5)
    is_scan: bool = False
    ocr: bool = False
    segments: list[Segment] = Field(default_factory=list, max_length=5000)
    obstacles: list[BBox] = Field(default_factory=list, max_length=20000)
    regions: list[Region] = Field(default_factory=list, max_length=2000)
    hidden_chars: int = Field(0, ge=0)  # số ký tự không nhìn thấy trong bản gốc (không dịch theo mặc định)
    hidden_spans: list[HiddenSpan] = Field(default_factory=list, max_length=500)  # 2.1.2: liệt kê, không lặng lẽ bỏ


class TocEntry(_Model):
    level: int = Field(ge=1, le=50)
    title: str = Field(max_length=2000)
    page: int
    translation: str | None = Field(None, max_length=8000)


class DocTerm(_Model):
    """Thuật ngữ của tài liệu (tầng 2 của kho thuật ngữ)."""

    en: str = Field(max_length=200)
    vi: str = Field(max_length=300)
    policy: Literal["translate", "keep"] = "translate"
    count: int = Field(0, ge=0)
    definition: str = Field("", max_length=1000)
    source: Literal["glossary", "llm", "user"] = "llm"
    approved: bool = True


class DocumentIR(_Model):
    version: Literal[1, 2] = 2
    source_name: str = Field(max_length=500)
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    page_count: int = Field(ge=0)
    title: str = Field("", max_length=2000)
    source_lang: str = Field("en", pattern=r"^[a-z]{2,3}(-[A-Za-z]{2,4})?$")
    target_lang: str = Field("vi", pattern=r"^[a-z]{2,3}(-[A-Za-z]{2,4})?$")
    domains: list[str] = Field(default_factory=list, max_length=5)
    summary: str = Field("", max_length=4000)
    chapter_summaries: dict[str, str] = Field(default_factory=dict)
    terms: list[DocTerm] = Field(default_factory=list, max_length=5000)
    pages: list[PageIR] = Field(default_factory=list)
    toc: list[TocEntry] = Field(default_factory=list, max_length=20000)
    # nhà cung cấp đã dịch; "fake" = bộ dịch giả để kiểm thử => mọi đầu ra bị đóng dấu "BẢN DỊCH GIẢ"
    provider: str = Field("", max_length=40, pattern=r"^[a-z0-9_\-]*$")

    @model_validator(mode="before")
    @classmethod
    def _migrate(cls, data: Any) -> Any:
        if isinstance(data, dict) and data.get("version", 1) == 1:
            data = {**data, "version": 2}  # v1 -> v2: chỉ thêm trường, giá trị mặc định
        return data

    def segments(self) -> list[Segment]:
        return [s for p in self.pages for s in p.segments]

    def formula_index(self) -> dict[str, Formula]:
        return {f.key: f for s in self.segments() for f in s.formulas}


def json_schema() -> dict[str, object]:
    return DocumentIR.model_json_schema()
