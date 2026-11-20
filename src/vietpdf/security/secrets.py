"""Quản lý API key và khoá mã hoá dữ liệu cục bộ.

- Lưu trong kho mật khẩu của hệ điều hành: Windows Credential Manager, macOS Keychain, Secret Service/KWallet
  (Linux). Backend được KHỞI TẠO TRỰC TIẾP theo nền tảng — KHÔNG dùng `keyring.get_keyring()`, vì hàm đó đọc
  `keyringrc.cfg` (`keyring-path`, `default-keyring`) và `PYTHON_KEYRING_BACKEND`: một tiến trình ghi được vào
  HOME (vd. parser PDF bị chiếm quyền) có thể cài backend giả để chạy mã và nhận khoá (L1-022).
  Biến `PYTHON_KEYRING_BACKEND` chỉ được dùng để TẮT keyring (null/fail — phục vụ test), không thể chọn backend khác.
- Biến môi trường `VIETPDF_<PROVIDER>_API_KEY` / `VIETPDF_PDF_PASSWORD`: `capture_env_secrets()` được gọi NGAY khi
  khởi động CLI/GUI, đọc và xoá MỌI biến dạng này khỏi môi trường, trước khi có tiến trình con nào (L1-003).
- Khoá mã hoá dữ liệu: không có kho an toàn => `None` (chế độ không mã hoá, app cảnh báo); kho lỗi tạm thời hoặc
  khoá đã lưu bị hỏng => `SecretStoreError` (KHÔNG âm thầm ghi dạng rõ, KHÔNG ghi đè khoá cũ) (L1-005).
- Key không bao giờ được ghi vào file cấu hình hay log.
"""

from __future__ import annotations

import base64
import binascii
import os
import re
import sys
from typing import Any

from vietpdf.security.logsafe import register_secret

SERVICE = "vietpdf-translate"
CUSTOM_PROVIDER = "custom"  # khoá riêng cho Base URL tuỳ chỉnh (không bao giờ dùng khoá của nhà cung cấp chính thức)
_PROVIDER_RE = re.compile(r"^[a-z][a-z0-9_]{1,31}$")
_ENV_KEY_RE = re.compile(r"^VIETPDF_([A-Z][A-Z0-9_]{1,31})_API_KEY$")
_DISABLE_VALUES = {"keyring.backends.null.Keyring", "keyring.backends.fail.Keyring"}
_mem_cache: dict[str, str] = {}
_env_password: dict[str, str] = {}  # "pdf" -> mật khẩu PDF lấy từ biến môi trường


class SecretStoreError(Exception):
    pass


def _check_provider(provider: str) -> None:
    if not _PROVIDER_RE.match(provider):
        raise ValueError(f"Tên nhà cung cấp không hợp lệ: {provider!r}")


def env_var_name(provider: str) -> str:
    _check_provider(provider)
    return f"VIETPDF_{provider.upper()}_API_KEY"


def capture_env_secrets() -> None:
    """Đọc rồi XOÁ mọi `VIETPDF_*_API_KEY` và `VIETPDF_PDF_PASSWORD` khỏi môi trường của tiến trình.
    Gọi một lần ở đầu `main()` của CLI và GUI, trước khi tạo tiến trình con."""
    for name in list(os.environ):
        m = _ENV_KEY_RE.match(name)
        if m:
            value = (os.environ.pop(name, "") or "").strip()
            if value:
                register_secret(value)
                _mem_cache.setdefault(m.group(1).lower(), value)
    pw = os.environ.pop("VIETPDF_PDF_PASSWORD", None)
    if pw:
        _env_password["pdf"] = pw


def take_env_pdf_password() -> str | None:
    """Mật khẩu PDF đã đọc từ biến môi trường (nếu có)."""
    return _env_password.get("pdf") or os.environ.pop("VIETPDF_PDF_PASSWORD", None)


def _platform_backend_classes() -> list[Any]:
    if sys.platform == "win32":
        from keyring.backends import Windows  # noqa: PLC0415

        return [Windows.WinVaultKeyring]
    if sys.platform == "darwin":
        from keyring.backends import macOS  # noqa: PLC0415

        return [macOS.Keyring]
    from keyring.backends import SecretService, kwallet, libsecret  # noqa: PLC0415

    return [SecretService.Keyring, libsecret.Keyring, kwallet.DBusKeyring]


