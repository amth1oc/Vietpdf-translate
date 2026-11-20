"""Xuất ghi chú: Markdown (Obsidian/Notion), BibTeX, thẻ Anki (TSV), ma trận so sánh nhiều bài (CSV/Markdown).

Mọi chuỗi đến từ tài liệu/LLM được làm sạch trước khi ghi (không để lọt cú pháp lạ vào BibTeX/CSV)."""

from __future__ import annotations

import csv
import io
import re
import unicodedata
from typing import Any

from vietpdf.core.ir import DocumentIR
from vietpdf.textfmt import md_escape


def _md(s: Any) -> str:
    return md_escape(s)


def _cite(c: dict[str, Any] | None) -> str:
    if not c:
        return ""
    page = f" (tr. {c['page']})" if c.get("page") else f" ({c.get('label', '')})"
    return f"{_md(c.get('text'))}{page}"


def notes_markdown(
    ir: DocumentIR,
    overview: dict[str, Any] | None = None,
    qa: list[dict[str, Any]] | None = None,
    highlights: list[dict[str, Any]] | None = None,
    vocab: list[dict[str, Any]] | None = None,
) -> str:
    lines = [
        f"# {_md(ir.title) or _md(ir.source_name)}",
        "",
        f"- Nguồn: {_md(ir.source_name)} (SHA-256 `{ir.source_sha256[:16]}…`)",  # tên file là dữ liệu ngoài (L1-018)
    ]
    if ir.domains:
        lines.append(f"- Lĩnh vực: {', '.join(ir.domains)}")
    if ir.summary:
        lines += ["", "## Tóm tắt (Giải thích của AI)", "", _md(ir.summary)]
    if overview:
        lines += ["", "## Thẻ tổng quan (Giải thích của AI)", ""]
        lines.append(f"- **Loại bài:** {_md(overview.get('paper_type'))}")
        lines.append(f"- **Vấn đề:** {_cite(overview.get('problem'))}")
        lines.append(f"- **Phương pháp:** {_cite(overview.get('method'))}")
        for key, title in (
            ("contributions", "Đóng góp"),
            ("results", "Kết quả"),
            ("limitations", "Hạn chế tác giả nêu"),
        ):
            items = overview.get(key) or []
            if items:
                lines.append(f"- **{title}:**")
                lines += [f"  - {_cite(c)}" for c in items]
    if highlights:
        lines += ["", "## Câu then chốt", ""]
        names = {"goal": "Mục tiêu", "method": "Phương pháp", "result": "Kết quả"}
        lines += [
            f"- [{names.get(h['kind'], h['kind'])}] {_md(h['sentence'])} (tr. {h['page'] + 1})" for h in highlights
        ]
    if ir.terms:
        lines += ["", "## Thuật ngữ", "", "| Thuật ngữ | Bản dịch | Định nghĩa |", "|---|---|---|"]
        lines += [f"| {_md(t.en)} | {_md(t.vi)} | {_md(t.definition)} |" for t in ir.terms[:300]]
    if qa:
        lines += ["", "## Hỏi đáp", ""]
        for item in qa:
            lines.append(f"**H:** {_md(item.get('question'))}")
            lines.append(f"**Đ:** {_md(item.get('answer'))}")
            for c in item.get("citations", []):
                lines.append(f"> tr. {c['page']}: “{_md(c['quote'])}”")
            lines.append("")
    if vocab:
        lines += ["", "## Sổ từ", ""] + [
            f"- **{_md(v['term'])}**: {_md(v['vi'])} — {_md(v.get('example'))}" for v in vocab
        ]
    return "\n".join(lines) + "\n"


def _bib_escape(s: str) -> str:
    s = unicodedata.normalize("NFC", s)
    s = re.sub(r"[{}\\@]", "", s)  # không cho chèn lệnh/khối BibTeX
    return re.sub(r"\s+", " ", s).strip()[:500]


