"""Định dạng kết quả (dict/list) thành văn bản thuần dễ đọc — dùng chung cho CLI và GUI (không HTML)."""

from __future__ import annotations

from typing import Any

_NAMES = {
    "label": "Nhãn",
    "paper_type": "Loại bài",
    "problem": "Vấn đề",
    "contributions": "Đóng góp",
    "method": "Phương pháp",
    "results": "Kết quả",
    "limitations": "Hạn chế",
    "five_c": "5C (Keshav)",
    "text": "Nội dung",
    "page": "Trang",
    "answer": "Trả lời",
    "citations": "Trích dẫn",
    "quote": "Trích",
    "explanation": "Giải thích",
    "example": "Ví dụ",
    "prerequisites": "Cần biết trước",
    "symbols": "Ký hiệu",
    "intuition": "Trực giác",
    "numeric_example": "Ví dụ số",
    "assumptions": "Giả định",
    "baselines_fair": "Baseline công bằng?",
    "datasets": "Dữ liệu",
    "metrics": "Độ đo",
    "reproducibility": "Khả năng tái lập",
    "limitations_stated": "Hạn chế tác giả nêu",
    "limitations_unstated": "Hạn chế chưa nêu",
    "pseudocode": "Mã giả",
    "python_skeleton": "Khung Python",
    "hyperparameters": "Siêu tham số",
    "code_links_found": "Link code trong bài",
    "warning": "Cảnh báo",
}


_MD_SPECIAL = str.maketrans(
    {
        "<": "&lt;",
        ">": "&gt;",
        "[": "\\[",
        "]": "\\]",
        "|": "\\|",
        "`": "\\`",
        "*": "\\*",
        "_": "\\_",
        "\r": " ",
        "\n": " ",
    }
)


def md_escape(text: Any) -> str:
    """Thoát ký tự đặc biệt khi đưa chữ KHÔNG TIN CẬY (từ PDF/LLM) vào Markdown.

    Trình xem Markdown (Obsidian, Notion, GitHub...) có thể render HTML nhúng, ảnh từ xa, hoặc link javascript:.
    Mọi chữ ngoài được đưa về chữ thường (không còn là cú pháp)."""
    s = str(text or "").translate(_MD_SPECIAL).strip()
    if s.startswith(("#", "-", "+", ">", "=")):
        s = "\\" + s
    return s


def to_text(obj: Any, indent: int = 0) -> str:
    pad = "  " * indent
    if isinstance(obj, dict):
        lines = []
        for k, v in obj.items():
            if k in ("chunk_id", "segment_ids", "found", "verified"):
                continue
            name = _NAMES.get(k, k)
            if isinstance(v, (dict, list)) and v:
                lines.append(f"{pad}{name}:")
                lines.append(to_text(v, indent + 1))
            elif v not in (None, "", []):
                lines.append(f"{pad}{name}: {v}")
        return "\n".join(lines)
    if isinstance(obj, list):
        return "\n".join(f"{pad}- " + to_text(x, indent + 1).lstrip() for x in obj)
    return f"{pad}{obj}"
