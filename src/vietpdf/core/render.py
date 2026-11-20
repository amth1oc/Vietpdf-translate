"""IR + bản dịch -> PDF. Phần ghi PDF CHỈ chạy trong tiến trình con (xem core/sandbox.py).

- Xoá glyph chữ ở các đoạn được dịch (redaction), giữ hình/ảnh/đường kẻ.
- Công thức trong dòng: xoá glyph + nét vẽ ở chỗ cũ, vẽ lại NGUYÊN KHỐI ở chỗ mới bằng `show_pdf_page`
  (lấy đúng vùng đó của trang gốc làm Form XObject) => không vỡ công thức, không cần font toán.
- Chữ Việt viết bằng TextWriter với font Noto nhúng subset; đậm/nghiêng theo thẻ ⟦B⟧/⟦I⟧.
- Trang scan: che chữ gốc bằng màu nền (xoá cả điểm ảnh bên dưới), rồi viết chữ Việt.
- Tuỳ chọn: ghi chú thuật ngữ (annotation), phụ lục chú giải, tô màu đọc lướt, bản song ngữ.
"""

from __future__ import annotations

import contextlib
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from vietpdf.core.fit import Box, Placement, fit_items, split_long_word
from vietpdf.core.fonts import FontBook, LoadedFont
from vietpdf.core.ir import DocumentIR, Formula, PageIR, Segment
from vietpdf.core.segment import free_space_below, widened_bbox
from vietpdf.security.fsutil import replace_from_temp, temp_near

_SPLIT_RE = re.compile(r"(⟦(?:/?[BI]|M[0-9x]{1,12})⟧)")


@dataclass
class Piece:
    kind: str  # "text" | "formula"
    text: str
    bold: bool = False
    italic: bool = False


def parse_rich(text: str, base_bold: bool, base_italic: bool) -> list[list[Piece]]:
    """Tách văn bản có thẻ thành danh sách "từ"; mỗi từ là các mảnh liền nhau (chữ / công thức)."""
    words: list[list[Piece]] = []
    cur: list[Piece] = []
    b = i = 0
    for tok in _SPLIT_RE.split(text):
        if not tok:
            continue
        if tok in ("⟦B⟧", "⟦/B⟧", "⟦I⟧", "⟦/I⟧"):
            delta = -1 if "/" in tok else 1
            if "B" in tok:
                b = max(0, b + delta)
            else:
                i = max(0, i + delta)
            continue
        if tok.startswith("⟦M"):
            cur.append(Piece("formula", tok[1:-1]))
            continue
        for part in re.split(r"(\s+)", tok):
            if not part:
                continue
            if part.isspace():
                if cur:
                    words.append(cur)
                    cur = []
            else:
                cur.append(Piece("text", part, base_bold or b > 0, base_italic or i > 0))
    if cur:
        words.append(cur)
    return words


@dataclass
class RichLayout:
    placement: Placement
    words: list[list[Piece]]

    @property
    def needs_review(self) -> bool:
        return self.placement.needs_review

    @property
    def scale(self) -> float:
        return self.placement.scale

    @property
    def status(self) -> str:
        return self.placement.status


class _Shaper:
    """Đo và chia chữ theo font, có font dự phòng cho ký tự thiếu glyph."""

    def __init__(self, fonts: FontBook, seg: Segment) -> None:
        self.fonts = fonts
        self.family = seg.style.family
        self._glyph: dict[tuple[str, str], bool] = {}

    def font(self, bold: bool, italic: bool) -> LoadedFont:
        return self.fonts.get(self.family, bold, italic)

    def runs(self, text: str, bold: bool, italic: bool) -> list[tuple[str, LoadedFont]]:
        primary = self.font(bold, italic)
        fallback = self.fonts.get("sans" if self.family != "sans" else "serif", bold, italic)
        out: list[tuple[str, LoadedFont]] = []
        for ch in text:
            key = (primary.path.name, ch)
            if key not in self._glyph:
                self._glyph[key] = bool(primary.font.has_glyph(ord(ch)))
            f = primary if self._glyph[key] or ch.isspace() else fallback
            if out and out[-1][1] is f:
                out[-1] = (out[-1][0] + ch, f)
            else:
                out.append((ch, f))
        return out

    def width1(self, word: list[Piece], formulas: dict[str, Formula], base_size: float) -> float:
        w = 0.0
        for p in word:
            if p.kind == "formula":
                f = formulas.get(p.text)
                w += (f.bbox[2] - f.bbox[0]) / base_size if f else 0.0
            else:
                w += sum(font.text_length(t, 1.0) for t, font in self.runs(p.text, p.bold, p.italic))
        return w


