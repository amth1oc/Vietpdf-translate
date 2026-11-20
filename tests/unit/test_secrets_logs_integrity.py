from __future__ import annotations

import logging
import shutil
from pathlib import Path

import pytest

from vietpdf.security import secrets
from vietpdf.security.integrity import ASSETS_DIR, IntegrityError, verify_assets
from vietpdf.security.logsafe import RedactingFilter, redact, register_secret


def test_redact_known_key_formats() -> None:
    s = "key=sk-proj-ABCDEFGHIJKLMNOPQRSTUV and AIzaSyA1234567890abcdefghijklmnopqrstu Bearer abc.def.ghijkl"
    out = redact(s)
    assert "sk-proj-ABC" not in out and "AIzaSy" not in out and "abc.def" not in out


def test_redact_registered_secret_and_log_filter(caplog: pytest.LogCaptureFixture) -> None:
    register_secret("my-very-secret-value")
    logger = logging.getLogger("t")
    logger.addFilter(RedactingFilter())
    with caplog.at_level(logging.INFO):
        logger.info("calling with %s", "my-very-secret-value")
    assert "my-very-secret-value" not in caplog.text


class _FakeBackend:
    __module__ = "keyrings.alt.file"


class _Spoofed:
    # 2.1.0 chỉ so tiền tố __module__ nên lớp giả mạo này được coi là "an toàn" (L1-022)
    __module__ = "keyring.backends.SecretService"


def test_insecure_keyring_backends_rejected() -> None:
    assert not secrets.backend_is_secure(_FakeBackend())
    assert not secrets.backend_is_secure(_Spoofed())
    assert all(secrets.backend_is_secure(object.__new__(c)) for c in secrets._platform_backend_classes())


def test_env_key_is_removed_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    import os

    secrets._mem_cache.clear()
    monkeypatch.setenv("VIETPDF_OPENAI_API_KEY", "sk-test-1234567890abcdefgh")  # gitleaks:allow (khoá giả cho test)
    assert secrets.get_api_key("openai") == "sk-test-1234567890abcdefgh"
    assert "VIETPDF_OPENAI_API_KEY" not in os.environ  # tiến trình con sẽ không thừa hưởng
    secrets._mem_cache.clear()


def test_invalid_provider_name() -> None:
    with pytest.raises(ValueError):
        secrets.get_api_key("../etc")


def test_assets_verified() -> None:
    verify_assets()


def _copy_assets(tmp: Path) -> Path:
    d = tmp / "assets"
    shutil.copytree(ASSETS_DIR, d)
    return d


def test_tampered_asset_detected(tmp_path: Path) -> None:
    d = _copy_assets(tmp_path)
    with (d / "glossary" / "cntt_ai.csv").open("a") as f:
        f.write("evil,thing\n")
    with pytest.raises(IntegrityError, match="checksum"):
        verify_assets(d)


def test_unlisted_file_detected(tmp_path: Path) -> None:
    d = _copy_assets(tmp_path)
    (d / "fonts" / "planted.ttf").write_bytes(b"x")
    with pytest.raises(IntegrityError, match="không được khai báo"):
        verify_assets(d)


def test_path_traversal_in_sums_rejected(tmp_path: Path) -> None:
    d = _copy_assets(tmp_path)
    with (d / "SHA256SUMS").open("a") as f:
        f.write("0" * 64 + "  ../../etc/passwd\n")
    with pytest.raises(IntegrityError, match="ngoài thư mục"):
        verify_assets(d)
