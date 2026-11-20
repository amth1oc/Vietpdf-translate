"""Mã hoá trường dữ liệu (AES-256-GCM) cho dữ liệu cá nhân lưu trên đĩa: thư viện tài liệu, sổ từ, lịch sử hỏi đáp.

- Khoá 256-bit nằm trong kho mật khẩu hệ điều hành (dùng chung khoá với cache, xem security/secrets.py).
- AAD = "bảng:id": bản mã của dòng này không thể chép sang dòng khác mà vẫn giải mã được.
- Không có kho mật khẩu an toàn: lưu chữ thường (tiền tố "p:"), file vẫn quyền 0600 — và ứng dụng báo rõ điều đó.
- Khi ĐÃ bật mã hoá, bản ghi chữ thường "p:" bị TỪ CHỐI (không thể chèn dòng giả mạo hay hạ cấp); dữ liệu cũ tạo khi
  chưa có kho mật khẩu được chuyển đổi tường minh bằng `vietpdf library migrate` (L1-005).
"""

from __future__ import annotations

import base64
import json
import os
from typing import Any

PLAIN, ENC = "p:", "v1:"


class FieldCipher:
    def __init__(self, key: bytes | None) -> None:
        self._aead: Any = None
        if key:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # noqa: PLC0415

            self._aead = AESGCM(key)

    @property
    def enabled(self) -> bool:
        return self._aead is not None

    def seal(self, aad: str, obj: Any) -> str:
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        if self._aead is None:
            return PLAIN + raw.decode("utf-8")
        nonce = os.urandom(12)
        return ENC + base64.b64encode(nonce + self._aead.encrypt(nonce, raw, aad.encode())).decode("ascii")

    def open(self, aad: str, stored: str) -> Any:
        """Trả về None nếu không giải mã được (sai khoá, dữ liệu bị sửa, hoặc bản ghi chữ thường khi đã bật mã hoá)."""
        try:
            if stored.startswith(PLAIN):
                return None if self._aead is not None else json.loads(stored[len(PLAIN) :])
            if stored.startswith(ENC) and self._aead is not None:
                blob = base64.b64decode(stored[len(ENC) :], validate=True)
                return json.loads(self._aead.decrypt(blob[:12], blob[12:], aad.encode()).decode("utf-8"))
        except Exception:
            return None
        return None

    def open_legacy_plain(self, stored: str) -> Any:
        """Chỉ dùng cho lệnh chuyển đổi do người dùng chủ động chạy: đọc bản ghi chữ thường cũ."""
        if not stored.startswith(PLAIN):
            return None
        try:
            return json.loads(stored[len(PLAIN) :])
        except (ValueError, RecursionError):
            return None