def segment_layout(
    seg: Segment, page: PageIR, fonts: FontBook, min_scale: float, formulas: dict[str, Formula] | None = None
) -> RichLayout:
    formulas = formulas if formulas is not None else {f.key: f for f in seg.formulas}
    shaper = _Shaper(fonts, seg)
    base = shaper.font(seg.style.bold, seg.style.italic)
    words = parse_rich(seg.translation or "", seg.style.bold, seg.style.italic)
    x0, y0, x1, y1 = seg.bbox
    hard_min = 0.4
    max_w1 = (x1 - x0) / max(seg.style.size * hard_min, 0.5)
    # Từ thuần chữ dài hơn cả dòng (URL): cắt theo ký tự
    expanded: list[list[Piece]] = []
    for w in words:
        if len(w) == 1 and w[0].kind == "text":
            p = w[0]
            f = shaper.font(p.bold, p.italic)
            if f.text_length(p.text, 1.0) > max_w1:
                expanded.extend(
                    [Piece("text", part, p.bold, p.italic)] for part in split_long_word(p.text, f.text_length, max_w1)
                )
                continue
        expanded.append(w)
    widths = [shaper.width1(w, formulas, seg.style.size) for w in expanded]
    wide = widened_bbox(page, seg)
    x_range = (wide[0], wide[2]) if wide else None
    placement = fit_items(
        widths,
        base.text_length(" ", 1.0) or 0.25,
        Box(x0, y0, x1, y1),
        ascender=base.ascender,
        descender=base.descender,
        base_size=seg.style.size,
        line_height=seg.style.line_height,
        align=seg.style.align,
        expand_to_y=free_space_below(page, seg, x_range=x_range),
        wide_box=Box(*wide) if wide else None,
        min_scale=min_scale,
        hard_min_scale=hard_min,
    )
    return RichLayout(placement, expanded)


def _rgb(color: int) -> tuple[float, float, float]:
    return ((color >> 16 & 255) / 255, (color >> 8 & 255) / 255, (color & 255) / 255)


# Kiểu hành động PDF (ISO 32000-2 §12.6.4). Mặc định CẤM; chỉ giữ GoTo (đích nội bộ), Named (lệnh điều hướng
# chuẩn) và URI có scheme an toàn. Áp cho MỌI nơi chứa hành động: /A, /AA, /OpenAction, chuỗi /Next — kể cả khi hành
# động nằm inline (không phải đối tượng riêng) trong Link, Widget, Screen, mục lục... (L1-006).
_ACTION_TYPES = {
    "/GoTo", "/GoToR", "/GoToE", "/GoToDp", "/Launch", "/Thread", "/URI", "/Sound", "/Movie", "/Hide", "/Named",
    "/SubmitForm", "/ResetForm", "/ImportData", "/SetOCGState", "/Rendition", "/Trans", "/GoTo3DView",
    "/JavaScript", "/RichMediaExecute", "/ECMAScript",
}  # fmt: skip
_SAFE_ACTIONS = {"/GoTo", "/Named", "/URI"}
# Dấu hiệu nội dung chủ động — sau khi lưu, PDF đầu ra không được còn bất kỳ dấu hiệu nào (kiểm tra fail-closed).
_ACTIVE_RE = re.compile(
    r"/(?:JavaScript|JS|Launch|SubmitForm|ImportData|GoToR|GoToE|Rendition|RichMediaExecute|Movie|Sound|XFA|"
    r"EmbeddedFile|AA|OpenAction\s*<<)(?![A-Za-z0-9])(?!\s*null\b)"
)


class ActiveContentError(Exception):
    """PDF đầu ra vẫn còn nội dung chủ động sau khi làm sạch (không ghi file)."""


SAFE_URI_SCHEMES = ("http://", "https://", "mailto:")


