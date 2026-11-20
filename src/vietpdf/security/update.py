"""Kiểm tra bản cập nhật — TUỲ CHỌN, mặc định tắt, chỉ chạy khi người dùng bấm/gõ lệnh.

Chỉ gọi GitHub Releases API (một host), chỉ THÔNG BÁO có bản mới và đường dẫn trang phát hành.
Ứng dụng KHÔNG BAO GIỜ tự tải hay chạy bản cập nhật: người dùng tự tải và kiểm tra SHA256SUMS + attestation.
"""

from __future__ import annotations

import re
from typing import Any

from vietpdf import __version__
from vietpdf.security.http import NetworkPolicy, SafeHttpClient

GITHUB_API = "api.github.com"
# Repo chính thức — PHẢI được người duy trì điền khi phát hành. Để trống = tắt kiểm tra cập nhật: nếu để một tên
# repo giả định, người khác có thể đăng ký đúng tên đó và "thông báo bản mới" dẫn người dùng tới trang giả mạo.
REPO = ""
_VER = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")


def _parse(v: str) -> tuple[int, int, int] | None:
    m = _VER.match(v.strip())
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


class UpdateCheckDisabledError(Exception):
    pass


async def check_for_update(repo: str = REPO, transport: Any = None) -> dict[str, Any]:
    if not repo:
        raise UpdateCheckDisabledError("Chưa cấu hình repo phát hành chính thức (security/update.py: REPO).")
    if not re.match(r"^[\w.\-]+/[\w.\-]+$", repo):
        raise ValueError("Tên repo không hợp lệ")
    async with SafeHttpClient(NetworkPolicy.create([GITHUB_API]), timeout=20.0, transport=transport) as http:
        resp = await http.get(
            f"https://{GITHUB_API}/repos/{repo}/releases/latest", headers={"Accept": "application/vnd.github+json"}
        )
    if resp.status != 200:
        return {"ok": False, "current": __version__, "message": f"GitHub trả về HTTP {resp.status}"}
    data = resp.json()
    tag = str(data.get("tag_name", ""))
    latest, current = _parse(tag), _parse(__version__)
    url = str(data.get("html_url", ""))
    if not url.startswith(f"https://github.com/{repo}/releases/"):
        url = f"https://github.com/{repo}/releases"
    return {
        "ok": True,
        "current": __version__,
        "latest": tag,
        "url": url,
        "update_available": bool(latest and current and latest > current),
    }
