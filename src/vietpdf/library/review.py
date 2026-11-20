"""Tạo thẻ ôn tập từ một mục từ vựng (dùng chung cho CLI và GUI).

Ba kiểu thẻ:
- ``en-vi``: thấy từ tiếng Anh (kèm câu ví dụ trong bài) → nhớ nghĩa tiếng Việt.
- ``vi-en``: thấy nghĩa tiếng Việt → nhớ lại từ tiếng Anh (khó hơn, luyện chủ động).
- ``cloze``: câu gốc trong bài bị che từ → điền lại từ (học từ trong ngữ cảnh thật).

Chỉ trả về văn bản thuần; giao diện tự hiển thị dạng plain text (không HTML).
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

MODES = ("en-vi", "vi-en", "cloze")
BLANK = "_____"


def _fold(s: str) -> str:
    s = unicodedata.normalize("NFKD", s.casefold().replace("đ", "d"))  # đ không tách dấu được
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^\w]+", " ", s).strip()


def cloze_sentence(term: str, sentence: str) -> str:
    """Che mọi lần xuất hiện của ``term`` (không phân biệt hoa thường, cả dạng số nhiều -s/-es)."""
    if not term or not sentence:
        return ""
    pat = re.compile(r"(?<![\w])" + re.escape(term) + r"(?:e?s)?(?![\w])", re.IGNORECASE)
    out, n = pat.subn(BLANK, sentence)
    return out if n else ""


def make_card(word: dict[str, Any], mode: str = "en-vi") -> dict[str, str]:
    term = str(word.get("term") or "")
    vi = str(word.get("vi") or "")
    definition = str(word.get("definition") or "")
    example = str(word.get("example") or "")
    example_vi = str(word.get("example_vi") or "")
    pos = str(word.get("pos") or "")
    head = f"{term} ({pos})" if pos else term
    meaning = vi or definition or "(chưa có nghĩa — hãy bổ sung)"
    if mode == "cloze":
        blanked = cloze_sentence(term, example)
        if blanked:
            hint = f"\nGợi ý: {vi}" if vi else ""
            return {
                "mode": "cloze",
                "front": f"Điền từ còn thiếu:\n{blanked}{hint}",
                "back": f"{head}\n{example}" + (f"\n{example_vi}" if example_vi else ""),
                "answer": term,
            }
        mode = "vi-en" if vi else "en-vi"  # không có câu ví dụ chứa từ → chuyển kiểu thẻ
    if mode == "vi-en" and vi:
        extra = f"\n({definition})" if definition and definition != vi else ""
        return {
            "mode": "vi-en",
            "front": f"Từ tiếng Anh nào nghĩa là:\n{vi}{extra}",
            "back": head + (f"\nVí dụ: {example}" if example else ""),
            "answer": term,
        }
    back = meaning
    if definition and definition != meaning:
        back += f"\n{definition}"
    if example_vi:
        back += f"\nCâu dịch: {example_vi}"
    return {
        "mode": "en-vi",
        "front": head + (f"\nVí dụ: {example}" if example else ""),
        "back": back,
        "answer": vi,
    }


def check_answer(typed: str, expected: str) -> bool:
    """So khớp đáp án gõ tay (bỏ dấu, hoa thường, dấu câu) — chỉ để gợi ý, người học tự chấm."""
    a, b = _fold(typed), _fold(expected)
    return bool(a) and (a == b or (len(b) > 3 and a in b.split()) or a.rstrip("s") == b.rstrip("s"))
