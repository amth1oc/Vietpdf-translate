"""Bảo vệ nội dung không được dịch: URL, email, DOI, arXiv ID, trích dẫn, code, ký hiệu toán.

Các đoạn đó được thay bằng ký hiệu giữ chỗ `⟦F1⟧`, `⟦F2⟧`… trước khi gửi đi dịch và đặt lại sau.
Ký hiệu công thức `⟦M…⟧` và thẻ kiểu chữ `⟦B⟧…⟦/B⟧` có sẵn trong văn bản được giữ nguyên.
Thuật ngữ khoá (chế độ nghiêm ngặt) được thay bằng `⟦T1⟧`… và điền bản dịch chuẩn khi đặt lại.
"""

from __future__ import annotations

import bisect
import re
from collections import Counter
from dataclasses import dataclass, field

# Ký hiệu bắt buộc phải còn nguyên (đúng số lượng) trong bản dịch
STRICT_TOKEN_RE = re.compile(r"⟦[FMT][0-9x]{1,12}⟧")
TAG_RE = re.compile(r"⟦/?[BI]⟧")
MARKER_RE = re.compile(r"⟦(?:/?[BI]|[FMT][0-9x]{1,12})⟧")
TOKEN_RE = re.compile(r"⟦F(\d{1,4})⟧")  # tương thích ngược

_MATH_CHARS = (
    "Ͱ-Ͽ"  # Hy Lạp
    "∀-⋿"  # toán tử
    "⨀-⫿"
    "⟀-⟯⦀-⧿"
    "\U0001d400-\U0001d7ff"
    "⁰-₟"  # chỉ số trên/dưới
    "±×÷√′″"
)

_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"`[^`\n]{1,200}`"),
    re.compile(r"\b(?:https?://|www\.)[^\s<>\"'\]\)⟦]+[^\s<>\"'\]\).,;:!?⟦]"),
    re.compile(r"\b[\w.+\-]{1,64}@[\w\-]{1,63}(?:\.[\w\-]{1,63})+\b"),
    re.compile(r"\b10\.\d{4,9}/[^\s\"<>⟦]+[^\s\"<>.,;:⟦]"),
    re.compile(r"\barXiv:\s?\d{4}\.\d{4,5}(?:v\d+)?\b", re.IGNORECASE),
    re.compile(r"\[\d{1,4}(?:\s*[,–\-]\s*\d{1,4}){0,20}\]"),
    re.compile(rf"[{_MATH_CHARS}](?:[\w{_MATH_CHARS}()^_=+\-*/<>|]*[{_MATH_CHARS}\w)])?"),
]


class PlaceholderError(Exception):
    pass


@dataclass
class Protected:
    text: str
    mapping: dict[str, str] = field(default_factory=dict)

    def token_counts(self) -> Counter[str]:
        return Counter(STRICT_TOKEN_RE.findall(self.text))


# Ký hiệu có sẵn trong văn bản do chính app chèn khi trích xuất (thẻ kiểu chữ, công thức) — giữ nguyên.
# ⟦F…⟧/⟦T…⟧ KHÔNG bao giờ có sẵn hợp lệ trong văn bản gốc: nếu xuất hiện (chữ trong PDF) thì được bảo vệ như nội dung
# thường, tránh va chạm với ký hiệu giữ chỗ mới (L1-019).
_KEEP_RE = re.compile(r"⟦(?:/?[BI]|M[0-9x]{1,12})⟧")


class _Spans:
    """Tập khoảng [s, e) không chồng nhau, kiểm tra chồng lấn O(log n) (thay cho quét tuyến tính — L1-019)."""

    def __init__(self) -> None:
        self.starts: list[int] = []
        self.ends: list[int] = []

    def free(self, s: int, e: int) -> bool:
        i = bisect.bisect_right(self.starts, s)
        if i > 0 and self.ends[i - 1] > s:
            return False
        return not (i < len(self.starts) and self.starts[i] < e)

    def add(self, s: int, e: int) -> None:
        i = bisect.bisect_right(self.starts, s)
        self.starts.insert(i, s)
        self.ends.insert(i, e)


def protect(text: str, term_tokens: dict[str, str] | None = None) -> Protected:
    """`term_tokens`: thuật ngữ (đã khớp, dạng nguyên văn trong text) -> bản dịch chuẩn (chế độ nghiêm ngặt)."""
    occupied = _Spans()
    for m in _KEEP_RE.finditer(text):  # ký hiệu có sẵn: không đụng tới
        occupied.add(*m.span())
    spans: list[tuple[int, int, str]] = []

    def take(s: int, e: int, kind: str) -> None:
        if e > s and occupied.free(s, e):
            occupied.add(s, e)
            spans.append((s, e, kind))

    # ký hiệu ⟦F…⟧/⟦T…⟧ nguyên văn trong văn bản gốc, rồi ⟦ ⟧ lẻ
    for m in re.finditer(r"⟦[FT][0-9x]{1,12}⟧", text):
        take(*m.span(), "F")
    for m in re.finditer(r"[⟦⟧]", text):
        take(*m.span(), "F")
    for pat in _PATTERNS:
        for m in pat.finditer(text):
            take(*m.span(), "F")
    mapping: dict[str, str] = {}
    if term_tokens:
        for term in sorted(term_tokens, key=len, reverse=True):
            for m in re.finditer(rf"(?<![\w-]){re.escape(term)}(?![\w-])", text):
                take(*m.span(), "T")
    if not spans:
        return Protected(text)
    spans.sort()
    out: list[str] = []
    pos = 0
    counters = {"F": 0, "T": 0}
    for s, e, kind in spans:
        out.append(text[pos:s])
        counters[kind] += 1
        token = f"⟦{kind}{counters[kind]}⟧"
        original = text[s:e]
        mapping[token] = term_tokens.get(original, original) if kind == "T" and term_tokens else original
        out.append(token)
        pos = e
    out.append(text[pos:])
    return Protected("".join(out), mapping)


def placeholders_intact(protected: Protected, translated: str) -> bool:
    return Counter(STRICT_TOKEN_RE.findall(translated)) == protected.token_counts()


def tags_balanced(text: str) -> bool:
    depth = {"B": 0, "I": 0}
    for t in TAG_RE.findall(text):
        k = t[-2]
        depth[k] += -1 if "/" in t else 1
        if depth[k] < 0:
            return False
    return depth == {"B": 0, "I": 0}


def strip_tags(text: str) -> str:
    return TAG_RE.sub("", text)


def restore(translated: str, protected: Protected) -> str:
    if not placeholders_intact(protected, translated):
        raise PlaceholderError("Bản dịch làm mất hoặc thêm ký hiệu giữ chỗ")
    if not tags_balanced(translated):
        translated = strip_tags(translated)  # mất thẻ kiểu chữ: bỏ kiểu thay vì hỏng cả đoạn
    return re.sub(r"⟦[FT]\d{1,4}⟧", lambda m: protected.mapping.get(m.group(0), m.group(0)), translated)
