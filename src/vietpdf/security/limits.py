"""Giới hạn tài nguyên khi xử lý file PDF không tin cậy."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


class InputRejectedError(Exception):
    """File đầu vào bị từ chối trước khi được phân tích."""


@dataclass(frozen=True)
class InputLimits:
    max_file_mb: int = 500
    max_pages: int = 5000
    memory_mb: int = 3072  # áp dụng bằng RLIMIT_AS trên Linux; nơi khác chỉ có timeout
    base_timeout_s: float = 60.0
    per_page_timeout_s: float = 3.0
    max_text_chars_per_page: int = 200_000
    max_segments_per_page: int = 3000

    def timeout_for(self, pages: int) -> float:
        return self.base_timeout_s + self.per_page_timeout_s * max(pages, 1)


DEFAULT_LIMITS = InputLimits()


def precheck_pdf(path: Path, limits: InputLimits = DEFAULT_LIMITS) -> Path:
    """Kiểm tra rẻ, không cần parser: tồn tại, là file thường, kích thước, chữ ký %PDF-."""
    try:
        real = path.resolve(strict=True)
    except (FileNotFoundError, RuntimeError) as e:
        raise InputRejectedError(f"Không tìm thấy file: {path}") from e
    if not real.is_file():
        raise InputRejectedError(f"Không phải file thường: {path}")
    size = real.stat().st_size
    if size == 0:
        raise InputRejectedError("File rỗng")
    if size > limits.max_file_mb * 1024 * 1024:
        raise InputRejectedError(f"File lớn hơn giới hạn {limits.max_file_mb} MB")
    with real.open("rb") as f:
        head = f.read(1024)
    if b"%PDF-" not in head:
        raise InputRejectedError("Không phải file PDF (thiếu chữ ký %PDF-)")
    return real
