"""Translator giả cho CI/test: không gọi mạng.

Sinh "tiếng Việt" giả dài ~1.3 lần bản gốc, chứa các ký tự có dấu khó (Ặ, Ữ, Ỹ, Ỡ) để kiểm tra
trường hợp xấu nhất về font và tràn khung. Giữ nguyên ký hiệu giữ chỗ/công thức/thẻ, số liệu và phủ định.
`complete_json` trả dữ liệu giả đúng schema cho các tác vụ khác (lĩnh vực, thuật ngữ, biên tập, trợ lý).
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from vietpdf.translate.base import Item, TranslateContext, estimate_tokens_simple

_WORDS = [
    "Ặng",
    "những",
    "Ữa",
    "người",
    "Ỹ",
    "được",
    "Ỡi",
    "trường",
    "nghiên",
    "cứu",
    "việc",
    "Ắc",
    "điều",
    "Ẫn",
    "mở",
    "rộng",
    "kết",
    "quả",
    "phương",
    "pháp",
    "dữ",
    "liệu",
    "Ổn",
    "định",
]
_TOKEN = re.compile(r"⟦[FMT][0-9x]{1,12}⟧|\d+(?:[.,]\d+)*")
_TAG_PAIR = re.compile(r"⟦([BI])⟧.*?⟦/\1⟧")
_NEG = re.compile(r"\b(not|no|never|without|cannot|n't)\b", re.IGNORECASE)


def fake_vietnamese(text: str) -> str:
    keep = _TOKEN.findall(text)
    tags = [m.group(1) for m in _TAG_PAIR.finditer(text)]
    plain = re.sub(r"⟦[^⟧]*⟧", "", text)
    target_len = int(len(_TOKEN.sub("", plain)) * 1.3)
    seed = int(hashlib.sha256(text.encode()).hexdigest()[:8], 16)
    out: list[str] = []
    if _NEG.search(plain):
        out.append("không")
    if re.search(r"\b(may|might|possibly|perhaps|likely)\b", plain, re.I):
        out.append("có thể")
    if re.search(r"\b(must|should|need to|have to)\b", plain, re.I):
        out.append("phải")
    length = 0
    i = 0
    while length < target_len:
        w = _WORDS[(seed + i * 7) % len(_WORDS)]
        out.append(w)
        length += len(w) + 1
        i += 1
    if keep:
        stride = max(1, len(out) // (len(keep) + 1))
        for k, tok in enumerate(keep, 1):
            out.insert(min(len(out), k * stride + k - 1), tok)
    for j, t in enumerate(tags):  # bọc lại một từ cho mỗi cặp thẻ
        pos = min(len(out) - 1, j * 2)
        out[pos] = f"⟦{t}⟧{out[pos]}⟦/{t}⟧"
    return " ".join(out)


def _stub(schema: dict[str, Any], data: Any) -> Any:
    t = schema.get("type")
    if "enum" in schema:
        return schema["enum"][0]
    if t == "object":
        return {k: _stub(v, data) for k, v in schema.get("properties", {}).items()}
    if t == "array":
        return [_stub(schema.get("items", {}), data)]
    if t in ("number", "integer"):
        return 1
    if t == "boolean":
        return False
    return "Nội dung giả do translator thử nghiệm tạo ra."


class FakeTranslator:
    provider = "fake"
    model = "fake-1"

    def __init__(self) -> None:
        self.calls = 0
        self.json_calls: list[str] = []

    async def list_models(self) -> list[str]:
        return [self.model]

    async def translate_batch(self, items: list[Item], ctx: TranslateContext) -> dict[str, str]:
        self.calls += 1
        result = {}
        for it in items:
            t = fake_vietnamese(it.text)
            if it.id in ctx.shorten_limits:
                t = re.sub(r"⟦[BI/]+⟧", "", t)[: ctx.shorten_limits[it.id]].rsplit(" ", 1)[0]
                missing = [tok for tok in _TOKEN.findall(it.text) if tok not in t]
                t = " ".join([t, *missing])
            for e in ctx.glossary:  # mô phỏng mô hình tuân thủ glossary
                if e.en.lower() in it.text.lower():
                    t += f" {e.en if e.policy == 'keep' else e.vi}"
            result[it.id] = t
        return result

    async def complete_json(self, messages: list[dict[str, str]], schema_name: str, schema: dict[str, Any]) -> Any:
        self.calls += 1
        self.json_calls.append(schema_name)
        data = json.loads(messages[-1]["content"])
        if schema_name == "edits":
            return {"edits": [{"id": r["id"], "text": r["draft"], "changes": []} for r in data["items"]]}
        if schema_name == "domain":
            return {"domains": ["ai"], "summary": "Tài liệu giả về học máy với tập huấn luyện nhỏ."}
        if schema_name == "terms":
            return {
                "terms": [
                    {
                        "en": c["term"],
                        "vi": fake_vietnamese(c["term"])[:40],
                        "policy": "translate",
                        "definition": "Định nghĩa giả.",
                        "confidence": 0.9,
                    }
                    for c in data["candidates"]
                ]
            }
        return _stub(schema, data)

    def estimate_tokens(self, items: list[Item]) -> int:
        return estimate_tokens_simple(items)

    async def aclose(self) -> None:
        return None
