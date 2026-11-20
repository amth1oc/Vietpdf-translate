"""Điều phối dịch: cache -> chia lô -> gọi nhà cung cấp -> kiểm tra -> chia đôi lô khi lỗi."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass

from vietpdf.core.protect import STRICT_TOKEN_RE
from vietpdf.translate.base import (
    AuthenticationError,
    InvalidResponseError,
    Item,
    RetryableError,
    TranslateContext,
    Translator,
    TranslatorError,
)
from vietpdf.translate.cache import TranslationCache
from vietpdf.translate.glossary import Glossary
from vietpdf.translate.prompts import EDIT_SCHEMA, context_fingerprint, edit_messages
from vietpdf.translate.qa import sanitize_output

log = logging.getLogger(__name__)
ProgressFn = Callable[[int, int], None]


@dataclass
class EngineStats:
    cached: int = 0
    translated: int = 0
    failed: int = 0
    requests: int = 0


def _tokens_ok(source: str, translated: str) -> bool:
    return sorted(STRICT_TOKEN_RE.findall(source)) == sorted(STRICT_TOKEN_RE.findall(translated))


class TranslationEngine:
    def __init__(
        self,
        translator: Translator,
        *,
        cache: TranslationCache | None = None,
        glossary: Glossary | None = None,
        batch_size: int = 20,
        max_batch_chars: int = 6000,
        concurrency: int = 2,
    ) -> None:
        self.translator = translator
        self.cache = cache
        self.glossary = glossary or Glossary([])
        self.batch_size = max(1, batch_size)
        self.max_batch_chars = max(500, max_batch_chars)
        self.concurrency = max(1, concurrency)
        self.stats = EngineStats()

    def _batches(self, items: list[Item]) -> list[list[Item]]:
        batches: list[list[Item]] = []
        cur: list[Item] = []
        chars = 0
        for it in items:
            if cur and (len(cur) >= self.batch_size or chars + len(it.text) > self.max_batch_chars):
                batches.append(cur)
                cur, chars = [], 0
            cur.append(it)
            chars += len(it.text)
        if cur:
            batches.append(cur)
        return batches

    def _ctx_for(self, batch: list[Item], base: TranslateContext) -> TranslateContext:
        terms = {e.en.lower(): e for it in batch for e in self.glossary.match(it.text)}
        extra = {e.en.lower(): e for e in base.glossary if any(e.en.lower() in it.text.lower() for it in batch)}
        return base.subset({i.id for i in batch}, list({**extra, **terms}.values()))

    def _validate(self, batch: list[Item], result: dict[str, str]) -> tuple[dict[str, str], list[Item]]:
        ok: dict[str, str] = {}
        bad: list[Item] = []
        for it in batch:
            raw = result.get(it.id)
            clean = sanitize_output(raw, len(it.text)) if isinstance(raw, str) else None
            if clean is None or not _tokens_ok(it.text, clean):
                bad.append(it)
            else:
                ok[it.id] = clean
        return ok, bad

    async def _run_batch(self, batch: list[Item], ctx: TranslateContext, depth: int = 0) -> dict[str, str | None]:
        out: dict[str, str | None] = {}
        try:
            self.stats.requests += 1
            result = await self.translator.translate_batch(batch, self._ctx_for(batch, ctx))
            ok, bad = self._validate(batch, result)
        except AuthenticationError:
            raise
        except (InvalidResponseError, RetryableError, TranslatorError) as e:
            log.warning("Lô %d đoạn lỗi: %s", len(batch), e)
            ok, bad = {}, list(batch)
        out.update(ok)
        if not bad:
            return out
        if len(bad) == 1:
            if depth < 2 and len(batch) > 1:  # một lần thử riêng cho đoạn lỗi
                out.update(await self._run_batch(bad, ctx, depth + 1))
            else:
                out[bad[0].id] = None
            return out
        mid = len(bad) // 2
        for half in (bad[:mid], bad[mid:]):
            out.update(await self._run_batch(half, ctx, depth + 1))
        return out

    async def translate(
        self, items: list[Item], ctx: TranslateContext, progress: ProgressFn | None = None
    ) -> dict[str, str | None]:
        """Trả về id -> bản dịch (None nếu không dịch được; khi đó giữ chữ gốc)."""
        ctx_fp = context_fingerprint(ctx, self.glossary.fingerprint())
        results: dict[str, str | None] = {}
        todo = items
        keys: dict[str, str] = {}
        if self.cache is not None and not ctx.shorten_limits:
            keys = {
                it.id: TranslationCache.make_key(self.translator.provider, self.translator.model, ctx_fp, it.text)
                for it in items
            }
            hits = self.cache.get_many(list(set(keys.values())))
            todo = []
            for it in items:
                if keys[it.id] in hits:
                    results[it.id] = hits[keys[it.id]]
                else:
                    todo.append(it)
            self.stats.cached += len(items) - len(todo)
        # Trùng nội dung (đầu/chân trang lặp lại): chỉ dịch một lần.
        unique: dict[str, Item] = {}
        dup_of: dict[str, str] = {}
        for it in todo:
            if it.text in unique:
                dup_of[it.id] = unique[it.text].id
            else:
                unique[it.text] = it
        batches = self._batches(list(unique.values()))
        total = len(items)
        done = len(results)
        if progress:
            progress(done, total)
        sem = asyncio.Semaphore(self.concurrency)
        lock = asyncio.Lock()

        async def worker(batch: list[Item]) -> None:
            nonlocal done
            async with sem:
                res = await self._run_batch(batch, ctx)
            async with lock:
                results.update(res)
                good = {keys[i]: v for i, v in res.items() if v is not None and i in keys}
                if self.cache is not None and good:
                    self.cache.put_many(good)
                done += len(batch)
                if progress:
                    progress(min(done, total), total)

        await asyncio.gather(*(worker(b) for b in batches))
        for dup, orig in dup_of.items():
            results[dup] = results.get(orig)
        self.stats.translated += sum(1 for it in todo if results.get(it.id))
        self.stats.failed += sum(1 for it in todo if not results.get(it.id))
        if progress and not batches:
            progress(total, total)
        return results

    async def edit(
        self, rows: list[tuple[str, str, str]], ctx: TranslateContext, batch_size: int = 10
    ) -> dict[str, tuple[str, list[str]]]:
        """Lượt 2 (biên tập): rows = (id, bản gốc đã giữ chỗ, bản nháp). Trả về id -> (bản sửa, các thay đổi).
        Bản sửa chỉ được nhận nếu qua cùng các kiểm tra như bản dịch (ký hiệu, độ dài, ký tự điều khiển)."""
        out: dict[str, tuple[str, list[str]]] = {}
        sem = asyncio.Semaphore(self.concurrency)

        async def one(chunk: list[tuple[str, str, str]]) -> None:
            items = [Item(i, src) for i, src, _ in chunk]
            sub = self._ctx_for(items, ctx)
            payload = [{"id": i, "source": src, "draft": draft} for i, src, draft in chunk]
            async with sem:
                try:
                    self.stats.requests += 1
                    data = await self.translator.complete_json(edit_messages(payload, sub), "edits", EDIT_SCHEMA)
                except AuthenticationError:
                    raise
                except TranslatorError as e:
                    log.warning("Lượt biên tập lỗi: %s", e)
                    return
            src_of = {i: src for i, src, _ in chunk}
            for row in (data or {}).get("edits", []) if isinstance(data, dict) else []:
                if not isinstance(row, dict) or row.get("id") not in src_of or not isinstance(row.get("text"), str):
                    continue
                clean = sanitize_output(row["text"], len(src_of[row["id"]]))
                if clean and _tokens_ok(src_of[row["id"]], clean):
                    changes = [str(c)[:200] for c in row.get("changes", []) if isinstance(c, str)][:10]
                    out[row["id"]] = (clean, changes)

        await asyncio.gather(*(one(rows[i : i + batch_size]) for i in range(0, len(rows), batch_size)))
        return out
