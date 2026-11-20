"""Test hành vi cho 2.1.2 — R1-001 (tiến trình dựng PDF chỉ ghi được thư mục tạm riêng của job) và R1-002 (cô lập
fail-closed, báo mức cô lập về tiến trình cha). Mọi test chạy tiến trình con THẬT (không mock confine/sandbox).

Các test viết sao cho đỏ trên 2.1.1 vì HÀNH VI (HOME bị ghi, func vẫn chạy khi thiếu Landlock, tín hiệu tới được tiến
trình cha...), chỉ dùng API công khai đã có ở 2.1.1 (`cli.main`, `run_isolated`, `render.render_document`) trừ các test
về chính API mới (`publish_staged`, `make_stage_dir`) được đánh dấu rõ."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pymupdf
import pytest
import sandbox_helpers as h

from vietpdf import cli
from vietpdf.core import render
from vietpdf.core.sandbox import run_isolated

LINUX = sys.platform.startswith("linux")


def _landlock_abi() -> int:
    if not LINUX:
        return 0
    import ctypes

    return int(ctypes.CDLL(None, use_errno=True).syscall(444, None, ctypes.c_size_t(0), 1))


needs_landlock = pytest.mark.skipif(_landlock_abi() < 1, reason="cần Linux có Landlock + seccomp")
needs_seccomp_arch = pytest.mark.skipif(
    not LINUX or os.uname().machine not in ("x86_64", "aarch64"), reason="cần Linux x86_64/aarch64"
)


def _make_home(home: Path) -> None:
    (home / ".ssh").mkdir(parents=True)
    (home / ".ssh" / "id_canary").write_text("-----BEGIN CANARY KEY-----\n")
    (home / ".bashrc").write_text("# bashrc gốc\n", encoding="utf-8")
    (home / "Documents").mkdir()
    (home / "Documents" / "x.docx").write_text("luận văn", encoding="utf-8")


def _assert_home_untouched(home: Path) -> None:
    assert (home / ".bashrc").read_text("utf-8") == "# bashrc gốc\n", ".bashrc bị ghi"
    assert not (home / ".config" / "autostart" / "pwn.desktop").exists(), "autostart bị tạo"
    assert (home / "Documents" / "x.docx").exists(), "tài liệu bị xoá"
    assert not (home / ".config" / "vietpdf" / "models.lock.json").exists(), "models.lock.json bị ghi"


def _hijacked_translate(
    monkeypatch: pytest.MonkeyPatch, home: Path, pdf: Path, out: Path | None
) -> tuple[int, dict[str, str]]:
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(render, "render_document", h.hijack_render)  # hook chỉ có trong test
    argv = ["translate", str(pdf), "-p", "fake", "--no-library", "--no-cache", "-q"]
    if out is not None:
        argv += ["-o", str(out)]
    rc = cli.main(argv)
    final = out or pdf.with_name(pdf.stem + ".vi.pdf")
    assert final.is_file(), "file đích phải được tạo đúng"
    with pymupdf.open(final) as d:
        attempts = json.loads(d.metadata.get("keywords") or "{}")
    return rc, attempts


# ------------------------------------------------------------------------------------------- R1-001


@needs_landlock
def test_r1001_pdf_in_home_render_child_cannot_touch_home(
    tmp_path: Path, samples: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PDF nằm NGAY trong HOME, đầu ra mặc định cạnh PDF: tiến trình dựng PDF bị chiếm vẫn không đọc/ghi/xoá được gì
    trong HOME; file đích vẫn được tạo đúng."""
    home = tmp_path / "home"
    _make_home(home)
    pdf = home / "paper.pdf"
    pdf.write_bytes((samples / "paper.pdf").read_bytes())
    rc, attempts = _hijacked_translate(monkeypatch, home, pdf, None)
    assert rc == 0
    assert attempts and all(v.startswith("blocked") for v in attempts.values()), attempts
    _assert_home_untouched(home)
    assert not [p for p in home.iterdir() if p.name.startswith(".vietpdf-stage-")], "thư mục tạm của job còn sót"


@needs_landlock
def test_r1001_output_in_home_render_child_cannot_touch_home(
    tmp_path: Path, samples: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Người dùng chọn đầu ra ở HOME (PDF nằm chỗ khác)."""
    home = tmp_path / "home"
    _make_home(home)
    dl = tmp_path / "dl"
    dl.mkdir()
    pdf = dl / "paper.pdf"
    pdf.write_bytes((samples / "paper.pdf").read_bytes())
    rc, attempts = _hijacked_translate(monkeypatch, home, pdf, home / "out.vi.pdf")
    assert rc == 0
    assert attempts and all(v.startswith("blocked") for v in attempts.values()), attempts
    _assert_home_untouched(home)


@pytest.mark.parametrize("where", ["home", "root"])
def test_r1001_run_isolated_refuses_user_dirs_as_write_dirs(tmp_path: Path, where: str) -> None:
    """Bất biến: write_dirs là HOME hay `/` => SandboxError, func không chạy."""
    target = str(Path.home()) if where == "home" else "/"
    marker = tmp_path / "ran"
    with pytest.raises(Exception, match="thư mục tạm của job"):
        run_isolated(h.write_marker, str(marker), timeout=30, write_dirs=[target])
    assert not marker.exists()


@needs_landlock
def test_r1001_child_cannot_plant_symlink_fifo_hardlink(tmp_path: Path) -> None:
    """Tiến trình con cố để lại symlink/FIFO/hardlink làm file đầu ra: Landlock chặn tạo (API mới của 2.1.2)."""
    from vietpdf.core.sandbox import discard_stage_dir, make_stage_dir

    stage = make_stage_dir(tmp_path)
    try:
        res = run_isolated(h.try_special_files, str(stage), timeout=30, write_dirs=[str(stage)])
        assert all(v.startswith("blocked") for v in res.values()), res
    finally:
        discard_stage_dir(stage)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="POSIX")
@pytest.mark.parametrize("kind", ["symlink", "fifo", "hardlink", "outside", "dir"])
def test_r1001_publish_staged_rejects_non_regular_outputs(tmp_path: Path, kind: str) -> None:
    """Kể cả khi Landlock không chặn (cô lập yếu được cho phép), tiến trình chính không công bố symlink, FIFO,
    hardlink, file ngoài thư mục tạm hay thư mục (API mới của 2.1.2)."""
    from vietpdf.core.sandbox import SandboxError, discard_stage_dir, make_stage_dir, publish_staged

    secret = tmp_path / "secret.txt"
    secret.write_text("bí mật", encoding="utf-8")
    stage = make_stage_dir(tmp_path)
    staged = stage / "out0.pdf"
    if kind == "symlink":
        staged.symlink_to(secret)
    elif kind == "fifo":
        os.mkfifo(staged)
    elif kind == "hardlink":
        os.link(secret, staged)
    elif kind == "outside":
        staged = secret
    else:
        staged.mkdir()
    final = tmp_path / "final.pdf"
    try:
        with pytest.raises(SandboxError):
            publish_staged(staged, final)
        assert not final.exists()
    finally:
        discard_stage_dir(stage)
    assert secret.read_text("utf-8") == "bí mật"


def test_r1001_publish_staged_scans_and_makes_private(tmp_path: Path, samples: Path) -> None:
    """File hợp lệ: công bố với quyền 0600. PDF còn JavaScript: bị từ chối bởi lần quét trong tiến trình con mới."""
    from vietpdf.core.sandbox import SandboxError, discard_stage_dir, make_stage_dir, publish_staged

    stage = make_stage_dir(tmp_path)
    try:
        good = stage / "out0.pdf"
        good.write_bytes((samples / "paper.pdf").read_bytes())
        os.chmod(good, 0o644)
        final = tmp_path / "ok.pdf"
        publish_staged(good, final)
        assert final.is_file() and (final.stat().st_mode & 0o777 == 0o600 or os.name != "posix")
        bad = stage / "out1.pdf"
        bad.write_bytes((samples / "js.pdf").read_bytes())
        with pytest.raises(SandboxError, match="ActiveContentError"):
            publish_staged(bad, tmp_path / "bad.pdf")
        assert not (tmp_path / "bad.pdf").exists()
    finally:
        discard_stage_dir(stage)


def test_r1001_stale_stage_dirs_cleaned_at_startup(tmp_path: Path) -> None:
    """Lần chạy trước bị kill để lại `.vietpdf-stage-*`: khởi động lại thì được dọn; thư mục lạ không bị đụng."""
    code = textwrap.dedent(
        f"""
        import os, sys
        sys.path[:0] = {sys.path!r}
        from pathlib import Path
        from vietpdf.core.sandbox import make_stage_dir
        d = make_stage_dir(Path({str(tmp_path)!r}))
        (d / "out0.pdf").write_text("dở", encoding="utf-8")
        os._exit(9)  # mô phỏng bị kill: không dọn
        """
    )
    subprocess.run([sys.executable, "-c", code], check=False, timeout=60)  # noqa: S603 - mã test cố định
    left = [p for p in tmp_path.iterdir() if p.name.startswith(".vietpdf-stage-")]
    assert len(left) == 1
    foreign = tmp_path / ".vietpdf-stage-khong-cua-app"
    foreign.mkdir()
    from vietpdf.cli import startup_hardening

    startup_hardening()
    assert not left[0].exists()
    assert foreign.exists()


# ------------------------------------------------------------------------------------------- R1-002


def _run_without_landlock(tmp_path: Path, body: str, block_nnp: bool = False) -> subprocess.CompletedProcess[str]:
    """Chạy `body` trong một tiến trình con của pytest đã cài bộ lọc seccomp ENOSYS cho landlock_* (kế thừa sang mọi
    tiến trình con của app) — mô phỏng kernel không có Landlock (cách của repro_r/l1_001/noll)."""
    code = (
        f"import sys; sys.path[:0] = {sys.path!r}\n"
        "import json, os\n"
        "import sandbox_helpers as h\n"
        f"h.install_no_landlock(block_nnp={block_nnp!r})\n" + textwrap.dedent(body)
    )
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "LANG") or k.startswith("VIETPDF_")}
    return subprocess.run(  # noqa: S603 - mã test cố định
        [sys.executable, "-c", code],
        check=False,
        capture_output=True,
        text=True,
        timeout=180,
        env=env,
        cwd=str(tmp_path),
        start_new_session=True,  # kill(0, ...) nếu lọt chỉ trúng nhóm tiến trình của test này
    )


@needs_seccomp_arch
def test_r1002_missing_landlock_refuses_and_func_never_runs(tmp_path: Path) -> None:
    marker = tmp_path / "ran.txt"
    r = _run_without_landlock(
        tmp_path,
        f"""
        from vietpdf.core.sandbox import run_isolated
        try:
            out = run_isolated(h.write_marker, {str(marker)!r}, timeout=60)
            print("RESULT", out)
        except Exception as e:
            print("REFUSED", type(e).__name__, e)
        """,
    )
    assert "REFUSED SandboxWeakError" in r.stdout, r.stdout + r.stderr
    assert "Landlock" in r.stdout
    assert not marker.exists(), "func đã chạy dù cô lập yếu"


@needs_seccomp_arch
def test_r1002_failed_no_new_privs_refuses(tmp_path: Path) -> None:
    marker = tmp_path / "ran.txt"
    r = _run_without_landlock(
        tmp_path,
        f"""
        from vietpdf.core.sandbox import run_isolated
        try:
            run_isolated(h.write_marker, {str(marker)!r}, timeout=60)
            print("RESULT ran")
        except Exception as e:
            print("REFUSED", type(e).__name__, e)
        """,
        block_nnp=True,
    )
    assert "REFUSED SandboxWeakError" in r.stdout and "no_new_privs" in r.stdout, r.stdout + r.stderr
    assert not marker.exists()


@needs_seccomp_arch
def test_r1002_unknown_arch_refuses(tmp_path: Path) -> None:
    """Kiến trúc chưa có bộ lọc seccomp: status thật do confine_child_linux trả về (chạy trong tiến trình nháp với
    bảng kiến trúc rỗng) bị chính sách từ chối."""
    code = textwrap.dedent(
        f"""
        import sys, os, json; sys.path[:0] = {sys.path!r}
        from vietpdf.core import confine
        confine._ARCH.clear()
        print(json.dumps(confine.confine_child_linux(os.getppid(), [], [])))
        """
    )
    r = subprocess.run([sys.executable, "-c", code], check=False, capture_output=True, text=True, timeout=60)  # noqa: S603
    status = json.loads(r.stdout.strip().splitlines()[-1])
    from vietpdf.core.sandbox import evaluate_confine

    problems, blocking = evaluate_confine(status, {"landlock_abi": _landlock_abi(), "seccomp": True})
    assert blocking and any("seccomp" in p for p in problems), problems


@needs_seccomp_arch
def test_r1002_signals_to_parent_and_group_blocked_without_landlock(tmp_path: Path) -> None:
    r = _run_without_landlock(
        tmp_path,
        """
        from vietpdf.core import sandbox
        getattr(sandbox, "set_allow_weak", lambda v: None)(True)  # người dùng cho phép cô lập yếu
        print("SIG", json.dumps(sandbox.run_isolated(h.try_signals, timeout=60)))
        """,
    )
    assert r.returncode == 0, f"tiến trình cha bị tín hiệu giết (rc={r.returncode}): {r.stderr[-500:]}"
    line = next(x for x in r.stdout.splitlines() if x.startswith("SIG "))
    res = json.loads(line[4:])
    assert res["kill_parent"].startswith("blocked") and res["kill_group"].startswith("blocked"), res
    assert res["kill_self_0"] == "ok"


@needs_seccomp_arch
def test_r1002_allow_weak_runs_and_report_says_weak(tmp_path: Path, samples: Path) -> None:
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes((samples / "paper.pdf").read_bytes())
    r = _run_without_landlock(
        tmp_path,
        f"""
        from vietpdf import cli
        from vietpdf.core import sandbox
        getattr(sandbox, "set_allow_weak", lambda v: None)(True)  # như khi người dùng chọn 'Tiếp tục'
        print("RC", cli.main(["translate", {str(pdf)!r}, "-p", "fake", "--no-library", "--no-cache", "-q"]))
        """,
    )
    assert "RC 0" in r.stdout, r.stdout + r.stderr
    report = (tmp_path / "paper.report.md").read_text("utf-8")
    assert "YẾU" in report and "Landlock" in report
    assert "YẾU" in r.stderr  # CLI luôn cảnh báo khi cô lập yếu


@needs_seccomp_arch
def test_r1002_cli_flag_allow_weak_sandbox(tmp_path: Path, samples: Path) -> None:
    """Cờ CLI mới của 2.1.2 (ngoại lệ do người dùng chọn); biến môi trường KHÔNG mở được ngoại lệ."""
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes((samples / "paper.pdf").read_bytes())
    r = _run_without_landlock(
        tmp_path,
        f"""
        os.environ["VIETPDF_ALLOW_WEAK_SANDBOX"] = "1"
        from vietpdf import cli
        print("RC1", cli.main(["translate", {str(pdf)!r}, "-p", "fake", "--no-library", "--no-cache", "-q"]))
        print("RC2", cli.main(["--allow-weak-sandbox", "translate", {str(pdf)!r}, "-p", "fake", "--no-library",
                              "--no-cache", "-q"]))
        """,
    )
    assert "RC1 5" in r.stdout and "RC2 0" in r.stdout, r.stdout + r.stderr


@needs_seccomp_arch
def test_r1002_cli_without_flag_exits_with_dedicated_code(tmp_path: Path, samples: Path) -> None:
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes((samples / "paper.pdf").read_bytes())
    r = _run_without_landlock(
        tmp_path,
        f"""
        from vietpdf import cli
        print("RC", cli.main(["translate", {str(pdf)!r}, "-p", "fake", "--no-library", "--no-cache", "-q"]))
        """,
    )
    assert "RC 5" in r.stdout, r.stdout + r.stderr
    assert not (tmp_path / "paper.vi.pdf").exists()


@needs_seccomp_arch
def test_r1002_gui_render_server_follows_same_policy(tmp_path: Path, samples: Path) -> None:
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes((samples / "paper.pdf").read_bytes())
    r = _run_without_landlock(
        tmp_path,
        f"""
        from vietpdf.core.sandbox import PageRenderer
        pr = PageRenderer()
        try:
            pr.render({str(pdf)!r}, 0, 1.0)
            print("RENDERED")
        except Exception as e:
            print("REFUSED", type(e).__name__)
        finally:
            pr.close()
        """,
    )
    assert "REFUSED SandboxWeakError" in r.stdout, r.stdout + r.stderr


@needs_landlock
def test_r1002_signals_blocked_with_landlock_too() -> None:
    res = run_isolated(h.try_signals, timeout=60)
    assert res["kill_parent"].startswith("blocked") and res["kill_group"].startswith("blocked"), res


@needs_landlock
def test_r1002_cli_verbose_prints_isolation_level(
    tmp_path: Path, samples: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes((samples / "paper.pdf").read_bytes())
    assert cli.main(["-v", "translate", str(pdf), "-p", "fake", "--no-library", "--no-cache", "-q"]) == 0
    err = capfd.readouterr().err
    assert "Cô lập tiến trình xử lý PDF: ĐẠT" in err
    assert "Cô lập tiến trình xử lý PDF: ĐẠT" in (tmp_path / "paper.report.md").read_text("utf-8")


def test_r1002_gui_shows_isolation_indicator() -> None:
    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from vietpdf.core.sandbox import predicted_confine
    from vietpdf.gui.app import MainWindow

    app = QApplication.instance() or QApplication([])
    w = MainWindow()
    text = w.isolation_label.text()
    assert "Cô lập" in text
    assert ("YẾU" in text) == (predicted_confine()["level"] != "strong")
    w.close()
    app.processEvents()