TEST_PROVIDER = "fake"
TEST_STAMP = "BẢN DỊCH GIẢ ĐỂ KIỂM THỬ (provider fake) — chữ sinh ngẫu nhiên, KHÔNG phải bản dịch, không có nghĩa"


def _stamp_test_output(doc: Any, fonts: FontBook) -> None:
    """Đóng dấu đỏ lên mọi trang khi bản dịch đến từ bộ dịch giả, để không ai nhầm là bản dịch thật."""
    import pymupdf  # noqa: PLC0415

    bold = fonts.get("sans", True, False)
    for page in doc:
        r = page.rect
        size = 8.0
        while size > 4 and bold.text_length(TEST_STAMP, size) > r.width - 20:
            size -= 0.5
        tw = pymupdf.TextWriter(r, color=(0.8, 0, 0))
        tw.append((10, size + 3), TEST_STAMP, font=bold.font, fontsize=size)
        tw.write_text(page)


def _uri_is_safe(uri: str) -> bool:
    u = uri.strip().lower()
    return u.startswith(SAFE_URI_SCHEMES) and not any(c in u for c in "\x00\r\n")


# Annotation được GIỮ trong PDF dịch (R1-004, 2.1.2). Mọi loại khác (Sound, Movie, Screen, RichMedia, 3D,
# FileAttachment, Widget...) bị XOÁ khi dựng — trước đây chúng làm bước kiểm cuối từ chối CẢ file đầu ra sau khi
# đã dịch.
ANNOT_ALLOW = {
    "/Link", "/Text", "/Highlight", "/Underline", "/StrikeOut", "/Squiggly", "/Square", "/Circle", "/Line", "/Ink",
    "/FreeText", "/Popup",
}  # fmt: skip
# khoá hành động được phép trong một hành động an toàn; khoá khác (vd. /JS "vô hại" kèm GoTo) bị xoá
_SAFE_ACTION_EXTRA_KEYS = ("JS", "AA", "F", "Win", "Mac", "Unix", "OP", "Rendition", "Sound", "Movie")


def _remove_unlisted(doc: Any, removed: Counter[str], strict: bool = False) -> None:
    """Xoá annotation ngoài ANNOT_ALLOW (đếm theo loại). Annotation được giữ (trừ Link) bị bỏ /A, /AA.
    `strict`: lần làm sạch thứ hai sau khi chốt kiểm cuối báo lỗi — chỉ còn Link."""
    allow = {"/Link"} if strict else ANNOT_ALLOW
    with contextlib.suppress(Exception):
        n_fields = sum(1 for page in doc for _ in page.widgets())
        if n_fields:
            try:
                doc.bake(annots=False, widgets=True)  # trường biểu mẫu -> nội dung tĩnh (giữ hình, bỏ hành vi)
                removed["trường biểu mẫu (đã chuyển thành nội dung tĩnh)"] += n_fields
            except Exception:  # noqa: S110 - không bake được: vòng dưới xoá các annotation Widget
                pass
    for page in doc:
        try:
            refs = [x for x, *_ in page.annot_xrefs()]
        except Exception:  # noqa: S112 - trang hỏng: không có annotation để giữ
            continue
        keep: list[int] = []
        for x in refs:
            try:
                sub = doc.xref_get_key(x, "Subtype")[1]
            except Exception:
                sub = "?"
            if sub in allow:
                keep.append(x)
                if sub != "/Link":
                    for k in ("A", "AA"):
                        if doc.xref_get_key(x, k)[0] != "null":
                            doc.xref_set_key(x, k, "null")
                            removed[f"hành động trong annotation {sub.lstrip('/')}"] += 1
            else:
                removed[f"annotation {sub.lstrip('/') or '?'}"] += 1
        if len(keep) != len(refs):
            doc.xref_set_key(page.xref, "Annots", "[" + " ".join(f"{x} 0 R" for x in keep) + "]" if keep else "null")
    if strict:
        cat = doc.pdf_catalog()
        for k in ("Names", "AcroForm", "OpenAction", "AA"):
            if doc.xref_get_key(cat, k)[0] != "null":
                doc.xref_set_key(cat, k, "null")
                removed[f"khoá /{k} của tài liệu"] += 1


def _scrub(doc: Any, removed: Counter[str] | None = None, strict: bool = False) -> None:
    """Xoá nội dung chủ động: annotation ngoài allowlist, JavaScript (kể cả XFA), mọi hành động không an toàn ở mọi vị
    trí, file đính kèm/nhúng, dữ liệu form đã điền. Giữ: link nội bộ (GoTo), Named, URI http(s)/mailto — và cắt
    chuỗi /Next, khoá thừa (/JS...) sau chúng. Đếm số đối tượng đã xoá theo loại vào `removed` (báo cáo)."""
    removed = removed if removed is not None else Counter()
    _remove_unlisted(doc, removed, strict)
    try:
        doc.scrub(
            attached_files=True,
            embedded_files=True,
            javascript=True,
            reset_responses=True,
            clean_pages=False,
            hidden_text=False,
            metadata=False,
            redactions=False,
            remove_links=False,
            reset_fields=False,
            thumbnails=False,
            xml_metadata=False,
        )
    except Exception:
        removed["lỗi khi làm sạch tự động (đã làm sạch thủ công)"] += 1

    def key(xref: int, k: str) -> tuple[str, str]:
        try:
            return tuple(doc.xref_get_key(xref, k))
        except Exception:
            return ("null", "null")

    def null(xref: int, k: str) -> None:
        with contextlib.suppress(Exception):
            doc.xref_set_key(xref, k, "null")
            removed[f"khoá /{k.rsplit('/', 1)[-1]}"] += 1

    def check_action(xref: int, prefix: str) -> None:
        """prefix = "" (đối tượng xref chính là hành động) hoặc "A/", "OpenAction/", "A/Next/"...
        Hành động là tham chiếu xref thì để vòng lặp chính xử lý đối tượng đó."""
        if prefix:
            kind, _ = key(xref, prefix.rstrip("/"))
            if kind != "dict":
                return
        s_type = key(xref, prefix + "S")[1]
        if s_type not in _ACTION_TYPES:
            if prefix:
                null(xref, prefix.rstrip("/"))  # có /A nhưng không phải hành động hợp lệ: bỏ
            return
        safe = s_type in _SAFE_ACTIONS
        if s_type == "/URI":
            safe = _uri_is_safe(key(xref, prefix + "URI")[1])
        if not safe:
            removed[f"hành động {s_type.lstrip('/')}"] += 1
            if prefix:
                null(xref, prefix.rstrip("/"))
            else:
                doc.update_object(xref, "<<>>")  # vô hiệu hoá hành động, giữ tham chiếu hợp lệ
            return
        if key(xref, prefix + "Next")[0] != "null":
            null(xref, prefix + "Next")  # không cho chuỗi hành động nối sau một link an toàn
        for extra in _SAFE_ACTION_EXTRA_KEYS:  # vd. GoTo kèm /JS "vô hại": bỏ khoá thừa, giữ link
            if key(xref, prefix + extra)[0] != "null":
                null(xref, prefix + extra)

    cat = doc.pdf_catalog()
    for k in ("AA", "Names/JavaScript", "Names/EmbeddedFiles", "AcroForm/XFA"):
        if key(cat, k)[0] != "null":
            null(cat, k)
    if key(cat, "OpenAction")[0] == "dict":
        # hành động tự chạy khi mở: kể cả GoTo an toàn cũng bỏ (chỉ ảnh hưởng trang mở đầu); giữ dạng mảng đích đến
        null(cat, "OpenAction")
    for x in range(1, doc.xref_length()):
        try:
            if not doc.xref_object(x, compressed=True).lstrip().startswith("<<"):
                continue
            keys = set(doc.xref_get_keys(x))
        except Exception:  # noqa: S112 - đối tượng hỏng/không phải dict: bỏ qua
            continue
        for k in (
            ("AA", "JS", "JavaScript", "EmbeddedFile", "RichMediaContent")
            if not strict
            else (
                "AA",
                "JS",
                "JavaScript",
                "EmbeddedFile",
                "RichMediaContent",
                "Sound",
                "Movie",
                "Rendition",
                "XFA",
                "EF",
            )
        ):
            if k in keys:
                null(x, k)
        if "S" in keys:
            check_action(x, "")
        if "A" in keys:
            check_action(x, "A/")
        if "OpenAction" in keys and x != cat:
            null(x, "OpenAction")
    for page in doc:
        for link in page.get_links():
            uri = str(link.get("uri") or "")
            kind = link.get("kind")
            unsafe_uri = kind == pymupdf_link_uri() and not _uri_is_safe(uri)
            if unsafe_uri or kind in pymupdf_link_dangerous():
                page.delete_link(link)
                removed["link không an toàn"] += 1


