"""Test hành vi cho 2.1.3 — cô lập mạnh trên Windows (AppContainer + Job Object, L1-001 phần Windows).

Mọi test PoC chạy tiến trình con THẬT trong AppContainer (không mock confine/sandbox): tiến trình con đóng vai parser bị
chiếm quyền và thử đọc HOME thật, ghi thư mục Startup thật, kết nối TCP loopback, tra DNS, sinh tiến trình. Chỉ test
chính sách (`test_win213_appcontainer_failure_*`) mới giả lập lỗi dựng AppContainer, vì không có cách làm hỏng API đó
một cách an toàn trên máy thật."""

from __future__ import annotations

import collections
import json
import os
import secrets
import socket
import sys
import threading
from pathlib import Path

import pymupdf
import pytest
import sandbox_helpers as h

from vietpdf import cli
from vietpdf.core import confine, render, sandbox
from vietpdf.core.sandbox import discard_stage_dir, make_stage_dir, run_isolated

pytestmark = pytest.mark.skipif(
    sys.platform != "win32" or not confine.appcontainer_supported(), reason="cần Windows 10 1607+ (AppContainer)"
)


class _Listener:
    """Máy chủ TCP 127.0.0.1 do test dựng; ghi lại mọi kết nối nhận được."""

    def __init__(self) -> None:
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(5)
        self.sock.settimeout(0.5)
        self.port = self.sock.getsockname()[1]
        self.accepted = 0
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._run, daemon=True)
        self._t.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                c, _ = self.sock.accept()
            except OSError:
                continue
            self.accepted += 1
            c.close()

    def close(self) -> None:
        self._stop.set()
        self._t.join(2)
        self.sock.close()


@pytest.fixture
def real_home_canary() -> object:
    """File canary trong HOME THẬT của người dùng (%USERPROFILE%), xoá sau test."""
    home = Path(os.environ["USERPROFILE"])
    canary = home / f"vietpdf_canary_{secrets.token_hex(6)}.txt"
    canary.write_text("BÍ MẬT CỦA NGƯỜI DÙNG", encoding="utf-8")
    yield canary
    canary.unlink(missing_ok=True)


@pytest.fixture
def startup_target() -> object:
    startup = Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
    target = startup / f"poc_{secrets.token_hex(6)}.txt"
    yield target
    target.unlink(missing_ok=True)  # chỉ có khi PoC thành công (test đã đỏ)


def test_win213_child_reports_strong_isolation() -> None:
    seq = sandbox.confine_seq()
    assert run_isolated(h.add, 2, 3, timeout=60) == 5
    s = sandbox.confine_summary(seq)
    assert s["level"] == "strong" and s["platform"] == "win32", s
    ev = sandbox._EVENTS[-1]["status"]
    assert ev["appcontainer"] is True and ev["capabilities"] == [] and ev["job_object"] is True, ev
    assert "ĐẠT" in sandbox.describe_confine(s)


def test_win213_poc_child_is_confined(tmp_path: Path, real_home_canary: Path, startup_target: Path) -> None:
    """Yêu cầu 1 của prompt Windows: HOME/Startup/TCP/DNS/subprocess bị từ chối; ghi thư mục tạm của job được phép."""
    lst = _Listener()
    stage = make_stage_dir(tmp_path)
    try:
        res = run_isolated(
            h.try_win_attempts,
            str(real_home_canary),
            str(startup_target),
            str(stage),
            lst.port,
            timeout=120,
            read_files=[],
            write_dirs=[str(stage)],
        )
        accepted = lst.accepted
    finally:
        lst.close()
    try:
        print("PoC:", json.dumps(res, ensure_ascii=False))
        for key in ("read_home_canary", "write_startup", "tcp_loopback", "dns", "subprocess"):
            assert res[key].startswith("blocked"), (key, res)
        assert res["write_stage"] == "ok", res
        assert (stage / "out0.pdf").read_bytes() == b"%PDF-1.7\n"
        assert accepted == 0, "listener nhận được kết nối từ tiến trình con"
        assert not startup_target.exists()
    finally:
        discard_stage_dir(stage)


def test_win213_parent_side_control_same_attempts_succeed(
    tmp_path: Path, real_home_canary: Path, startup_target: Path
) -> None:
    """Đối chứng: cùng các thao tác chạy ở tiến trình cha (không cô lập) thì thành công — PoC thật sự kiểm được."""
    lst = _Listener()
    try:
        assert h.try_read(str(real_home_canary)) == "ok"
        assert h.try_tcp_native(lst.port) == "ok"
    finally:
        lst.close()
    assert h.try_write(str(startup_target)) == "ok"
    startup_target.unlink()


