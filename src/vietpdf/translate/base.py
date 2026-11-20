"""Interface chung cho mọi nhà cung cấp dịch."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

from vietpdf.translate.glossary import GlossaryEntry

TranslationStyle = Literal["de_doc", "sat_nghia", "hanh_chinh"]
Quality = Literal["fast", "standard", "high"]

LANG_NAMES = {
    "en": "tiếng Anh",
    "vi": "tiếng Việt",
    "zh": "tiếng Trung",
    "ja": "tiếng Nhật",
    "ko": "tiếng Hàn",
    "fr": "tiếng Pháp",
    "de": "tiếng Đức",
    "es": "tiếng Tây Ban Nha",
    "ru": "tiếng Nga",
    "pt": "tiếng Bồ Đào Nha",
    "it": "tiếng Ý",
    "th": "tiếng Thái",
    "id": "tiếng Indonesia",
}


def lang_name(code: str) -> str:
    return LANG_NAMES.get(code.split("-", maxsplit=1)[0].lower(), code)


@dataclass(frozen=True)
class Item:
    id: str
    text: str


@dataclass
class TranslateContext:
    source_lang: str = "en"
    target_lang: str = "vi"
    title: str = ""
    domain: str = ""
    style: TranslationStyle = "de_doc"
    glossary: list[GlossaryEntry] = field(default_factory=list)
    summary: str = ""  # tóm tắt tài liệu / chương: ngữ cảnh cho mọi lô
    # Chế độ "dịch gọn": id -> số ký tự tối đa mong muốn
    shorten_limits: dict[str, int] = field(default_factory=dict)
    # Nhắc riêng cho từng đoạn khi dịch lại (vd. "thiếu thuật ngữ overfitting -> quá khớp")
    notes: dict[str, str] = field(default_factory=dict)
    # Đoạn liền trước (theo thứ tự đọc) để giữ đại từ/cách gọi nhất quán
    previous: dict[str, str] = field(default_factory=dict)

    def subset(self, ids: set[str], glossary: list[GlossaryEntry]) -> TranslateContext:
        return TranslateContext(
            source_lang=self.source_lang,
            target_lang=self.target_lang,
            title=self.title,
            domain=self.domain,
            style=self.style,
            glossary=glossary,
            summary=self.summary,
            shorten_limits={k: v for k, v in self.shorten_limits.items() if k in ids},
            notes={k: v for k, v in self.notes.items() if k in ids},
            previous={k: v for k, v in self.previous.items() if k in ids},
        )


class TranslatorError(Exception):
    """Lỗi chung, không thử lại."""


class AuthenticationError(TranslatorError):
    """Sai/thiếu API key (401/403): dừng ngay."""


class ConfigurationError(TranslatorError):
    pass


class RetryableError(TranslatorError):
    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class RateLimitedError(RetryableError):
    """429 / RESOURCE_EXHAUSTED."""


class TransientError(RetryableError):
    """5xx, timeout, lỗi mạng."""


class InvalidResponseError(TranslatorError):
    """Phản hồi không đúng schema."""


@runtime_checkable
class Translator(Protocol):
    provider: str
    model: str

    async def list_models(self) -> list[str]: ...

    async def translate_batch(self, items: list[Item], ctx: TranslateContext) -> dict[str, str]: ...

    async def complete_json(self, messages: list[dict[str, str]], schema_name: str, schema: dict[str, Any]) -> Any:
        """Tác vụ JSON chung (nhận diện lĩnh vực, trích thuật ngữ, biên tập, trợ lý đọc...)."""
        ...

    def estimate_tokens(self, items: list[Item]) -> int: ...

    async def aclose(self) -> None: ...


def estimate_tokens_simple(items: list[Item]) -> int:
    """Ước tính thô: ~4 ký tự/token đầu vào, đầu ra tiếng Việt ~1.6x số token đầu vào."""
    chars = sum(len(i.text) for i in items)
    return int(chars / 4 * 2.6) + 400 * max(1, len(items) // 20)