def backend_is_secure(backend: Any) -> bool:
    """Chỉ chấp nhận đúng các lớp backend của hệ điều hành (so sánh lớp, không so chuỗi __module__)."""
    try:
        return any(type(backend) is cls for cls in _platform_backend_classes())
    except ImportError:
        return False


def _secure_keyring() -> Any:
    if os.environ.get("PYTHON_KEYRING_BACKEND") in _DISABLE_VALUES:
        raise SecretStoreError("Kho mật khẩu bị tắt (PYTHON_KEYRING_BACKEND).")
    try:
        classes = _platform_backend_classes()
    except ImportError as e:
        raise SecretStoreError(f"Không nạp được backend kho mật khẩu: {e}") from e
    for cls in classes:
        try:
            if cls.priority > 0:  # thuộc tính lớp; ném lỗi nếu backend không khả dụng trên máy
                return cls()
        except Exception:  # noqa: S112 - backend không khả dụng: thử backend kế tiếp
            continue
    raise SecretStoreError(
        "Không có kho mật khẩu an toàn của hệ điều hành. Hãy bật Secret Service/KWallet (Linux), hoặc dùng biến "
        "môi trường VIETPDF_<PROVIDER>_API_KEY."
    )


def get_api_key(provider: str) -> str | None:
    _check_provider(provider)
    if provider in _mem_cache:
        return _mem_cache[provider]
    value = os.environ.pop(env_var_name(provider), None)  # dự phòng nếu capture_env_secrets() chưa được gọi
    if not value:
        try:
            value = _secure_keyring().get_password(SERVICE, provider)
        except SecretStoreError:
            value = None
    if value:
        value = value.strip()
        register_secret(value)
        _mem_cache[provider] = value
    return value or None


def set_api_key(provider: str, value: str) -> None:
    _check_provider(provider)
    value = value.strip()
    if not value or len(value) > 512 or any(c.isspace() for c in value):
        raise ValueError("API key không hợp lệ")
    _secure_keyring().set_password(SERVICE, provider, value)
    register_secret(value)
    _mem_cache[provider] = value


def delete_api_key(provider: str) -> None:
    _check_provider(provider)
    _mem_cache.pop(provider, None)
    kr = _secure_keyring()
    try:
        kr.delete_password(SERVICE, provider)
    except Exception as e:  # keyring.errors.PasswordDeleteError khi chưa có key
        if type(e).__name__ != "PasswordDeleteError":
            raise


CACHE_KEY_NAME = "__cache_key__"


def get_or_create_cache_key() -> bytes | None:
    """Khoá AES-256 dùng chung cho cache bản dịch và kho học tập, lưu trong kho mật khẩu hệ điều hành.

    - Không có kho an toàn trên máy => `None` (dữ liệu không mã hoá; app cảnh báo rõ).
    - Kho có nhưng đọc/ghi lỗi (đang khoá, D-Bus lỗi...) => `SecretStoreError`: KHÔNG lùi về ghi dạng rõ.
    - Khoá đã lưu bị hỏng => `SecretStoreError`: KHÔNG tạo khoá mới đè lên (sẽ làm mất dữ liệu cũ vĩnh viễn)."""
    import secrets as pysecrets  # noqa: PLC0415

    try:
        kr = _secure_keyring()
    except SecretStoreError:
        return None
    try:
        raw = kr.get_password(SERVICE, CACHE_KEY_NAME)
    except Exception as e:
        raise SecretStoreError(f"Không đọc được khoá mã hoá từ kho mật khẩu ({type(e).__name__}); thử lại sau.") from e
    if raw:
        try:
            key = base64.b64decode(raw, validate=True)
        except (binascii.Error, ValueError) as e:
            raise SecretStoreError("Khoá mã hoá trong kho mật khẩu bị hỏng — không ghi đè để tránh mất dữ liệu.") from e
        if len(key) != 32:
            raise SecretStoreError("Khoá mã hoá trong kho mật khẩu sai độ dài — không ghi đè để tránh mất dữ liệu.")
        return key
    key = pysecrets.token_bytes(32)
    try:
        kr.set_password(SERVICE, CACHE_KEY_NAME, base64.b64encode(key).decode())
    except Exception as e:
        raise SecretStoreError(f"Không lưu được khoá mã hoá vào kho mật khẩu ({type(e).__name__}).") from e
    return key
