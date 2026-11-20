"""Thư mục dữ liệu người dùng và cấu hình. File cấu hình KHÔNG BAO GIỜ chứa API key."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, StrictBool, ValidationError

from vietpdf.security.fsutil import atomic_write_text

APP = "VietPDF"


def _base_dir(kind: str) -> Path:
    home = Path.home()
    if sys.platform == "win32":
        root = Path(os.environ.get("LOCALAPPDATA") or home / "AppData" / "Local")
        return root / APP / kind
    if sys.platform == "darwin":
        sub = "Caches" if kind == "cache" else "Application Support"
        return home / "Library" / sub / APP / ("" if kind == "cache" else kind)
    env = {"config": "XDG_CONFIG_HOME", "cache": "XDG_CACHE_HOME", "data": "XDG_DATA_HOME"}[kind]
    default = {"config": home / ".config", "cache": home / ".cache", "data": home / ".local" / "share"}[kind]
    return Path(os.environ.get(env) or default) / "vietpdf"


def config_dir() -> Path:
    return Path(os.environ.get("VIETPDF_CONFIG_DIR") or _base_dir("config"))


def data_dir() -> Path:
    return Path(os.environ.get("VIETPDF_DATA_DIR") or _base_dir("data"))


def cache_dir() -> Path:
    return Path(os.environ.get("VIETPDF_CACHE_DIR") or _base_dir("cache"))


class ProviderSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: str = Field("", max_length=200)
    base_url: str = Field("", max_length=500)
    rpm: float | None = Field(None, gt=0, le=10000)


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider: str = "gemini"
    offline: StrictBool = False
    style: str = "de_doc"
    min_scale: float = Field(0.7, ge=0.3, le=1.0)
    providers: dict[str, ProviderSettings] = Field(default_factory=dict)
    gemini_privacy_ack: StrictBool = False  # cũ (≤ 2.1.0) — thay bằng privacy_consents
    privacy_consents: dict[str, StrictBool] = Field(default_factory=dict)  # "provider@host" -> đã đồng ý (2.1.1)
    https_proxy: str = Field("", max_length=500)  # proxy HTTPS tường minh; biến môi trường bị bỏ qua (2.1.1)
    quality: str = Field("standard", pattern="^(fast|standard|high)$")
    review_terms: StrictBool = True
    ui_lang: str = Field("vi", pattern="^(vi|en)$")
    theme: str = Field("light", pattern="^(light|dark)$")
    # extra="forbid": mọi trường lạ (vd. "api_key") trong file cấu hình đều bị từ chối.


def settings_path() -> Path:
    return config_dir() / "settings.json"


class ConfigError(ValueError):
    """settings.json không hợp lệ: báo rõ vị trí, KHÔNG lặng lẽ dùng mặc định và KHÔNG ghi đè file (R1-006)."""


def _describe(e: ValidationError) -> str:
    parts = []
    for err in e.errors()[:5]:
        loc = ".".join(str(x) for x in err.get("loc", ())) or "(gốc)"
        parts.append(f"{loc}: {err.get('msg', '')}")
    return "; ".join(parts)


def load_settings() -> Settings:
    """Không có file => mặc định. File có nhưng sai => ConfigError kèm đường dẫn trường (2.1.2, R1-006); trước đây
    trả mặc định trong im lặng rồi lần lưu kế tiếp ghi đè cấu hình của người dùng."""
    p = settings_path()
    if not p.is_file():
        return Settings()
    try:
        raw = p.read_text("utf-8")
    except OSError as e:
        raise ConfigError(f"Không đọc được {p}: {e}") from e
    try:
        return Settings.model_validate_json(raw)
    except ValidationError as e:
        raise ConfigError(f"{p} không hợp lệ — {_describe(e)}. Sửa hoặc xoá file này (app không tự ghi đè).") from e


def _raw_settings() -> dict[str, object]:
    """JSON thô của settings.json (rỗng nếu chưa có). File không parse được => ConfigError (không ghi đè)."""
    p = settings_path()
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text("utf-8"))
    except (OSError, ValueError) as e:
        raise ConfigError(f"{p} không phải JSON hợp lệ — không ghi đè. Sửa hoặc xoá file này.") from e
    if not isinstance(data, dict):
        raise ConfigError(f"{p} không phải object JSON — không ghi đè.")
    return data


def _write_settings(data: dict[str, object]) -> None:
    p = settings_path()
    if p.is_file():
        atomic_write_text(p.with_name(p.name + ".bak"), p.read_text("utf-8"), private=True)
    atomic_write_text(p, json.dumps(data, ensure_ascii=False, indent=2), private=True)


def save_settings(s: Settings) -> None:
    """Ghi toàn bộ cấu hình. Từ chối nếu file hiện có không hợp lệ (không đè mất cấu hình người dùng); giữ bản
    `settings.json.bak`."""
    load_settings()  # ném ConfigError nếu file hiện có hỏng
    _write_settings(s.model_dump())


def save_consent(consent: str) -> None:
    """Lưu một đồng ý gửi tài liệu: CHỈ sửa khoá `privacy_consents[consent]`, giữ nguyên mọi khoá khác (R1-006)."""
    load_settings()  # file hiện có phải hợp lệ
    data = _raw_settings()
    consents = data.get("privacy_consents")
    consents = dict(consents) if isinstance(consents, dict) else {}
    consents[consent] = True
    data["privacy_consents"] = consents
    Settings.model_validate(data)
    _write_settings(data)


def history_path() -> Path:
    return config_dir() / "history.json"


def load_history() -> list[dict[str, str]]:
    try:
        data = json.loads(history_path().read_text("utf-8"))
        return [d for d in data if isinstance(d, dict)][-500:] if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def add_history(entry: dict[str, str]) -> None:
    items = [h for h in load_history() if h.get("output") != entry.get("output")] + [entry]
    atomic_write_text(history_path(), json.dumps(items[-500:], ensure_ascii=False, indent=1), private=True)
