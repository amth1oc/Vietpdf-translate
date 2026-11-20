"""Bảng khai báo nhà cung cấp. Thêm nhà cung cấp kiểu OpenAI mới = thêm một dòng ở đây."""

from __future__ import annotations

from dataclasses import dataclass

from vietpdf.security.http import LOOPBACK_HOSTS, NetworkPolicy, SafeHttpClient, url_host
from vietpdf.translate.base import AuthenticationError, ConfigurationError, Translator
from vietpdf.translate.fake import FakeTranslator
from vietpdf.translate.openai_compat import OpenAICompatTranslator


@dataclass(frozen=True)
class ProviderSpec:
    id: str
    display: str
    base_url: str
    needs_key: bool
    default_rpm: float
    local: bool
    privacy_note: str = ""
    # "json_schema": structured outputs; "json_object": JSON mode; "prompt": máy chủ bỏ qua response_format
    json_mode: str = "json_schema"
    key_header: str = "bearer"  # "bearer" | "x-api-key"


PROVIDERS: dict[str, ProviderSpec] = {
    "gemini": ProviderSpec(
        "gemini",
        "Google Gemini",
        "https://generativelanguage.googleapis.com/v1beta/openai",
        needs_key=True,
        default_rpm=5,
        local=False,
        privacy_note=(
            "Gói miễn phí của Gemini: Google có thể dùng dữ liệu gửi lên để cải thiện mô hình. "
            "Với tài liệu mật, hãy dùng Ollama (chạy hoàn toàn trên máy)."
        ),
    ),
    "openai": ProviderSpec(
        "openai", "OpenAI", "https://api.openai.com/v1", needs_key=True, default_rpm=60, local=False
    ),
    "ollama": ProviderSpec(
        "ollama", "Ollama (offline)", "http://localhost:11434/v1", needs_key=False, default_rpm=600, local=True
    ),
    # v2.0 — thêm nhà cung cấp chỉ là thêm cấu hình
    "claude": ProviderSpec(
        "claude",
        "Anthropic Claude",
        "https://api.anthropic.com/v1",
        needs_key=True,
        default_rpm=50,
        local=False,
        json_mode="prompt",
        key_header="x-api-key",
    ),
    "deepseek": ProviderSpec(
        "deepseek",
        "DeepSeek",
        "https://api.deepseek.com/v1",
        needs_key=True,
        default_rpm=60,
        local=False,
        json_mode="json_object",
    ),
    "openrouter": ProviderSpec(
        "openrouter", "OpenRouter", "https://openrouter.ai/api/v1", needs_key=True, default_rpm=20, local=False
    ),
    "groq": ProviderSpec("groq", "Groq", "https://api.groq.com/openai/v1", needs_key=True, default_rpm=30, local=False),
    "fake": ProviderSpec("fake", "Translator giả (test)", "", needs_key=False, default_rpm=100000, local=True),
}


def host_of(url: str) -> str:
    return url_host(url)


class ConsentRequiredError(ConfigurationError):
    """Chưa có đồng ý của người dùng cho việc gửi tài liệu tới nhà cung cấp/host này (L1-008)."""


def endpoint(provider_id: str, base_url: str | None) -> tuple[str, str, bool]:
    """(URL dùng thật, host, có phải Base URL tuỳ chỉnh khác host mặc định không)."""
    spec = PROVIDERS[provider_id]
    url = (base_url or spec.base_url).rstrip("/")
    host = host_of(url) if url else ""
    default_host = host_of(spec.base_url) if spec.base_url else ""
    return url, host, bool(base_url) and host != default_host


def needs_consent(provider_id: str, base_url: str | None) -> bool:
    """Dữ liệu có rời khỏi máy không: mọi host không phải loopback đều cần người dùng đồng ý trước lần gửi đầu."""
    if provider_id == "fake":
        return False
    _, host, _ = endpoint(provider_id, base_url)
    return host not in LOOPBACK_HOSTS


def consent_id(provider_id: str, base_url: str | None) -> str:
    return f"{provider_id}@{endpoint(provider_id, base_url)[1]}"


def _scripts(label: str) -> set[str]:
    import unicodedata  # noqa: PLC0415

    out = set()
    for ch in label:
        if ch.isalpha():
            name = unicodedata.name(ch, "")
            out.add(name.split(" ", 1)[0] if name else "?")
    return out


