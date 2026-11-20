"""Thẻ trích dẫn: tách danh mục tham khảo, ánh xạ [12] trong thân bài, tra cứu tuỳ chọn.

Tra cứu (Semantic Scholar, dự phòng OpenAlex) là TUỲ CHỌN, mặc định tắt. Chỉ gửi định danh (DOI, arXiv ID)
hoặc tên bài — KHÔNG BAO GIỜ gửi nội dung PDF. Hai host này chỉ được thêm vào allowlist khi người dùng bật.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import asdict, dataclass, field
from typing import Any
from urllib.parse import quote

from vietpdf.core.ir import DocumentIR
from vietpdf.security.http import NetworkError, NetworkPolicy, SafeHttpClient

S2_HOST = "api.semanticscholar.org"
OPENALEX_HOST = "api.openalex.org"
LOOKUP_HOSTS = (S2_HOST, OPENALEX_HOST)
_REF_HEAD = re.compile(r"^\s*(\d+\.?\s*)?(references|bibliography|literature cited|tài liệu tham khảo)\s*$", re.I)
_ENTRY_NUM = re.compile(r"^\s*\[(\d{1,4})\]\s*|^\s*(\d{1,4})\.\s+(?=[A-Z])")
DOI_RE = re.compile(r"\b(10\.\d{4,9}/[^\s\"<>]+[^\s\"<>.,;:)\]])")
ARXIV_RE = re.compile(r"(?:arXiv:\s?|arxiv\.org/abs/)(\d{4}\.\d{4,5})(?:v\d+)?", re.I)
_MARKER = re.compile(r"\[(\d{1,4}(?:\s*[,–\-]\s*\d{1,4}){0,20})\]")


@dataclass
class Reference:
    number: int | None
    text: str
    page: int
    doi: str | None = None
    arxiv: str | None = None
    title_guess: str = ""
    lookup: dict[str, Any] = field(default_factory=dict)


def _guess_title(text: str) -> str:
    quoted = re.search(r"[\"“](.{10,250}?)[\"”]", text)
    if quoted:
        return quoted.group(1).strip(" .,")
    # tách câu tại ". " nhưng KHÔNG tách sau chữ viết tắt tên (K. He, X. Zhang)
    parts = [p.strip() for p in re.split(r"(?<!\b[A-Z])\.\s+", text) if p.strip()]
    # dạng "Tác giả. Tên bài. Nơi đăng, năm." -> phần thứ hai
    return parts[1][:250] if len(parts) >= 3 else (parts[0][:250] if parts else "")


def parse_references(ir: DocumentIR) -> list[Reference]:
    segs = [s for p in ir.pages for s in sorted(p.segments, key=lambda x: x.order)]
    start = next((i for i, s in enumerate(segs) if _REF_HEAD.match(s.plain_source())), None)
    if start is None:
        return []
    refs: list[Reference] = []
    for s in segs[start + 1 :]:
        text = s.plain_source().strip()
        if not text or s.role in ("header", "footer"):
            continue
        if _REF_HEAD.match(text) or (s.role == "title" and len(refs) > 3):
            break  # sang phần phụ lục
        # một đoạn có thể chứa nhiều mục [1] ... [2] ...
        pieces = re.split(r"(?=\[\d{1,4}\]\s)", text)
        for piece in pieces:
            piece = piece.strip()  # noqa: PLW2901
            if len(piece) < 15:
                continue
            m = _ENTRY_NUM.match(piece)
            num = int(m.group(1) or m.group(2)) if m else None
            body = piece[m.end() :] if m else piece
            doi = DOI_RE.search(body)
            ax = ARXIV_RE.search(body)
            refs.append(
                Reference(
                    num,
                    body[:1500],
                    s.page + 1,
                    doi.group(1) if doi else None,
                    ax.group(1) if ax else None,
                    _guess_title(body),
                )
            )
    return refs[:2000]


def expand_marker(marker: str) -> list[int]:
    nums: list[int] = []
    for part in re.split(r"\s*,\s*", marker.strip("[]")):
        if re.match(r"^\d+\s*[–\-]\s*\d+$", part):
            a, b = (int(x) for x in re.split(r"\s*[–\-]\s*", part))
            nums.extend(range(a, min(b, a + 50) + 1))
        elif part.strip().isdigit():
            nums.append(int(part))
    return nums


def citation_markers(ir: DocumentIR) -> list[dict[str, Any]]:
    """Vị trí các trích dẫn kiểu [12] trong thân bài (theo đoạn)."""
    out = []
    for s in ir.segments():
        if s.role != "body":
            continue
        for m in _MARKER.finditer(s.plain_source()):
            out.append(
                {"segment_id": s.id, "page": s.page + 1, "marker": m.group(0), "numbers": expand_marker(m.group(0))}
            )
    return out


_SAFE_DOI = re.compile(r"^10\.\d{4,9}/[A-Za-z0-9._;()/:<>\-]{1,200}$")


def safe_doi(doi: str | None) -> str | None:
    """DOI lấy từ PDF (không tin cậy) trước khi ghép vào URL: đúng dạng, không '..', không ? # \\ khoảng trắng."""
    if not doi or ".." in doi or not _SAFE_DOI.match(doi):
        return None
    return doi


