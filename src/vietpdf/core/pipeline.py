"""Điều phối dịch một file PDF.

  kiểm tra → trích xuất (cô lập, song song) → OCR trang scan (cô lập) → thứ tự đọc, đầu/chân trang, nối đoạn
  → lĩnh vực + tóm tắt → thuật ngữ (3 tầng + trích + đề xuất) → giữ chỗ → dịch → kiểm tra (ký hiệu, nghĩa,
  thuật ngữ) → dịch lại có nhắc → biên tập (lượt 2) → kèm gốc lần đầu → cho vừa khung (xin dịch gọn)
  → ghi IR + báo cáo → dựng PDF (cô lập)

Tiến trình chính chỉ xử lý dữ liệu đã kiểm tra (IR); mọi bước đọc/ghi PDF chạy trong tiến trình con.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import json
import logging
import os
import time
from collections import Counter
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from vietpdf.core import extract, render, study
from vietpdf.core.fonts import default_fontbook
from vietpdf.core.ir import MARKER_RE, DocTerm, DocumentIR, PageIR, Region, Segment, TocEntry
from vietpdf.core.protect import PlaceholderError, Protected, protect, restore
from vietpdf.core.sandbox import (
    SandboxError,
    confine_seq,
    confine_summary,
    describe_confine,
    publish_staged,
    run_isolated,
    run_to_outputs,
    staged_outputs,
)
from vietpdf.core.segment import (
    assign_reading_order,
    link_continuations,
    mark_headers_footers,
    should_translate,
    split_translation,
)
from vietpdf.security.fsutil import atomic_write_text
from vietpdf.security.integrity import sha256_file, verify_assets
from vietpdf.security.limits import DEFAULT_LIMITS, InputLimits, precheck_pdf
from vietpdf.textfmt import md_escape
from vietpdf.translate.base import AuthenticationError, Item, Quality, TranslateContext, Translator, TranslatorError
from vietpdf.translate.cache import TranslationCache
from vietpdf.translate.engine import TranslationEngine
from vietpdf.translate.glossary import Glossary, build_glossary
from vietpdf.translate.prompts import DOMAIN_SCHEMA, DOMAINS, domain_messages, json_messages
from vietpdf.translate.qa import check_meaning, injection_in_window, injection_like, output_anomalies, sanitize_output
from vietpdf.translate.terms import (
    apply_first_mention,
    consistency_report,
    doc_terms_to_entries,
    extract_candidates,
    missing_in,
    propose_terms,
    strict_term_tokens,
)

log = logging.getLogger(__name__)
ProgressCb = Callable[[str, float, str], None]
TermReview = Callable[[list[DocTerm]], Awaitable[list[DocTerm]]]
QA_FLAGS = ("qa:", "term:missing", "security:", "ocr-layer")
SECURITY = "security:"


def _suspicious(s: Segment) -> bool:
    """Đoạn có cờ bảo mật (nghi injection, chữ mờ/bị che một phần/chồng chữ, chưa kiểm được độ hiển thị): dịch RIÊNG,
    không kèm ngữ cảnh, không làm ngữ cảnh cho đoạn khác, không gửi vào bước tóm tắt/thuật ngữ (2.1.2)."""
    return any(f.startswith(SECURITY) for f in s.flags)


@dataclass
class PipelineOptions:
    pages: list[int] | None = None  # chỉ số trang tính từ 0; None = tất cả
    page_spec: str | None = None  # vd. '1-3,5' (đánh số từ 1)
    dual: bool = False
    write_ir: bool = True
    write_report: bool = True
    min_scale: float = 0.7
    chunk_pages: int = 20
    workers: int = 2
    shorten_overflow: bool = True
    batch_size: int = 20
    concurrency: int = 2
    password: str | None = None
    limits: InputLimits = DEFAULT_LIMITS
    ctx: TranslateContext = field(default_factory=TranslateContext)
    # v1.0 — PDF khó
    ocr: bool = True
    layout_model: str | None = None
    translate_tables: bool = False
    # v1.0 — thuật ngữ & chất lượng
    detect_domain: bool = True
    auto_terms: bool = True
    strict_terms: bool = False
    first_mention: bool = True
    # Chú thích thuật ngữ gốc trong ngoặc: "document" = lần đầu trong tài liệu; "paragraph" = lần đầu trong mỗi
    # đoạn (chế độ học tiếng Anh); "off" = tắt
    gloss: str = "document"
    study: bool = False  # xuất thêm bản học song ngữ *.study.pdf
    use_builtin_glossary: bool = True
    user_glossaries: list[Path] = field(default_factory=list)
    quality: Quality = "standard"
    editor: Translator | None = None
    annotate_terms: bool = False
    glossary_appendix: bool = False
    term_review: TermReview | None = None
    # v2.0
    chapter_summaries: bool = False
    dry_run: bool = False
    # 2.1.2: người dùng chọn dịch cả chữ KHÔNG nhìn thấy trong bản gốc (vẫn gắn cờ, dịch riêng)
    include_hidden: bool = False


@dataclass
class PipelineResult:
    output: Path | None
    dual: Path | None
    ir_path: Path | None
    report_path: Path | None
    pages: int
    segments: int
    translated: int
    failed: int
    skipped: int
    scan_pages: list[int]
    ocr_pages: list[int]
    flags: dict[str, int]
    cached: int
    requests: int
    seconds: float
    domains: list[str] = field(default_factory=list)
    term_issues: list[dict[str, Any]] = field(default_factory=list)
    estimate: dict[str, Any] = field(default_factory=dict)
    study: Path | None = None
    hidden_chars: int = 0
    suspicious_meta: list[str] = field(default_factory=list)
    removed_objects: dict[str, int] = field(default_factory=dict)  # nội dung chủ động đã xoá khi dựng (R1-004)
    isolation: dict[str, Any] = field(default_factory=dict)  # mức cô lập tiến trình con của job (R1-002)
    hidden_spans_by_page: dict[int, list[str]] = field(default_factory=dict)  # trang (từ 1) -> chữ ẩn (L1-010)
    dropped: list[str] = field(default_factory=list)  # trang/đoạn bị bỏ do dữ liệu không hợp lệ (L1-012)


def parse_page_ranges(spec: str | None, page_count: int) -> list[int] | None:
    """'1-3,5' (đánh số từ 1) -> [0,1,2,4]."""
    if not spec:
        return None
    pages: set[int] = set()
    for part in spec.replace(" ", "").split(","):
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            start, end = int(a or 1), int(b or page_count)
        else:
            start = end = int(part)
        if start < 1 or end < start:
            raise ValueError(f"Phạm vi trang không hợp lệ: {part!r}")
        pages.update(range(start - 1, min(end, page_count)))
    return sorted(pages)


def default_output_paths(src: Path) -> tuple[Path, Path, Path, Path]:
    stem = src.with_suffix("")
    n = stem.name
    return (
        stem.with_name(n + ".vi.pdf"),
        stem.with_name(n + ".dual.pdf"),
        stem.with_name(n + ".ir.json"),
        stem.with_name(n + ".report.md"),
    )


def _sibling_paths(output: Path) -> tuple[Path, Path, Path]:
    base = output.stem.removesuffix(".vi")
    return (
        output.with_name(base + ".dual.pdf"),
        output.with_name(base + ".ir.json"),
        output.with_name(base + ".report.md"),
    )


def _result_cap_mb(n_pages: int) -> int:
    """Trần kích thước kết quả từ tiến trình con theo loại việc (R1-007): trích xuất ~ ≤ 6 MB/trang (200 000 ký tự
    chữ + khung + vật cản ở mức giới hạn của InputLimits, JSON) + 8 MB; không vượt 512 MB như trước."""
    return min(512, 8 + 6 * max(1, n_pages))


def _clamp_bbox(seg: Segment, page: PageIR) -> None:
    x0, y0, x1, y1 = seg.bbox
    seg.bbox = (max(0.0, x0), max(0.0, y0), min(page.width, x1), min(page.height, y1))


def _add_flags(seg: Segment, *flags: str) -> None:
    seg.flags = sorted(set(seg.flags) | {f for f in flags if f})[:50]


def _write_atomic(path: Path, text: str) -> None:
    atomic_write_text(path, text)


def _safe_page_ir(raw: Any, drops: list[str] | None = None) -> PageIR:
    """Kiểm tra lại dữ liệu trang từ tiến trình con. Phần tử lỗi (toạ độ vô lý, kiểu sai...) bị bỏ riêng lẻ thay vì
    làm hỏng cả tài liệu (L1-012); trang có kích thước vô lý thành trang rỗng được đánh dấu bỏ qua."""
    try:
        return PageIR.model_validate(raw)
    except ValidationError:
        pass
    if not isinstance(raw, dict):
        raise ValueError("Dữ liệu trang từ tiến trình con sai định dạng")
    number = int(raw.get("number", 0))
    try:
        base = PageIR.model_validate({**raw, "segments": [], "obstacles": [], "regions": []})
    except ValidationError:
        log.warning("Trang %d có kích thước không hợp lệ: bỏ qua trang", number + 1)
        if drops is not None:
            drops.append(f"Trang {number + 1}: kích thước/khung trang không hợp lệ — bỏ cả trang, không dịch")
        return PageIR(number=max(number, 0), width=1, height=1, is_scan=False)
    segs = []
    for item in raw.get("segments") or []:
        try:
            segs.append(Segment.model_validate(item))
        except ValidationError:
            log.warning("Trang %d: bỏ một đoạn có dữ liệu không hợp lệ", number + 1)
            if drops is not None:
                drops.append(f"Trang {number + 1}: bỏ một đoạn có dữ liệu không hợp lệ (toạ độ/kiểu sai)")
    obstacles = [o for o in raw.get("obstacles") or [] if _valid_bbox(o)]
    regions = []
    for r in raw.get("regions") or []:
        with contextlib.suppress(ValidationError):
            regions.append(Region.model_validate(r))
    return base.model_copy(update={"segments": segs, "obstacles": obstacles, "regions": regions})


def _safe_toc(raw: Any) -> list[TocEntry]:
    out = []
    for t in raw if isinstance(raw, list) else []:
        with contextlib.suppress(ValidationError, TypeError):
            out.append(TocEntry.model_validate(t))
    return out[:20000]


def _valid_bbox(b: Any) -> bool:
    try:
        PageIR(number=0, width=1, height=1, obstacles=[b])
    except (ValidationError, TypeError):
        return False
    return True


async def _extract(
    src: Path, pages: list[int], opts: PipelineOptions, report: Callable[..., None], drops: list[str]
) -> list[PageIR]:
    chunks = [pages[i : i + opts.chunk_pages] for i in range(0, len(pages), opts.chunk_pages)]

    def extract_chunk(chunk: list[int]) -> list[PageIR]:
        from vietpdf.core.layout_model import model_read_files  # noqa: PLC0415

        raw = run_isolated(
            extract.extract_pages,
            str(src),
            chunk,
            opts.password,
            opts.layout_model,
            opts.include_hidden,
            timeout=opts.limits.timeout_for(len(chunk)),
            memory_mb=opts.limits.memory_mb,
            max_result_mb=_result_cap_mb(len(chunk)),
            read_files=[str(src), *model_read_files(opts.layout_model)],
        )
        return [_safe_page_ir(p, drops) for p in raw]  # dữ liệu từ tiến trình con: kiểm tra lại

    out: list[PageIR] = []
    loop = asyncio.get_running_loop()
    with ThreadPoolExecutor(max_workers=max(1, min(opts.workers, os.cpu_count() or 1))) as pool:
        futs = [loop.run_in_executor(pool, extract_chunk, c) for c in chunks]
        for done, fut in enumerate(asyncio.as_completed(futs), 1):
            out.extend(await fut)
            report("extract", done / len(chunks), f"{len(out)}/{len(pages)} trang")
    out.sort(key=lambda p: p.number)
    return out


async def _ocr(src: Path, page_irs: list[PageIR], opts: PipelineOptions, report: Callable[..., None]) -> list[int]:
    from vietpdf.core import ocr  # noqa: PLC0415

    scan = [p.number for p in page_irs if p.is_scan]
    if not scan or not opts.ocr:
        return []
    if not await asyncio.to_thread(run_isolated, ocr.available, timeout=60, max_result_mb=1):
        log.warning("Có %d trang scan nhưng chưa cài OCR (pip install vietpdf-translate[ocr])", len(scan))
        return []
    by_no = {p.number: i for i, p in enumerate(page_irs)}
    done: list[int] = []
    for i in range(0, len(scan), 5):
        chunk = scan[i : i + 5]
        report("ocr", i / len(scan), f"OCR trang {chunk[0] + 1}")
        raw = await asyncio.to_thread(
            run_isolated,
            ocr.ocr_pages,
            str(src),
            chunk,
            opts.password,
            timeout=120 + 40 * len(chunk),
            memory_mb=max(opts.limits.memory_mb, 3072),
            max_result_mb=_result_cap_mb(len(chunk)),
        )
        for p in raw:
            pir = _safe_page_ir(p)
            if pir.number in by_no:
                page_irs[by_no[pir.number]] = pir
                done.append(pir.number)
    return done


async def _cached_json(
    cache: TranslationCache | None, translator: Translator, task: str, payload: str, call: Callable[[], Awaitable[Any]]
) -> Any:
    """Tính một lần, dùng nhiều lần: kết quả tác vụ JSON (lĩnh vực, thuật ngữ...) được lưu trong cache."""
    key = TranslationCache.make_key(translator.provider, translator.model, f"json:{task}", payload)
    if cache is not None:
        hit = cache.get_json(key)
        if hit is not None:
            return hit
    data = await call()
    if cache is not None and data is not None:
        cache.put_json(key, data)
    return data


async def _detect_domain(
    translator: Translator, ir: DocumentIR, ctx: TranslateContext, cache: TranslationCache | None = None
) -> None:
    text = "\n".join(s.plain_source() for s in ir.segments() if s.role in ("title", "body") and not _suspicious(s))[
        :12000
    ]
    msgs = domain_messages(ir.title, text, ctx.target_lang)
    try:
        data = await _cached_json(
            cache,
            translator,
            "domain",
            msgs[1]["content"],
            lambda: translator.complete_json(msgs, "domain", DOMAIN_SCHEMA),
        )
    except AuthenticationError:
        raise
    except TranslatorError as e:
        log.warning("Không nhận diện được lĩnh vực: %s", e)
        return
    if not isinstance(data, dict):
        return
    ir.domains = [d for d in data.get("domains", []) if d in DOMAINS][:2]
    summary = sanitize_output(str(data.get("summary", "")), 1500)
    if summary and injection_like(summary):
        # tóm tắt do LLM sinh được gửi kèm MỌI lô dịch: không để chỉ dẫn lọt vào ngữ cảnh chung (L1-010)
        log.warning("Tóm tắt tài liệu trông như chứa chỉ dẫn cho AI: không dùng làm ngữ cảnh")
        summary = None
    ir.summary = (summary or "")[:4000]


async def _chapter_summaries(translator: Translator, ir: DocumentIR) -> dict[int, str]:
    """v2.0: tóm tắt từng chương (mục lục cấp 1) làm ngữ cảnh cho các lô dịch của chương đó."""
    starts = sorted({t.page - 1 for t in ir.toc if t.level == 1 and t.page >= 1})
    if len(starts) < 2:
        return {}
    schema = {
        "type": "object",
        "properties": {"summary": {"type": "string"}},
        "required": ["summary"],
        "additionalProperties": False,
    }
    out: dict[int, str] = {}
    for i, start in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else ir.page_count
        text = " ".join(s.plain_source() for s in ir.segments() if start <= s.page < end and not _suspicious(s))[:6000]
        if len(text) < 200:
            continue
        msgs = json_messages(
            "Tóm tắt chương sau trong 3–4 câu tiếng Việt để làm ngữ cảnh cho người dịch.",
            '{"summary": "..."}',
            {"chapter": text},
        )
        try:
            data = await translator.complete_json(msgs, "chapter_summary", schema)
        except TranslatorError:
            continue
        summary = sanitize_output(str((data or {}).get("summary", "")), 1200) if isinstance(data, dict) else None
        if summary and injection_like(summary):
            summary = None
        if summary:
            out[start] = summary
            ir.chapter_summaries[str(start + 1)] = summary[:2000]
    return out


async def _terms_json(translator: Translator, candidates: Any, glossary: Glossary, ctx: TranslateContext) -> Any:
    terms = await propose_terms(translator, candidates, glossary, ctx.domain, ctx.target_lang)
    return [t.model_dump() for t in terms]


_FLAG_LEGEND = " ".join(
    [
        "Ý nghĩa cờ: `qa:negation` có thể lật nghĩa phủ định · `qa:number` sai/thiếu số liệu ·",
        "`qa:direction` sai chiều tăng/giảm · `qa:modality` sai sắc thái (có thể/phải/nên) ·",
        "`qa:untranslated` còn sót tiếng gốc · `qa:length` dài/ngắn bất thường ·",
        "`term:missing` thiếu thuật ngữ chuẩn · `overflow` chữ phải co quá nhỏ ·",
        "`security:injection-like` đoạn PDF trông như lệnh cho AI · `security:low-contrast` chữ rất mờ ·",
        "`security:partly-hidden` một phần dòng bị che · `security:overlapping-text` chữ khác nhau chồng lên nhau ·",
        "`security:hidden-text` chữ không nhìn thấy (đã dịch theo lựa chọn) ·",
        "`security:output-*` bản dịch của đoạn nghi ngờ có dấu hiệu lạ · `ocr-layer` lớp chữ OCR có sẵn.",
        "Đoạn có cờ `security:` được dịch RIÊNG, không kèm ngữ cảnh.",
    ]
)


def _report_markdown(ir: DocumentIR, res: PipelineResult) -> str:
    lines = [
        f"# Báo cáo dịch: {md_escape(ir.source_name)}",
        "",
        f"- Trang: {res.pages} · Đoạn: {res.segments} · Đã dịch: {res.translated} · Lỗi: {res.failed} · "
        f"Bỏ qua: {res.skipped}",
        f"- Lĩnh vực: {', '.join(ir.domains) or 'không rõ'} · Request: {res.requests} · Cache: {res.cached} · "
        f"Thời gian: {res.seconds}s",
    ]
    if res.isolation:
        lines.append(f"- {md_escape(describe_confine(res.isolation))}")
    if res.removed_objects:
        lines.append(
            "- Đã bỏ khi dựng PDF (không hỗ trợ trong bản dịch): "
            + ", ".join(f"{n} {md_escape(k)}" for k, n in sorted(res.removed_objects.items()))
        )
    if res.dropped:
        lines.append("- **Phần bị bỏ do dữ liệu PDF không hợp lệ** (không dịch): " + "; ".join(res.dropped[:50]))
    if res.ocr_pages:
        lines.append(f"- Trang đã OCR: {', '.join(str(p + 1) for p in res.ocr_pages)}")
    if res.scan_pages:
        lines.append(f"- Trang scan CHƯA dịch (thiếu OCR): {', '.join(map(str, res.scan_pages))}")
    hidden = [(p.number, h) for p in ir.pages for h in p.hidden_spans]
    if res.hidden_chars or hidden:
        pages = [str(p.number + 1) for p in ir.pages if p.hidden_chars or p.hidden_spans]
        lines.append(
            f"- **Chữ không nhìn thấy trong bản gốc**: {len(hidden)} đoạn, {res.hidden_chars} ký tự ở trang "
            f"{', '.join(pages[:50])} — "
            + (
                "ĐÃ DỊCH theo lựa chọn của bạn (gắn cờ `security:hidden-text`)."
                if any("security:hidden-text" in s.flags for s in ir.segments())
                else "KHÔNG gửi đi dịch (có thể là chỉ dẫn ẩn nhắm vào AI). Xem danh sách bên dưới; muốn dịch cả "
                "phần này: `--translate-hidden` (CLI) hoặc ô 'Dịch cả chữ không nhìn thấy' (GUI)."
            )
        )
    ocr_layer = sorted({s.page + 1 for s in ir.segments() if "ocr-layer" in s.flags})
    if ocr_layer:
        lines.append(
            "- Trang dùng lại lớp chữ OCR có sẵn (chữ vô hình đặt trên ảnh scan — không đối chiếu được bằng mắt): "
            + ", ".join(map(str, ocr_layer[:50]))
        )
    if res.suspicious_meta:
        lines.append(
            "- **Tiêu đề/mục lục trông như chỉ dẫn cho AI** (không dùng làm ngữ cảnh, không dịch): "
            + "; ".join(res.suspicious_meta)
        )
    if ir.summary:
        lines += ["", "## Tóm tắt (của AI)", "", md_escape(ir.summary)]
    flagged = [s for s in ir.segments() if any(f.startswith(QA_FLAGS) or f == "overflow" for f in s.flags)]
    if flagged:
        lines += ["", "## Đoạn cần xem lại", "", "| Trang | Đoạn | Cờ | Bản gốc (rút gọn) |", "|---|---|---|---|"]
        for s in flagged[:500]:
            src = md_escape(s.plain_source()[:90])
            flags = ", ".join(f for f in s.flags if f.startswith(QA_FLAGS) or f == "overflow")
            lines.append(f"| {s.page + 1} | {s.id} | {flags} | {src} |")
    if hidden:
        lines += [
            "",
            "## Chữ không nhìn thấy trong bản gốc",
            "",
            "| Trang | Vị trí (x, y) pt | Lý do | Nội dung (80 ký tự đầu) |",
            "|---|---|---|---|",
        ]
        for page_no, h in hidden[:500]:
            lines.append(
                f"| {page_no + 1} | {h.bbox[0]:.0f}, {h.bbox[1]:.0f} | {md_escape(h.reason)} | "
                f"{md_escape(h.text[:80])} |"
            )
    if res.term_issues:
        lines += [
            "",
            "## Thuật ngữ chưa nhất quán",
            "",
            "| Thuật ngữ | Bản dịch chuẩn | Lần xuất hiện | Trang thiếu |",
            "|---|---|---|---|",
        ]
        for t in res.term_issues:
            lines.append(
                f"| {md_escape(t['term'])} | {md_escape(t['expected'])} | {t['occurrences']} | "
                f"{', '.join(map(str, t['pages_missing']))} |"
            )
    lines += [
        "",
        _FLAG_LEGEND,
    ]
    return "\n".join(lines) + "\n"


async def run_pipeline(
    input_path: Path,
    output_path: Path | None,
    translator: Translator,
    opts: PipelineOptions | None = None,
    *,
    cache: TranslationCache | None = None,
    glossary: Glossary | None = None,
    progress: ProgressCb | None = None,
) -> PipelineResult:
    opts = opts or PipelineOptions()
    t0 = time.monotonic()
    seq0 = confine_seq()
    drops: list[str] = []  # phần bị bỏ vì dữ liệu từ tiến trình con không hợp lệ (L1-012)

    def report(stage: str, frac: float, msg: str = "") -> None:
        if progress:
            progress(stage, max(0.0, min(frac, 1.0)), msg)

    # 1. Kiểm tra
    report("check", 0, "Kiểm tra tài nguyên và file đầu vào")
    verify_assets()
    src = precheck_pdf(input_path, opts.limits)
    out_pdf, dual_path, ir_path, report_path = default_output_paths(src)
    if output_path is not None:
        out_pdf = output_path
        dual_path, ir_path, report_path = _sibling_paths(output_path)
    if out_pdf.resolve() == src:
        raise ValueError("File đầu ra trùng file đầu vào")
    digest = await asyncio.to_thread(sha256_file, src)
    from vietpdf.core.layout_model import model_read_files  # noqa: PLC0415

    first = await asyncio.to_thread(
        run_isolated,
        extract.info_and_pages,
        str(src),
        opts.password,
        opts.layout_model,
        opts.include_hidden,
        opts.chunk_pages,
        timeout=opts.limits.timeout_for(opts.chunk_pages),
        memory_mb=opts.limits.memory_mb,
        read_files=[str(src), *model_read_files(opts.layout_model)],
        max_result_mb=_result_cap_mb(opts.chunk_pages),
    )
    if not isinstance(first, dict) or not isinstance(first.get("info"), dict):
        raise ValueError("Kết quả từ tiến trình con sai định dạng")
    info = first["info"]
    page_count = int(info["page_count"])
    if opts.page_spec:
        pages = parse_page_ranges(opts.page_spec, page_count) or []
    else:
        pages = opts.pages if opts.pages is not None else list(range(page_count))
    pages = [p for p in pages if 0 <= p < page_count]
    if not pages:
        raise ValueError("Không có trang nào để dịch")

    # 2–3. Trích xuất + OCR
    report("extract", 0, f"Đọc {len(pages)} trang")
    if isinstance(first.get("pages"), list):  # tài liệu ngắn: đã trích xuất cùng lúc với doc_info
        wanted = set(pages)
        page_irs = sorted(
            (_safe_page_ir(p, drops) for p in first["pages"] if isinstance(p, dict) and p.get("number") in wanted),
            key=lambda p: p.number,
        )
        report("extract", 1, f"{len(page_irs)}/{len(pages)} trang")
    else:
        page_irs = await _extract(src, pages, opts, report, drops)
    ocr_done = await _ocr(src, page_irs, opts, report)
    ctx = opts.ctx
    ir = DocumentIR(
        source_name=src.name[:500],
        source_sha256=digest,
        page_count=page_count,
        title=str(info.get("title", ""))[:2000],
        source_lang=ctx.source_lang,
        target_lang=ctx.target_lang,
        pages=page_irs,
        toc=_safe_toc(info.get("toc", [])),
        provider=str(getattr(translator, "provider", ""))[:40],
    )
    mark_headers_footers(ir.pages)
    for page in ir.pages:
        for seg in page.segments:
            _clamp_bbox(seg, page)
        assign_reading_order(page)
    chains = [c for c in link_continuations(ir.pages) if all(should_translate(s, opts.translate_tables) for s in c)]
    for s in ir.segments():
        if s.continues and not any(s in c[:-1] for c in chains):
            s.continues = None
    _flag_injections(ir, chains, opts.translate_tables)
    suspicious_meta = [t for t in [ir.title, *(t.title for t in ir.toc)] if t and injection_like(t)]
    if not ctx.title:
        title_ok = ir.title if ir.title and not injection_like(ir.title) else ""
        ctx.title = (
            title_ok
            or next(
                (
                    s.plain_source()
                    for s in ir.segments()[:5]
                    if len(s.source) > 10 and not injection_like(s.source) and not _suspicious(s)
                ),
                "",
            )[:300]
        )

    # 4. Lĩnh vực, tóm tắt, thuật ngữ
    if not opts.dry_run:
        if ctx.domain:
            ir.domains = [ctx.domain] if ctx.domain in DOMAINS else []
        if opts.detect_domain:
            report("terms", 0, "Nhận diện lĩnh vực")
            await _detect_domain(translator, ir, ctx, cache)
            if ctx.domain and ctx.domain in DOMAINS:
                ir.domains = [ctx.domain]
            ctx.domain = ctx.domain or ", ".join(ir.domains)
            ctx.summary = ctx.summary or ir.summary
    if glossary is None:
        glossary = build_glossary(ir.domains, [], opts.user_glossaries, opts.use_builtin_glossary)
    if opts.auto_terms and not opts.dry_run:
        report("terms", 0.4, "Trích thuật ngữ")
        candidates = extract_candidates(
            [s for s in ir.segments() if should_translate(s, opts.translate_tables) and not _suspicious(s)]
        )
        terms_key = json.dumps([[c.term, c.count] for c in candidates] + [ctx.domain, glossary.fingerprint()])
        cached_terms = await _cached_json(
            cache,
            translator,
            "terms",
            terms_key,
            functools.partial(_terms_json, translator, candidates, glossary, ctx),
        )
        ir.terms = [DocTerm.model_validate(t) for t in cached_terms or []]
        if opts.term_review is not None:
            ir.terms = await opts.term_review(ir.terms)
        doc_entries = doc_terms_to_entries([t for t in ir.terms if t.source != "glossary"])
        # nạp lại theo đúng thứ tự tầng: chuyên ngành < tài liệu < người dùng
        glossary = build_glossary(ir.domains, doc_entries, opts.user_glossaries, opts.use_builtin_glossary)

    keep_terms = {e.en for e in glossary.entries if e.policy == "keep"}

    # 5. Giữ chỗ, gom chuỗi đoạn nối tiếp
    segs = {s.id: s for s in ir.segments()}
    chain_of: dict[str, list[Segment]] = {c[0].id: c for c in chains}
    in_chain = {s.id for c in chains for s in c[1:]}
    protected: dict[str, Protected] = {}
    items: list[Item] = []
    isolated: list[Item] = []  # đoạn có cờ bảo mật: dịch riêng từng đoạn, không ngữ cảnh (2.1.2)
    ordered = [s for p in ir.pages for s in sorted(p.segments, key=lambda x: x.order)]
    prev_text = ""
    for seg in ordered:
        if not should_translate(seg, opts.translate_tables):
            seg.status = "skipped"
            continue
        if seg.id in in_chain:
            continue
        chain = chain_of.get(seg.id, [seg])
        source = " ".join(s.source for s in chain)
        tokens = strict_term_tokens(source, glossary, opts.strict_terms)
        pr = protect(source, tokens)
        protected[seg.id] = pr
        if any(_suspicious(s) for s in chain):
            isolated.append(Item(seg.id, pr.text))
            continue  # không làm ngữ cảnh "đoạn trước" cho đoạn kế tiếp
        items.append(Item(seg.id, pr.text))
        if prev_text:
            ctx.previous[seg.id] = prev_text[-400:]
        prev_text = seg.plain_source()
    # tiêu đề mục lục trông như chỉ dẫn cho AI: không gửi đi dịch (giữ nguyên tiếng gốc), ghi vào báo cáo
    toc_items = [
        Item(f"toc-{i}", t.title)
        for i, t in enumerate(ir.toc)
        if len(t.title.strip()) >= 2 and not injection_like(t.title)
    ]

    engine = TranslationEngine(
        translator, cache=cache, glossary=glossary, batch_size=opts.batch_size, concurrency=opts.concurrency
    )
    if opts.dry_run:
        all_items = items + isolated + toc_items
        est_tokens = translator.estimate_tokens(all_items)
        scan_left = [p.number + 1 for p in ir.pages if p.is_scan and p.number not in ocr_done]
        n_batches = max(1, -(-len(all_items) // opts.batch_size))
        extra = {"fast": 0.0, "standard": 0.25, "high": 1.0}[opts.quality]
        est: dict[str, Any] = {
            "segments": len(items) + len(isolated),
            "isolated": len(isolated),
            "toc": len(toc_items),
            "tokens": int(est_tokens * (1 + extra)) + 3000,
            "requests": int(n_batches * (1 + extra)) + 2,
            "pages": len(pages),
            "scan_pages": scan_left,
        }
        return PipelineResult(
            None,
            None,
            None,
            None,
            len(pages),
            len(ir.segments()),
            0,
            0,
            0,
            scan_left,
            ocr_done,
            {},
            0,
            0,
            round(time.monotonic() - t0, 2),
            ir.domains,
            [],
            est,
        )

    # 6. Dịch (theo chương nếu bật tóm tắt chương)
    report("translate", 0, f"Dịch {len(items)} đoạn")
    chapter_ctx: dict[int, str] = await _chapter_summaries(translator, ir) if opts.chapter_summaries else {}
    results: dict[str, str | None] = {}
    groups: list[tuple[TranslateContext, list[Item]]] = []
    if chapter_ctx:
        starts = sorted(chapter_ctx)
        buckets: dict[int, list[Item]] = {s: [] for s in [-1, *starts]}
        for it in items:
            seg_page = segs[it.id].page
            bucket = max([st for st in starts if st <= seg_page], default=-1)
            buckets[bucket].append(it)
        for bucket, its in buckets.items():
            sub = TranslateContext(**{**ctx.__dict__})
            if bucket in chapter_ctx:
                sub.summary = f"{ctx.summary}\nChương hiện tại: {chapter_ctx[bucket]}".strip()
            groups.append((sub, its))
        groups.append((ctx, toc_items))
    else:
        groups = [(ctx, items + toc_items)]
    total = sum(len(g[1]) for g in groups) or 1
    done_before = 0
    for gctx, its in groups:
        base = done_before

        def on_progress(d: int, _t: int, b: int = base) -> None:
            report("translate", (b + d) / total, f"{b + d}/{total} đoạn")

        res = await engine.translate(its, gctx, progress=on_progress)
        results.update(res)
        done_before += len(its)
    if isolated:
        report("translate", 1, f"Dịch riêng {len(isolated)} đoạn có cờ bảo mật (không kèm ngữ cảnh)")
        iso_engine = TranslationEngine(translator, cache=cache, batch_size=1, concurrency=opts.concurrency)
        iso_ctx = TranslateContext(source_lang=ctx.source_lang, target_lang=ctx.target_lang, style=ctx.style)
        results.update(await iso_engine.translate(isolated, iso_ctx))
        engine.stats.requests += iso_engine.stats.requests
        engine.stats.cached += iso_engine.stats.cached

    # 7. Đặt lại giữ chỗ, chia chuỗi, kiểm tra nghĩa & thuật ngữ
    def apply(seg_id: str, translated: str, extra_flags: tuple[str, ...] = ()) -> bool:
        try:
            text = restore(translated, protected[seg_id])
        except PlaceholderError:
            return False
        chain = chain_of.get(seg_id, [segs[seg_id]])
        parts = split_translation(text, [len(s.source) for s in chain])
        for s, part in zip(chain, parts, strict=True):
            s.translation = part
            s.status = "translated"
        src_plain, tr_plain = unit_text(seg_id)
        # kiểm tra trên cả chuỗi, không trên từng mảnh đã chia
        qa = check_meaning(src_plain, tr_plain, ctx.source_lang, keep_terms)
        for s in chain:
            _add_flags(s, *extra_flags, *qa, "joined" if len(chain) > 1 else "")
        return True

    def unit_text(seg_id: str) -> tuple[str, str]:
        chain = chain_of.get(seg_id, [segs[seg_id]])
        return (" ".join(s.plain_source() for s in chain), " ".join(s.plain_translation() for s in chain))

    for it in items + isolated:
        tr = results.get(it.id)
        if not tr or not apply(it.id, tr):
            segs[it.id].status = "failed"
    for it in isolated:  # kiểm đầu ra của đoạn nghi injection: độ dài, URL/email/mã mới, đúng ngôn ngữ đích
        if segs[it.id].status == "translated":
            anomalies = output_anomalies(*unit_text(it.id), ctx.target_lang)
            for s in chain_of.get(it.id, [segs[it.id]]):
                _add_flags(s, *anomalies)
    for i, t in enumerate(ir.toc):
        tr = results.get(f"toc-{i}")
        t.translation = sanitize_output(tr, len(t.title)) if tr else None

    # Thiếu thuật ngữ chuẩn / còn sót tiếng gốc -> dịch lại một lần với lời nhắc cụ thể
    retry: dict[str, str] = {}
    for it in items:
        s = segs[it.id]
        if s.status != "translated":
            continue
        notes = []
        miss = missing_in(*unit_text(it.id), glossary)
        if miss:
            notes.append(
                "BẮT BUỘC dùng thuật ngữ: "
                + "; ".join(f"{e.en} → {e.en if e.policy == 'keep' else e.vi}" for e in miss)
            )
        if "qa:untranslated" in s.flags:
            notes.append(
                "Bản dịch trước còn để nguyên từ tiếng gốc: hãy dịch đầy đủ (chỉ giữ tên riêng, viết tắt, mã nguồn)."
            )
        if notes:
            retry[it.id] = " ".join(notes)
    if retry:
        report("translate", 1, f"Dịch lại {len(retry)} đoạn (thiếu thuật ngữ / sót tiếng gốc)")
        rctx = TranslateContext(**{**ctx.__dict__})
        rctx.notes = retry
        again = await engine.translate([Item(k, protected[k].text) for k in retry], rctx)
        for k, tr in again.items():
            if not tr:
                continue
            chain = chain_of.get(k, [segs[k]])
            saved = [(c.translation, list(c.flags)) for c in chain]
            for c in chain:
                c.flags = [f for f in c.flags if not f.startswith("qa:")]
            if not apply(k, tr):
                for c, (tr0, fl0) in zip(chain, saved, strict=True):
                    c.translation, c.flags = tr0, fl0
        for k in retry:
            if missing_in(*unit_text(k), glossary):
                for s in chain_of.get(k, [segs[k]]):
                    _add_flags(s, "term:missing")

    # 8. Lượt 2: biên tập
    if opts.quality != "fast":

        def needs_edit(s: Segment) -> bool:
            return opts.quality == "high" or any(f.startswith(QA_FLAGS) for f in s.flags)

        rows: list[tuple[str, str, str]] = []
        for it in items:
            s = segs[it.id]
            draft = _re_protect(s, protected[it.id], chain_of) if s.status == "translated" and needs_edit(s) else None
            if draft:
                rows.append((it.id, protected[it.id].text, draft))
        if rows:
            report("edit", 0, f"Biên tập {len(rows)} đoạn")
            editor = TranslationEngine(opts.editor, glossary=glossary) if opts.editor else engine
            edited = await editor.edit(rows, ctx)
            for sid, (text, changes) in edited.items():
                chain = chain_of.get(sid, [segs[sid]])
                saved = [(s.translation, list(s.flags)) for s in chain]
                for s in chain:  # cờ kiểm tra được tính lại trên bản đã biên tập
                    s.flags = [f for f in s.flags if not f.startswith("qa:")]
                if not apply(sid, text, ("edited",) if changes else ()):
                    for s, (tr, fl) in zip(chain, saved, strict=True):
                        s.translation, s.flags = tr, fl

    # 9. Kèm thuật ngữ gốc ở lần đầu
    if opts.first_mention and opts.gloss != "off":
        apply_first_mention(ordered, glossary, scope=opts.gloss)

    # 10. Cho vừa khung: đoạn nào phải co dưới sàn thì xin dịch gọn hơn (một lần)
    report("fit", 0, "Dàn chữ")
    fonts = default_fontbook()
    pages_by_no = {p.number: p for p in ir.pages}
    formulas = ir.formula_index()

    def overflowing() -> dict[str, tuple[float, int]]:
        out: dict[str, tuple[float, int]] = {}
        for s in ir.segments():
            if s.status != "translated" or not s.translation or s.id not in protected:
                continue
            if s.id in chain_of and len(chain_of[s.id]) > 1:
                continue  # chuỗi nối: không xin dịch gọn từng phần
            if _suspicious(s):
                continue  # đoạn có cờ bảo mật: không gửi lại kèm ngữ cảnh
            lay = render.segment_layout(s, pages_by_no[s.page], fonts, opts.min_scale, formulas)
            if lay.needs_review:
                out[s.id] = (lay.scale, len(s.translation))
        return out

    over = overflowing()
    if over and opts.shorten_overflow:
        limits = {sid: max(10, int(n * (sc / opts.min_scale) ** 2 * 0.95)) for sid, (sc, n) in over.items()}
        short_ctx = TranslateContext(
            source_lang=ctx.source_lang,
            target_lang=ctx.target_lang,
            title=ctx.title,
            domain=ctx.domain,
            style=ctx.style,
            shorten_limits=limits,
        )
        report("fit", 0.3, f"Xin dịch gọn {len(over)} đoạn")
        shorter = await engine.translate([Item(sid, protected[sid].text) for sid in over], short_ctx)
        for sid, tr in shorter.items():
            seg = segs[sid]
            if not tr or not seg.translation:
                continue
            try:
                candidate = restore(tr, protected[sid])
            except PlaceholderError:
                continue
            if len(candidate) < len(seg.translation):
                seg.translation = candidate
                _add_flags(
                    seg,
                    "shortened",
                    *check_meaning(seg.plain_source(), seg.plain_translation(), ctx.source_lang, keep_terms),
                )
        over = overflowing()
    for sid in over:
        _add_flags(segs[sid], "overflow")

    # 11. Ghi chú thuật ngữ, phụ lục
    annotations: list[dict[str, Any]] = []
    appendix: list[list[str]] = []
    defs = {t.en.lower(): t for t in ir.terms}
    seen: set[str] = set()
    for s in ordered:
        if s.status != "translated":
            continue
        for e in glossary.match(s.plain_source()):
            term_key = e.en.lower()
            if term_key in seen:
                continue
            seen.add(term_key)
            dt = defs.get(term_key)
            definition = (dt.definition if dt else "") or e.note
            label = "Giải thích của AI" if dt is not None and dt.source == "llm" else "Theo bảng thuật ngữ"
            vi = e.en if e.policy == "keep" else e.vi
            appendix.append([e.en, vi, f"{definition} ({label})" if definition else ""])
            if opts.annotate_terms and definition:
                annotations.append(
                    {
                        "page": s.page,
                        "x": s.bbox[2] + 2,
                        "y": s.bbox[1],
                        "title": f"{e.en} → {vi}",
                        "content": f"{e.en} → {vi}\n{definition}\n— {label}",
                    }
                )

    # 12. Ghi IR, báo cáo, dựng PDF
    all_segs = ir.segments()
    flag_counts: Counter[str] = Counter(f for s in all_segs for f in s.flags)
    result = PipelineResult(
        output=out_pdf,
        dual=dual_path if opts.dual else None,
        ir_path=ir_path if opts.write_ir else None,
        report_path=report_path if opts.write_report else None,
        pages=len(pages),
        segments=len(all_segs),
        translated=sum(s.status == "translated" for s in all_segs),
        failed=sum(s.status == "failed" for s in all_segs),
        skipped=sum(s.status == "skipped" for s in all_segs),
        scan_pages=[p.number + 1 for p in ir.pages if p.is_scan and not p.ocr],
        ocr_pages=ocr_done,
        flags=dict(flag_counts),
        cached=engine.stats.cached,
        requests=engine.stats.requests,
        seconds=0.0,
        domains=ir.domains,
        hidden_chars=sum(p.hidden_chars for p in ir.pages),
        dropped=drops[:200],
        hidden_spans_by_page={p.number + 1: [h.text[:80] for h in p.hidden_spans] for p in ir.pages if p.hidden_spans},
        suspicious_meta=[md_escape(t[:120]) for t in suspicious_meta][:20],
        term_issues=consistency_report(
            [(segs[it.id].page, *unit_text(it.id)) for it in items + isolated if segs[it.id].status == "translated"],
            glossary,
        ),
    )
    if opts.write_ir:
        _write_atomic(ir_path, ir.model_dump_json(indent=1))
    report("render", 0, "Dựng PDF")
    render_opts = {
        "min_scale": opts.min_scale,
        "password": opts.password,
        "annotations": annotations,
        "appendix": appendix if opts.glossary_appendix else [],
        "appendix_title": "Phụ lục: Chú giải thuật ngữ",
    }
    render_report = await asyncio.to_thread(
        _render_published,
        src,
        out_pdf,
        dual_path if opts.dual else None,
        ir.model_dump_json(),
        render_opts,
        opts.limits.timeout_for(len(pages)) * 2,
        opts.limits.memory_mb,
    )
    result.removed_objects = _removed_counts(render_report)
    if opts.study:
        report("render", 0.8, "Bản học song ngữ")
        study_path = report_path.with_name(report_path.name.removesuffix(".report.md") + ".study.pdf")
        data = study.build_study_data(ir, glossary)
        await asyncio.to_thread(
            run_to_outputs, study.render_study, lambda st: (str(st[0]), data), [study_path], timeout=300
        )
        result.study = study_path
    result.seconds = round(time.monotonic() - t0, 2)
    result.isolation = confine_summary(seq0)
    if opts.write_report:
        _write_atomic(report_path, _report_markdown(ir, result))
        _write_atomic(
            report_path.with_suffix(".json"),
            json.dumps(
                {
                    "flags": result.flags,
                    "term_issues": result.term_issues,
                    "segments": [
                        {"id": s.id, "page": s.page + 1, "flags": s.flags, "status": s.status}
                        for s in all_segs
                        if s.flags
                    ],
                },
                ensure_ascii=False,
                indent=1,
            ),
        )
    report("render", 1, "Xong")
    return result


def _flag_injections(ir: DocumentIR, chains: list[list[Segment]], translate_tables: bool) -> None:
    """Gắn `security:injection-like` trên văn bản đã chuẩn hoá, từng đơn vị dịch và cửa sổ trượt 2–3 đơn vị liền nhau
    (câu lệnh bị tách qua nhiều đoạn — B8). Chạy TRƯỚC bước tóm tắt/thuật ngữ để đoạn nghi ngờ không vào ngữ cảnh."""
    chain_of = {c[0].id: c for c in chains}
    in_chain = {s.id for c in chains for s in c[1:]}
    units: list[list[Segment]] = []
    for p in ir.pages:
        for seg in sorted(p.segments, key=lambda x: x.order):
            if seg.id in in_chain or not should_translate(seg, translate_tables):
                continue
            units.append(chain_of.get(seg.id, [seg]))
    texts = [MARKER_RE.sub("", " ".join(s.source for s in u)) for u in units]
    hit = [injection_like(t) for t in texts]
    for i, u in enumerate(units):
        if hit[i]:
            for s in u:
                _add_flags(s, "security:injection-like")
    for size in (2, 3):
        for i in range(len(units) - size + 1):
            if any(hit[i : i + size]) and size > 2:
                continue  # đơn vị đã khớp riêng: chỉ kéo theo đoạn liền kề (lời dẫn kiểu "Note to the" + "AI: ...")
            if injection_in_window(texts[i : i + size]):
                for u in units[i : i + size]:
                    for s in u:
                        _add_flags(s, "security:injection-like")


def _render_published(
    src: Path,
    out_pdf: Path,
    dual_path: Path | None,
    ir_json: str,
    options: dict[str, Any],
    timeout: float,
    memory_mb: int,
) -> dict[str, Any]:
    """Dựng PDF trong tiến trình con: chỉ ghi vào thư mục tạm riêng của job; tiến trình chính kiểm (quét nội dung chủ
    động trong một tiến trình con mới chỉ đọc) rồi mới chuyển sang đích (R1-001).

    Chốt kiểm cuối còn thấy nội dung chủ động (R1-004): làm sạch lại MỘT lần (chế độ nghiêm) trong tiến trình con rồi
    quét lại; chỉ từ chối nếu vẫn còn. Không dịch lại, không gọi API — bản dịch đã nằm trong IR/cache."""
    finals = [out_pdf, dual_path]
    with staged_outputs(*finals) as (stage, staged):
        s_out, s_dual = staged
        rep = run_isolated(
            render.render_document,
            str(src),
            str(s_out),
            ir_json,
            {**options, "dual_path": str(s_dual) if s_dual else None},
            timeout=timeout,
            memory_mb=memory_mb,
            write_dirs=[str(stage)],
            max_result_mb=4,  # chỉ là báo cáo nhỏ
        )
        rep = rep if isinstance(rep, dict) else {}
        removed = Counter(_removed_counts(rep))
        for i, (st, fin) in enumerate(zip(staged, finals, strict=True)):
            if fin is None or st is None:
                continue
            try:
                publish_staged(st, fin)
            except SandboxError as e:
                if "ActiveContentError" not in str(e):
                    raise
                log.warning("PDF đầu ra còn nội dung chủ động sau khi dựng: làm sạch lại một lần (%s)", fin.name)
                st2 = stage / f"rescrub{i}.pdf"
                extra = run_isolated(
                    render.rescrub_pdf, str(st), str(st2), timeout=timeout, write_dirs=[str(stage)], max_result_mb=1
                )
                removed.update(_removed_counts({"removed": extra}))
                removed["làm sạch lại sau chốt kiểm cuối"] += 1
                publish_staged(st2, fin)  # vẫn còn => SandboxError: từ chối (fail-closed)
        rep["removed"] = dict(removed)
    return rep


def _removed_counts(rep: dict[str, Any]) -> dict[str, int]:
    raw = rep.get("removed") if isinstance(rep, dict) else None
    if not isinstance(raw, dict):
        return {}
    return {str(k)[:80]: int(v) for k, v in list(raw.items())[:50] if isinstance(v, int) and v > 0}


def _re_protect(seg: Segment, pr: Protected, chain_of: dict[str, list[Segment]]) -> str | None:
    """Đưa bản dịch đã đặt lại giữ chỗ về dạng có ký hiệu (để biên tập viên giữ nguyên chúng)."""
    chain = chain_of.get(seg.id, [seg])
    text = " ".join(s.translation or "" for s in chain).strip()
    if not text:
        return None
    for token, original in sorted(pr.mapping.items(), key=lambda kv: -len(kv[1])):
        if original and original in text:
            text = text.replace(original, token, 1)
        else:
            return None  # không ánh xạ ngược được: bỏ qua biên tập đoạn này
    return text


async def rerender(
    input_path: Path,
    ir_path: Path,
    output_path: Path,
    *,
    dual: bool = False,
    min_scale: float = 0.7,
    password: str | None = None,
) -> Path:
    """Dựng lại PDF từ IR đã sửa tay — không gọi API."""
    verify_assets()
    src = precheck_pdf(input_path)
    ir = DocumentIR.model_validate_json(ir_path.read_text("utf-8"))
    if ir.source_sha256 != await asyncio.to_thread(sha256_file, src):
        raise ValueError("IR không khớp file PDF gốc (SHA-256 khác)")
    if output_path.resolve() == src:
        raise ValueError("File đầu ra trùng file đầu vào")
    dual_path, _, _ = _sibling_paths(output_path)
    await asyncio.to_thread(
        _render_published,
        src,
        output_path,
        dual_path if dual else None,
        ir.model_dump_json(),
        {"min_scale": min_scale, "password": password},
        DEFAULT_LIMITS.timeout_for(len(ir.pages)) * 2,
        DEFAULT_LIMITS.memory_mb,
    )
    return output_path
