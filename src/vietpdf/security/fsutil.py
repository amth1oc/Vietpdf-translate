"""Ghi file an toàn.

Không dùng tên file tạm cố định/đoán được (vd. `out.tmp.pdf`): trong thư mục dùng chung, kẻ xấu có thể tạo sẵn
symlink cùng tên trỏ tới file khác để ứng dụng ghi đè. `mkstemp` tạo file mới với tên ngẫu nhiên (O_EXCL, 0600)."""

from __future__ import annotations

import contextlib
import os
import tempfile
from pathlib import Path


def temp_near(target: Path, suffix: str = ".tmp") -> Path:
    """Tạo (an toàn) một file tạm rỗng cùng thư mục với `target`, trả về đường dẫn."""
    fd, name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=suffix, dir=str(target.parent))
    os.close(fd)
    return Path(name)


def atomic_write_text(target: Path, text: str, private: bool = False) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = temp_near(target)
    try:
        tmp.write_text(text, "utf-8")
        if private and os.name == "posix":
            tmp.chmod(0o600)
        tmp.replace(target)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


def replace_from_temp(tmp: Path, target: Path, private: bool = True) -> None:
    """Đổi tên nguyên tử file tạm thành đích. Mặc định quyền 0600 (POSIX): PDF đầu ra chứa nội dung tài liệu — thư
    viện ghi PDF (PyMuPDF) tạo lại file theo umask (thường 0644), nên phải đặt lại quyền trước khi đổi tên (L1-011)."""
    try:
        if private and os.name == "posix":
            tmp.chmod(0o600)
        tmp.replace(target)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise
