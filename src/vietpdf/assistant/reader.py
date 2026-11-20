"""Trợ lý đọc hiểu theo phương pháp đọc 3 lượt (S. Keshav).

Nguyên tắc (xem docs/v1.5.md):
- Mọi kết luận về bài báo phải kèm id đoạn (chunk) làm bằng chứng; id/trích dẫn không có thật bị loại.
- Phần kiến thức AI tự thêm (không có trong bài) được gắn nhãn "Kiến thức ngoài bài".
- Kết quả được cache (tính một lần, dùng nhiều lần).
- Nội dung tài liệu luôn là dữ liệu trong JSON, không phải chỉ dẫn (chống prompt injection).
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any

from vietpdf.assistant.index import BM25Index, Chunk, build_chunks, quote_supported
from vietpdf.core.ir import DocumentIR
from vietpdf.translate.base import Translator
from vietpdf.translate.cache import TranslationCache
from vietpdf.translate.prompts import json_messages
from vietpdf.translate.qa import sanitize_output

AI_LABEL = "Giải thích của AI"
OUTSIDE_LABEL = "Kiến thức ngoài bài"
NOT_FOUND = "Không tìm thấy thông tin này trong tài liệu."
GITHUB_RE = re.compile(r"https?://(?:www\.)?github\.com/[\w.\-]+/[\w\-]+(?:\.[\w\-]+)*")


def _obj(props: dict[str, Any]) -> dict[str, Any]:
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


_S = {"type": "string"}
_B = {"type": "boolean"}
_CITED = _obj({"text": _S, "chunk_id": _S})
_LIST = {"type": "array", "items": _S}
_CITED_LIST = {"type": "array", "items": _CITED}

SCHEMAS: dict[str, dict[str, Any]] = {
    "overview": _obj(
        {
            "paper_type": _S,
            "problem": _CITED,
            "contributions": _CITED_LIST,
            "method": _CITED,
            "results": _CITED_LIST,
            "limitations": _CITED_LIST,
            "five_c": _obj({"category": _S, "context": _S, "correctness": _S, "contributions": _S, "clarity": _S}),
        }
    ),
    "answer": _obj(
        {"found": _B, "answer": _S, "citations": {"type": "array", "items": _obj({"chunk_id": _S, "quote": _S})}}
    ),
    "explain": _obj({"explanation": _S, "example": _S, "prerequisites": _LIST, "uses_outside_knowledge": _B}),
    "formula": _obj(
        {
            "symbols": {"type": "array", "items": _obj({"symbol": _S, "meaning": _S})},
            "intuition": _S,
            "numeric_example": _S,
        }
    ),
    "figure": _obj({"what_it_shows": _S, "axes_or_columns": _LIST, "takeaways": _LIST}),
    "prerequisites": _obj(
        {"concepts": {"type": "array", "items": _obj({"name": _S, "explanation": _S, "suggested_reading": _S})}}
    ),
    "critique": _obj(
        {
            "assumptions": _CITED_LIST,
            "baselines_fair": _S,
            "baselines_notes": _CITED_LIST,
            "datasets": _CITED_LIST,
            "metrics": _CITED_LIST,
            "reproducibility": _S,
            "limitations_stated": _CITED_LIST,
            "limitations_unstated": _LIST,
        }
    ),
    "reproduce": _obj(
        {
            "pseudocode": _S,
            "python_skeleton": _S,
            "hyperparameters": {"type": "array", "items": _obj({"name": _S, "value": _S, "chunk_id": _S})},
        }
    ),
    "skim": _obj(
        {
            "labels": {
                "type": "array",
                "items": _obj(
                    {"chunk_id": _S, "sentence": _S, "kind": {"type": "string", "enum": ["goal", "method", "result"]}}
                ),
            }
        }
    ),
    "sentence": _obj(
        {
            "main_subject": _S,
            "main_verb": _S,
            "clauses": {"type": "array", "items": _obj({"text": _S, "role": _S, "translation": _S})},
            "simplified": _S,
        }
    ),
}


@dataclass
class Assistant:
    ir: DocumentIR
    translator: Translator
    cache: TranslationCache | None = None
    chunks: list[Chunk] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.chunks = self.chunks or build_chunks(self.ir)
        self.index = BM25Index(self.chunks)

    # ------------------------------------------------------------------ hạ tầng

    async def _ask_json(self, task: str, instruction: str, rules: str, data: dict[str, Any]) -> Any:
        payload = json.dumps(data, ensure_ascii=False, sort_keys=True)
        key = TranslationCache.make_key(
            self.translator.provider,
            self.translator.model,
            f"assist:{task}",
            hashlib.sha256((instruction + rules + payload).encode()).hexdigest(),
        )
        if self.cache is not None:
            hit = self.cache.get_json(key)
            if hit is not None:
                return hit
        msgs = json_messages(instruction, rules, data)
        result = await self.translator.complete_json(msgs, task, SCHEMAS[task])
        if self.cache is not None and isinstance(result, dict):
            self.cache.put_json(key, result)
        return result

    def _chunks_payload(self, chunks: list[Chunk], limit: int = 12000) -> list[dict[str, Any]]:
        out, total = [], 0
        for c in chunks:
            if total > limit:
                break
            out.append({"chunk_id": c.id, "page": c.page + 1, "text": c.source[:2500]})
            total += len(c.source)
        return out

    def _cite(self, item: Any) -> dict[str, Any] | None:
        """Giữ ý có bằng chứng hợp lệ; gắn số trang thật từ chunk."""
        if not isinstance(item, dict):
            return None
        text = sanitize_output(str(item.get("text", "")), 2000)
        c = self.index.by_id.get(str(item.get("chunk_id", "")))
        if not text:
            return None
        if c is None:
            return {"text": text, "page": None, "chunk_id": None, "label": OUTSIDE_LABEL}
        return {"text": text, "page": c.page + 1, "chunk_id": c.id, "label": AI_LABEL}

    def _clean(self, s: Any, limit: int = 4000) -> str:
        return sanitize_output(str(s or ""), limit) or ""

    def _front_matter(self) -> list[Chunk]:
        """Lượt 1: tiêu đề, tóm tắt, mở đầu (2 trang đầu) và kết luận."""
        head = [c for c in self.chunks if c.page <= 1][:25]
        concl = [c for c in self.chunks if re.search(r"\b(conclusion|discussion|summary)\b", c.source[:80], re.I)]
        tail = self.chunks[-6:] if not concl else [c for c in self.chunks if int(c.id[1:]) >= int(concl[0].id[1:])][:8]
        return list({c.id: c for c in head + tail}.values())

    # ------------------------------------------------------------------ lượt 1: đọc lướt

    async def overview(self) -> dict[str, Any]:
        data = await self._ask_json(
            "overview",
            "Lập 'thẻ tổng quan 5 phút' bằng tiếng Việt cho bài báo/tài liệu dưới đây.",
            "Mỗi ý phải kèm chunk_id của đoạn làm bằng chứng (lấy đúng từ dữ liệu). Kết quả nêu con số cụ thể nếu có. "
            "Hạn chế: chỉ ghi những gì tác giả tự nêu. five_c theo phương pháp Keshav: category, context, "
            "correctness (giả định có hợp lý không), contributions, clarity.",
            {"title": self.ir.title, "chunks": self._chunks_payload(self._front_matter())},
        )
        d = data if isinstance(data, dict) else {}
        five_raw = d.get("five_c")
        five: dict[str, Any] = five_raw if isinstance(five_raw, dict) else {}
        return {
            "label": AI_LABEL,
            "paper_type": self._clean(d.get("paper_type"), 200),
            "problem": self._cite(d.get("problem")),
            "contributions": [x for x in (self._cite(i) for i in d.get("contributions", [])) if x],
            "method": self._cite(d.get("method")),
            "results": [x for x in (self._cite(i) for i in d.get("results", [])) if x],
            "limitations": [x for x in (self._cite(i) for i in d.get("limitations", [])) if x],
            "five_c": {k: self._clean(v, 800) for k, v in five.items()},
        }

    async def skim(self) -> list[dict[str, Any]]:
        """Tô màu đọc lướt: câu Mục tiêu / Phương pháp / Kết quả (câu phải có thật trong đoạn)."""
        data = await self._ask_json(
            "skim",
            "Đánh dấu các câu then chốt để đọc lướt.",
            "Chọn tối đa 12 câu NGUYÊN VĂN từ dữ liệu (không sửa chữ), gán kind: goal (mục tiêu/vấn đề), "
            "method (phương pháp), result (kết quả). Kèm chunk_id.",
            {"chunks": self._chunks_payload(self._front_matter())},
        )
        out = []
        rows: list[Any] = data.get("labels", []) if isinstance(data, dict) else []
        for row in rows:
            c = self.index.by_id.get(str(row.get("chunk_id", ""))) if isinstance(row, dict) else None
            sent = str(row.get("sentence", "")) if isinstance(row, dict) else ""
            if c and row.get("kind") in ("goal", "method", "result") and quote_supported(sent, c, 0.9):
                out.append(
                    {
                        "chunk_id": c.id,
                        "page": c.page,
                        "segment_ids": c.segment_ids,
                        "sentence": sent[:600],
                        "kind": row["kind"],
                    }
                )
        return out

    # ------------------------------------------------------------------ lượt 2: đọc hiểu

    async def explain(self, text: str, kind: str = "paragraph", context_query: str | None = None) -> dict[str, Any]:
        """Giải thích dễ hiểu một đoạn / công thức / bảng-hình. `kind`: paragraph | formula | figure."""
        ctx = self._chunks_payload(self.index.search(context_query or text, k=4), 6000)
        if kind == "formula":
            data = await self._ask_json(
                "formula",
                "Giải thích công thức cho người mới học bằng tiếng Việt.",
                "Nêu ý nghĩa từng ký hiệu (ưu tiên định nghĩa trong bài), trực giác, "
                "và một ví dụ số nhỏ tính tay được.",
                {"formula": text[:2000], "context": ctx},
            )
            d = data if isinstance(data, dict) else {}
            return {
                "label": AI_LABEL,
                "symbols": [
                    {"symbol": self._clean(s.get("symbol"), 100), "meaning": self._clean(s.get("meaning"), 600)}
                    for s in d.get("symbols", [])
                    if isinstance(s, dict)
                ],
                "intuition": self._clean(d.get("intuition")),
                "numeric_example": self._clean(d.get("numeric_example")),
            }
        if kind == "figure":
            data = await self._ask_json(
                "figure",
                "Giải thích bảng/hình cho người đọc tiếng Việt.",
                "Nêu hình/bảng cho thấy điều gì, ý nghĩa các trục/cột, và 2–4 điểm cần rút ra. Không bịa số liệu.",
                {"figure_text_and_caption": text[:4000], "context": ctx},
            )
            d = data if isinstance(data, dict) else {}
            return {
                "label": AI_LABEL,
                "what_it_shows": self._clean(d.get("what_it_shows")),
                "axes_or_columns": [self._clean(x, 400) for x in d.get("axes_or_columns", [])][:20],
                "takeaways": [self._clean(x, 600) for x in d.get("takeaways", [])][:10],
            }
        data = await self._ask_json(
            "explain",
            "Diễn giải lại đoạn văn bằng lời đơn giản, tiếng Việt, cho sinh viên.",
            "Kèm một ví dụ, liệt kê khái niệm cần biết trước. uses_outside_knowledge=true nếu phải dùng kiến thức "
            "không có trong bài.",
            {"paragraph": text[:4000], "context": ctx},
        )
        d = data if isinstance(data, dict) else {}
        return {
            "label": OUTSIDE_LABEL if d.get("uses_outside_knowledge") else AI_LABEL,
            "explanation": self._clean(d.get("explanation")),
            "example": self._clean(d.get("example")),
            "prerequisites": [self._clean(x, 200) for x in d.get("prerequisites", [])][:10],
        }

    async def prerequisites(self) -> list[dict[str, str]]:
        data = await self._ask_json(
            "prerequisites",
            "Liệt kê kiến thức nền cần biết trước để đọc tài liệu này.",
            "5–10 khái niệm; mỗi khái niệm giải thích 2–3 câu tiếng Việt và gợi ý tài liệu nền (tên sách/khoá học).",
            {"title": self.ir.title, "chunks": self._chunks_payload(self._front_matter(), 8000)},
        )
        return [
            {
                "name": self._clean(c.get("name"), 200),
                "explanation": self._clean(c.get("explanation"), 1200),
                "suggested_reading": self._clean(c.get("suggested_reading"), 400),
                "label": OUTSIDE_LABEL,
            }
            for c in ((data or {}).get("concepts", []) if isinstance(data, dict) else [])
            if isinstance(c, dict)
        ]

    async def analyze_sentence(self, sentence: str) -> dict[str, Any]:
        data = await self._ask_json(
            "sentence",
            "Phân tích ngữ pháp câu tiếng Anh dài cho người Việt học đọc tài liệu chuyên ngành.",
            "Chỉ ra chủ ngữ chính, động từ chính, tách các mệnh đề (vai trò + dịch), và viết lại câu ngắn gọn.",
            {"sentence": sentence[:2000]},
        )
        d = data if isinstance(data, dict) else {}
        return {
            "label": AI_LABEL,
            "main_subject": self._clean(d.get("main_subject"), 300),
            "main_verb": self._clean(d.get("main_verb"), 200),
            "clauses": [
                {k: self._clean(c.get(k), 800) for k in ("text", "role", "translation")}
                for c in d.get("clauses", [])
                if isinstance(c, dict)
            ][:20],
            "simplified": self._clean(d.get("simplified"), 1500),
        }

    # ------------------------------------------------------------------ lượt 3: phân tích sâu

    async def ask(self, question: str, k: int = 6) -> dict[str, Any]:
        """Hỏi đáp có dẫn trang. Trích dẫn được kiểm tra có thật trong đoạn truy xuất; không có thì nói rõ."""
        hits = self.index.search(question, k=k)
        if not hits:
            return {"found": False, "answer": NOT_FOUND, "citations": []}
        data = await self._ask_json(
            "answer",
            "Trả lời câu hỏi của người đọc CHỈ dựa trên các đoạn tài liệu được cung cấp, bằng tiếng Việt.",
            "Mỗi ý phải kèm trích dẫn: chunk_id và quote là câu NGUYÊN VĂN (tiếng gốc) trong đoạn đó. Nếu tài liệu "
            "không chứa câu trả lời, đặt found=false và không đoán.",
            {"question": question[:1000], "chunks": self._chunks_payload(hits, 10000)},
        )
        d = data if isinstance(data, dict) else {}
        cites = []
        for cobj in d.get("citations", []) if isinstance(d.get("citations"), list) else []:
            c = self.index.by_id.get(str(cobj.get("chunk_id", ""))) if isinstance(cobj, dict) else None
            if c is not None and c in hits and quote_supported(str(cobj.get("quote", "")), c):
                quote = sanitize_output(str(cobj["quote"]), 600) or ""
                cites.append({"chunk_id": c.id, "page": c.page + 1, "quote": quote[:600]})
        answer = self._clean(d.get("answer"))
        if not d.get("found") or not answer:
            return {"found": False, "answer": NOT_FOUND, "citations": []}
        if not cites:
            return {
                "found": True,
                "verified": False,
                "answer": answer,
                "citations": [],
                "warning": "Không xác minh được trích dẫn trong tài liệu — hãy kiểm tra lại.",
            }
        return {"found": True, "verified": True, "answer": answer, "citations": cites, "label": AI_LABEL}

    async def critique(self) -> dict[str, Any]:
        queries = [
            "baseline comparison",
            "dataset benchmark evaluation",
            "metric accuracy evaluation",
            "assume assumption",
            "limitation future work",
            "code available github reproduce",
        ]
        hits = list({c.id: c for q in queries for c in self.index.search(q, k=3)}.values())
        data = await self._ask_json(
            "critique",
            "Lập bảng kiểm phản biện cho bài báo bằng tiếng Việt.",
            "Mỗi mục rút từ bài phải kèm chunk_id. baselines_fair: 'có' / 'không' / 'không rõ' kèm lý do. "
            "limitations_unstated: hạn chế bạn suy ra nhưng tác giả KHÔNG nêu (sẽ được gắn nhãn kiến thức ngoài bài).",
            {"title": self.ir.title, "chunks": self._chunks_payload(hits, 12000)},
        )
        d = data if isinstance(data, dict) else {}
        links = sorted({m for c in self.chunks for m in GITHUB_RE.findall(c.source)})

        def cl(key: str) -> list[dict[str, Any]]:
            return [x for x in (self._cite(i) for i in d.get(key, [])) if x]

        return {
            "label": AI_LABEL,
            "assumptions": cl("assumptions"),
            "baselines_fair": self._clean(d.get("baselines_fair"), 600),
            "baselines_notes": cl("baselines_notes"),
            "datasets": cl("datasets"),
            "metrics": cl("metrics"),
            "reproducibility": self._clean(d.get("reproducibility"), 1500),
            "code_links_found": links,
            "limitations_stated": cl("limitations_stated"),
            "limitations_unstated": [
                {"text": self._clean(x, 800), "label": OUTSIDE_LABEL} for x in d.get("limitations_unstated", [])
            ][:10],
        }

    async def reproduce(self) -> dict[str, Any]:
        """Tái hiện: mã giả + khung code Python (CHỈ để đọc, ứng dụng không bao giờ chạy code này)."""
        queries = [
            "algorithm procedure step",
            "hyperparameter learning rate batch size epochs optimizer",
            "architecture layer model training",
            "implementation details",
        ]
        hits = list({c.id: c for q in queries for c in self.index.search(q, k=3)}.values())
        data = await self._ask_json(
            "reproduce",
            "Rút các bước thuật toán thành mã giả và khung code Python để người đọc tự cài đặt.",
            "Mã giả ngắn gọn. Khung Python chỉ gồm hàm/lớp với TODO, không gọi mạng, không đọc/ghi file hệ thống. "
            "Siêu tham số kèm chunk_id nơi bài nêu giá trị.",
            {"chunks": self._chunks_payload(hits, 12000)},
        )
        d = data if isinstance(data, dict) else {}
        hps = []
        for h in d.get("hyperparameters", []) if isinstance(d.get("hyperparameters"), list) else []:
            c = self.index.by_id.get(str(h.get("chunk_id", ""))) if isinstance(h, dict) else None
            if c is not None and str(h.get("value", "")).strip() and str(h["value"]).split()[0].strip(".,") in c.source:
                hps.append(
                    {"name": self._clean(h.get("name"), 200), "value": str(h["value"])[:100], "page": c.page + 1}
                )
        return {
            "label": AI_LABEL,
            "pseudocode": str(d.get("pseudocode", ""))[:8000],
            "python_skeleton": str(d.get("python_skeleton", ""))[:12000],
            "hyperparameters": hps,
            "code_links_found": sorted({m for c in self.chunks for m in GITHUB_RE.findall(c.source)}),
            "note": "Khung code chỉ để tham khảo; ứng dụng không chạy mã này.",
        }
