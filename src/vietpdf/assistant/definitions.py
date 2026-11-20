"""Định nghĩa tại chỗ: tìm câu định nghĩa thuật ngữ, chữ viết tắt và ký hiệu NGAY TRONG BÀI (không tốn token).
Chỉ khi bài không định nghĩa mới dùng giải thích của AI (gắn nhãn "Kiến thức ngoài bài")."""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass

from vietpdf.core.ir import FORMULA_RE, DocumentIR

_PATTERNS = [
    re.compile(r"\b(?:we\s+)?define\s+(?P<t>[\w\-\s]{2,40}?)\s+as\s+(?P<d>[^.;]{5,300})", re.I),
    # neo \b + giới hạn độ dài từng từ: tránh backtracking O(n²) trên chuỗi dài không có khoảng trắng (ReDoS)
    re.compile(
        r"\b(?P<t>[A-Z]?[\w\-]{1,40}(?:\s[\w\-]{1,40}){0,3})\s+(?:is defined as|refers to|is called|is termed)\s+"
        r"(?P<d>[^.;]{5,300})",
        re.I,
    ),
    re.compile(r"\blet\s+(?P<t>\S{1,30})\s+(?:be|denote)\s+(?P<d>[^.;]{3,300})", re.I),
    re.compile(r"\bwhere\s+(?P<t>\S{1,30})\s+(?:is|denotes|represents|stands for)\s+(?P<d>[^.;,]{3,200})", re.I),
    re.compile(r"(?P<t>\S{1,30})\s+denotes\s+(?P<d>[^.;]{3,300})", re.I),
]
_ACRONYM = re.compile(r"\b(?P<d>(?:[A-Z][a-z]+[\s-]){1,6}[A-Z]?[a-z]+)\s*\((?P<t>[A-Z][A-Za-z]{1,7}?)s?\)")


@dataclass
class Definition:
    term: str
    definition: str
    sentence: str
    page: int  # đánh số từ 1
    kind: str  # term | acronym | symbol


def find_definitions(ir: DocumentIR) -> dict[str, list[Definition]]:
    out: dict[str, list[Definition]] = defaultdict(list)
    for seg in ir.segments():
        if seg.role not in ("body", "caption", "footnote", "ocr"):
            continue
        ftext = {f.key: f.text.strip() for f in seg.formulas}

        # thay ký hiệu công thức bằng chữ của nó để nhận ra định nghĩa ký hiệu: "where θ denotes ..."
        def sub(m: re.Match[str], ft: dict[str, str] = ftext) -> str:
            return f" {ft.get(m.group(1), '')} "

        text = FORMULA_RE.sub(sub, seg.source)
        text = re.sub(r"⟦/?[BI]⟧", "", text)
        text = re.sub(r"\s+", " ", text)
        for sent in re.split(r"(?<=[.!?])\s+", text):
            for m in _ACRONYM.finditer(sent):
                out[m.group("t").lower()].append(
                    Definition(m.group("t"), m.group("d"), sent[:500], seg.page + 1, "acronym")
                )
            for pat in _PATTERNS:
                for m in pat.finditer(sent):
                    term = m.group("t").strip(" ,:")
                    if not term or len(term) > 40:
                        continue
                    kind = (
                        "symbol"
                        if (len(term) <= 6 and not term.isalpha())
                        or any(f and f in term for f in ftext.values())
                        or len(term) <= 2
                        else "term"
                    )
                    out[term.lower()].append(Definition(term, m.group("d").strip(), sent[:500], seg.page + 1, kind))
    return dict(out)


def lookup(defs: dict[str, list[Definition]], term: str) -> list[Definition]:
    key = term.strip().lower()
    if key in defs:
        return defs[key]
    return [d for k, v in defs.items() if key in k.split() or k in key.split() for d in v][:5]
