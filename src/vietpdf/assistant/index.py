"""Chỉ mục tìm kiếm BM25 trên các đoạn của IR — chạy hoàn toàn trên máy, không cần mô hình embedding.

Mỗi "chunk" là một đoạn (đoạn quá ngắn được gộp với đoạn kế tiếp cùng trang), có id ổn định để LLM trích dẫn
và để kiểm tra trích dẫn có thật."""

from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass

from vietpdf.core.ir import DocumentIR

_TOKEN_RE = re.compile(r"\w+", re.UNICODE)
_STOP = set(
    [
        "the",
        "a",
        "an",
        "of",
        "in",
        "on",
        "to",
        "and",
        "or",
        "for",
        "is",
        "are",
        "was",
        "were",
        "be",
        "by",
        "with",
        "as",
        "that",
        "this",
        "it",
        "its",
        "we",
        "our",
        "from",
        "at",
    ]
)


def tokenize(text: str) -> list[str]:
    t = unicodedata.normalize("NFC", text).casefold()
    return [w for w in _TOKEN_RE.findall(t) if w not in _STOP and len(w) > 1]


@dataclass
class Chunk:
    id: str
    page: int  # đánh số từ 0
    source: str
    translation: str
    segment_ids: list[str]
    role: str

    def label(self) -> str:
        return f"[{self.id}, trang {self.page + 1}]"


def build_chunks(ir: DocumentIR, min_chars: int = 120) -> list[Chunk]:
    chunks: list[Chunk] = []
    buf: list = []  # type: ignore[type-arg]
    for page in ir.pages:
        for s in sorted(page.segments, key=lambda x: x.order):
            if s.role in ("header", "footer", "abandon", "formula"):
                continue
            src = s.plain_source().strip()
            if not src:
                continue
            if buf and buf[-1].page != s.page:
                chunks.append(_make(len(chunks), buf))
                buf = []
            buf.append(s)
            if sum(len(x.plain_source()) for x in buf) >= min_chars or s.role in ("title", "caption"):
                chunks.append(_make(len(chunks), buf))
                buf = []
    if buf:
        chunks.append(_make(len(chunks), buf))
    return chunks


def _make(n: int, segs: list) -> Chunk:  # type: ignore[type-arg]
    return Chunk(
        id=f"c{n}",
        page=segs[0].page,
        source=" ".join(s.plain_source() for s in segs),
        translation=" ".join(s.plain_translation() for s in segs if s.translation),
        segment_ids=[s.id for s in segs],
        role=segs[0].role,
    )


class BM25Index:
    def __init__(self, chunks: list[Chunk], k1: float = 1.5, b: float = 0.75) -> None:
        self.chunks = chunks
        self.by_id = {c.id: c for c in chunks}
        self.k1, self.b = k1, b
        self.docs = [Counter(tokenize(c.source + " " + c.translation)) for c in chunks]
        self.lens = [sum(d.values()) for d in self.docs]
        self.avg = (sum(self.lens) / len(self.lens)) if self.lens else 1.0
        df: Counter[str] = Counter()
        for d in self.docs:
            df.update(d.keys())
        n = len(chunks)
        self.idf = {t: math.log(1 + (n - f + 0.5) / (f + 0.5)) for t, f in df.items()}

    def search(self, query: str, k: int = 6) -> list[Chunk]:
        q = tokenize(query)
        scores: list[tuple[float, int]] = []
        for i, d in enumerate(self.docs):
            s = 0.0
            for t in q:
                tf = d.get(t, 0)
                if tf:
                    s += (
                        self.idf.get(t, 0)
                        * tf
                        * (self.k1 + 1)
                        / (tf + self.k1 * (1 - self.b + self.b * self.lens[i] / self.avg))
                    )
            if s > 0:
                scores.append((s, i))
        scores.sort(reverse=True)
        return [self.chunks[i] for _, i in scores[:k]]


def quote_supported(quote: str, chunk: Chunk, threshold: float = 0.85) -> bool:
    """Trích dẫn có thật nằm trong đoạn được truy xuất (bản gốc hoặc bản dịch) không — kiểu PaperQA2."""
    from difflib import SequenceMatcher  # noqa: PLC0415

    q = " ".join(tokenize(quote))[:1200]
    if len(q) < 8:
        return False
    for text in (chunk.source, chunk.translation):
        t = " ".join(tokenize(text))[:20000]  # giới hạn chi phí so khớp trên đoạn rất dài
        if q in t:
            return True
        if not t:
            continue
        m = SequenceMatcher(None, q, t, autojunk=False).find_longest_match(0, len(q), 0, len(t))
        if m.size / len(q) >= threshold:
            return True
        # cửa sổ cùng độ dài quanh vị trí khớp dài nhất
        start = max(0, m.b - m.a)
        window = t[start : start + len(q) + 20]
        if SequenceMatcher(None, q, window, autojunk=False).ratio() >= threshold:
            return True
    return False
