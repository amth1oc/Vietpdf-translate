"""v1.5 — trợ lý đọc hiểu; v2.0 — nhà cung cấp mới, ngôn ngữ nguồn khác, tóm tắt chương."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import make_samples
import pytest

from vietpdf.assistant.citations import CitationLookup, Reference, expand_marker, parse_references
from vietpdf.assistant.definitions import find_definitions, lookup
from vietpdf.assistant.export import anki_tsv, bibtex, comparison_matrix, notes_markdown
from vietpdf.assistant.index import BM25Index, build_chunks, quote_supported
from vietpdf.assistant.library import Zotero
from vietpdf.assistant.reader import NOT_FOUND, Assistant
from vietpdf.core.ir import DocumentIR, PageIR, Segment
from vietpdf.core.pipeline import PipelineOptions, run_pipeline
from vietpdf.library.store import LearningStore
from vietpdf.translate.base import Item, TranslateContext
from vietpdf.translate.fake import FakeTranslator
from vietpdf.translate.providers import PROVIDERS, build_translator

FAKE_KEY = "sk-test-0123456789abcdef"  # gitleaks:allow (khoá giả cho test)


def run(coro):  # type: ignore[no-untyped-def]
    return asyncio.run(coro)


def make_ir(texts: list[tuple[int, str]]) -> DocumentIR:
    pages: dict[int, list[Segment]] = {}
    for i, (page, text) in enumerate(texts):
        pages.setdefault(page, []).append(
            Segment(
                id=f"p{page}-s{i}",
                page=page,
                bbox=(0, i * 20, 400, i * 20 + 15),
                source=text,
                order=i,
                translation=f"[vi] {text}",
                status="translated",
            )
        )
    return DocumentIR(
        source_name="t.pdf",
        source_sha256="0" * 64,
        page_count=max(pages) + 1,
        title="Test paper",
        pages=[PageIR(number=n, width=595, height=842, segments=s) for n, s in sorted(pages.items())],
    )


IR = make_ir(
    [
        (0, "We propose ResNet, a residual network. Residual learning eases the training of very deep networks."),
        (0, "The learning rate is set to 0.1 and the batch size is 256. A Convolutional Neural Network (CNN) is used."),
        (
            1,
            "Results show that the error decreases by 3.6 points on ImageNet. Code: https://github.com/kaiming/resnet.",
        ),
        (1, "where θ denotes the model parameters. We define the residual mapping as F(x) = H(x) - x."),
        (2, "References"),
        (2, "[1] K. He, X. Zhang. Deep Residual Learning for Image Recognition. CVPR, 2016. doi:10.1109/CVPR.2016.90"),
        (2, "[2] A. Vaswani et al. Attention is all you need. NeurIPS 2017. arXiv:1706.03762"),
    ]
)


class Scripted(FakeTranslator):
    """Trả lời theo kịch bản để kiểm tra phần xác minh trích dẫn."""

    def __init__(self, replies: dict[str, Any]) -> None:
        super().__init__()
        self.replies = replies

    async def complete_json(self, messages: list[dict[str, str]], schema_name: str, schema: dict[str, Any]) -> Any:
        self.json_calls.append(schema_name)
        r = self.replies.get(schema_name)
        return r(json.loads(messages[1]["content"])) if callable(r) else r


# --------------------------------------------------------------------------- v1.5


def test_bm25_and_quote_verification() -> None:
    chunks = build_chunks(IR)
    idx = BM25Index(chunks)
    top = idx.search("learning rate batch size")
    assert "0.1" in top[0].source
    assert quote_supported("The learning rate is set to 0.1", top[0])
    assert not quote_supported("The learning rate is set to 0.5 with warmup", top[0])


def test_ask_drops_fabricated_citations() -> None:
    def answer(data: dict[str, Any]) -> dict[str, Any]:
        cid = data["chunks"][0]["chunk_id"]
        return {
            "found": True,
            "answer": "Tốc độ học là 0.1.",
            "citations": [
                {"chunk_id": cid, "quote": "The learning rate is set to 0.1"},
                {"chunk_id": cid, "quote": "The learning rate was tuned to 0.7 on validation"},  # bịa
                {"chunk_id": "c999", "quote": "anything"},
            ],
        }  # id không tồn tại

    res = run(Assistant(IR, Scripted({"answer": answer})).ask("What is the learning rate?"))
    assert res["verified"] and len(res["citations"]) == 1 and res["citations"][0]["page"] == 1


def test_ask_not_found_and_unverified() -> None:
    a = Assistant(IR, Scripted({"answer": {"found": False, "answer": "", "citations": []}}))
    assert run(a.ask("What GPU was used?"))["answer"] == NOT_FOUND
    b = Assistant(
        IR,
        Scripted(
            {
                "answer": {
                    "found": True,
                    "answer": "Dùng 8 GPU.",
                    "citations": [{"chunk_id": "c0", "quote": "We trained on eight GPUs for a week"}],
                }
            }
        ),
    )
    res = run(b.ask("residual learning training"))
    assert res["verified"] is False and "warning" in res


def test_overview_cites_real_pages_and_labels_outside_knowledge() -> None:
    def ov(data: dict[str, Any]) -> dict[str, Any]:
        cid = data["chunks"][0]["chunk_id"]
        return {
            "paper_type": "Phương pháp mới",
            "problem": {"text": "Khó huấn luyện mạng rất sâu", "chunk_id": cid},
            "contributions": [{"text": "Học phần dư", "chunk_id": cid}],
            "method": {"text": "ResNet", "chunk_id": cid},
            "results": [{"text": "Giảm lỗi 3.6 điểm", "chunk_id": "c-khong-co"}],
            "limitations": [],
            "five_c": {"category": "a", "context": "b", "correctness": "c", "contributions": "d", "clarity": "e"},
        }

    res = run(Assistant(IR, Scripted({"overview": ov})).overview())
    assert res["problem"]["page"] == 1 and res["results"][0]["label"] == "Kiến thức ngoài bài"


def test_reproduce_only_keeps_hyperparameters_found_in_paper() -> None:
    def rep(data: dict[str, Any]) -> dict[str, Any]:
        cid = next(c["chunk_id"] for c in data["chunks"] if "0.1" in c["text"])
        return {
            "pseudocode": "for epoch...",
            "python_skeleton": "def train(): ...",
            "hyperparameters": [
                {"name": "learning rate", "value": "0.1", "chunk_id": cid},
                {"name": "weight decay", "value": "5e-4", "chunk_id": cid},
            ],
        }

    res = run(Assistant(IR, Scripted({"reproduce": rep})).reproduce())
    assert [h["name"] for h in res["hyperparameters"]] == ["learning rate"]
    assert res["code_links_found"] == ["https://github.com/kaiming/resnet"]


def test_skim_requires_verbatim_sentences() -> None:
    def sk(data: dict[str, Any]) -> dict[str, Any]:
        cid = data["chunks"][0]["chunk_id"]
        return {
            "labels": [
                {"chunk_id": cid, "sentence": "We propose ResNet, a residual network.", "kind": "goal"},
                {"chunk_id": cid, "sentence": "We beat every method by 50 points.", "kind": "result"},
            ]
        }

    out = run(Assistant(IR, Scripted({"skim": sk})).skim())
    assert [o["kind"] for o in out] == ["goal"]


def test_local_definitions() -> None:
    defs = find_definitions(IR)
    assert lookup(defs, "CNN")[0].definition == "Convolutional Neural Network"
    assert any(d.kind == "symbol" for d in lookup(defs, "θ"))
    assert "H(x) - x" in lookup(defs, "the residual mapping")[0].definition


def test_references_and_markers() -> None:
    refs = parse_references(IR)
    assert [r.number for r in refs] == [1, 2]
    assert refs[0].doi == "10.1109/CVPR.2016.90" and refs[1].arxiv == "1706.03762"
    assert "Deep Residual Learning" in refs[0].title_guess
    assert expand_marker("[3, 5-7]") == [3, 5, 6, 7]


def test_citation_lookup_sends_only_identifiers() -> None:
    seen: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(str(req.url))
        if "semanticscholar" in req.url.host:
            return httpx.Response(
                200,
                json={
                    "title": "Deep Residual Learning",
                    "year": 2016,
                    "tldr": {"text": "Residual nets."},
                    "authors": [{"name": "K. He"}],
                },
            )
        return httpx.Response(404)

    async def go() -> dict[str, Any]:
        cl = CitationLookup(transport=httpx.MockTransport(handler))
        try:
            return await cl.lookup(Reference(1, "x", 1, doi="10.1109/CVPR.2016.90"))
        finally:
            await cl.aclose()

    res = run(go())
    assert res["tldr"] == "Residual nets." and res["source"] == "Semantic Scholar"
    assert len(seen) == 1 and "DOI:10.1109/CVPR.2016.90" in seen[0] and "residual" not in seen[0].split("?")[0].lower()


def test_exports_are_sanitized() -> None:
    bib = bibtex({"title": "Evil} @misc{x, title={pwn", "authors": ["Kaiming He"], "year": 2016})
    assert bib.count("@") == 1 and "}," in bib
    csv_text, _md = comparison_matrix([{"title": "=cmd|' /C calc'!A0", "overview": {"paper_type": "+SUM(1)"}}])
    assert "'=cmd" in csv_text and "'+SUM" in csv_text  # chống CSV injection
    tsv = anki_tsv([{"term": "overfitting\tX", "vi": "quá khớp\nY", "example": "prone to overfitting"}])
    assert "overfitting X" in tsv and "#separator:tab" in tsv
    md_notes = notes_markdown(IR)
    assert md_notes.startswith("# Test paper")


def test_vocabulary_store(tmp_path: Path) -> None:
    v = LearningStore(tmp_path / "v.db", tmp_path / "lib")  # 2.1: sổ từ chuyển sang kho học tập
    v.add_word("overfitting", "quá khớp", doc_id="a", example="prone to overfitting")
    v.add_word("overfitting", "quá khớp", doc_id="a", definition="def")  # cập nhật, không trùng
    assert len(v.list_words("a")) == 1 and v.list_words()[0]["definition"] == "def"
    v.close()


def test_zotero_is_localhost_only_and_read_only() -> None:
    seen: list[tuple[str, str]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append((req.method, str(req.url)))
        return httpx.Response(
            200,
            json=[
                {
                    "data": {
                        "key": "ABC123",
                        "title": "Paper",
                        "date": "2020",
                        "creators": [{"firstName": "A", "lastName": "B"}],
                    }
                }
            ],
        )

    async def go() -> list[dict[str, Any]]:
        z = Zotero(transport=httpx.MockTransport(handler))
        try:
            with pytest.raises(ValueError):
                await z.pdf_path("../../etc")
            return await z.items("deep")
        finally:
            await z.aclose()

    items = run(go())
    assert items[0]["key"] == "ABC123" and all(m == "GET" and "127.0.0.1:23119" in u for m, u in seen)


def test_highlight_original(tmp_path: Path) -> None:
    import pymupdf

    from vietpdf.core import render

    src = tmp_path / "p.pdf"
    make_samples.make_paper(src)
    n = render.highlight_original(
        str(src),
        str(tmp_path / "p.skim.pdf"),
        [{"page": 0, "sentence": "We evaluate the neural network on three datasets.", "kind": "method"}],
    )
    assert n == 1 and len(list(pymupdf.open(tmp_path / "p.skim.pdf")[0].annots())) == 1


# --------------------------------------------------------------------------- v2.0


def test_new_providers_registered() -> None:
    for pid in ("claude", "deepseek", "openrouter", "groq"):
        assert pid in PROVIDERS and PROVIDERS[pid].base_url.startswith("https://")


def _capture(provider: str) -> dict[str, Any]:
    seen: dict[str, Any] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        seen.update(headers=dict(req.headers), body=body)
        items = json.loads(body["messages"][1]["content"])["items"]
        content = json.dumps({"translations": [{"id": i["id"], "text": "Xin chào"} for i in items]})
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    t = build_translator(provider, "m", api_key=FAKE_KEY, transport=httpx.MockTransport(handler))
    run(t.translate_batch([Item("x", "Hello")], TranslateContext()))
    run(t.aclose())
    return seen


def test_claude_uses_prompt_json_mode_and_api_key_header() -> None:
    seen = _capture("claude")
    assert "response_format" not in seen["body"]  # API Claude bỏ qua response_format: mô tả schema trong prompt
    assert "JSON Schema" in seen["body"]["messages"][0]["content"]
    assert seen["headers"]["x-api-key"] and seen["headers"]["anthropic-version"]


def test_deepseek_uses_json_object() -> None:
    assert _capture("deepseek")["body"]["response_format"] == {"type": "json_object"}


def test_json_schema_fallback_on_unsupported_model() -> None:
    calls: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        calls.append(body.get("response_format", {}).get("type", "none"))
        if body["response_format"]["type"] == "json_schema":
            return httpx.Response(400, json={"error": {"message": "json_schema is not supported by this model"}})
        items = json.loads(body["messages"][1]["content"])["items"]
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps({"translations": [{"id": i["id"], "text": "ok"} for i in items]})
                        }
                    }
                ]
            },
        )

    t = build_translator("groq", "m", api_key="gsk_" + "a" * 30, transport=httpx.MockTransport(handler))
    assert run(t.translate_batch([Item("x", "Hi")], TranslateContext())) == {"x": "ok"}
    assert calls == ["json_schema", "json_object"]
    run(t.aclose())


def test_source_language_other_than_english(tmp_path: Path) -> None:
    import pymupdf

    from vietpdf.translate.prompts import build_system_prompt

    assert "tiếng Trung" in build_system_prompt(TranslateContext(source_lang="zh"))
    src = tmp_path / "fr.pdf"
    doc = pymupdf.open()
    doc.new_page().insert_textbox(
        pymupdf.Rect(72, 72, 500, 200),
        "L'apprentissage automatique est une branche de l'intelligence artificielle.",
        fontname="Times-Roman",
        fontsize=12,
    )
    doc.save(str(src))
    res = run(
        run_pipeline(
            src,
            tmp_path / "fr.vi.pdf",
            FakeTranslator(),
            PipelineOptions(auto_terms=False, ctx=TranslateContext(source_lang="fr")),
        )
    )
    ir = DocumentIR.model_validate_json(res.ir_path.read_text("utf-8"))  # type: ignore[union-attr]
    assert res.translated == 1 and ir.source_lang == "fr"


def test_chapter_summaries_used_as_context(tmp_path: Path) -> None:
    import pymupdf

    src = tmp_path / "book.pdf"
    doc = pymupdf.open()
    for i in range(4):
        doc.new_page().insert_textbox(
            pymupdf.Rect(72, 72, 520, 700),
            f"Chapter text about topic {i}. " * 30,
            fontname="Times-Roman",
            fontsize=11,
        )
    doc.set_toc([[1, "Chapter 1", 1], [1, "Chapter 2", 3]])
    doc.save(str(src))
    seen: list[str] = []

    class Spy(FakeTranslator):
        async def translate_batch(self, items: list[Item], ctx: TranslateContext) -> dict[str, str]:
            seen.append(ctx.summary)
            return await super().translate_batch(items, ctx)

        async def complete_json(self, messages: list[dict[str, str]], schema_name: str, schema: dict[str, Any]) -> Any:
            if schema_name == "chapter_summary":
                return {"summary": "Tóm tắt chương " + json.loads(messages[1]["content"])["chapter"][:30]}
            return await super().complete_json(messages, schema_name, schema)

    res = run(
        run_pipeline(src, tmp_path / "book.vi.pdf", Spy(), PipelineOptions(auto_terms=False, chapter_summaries=True))
    )
    ir = DocumentIR.model_validate_json(res.ir_path.read_text("utf-8"))  # type: ignore[union-attr]
    assert set(ir.chapter_summaries) == {"1", "3"}
    assert any("Chương hiện tại" in s for s in seen)
