"""Test hành vi cho các mục P2 của 2.1.2: R1-003, R1-006, R1-007, R1-008, R1-009, R1-010, R1-011, L1-012, L1-023.
Chạy tiến trình con thật khi mục liên quan tới cô lập."""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

import pytest
import sandbox_helpers as h

from vietpdf import cli
from vietpdf.core.sandbox import run_isolated

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

LINUX = sys.platform.startswith("linux")
needs_seccomp = pytest.mark.skipif(
    not LINUX or os.uname().machine not in ("x86_64", "aarch64"), reason="cần Linux x86_64/aarch64"
)


# ------------------------------------------------------------------ R1-003: mật khẩu PDF không qua argv


def test_r1003_password_flag_removed() -> None:
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["translate", "x.pdf", "--password", "secret"])
    ns = cli.build_parser().parse_args(["translate", "x.pdf", "--password-stdin"])
    assert ns.password_stdin and ns.password is None


# ------------------------------------------------------------------ R1-006: settings.json hỏng


@pytest.mark.parametrize(
    "bad",
    [
        {"provider": "ollama", "https_proxy": 8080},
        {"provider": "ollama", "privacy_consents": "openai"},
        {"provider": "ollama", "qualty": "high"},
        {"provider": "ollama", "offline": "yes"},  # bool strict: không nhận "yes"
    ],
)
def test_r1006_bad_settings_reported_and_never_overwritten(bad: dict[str, Any]) -> None:
    from vietpdf.config import settings_path

    p = settings_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    original = json.dumps({**bad, "providers": {"ollama": {"model": "qwen"}}, "style": "sat_nghia"})
    p.write_text(original)
    rc = cli.main(["translate", "x.pdf", "-p", "openai", "--accept-privacy"])
    assert rc != 0
    assert p.read_text("utf-8") == original, "file cấu hình của người dùng bị ghi đè"


def test_r1006_saving_consent_touches_only_that_key() -> None:
    from vietpdf.config import load_settings, save_consent, settings_path

    p = settings_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"provider": "ollama", "providers": {"ollama": {"model": "qwen"}}, "style": "sat_nghia"}))
    save_consent("openai@api.openai.com")
    data = json.loads(p.read_text("utf-8"))
    assert (
        data["provider"] == "ollama" and data["style"] == "sat_nghia" and data["providers"]["ollama"]["model"] == "qwen"
    )
    assert data["privacy_consents"] == {"openai@api.openai.com": True}
    assert load_settings().provider == "ollama"
    assert p.with_name("settings.json.bak").is_file()


# ------------------------------------------------------------------ R1-007: kết quả lớn


def test_r1007_result_cap_is_per_job() -> None:
    from vietpdf.core.sandbox import SandboxError

    with pytest.raises(SandboxError, match="quá lớn"):
        run_isolated(h.allocate_string, 3, timeout=60, max_result_mb=1)
    assert run_isolated(h.allocate_string, 1, timeout=60, max_result_mb=4) == 1


# ------------------------------------------------------------------ R1-008: seccomp


@needs_seccomp
def test_r1008_child_cannot_sendto_existing_unix_datagram_socket(tmp_path: Path) -> None:
    import socket

    path = str(tmp_path / "l.sock")
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    srv.bind(path)
    srv.settimeout(0.5)
    try:
        res = run_isolated(h.try_dgram_and_ns, path, timeout=30)
        assert all(v.startswith("blocked") for v in res.values()), res
        with pytest.raises(OSError):
            srv.recv(100)
    finally:
        srv.close()


# ------------------------------------------------------------------ R1-009: hộp thoại đồng ý


def test_r1009_consent_text_shows_punycode_and_warns_mixed_script() -> None:
    from vietpdf.translate.providers import privacy_text

    text = privacy_text("openai", "https://аpi.openai.com/v1")  # chữ а Kirin
    assert "xn--" in text and "CẢNH BÁO" in text


# ------------------------------------------------------------------ R1-010


