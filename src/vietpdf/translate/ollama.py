"""Tiện ích riêng cho Ollama: kiểm tra server, liệt kê và tải mô hình.

Tải mô hình qua HTTP API (`POST /api/pull`), KHÔNG gọi lệnh `ollama pull` (tránh command injection và
phụ thuộc PATH). Tên mô hình được kiểm tra bằng regex chặt.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from typing import Any

from vietpdf.security.http import NetworkError, NetworkPolicy, SafeHttpClient
from vietpdf.translate.providers import PROVIDERS, host_of

MODEL_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._\-/]{0,100}(?::[a-zA-Z0-9._\-]{1,60})?$")


def root_url(base_url: str | None = None) -> str:
    url = (base_url or PROVIDERS["ollama"].base_url).rstrip("/")
    return url.removesuffix("/v1")


def _client(base_url: str | None) -> SafeHttpClient:
    root = root_url(base_url)
    return SafeHttpClient(NetworkPolicy.create([host_of(root)]), timeout=30.0)


async def server_running(base_url: str | None = None) -> bool:
    async with _client(base_url) as http:
        try:
            resp = await http.get(root_url(base_url) + "/api/version")
        except NetworkError:
            return False
        return resp.status == 200


async def local_models(base_url: str | None = None) -> list[str]:
    async with _client(base_url) as http:
        resp = await http.get(root_url(base_url) + "/api/tags")
        if resp.status != 200:
            raise NetworkError(f"Ollama trả về HTTP {resp.status}")
        data = resp.json()
        return sorted(m["name"] for m in data.get("models", []) if isinstance(m, dict) and "name" in m)


async def pull_model(name: str, base_url: str | None = None) -> AsyncIterator[dict[str, Any]]:
    if not MODEL_NAME_RE.match(name):
        raise ValueError(f"Tên mô hình không hợp lệ: {name!r}")
    http = SafeHttpClient(NetworkPolicy.create([host_of(root_url(base_url))]), timeout=3600.0)
    try:
        async for event in http.stream_json_lines(
            "POST", root_url(base_url) + "/api/pull", json_body={"model": name, "stream": True}
        ):
            if isinstance(event, dict):
                yield event
    finally:
        await http.aclose()
