"""Tra nghĩa một từ/cụm người dùng bôi đen, để thêm vào sổ từ vựng.

Thứ tự (rẻ và đáng tin trước): bảng thuật ngữ (3 tầng) → thuật ngữ của tài liệu → câu định nghĩa trong bài →
(tuỳ chọn, khi người dùng bấm) hỏi AI. Mỗi kết quả ghi rõ NGUỒN. Câu ví dụ và bản dịch đoạn lấy từ chính tài liệu.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

from vietpdf.core.ir import DocumentIR, Segment
from vietpdf.translate.base import Translator, TranslatorError
from vietpdf.translate.glossary import Glossary
from vietpdf.translate.prompts import json_messages, safe_term
from vietpdf.translate.qa import sanitize_output

WORD_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "vi": {"type": "string"},
        "pos": {"type": "string"},
        "definition_vi": {"type": "string"},
        "example_vi": {"type": "string"},
        "note": {"type": "string"},
    },
    "required": ["vi", "pos", "definition_vi", "example_vi", "note"],
    "additionalProperties": False,
}


def clean_selection(text: str, limit: int = 120) -> str:
    """Chuẩn hoá đoạn bôi đen: bỏ ký hiệu nội bộ, ký tự điều khiển, dấu câu thừa ở hai đầu; giới hạn độ dài."""
    t = unicodedata.normalize("NFC", safe_term(re.sub(r"⟦[^⟧]{0,14}⟧", " ", text), 400))
    t = " ".join(t.split()).strip(" .,;:!?\"'()[]{}“”‘’-–—")
    return t[:limit]


_SENT_SPLIT = re.compile(r"(?<=[.!?。])\s+")


def _sentences(text: str) -> list[str]:
    return [x for x in _SENT_SPLIT.split(text.strip()) if x]


def _word_re(term: str) -> re.Pattern[str]:
    return re.compile(r"(?<!\w)" + re.escape(term) + r"(?!\w)", re.IGNORECASE)


def _aligned(src: str, tr: str, idx: int) -> str:
    """Câu dịch tương ứng câu gốc thứ ``idx`` khi số câu khớp; nếu không thì cả đoạn (cắt gọn)."""
    s_src, s_tr = _sentences(src), _sentences(tr)
    if s_tr and len(s_src) == len(s_tr):
        return s_tr[idx][:600]
    return tr[:400] + ("…" if len(tr) > 400 else "")


def find_context(ir: DocumentIR | None, term: str, page: int | None = None) -> tuple[str, str, int | None]:
    """(câu gốc chứa từ, câu/đoạn dịch tương ứng, trang 1-based). Ưu tiên trang người dùng đang xem.

    So khớp theo ranh giới từ ("art" không khớp "start")."""
    if ir is None or not term:
        return "", "", page
    pat = _word_re(term)
    segs: list[Segment] = ir.segments()
    if page is not None:
        segs = sorted(segs, key=lambda s: (s.page != page - 1, s.page, s.order))
    for s in segs:
        src = s.plain_source()
        if pat.search(src):
            sents = _sentences(src) or [src]
            idx = next((k for k, x in enumerate(sents) if pat.search(x)), 0)
            tr = s.plain_translation() if s.translation else ""
            return sents[idx][:600], _aligned(src, tr, idx) if tr else "", s.page + 1
        if s.translation and pat.search(s.plain_translation()):  # bôi đen trên bản dịch
            return src[:600], s.plain_translation()[:600], s.page + 1
    return "", "", page


def _variants(term: str) -> set[str]:
    """Dạng gốc đơn giản để khớp số nhiều: "mechanisms" → "mechanism", "approaches" → "approach"."""
    k = term.casefold()
    out = {k}
    if len(k) > 3 and k.endswith("s") and not k.endswith("ss"):
        out.add(k[:-1])
        if k.endswith("es"):
            out.add(k[:-2])
        if k.endswith("ies"):
            out.add(k[:-3] + "y")
    return out


def local_meaning(term: str, ir: DocumentIR | None, glossary: Glossary | None) -> dict[str, str]:
    """Nghĩa có sẵn, ưu tiên: bảng thuật ngữ (người dùng > tài liệu > chuyên ngành) → thuật ngữ của bài
    → định nghĩa tác giả viết trong bài. Không gọi mạng."""
    keys = _variants(term)
    if glossary is not None:
        for e in glossary.entries:
            if e.en.casefold() in keys or e.vi.casefold() == term.casefold():
                return {
                    "term": e.en,
                    "vi": e.en if e.policy == "keep" else e.vi,
                    "definition": e.note,
                    "source": "Bảng thuật ngữ",
                }
    if ir is not None:
        for t in ir.terms:
            if t.en.casefold() in keys or t.vi.casefold() == term.casefold():
                src = "Thuật ngữ của tài liệu (AI đề xuất)" if t.source == "llm" else "Thuật ngữ của tài liệu"
                return {"term": t.en, "vi": t.vi, "definition": t.definition, "source": src}
        from vietpdf.assistant.definitions import find_definitions, lookup  # noqa: PLC0415

        found = lookup(find_definitions(ir), term)
        if found:
            d = found[0]
            return {
                "term": term,
                "vi": "",
                "definition": f"{d.definition} (định nghĩa trong bài, tr. {d.page})",
                "source": "Định nghĩa trong bài",
            }
    return {"term": term, "vi": "", "definition": "", "source": ""}


async def ai_meaning(translator: Translator, term: str, sentence: str) -> dict[str, str]:
    """Hỏi AI nghĩa của từ trong ngữ cảnh câu (chỉ khi người dùng bấm). Kết quả gắn nhãn 'Giải thích của AI'."""
    msgs = json_messages(
        "Giải nghĩa từ/cụm tiếng Anh trong ngữ cảnh câu cho người Việt học tiếng Anh chuyên ngành.",
        '"vi": nghĩa tiếng Việt ngắn gọn đúng ngữ cảnh; "pos": từ loại (danh từ, động từ...); "definition_vi": giải '
        'thích 1–2 câu; "example_vi": dịch câu ngữ cảnh; "note": lưu ý cách dùng/cụm từ đi kèm (có thể rỗng).',
        {"term": clean_selection(term), "sentence": safe_term(sentence, 600)},
    )
    try:
        data = await translator.complete_json(msgs, "word", WORD_SCHEMA)
    except TranslatorError:
        return {}
    if not isinstance(data, dict):
        return {}
    out = {
        k: (sanitize_output(str(data.get(k, "")), 300) or "")[:600]
        for k in ("vi", "pos", "definition_vi", "example_vi", "note")
    }
    out["source"] = "Giải thích của AI"
    return out