def test_r1010_cache_rows_with_null_created_expire(tmp_path: Path) -> None:
    from vietpdf.translate.cache import TranslationCache

    db = tmp_path / "c.db"
    c = TranslationCache(db)
    c.close()
    con = sqlite3.connect(db)
    con.execute("INSERT INTO translations (key, value, created) VALUES ('k', 'v', NULL)")
    con.execute("INSERT INTO translations (key, value, created) VALUES ('k2', 'v', ?)", (int(time.time()),))
    con.commit()
    con.close()
    TranslationCache(db).close()
    con = sqlite3.connect(db)
    keys = {r[0] for r in con.execute("SELECT key FROM translations")}
    con.close()
    assert "k" not in keys and "k2" in keys


def test_r1010_library_migrate_requires_confirmation(monkeypatch: pytest.MonkeyPatch) -> None:
    from vietpdf.library import store as store_mod

    class FakeStore:
        encrypted = True
        migrated = False

        def plaintext_rows(self) -> int:
            return 1

        def plaintext_preview(self) -> list[tuple[str, str, str]]:
            return [("vocab", "x1", '{"term": "chèn"}')]

        def migrate_plaintext(self) -> int:
            FakeStore.migrated = True
            return 1

        def close(self) -> None:
            pass

    monkeypatch.setattr(store_mod, "open_store", FakeStore)
    assert cli.main(["library", "migrate"]) == 1 and not FakeStore.migrated
    assert cli.main(["library", "migrate", "--yes"]) == 0 and FakeStore.migrated


# ------------------------------------------------------------------ R1-011: tải mô hình sai hash


def test_r1011_bad_download_never_replaces_good_model(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import fetch_models as fm

    lock = json.loads(fm.LOCK.read_text("utf-8"))
    name, entry = next(iter(lock["models"].items()))
    out = tmp_path / "models"
    out.mkdir()
    good = out / entry["file"]
    good.write_bytes(b"mo hinh tot dang dung")

    async def fake_fetch(url: str, dest: Path) -> int:
        dest.write_bytes(b"ban tai bi tan cong")
        return 20

    monkeypatch.setattr(fm, "fetch", fake_fetch)
    monkeypatch.setattr(sys, "argv", ["fetch_models.py", "--name", name, "--out", str(out)])
    if entry.get("sha256"):
        assert fm.main() == 2
        assert good.read_bytes() == b"mo hinh tot dang dung"
    assert not [p for p in out.iterdir() if p.name.endswith(".download")]


# ------------------------------------------------------------------ L1-012: trang bị bỏ được báo


def test_l1012_dropped_page_listed_in_report(tmp_path: Path) -> None:
    import pymupdf

    from vietpdf.core.pipeline import PipelineOptions, run_pipeline
    from vietpdf.translate.fake import FakeTranslator

    d = pymupdf.open()
    d.new_page().insert_text((72, 72), "A normal page with an ordinary sentence.")
    p2 = d.new_page()
    p2.insert_text((72, 72), "A page with an absurd user unit.")
    d.xref_set_key(p2.xref, "UserUnit", "75000")
    src = tmp_path / "u.pdf"
    d.save(str(src))
    res = asyncio.run(run_pipeline(src, None, FakeTranslator(), PipelineOptions(auto_terms=False)))
    report = res.report_path.read_text("utf-8")
    assert "Phần bị bỏ do dữ liệu PDF không hợp lệ" in report and "Trang 2" in report


# ------------------------------------------------------------------ L1-023


def test_l1023_audit_catches_sys_modules_subscript(tmp_path: Path) -> None:
    import security_audit as sa

    f = tmp_path / "bad.py"
    f.write_text('import sys\nsys.modules["os"].system("id")\nsys.modules["subprocess"].run(["id"])\n')
    assert sa.main([str(tmp_path)]) == 1


# ------------------------------------------------------------------ L1-024: build backend có hash


def test_l1024_build_constraints_have_hashes_and_ci_uses_them(tmp_path: Path) -> None:
    import check_workflows as cw

    assert cw.check_build_constraints(ROOT / "build-constraints.txt") == []
    bad = tmp_path / "build-constraints.txt"
    bad.write_text("hatchling==1.32.0\n")
    assert cw.check_build_constraints(bad)
    assert cw.main() == 0
