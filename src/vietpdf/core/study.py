"""Bản học song ngữ (study edition): dàn lại tài liệu theo dòng chảy, mỗi đoạn gồm
  - câu gốc tiếng Anh (chữ xám, nhỏ)  → để đối chiếu và học diễn đạt gốc
  - bản dịch tiếng Việt                → đọc hiểu
  - dòng thuật ngữ Anh–Việt của đoạn   → học từ chuyên ngành ngay tại chỗ
Cuối file: bảng thuật ngữ có định nghĩa (ghi rõ nguồn: bảng thuật ngữ / giải thích của AI) và sổ từ của người dùng.

`build_study_data` chạy ở tiến trình chính (chỉ đọc IR đã kiểm tra); `render_study` ghi PDF trong tiến trình con.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from vietpdf.core.fonts import FontBook, LoadedFont
from vietpdf.core.ir import DocumentIR
from vietpdf.security.fsutil import replace_from_temp, temp_near
from vietpdf.translate.glossary import Glossary

SKIP_ROLES = {"header", "footer", "abandon"}


def build_study_data(ir: DocumentIR, glossary: Glossary, vocab: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    blocks: list[dict[str, Any]] = []
    for page in ir.pages:
        for s in sorted(page.segments, key=lambda x: x.order):
            if s.role in SKIP_ROLES or s.status != "translated" or not s.translation:
                continue
            terms = []
            for e in glossary.match(s.plain_source())[:12]:
                terms.append([e.en, e.en if e.policy == "keep" else e.vi])
            blocks.append(
                {
                    "page": s.page + 1,
                    "role": s.role,
                    "en": s.plain_source()[:6000],
                    "vi": s.plain_translation()[:8000],
                    "terms": terms,
                }
            )
    defs = {t.en.lower(): t for t in ir.terms}
    used: dict[str, list[str]] = {}
    for b in blocks:
        for en, vi in b["terms"]:
            if en.lower() not in used:
                t = defs.get(en.lower())
                note = next((e.note for e in glossary.entries if e.en.lower() == en.lower()), "")
                definition = (t.definition if t else "") or note
                label = "Giải thích của AI" if t is not None and t.source == "llm" else "Theo bảng thuật ngữ"
                used[en.lower()] = [en, vi, f"{definition} ({label})" if definition else ""]
    return {
        "title": ir.title or ir.source_name,
        "source_name": ir.source_name,
        "provider": ir.provider,
        "summary": ir.summary,
        "blocks": blocks,
        "glossary": list(used.values()),
        "vocab": [[v.get("term", ""), v.get("vi", ""), v.get("example", "")] for v in (vocab or [])][:2000],
    }


class _Flow:
    """Viết chữ theo dòng chảy, tự xuống dòng, tự sang trang; font dự phòng cho ký tự thiếu glyph."""

    W, H, M = 595.0, 842.0, 50.0

    def __init__(self, doc: Any, fonts: FontBook, header: str) -> None:
        self.doc, self.fonts, self.header = doc, fonts, header
        self.page: Any = None
        self.writers: dict[tuple[float, float, float], Any] = {}
        self.y = 0.0
        self.new_page()

    def flush(self) -> None:
        if self.page is not None:
            for tw in self.writers.values():
                tw.write_text(self.page)
        self.writers = {}

    def new_page(self) -> None:
        self.flush()
        self.page = self.doc.new_page(width=self.W, height=self.H)
        self.y = self.M
        self.text_line(self.header, self.fonts.get("sans", False, True), 7.5, (0.55, 0.55, 0.55))
        self.page.draw_line((self.M, self.y + 2), (self.W - self.M, self.y + 2), color=(0.85, 0.85, 0.85), width=0.4)
        self.y += 10
        self.text_line(
            str(self.doc.page_count),
            self.fonts.get("sans", False, False),
            7.5,
            (0.55, 0.55, 0.55),
            x=self.W / 2,
            y=self.H - 25,
        )

    def _writer(self, color: tuple[float, float, float]) -> Any:
        import pymupdf  # noqa: PLC0415

        if color not in self.writers:
            self.writers[color] = pymupdf.TextWriter(self.page.rect, color=color)
        return self.writers[color]

    def _runs(self, text: str, font: LoadedFont) -> list[tuple[str, LoadedFont]]:
        alt = self.fonts.get("serif" if font.key[0] == "sans" else "sans", font.key[1], font.key[2])
        out: list[tuple[str, LoadedFont]] = []
        for ch in text:
            f = font if ch.isspace() or font.font.has_glyph(ord(ch)) else alt
            if not f.font.has_glyph(ord(ch)) and not ch.isspace():
                ch = "?"  # noqa: PLW2901 - ký tự không font nào có
                f = font
            if out and out[-1][1] is f:
                out[-1] = (out[-1][0] + ch, f)
            else:
                out.append((ch, f))
        return out

    def _width(self, text: str, font: LoadedFont, size: float) -> float:
        return sum(f.text_length(t, size) for t, f in self._runs(text, font))

    def text_line(
        self,
        text: str,
        font: LoadedFont,
        size: float,
        color: tuple[float, float, float],
        x: float | None = None,
        y: float | None = None,
    ) -> None:
        tw = self._writer(color)
        cx = self.M if x is None else x
        cy = (self.y + size) if y is None else y
        for t, f in self._runs(text, font):
            tw.append((cx, cy), t, font=f.font, fontsize=size)
            cx += f.text_length(t, size)
        if y is None:
            self.y += size * 1.35

    def para(
        self,
        text: str,
        font: LoadedFont,
        size: float,
        color: tuple[float, float, float],
        indent: float = 0.0,
        after: float = 3.0,
    ) -> None:
        width = self.W - 2 * self.M - indent
        line = ""
        for word in text.split():
            cand = f"{line} {word}".strip()
            if line and self._width(cand, font, size) > width:
                self._emit(line, font, size, color, indent)
                line = word
            else:
                line = cand
        if line:
            self._emit(line, font, size, color, indent)
        self.y += after

    def _emit(self, line: str, font: LoadedFont, size: float, color: tuple[float, float, float], indent: float) -> None:
        if self.y + size * 1.4 > self.H - self.M:
            self.new_page()
        self.text_line(line, font, size, color, x=self.M + indent, y=self.y + size)
        self.y += size * 1.4

    def space(self, h: float) -> None:
        self.y += h
        if self.y > self.H - self.M - 20:
            self.new_page()


def render_study(out_path: str, data: dict[str, Any]) -> int:
    import pymupdf  # noqa: PLC0415

    fonts = FontBook()
    doc = pymupdf.open()
    header = f"VietPDF · Bản học song ngữ · {str(data.get('title', ''))[:80]}"
    if data.get("provider") == "fake":
        header = "BẢN DỊCH GIẢ ĐỂ KIỂM THỬ — KHÔNG CÓ NGHĨA · " + header
    flow = _Flow(doc, fonts, header)
    serif, serif_b = fonts.get("serif", False, False), fonts.get("serif", True, False)
    sans, sans_i, sans_b = (
        fonts.get("sans", False, False),
        fonts.get("sans", False, True),
        fonts.get("sans", True, False),
    )
    gray, blue, black = (0.42, 0.42, 0.42), (0.1, 0.3, 0.65), (0.0, 0.0, 0.0)

    flow.para(str(data.get("title", "")), serif_b, 16, black, after=4)
    flow.para(f"Nguồn: {data.get('source_name', '')}", sans, 8, gray, after=8)
    flow.para(
        "Cách đọc: dòng chữ xám là câu gốc (EN), dòng đen là bản dịch (VI), dòng xanh là thuật ngữ của đoạn. "
        "Bảng thuật ngữ và sổ từ ở cuối tài liệu.",
        sans_i,
        8,
        gray,
        after=8,
    )
    if data.get("summary"):
        flow.para("Tóm tắt (Giải thích của AI): " + str(data["summary"]), sans, 9, black, after=10)

    last_page = None
    for b in data.get("blocks", []):
        if b["page"] != last_page:
            flow.space(4)
            flow.para(f"— Trang {b['page']} —", sans_b, 8, gray, after=4)
            last_page = b["page"]
        is_title = b.get("role") == "title"
        flow.para("EN  " + b["en"], sans_i, 8.5, gray, after=1.5)
        flow.para(b["vi"], serif_b if is_title else serif, 12 if is_title else 10.5, black, after=1.5)
        if b.get("terms"):
            flow.para("Thuật ngữ: " + " · ".join(f"{en} = {vi}" for en, vi in b["terms"]), sans, 8, blue, after=2)
        flow.space(5)

    if data.get("glossary"):
        flow.new_page()
        flow.para("Bảng thuật ngữ (Anh – Việt)", serif_b, 14, black, after=6)
        for en, vi, definition in data["glossary"]:
            flow.para(f"{en} — {vi}", sans_b, 9.5, black, after=0.5)
            if definition:
                flow.para(definition, sans, 8.5, gray, indent=12, after=3)
            else:
                flow.space(2)
    if data.get("vocab"):
        flow.new_page()
        flow.para("Sổ từ của tôi", serif_b, 14, black, after=6)
        for term, vi, example in data["vocab"]:
            flow.para(f"{term} — {vi}", sans_b, 9.5, black, after=0.5)
            if example:
                flow.para(f"“{example}”", sans_i, 8.5, gray, indent=12, after=3)
    flow.flush()
    doc.subset_fonts()
    tmp = temp_near(Path(out_path), ".pdf")
    n = doc.page_count
    doc.save(str(tmp), garbage=3, deflate=True)
    doc.close()
    replace_from_temp(tmp, Path(out_path))
    return int(n)
