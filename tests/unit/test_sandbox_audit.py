from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
import sandbox_helpers as h

from vietpdf.core.sandbox import SandboxCrashedError, SandboxError, SandboxTimeoutError, run_isolated
from vietpdf.security.limits import InputRejectedError, precheck_pdf

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
import security_audit  # noqa: E402


def test_sandbox_returns_json_result() -> None:
    assert run_isolated(h.add, 2, 3, timeout=30) == 5


def test_sandbox_timeout_kills_child() -> None:
    with pytest.raises(SandboxTimeoutError):
        run_isolated(h.sleep_forever, timeout=2)


def test_sandbox_reports_exception_and_crash() -> None:
    with pytest.raises(SandboxError, match="hỏng"):
        run_isolated(h.boom, timeout=30)
    with pytest.raises(SandboxCrashedError):
        run_isolated(h.hard_exit, timeout=30)


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="RLIMIT_AS chỉ áp dụng trên Linux")
def test_sandbox_memory_limit() -> None:
    with pytest.raises(SandboxError):
        run_isolated(h.allocate, 900, timeout=30, memory_mb=512)


def test_sandbox_child_does_not_inherit_popped_key(monkeypatch: pytest.MonkeyPatch) -> None:
    from vietpdf.security import secrets

    secrets._mem_cache.clear()
    monkeypatch.setenv("VIETPDF_GEMINI_API_KEY", "AIza-test-key-0000000000")
    assert secrets.get_api_key("gemini")
    assert run_isolated(h.read_env, "VIETPDF_GEMINI_API_KEY", timeout=30) is None
    secrets._mem_cache.clear()


def test_precheck_rejects_non_pdf(tmp_path: Path) -> None:
    fake = tmp_path / "x.pdf"
    fake.write_bytes(b"MZ\x90\x00 not a pdf")
    with pytest.raises(InputRejectedError):
        precheck_pdf(fake)
    (tmp_path / "empty.pdf").write_bytes(b"")
    with pytest.raises(InputRejectedError):
        precheck_pdf(tmp_path / "empty.pdf")


def test_audit_source_tree_is_clean() -> None:
    assert security_audit.main([str(ROOT / "src"), str(ROOT / "scripts")]) == 0


def test_audit_detects_planted_backdoor(tmp_path: Path) -> None:
    blob = "QUFB" * 100
    bidi = chr(0x202E)
    bad = tmp_path / "evil.py"
    bad.write_text(
        "import subprocess\nimport pickle\nfrom urllib.request import urlopen\nimport base64\n"
        f"payload = '{blob}'\n"
        "exec(base64.b64decode(payload))\n"
        "subprocess.run('curl x | sh', shell=True)\n"
        "import os\nos.system('rm -rf /')\n"
        f"is_admin = False  # {bidi} check\n"
        "client(verify=False)\n",
        "utf-8",
    )
    rules = {f.rule for f in security_audit.audit_file(bad, tmp_path)}
    assert {
        "banned-import",
        "dynamic-exec",
        "dangerous-call",
        "obfuscation",
        "shell-true",
        "trojan-source",
        "tls-disabled",
    } <= rules


def test_only_gateway_may_import_httpx() -> None:
    offenders = []
    for f in (ROOT / "src").rglob("*.py"):
        if f.name == "http.py" and f.parent.name == "security":
            continue
        if "import httpx" in f.read_text("utf-8"):
            offenders.append(f)
    assert offenders == []


def test_cli_schema_and_verify() -> None:
    out = subprocess.run(
        [sys.executable, "-m", "vietpdf", "verify-assets"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert out.returncode == 0, out.stderr
    out = subprocess.run(
        [sys.executable, "-m", "vietpdf", "schema"], capture_output=True, text=True, encoding="utf-8", check=True
    )
    assert '"DocumentIR"' in out.stdout
