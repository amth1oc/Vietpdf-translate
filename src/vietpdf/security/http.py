"""HTTP client duy nhất của ứng dụng.

Đây là file DUY NHẤT trong `src/` được phép import thư viện mạng (httpx). Quy tắc này được
`scripts/security_audit.py` kiểm tra trong CI. Mọi module khác phải đi qua `SafeHttpClient`.

Các bảo đảm:
- Chỉ kết nối tới host nằm trong allowlist (kiểm tra ở mọi request, kể cả request nội bộ của httpx).
- Không tự theo redirect (một host hợp lệ không thể chuyển hướng sang host lạ).
- Bắt buộc HTTPS cho mọi host không phải loopback; luôn xác minh chứng chỉ TLS.
- URL không được chứa thông tin đăng nhập (user:pass@host).
- Giới hạn kích thước phản hồi để chống làm tràn bộ nhớ.
- Chế độ offline: chỉ cho phép loopback.
- KHÔNG đọc cấu hình mạng từ biến môi trường (`trust_env=False`): HTTP(S)_PROXY, ALL_PROXY, SSL_CERT_FILE,
  SSL_CERT_DIR, .netrc bị bỏ qua — nếu không, lưu lượng "offline" tới Ollama có thể đi qua proxy và kho CA có thể bị
  thay (L1-004). Mạng cần proxy: cấu hình tường minh `https_proxy` trong settings.json; proxy CHỈ áp dụng cho HTTPS
  tới host không phải loopback và không bao giờ dùng trong chế độ offline.
- JSON từ mạng được đọc bằng `safe_json_loads` (JSON lồng quá sâu => lỗi JSON bình thường, không RecursionError).
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from vietpdf.security.logsafe import redact

log = logging.getLogger(__name__)

LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
DEFAULT_MAX_RESPONSE_BYTES = 20 * 1024 * 1024
DEFAULT_TIMEOUT_S = 180.0


_PROXY: dict[str, str | None] = {"url": None}


def configure_proxy(url: str | None) -> None:
    """Đặt proxy HTTPS tường minh (từ settings.json). Chỉ nhận http(s)://host:port, không có thông tin đăng nhập."""
    if not url:
        _PROXY["url"] = None
        return
    u = httpx.URL(url)
    if u.scheme not in ("http", "https") or not u.host or u.userinfo:
        raise ValueError("Proxy không hợp lệ (cần http(s)://host:port, không kèm user:pass)")
    _PROXY["url"] = str(u)


def safe_json_loads(data: str | bytes) -> Any:
    """json.loads nhưng JSON lồng quá sâu (dữ liệu thù địch) thành JSONDecodeError thay vì RecursionError."""
    try:
        return json.loads(data)
    except RecursionError:
        raise json.JSONDecodeError("JSON lồng quá sâu", "", 0) from None


def _transport_kwargs(policy: NetworkPolicy, transport: Any) -> dict[str, Any]:
    if transport is not None:
        return {"transport": transport}
    proxy = _PROXY["url"]
    if proxy and not policy.offline:
        # chỉ HTTPS (host không phải loopback, xem check_url) đi qua proxy; http:// (loopback) luôn đi thẳng
        return {"mounts": {"https://": httpx.AsyncHTTPTransport(proxy=proxy, verify=True, trust_env=False)}}
    return {}


class NetworkPolicyError(Exception):
    """Request bị chặn bởi chính sách mạng."""


class ResponseTooLargeError(Exception):
    """Phản hồi vượt quá giới hạn kích thước."""


class NetworkError(Exception):
    """Lỗi kết nối / timeout (có thể thử lại)."""


def normalize_host(host: str) -> str:
    return host.strip().lower().rstrip(".").strip("[]")


def url_host(url: str) -> str:
    return normalize_host(httpx.URL(url).host or "")


@dataclass(frozen=True)
class NetworkPolicy:
    allowed_hosts: frozenset[str]
    offline: bool = False
    max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES

    @classmethod
    def create(cls, hosts: Iterable[str], *, offline: bool = False, **kw: Any) -> NetworkPolicy:
        return cls(frozenset(normalize_host(h) for h in hosts if h), offline=offline, **kw)

    def check_url(self, url: httpx.URL | str) -> None:
        u = httpx.URL(str(url))
        scheme = u.scheme.lower()
        host = normalize_host(u.host or "")
        if scheme not in ("http", "https"):
            raise NetworkPolicyError(f"Giao thức không được phép: {scheme!r}")
        if u.userinfo:
            raise NetworkPolicyError("URL không được chứa thông tin đăng nhập")
        if not host:
            raise NetworkPolicyError("URL thiếu host")
        is_loopback = host in LOOPBACK_HOSTS
        if self.offline and not is_loopback:
            raise NetworkPolicyError(f"Chế độ offline nghiêm ngặt: chặn kết nối tới {host!r}")
        if not is_loopback and scheme != "https":
            raise NetworkPolicyError(f"Bắt buộc HTTPS cho host {host!r}")
        if host not in self.allowed_hosts:
            raise NetworkPolicyError(f"Host không nằm trong danh sách cho phép: {host!r}")


@dataclass
class HttpResponse:
    status: int
    headers: dict[str, str]
    body: bytes = field(repr=False)

    def json(self) -> Any:
        return safe_json_loads(self.body.decode("utf-8"))

    def text(self, limit: int = 2000) -> str:
        """Nội dung đã che bí mật, cắt ngắn — an toàn để ghi log/hiển thị."""
        return redact(self.body[:limit].decode("utf-8", errors="replace"))


