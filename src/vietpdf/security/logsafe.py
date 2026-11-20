"""Ghi log an toàn: tự che API key và các chuỗi giống bí mật trước khi ghi."""

from __future__ import annotations

import logging
import re
import threading

_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"sk-(?:proj-|ant-|or-)?[A-Za-z0-9_\-]{16,}"),  # OpenAI / OpenRouter / Anthropic
    re.compile(r"AIza[0-9A-Za-z_\-]{30,}"),  # Google API key
    re.compile(r"gsk_[A-Za-z0-9]{20,}"),  # Groq
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/\-]{8,}=*"),
    re.compile(
        r"(?i)((?:api[_-]?key|authorization|x-goog-api-key|access[_-]?token|secret)[\"'\s]*[:=]\s*[\"']?)"
        r"[^\s\"',}]{6,}"
    ),
]

_known_secrets: set[str] = set()
_lock = threading.Lock()
MASK = "***"


def register_secret(value: str | None) -> None:
    """Đăng ký một bí mật cụ thể (vd. key vừa đọc từ keyring) để luôn bị che trong log."""
    if value and len(value) >= 6:
        with _lock:
            _known_secrets.add(value)


def redact(text: str) -> str:
    if not text:
        return text
    with _lock:
        secrets = sorted(_known_secrets, key=len, reverse=True)
    for s in secrets:
        text = text.replace(s, MASK)
    for pat in _PATTERNS:
        text = pat.sub(lambda m: m.group(1) + MASK, text) if pat.groups else pat.sub(MASK, text)
    return text


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:  # định dạng log hỏng: vẫn ghi nhưng không lộ args
            msg = str(record.msg)
        record.msg = redact(msg)
        record.args = None
        if record.exc_text:
            record.exc_text = redact(record.exc_text)
        return True


class _RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))  # che cả traceback


def setup_logging(level: int = logging.INFO) -> None:
    root = logging.getLogger()
    root.setLevel(level)
    for h in list(root.handlers):
        root.removeHandler(h)
    handler = logging.StreamHandler()
    handler.addFilter(RedactingFilter())
    handler.setFormatter(_RedactingFormatter("%(levelname)s %(name)s: %(message)s"))
    root.addHandler(handler)
    # httpx ghi URL ở mức INFO; giữ ở WARNING để tránh lộ thông tin không cần thiết.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