def pymupdf_link_uri() -> int:
    import pymupdf  # noqa: PLC0415

    return int(pymupdf.LINK_URI)


def pymupdf_link_dangerous() -> tuple[int, ...]:
    import pymupdf  # noqa: PLC0415

    return (int(pymupdf.LINK_LAUNCH), int(pymupdf.LINK_GOTOR))


def assert_no_active_content(path: str) -> None:
    """Mở lại PDF đã lưu, quét MỌI đối tượng; còn dấu hiệu nội dung chủ động hay URI nguy hiểm => ActiveContentError."""
    import pymupdf  # noqa: PLC0415

    d = pymupdf.open(path)
    try:
        for x in range(1, d.xref_length()):
            try:
                obj = d.xref_object(x, compressed=True)
            except Exception as e:
                # fail-closed: đối tượng trong file vừa ghi mà không đọc lại được thì không chứng minh được là sạch
                raise ActiveContentError(f"Không đọc lại được đối tượng {x} của PDF đầu ra") from e
            m = _ACTIVE_RE.search(obj)
            if m:
                raise ActiveContentError(f"Đối tượng {x} còn nội dung chủ động ({m.group(0)})")
            for uri in re.findall(r"/URI\s*\(([^)]*)\)", obj):
                if not _uri_is_safe(uri):
                    raise ActiveContentError(f"Đối tượng {x} còn URI không an toàn")
            if re.search(r"/URI\s*<", obj):  # URI dạng chuỗi hex: không kiểm được scheme => từ chối
                raise ActiveContentError(f"Đối tượng {x} có URI dạng hex")
    finally:
        d.close()


def _save_clean(doc: Any, out_path: Path) -> None:
    """Lưu vào file tạm, kiểm tra không còn nội dung chủ động, rồi mới đổi tên thành file đích (fail-closed)."""
    tmp = temp_near(out_path, ".pdf")
    try:
        doc.save(str(tmp), garbage=3, deflate=True)
        assert_no_active_content(str(tmp))
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise
    replace_from_temp(tmp, out_path)


def _write_page(
    page: Any,
    orig: Any,
    page_ir: PageIR,
    segs: list[Segment],
    fonts: FontBook,
    min_scale: float,
    formulas: dict[str, Formula],
    report: dict[str, Any],
) -> None:
    import pymupdf  # noqa: PLC0415

    # 1) Công thức trong dòng của các đoạn được dịch: xoá cả glyph lẫn nét vẽ (gạch phân số, căn...)
    used = {f.key for s in segs for f in s.formulas}
    if used:
        for fkey in used:
            page.add_redact_annot(pymupdf.Rect(formulas[fkey].bbox), fill=False, cross_out=False)
        page.apply_redactions(
            images=pymupdf.PDF_REDACT_IMAGE_NONE,
            graphics=pymupdf.PDF_REDACT_LINE_ART_REMOVE_IF_COVERED,
            text=pymupdf.PDF_REDACT_TEXT_REMOVE,
        )
    # 2) Chữ gốc: chỉ xoá glyph (trang số) / che điểm ảnh bằng màu nền (trang scan)
    for seg in segs:
        r = pymupdf.Rect(seg.bbox)
        if seg.ocr_bg is not None:
            grown = pymupdf.Rect(r.x0 - 1, r.y0 - 1, r.x1 + 1, r.y1 + 1)
            page.add_redact_annot(grown, fill=_rgb(seg.ocr_bg), cross_out=False)
        else:
            page.add_redact_annot(r, fill=False, cross_out=False)
    page.apply_redactions(
        images=pymupdf.PDF_REDACT_IMAGE_PIXELS if page_ir.ocr else pymupdf.PDF_REDACT_IMAGE_NONE,
        graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
        text=pymupdf.PDF_REDACT_TEXT_REMOVE,
    )
    # 3) Viết chữ Việt + đặt lại công thức
    for seg in segs:
        lay = segment_layout(seg, page_ir, fonts, min_scale, formulas)
        shaper = _Shaper(fonts, seg)
        p = lay.placement
        tw = pymupdf.TextWriter(page.rect, color=_rgb(seg.style.color))
        for row, baseline in zip(p.lines, p.baselines, strict=True):
            for j, (x, wi) in enumerate(row):
                word = lay.words[wi]
                cx = x
                last_word = j == len(row) - 1
                for pi, piece in enumerate(word):
                    if piece.kind == "formula":
                        f = formulas.get(piece.text)
                        if f is None:
                            continue
                        sc = p.scale
                        fw, fh = (f.bbox[2] - f.bbox[0]) * sc, (f.bbox[3] - f.bbox[1]) * sc
                        top = baseline - f.baseline_offset * sc
                        page.show_pdf_page(
                            pymupdf.Rect(cx, top, cx + fw, top + fh), orig, f.page, clip=pymupdf.Rect(f.bbox)
                        )
                        cx += fw
                        continue
                    runs = shaper.runs(piece.text, piece.bold, piece.italic)
                    for ri, (t, font) in enumerate(runs):
                        trailing = not last_word and pi == len(word) - 1 and ri == len(runs) - 1
                        # dấu cách cuối từ: chữ trích/tìm kiếm/copy từ PDF đầu ra không bị dính từ
                        tw.append((cx, baseline), t + (" " if trailing else ""), font=font.font, fontsize=p.size)
                        cx += font.text_length(t, p.size)
        tw.write_text(page)
        report[seg.id] = {"scale": round(p.scale, 3), "status": p.status}