class SafeHttpClient:
    """Bọc httpx.AsyncClient với chính sách mạng bắt buộc."""

    def __init__(
        self,
        policy: NetworkPolicy,
        *,
        timeout: float = DEFAULT_TIMEOUT_S,
        transport: httpx.AsyncBaseTransport | None = None,
        user_agent: str = "VietPDF-Translate",
    ) -> None:
        self.policy = policy

        async def _guard(request: httpx.Request) -> None:
            policy.check_url(request.url)

        self._client = httpx.AsyncClient(
            follow_redirects=False,
            verify=True,
            timeout=httpx.Timeout(timeout, connect=15.0),
            event_hooks={"request": [_guard]},
            headers={"User-Agent": user_agent},
            trust_env=False,  # không để biến môi trường (proxy, CA, .netrc) thay đổi đường đi của dữ liệu
            **_transport_kwargs(policy, transport),
        )

    async def __aenter__(self) -> SafeHttpClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        json_body: Any = None,
    ) -> HttpResponse:
        self.policy.check_url(url)  # kiểm tra sớm, trước cả khi dựng request
        try:
            async with self._client.stream(method, url, headers=headers, json=json_body) as resp:
                body = await self._read_limited(resp.aiter_bytes())
                return HttpResponse(resp.status_code, dict(resp.headers), body)
        except NetworkPolicyError:
            raise
        except httpx.TimeoutException as e:
            raise NetworkError(f"Hết thời gian chờ khi gọi {httpx.URL(url).host}") from e
        except httpx.TransportError as e:
            raise NetworkError(f"Lỗi kết nối tới {httpx.URL(url).host}: {type(e).__name__}") from e

    async def get(self, url: str, *, headers: dict[str, str] | None = None) -> HttpResponse:
        return await self.request("GET", url, headers=headers)

    async def post_json(self, url: str, body: Any, *, headers: dict[str, str] | None = None) -> HttpResponse:
        return await self.request("POST", url, headers=headers, json_body=body)

    async def download_to(
        self, url: str, dest: Any, *, max_bytes: int, redirect_hosts: Iterable[str] = (), max_redirects: int = 5
    ) -> int:
        """Tải file lớn ra đĩa. Redirect chỉ được theo tới host nằm trong allowlist HOẶC `redirect_hosts`
        (vd. CDN của Hugging Face), bắt buộc HTTPS, tối đa `max_redirects` lần. Ghi vào file tạm cùng thư mục rồi
        đổi tên nguyên tử; lỗi/quá giới hạn/bị ngắt thì xoá file tạm, không để lại file dở mang tên đích (L1-026)."""
        extra = {normalize_host(h) for h in redirect_hosts}
        current = url
        dest_path = Path(dest)
        for _ in range(max_redirects + 1):
            host = url_host(current)
            if host in extra:
                if httpx.URL(current).scheme != "https":
                    raise NetworkPolicyError("Redirect phải dùng HTTPS")
                policy = NetworkPolicy(self.policy.allowed_hosts | {host}, self.policy.offline, max_bytes)
            else:
                policy = NetworkPolicy(self.policy.allowed_hosts, self.policy.offline, max_bytes)
            policy.check_url(current)
            async with (
                httpx.AsyncClient(
                    follow_redirects=False,
                    verify=True,
                    timeout=httpx.Timeout(600, connect=15),
                    trust_env=False,
                    **_transport_kwargs(policy, None),
                ) as client,
                client.stream("GET", current) as resp,
            ):
                if resp.status_code in (301, 302, 303, 307, 308):
                    current = str(resp.url.join(resp.headers.get("location", "")))
                    continue
                if resp.status_code != 200:
                    raise NetworkError(f"HTTP {resp.status_code} khi tải {host}")
                fd, tmp_name = tempfile.mkstemp(prefix=f".{dest_path.name}.", suffix=".part", dir=dest_path.parent)
                total = 0
                try:
                    with os.fdopen(fd, "wb") as fh:
                        async for chunk in resp.aiter_bytes():
                            total += len(chunk)
                            if total > max_bytes:
                                raise ResponseTooLargeError("File tải về vượt giới hạn kích thước")
                            fh.write(chunk)
                    os.replace(tmp_name, dest_path)
                except BaseException:
                    with contextlib.suppress(OSError):
                        os.unlink(tmp_name)
                    raise
                return total
        raise NetworkPolicyError("Quá nhiều lần chuyển hướng")

    async def stream_json_lines(
        self, method: str, url: str, *, json_body: Any = None, max_line_bytes: int = 64 * 1024
    ) -> AsyncIterator[Any]:
        """Đọc phản hồi dạng JSON-lines (vd. tiến độ `ollama pull`), mỗi dòng có giới hạn độ dài."""
        self.policy.check_url(url)
        try:
            async with self._client.stream(method, url, json=json_body) as resp:
                if resp.status_code >= 400:
                    body = await self._read_limited(resp.aiter_bytes())
                    raise NetworkError(f"HTTP {resp.status_code}: {redact(body[:500].decode(errors='replace'))}")
                n_lines = 0
                async for line in resp.aiter_lines():
                    n_lines += 1
                    if n_lines > 1_000_000:
                        raise ResponseTooLargeError("Quá nhiều dòng phản hồi")
                    if len(line) > max_line_bytes:
                        raise ResponseTooLargeError("Dòng phản hồi quá dài")
                    if line.strip():
                        yield safe_json_loads(line)
        except httpx.TransportError as e:
            raise NetworkError(f"Lỗi kết nối: {type(e).__name__}") from e

    async def _read_limited(self, chunks: AsyncIterator[bytes]) -> bytes:
        buf = bytearray()
        limit = self.policy.max_response_bytes
        async for chunk in chunks:
            buf.extend(chunk)
            if len(buf) > limit:
                raise ResponseTooLargeError(f"Phản hồi vượt quá {limit // (1024 * 1024)} MB")
        return bytes(buf)