class CitationLookup:
    """Tra cứu siêu dữ liệu bài được trích dẫn qua Semantic Scholar (có TLDR), dự phòng OpenAlex."""

    def __init__(self, s2_api_key: str | None = None, transport: Any = None) -> None:
        self._http = SafeHttpClient(NetworkPolicy.create(LOOKUP_HOSTS), timeout=30.0, transport=transport)
        self._s2_key = s2_api_key
        self._lock = asyncio.Lock()

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _s2(self, path: str) -> dict[str, Any] | None:
        fields = "title,year,authors,venue,tldr,openAccessPdf,externalIds,url,abstract"
        headers = {"x-api-key": self._s2_key} if self._s2_key else None
        async with self._lock:  # hạn mức chung: tuần tự, ~1 request/giây
            try:
                resp = await self._http.get(
                    f"https://{S2_HOST}/graph/v1/{path}{'&' if '?' in path else '?'}fields={fields}", headers=headers
                )
            except NetworkError:
                return None
            await asyncio.sleep(1.0)
        if resp.status != 200:
            return None
        data = resp.json()
        if isinstance(data, dict) and "data" in data and isinstance(data["data"], list):
            data = data["data"][0] if data["data"] else None
        return data if isinstance(data, dict) else None

    async def _openalex(self, ref: Reference) -> dict[str, Any] | None:
        doi = safe_doi(ref.doi)
        if doi:
            url = f"https://{OPENALEX_HOST}/works/doi:{quote(doi, safe='/')}"
        elif ref.title_guess:
            url = f"https://{OPENALEX_HOST}/works?search={quote(ref.title_guess[:200])}&per-page=1"
        else:
            return None
        try:
            resp = await self._http.get(url)
        except NetworkError:
            return None
        if resp.status != 200:
            return None
        data = resp.json()
        if isinstance(data, dict) and isinstance(data.get("results"), list):
            data = data["results"][0] if data["results"] else None
        if not isinstance(data, dict):
            return None
        return {
            "title": data.get("title"),
            "year": data.get("publication_year"),
            "url": (data.get("open_access") or {}).get("oa_url") or data.get("id"),
            "tldr": None,
        }

    async def lookup(self, ref: Reference) -> dict[str, Any]:
        data = None
        doi = safe_doi(ref.doi)
        if doi:
            data = await self._s2(f"paper/DOI:{quote(doi, safe='/')}")
        elif ref.arxiv:
            data = await self._s2(f"paper/arXiv:{ref.arxiv}")
        elif ref.title_guess:
            data = await self._s2(f"paper/search/match?query={quote(ref.title_guess[:200])}")
        if data:
            tldr = data.get("tldr") or {}
            pdf = data.get("openAccessPdf") or {}
            return {
                "title": str(data.get("title") or "")[:500],
                "year": data.get("year"),
                "venue": str(data.get("venue") or "")[:200],
                "authors": [str(a.get("name", ""))[:100] for a in (data.get("authors") or [])[:10]],
                "tldr": str(tldr.get("text") or "")[:1000] or None,
                "url": pdf.get("url") or data.get("url"),
                "source": "Semantic Scholar",
            }
        alt = await self._openalex(ref)
        return {**alt, "source": "OpenAlex"} if alt else {}


def reference_dict(r: Reference) -> dict[str, Any]:
    return asdict(r)
