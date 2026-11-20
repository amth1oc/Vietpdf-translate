"""Adapter chung cho mọi API kiểu OpenAI Chat Completions (Gemini, OpenAI, Ollama, ...).

Không dùng SDK `openai`: mọi request phải đi qua SafeHttpClient để allowlist host không bị vượt qua.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from vietpdf.security.http import HttpResponse, NetworkError, SafeHttpClient, safe_json_loads
from vietpdf.translate.base import (
    AuthenticationError,
    InvalidResponseError,
    Item,
    RateLimitedError,
    TransientError,
    TranslateContext,
    TranslatorError,
    estimate_tokens_simple,
)
from vietpdf.translate.prompts import RESPONSE_SCHEMA, build_messages
from vietpdf.translate.ratelimit import RetryPolicy, TokenBucket, parse_retry_after, with_retry

log = logging.getLogger(__name__)
_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


def _raise_for_status(resp: HttpResponse) -> None:
    s = resp.status
    if s < 300:
        return
    detail = resp.text(500)
    if s in (401, 403):
        raise AuthenticationError(f"API key không hợp lệ hoặc không có quyền (HTTP {s})")
    if s == 429:
        raise RateLimitedError(
            f"Vượt hạn mức (HTTP 429): {detail}", retry_after=parse_retry_after(resp.headers.get("retry-after"))
        )
    if s >= 500 or s == 408:
        raise TransientError(f"Lỗi máy chủ (HTTP {s})", retry_after=parse_retry_after(resp.headers.get("retry-after")))
    if 300 <= s < 400:
        raise TranslatorError(f"Máy chủ chuyển hướng (HTTP {s}) — bị chặn vì lý do bảo mật")
    raise TranslatorError(f"HTTP {s}: {detail}")


def parse_json_content(content: str) -> Any:
    content = _THINK_RE.sub("", content).strip()
    content = _FENCE_RE.sub("", content).strip()
    if len(content) > 4_000_000:
        raise InvalidResponseError("Phản hồi quá lớn")
    try:
        return safe_json_loads(content)
    except json.JSONDecodeError as e:
        raise InvalidResponseError("Phản hồi không phải JSON hợp lệ") from e


def parse_translations(content: str) -> dict[str, str]:
    data = parse_json_content(content)
    if isinstance(data, dict):
        data = data.get("translations")
    if not isinstance(data, list):
        raise InvalidResponseError("Thiếu trường 'translations'")
    out: dict[str, str] = {}
    for row in data:
        if isinstance(row, dict) and isinstance(row.get("id"), str) and isinstance(row.get("text"), str):
            out[row["id"]] = row["text"]
    return out


class OpenAICompatTranslator:
    def __init__(
        self,
        *,
        provider: str,
        base_url: str,
        model: str,
        http: SafeHttpClient,
        api_key: str | None = None,
        rpm: float = 10,
        temperature: float = 0.2,
        retry: RetryPolicy | None = None,
        extra_headers: dict[str, str] | None = None,
        json_mode: str = "json_schema",
        key_header: str = "bearer",
    ) -> None:
        self.provider = provider
        self.model = model
        self.base_url = base_url.rstrip("/")
        self._http = http
        self._api_key = api_key
        self._limiter = TokenBucket(rpm)
        self._retry = retry or RetryPolicy()
        self._temperature = temperature
        self._extra_headers = extra_headers or {}
        self._json_mode = json_mode
        self._key_header = key_header

    def _headers(self) -> dict[str, str]:
        h = {"Content-Type": "application/json", **self._extra_headers}
        if self._api_key:
            h["Authorization"] = f"Bearer {self._api_key}"
            if self._key_header == "x-api-key":
                h["x-api-key"] = self._api_key
        return h

    async def _call(self, method: str, path: str, body: Any = None) -> Any:
        async def once() -> Any:
            try:
                resp = await self._http.request(method, self.base_url + path, headers=self._headers(), json_body=body)
            except NetworkError as e:
                raise TransientError(str(e)) from e
            _raise_for_status(resp)
            try:
                return resp.json()
            except (json.JSONDecodeError, UnicodeDecodeError) as e:
                raise InvalidResponseError("Máy chủ trả về dữ liệu không phải JSON") from e

        return await with_retry(once, self._retry, self._limiter)

    async def list_models(self) -> list[str]:
        data = await self._call("GET", "/models")
        models = data.get("data", []) if isinstance(data, dict) else []
        ids = [m.get("id", "") for m in models if isinstance(m, dict)]
        return sorted({i.removeprefix("models/") for i in ids if isinstance(i, str) and i})

    def _response_format(self, name: str, schema: dict[str, Any]) -> dict[str, Any] | None:
        if self._json_mode == "json_schema":
            return {"type": "json_schema", "json_schema": {"name": name, "strict": True, "schema": schema}}
        if self._json_mode == "json_object":
            return {"type": "json_object"}
        return None

    async def _chat_json(self, messages: list[dict[str, str]], name: str, schema: dict[str, Any]) -> str:
        if self._json_mode != "json_schema":
            # máy chủ không ép schema: nêu schema ngay trong chỉ dẫn hệ thống
            hint = "\nĐỊNH DẠNG ĐẦU RA (JSON Schema): " + json.dumps(schema, ensure_ascii=False)
            messages = [{**messages[0], "content": messages[0]["content"] + hint}, *messages[1:]]
        body: dict[str, Any] = {"model": self.model, "messages": messages, "temperature": self._temperature}
        fmt = self._response_format(name, schema)
        if fmt is not None:
            body["response_format"] = fmt
        try:
            data = await self._call("POST", "/chat/completions", body)
        except TranslatorError as e:
            # Một số máy chủ tương thích chưa hỗ trợ json_schema -> lùi về json_object một lần.
            msg = str(e).lower()
            if self._json_mode == "json_schema" and ("response_format" in msg or "json_schema" in msg):
                log.info("Máy chủ/mô hình không hỗ trợ json_schema, chuyển sang json_object")
                self._json_mode = "json_object"
                return await self._chat_json(messages, name, schema)
            else:
                raise
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as e:
            raise InvalidResponseError("Phản hồi thiếu choices[0].message.content") from e
        if not isinstance(content, str):
            raise InvalidResponseError("Nội dung phản hồi không phải chuỗi")
        return content

    async def translate_batch(self, items: list[Item], ctx: TranslateContext) -> dict[str, str]:
        return parse_translations(await self._chat_json(build_messages(items, ctx), "translations", RESPONSE_SCHEMA))

    async def complete_json(self, messages: list[dict[str, str]], schema_name: str, schema: dict[str, Any]) -> Any:
        return parse_json_content(await self._chat_json(messages, schema_name, schema))

    def estimate_tokens(self, items: list[Item]) -> int:
        return estimate_tokens_simple(items)

    async def aclose(self) -> None:
        await self._http.aclose()