def bibtex(meta: dict[str, Any], key_hint: str = "") -> str:
    """meta: title, authors[], year, venue, doi, arxiv, url."""
    authors = [a for a in meta.get("authors") or [] if a]
    first = re.sub(r"\W", "", (authors[0].split()[-1] if authors else key_hint or "doc"))[:20] or "doc"
    key = f"{first.lower()}{meta.get('year') or ''}"
    fields = {
        "title": meta.get("title"),
        "author": " and ".join(authors),
        "year": meta.get("year"),
        "journal": meta.get("venue"),
        "doi": meta.get("doi"),
        "eprint": meta.get("arxiv"),
        "url": meta.get("url"),
    }
    kind = "article" if meta.get("venue") else "misc"
    body = ",\n".join(f"  {k} = {{{_bib_escape(str(v))}}}" for k, v in fields.items() if v)
    return f"@{kind}{{{key},\n{body}\n}}\n"


def anki_tsv(cards: list[dict[str, str]]) -> str:
    """Thẻ Anki dạng văn bản (Anki: File > Import). Mặt trước: thuật ngữ + câu ví dụ; mặt sau: nghĩa + định nghĩa.

    ``#html:false`` buộc Anki coi mọi trường là chữ thuần (thẻ <img>/<script> trong dữ liệu từ PDF/AI không chạy)."""
    buf = io.StringIO()
    buf.write("#separator:tab\n#html:false\n#columns:Front\tBack\tTags\n")
    w = csv.writer(buf, delimiter="\t", lineterminator="\n")
    for c in cards:

        def clean(x: str) -> str:
            return re.sub(r"[\t\r\n]+", " ", str(x or "")).strip()[:1000]

        front = clean(c.get("term", "")) + (f" — “{clean(c.get('example', ''))}”" if c.get("example") else "")
        back = clean(c.get("vi", "")) + (f" — {clean(c.get('definition', ''))}" if c.get("definition") else "")
        if front.startswith("#"):  # dòng bắt đầu bằng # bị Anki hiểu là dòng tiêu đề/tuỳ chọn
            front = " " + front
        w.writerow([front, back, "vietpdf " + clean(c.get("tag", ""))])
    return buf.getvalue()


_FORMULA_START = ("=", "+", "-", "@", "\t", "\r", "|", "%")


def _csv_safe(v: Any) -> str:
    """Chống CSV injection: ô bắt đầu bằng = + - @ (kể cả dạng toàn chiều rộng như ＝, hay đứng sau ký tự vô hình)
    bị bảng tính hiểu là công thức (L1-017). Ký tự định dạng vô hình ở đầu ô bị bỏ."""
    s = re.sub(r"\s+", " ", str(v or "")).strip()
    i = 0
    while i < len(s) and unicodedata.category(s[i]) in ("Cf", "Cc", "Zs"):
        i += 1
    s = s[i:]
    first = unicodedata.normalize("NFKC", s[:1])
    return "'" + s if first[:1] in _FORMULA_START else s


def comparison_matrix(rows: list[dict[str, Any]]) -> tuple[str, str]:
    """Ma trận so sánh nhiều bài từ thẻ tổng quan: trả về (CSV, Markdown)."""
    cols = ["Bài", "Loại", "Vấn đề", "Phương pháp", "Kết quả chính", "Hạn chế"]
    table = []
    for r in rows:
        ov = r.get("overview", {})
        table.append(
            [
                r.get("title", ""),
                ov.get("paper_type", ""),
                (ov.get("problem") or {}).get("text", ""),
                (ov.get("method") or {}).get("text", ""),
                "; ".join(c.get("text", "") for c in ov.get("results", [])[:3]),
                "; ".join(c.get("text", "") for c in ov.get("limitations", [])[:3]),
            ]
        )
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")  # ghi bằng write_text: \n tự đổi theo hệ điều hành
    w.writerow(cols)
    w.writerows([[_csv_safe(c) for c in row] for row in table])
    md = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    md += ["| " + " | ".join(md_escape(str(c)[:300]) for c in row) + " |" for row in table]
    return buf.getvalue(), "\n".join(md) + "\n"
