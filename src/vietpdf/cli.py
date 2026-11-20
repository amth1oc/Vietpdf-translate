"""Dòng lệnh `vietpdf`. Xem `vietpdf --help` và docs/HUONG_DAN.md."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import csv
import getpass
import io
import json
import logging
import multiprocessing
import sys
import time
from pathlib import Path
from typing import Any

from vietpdf import __version__
from vietpdf.config import cache_dir, load_settings
from vietpdf.security.logsafe import setup_logging
from vietpdf.textfmt import to_text

log = logging.getLogger("vietpdf")
STAGES = {
    "check": "Kiểm tra",
    "extract": "Đọc PDF",
    "ocr": "OCR",
    "terms": "Thuật ngữ",
    "translate": "Dịch",
    "edit": "Biên tập",
    "fit": "Dàn chữ",
    "render": "Dựng PDF",
}


def _progress(stage: str, frac: float, msg: str) -> None:
    bar = "#" * int(frac * 20)
    sys.stderr.write(f"\r{STAGES.get(stage, stage):<10} [{bar:<20}] {frac * 100:5.1f}% {msg[:40]:<40}")
    if frac >= 1:
        sys.stderr.write("\n")
    sys.stderr.flush()


def _provider_ids() -> list[str]:
    from vietpdf.translate.providers import PROVIDERS  # noqa: PLC0415

    return list(PROVIDERS)


def _make_translator(
    provider: str,
    model: str,
    base_url: str | None,
    rpm: float | None,
    offline: bool,
    quiet: bool = False,
    accept_privacy: bool = False,
) -> Any:
    from vietpdf.config import save_consent  # noqa: PLC0415
    from vietpdf.security.secrets import CUSTOM_PROVIDER, get_api_key  # noqa: PLC0415
    from vietpdf.translate.providers import (  # noqa: PLC0415
        PROVIDERS,
        build_translator,
        check_consent,
        consent_id,
        endpoint,
        needs_consent,
        privacy_text,
    )

    settings = load_settings()
    ps = settings.providers.get(provider)
    model = model or (ps.model if ps else "") or ("fake-1" if provider == "fake" else "")
    base_url = base_url or (ps.base_url if ps and ps.base_url else None)
    spec = PROVIDERS[provider]
    _url, host, custom = endpoint(provider, base_url)
    if custom:
        log.warning(
            "Dùng Base URL tuỳ chỉnh: %s — nội dung tài liệu sẽ được gửi tới host này; khoá API dùng khoá riêng "
            "'custom' (không dùng khoá của %s).",
            host,
            spec.display,
        )
    if needs_consent(provider, base_url) and not offline and not settings.offline:
        cid = consent_id(provider, base_url)
        if not settings.privacy_consents.get(cid):
            ok = accept_privacy
            if not ok and sys.stdin.isatty() and not quiet:
                sys.stderr.write("\n" + privacy_text(provider, base_url) + "\n")
                ok = input(f"Đồng ý gửi tài liệu tới {host}? [y/N] ").strip().lower() in ("y", "yes", "c", "có")
            if ok:
                settings.privacy_consents[cid] = True
                save_consent(cid)  # chỉ sửa đúng khoá này, giữ nguyên phần còn lại của settings.json (R1-006)
            check_consent(provider, base_url, settings.privacy_consents)
    key = None
    if spec.needs_key:
        key = get_api_key(CUSTOM_PROVIDER) if custom else get_api_key(provider)
    return build_translator(
        provider,
        model,
        api_key=key,
        base_url=base_url,
        rpm=rpm or (ps.rpm if ps else None),
        offline=offline or settings.offline,
        custom_key=custom,
    )


def _translator_from(args: argparse.Namespace) -> Any:
    return _make_translator(
        args.provider,
        args.model,
        args.base_url,
        args.rpm,
        args.offline,
        getattr(args, "quiet", False),
        getattr(args, "accept_privacy", False),
    )


def _cache() -> Any:
    from vietpdf.security.secrets import get_or_create_cache_key  # noqa: PLC0415
    from vietpdf.translate.cache import TranslationCache  # noqa: PLC0415

    key = get_or_create_cache_key()  # SecretStoreError (kho lỗi tạm/khoá hỏng) => dừng, không ghi dạng rõ
    if key is None:
        log.warning("Không có kho mật khẩu an toàn: cache bản dịch KHÔNG được mã hoá (chỉ giới hạn quyền 0600).")
    return TranslationCache(cache_dir() / "translations.sqlite3", key=key)


def _write_private(path: Path, text: str) -> None:
    """File xuất chứa nội dung tài liệu/sổ từ: ghi nguyên tử, quyền 0600 (L1-011)."""
    from vietpdf.security.fsutil import atomic_write_text  # noqa: PLC0415

    atomic_write_text(path, text, private=True)


def _print(obj: Any, as_json: bool) -> None:
    if as_json or not isinstance(obj, (dict, list)):
        print(json.dumps(obj, ensure_ascii=False, indent=2) if not isinstance(obj, str) else obj)
        return
    print(to_text(obj))


# --------------------------------------------------------------------------- dịch


def _pipeline_options(args: argparse.Namespace, editor: Any) -> Any:
    from vietpdf.core.pipeline import PipelineOptions  # noqa: PLC0415
    from vietpdf.translate.base import TranslateContext  # noqa: PLC0415

    strict = args.strict_terms or (args.provider == "ollama" and not args.no_strict_terms)
    return PipelineOptions(
        page_spec=args.pages,
        dual=args.dual,
        min_scale=args.min_scale,
        workers=args.workers,
        batch_size=args.batch_size,
        concurrency=args.concurrency,
        password=args.password,
        ctx=TranslateContext(style=args.style, domain=args.domain or "", source_lang=args.source_lang),
        ocr=not args.no_ocr,
        layout_model=args.layout_model,
        translate_tables=args.translate_tables,
        detect_domain=not args.no_domain,
        auto_terms=not args.no_auto_terms,
        strict_terms=strict,
        first_mention=not args.no_first_mention,
        gloss="off" if args.no_first_mention else args.gloss,
        study=args.study,
        use_builtin_glossary=not args.no_builtin_glossary,
        user_glossaries=[Path(g) for g in args.glossary or []],
        quality=args.quality,
        editor=editor,
        annotate_terms=args.annotate_terms,
        glossary_appendix=args.glossary_appendix,
        chapter_summaries=args.chapter_summaries,
        include_hidden=getattr(args, "translate_hidden", False),
    )


def cmd_translate(args: argparse.Namespace) -> int:
    from vietpdf.core.pipeline import run_pipeline  # noqa: PLC0415

    async def go() -> int:
        translator = _translator_from(args)
        editor = (
            _make_translator(
                args.editor_provider, args.editor_model, None, None, args.offline, True, args.accept_privacy
            )
            if args.editor_provider
            else None
        )
        cache = None if args.no_cache else _cache()
        try:
            res = await run_pipeline(
                Path(args.input),
                Path(args.output) if args.output else None,
                translator,
                _pipeline_options(args, editor),
                cache=cache,
                progress=None if args.quiet else _progress,
            )
        finally:
            await translator.aclose()
            if editor is not None:
                await editor.aclose()
            if cache:
                cache.close()
        print(
            json.dumps(
                {
                    "output": str(res.output),
                    "dual": str(res.dual) if res.dual else None,
                    "ir": str(res.ir_path) if res.ir_path else None,
                    "report": str(res.report_path) if res.report_path else None,
                    "pages": res.pages,
                    "segments": res.segments,
                    "translated": res.translated,
                    "failed": res.failed,
                    "skipped": res.skipped,
                    "study": str(res.study) if res.study else None,
                    "ocr_pages": [p + 1 for p in res.ocr_pages],
                    "scan_pages_not_translated": res.scan_pages,
                    "domains": res.domains,
                    "flags": res.flags,
                    "term_issues": len(res.term_issues),
                    "cache_hits": res.cached,
                    "requests": res.requests,
                    "seconds": res.seconds,
                    "isolation": res.isolation.get("level"),
                    "hidden_spans": sum(len(p) for p in res.hidden_spans_by_page.values()),
                    "removed_objects": res.removed_objects,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        if args.provider == "fake":
            log.warning(
                "ĐANG DÙNG BỘ DỊCH GIẢ (fake): chữ sinh ngẫu nhiên để kiểm thử, KHÔNG phải bản dịch. "
                "Hãy chọn nhà cung cấp thật, ví dụ -p gemini/openai/claude."
            )
        n_hidden = sum(len(p) for p in res.hidden_spans_by_page.values())
        if n_hidden and not args.translate_hidden:
            log.warning(
                "Chữ không nhìn thấy trong bản gốc: %d đoạn (trang %s) — KHÔNG dịch, xem báo cáo. Dịch cả phần này: "
                "--translate-hidden",
                n_hidden,
                ", ".join(str(k) for k in sorted(res.hidden_spans_by_page)[:20]),
            )
        if res.scan_pages:
            log.warning("Trang scan chưa dịch (cài OCR: pip install vietpdf-translate[ocr]): %s", res.scan_pages)
        if not args.no_library:
            from vietpdf.library.store import open_store  # noqa: PLC0415

            store = open_store()
            try:
                doc_id = _save_to_library(store, Path(args.input), res)
                log.info("Đã lưu vào thư viện (id %s). Mở lại: vietpdf library show %s", doc_id, doc_id)
            finally:
                store.close()
        return 0 if res.failed == 0 else 3

    return asyncio.run(go())


def cmd_estimate(args: argparse.Namespace) -> int:
    from vietpdf.core.pipeline import run_pipeline  # noqa: PLC0415

    async def go() -> int:
        translator = _translator_from(args)
        opts = _pipeline_options(args, None)
        opts.dry_run = True
        try:
            res = await run_pipeline(Path(args.input), None, translator, opts, progress=None)
        finally:
            await translator.aclose()
        est = dict(res.estimate)
        rpm = args.rpm or 10
        est["minutes_at_rpm"] = round(est["requests"] / rpm, 1)
        if args.price_in is not None and args.price_out is not None:
            tin, tout = est["tokens"] * 0.4, est["tokens"] * 0.6
            est["cost_estimate"] = round(tin / 1e6 * args.price_in + tout / 1e6 * args.price_out, 4)
        est["note"] = (
            "Ước tính thô (~4 ký tự/token). Giá thay đổi theo nhà cung cấp: truyền --price-in/--price-out "
            "(đơn vị tiền / 1 triệu token) để tính chi phí."
        )
        print(json.dumps(est, ensure_ascii=False, indent=2))
        return 0

    return asyncio.run(go())


def cmd_render(args: argparse.Namespace) -> int:
    from vietpdf.core.pipeline import rerender  # noqa: PLC0415

    out = asyncio.run(
        rerender(
            Path(args.input),
            Path(args.ir),
            Path(args.output),
            dual=args.dual,
            min_scale=args.min_scale,
            password=args.password,
        )
    )
    print(f"Đã dựng lại: {out}")
    return 0


def cmd_terms(args: argparse.Namespace) -> int:
    """Trích thuật ngữ + đề xuất bản dịch ra CSV để duyệt, rồi dùng lại bằng `translate --glossary`."""
    from vietpdf.core import extract  # noqa: PLC0415
    from vietpdf.core.ir import Segment  # noqa: PLC0415
    from vietpdf.core.sandbox import run_isolated  # noqa: PLC0415
    from vietpdf.core.segment import should_translate  # noqa: PLC0415
    from vietpdf.security.limits import DEFAULT_LIMITS, precheck_pdf  # noqa: PLC0415
    from vietpdf.translate.glossary import build_glossary  # noqa: PLC0415
    from vietpdf.translate.terms import extract_candidates, propose_terms  # noqa: PLC0415

    src = precheck_pdf(Path(args.input))
    info = run_isolated(extract.doc_info, str(src), args.password, timeout=DEFAULT_LIMITS.base_timeout_s)
    pages = list(range(min(info["page_count"], args.max_pages)))
    raw = run_isolated(
        extract.extract_pages, str(src), pages, args.password, None, timeout=DEFAULT_LIMITS.timeout_for(len(pages))
    )
    segs = [Segment.model_validate(s) for p in raw for s in p["segments"]]
    cands = extract_candidates([s for s in segs if should_translate(s)], limit=args.limit)

    async def go() -> list[Any]:
        t = _translator_from(args)
        try:
            return await propose_terms(
                t, cands, build_glossary([args.domain] if args.domain else [], []), args.domain or ""
            )
        finally:
            await t.aclose()

    terms = asyncio.run(go())
    out = Path(args.output)
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")  # write_text tự đổi \n theo hệ điều hành
    w.writerow(["english", "vietnamese", "ghi_chu", "chinh_sach"])
    for t in terms:
        w.writerow([t.en, t.vi, t.definition, t.policy])
    _write_private(out, buf.getvalue())
    print(
        f"Đã ghi {len(terms)} thuật ngữ vào {out}. Sửa file rồi dùng: vietpdf translate {args.input} --glossary {out}"
    )
    return 0


# --------------------------------------------------------------------------- trợ lý đọc (v1.5)


def _load_ir(args: argparse.Namespace) -> Any:
    from vietpdf.core.ir import DocumentIR  # noqa: PLC0415

    src = Path(args.input)
    ir_path = Path(args.ir) if getattr(args, "ir", None) else src.with_name(src.stem + ".ir.json")
    if not ir_path.exists():
        raise FileNotFoundError(f"Chưa có IR ({ir_path.name}). Hãy chạy `vietpdf translate {args.input}` trước.")
    return DocumentIR.model_validate_json(ir_path.read_text("utf-8"))


def cmd_assist(args: argparse.Namespace) -> int:
    from vietpdf.assistant.reader import Assistant  # noqa: PLC0415

    ir = _load_ir(args)

    async def go() -> Any:
        translator = _translator_from(args)
        cache = _cache()
        a = Assistant(ir, translator, cache)
        try:
            action = args.action
            if action == "overview":
                return await a.overview()
            if action == "ask":
                return await a.ask(" ".join(args.text))
            if action == "explain":
                text = " ".join(args.text)
                if args.segment:
                    seg = next((s for s in ir.segments() if s.id == args.segment), None)
                    if seg is None:
                        raise ValueError(f"Không có đoạn {args.segment}")
                    text = seg.plain_source()
                return await a.explain(text, kind=args.kind)
            if action == "define":
                from vietpdf.assistant.definitions import find_definitions, lookup  # noqa: PLC0415

                term = " ".join(args.text)
                found = lookup(find_definitions(ir), term)
                if found:
                    return {
                        "term": term,
                        "source": "Định nghĩa trong bài",
                        "definitions": [
                            {"definition": d.definition, "sentence": d.sentence, "page": d.page} for d in found
                        ],
                    }
                ex = await a.explain(f"Thuật ngữ: {term}", context_query=term)
                return {"term": term, "source": "Kiến thức ngoài bài", "explanation": ex}
            if action == "prereq":
                return await a.prerequisites()
            if action == "critique":
                return await a.critique()
            if action == "reproduce":
                return await a.reproduce()
            if action == "sentence":
                return await a.analyze_sentence(" ".join(args.text))
            if action == "skim":
                from vietpdf.core import render  # noqa: PLC0415
                from vietpdf.core.sandbox import run_to_outputs  # noqa: PLC0415

                items = await a.skim()
                src_pdf = Path(args.input)
                out = src_pdf.with_name(src_pdf.stem + ".skim.pdf")
                n = await asyncio.to_thread(
                    run_to_outputs,
                    render.highlight_original,
                    lambda st: (str(src_pdf), str(st[0]), items, None),
                    [out],
                    timeout=120,
                )
                return {"highlighted": n, "output": str(out), "sentences": items}
            raise ValueError(action)
        finally:
            await translator.aclose()
            cache.close()

    result = asyncio.run(go())
    if not getattr(args, "no_library", False):
        from vietpdf.library.store import open_store  # noqa: PLC0415

        store = open_store()
        try:  # lưu lịch sử trợ lý vào thư viện (mã hoá) để mở lại tài liệu là thấy
            store.add_assist(ir.source_sha256[:24], args.action, " ".join(args.text or []), _jsonable(result))
        finally:
            store.close()
    _print(result, args.json)
    return 0


def _jsonable(obj: Any) -> Any:
    return json.loads(json.dumps(obj, ensure_ascii=False, default=str))


def cmd_citations(args: argparse.Namespace) -> int:
    from vietpdf.assistant.citations import CitationLookup, citation_markers, parse_references, reference_dict  # noqa: PLC0415

    ir = _load_ir(args)
    refs = parse_references(ir)

    async def lookup() -> None:
        cl = CitationLookup()
        try:
            for r in refs[: args.limit]:
                r.lookup = await cl.lookup(r)
        finally:
            await cl.aclose()

    if args.lookup:
        log.warning("Tra cứu trực tuyến: chỉ gửi DOI/arXiv ID/tên bài tới Semantic Scholar/OpenAlex.")
        asyncio.run(lookup())
    _print({"references": [reference_dict(r) for r in refs], "markers": citation_markers(ir)[:500]}, args.json)
    return 0


def cmd_notes(args: argparse.Namespace) -> int:
    from vietpdf.assistant.export import bibtex, notes_markdown  # noqa: PLC0415
    from vietpdf.assistant.reader import Assistant  # noqa: PLC0415
    from vietpdf.library.store import open_store  # noqa: PLC0415

    ir = _load_ir(args)
    overview = None
    if args.with_overview:

        async def go() -> Any:
            t = _translator_from(args)
            c = _cache()
            try:
                return await Assistant(ir, t, c).overview()
            finally:
                await t.aclose()
                c.close()

        overview = asyncio.run(go())
    store = open_store()
    md = notes_markdown(
        ir,
        overview=overview,
        vocab=store.list_words(ir.source_sha256[:24]),
        qa=[
            {"question": h["question"], **(h["result"] if isinstance(h["result"], dict) else {})}
            for h in store.list_assist(ir.source_sha256[:24])
            if h["kind"] == "ask"
        ],
    )
    store.close()
    out = Path(args.output or Path(args.input).with_name(Path(args.input).stem + ".notes.md"))
    _write_private(out, md)
    bib = out.with_suffix(".bib")
    _write_private(bib, bibtex({"title": ir.title or ir.source_name}, ir.source_name))
    print(f"Đã ghi {out} và {bib}")
    return 0


def cmd_compare(args: argparse.Namespace) -> int:
    from vietpdf.assistant.export import comparison_matrix  # noqa: PLC0415
    from vietpdf.assistant.reader import Assistant  # noqa: PLC0415
    from vietpdf.core.ir import DocumentIR  # noqa: PLC0415

    async def go() -> list[dict[str, Any]]:
        t = _translator_from(args)
        c = _cache()
        rows = []
        try:
            for p in args.inputs:
                ir_path = Path(p).with_name(Path(p).stem + ".ir.json")
                ir = DocumentIR.model_validate_json(ir_path.read_text("utf-8"))
                rows.append({"title": ir.title or ir.source_name, "overview": await Assistant(ir, t, c).overview()})
        finally:
            await t.aclose()
            c.close()
        return rows

    csv_text, md = comparison_matrix(asyncio.run(go()))
    out = Path(args.output)
    _write_private(out, csv_text)
    _write_private(out.with_suffix(".md"), md)
    print(md)
    return 0


def _doc_id_for(args: argparse.Namespace) -> str:
    """id tài liệu trong thư viện = 24 ký tự đầu SHA-256 của file gốc (đọc từ IR nếu có)."""
    if getattr(args, "doc", None):
        return str(args.doc)
    if getattr(args, "input", None):
        try:
            return str(_load_ir(args).source_sha256[:24])
        except FileNotFoundError:
            from vietpdf.security.integrity import sha256_file  # noqa: PLC0415

            return sha256_file(Path(args.input))[:24]
    return ""


def cmd_vocab(args: argparse.Namespace) -> int:
    from vietpdf.library.store import open_store  # noqa: PLC0415

    store = open_store()
    try:
        if args.action == "add":
            return _vocab_add(args, store)
        if args.action == "list":
            words = store.list_words(args.doc or None, args.query or "")
            _print([{k: w.get(k) for k in ("id", "term", "vi", "example", "page", "reps")} for w in words], args.json)
        elif args.action == "due":
            _print(
                [{"id": w["id"], "term": w["term"], "vi": w.get("vi")} for w in store.due_words(doc_id=args.doc)],
                args.json,
            )
        elif args.action == "stats":
            _print(store.stats(args.doc), True)
        elif args.action == "review":
            return _vocab_review(args, store)
        elif args.action == "export":
            text = store.export(args.format, args.doc or None)
            out = Path(
                args.output or {"anki": "tu-vung-anki.txt", "csv": "tu-vung.csv", "md": "tu-vung.md"}[args.format]
            )
            from vietpdf.security.fsutil import atomic_write_text  # noqa: PLC0415

            atomic_write_text(out, text, private=True)
            print(f"Đã xuất {out}" + (" — mở Anki: File › Import." if args.format == "anki" else ""))
        elif args.action == "import":
            print(f"Đã nhập {store.import_csv(Path(args.term), args.doc or '')} từ.")
        elif args.action == "delete":
            store.delete_word(args.term)
            print("Đã xoá.")
    finally:
        store.close()
    return 0


def _vocab_add(args: argparse.Namespace, store: Any) -> int:
    from vietpdf.library.lookup import ai_meaning, clean_selection, find_context, local_meaning  # noqa: PLC0415
    from vietpdf.translate.glossary import build_glossary  # noqa: PLC0415
    from vietpdf.translate.terms import doc_terms_to_entries  # noqa: PLC0415

    term = clean_selection(args.term or "")
    ir = None
    if args.input:
        ir = _load_ir(args)
    doc_id = args.doc or (ir.source_sha256[:24] if ir else "")
    glossary = build_glossary(ir.domains if ir else [], doc_terms_to_entries(ir.terms) if ir else [])
    found = local_meaning(term, ir, glossary)
    sentence, sentence_vi, page = find_context(ir, term, args.page)
    vi, definition, source = args.vi or found["vi"], args.definition or found["definition"], found["source"]
    extra: dict[str, str] = {}
    if args.ai and not vi:

        async def go() -> dict[str, str]:
            t = _translator_from(args)
            try:
                return await ai_meaning(t, term, sentence)
            finally:
                await t.aclose()

        extra = asyncio.run(go())
        vi, definition, source = extra.get("vi", ""), extra.get("definition_vi", ""), extra.get("source", "")
    wid = store.add_word(
        found.get("term") or term,
        vi,
        doc_id=doc_id,
        definition=definition,
        example=args.example or sentence,
        example_vi=sentence_vi,
        page=page,
        pos=extra.get("pos", ""),
        source=source or ("người dùng" if args.vi else ""),
        note=extra.get("note", ""),
    )
    _print(
        {
            "id": wid,
            "term": term,
            "vi": vi,
            "definition": definition,
            "example": args.example or sentence,
            "nguồn": source,
        },
        args.json,
    )
    return 0


def _vocab_review(args: argparse.Namespace, store: Any) -> int:
    """Ôn tập trong terminal: thẻ EN→VI, VI→EN hoặc điền từ vào câu; tự chấm theo SM-2."""
    from vietpdf.library.review import make_card  # noqa: PLC0415

    words = store.due_words(doc_id=args.doc, limit=args.limit)
    if not words:
        print("Không có từ nào đến hạn ôn. Giỏi lắm!")
        return 0
    keys = {"1": "again", "2": "hard", "3": "good", "4": "easy"}
    for i, w in enumerate(words, 1):
        card = make_card(w, args.mode)
        print(f"\n[{i}/{len(words)}] {card['front']}")
        input("  (Enter để xem đáp án) ")
        print(f"  → {card['back']}")
        ans = ""
        while ans not in keys:
            ans = input("  Bạn nhớ thế nào? 1 Quên · 2 Khó · 3 Nhớ · 4 Dễ · q thoát: ").strip().lower()
            if ans == "q":
                return 0
        r = store.review(w["id"], keys[ans])
        print("  Sẽ hỏi lại sớm." if not r["interval_days"] else f"  Lần ôn tới: sau {r['interval_days']:g} ngày")
    print(f"\nXong {len(words)} từ.")
    return 0


def cmd_library(args: argparse.Namespace) -> int:
    from vietpdf.library.store import open_store  # noqa: PLC0415

    store = open_store()
    if not store.encrypted:
        log.warning("Không có kho mật khẩu an toàn: thư viện/sổ từ KHÔNG được mã hoá (chỉ giới hạn quyền 0600).")
    elif args.action != "migrate" and (n_plain := store.plaintext_rows()):
        log.warning(
            "Có %d bản ghi chưa mã hoá (tạo khi chưa có kho mật khẩu) đang bị bỏ qua. Nếu chúng là của bạn, chạy: "
            "vietpdf library migrate",
            n_plain,
        )
    try:
        if args.action == "migrate":
            # R1-010 (2.1.2): liệt kê và yêu cầu xác nhận — mặc định KHÔNG mã hoá lại (tránh hợp thức hoá bản ghi chèn)
            from vietpdf.textfmt import to_text  # noqa: PLC0415

            rows = store.plaintext_preview()
            if not rows:
                print("Không có bản ghi chưa mã hoá.")
                return 0
            print(f"{len(rows)} bản ghi chưa mã hoá (kiểm tra đúng là của bạn trước khi mã hoá lại):")
            for table, rid, text in rows:
                print(f"  [{table}] {rid}: {to_text(text)[:60]}")
            ok = bool(getattr(args, "yes", False))
            if not ok and sys.stdin.isatty():
                ok = input("Mã hoá lại TẤT CẢ các bản ghi trên? [y/N] ").strip().lower() in ("y", "yes", "c", "có")
            if not ok:
                print("Không mã hoá lại. Bản ghi chưa mã hoá tiếp tục bị bỏ qua. Xác nhận: --yes")
                return 1
            print(f"Đã mã hoá lại {store.migrate_plaintext()} bản ghi cũ.")
        elif args.action == "list":
            docs = store.list_documents(args.query or "")
            _print(
                [
                    {
                        "id": d["id"],
                        "title": d.get("title"),
                        "vocab": d.get("vocab"),
                        "pages": d.get("pages"),
                        "opened": time.strftime("%Y-%m-%d %H:%M", time.localtime(d["opened"])),
                    }
                    for d in docs
                ],
                args.json,
            )
        elif args.action == "show":
            _print(store.get_document(args.target) or {}, args.json)
        elif args.action == "history":
            _print(store.list_assist(args.target), args.json)
        elif args.action == "notes":
            store.update_document(args.target, notes=args.text or "")
            print("Đã lưu ghi chú.")
        elif args.action == "remove":
            store.remove_document(args.target, delete_files=not args.keep_files)
            print("Đã xoá khỏi thư viện.")
        elif args.action == "add":
            doc_id = _save_to_library(store, Path(args.target), None, copy=not args.no_copy)
            print(f"Đã lưu vào thư viện: {doc_id}")
    finally:
        store.close()
    return 0


def _save_to_library(store: Any, src: Path, res: Any, copy: bool = True) -> str:
    from vietpdf.library.store import save_translation  # noqa: PLC0415

    files = None
    if res is not None:
        files = {
            "output": res.output,
            "dual": res.dual,
            "ir": res.ir_path,
            "report": res.report_path,
            "study": res.study,
        }
    return save_translation(store, src, files, copy=copy)


def cmd_study(args: argparse.Namespace) -> int:
    from vietpdf.core import study  # noqa: PLC0415
    from vietpdf.core.sandbox import run_to_outputs  # noqa: PLC0415
    from vietpdf.library.store import open_store  # noqa: PLC0415
    from vietpdf.translate.glossary import build_glossary  # noqa: PLC0415
    from vietpdf.translate.terms import doc_terms_to_entries  # noqa: PLC0415

    ir = _load_ir(args)
    store = open_store()
    vocab = store.list_words(ir.source_sha256[:24])
    store.close()
    data = study.build_study_data(ir, build_glossary(ir.domains, doc_terms_to_entries(ir.terms)), vocab)
    out = Path(args.output or Path(args.input).with_name(Path(args.input).stem + ".study.pdf"))
    if out.resolve() == Path(args.input).resolve():
        raise ValueError("File đầu ra trùng file đầu vào")
    pages = run_to_outputs(study.render_study, lambda st: (str(st[0]), data), [out], timeout=300)
    print(f"Đã tạo bản học song ngữ {out} ({pages} trang).")
    return 0


def cmd_zotero(args: argparse.Namespace) -> int:
    from vietpdf.assistant.library import Zotero  # noqa: PLC0415

    async def go() -> Any:
        z = Zotero()
        try:
            if args.action == "list":
                return await z.items(args.query or "")
            p = await z.pdf_path(args.key)
            return {"pdf": str(p) if p else None}
        finally:
            await z.aclose()

    _print(asyncio.run(go()), args.json)
    return 0


# --------------------------------------------------------------------------- tiện ích


def cmd_models(args: argparse.Namespace) -> int:
    async def go() -> int:
        t = _make_translator(
            args.provider, args.model or "-", args.base_url, args.rpm, args.offline, True, args.accept_privacy
        )
        try:
            for m in await t.list_models():
                print(m)
        finally:
            await t.aclose()
        return 0

    return asyncio.run(go())


def cmd_key(args: argparse.Namespace) -> int:
    from vietpdf.security.secrets import delete_api_key, set_api_key  # noqa: PLC0415

    if args.action == "set":
        value = getpass.getpass(f"API key cho {args.provider} (không hiển thị khi gõ): ")
        set_api_key(args.provider, value)
        print("Đã lưu vào kho mật khẩu của hệ điều hành.")
    else:
        delete_api_key(args.provider)
        print("Đã xoá.")
    return 0


def cmd_inspect(args: argparse.Namespace) -> int:
    from vietpdf.core import extract  # noqa: PLC0415
    from vietpdf.core.ir import PageIR  # noqa: PLC0415
    from vietpdf.core.sandbox import run_isolated  # noqa: PLC0415
    from vietpdf.security.limits import DEFAULT_LIMITS, precheck_pdf  # noqa: PLC0415

    src = precheck_pdf(Path(args.input))
    info = run_isolated(extract.doc_info, str(src), None, timeout=DEFAULT_LIMITS.base_timeout_s)
    pages = list(range(min(info["page_count"], args.max_pages)))
    from vietpdf.core.layout_model import model_read_files  # noqa: PLC0415

    raw = run_isolated(
        extract.extract_pages,
        str(src),
        pages,
        None,
        args.layout_model,
        timeout=DEFAULT_LIMITS.timeout_for(len(pages)),
        read_files=[str(src), *model_read_files(args.layout_model)],
    )
    print(
        json.dumps(
            {"info": info, "pages": [PageIR.model_validate(p).model_dump() for p in raw]}, ensure_ascii=False, indent=1
        )
    )
    return 0


def cmd_cache(args: argparse.Namespace) -> int:
    c = _cache()
    try:
        if args.action == "clear":
            print(f"Đã xoá {c.clear()} mục khỏi cache.")
        else:
            print(f"{c.count()} mục trong {c.path} (mã hoá: {'có' if c.encrypted else 'không'})")
    finally:
        c.close()
    return 0


def cmd_schema(_: argparse.Namespace) -> int:
    from vietpdf.core.ir import json_schema  # noqa: PLC0415

    print(json.dumps(json_schema(), ensure_ascii=False, indent=2))
    return 0


def cmd_verify(_: argparse.Namespace) -> int:
    from vietpdf.core.fonts import FONT_FILES, default_fontbook  # noqa: PLC0415
    from vietpdf.security.integrity import verify_assets  # noqa: PLC0415

    verify_assets()
    fb = default_fontbook()
    bad = {k: m for k in FONT_FILES if (m := fb.missing_vietnamese(*k))}
    if bad:
        print(f"Font thiếu ký tự tiếng Việt: {bad}")
        return 1
    print("OK: checksum tài nguyên đúng, mọi font đủ 134 ký tự tiếng Việt có dấu.")
    return 0


def cmd_update(args: argparse.Namespace) -> int:
    from vietpdf.security.update import UpdateCheckDisabledError, check_for_update  # noqa: PLC0415

    try:
        res = asyncio.run(check_for_update(args.repo) if args.repo else check_for_update())
    except UpdateCheckDisabledError as e:
        print(str(e))
        return 2
    _print(res, True)
    if res.get("update_available"):
        print("Có bản mới. Tải từ trang phát hành và kiểm tra SHA256SUMS + `gh attestation verify` trước khi cài.")
    return 0


# --------------------------------------------------------------------------- parser


def _provider_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--provider", "-p", default=None, choices=_provider_ids())
    p.add_argument("--model", "-m", default="")
    p.add_argument("--base-url", default=None, help="Base URL tuỳ chỉnh (dịch vụ tương thích OpenAI khác)")
    p.add_argument("--rpm", type=float, default=None, help="Giới hạn request/phút")
    p.add_argument("--offline", action="store_true", help="Offline nghiêm ngặt: chỉ cho phép localhost")
    p.add_argument(
        "--accept-privacy",
        action="store_true",
        help="Đồng ý gửi nội dung tài liệu tới nhà cung cấp/host đang chọn (ghi nhớ trong settings.json)",
    )
    p.add_argument("-q", "--quiet", action="store_true")


def _translate_args(t: argparse.ArgumentParser) -> None:
    t.add_argument("input")
    _provider_args(t)
    t.add_argument("--pages", default=None, help="vd. 1-3,5 (đánh số từ 1)")
    t.add_argument("--source-lang", default="en", help="Ngôn ngữ nguồn (v2.0): en, zh, ja, ko, fr, de...")
    t.add_argument("--style", choices=["de_doc", "sat_nghia", "hanh_chinh"], default="de_doc")
    t.add_argument(
        "--quality",
        choices=["fast", "standard", "high"],
        default="standard",
        help="fast: 1 lượt; standard: biên tập đoạn bị nghi; high: biên tập mọi đoạn (~2x token)",
    )
    t.add_argument(
        "--domain", default="", help="Lĩnh vực: cntt, ai, toan_thong_ke, vat_ly, hoa, sinh_y, kinh_te, phap_ly"
    )
    t.add_argument(
        "--glossary", action="append", help="CSV english,vietnamese[,ghi_chu][,chinh_sach] (tầng người dùng)"
    )
    t.add_argument("--no-builtin-glossary", action="store_true")
    t.add_argument("--no-domain", action="store_true", help="Không gọi LLM nhận diện lĩnh vực/tóm tắt")
    t.add_argument("--no-auto-terms", action="store_true", help="Không trích/đề xuất thuật ngữ tự động")
    t.add_argument(
        "--strict-terms", action="store_true", help="Ép thuật ngữ bằng ký hiệu giữ chỗ (mặc định với Ollama)"
    )
    t.add_argument("--no-strict-terms", action="store_true")
    t.add_argument("--no-first-mention", action="store_true", help="Không kèm thuật ngữ gốc ở lần đầu")
    t.add_argument(
        "--gloss",
        choices=["off", "document", "paragraph"],
        default="document",
        help="Kèm thuật ngữ tiếng Anh trong ngoặc: lần đầu trong tài liệu / lần đầu trong MỖI đoạn (học tiếng Anh)",
    )
    t.add_argument("--study", action="store_true", help="Xuất thêm bản học song ngữ *.study.pdf (EN + VI + thuật ngữ)")
    t.add_argument("--annotate-terms", action="store_true", help="Ghi chú nổi định nghĩa thuật ngữ trong PDF")
    t.add_argument("--glossary-appendix", action="store_true", help="Thêm trang phụ lục chú giải ở cuối")
    t.add_argument("--editor-provider", default=None, choices=_provider_ids(), help="Mô hình khác cho lượt biên tập")
    t.add_argument("--editor-model", default="")
    t.add_argument("--no-ocr", action="store_true")
    t.add_argument("--layout-model", default=None, help="File DocLayout-YOLO .onnx (đã ghim SHA-256)")
    t.add_argument("--translate-tables", action="store_true", help="Dịch cả chữ trong ô bảng (v2.0)")
    t.add_argument(
        "--translate-hidden",
        action="store_true",
        help="Dịch cả chữ KHÔNG nhìn thấy trong bản gốc (bị che, trùng màu nền, quá nhỏ...). Mặc định: không dịch, "
        "chỉ liệt kê trong báo cáo — chữ ẩn thường là chỉ dẫn nhắm vào AI.",
    )
    t.add_argument("--chapter-summaries", action="store_true", help="Tóm tắt từng chương làm ngữ cảnh (v2.0)")
    t.add_argument("--min-scale", type=float, default=0.7, help="Sàn co chữ (0.3–1.0)")
    t.add_argument("--batch-size", type=int, default=20)
    t.add_argument("--concurrency", type=int, default=2)
    t.add_argument("--workers", type=int, default=2, help="Số tiến trình đọc PDF song song")
    _password_args(t)
    t.add_argument("--dual", action="store_true", help="Xuất thêm bản song ngữ")


def _password_args(p: argparse.ArgumentParser) -> None:
    """R1-003 (2.1.2): KHÔNG còn `--password` — mật khẩu trong argv lộ qua `ps`/`/proc/<pid>/cmdline` với user khác.
    Mật khẩu chỉ nhận qua nhập ẩn, stdin, hoặc biến môi trường VIETPDF_PDF_PASSWORD (bị gỡ ngay khi khởi động)."""
    p.set_defaults(password=None)
    p.add_argument("--ask-password", action="store_true", help="Nhập mật khẩu PDF ẩn (không hiện, không lưu)")
    p.add_argument("--password-stdin", action="store_true", help="Đọc mật khẩu PDF từ dòng đầu của stdin (cho script)")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="vietpdf", description="Dịch PDF sang tiếng Việt giữ nguyên bố cục + trợ lý đọc")
    ap.add_argument("--version", action="version", version=f"vietpdf {__version__}")
    ap.add_argument("-v", "--verbose", action="store_true", help="Chi tiết (gồm mức cô lập tiến trình xử lý PDF)")
    ap.add_argument(
        "--allow-weak-sandbox",
        action="store_true",
        help="Vẫn xử lý PDF khi hệ thống không cô lập được tiến trình con (vd. kernel thiếu Landlock). RỦI RO: PDF độc "
        "khai thác lỗi parser có thể đọc/ghi file của bạn. Chỉ áp cho lần chạy này.",
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    t = sub.add_parser("translate", help="Dịch một file PDF")
    _translate_args(t)
    t.add_argument("-o", "--output", default=None)
    t.add_argument("--no-cache", action="store_true")
    t.add_argument("--no-library", action="store_true", help="Không lưu tài liệu vào thư viện")
    t.set_defaults(func=cmd_translate)

    e = sub.add_parser("estimate", help="Ước tính số token, request, thời gian (và chi phí) trước khi dịch")
    _translate_args(e)
    e.add_argument("--price-in", type=float, default=None, help="Giá / 1 triệu token đầu vào")
    e.add_argument("--price-out", type=float, default=None, help="Giá / 1 triệu token đầu ra")
    e.set_defaults(func=cmd_estimate)

    r = sub.add_parser("render", help="Dựng lại PDF từ IR đã sửa tay (không gọi API)")
    r.add_argument("input")
    r.add_argument("--ir", required=True)
    r.add_argument("-o", "--output", required=True)
    r.add_argument("--dual", action="store_true")
    r.add_argument("--min-scale", type=float, default=0.7)
    _password_args(r)
    r.set_defaults(func=cmd_render)

    tm = sub.add_parser("terms", help="Trích + đề xuất thuật ngữ ra CSV để duyệt")
    tm.add_argument("input")
    _provider_args(tm)
    tm.add_argument("-o", "--output", default="thuat-ngu.csv")
    tm.add_argument("--domain", default="")
    tm.add_argument("--limit", type=int, default=60)
    tm.add_argument("--max-pages", type=int, default=50)
    _password_args(tm)
    tm.set_defaults(func=cmd_terms)

    a = sub.add_parser("assist", help="Trợ lý đọc hiểu (v1.5) trên file đã dịch")
    a.add_argument(
        "action",
        choices=["overview", "ask", "explain", "define", "prereq", "critique", "reproduce", "sentence", "skim"],
    )
    a.add_argument("input")
    a.add_argument("text", nargs="*", help="Câu hỏi / đoạn / thuật ngữ / câu cần phân tích")
    a.add_argument("--ir", default=None)
    a.add_argument("--segment", default=None, help="id đoạn trong IR (cho explain)")
    a.add_argument("--kind", choices=["paragraph", "formula", "figure"], default="paragraph")
    a.add_argument("--json", action="store_true")
    a.add_argument("--no-library", action="store_true", help="Không lưu kết quả vào lịch sử của tài liệu")
    _provider_args(a)
    a.set_defaults(func=cmd_assist)

    c = sub.add_parser("citations", help="Danh mục tham khảo + vị trí trích dẫn; --lookup để tra cứu trực tuyến")
    c.add_argument("input")
    c.add_argument("--ir", default=None)
    c.add_argument("--lookup", action="store_true", help="Tra Semantic Scholar/OpenAlex (chỉ gửi DOI/ID/tên bài)")
    c.add_argument("--limit", type=int, default=30)
    c.add_argument("--json", action="store_true")
    c.set_defaults(func=cmd_citations)

    n = sub.add_parser("notes", help="Xuất ghi chú Markdown + BibTeX")
    n.add_argument("input")
    n.add_argument("--ir", default=None)
    n.add_argument("-o", "--output", default=None)
    n.add_argument("--with-overview", action="store_true")
    _provider_args(n)
    n.set_defaults(func=cmd_notes)

    cp = sub.add_parser("compare", help="Ma trận so sánh nhiều bài (CSV + Markdown)")
    cp.add_argument("inputs", nargs="+")
    cp.add_argument("-o", "--output", default="so-sanh.csv")
    _provider_args(cp)
    cp.set_defaults(func=cmd_compare)

    v = sub.add_parser("vocab", help="Sổ từ vựng: thêm (tra nghĩa), xem, ôn tập SRS, xuất/nhập")
    v.add_argument("action", choices=["add", "list", "due", "stats", "review", "export", "import", "delete"])
    v.add_argument("term", nargs="?", default="", help="Từ/cụm (add) · file CSV (import) · id (delete)")
    v.add_argument("--input", default=None, help="PDF đã dịch: lấy câu ví dụ, thuật ngữ, gắn vào tài liệu")
    v.add_argument("--ir", default=None)
    v.add_argument("--doc", default=None, help="id tài liệu trong thư viện")
    v.add_argument("--page", type=int, default=None)
    v.add_argument("--vi")
    v.add_argument("--definition")
    v.add_argument("--example")
    v.add_argument("--ai", action="store_true", help="Không có trong bảng thuật ngữ/bài thì hỏi AI nghĩa theo ngữ cảnh")
    v.add_argument("--query", default="")
    v.add_argument("--mode", choices=["en-vi", "vi-en", "cloze"], default="en-vi")
    v.add_argument("--limit", type=int, default=30)
    v.add_argument("--format", choices=["anki", "csv", "md"], default="anki")
    v.add_argument("-o", "--output")
    v.add_argument("--json", action="store_true")
    _provider_args(v)
    v.set_defaults(func=cmd_vocab)

    lb = sub.add_parser("library", help="Thư viện tài liệu đã dịch (mở lại, ghi chú, lịch sử hỏi đáp)")
    lb.add_argument("action", choices=["list", "show", "add", "notes", "history", "remove", "migrate"])
    lb.add_argument("--yes", action="store_true", help="migrate: xác nhận mã hoá lại các bản ghi đã liệt kê")
    lb.add_argument("target", nargs="?", default="", help="id tài liệu (show/notes/history/remove) · PDF gốc (add)")
    lb.add_argument("--query", default="")
    lb.add_argument("--text", default="")
    lb.add_argument("--keep-files", action="store_true")
    lb.add_argument("--no-copy", action="store_true", help="Chỉ lưu đường dẫn, không chép file vào thư viện")
    lb.add_argument("--json", action="store_true")
    lb.set_defaults(func=cmd_library)

    st = sub.add_parser("study", help="Tạo bản học song ngữ (EN + VI + thuật ngữ + sổ từ) từ IR, không gọi API")
    st.add_argument("input")
    st.add_argument("--ir", default=None)
    st.add_argument("-o", "--output", default=None)
    st.set_defaults(func=cmd_study)

    z = sub.add_parser("zotero", help="Zotero 7 local API (chỉ đọc)")
    z.add_argument("action", choices=["list", "pdf"])
    z.add_argument("--query", default="")
    z.add_argument("--key", default="")
    z.add_argument("--json", action="store_true")
    z.set_defaults(func=cmd_zotero)

    m = sub.add_parser("models", help="Liệt kê mô hình của nhà cung cấp")
    _provider_args(m)
    m.set_defaults(func=cmd_models)

    k = sub.add_parser("key", help="Lưu/xoá API key trong kho mật khẩu hệ điều hành")
    k.add_argument("action", choices=["set", "delete"])
    k.add_argument(
        "provider",
        choices=[*(p for p in _provider_ids() if p not in ("ollama", "fake")), "custom"],
        help="'custom': khoá riêng cho Base URL tuỳ chỉnh",
    )
    k.set_defaults(func=cmd_key)

    i = sub.add_parser("inspect", help="In IR (JSON) của file PDF")
    i.add_argument("input")
    i.add_argument("--max-pages", type=int, default=3)
    i.add_argument("--layout-model", default=None)
    i.set_defaults(func=cmd_inspect)

    ca = sub.add_parser("cache", help="Xem hoặc xoá cache bản dịch")
    ca.add_argument("action", choices=["info", "clear"])
    ca.set_defaults(func=cmd_cache)

    sub.add_parser("schema", help="In JSON Schema của IR").set_defaults(func=cmd_schema)
    sub.add_parser("verify-assets", help="Kiểm tra checksum font/tài nguyên").set_defaults(func=cmd_verify)
    u = sub.add_parser("update-check", help="Kiểm tra bản mới trên GitHub (không tự tải)")
    u.add_argument("--repo", default=None)
    u.set_defaults(func=cmd_update)
    return ap


EXIT_WEAK_SANDBOX = 5  # mã thoát riêng: từ chối vì cô lập yếu (R1-002)
EXIT_CONFIG = 6  # settings.json không hợp lệ (R1-006)


def startup_hardening() -> None:
    """Chạy đầu tiên ở CLI và GUI, trước khi có tiến trình con: lấy & xoá bí mật khỏi biến môi trường, không cho
    tiến trình khác cùng người dùng đọc /proc/<pid>/environ|mem (Linux), cấu hình proxy tường minh (nếu có)."""
    from vietpdf.core.confine import harden_current_process  # noqa: PLC0415
    from vietpdf.security.http import configure_proxy  # noqa: PLC0415
    from vietpdf.security.secrets import capture_env_secrets  # noqa: PLC0415

    capture_env_secrets()
    harden_current_process()
    from vietpdf.core.sandbox import cleanup_stale_stages  # noqa: PLC0415

    with contextlib.suppress(Exception):
        cleanup_stale_stages()  # thư mục tạm của job do lần chạy bị kill để lại (R1-001)
    from vietpdf.config import ConfigError  # noqa: PLC0415

    try:
        configure_proxy(load_settings().https_proxy or None)
    except ConfigError:
        raise
    except ValueError as e:
        log.warning("Bỏ qua https_proxy trong settings.json: %s", e)


def _install_signal_handlers() -> None:
    """Ctrl+C / SIGTERM: kill ngay mọi tiến trình con rồi dừng (không chờ tiến trình con chạy hết)."""
    import os  # noqa: PLC0415
    import signal  # noqa: PLC0415

    from vietpdf.core.sandbox import cancel_all  # noqa: PLC0415

    hits = [0]

    def handler(_sig: int, _frame: Any) -> None:
        hits[0] += 1
        cancel_all(stop_new=True)
        if hits[0] > 1:  # tín hiệu thứ hai: thoát ngay, không chờ dọn dẹp
            os._exit(130)
        raise KeyboardInterrupt

    with contextlib.suppress(ValueError):  # chỉ đặt được ở luồng chính
        signal.signal(signal.SIGINT, handler)
        if hasattr(signal, "SIGTERM"):
            signal.signal(signal.SIGTERM, handler)


def _utf8_stdio() -> None:
    """Windows: stdout/stderr là pipe hoặc file thì Python dùng code page ANSI (vd. cp1252) — in tiếng Việt làm lệnh
    thoát với UnicodeEncodeError (2.1.3). Ghi UTF-8 cho trường hợp đó; console Windows vốn đã là UTF-8."""
    if sys.platform != "win32":
        return
    for stream in (sys.stdout, sys.stderr):
        enc = (getattr(stream, "encoding", None) or "").replace("-", "").lower()
        if enc != "utf8" and hasattr(stream, "reconfigure"):
            with contextlib.suppress(Exception):
                stream.reconfigure(encoding="utf-8", errors="replace")


def main(argv: list[str] | None = None) -> int:
    multiprocessing.freeze_support()  # cần cho bản đóng gói PyInstaller
    _utf8_stdio()
    args = build_parser().parse_args(argv)
    setup_logging(logging.DEBUG if args.verbose else logging.INFO)
    from vietpdf.config import ConfigError  # noqa: PLC0415

    try:
        startup_hardening()
    except ConfigError as e:
        log.error("%s", e)
        return EXIT_CONFIG
    _install_signal_handlers()
    from vietpdf.core import sandbox  # noqa: PLC0415

    if args.allow_weak_sandbox:  # lựa chọn tường minh của người dùng cho lần chạy này; KHÔNG đọc từ biến môi trường
        sandbox.set_allow_weak(True)
        log.warning("--allow-weak-sandbox: cho phép xử lý PDF kể cả khi cô lập tiến trình con yếu.")
    try:
        return _run_command(args)
    finally:
        _report_isolation(args.verbose)


def _report_isolation(verbose: bool) -> None:
    """CLI -v: in mức cô lập; cô lập yếu thì luôn cảnh báo (kể cả không -v) — R1-002."""
    from vietpdf.core.sandbox import confine_summary, describe_confine  # noqa: PLC0415

    summary = confine_summary()
    if summary.get("level") == "weak":
        log.warning("%s", describe_confine(summary))
    elif verbose and summary.get("runs"):
        log.info("%s", describe_confine(summary))


def _run_command(args: argparse.Namespace) -> int:
    from vietpdf.config import ConfigError  # noqa: PLC0415
    from vietpdf.core.sandbox import SandboxWeakError  # noqa: PLC0415

    if getattr(args, "provider", "x") is None:
        args.provider = load_settings().provider
    if getattr(args, "ask_password", False):
        args.password = getpass.getpass("Mật khẩu PDF: ")
    elif getattr(args, "password_stdin", False):
        args.password = sys.stdin.readline().rstrip("\r\n") or None
    elif hasattr(args, "password") and args.password is None:
        from vietpdf.security.secrets import take_env_pdf_password  # noqa: PLC0415

        args.password = take_env_pdf_password()
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        print("\nĐã dừng. Chạy lại cùng lệnh để tiếp tục (các đoạn đã dịch nằm trong cache).", file=sys.stderr)
        return 130
    except SandboxWeakError as e:
        log.error("%s", e)
        return EXIT_WEAK_SANDBOX
    except ConfigError as e:
        log.error("%s", e)
        return EXIT_CONFIG
    except Exception as e:
        if args.verbose:
            log.exception("Lỗi")
        else:
            log.error("%s: %s", type(e).__name__, e)
        return 1


if __name__ == "__main__":
    sys.exit(main())