def _add_annotations(doc: Any, annotations: list[dict[str, Any]]) -> None:
    import pymupdf  # noqa: PLC0415

    for a in annotations[:5000]:
        page = doc[int(a["page"])]
        x, y = float(a["x"]), float(a["y"])
        annot = page.add_text_annot(pymupdf.Point(x, y), str(a["content"])[:4000], icon="Comment")
        annot.set_info(title=str(a.get("title", "VietPDF"))[:200], subject="Giải thích của AI")
        annot.set_colors(stroke=(0.95, 0.75, 0.1))
        annot.update()


def _add_highlights(doc: Any, highlights: list[dict[str, Any]]) -> None:
    import pymupdf  # noqa: PLC0415

    colors = {"goal": (1.0, 0.85, 0.3), "method": (0.55, 0.8, 1.0), "result": (0.6, 0.95, 0.6)}
    for h in highlights[:5000]:
        page = doc[int(h["page"])]
        annot = page.add_highlight_annot(pymupdf.Rect(h["bbox"]))
        if annot is None:
            continue
        annot.set_colors(stroke=colors.get(h.get("kind", ""), (1, 1, 0)))
        annot.set_info(content=str(h.get("label", ""))[:500], title="VietPDF")
        annot.update(opacity=0.35)


def _add_appendix(doc: Any, rows: list[list[str]], fonts: FontBook, title: str) -> None:
    """Thêm các trang 'Phụ lục chú giải' ở cuối: thuật ngữ gốc | bản dịch | giải thích."""
    import pymupdf  # noqa: PLC0415

    if not rows:
        return
    w, h = (doc[-1].rect.width, doc[-1].rect.height) if doc.page_count else (595, 842)
    regular, bold = fonts.get("sans", False, False), fonts.get("sans", True, False)
    margin, size = 50.0, 9.0
    cols = [margin, margin + (w - 2 * margin) * 0.28, margin + (w - 2 * margin) * 0.55]
    widths = [cols[1] - cols[0] - 8, cols[2] - cols[1] - 8, w - margin - cols[2]]

    def wrap(text: str, font: LoadedFont, width: float) -> list[str]:
        lines, cur = [], ""
        for word in text.split():
            cand = f"{cur} {word}".strip()
            if cur and font.text_length(cand, size) > width:
                lines.append(cur)
                cur = word
            else:
                cur = cand
        return [*lines, cur] if cur else lines or [""]

    page = None
    y = 0.0
    tw = None
    for idx, row in enumerate([["Thuật ngữ gốc", "Bản dịch", "Giải thích (của AI, nếu có)"], *rows]):
        is_head = idx == 0
        font = bold if is_head else regular
        cells = [wrap(str(c), font, widths[i]) for i, c in enumerate(row[:3])]
        need = max(len(c) for c in cells) * size * 1.35 + 6
        if page is None or y + need > h - margin:
            if tw is not None and page is not None:
                tw.write_text(page)
            page = doc.new_page(width=w, height=h)
            tw = pymupdf.TextWriter(page.rect)
            tw.append((margin, margin), title, font=bold.font, fontsize=14)
            y = margin + 24
        if tw is None:  # không xảy ra: trang đầu luôn được tạo ở vòng lặp đầu
            continue
        for ci, lines in enumerate(cells):
            for li, text in enumerate(lines):
                tw.append((cols[ci], y + size + li * size * 1.35), text, font=font.font, fontsize=size)
        y += need
        page.draw_line((margin, y - 3), (w - margin, y - 3), color=(0.8, 0.8, 0.8), width=0.4)
    if tw is not None and page is not None:
        tw.write_text(page)