def host_display(host: str) -> tuple[str, str | None]:
    """(dạng hiển thị, cảnh báo). Host quốc tế hoá (IDN) hiện CẢ dạng Unicode lẫn punycode; nhãn trộn nhiều hệ chữ
    (vd. Latin + Kirin như 'аpi.openai.com') bị cảnh báo đồng hình (UTS #39, mức hạn chế) — R1-009, 2.1.2."""
    h = host.rstrip(".")
    try:
        ascii_form = h.encode("idna").decode("ascii")
    except UnicodeError:
        ascii_form = h
    try:
        uni = ascii_form.encode("ascii").decode("idna")
    except UnicodeError:
        uni = h
    if ascii_form == uni:
        return ascii_form, None
    warn = None
    if any(len(_scripts(lbl)) > 1 for lbl in uni.split(".")):
        warn = (
            "CẢNH BÁO: tên máy chủ trộn nhiều hệ chữ (có thể là tên giả mạo trông giống tên thật): "
            f"{uni} = {ascii_form}"
        )
    return f"{uni} (punycode: {ascii_form})", warn


def privacy_text(provider_id: str, base_url: str | None) -> str:
    spec = PROVIDERS[provider_id]
    _, host, custom = endpoint(provider_id, base_url)
    host, homoglyph_warning = host_display(host)
    lines = [
        f"Nội dung tài liệu (các đoạn văn, tiêu đề, thuật ngữ, câu hỏi của bạn) sẽ được gửi tới {host} "
        f"({'máy chủ TUỲ CHỈNH' if custom else spec.display}) để xử lý. Máy chủ có thể đặt ngoài Việt Nam; việc lưu "
        "trữ và dùng dữ liệu để huấn luyện mô hình tuỳ theo điều khoản của nhà cung cấp.",
        "Tài liệu có dữ liệu cá nhân hoặc chưa công bố: hãy dùng Ollama (chạy hoàn toàn trên máy).",
    ]
    if homoglyph_warning:
        lines.insert(0, homoglyph_warning)
    if spec.privacy_note:
        lines.append(spec.privacy_note)
    if custom and spec.needs_key:
        lines.append(
            f"Base URL tuỳ chỉnh dùng khoá riêng 'custom' (vietpdf key set custom), KHÔNG dùng khoá của {spec.display}."
        )
    return "\n".join(lines)


def check_consent(provider_id: str, base_url: str | None, consents: dict[str, bool]) -> None:
    if needs_consent(provider_id, base_url) and not consents.get(consent_id(provider_id, base_url)):
        raise ConsentRequiredError(
            f"Chưa đồng ý gửi tài liệu tới {endpoint(provider_id, base_url)[1]}. "
            "Chạy lại với --accept-privacy (CLI) hoặc xác nhận trong hộp thoại (GUI)."
        )


def build_translator(
    provider_id: str,
    model: str,
    *,
    api_key: str | None = None,
    base_url: str | None = None,
    rpm: float | None = None,
    offline: bool = False,
    transport: object | None = None,
    custom_key: bool = False,
) -> Translator:
    """`custom_key=True`: `api_key` là khoá riêng cho Base URL tuỳ chỉnh (provider 'custom'). Khoá của nhà cung cấp
    chính thức KHÔNG BAO GIỜ được gửi tới host khác host mặc định của nhà cung cấp đó (L1-002)."""
    spec = PROVIDERS.get(provider_id)
    if spec is None:
        raise ConfigurationError(f"Nhà cung cấp không hỗ trợ: {provider_id!r}")
    if provider_id == "fake":
        return FakeTranslator()
    url, _host, custom = endpoint(provider_id, base_url)
    if custom and api_key and not custom_key:
        raise ConfigurationError(
            f"Không gửi khoá API của {spec.display} tới Base URL tuỳ chỉnh {_host}. "
            "Dùng khoá riêng: vietpdf key set custom"
        )
    if not model:
        raise ConfigurationError("Chưa chọn mô hình")
    if spec.needs_key and not api_key and not custom:
        raise AuthenticationError(f"Chưa có API key cho {spec.display}")
    # Allowlist chỉ gồm đúng host của nhà cung cấp đang dùng.
    policy = NetworkPolicy.create([host_of(url)], offline=offline)
    policy.check_url(url)  # báo lỗi sớm (vd. offline mà chọn Gemini)
    http = SafeHttpClient(policy, transport=transport)  # type: ignore[arg-type]
    return OpenAICompatTranslator(
        provider=provider_id,
        base_url=url,
        model=model,
        http=http,
        api_key=api_key,
        rpm=rpm or spec.default_rpm,
        json_mode=spec.json_mode,
        key_header=spec.key_header,
        extra_headers={"anthropic-version": "2023-06-01"} if provider_id == "claude" else None,
    )
