"""Kết nối Zotero 7 (local API, chỉ đọc, chỉ localhost). Sổ từ vựng: xem vietpdf/library/store.py."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from vietpdf.security.http import NetworkError, NetworkPolicy, SafeHttpClient

ZOTERO_URL = "http://127.0.0.1:23119/api/users/0"


class Zotero:
    """Zotero 7 local API (bật trong Zotero: Settings > Advanced > Allow other applications...). Chỉ đọc."""

    def __init__(self, transport: Any = None) -> None:
        self._http = SafeHttpClient(NetworkPolicy.create(["127.0.0.1"]), timeout=15.0, transport=transport)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def items(self, query: str = "", limit: int = 50) -> list[dict[str, Any]]:
        url = f"{ZOTERO_URL}/items/top?format=json&limit={max(1, min(limit, 100))}"
        if query:
            from urllib.parse import quote  # noqa: PLC0415

            url += f"&q={quote(query[:200])}"
        try:
            resp = await self._http.get(url)
        except NetworkError as e:
            raise NetworkError("Không kết nối được Zotero (Zotero 7 đang chạy và đã bật local API?)") from e
        if resp.status != 200:
            raise NetworkError(f"Zotero trả về HTTP {resp.status}")
        out = []
        for it in resp.json() if isinstance(resp.json(), list) else []:
            d = it.get("data", {}) if isinstance(it, dict) else {}
            out.append(
                {
                    "key": str(d.get("key", ""))[:20],
                    "title": str(d.get("title", ""))[:500],
                    "year": str(d.get("date", ""))[:20],
                    "doi": str(d.get("DOI", ""))[:200],
                    "creators": [
                        f"{c.get('firstName', '')} {c.get('lastName', '')}".strip()
                        for c in d.get("creators", [])[:10]
                        if isinstance(c, dict)
                    ],
                }
            )
        return out

    async def pdf_path(self, item_key: str) -> Path | None:
        """Đường dẫn file PDF đính kèm của một mục (file cục bộ do Zotero quản lý)."""
        if not item_key.isalnum():
            raise ValueError("Khoá mục Zotero không hợp lệ")
        resp = await self._http.get(f"{ZOTERO_URL}/items/{item_key}/children?format=json")
        if resp.status != 200:
            return None
        for child in resp.json() if isinstance(resp.json(), list) else []:
            d = child.get("data", {}) if isinstance(child, dict) else {}
            if d.get("contentType") == "application/pdf":
                r = await self._http.get(f"{ZOTERO_URL}/items/{d.get('key')}/file/view/url")
                if r.status == 200:
                    u = urlparse(r.text(4000).strip())
                    if u.scheme == "file":
                        p = Path(unquote(u.path))
                        return p if p.suffix.lower() == ".pdf" and p.is_file() else None
        return None