def render_document(
    src_path: str, out_path: str, ir_json: str, options: dict[str, Any] | None = None
) -> dict[str, Any]:
    """options: dual_path, min_scale, password, annotations, highlights, appendix, appendix_title."""
    import pymupdf  # noqa: PLC0415

    opts = options or {}
    password = opts.get("password")
    min_scale = float(opts.get("min_scale", 0.7))
    ir = DocumentIR.model_validate_json(ir_json)
    fonts = FontBook()
    formulas = ir.formula_index()
    report: dict[str, Any] = {}
    removed: Counter[str] = Counter()
    doc = pymupdf.open(src_path, filetype="pdf")
    orig = pymupdf.open(src_path, filetype="pdf")  # bản gốc nguyên vẹn để lấy vùng công thức
    try:
        for d in (doc, orig):
            if d.needs_pass and not (password and d.authenticate(password)):
                raise ValueError("PDF được mã hoá")
        for page_ir in ir.pages:
            segs = [s for s in page_ir.segments if s.status == "translated" and s.translation]
            if segs:
                _write_page(doc[page_ir.number], orig, page_ir, segs, fonts, min_scale, formulas, report)
        if ir.toc and any(t.translation for t in ir.toc):
            doc.set_toc([[t.level, t.translation or t.title, t.page] for t in ir.toc])
        _scrub(doc, removed)
        _add_annotations(doc, opts.get("annotations") or [])
        _add_highlights(doc, opts.get("highlights") or [])
        _add_appendix(doc, opts.get("appendix") or [], fonts, str(opts.get("appendix_title", "Phụ lục: Chú giải")))
        if ir.provider == TEST_PROVIDER:
            _stamp_test_output(doc, fonts)
        doc.subset_fonts()
        _save_clean_retry(doc, Path(out_path), removed)  # ghi nguyên tử + kiểm tra; còn sót => làm sạch lại 1 lần
    finally:
        doc.close()
        orig.close()

    dual_path = opts.get("dual_path")
    if dual_path:
        selected = [p.number for p in ir.pages]
        src = pymupdf.open(src_path, filetype="pdf")
        out = pymupdf.open(out_path)
        dual = pymupdf.open()
        try:
            if src.needs_pass and password:
                src.authenticate(password)
            for n in selected:
                dual.insert_pdf(src, from_page=n, to_page=n)
                dual.insert_pdf(out, from_page=n, to_page=n)
            if out.page_count > src.page_count:  # phụ lục
                dual.insert_pdf(out, from_page=src.page_count, to_page=out.page_count - 1)
            _scrub(dual)
            _save_clean_retry(dual, Path(dual_path), Counter())
        finally:
            dual.close()
            out.close()
            src.close()
    report["removed"] = dict(removed)
    return report


def _save_clean_retry(doc: Any, out_path: Path, removed: Counter[str]) -> None:
    try:
        _save_clean(doc, out_path)
    except ActiveContentError:
        _scrub(doc, removed, strict=True)
        removed["làm sạch lần 2 (chế độ nghiêm)"] += 1
        _save_clean(doc, out_path)


