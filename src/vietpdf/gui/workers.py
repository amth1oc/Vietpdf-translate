"""Luồng nền cho GUI: chạy asyncio trong QThread, giao diện không bao giờ đứng hình.

Duyệt thuật ngữ cần hỏi người dùng giữa chừng: luồng nền phát tín hiệu sang luồng giao diện rồi chờ
(threading.Event) tới khi người dùng bấm Áp dụng / Dùng đề xuất."""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, Signal

from vietpdf.config import ProviderSettings, add_history, cache_dir, load_settings
from vietpdf.security.logsafe import redact
from vietpdf.translate.providers import PROVIDERS


def make_translator(provider: str, model: str, offline: bool, require_consent: bool = True) -> Any:
    """`require_consent`: nội dung tài liệu sẽ được gửi đi => phải có đồng ý đã lưu (GUI hỏi bằng confirm_privacy
    trước khi gọi). Liệt kê mô hình / ước tính không gửi tài liệu nên không cần."""
    from vietpdf.security.secrets import CUSTOM_PROVIDER, get_api_key  # noqa: PLC0415
    from vietpdf.translate.providers import build_translator, check_consent, endpoint  # noqa: PLC0415

    settings = load_settings()
    ps = settings.providers.get(provider) or ProviderSettings()
    base_url = ps.base_url or None
    _url, _host, custom = endpoint(provider, base_url)
    if require_consent and not offline and not settings.offline:
        check_consent(provider, base_url, settings.privacy_consents)
    key = None
    if PROVIDERS[provider].needs_key:
        key = get_api_key(CUSTOM_PROVIDER) if custom else get_api_key(provider)
    return build_translator(
        provider, model, api_key=key, offline=offline, base_url=base_url, rpm=ps.rpm, custom_key=custom
    )


def make_cache() -> Any:
    from vietpdf.security.secrets import get_or_create_cache_key  # noqa: PLC0415
    from vietpdf.translate.cache import TranslationCache  # noqa: PLC0415

    return TranslationCache(cache_dir() / "translations.sqlite3", key=get_or_create_cache_key())


class TranslateWorker(QObject):
    progress = Signal(str, float, str)
    file_done = Signal(str, str)
    saved = Signal(object)  # dict đường dẫn kết quả → luồng giao diện lưu vào thư viện
    failed = Signal(str, str)
    review_terms = Signal(object)  # list[dict]
    finished = Signal()

    def __init__(self, files: list[Path], provider: str, model: str, options: dict[str, Any]) -> None:
        super().__init__()
        self.files, self.provider, self.model, self.options = files, provider, model, options
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task[Any] | None = None
        self._review_event = threading.Event()
        self._review_result: list[dict[str, Any]] | None = None

    def cancel(self) -> None:
        if self._loop and self._task:
            self._loop.call_soon_threadsafe(self._task.cancel)
        self._review_event.set()

    def submit_review(self, terms: list[dict[str, Any]]) -> None:
        self._review_result = terms
        self._review_event.set()

    def run(self) -> None:
        try:
            asyncio.run(self._main())
        finally:
            self.finished.emit()

    async def _term_review(self, terms: list[Any]) -> list[Any]:
        from vietpdf.core.ir import DocTerm  # noqa: PLC0415

        self._review_event.clear()
        self._review_result = None
        self.review_terms.emit([t.model_dump() for t in terms])
        await asyncio.to_thread(self._review_event.wait)
        if self._review_result is None:
            return list(terms)
        return [DocTerm.model_validate(t) for t in self._review_result]

    async def _main(self) -> None:
        from vietpdf.core.pipeline import PipelineOptions, run_pipeline  # noqa: PLC0415
        from vietpdf.translate.base import TranslateContext  # noqa: PLC0415

        self._loop = asyncio.get_running_loop()
        self._task = asyncio.current_task()
        o = self.options
        translator = make_translator(self.provider, self.model, o.get("offline", False))
        cache = make_cache()
        try:
            for f in self.files:
                try:
                    opts = PipelineOptions(
                        page_spec=o.get("pages") or None,
                        dual=o.get("dual", False),
                        min_scale=load_settings().min_scale,
                        quality=o.get("quality", "standard"),
                        ctx=TranslateContext(style=o.get("style", "de_doc"), source_lang=o.get("source_lang", "en")),
                        annotate_terms=o.get("annotate_terms", False),
                        glossary_appendix=o.get("appendix", False),
                        translate_tables=o.get("translate_tables", False),
                        include_hidden=o.get("include_hidden", False),
                        ocr=o.get("ocr", True),
                        chapter_summaries=o.get("chapter_summaries", False),
                        strict_terms=self.provider == "ollama",
                        term_review=self._term_review if o.get("review_terms") else None,
                        gloss=o.get("gloss", "document"),
                        study=o.get("study", False),
                    )
                    res = await run_pipeline(f, None, translator, opts, cache=cache, progress=self.progress.emit)
                    add_history(
                        {
                            "input": str(f),
                            "output": str(res.output),
                            "ir": str(res.ir_path or ""),
                            "report": str(res.report_path or ""),
                            "time": time.strftime("%Y-%m-%d %H:%M"),
                            "provider": self.provider,
                            "model": self.model,
                        }
                    )
                    self.saved.emit(
                        {
                            "input": str(f),
                            "library": bool(o.get("library", True)),
                            **{
                                k: str(v) if v else ""
                                for k, v in (
                                    ("output", res.output),
                                    ("dual", res.dual),
                                    ("ir", res.ir_path),
                                    ("report", res.report_path),
                                    ("study", res.study),
                                )
                            },
                        }
                    )
                    self.file_done.emit(
                        str(f),
                        f"{res.output.name if res.output else ''}: {res.translated} đoạn đã "
                        f"dịch, {res.failed} lỗi, cần xem lại: {sum(res.flags.values())}",
                    )
                    n_hidden = sum(len(v) for v in res.hidden_spans_by_page.values())
                    if n_hidden:
                        pages = ", ".join(str(k) for k in sorted(res.hidden_spans_by_page)[:20])
                        verb = "ĐÃ dịch (theo lựa chọn)" if o.get("include_hidden") else "KHÔNG dịch"
                        self.file_done.emit(
                            str(f),
                            f"⚠ Chữ không nhìn thấy trong bản gốc: {n_hidden} đoạn (trang {pages}) — {verb}. Xem "
                            f"danh sách trong báo cáo {res.report_path.name if res.report_path else ''}; muốn dịch cả "
                            "phần này: bật 'Dịch cả chữ không nhìn thấy' rồi dịch lại.",
                        )
                except asyncio.CancelledError:
                    self.failed.emit(str(f), "Đã dừng (phần đã dịch được lưu trong cache)")
                    return
                except Exception as e:
                    self.failed.emit(str(f), redact(f"{type(e).__name__}: {e}"))
        finally:
            await translator.aclose()
            cache.close()


class AsyncJob(QObject):
    """Chạy một coroutine bất kỳ trong luồng nền và trả kết quả về luồng giao diện."""

    done = Signal(object)
    error = Signal(str)
    finished = Signal()

    def __init__(self, factory: Callable[[], Awaitable[Any]]) -> None:
        super().__init__()
        self._factory = factory

    def run(self) -> None:
        try:
            self.done.emit(asyncio.run(self._factory()))  # type: ignore[arg-type]
        except Exception as e:
            self.error.emit(redact(f"{type(e).__name__}: {e}"))
        finally:
            self.finished.emit()
