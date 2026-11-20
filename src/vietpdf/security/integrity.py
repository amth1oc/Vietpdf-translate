"""Kiểm tra toàn vẹn tài nguyên đóng gói (font, glossary, mô hình) bằng SHA-256.

Fail-closed: sai checksum, thiếu file, hoặc có file lạ không được khai báo (KỂ CẢ file ẩn) => dừng.
Tài nguyên không bao giờ được tải về lúc chạy.

2.1.1 (L1-020): SHA256SUMS nằm cùng thư mục với tài nguyên nên tự nó không chống được việc tráo cả hai. Mã băm của
chính SHA256SUMS được ghim trong mã nguồn (`SUMS_SHA256`, nằm trong PYZ/exe đã ký khi phát hành) — sửa tài nguyên
giờ đòi hỏi sửa cả mã chương trình. `scripts/update_asset_sums.py` cập nhật cả hai, thay đổi phải qua review.
Lớp bảo vệ thật trước kẻ ghi được thư mục cài đặt là chữ ký số của gói phát hành (lượt 3).
"""

from __future__ import annotations

import hashlib
import hmac
import threading
from pathlib import Path

ASSETS_DIR = Path(__file__).resolve().parent.parent / "assets"
SUMS_FILE = "SHA256SUMS"
# cập nhật bằng scripts/update_asset_sums.py (thay đổi phải qua review)
SUMS_SHA256 = "199d94651398b391734c1c0439b5c89b2b0b3c97c49b6b216c29e78cc30f4570"
_verified: set[Path] = set()
_lock = threading.Lock()


class IntegrityError(Exception):
    pass


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while block := f.read(chunk):
            h.update(block)
    return h.hexdigest()


def _parse_sums(assets_dir: Path) -> dict[str, str]:
    sums_path = assets_dir / SUMS_FILE
    if not sums_path.is_file():
        raise IntegrityError(f"Thiếu {sums_path}")
    if assets_dir.resolve() == ASSETS_DIR.resolve() and not hmac.compare_digest(sha256_file(sums_path), SUMS_SHA256):
        raise IntegrityError(f"{SUMS_FILE} không khớp mã băm ghim trong chương trình (có thể đã bị thay)")
    entries: dict[str, str] = {}
    for n, raw in enumerate(sums_path.read_text("utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(maxsplit=1)
        if len(parts) != 2 or len(parts[0]) != 64:
            raise IntegrityError(f"{SUMS_FILE}: dòng {n} sai định dạng")
        digest, rel = parts[0].lower(), parts[1].lstrip("*").strip()
        target = (assets_dir / rel).resolve()
        if not target.is_relative_to(assets_dir.resolve()) or rel.startswith("/"):
            raise IntegrityError(f"{SUMS_FILE}: đường dẫn ngoài thư mục assets: {rel!r}")
        entries[Path(rel).as_posix()] = digest
    return entries


def verify_assets(assets_dir: Path = ASSETS_DIR) -> None:
    assets_dir = assets_dir.resolve()
    with _lock:
        if assets_dir in _verified:
            return
        expected = _parse_sums(assets_dir)
        actual_files = {
            p.relative_to(assets_dir).as_posix()
            for p in assets_dir.rglob("*")
            if p.is_file() and p.relative_to(assets_dir).as_posix() != SUMS_FILE
        }
        unlisted = sorted(actual_files - expected.keys())
        if unlisted:
            raise IntegrityError(f"Có file không được khai báo trong {SUMS_FILE}: {unlisted}")
        missing = sorted(expected.keys() - actual_files)
        if missing:
            raise IntegrityError(f"Thiếu tài nguyên: {missing}")
        for rel, digest in expected.items():
            if not hmac.compare_digest(sha256_file(assets_dir / rel), digest):
                raise IntegrityError(f"Sai checksum: {rel} (file có thể đã bị thay đổi)")
        _verified.add(assets_dir)


def asset_path(rel: str, assets_dir: Path = ASSETS_DIR) -> Path:
    verify_assets(assets_dir)
    p = (assets_dir / rel).resolve()
    if not p.is_relative_to(assets_dir.resolve()) or not p.is_file():
        raise IntegrityError(f"Tài nguyên không hợp lệ: {rel!r}")
    return p