def rescrub_pdf(in_path: str, out_path: str) -> dict[str, int]:
    """Chốt kiểm cuối (tiến trình con chỉ đọc) báo PDF đầu ra còn nội dung chủ động: làm sạch lại MỘT lần ở chế độ
    nghiêm (chỉ giữ Link an toàn) — chạy trong tiến trình con; không dịch lại, không gọi API (R1-004)."""
    import pymupdf  # noqa: PLC0415

    removed: Counter[str] = Counter()
    doc = pymupdf.open(in_path, filetype="pdf")
    try:
        _scrub(doc, removed, strict=True)
        _save_clean(doc, Path(out_path))
    finally:
        doc.close()
    return dict(removed)


def render_page_png(src_path: str, page_no: int, zoom: float, password: str | None = None) -> str:
    """Render một trang thành PNG (base64) cho trình xem trong GUI — chạy trong tiến trình con."""
    import base64  # noqa: PLC0415

    import pymupdf  # noqa: PLC0415

    zoom = max(0.2, min(zoom, 4.0))
    doc = pymupdf.open(src_path, filetype="pdf")
    try:
        if doc.needs_pass and not (password and doc.authenticate(password)):
            raise ValueError("PDF được mã hoá")
        pix = doc[page_no].get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
        return base64.b64encode(pix.tobytes("png")).decode("ascii")
    finally:
        doc.close()


def text_in_rect(src_path: str, page_no: int, rect: list[float], password: str | None = None) -> str:
    """Chữ nằm trong vùng người dùng bôi đen trên trang (toạ độ điểm PDF) — chạy trong tiến trình con.

    Lấy theo từng từ (tâm từ nằm trong vùng) và giữ thứ tự đọc; trả về văn bản thuần, tối đa 2000 ký tự."""
    import pymupdf  # noqa: PLC0415

    x0, y0, x1, y1 = (float(v) for v in rect[:4])
    doc = pymupdf.open(src_path, filetype="pdf")
    try:
        if doc.needs_pass and not (password and doc.authenticate(password)):
            raise ValueError("PDF được mã hoá")
        page = doc[page_no]
        out: list[str] = []
        last: tuple[int, int] | None = None
        for w in page.get_text("words"):  # thứ tự gốc theo khối → đúng cột
            cx, cy = (w[0] + w[2]) / 2, (w[1] + w[3]) / 2
            if x0 <= cx <= x1 and y0 <= cy <= y1:
                key = (int(w[5]), int(w[6]))  # (khối, dòng)
                if last is not None and key != last and out and out[-1].endswith("-"):
                    out[-1] = out[-1][:-1] + str(w[4])  # nối từ bị ngắt dòng "atten-\ntion"
                else:
                    out.append(str(w[4]))
                last = key
        return " ".join(out)[:2000]
    finally:
        doc.close()


def highlight_original(src_path: str, out_path: str, items: list[dict[str, Any]], password: str | None = None) -> int:
    """Bản gốc có tô màu đọc lướt (Mục tiêu / Phương pháp / Kết quả) — chạy trong tiến trình con."""
    import pymupdf  # noqa: PLC0415

    colors = {"goal": (1.0, 0.85, 0.3), "method": (0.55, 0.8, 1.0), "result": (0.6, 0.95, 0.6)}
    names = {"goal": "Mục tiêu", "method": "Phương pháp", "result": "Kết quả"}
    doc = pymupdf.open(src_path, filetype="pdf")
    n = 0
    try:
        if doc.needs_pass and not (password and doc.authenticate(password)):
            raise ValueError("PDF được mã hoá")
        for it in items[:200]:
            page = doc[int(it["page"])]
            words = str(it["sentence"]).split()
            # tìm theo cụm đầu và cụm cuối câu (câu có thể xuống dòng)
            quads = page.search_for(" ".join(words[:8]), quads=True)[:3]
            if len(words) > 8:
                quads += page.search_for(" ".join(words[-6:]), quads=True)[:3]
            if not quads:
                continue
            annot = page.add_highlight_annot(quads)
            annot.set_colors(stroke=colors.get(it.get("kind", ""), (1, 1, 0)))
            annot.set_info(content=names.get(it.get("kind", ""), ""), title="VietPDF")
            annot.update()
            n += 1
        _scrub(doc)
        _save_clean(doc, Path(out_path))
    finally:
        doc.close()
    return n