def _caps_spy(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    seen: list[str] = []
    real = confine.new_job_capability

    def spy() -> str:
        sid = real()
        seen.append(sid)
        return sid

    monkeypatch.setattr(confine, "new_job_capability", spy)
    return seen


def test_win213_job_acl_removed_after_job(tmp_path: Path, samples: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Quyền đọc PDF đầu vào và quyền ghi thư mục tạm chỉ tồn tại trong lúc tiến trình con chạy."""
    caps = _caps_spy(monkeypatch)
    pdf = tmp_path / "in.pdf"
    pdf.write_bytes((samples / "paper.pdf").read_bytes())
    stage = make_stage_dir(tmp_path)
    try:
        assert run_isolated(h.try_read, str(pdf), timeout=60, write_dirs=[str(stage)]) == "ok"
        assert caps, "không tạo capability cho job"
        for path in (pdf, stage):
            assert confine.effective_rights(str(path), caps[-1]) == 0, f"ACE của job còn trên {path}"
        # tiến trình con KHÁC (capability mới) không đọc được file đã được cấp cho job trước
        assert run_isolated(h.try_read, str(pdf), timeout=60, read_files=[]).startswith("blocked")
    finally:
        discard_stage_dir(stage)


def test_win213_hijacked_render_cannot_touch_home(
    tmp_path: Path, samples: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Như test Linux R1-001: PDF nằm trong HOME, tiến trình dựng PDF bị chiếm quyền thử đọc/ghi/xoá trong HOME."""
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    (home / ".ssh" / "id_canary").write_text("-----BEGIN CANARY KEY-----\n")
    (home / ".bashrc").write_text("# bashrc gốc\n", encoding="utf-8")
    (home / "Documents").mkdir()
    (home / "Documents" / "x.docx").write_text("luận văn", encoding="utf-8")
    pdf = home / "paper.pdf"
    pdf.write_bytes((samples / "paper.pdf").read_bytes())
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(render, "render_document", h.hijack_render)  # hook chỉ có trong test
    rc = cli.main(["translate", str(pdf), "-p", "fake", "--no-library", "--no-cache", "-q"])
    assert rc == 0
    out = home / "paper.vi.pdf"
    with pymupdf.open(out) as d:
        attempts = json.loads(d.metadata.get("keywords") or "{}")
    assert attempts and all(v.startswith("blocked") for v in attempts.values()), attempts
    assert (home / ".bashrc").read_text("utf-8") == "# bashrc gốc\n"
    assert not (home / ".config" / "autostart").exists()
    assert (home / "Documents" / "x.docx").exists()
    assert not [p for p in home.iterdir() if p.name.startswith(".vietpdf-stage-")]
    report = (home / "paper.report.md").read_text("utf-8")
    assert "Cô lập tiến trình xử lý PDF: ĐẠT" in report


def test_win213_publish_drops_child_streams_and_acl(tmp_path: Path, samples: Path) -> None:
    """Tiến trình con trong AppContainer ghi file đích kèm luồng dữ liệu phụ (ADS): file công bố chỉ có nội dung chính,
    do tiến trình chính tạo (không mang DACL/nhãn của AppContainer)."""
    stage = make_stage_dir(tmp_path)
    staged = stage / "out0.pdf"
    final = tmp_path / "final.pdf"
    try:
        res = run_isolated(
            h.write_with_ads, str(staged), str(samples / "paper.pdf"), timeout=60, write_dirs=[str(stage)]
        )
        assert res == "ok", res
        assert (stage / "out0.pdf:hidden").read_bytes() == b"payload"  # ADS thật sự được tạo
        sandbox.publish_staged(staged, final)
    finally:
        discard_stage_dir(stage)
    assert final.read_bytes() == (samples / "paper.pdf").read_bytes()
    with pytest.raises(OSError):
        Path(str(final) + ":hidden").read_bytes()
    assert confine.effective_rights(str(final), confine.appcontainer_sid()) == 0
    assert confine.effective_rights(str(final), confine.ALL_APP_PACKAGES) == 0


def test_win213_gui_render_server_in_appcontainer(samples: Path) -> None:
    seq = sandbox.confine_seq()
    pr = sandbox.PageRenderer()
    try:
        assert pr.render(str(samples / "paper.pdf"), 0, 1.0).startswith(pr.PNG_MAGIC)
    finally:
        pr.close()
    assert sandbox.confine_summary(seq)["level"] == "strong"


def test_win213_no_dangerous_library_roots() -> None:
    roots = confine.library_roots()
    assert roots and not [r for r in roots if confine.is_dangerous_root(r)], roots
    assert confine.is_dangerous_root(os.environ["USERPROFILE"])
    assert confine.is_dangerous_root("C:\\")


def _break_capability(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken() -> str:
        raise OSError(5, "DeriveCapabilitySidsFromName: WinError 5 (giả lập)")

    monkeypatch.setattr(confine, "new_job_capability", broken)
    # sự kiện "YẾU" giả lập không được lọt sang test khác (chỉ báo GUI đọc sổ sự kiện của cả tiến trình)
    monkeypatch.setattr(sandbox, "_EVENTS", collections.deque(maxlen=4096))
    monkeypatch.setattr(sandbox, "_LAST_REFUSAL", [])


def test_win213_appcontainer_failure_refuses_and_func_never_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Chính sách: không dựng được AppContainer => SandboxWeakError, func không chạy (fail-closed như Linux)."""
    _break_capability(monkeypatch)
    marker = tmp_path / "ran.txt"
    with pytest.raises(sandbox.SandboxWeakError, match="AppContainer"):
        run_isolated(h.write_marker, str(marker), timeout=60)
    assert not marker.exists()


def test_win213_appcontainer_failure_cli_exit_code_and_allow_weak(
    tmp_path: Path, samples: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _break_capability(monkeypatch)
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes((samples / "paper.pdf").read_bytes())
    argv = ["translate", str(pdf), "-p", "fake", "--no-library", "--no-cache", "-q"]
    monkeypatch.setenv("VIETPDF_ALLOW_WEAK_SANDBOX", "1")  # biến môi trường KHÔNG mở được ngoại lệ
    assert cli.main(argv) == 5
    assert not (tmp_path / "paper.vi.pdf").exists()
    try:
        assert cli.main(["--allow-weak-sandbox", *argv]) == 0
    finally:
        sandbox.set_allow_weak(False)
    report = (tmp_path / "paper.report.md").read_text("utf-8")
    assert "YẾU" in report and "AppContainer" in report
