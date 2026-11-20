"""Chạy việc phân tích/dựng PDF trong tiến trình con cô lập.

Lý do: file PDF là dữ liệu không tin cậy, và parser PDF (MuPDF, viết bằng C) là bề mặt tấn công lớn.
- Dùng `spawn` (không `fork`): tiến trình con là trình thông dịch mới, KHÔNG thừa hưởng bộ nhớ của tiến trình chính.
- Môi trường của tiến trình con là môi trường TỐI GIẢN (không khoá, token, proxy, HOME) — `confine.minimal_env`.
- Tiến trình con tự khoá mình ở mức hệ điều hành trước khi đụng dữ liệu (`core/confine.py`): Linux — PDEATHSIG,
  seccomp (không mạng, không exec, không sinh tiến trình, không gửi tín hiệu ra ngoài), Landlock (chỉ đọc file đầu
  vào + thư viện, chỉ ghi THƯ MỤC TẠM RIÊNG của job); Windows (2.1.3) — AppContainer không capability mạng (cùng
  giới hạn file như Landlock, theo ACL) + Job Object gán khi còn SUSPENDED; macOS — chưa có.
- Fail-closed (2.1.2, R1-002): tiến trình con gửi mức cô lập về TRƯỚC khi chạy `func`, rồi chờ tín hiệu "go". Tiến
  trình cha so với chính sách và với khả năng kernel tự đo; không đạt thì không gửi "go", kill và ném
  `SandboxWeakError` — trừ khi người dùng chủ động cho phép (CLI `--allow-weak-sandbox`, hộp thoại GUI). Không đọc
  lựa chọn này từ biến môi trường.
- Ghi file (2.1.2, R1-001): `write_dirs` CHỈ được là thư mục tạm của job do `make_stage_dir` tạo (bất biến, không tắt
  được). Tiến trình chính kiểm file trong đó (`publish_staged`: lstat/fstat, file thường, 1 link, đúng chủ, kích
  thước, quét nội dung chủ động trong một tiến trình con MỚI chỉ có quyền đọc) rồi mới chuyển sang đích.
- Kết quả trả về dưới dạng **JSON** (không pickle), đọc bằng `safe_json_loads`.
- Giới hạn bộ nhớ: RLIMIT_AS (Linux) / Job Object (Windows) + giám sát RSS bằng psutil; quá thời gian thì kill.
- Huỷ: `cancel_all()` (Ctrl+C, SIGTERM, đóng GUI) kill ngay mọi tiến trình con đang chạy.
- Khoá mạng mức Python giữ lại như lớp phòng thủ cuối.
"""

from __future__ import annotations

import base64
import collections
import contextlib
import errno
import io
import json
import logging
import multiprocessing as mp
import os
import shutil
import stat
import sys
import tempfile
import threading
import traceback
from collections.abc import Callable, Iterable, Iterator
from multiprocessing.connection import Connection
from multiprocessing.context import SpawnProcess
from pathlib import Path
from typing import Any

from vietpdf.core import confine as _cf
from vietpdf.core.confine import WindowsJob, confine_child_linux, minimal_env, probe_kernel
from vietpdf.security.http import safe_json_loads

log = logging.getLogger(__name__)

_WIN = sys.platform == "win32"
_CTX = mp.get_context("spawn")
_START_LOCK = threading.Lock()  # os.environ được thay tạm thời khi start tiến trình con
_LIVE: set[Any] = set()
_LIVE_LOCK = threading.Lock()
_GENERATION = [0]  # tăng mỗi lần cancel_all(): mọi việc bắt đầu ở thế hệ cũ bị huỷ
_STOPPED = threading.Event()  # cancel_all(stop_new=True): app đang dừng => không khởi động tiến trình con mới nữa


class SandboxError(Exception):
    """Lỗi xảy ra trong tiến trình con."""


class SandboxTimeoutError(SandboxError):
    pass


class SandboxCrashedError(SandboxError):
    pass


class SandboxMemoryError(SandboxError):
    pass


class SandboxCancelledError(SandboxError):
    pass


class SandboxWeakError(SandboxError):
    """Mức cô lập tiến trình con thấp hơn chính sách: không xử lý dữ liệu (R1-002)."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = list(problems)
        super().__init__(
            "Cô lập tiến trình con yếu hơn chính sách, đã dừng trước khi mở dữ liệu: "
            + "; ".join(problems)
            + ". Nếu chấp nhận rủi ro cho riêng lần chạy này: CLI `--allow-weak-sandbox`, GUI chọn 'Tiếp tục'."
        )


def cancel_all(stop_new: bool = False) -> None:
    """Huỷ mọi việc ĐANG chạy trong tiến trình con (Ctrl+C, SIGTERM, đóng cửa sổ). Gọi được từ signal handler.

    `stop_new=True` (app đang thoát): việc bắt đầu SAU đó cũng bị từ chối ngay — nếu không, các khối trang còn xếp hàng
    trong ThreadPoolExecutor sẽ lần lượt khởi động tiến trình con mới trong lúc app đang dừng."""
    if stop_new:
        _STOPPED.set()
    _GENERATION[0] += 1
    with _LIVE_LOCK:
        procs = list(_LIVE)
    for p in procs:
        with contextlib.suppress(Exception):
            p.kill()


@contextlib.contextmanager
def _minimal_environ() -> Iterator[None]:
    """multiprocessing (spawn) truyền os.environ hiện tại cho tiến trình con: tạm GỠ các biến ngoài danh sách cho phép.

    Chỉ gỡ biến không cần (token, proxy, chứng chỉ...) rồi đặt lại ngay; biến được phép (HOME, PATH, XDG_*...) không
    bao giờ biến mất, nên luồng khác đang đọc đường dẫn cấu hình không thấy môi trường rỗng. Bí mật của chính app đã
    được `capture_env_secrets()` gỡ hẳn từ lúc khởi động."""
    from vietpdf.security.secrets import capture_env_secrets  # noqa: PLC0415

    with _START_LOCK:
        capture_env_secrets()  # phòng khi bị gọi từ mã không đi qua main() (test, thư viện)
        keep = minimal_env(dict(os.environ))
        removed = {k: v for k, v in os.environ.items() if k not in keep}
        try:
            for k in removed:
                os.environ.pop(k, None)
            yield
        finally:
            for k, v in removed.items():
                os.environ.setdefault(k, v)


class _WinPrep:
    """Windows (2.1.3): chuẩn bị quyền file cho MỘT tiến trình con trong AppContainer.

    - thư viện Python/app (gồm thư mục module của hàm chạy): ĐỌC, cấp cho SID của AppContainer (`confine.py`);
    - `reads` (file đầu vào): ĐỌC, `writes` (thư mục tạm của job): GHI — cấp cho capability SID ngẫu nhiên MỚI của
      riêng tiến trình con này; `close()` gỡ sau khi tiến trình con đã dừng.
    Lỗi nào cũng được ghi vào `errors` (=> cô lập yếu, fail-closed theo chính sách), không nuốt."""

    def __init__(self, func: Callable[..., Any] | None, reads: Iterable[str], writes: Iterable[str]) -> None:
        self.errors: list[str] = []
        self.capability: str | None = None
        self._granted: list[str] = []
        if not _cf.appcontainer_supported():
            self.errors.append("Windows này không hỗ trợ AppContainer (cần Windows 10 1607 trở lên)")
            return
        try:
            mod = sys.modules.get(getattr(func, "__module__", "") or "")
            mod_file = getattr(mod, "__file__", None)
            extra = [os.path.dirname(os.path.abspath(mod_file))] if isinstance(mod_file, str) else []
            self.errors += _cf.ensure_library_access(_cf.library_roots(extra))
            self.capability = _cf.new_job_capability()
            for f in reads:
                if os.path.isfile(f):
                    self._grant(f, _cf.FILE_READ_EXEC, inherit=False)
            for d in writes:
                self._grant(d, _cf.FILE_MODIFY, inherit=True)
        except Exception as e:  # mọi lỗi => cô lập yếu, quyết định theo chính sách (fail-closed), không nuốt
            self.errors.append(f"AppContainer: {type(e).__name__}: {e}")

    def _grant(self, path: str, access: int, inherit: bool) -> None:
        if self.capability is None:
            raise OSError(0, "chưa có capability của job")
        _cf.set_acl(path, self.capability, access, _cf.ACL_GRANT, inherit)
        self._granted.append(path)

    @property
    def appcontainer(self) -> bool:
        return not self.errors

    def close(self) -> None:
        """Gỡ mọi ACE của capability này (kể cả ACE kế thừa trên file tiến trình con đã tạo trong thư mục tạm)."""
        while self._granted:
            path = self._granted.pop()
            try:
                _cf.set_acl(path, str(self.capability), 0, _cf.ACL_REVOKE, False)
            except OSError as e:
                if os.path.exists(path):
                    log.warning("Không gỡ được quyền tạm của tiến trình con trên %s: %s", path, e)


if sys.platform == "win32":

    def _win_popen(process_obj: Any) -> Any:
        """multiprocessing `spawn` trên Windows, nhưng tạo tiến trình bằng `confine.WindowsSandboxProcess`
        (AppContainer, SUSPENDED + Job Object, chỉ kế thừa đúng handle pipe khởi động). Tiến trình con KHÔNG phải mở
        tiến trình cha (`spawn_main` không có parent_pid) — AppContainer không được phép làm điều đó."""
        import msvcrt  # noqa: PLC0415
        from multiprocessing import popen_spawn_win32, reduction, spawn, util  # noqa: PLC0415
        from multiprocessing.context import set_spawning_popen  # noqa: PLC0415

        class _Popen(popen_spawn_win32.Popen):
            def __init__(self, process_obj: Any) -> None:  # không gọi __init__ của lớp cha (tự tạo tiến trình)
                appcontainer, capability, memory_mb = process_obj._vietpdf_launch
                prep_data = spawn.get_preparation_data(process_obj._name)
                # Không chạy lại script `__main__` của tiến trình cha trong tiến trình con (AppContainer không đọc
                # được, và không nên đọc, script/thư mục làm việc của người dùng): hàm chạy luôn là hàm cấp module của
                # một module import được (yêu cầu sẵn có của `run_isolated`).
                prep_data.pop("init_main_from_path", None)
                prep_data.pop("init_main_from_name", None)
                rfd, wfd = os.pipe()
                try:
                    rh = msvcrt.get_osfhandle(rfd)
                    os.set_handle_inheritable(rh, True)
                    cmd = spawn.get_command_line(pipe_handle=rh)
                    exe = (
                        sys.executable
                        if getattr(sys, "frozen", False)
                        else getattr(sys, "_base_executable", sys.executable)
                    )
                    cmd[0] = exe
                    vp = _cf.WindowsSandboxProcess(
                        exe, " ".join(f'"{x}"' for x in cmd), dict(os.environ), None, [rh], memory_mb, appcontainer,
                        capability,
                    )  # fmt: skip
                except BaseException:
                    os.close(wfd)
                    raise
                finally:
                    os.close(rfd)
                self.vp = vp
                self.pid = vp.pid
                self.returncode = None
                self._handle = vp.process_handle
                self.sentinel = vp.process_handle
                self.finalizer = util.Finalize(self, _cf.close_handle, (self.sentinel,))
                with open(wfd, "wb", closefd=True) as to_child:
                    set_spawning_popen(self)
                    try:
                        _ChildPickler(to_child, None, child_pid=vp.pid).dump(prep_data)
                        _ChildPickler(to_child, None, child_pid=vp.pid).dump(process_obj)
                    finally:
                        set_spawning_popen(None)

        from multiprocessing.connection import (  # type: ignore[attr-defined]  # noqa: PLC0415
            PipeConnection,
            rebuild_pipe_connection,
        )

        class _ChildPickler(reduction.ForkingPickler):
            """Mặc định multiprocessing để tiến trình con tự "lấy" handle của Pipe từ tiến trình cha (OpenProcess
            PROCESS_DUP_HANDLE lên tiến trình cha) — AppContainer không được phép, và handle bị bỏ lại ở tiến trình
            cha làm `recv` chờ mãi. Ở đây tiến trình cha nhân bản handle THẲNG vào tiến trình con."""

            def __init__(self, file: Any, protocol: Any, *, child_pid: int) -> None:
                super().__init__(file, protocol)
                access_r, access_w = 0x120089, 0x120116  # FILE_GENERIC_READ / FILE_GENERIC_WRITE

                def reduce_conn(conn: Any) -> Any:
                    access = (access_r if conn.readable else 0) | (access_w if conn.writable else 0)
                    dh = reduction.DupHandle(conn.fileno(), access, child_pid)
                    return rebuild_pipe_connection, (dh, conn.readable, conn.writable)

                self.dispatch_table[PipeConnection] = reduce_conn

        return _Popen(process_obj)

else:

    def _win_popen(process_obj: Any) -> Any:
        raise SandboxError("AppContainer chỉ có trên Windows")


class _WinProcess(SpawnProcess):  # type: ignore[misc,valid-type,unused-ignore]
    _vietpdf_launch: tuple[bool, str | None, int] = (False, None, 3072)

    @staticmethod
    def _Popen(process_obj: Any) -> Any:  # tên do multiprocessing đặt
        return _win_popen(process_obj)


def _start(
    target: Callable[..., Any], args: tuple[Any, ...], win: _WinPrep | None = None, memory_mb: int = 3072
) -> Any:
    proc: Any
    if _WIN:
        proc = _WinProcess(target=target, args=args, daemon=True)
        proc._vietpdf_launch = (bool(win and win.appcontainer), win.capability if win else None, memory_mb + 256)
    else:
        proc = _CTX.Process(target=target, args=args, daemon=True)
    with _minimal_environ():
        proc.start()
    with _LIVE_LOCK:
        _LIVE.add(proc)
    return proc


def _job_of(proc: Any, memory_mb: int) -> WindowsJob:
    """Windows: Job Object đã gán lúc tiến trình con còn SUSPENDED. Nền tảng khác: đối tượng rỗng."""
    vp = getattr(getattr(proc, "_popen", None), "vp", None)
    return vp.job if vp is not None else WindowsJob(proc.pid or 0, memory_mb)


def _win_status(status: Any, proc: Any, win: _WinPrep | None) -> Any:
    """Đối chiếu mức cô lập tiến trình con Windows tự báo với token của nó do CHÍNH tiến trình cha đọc."""
    vp = getattr(getattr(proc, "_popen", None), "vp", None)
    if not _WIN or not isinstance(status, dict) or vp is None:
        return status
    st = dict(status)
    tok = vp.token
    errors = [e for e in st.get("errors") or [] if isinstance(e, str)]
    errors += (win.errors if win else []) + vp.errors + tok["errors"]
    child_ac = st.get("appcontainer") is True
    if child_ac != bool(tok["appcontainer"]):
        errors.append("mức AppContainer tiến trình con báo khác token tiến trình cha đọc được")
    st["appcontainer"] = child_ac and bool(tok["appcontainer"]) and bool(win and win.appcontainer)
    expected = {win.capability} if win and win.capability else set()
    caps_child, caps_parent = st.get("capabilities"), tok["capabilities"]
    if isinstance(caps_child, list) and isinstance(caps_parent, list):
        st["capabilities"] = sorted((set(caps_child) | set(caps_parent)) - expected)
    else:
        st["capabilities"] = None
    st["job_object"] = st.get("job_object") is True and vp.job.handle is not None
    st["errors"] = list(dict.fromkeys(errors))
    return st


def _forget(proc: Any) -> None:
    with _LIVE_LOCK:
        _LIVE.discard(proc)


def _apply_limits(memory_mb: int, cpu_s: int | None) -> None:
    if sys.platform.startswith("linux"):
        import resource  # noqa: PLC0415 - chỉ có trên POSIX

        mem = memory_mb * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (mem, mem))
        if cpu_s is not None:
            resource.setrlimit(resource.RLIMIT_CPU, (cpu_s, cpu_s + 5))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))  # không ghi core dump chứa nội dung PDF


def _disable_network() -> None:
    """Khoá tạo socket mạng ở mức Python (lớp phòng thủ cuối; lớp chính là seccomp/Landlock trong confine.py).

    Ngoại lệ được ghi rõ trong scripts/security_audit.py: chỉ file này được import `socket`, và chỉ để
    VÔ HIỆU HOÁ nó. Socket AF_UNIX (dùng nội bộ bởi multiprocessing trên POSIX) vẫn được phép."""
    import socket  # noqa: PLC0415

    real_socket = socket.socket

    class _Blocked(real_socket):  # type: ignore[misc, valid-type]
        def __init__(self, family: int = -1, *args: Any, **kwargs: Any) -> None:
            if family in (socket.AF_INET, socket.AF_INET6, -1):
                raise PermissionError("Truy cập mạng bị chặn trong tiến trình xử lý PDF")
            super().__init__(family, *args, **kwargs)

    def _blocked(*_a: Any, **_k: Any) -> Any:
        raise PermissionError("Truy cập mạng bị chặn trong tiến trình xử lý PDF")

    socket.socket = _Blocked  # type: ignore[misc]
    socket.create_connection = _blocked
    socket.getaddrinfo = _blocked


def _confine(parent_pid: int, read_files: list[str], write_dirs: list[str]) -> dict[str, Any]:
    status: dict[str, Any] = {"platform": sys.platform, "errors": []}
    try:
        if sys.platform.startswith("linux"):
            status = confine_child_linux(parent_pid, read_files, write_dirs)
        elif _WIN:
            status = _cf.confine_child_windows()
    except Exception as e:  # báo về cha (fail-closed ở phía cha), không nuốt
        status.setdefault("errors", []).append(f"confine: {type(e).__name__}: {str(e)[:200]}")
    _disable_network()
    return status


CONFINE_STATUS: dict[str, Any] = {}  # mức cô lập đạt được trong tiến trình con hiện tại (chỉ để chẩn đoán)

# ------------------------------------------------------------------------------- chính sách cô lập (R1-002)

# Chính sách khi lớp cô lập mạnh của nền tảng KHÔNG áp dụng được (xem docs/BAO_MAT.md, "Quyết định Windows/macOS"):
# - win32 (2.1.3): "block" — có AppContainer + Job Object (đã kiểm trên máy Windows thật); thiếu => từ chối như Linux
#   thiếu Landlock, trừ khi người dùng chủ động cho phép (`--allow-weak-sandbox`, hộp thoại GUI);
# - darwin: vẫn "warn" — lựa chọn tạm thời (b) của 2.1.2, cho tới khi sandbox macOS được làm và kiểm trên máy Mac.
WEAK_PLATFORM_POLICY: dict[str, str] = {"win32": "block", "darwin": "warn"}
_START_TIMEOUT_S = 60.0  # thời gian tối đa từ lúc khởi động tới khi tiến trình con báo mức cô lập
_POLICY = {"allow_weak": False}
_EVENTS: collections.deque[dict[str, Any]] = collections.deque(maxlen=4096)
_EVENTS_LOCK = threading.Lock()
_SEQ = [0]
_LAST_REFUSAL: list[str] = []  # vấn đề của lần từ chối gần nhất (GUI dùng để hỏi người dùng)


def set_allow_weak(allow: bool) -> None:
    """Cho phép chạy khi cô lập yếu — CHỈ gọi từ lựa chọn tường minh của người dùng (cờ CLI, hộp thoại GUI)."""
    _POLICY["allow_weak"] = bool(allow)


def allow_weak() -> bool:
    return bool(_POLICY["allow_weak"])


def evaluate_confine(status: Any, expected: dict[str, Any] | None = None) -> tuple[list[str], bool]:
    """So mức cô lập tiến trình con báo về với chính sách tối thiểu và với khả năng kernel đo ở tiến trình cha.
    Trả (các vấn đề, có chặn hay không). Không có vấn đề => cô lập đạt chính sách."""
    if not isinstance(status, dict):
        return (["tiến trình con không báo mức cô lập"], True)
    plat = str(status.get("platform", ""))
    problems = [str(e)[:300] for e in status.get("errors") or [] if isinstance(e, str)][:20]
    if plat.startswith("linux"):
        exp = expected or {}
        for key, label in (
            ("pdeathsig", "PDEATHSIG"),
            ("no_new_privs", "NO_NEW_PRIVS"),
            ("seccomp", "seccomp"),
            ("signal_filter", "lọc tín hiệu"),
        ):
            if status.get(key) is not True:
                problems.append(f"thiếu {label}")
        abi = status.get("landlock_abi")
        abi = abi if isinstance(abi, int) else 0
        if abi < 1:
            problems.append("thiếu Landlock (không giới hạn được file đọc/ghi)")
        exp_abi = int(exp.get("landlock_abi") or 0)
        if abi < exp_abi:
            problems.append(f"tiến trình con báo Landlock ABI {abi} < kernel hỗ trợ {exp_abi}")
        if exp.get("seccomp") and status.get("seccomp") is not True:
            problems.append("kernel hỗ trợ seccomp nhưng tiến trình con không cài được")
        return (list(dict.fromkeys(problems)), bool(problems))
    if plat == "win32":
        if status.get("appcontainer") is not True:
            problems.append(
                "Windows: tiến trình con KHÔNG chạy trong AppContainer — không chặn mạng và đọc file người dùng"
            )
        caps = status.get("capabilities")
        if not isinstance(caps, list):
            problems.append("Windows: không xác định được capability của tiến trình con")
        else:
            problems += [f"Windows: AppContainer có capability thừa {_cf.NETWORK_CAPABILITIES.get(c, c)}" for c in caps]
        if status.get("job_object") is not True:
            problems.append("Windows: thiếu Job Object (chết theo app, cấm sinh tiến trình, giới hạn bộ nhớ)")
    elif plat == "darwin":
        problems.append("macOS: CHƯA có sandbox — không chặn mạng và đọc file người dùng")
    else:
        problems.append(f"nền tảng {plat or '?'}: không có cô lập mức hệ điều hành")
        return (problems, True)
    problems = list(dict.fromkeys(problems))
    return (problems, bool(problems) and WEAK_PLATFORM_POLICY.get(plat) == "block")


def _record(status: dict[str, Any], problems: list[str]) -> None:
    with _EVENTS_LOCK:
        _SEQ[0] += 1
        _EVENTS.append({"seq": _SEQ[0], "status": status, "problems": problems})


def confine_seq() -> int:
    with _EVENTS_LOCK:
        return _SEQ[0]


def confine_summary(since: int = 0) -> dict[str, Any]:
    """Mức cô lập THẤP NHẤT của các tiến trình con chạy sau mốc `since` (báo cáo, GUI, CLI -v)."""
    with _EVENTS_LOCK:
        evs = [e for e in _EVENTS if e["seq"] > since]
    if not evs:
        return {"level": "unknown", "runs": 0, "problems": []}
    problems = list(dict.fromkeys(p for e in evs for p in e["problems"]))
    abis = [int(e["status"].get("landlock_abi") or 0) for e in evs]
    return {
        "level": "weak" if problems else "strong",
        "runs": len(evs),
        "problems": problems[:20],
        "platform": str(evs[-1]["status"].get("platform", "")),
        "landlock_abi": min(abis) if abis else 0,
        "seccomp": all(e["status"].get("seccomp") is True for e in evs),
    }


def describe_confine(summary: dict[str, Any]) -> str:
    """Một dòng tiếng Việt cho người dùng."""
    level = summary.get("level")
    if level == "strong" and summary.get("platform") == "win32":
        return (
            "Cô lập tiến trình xử lý PDF: ĐẠT (Windows — AppContainer không capability mạng + Job Object, "
            f"{summary.get('runs')} tiến trình con)"
        )
    if level == "strong":
        return (
            f"Cô lập tiến trình xử lý PDF: ĐẠT (Linux — seccomp, Landlock ABI {summary.get('landlock_abi')}, "
            f"{summary.get('runs')} tiến trình con)"
        )
    if level == "weak":
        return "Cô lập tiến trình xử lý PDF: YẾU — " + "; ".join(summary.get("problems") or [])
    return "Cô lập tiến trình xử lý PDF: chưa có tiến trình con nào chạy"


def predicted_confine() -> dict[str, Any]:
    """Mức cô lập dự kiến theo khả năng kernel (chưa chạy tiến trình con) — cho chỉ báo GUI lúc khởi động."""
    k = probe_kernel()
    if sys.platform.startswith("linux"):
        status = {
            "platform": "linux",
            "pdeathsig": True,
            "no_new_privs": k["no_new_privs"],
            "seccomp": k["seccomp"],
            "signal_filter": k["seccomp"],
            "landlock_abi": k["landlock_abi"],
            "errors": [],
        }
    elif _WIN:
        ok = _cf.appcontainer_supported()
        status = {"platform": "win32", "appcontainer": ok, "capabilities": [], "job_object": True, "errors": []}
    else:
        status = {"platform": sys.platform, "errors": []}
    problems, blocking = evaluate_confine(status, k)
    return {"level": "weak" if problems else "strong", "problems": problems, "blocking": blocking, **k}


def isolation_needs_consent() -> list[str]:
    """Các vấn đề khiến job sẽ bị từ chối nếu người dùng chưa cho phép (dự kiến theo kernel + lần từ chối gần nhất).
    Rỗng = không cần hỏi. Dùng cho hộp thoại GUI trước khi bắt đầu job."""
    if allow_weak():
        return []
    pred = predicted_confine()
    out = list(pred["problems"]) if pred["blocking"] else []
    return list(dict.fromkeys([*out, *_LAST_REFUSAL]))


def _check_confine(status: Any, allow_weak_override: bool | None) -> dict[str, Any]:
    """Phía cha: đánh giá, ghi nhận; vi phạm chính sách và người dùng chưa cho phép => SandboxWeakError."""
    st = status if isinstance(status, dict) else {}
    problems, blocking = evaluate_confine(status, probe_kernel())
    _record(st, problems)
    permitted = allow_weak() if allow_weak_override is None else allow_weak_override
    if problems and blocking and not permitted:
        _LAST_REFUSAL[:] = problems
        raise SandboxWeakError(problems)
    if problems:
        log.warning("Cô lập tiến trình con YẾU: %s", "; ".join(problems))
    return st


# ------------------------------------------------------------------------------- thư mục tạm của job (R1-001)

STAGE_PREFIX = ".vietpdf-stage-"
MAX_OUTPUT_BYTES = 2 << 30  # file đầu ra lớn hơn 2 GiB: coi là bất thường
_STAGE_DIRS: set[str] = set()
_STAGE_LOCK = threading.Lock()


def _stage_registry() -> Path | None:
    try:
        from vietpdf.config import cache_dir  # noqa: PLC0415

        d = cache_dir() / "stage-registry"
        d.mkdir(parents=True, exist_ok=True, mode=0o700)
        return d
    except OSError:
        return None


def make_stage_dir(dest_dir: Path) -> Path:
    """Tạo thư mục tạm MỚI, rỗng, quyền 0700, riêng cho một job — nơi DUY NHẤT tiến trình con được ghi.

    Ưu tiên tạo cạnh đích (cùng filesystem => `os.replace` nguyên tử); không tạo được (thư mục chỉ đọc...) thì tạo
    trong thư mục cache của app, khi công bố sẽ sao chép sang đích."""
    dest_dir = Path(dest_dir)
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
        d = tempfile.mkdtemp(prefix=STAGE_PREFIX, dir=str(dest_dir))
    except OSError:
        from vietpdf.config import cache_dir  # noqa: PLC0415

        root = cache_dir() / "stage"
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        d = tempfile.mkdtemp(prefix=STAGE_PREFIX, dir=str(root))
    real = os.path.realpath(d)
    with _STAGE_LOCK:
        _STAGE_DIRS.add(real)
    reg = _stage_registry()
    if reg is not None:
        with contextlib.suppress(OSError):
            fd, _ = tempfile.mkstemp(prefix=f"{os.getpid()}-", suffix=".stage", dir=str(reg))
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(real)
    return Path(real)


def _remove_stage_dir(path: str) -> bool:
    """Xoá một thư mục tạm của job: chỉ khi đúng là thư mục (không symlink) do chính người dùng này sở hữu và có tên
    đúng tiền tố. rmtree trên Linux dùng *at() với dir_fd nên không theo symlink bên trong."""
    try:
        st = os.lstat(path)
    except OSError:
        return False
    if not stat.S_ISDIR(st.st_mode) or not os.path.basename(path).startswith(STAGE_PREFIX):
        return False
    if hasattr(os, "getuid") and st.st_uid != os.getuid():
        return False
    shutil.rmtree(path, ignore_errors=True)
    return not os.path.lexists(path)


def discard_stage_dir(stage: Path) -> None:
    real = os.path.realpath(stage)
    _remove_stage_dir(real)
    with _STAGE_LOCK:
        _STAGE_DIRS.discard(real)
    reg = _stage_registry()
    if reg is not None:
        for m in reg.glob(f"{os.getpid()}-*.stage"):
            with contextlib.suppress(OSError):
                if m.read_text("utf-8").strip() == real:
                    m.unlink()


def _pid_alive(pid: int) -> bool:
    if _WIN:
        # 2.1.3: trên Windows `os.kill(pid, 0)` là CTRL_C_EVENT — gửi Ctrl+C cho cả nhóm tiến trình console (kể cả chính
        # app), không phải phép thử "còn sống". Dùng psutil (phụ thuộc sẵn có).
        import psutil  # noqa: PLC0415

        return bool(psutil.pid_exists(pid))
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def cleanup_stale_stages() -> int:
    """Khi khởi động: dọn các `.vietpdf-stage-*` mà một lần chạy trước (bị kill) để lại. Chỉ dọn thư mục đã được
    app ghi vào sổ đăng ký và tiến trình tạo ra nó không còn sống. Trả về số thư mục đã dọn."""
    reg = _stage_registry()
    if reg is None:
        return 0
    n = 0
    for m in reg.glob("*.stage"):
        try:
            pid = int(m.name.split("-", 1)[0])
        except ValueError:
            continue
        if pid != os.getpid() and _pid_alive(pid):
            continue
        with contextlib.suppress(OSError):
            path = m.read_text("utf-8").strip()
            if path and _remove_stage_dir(path):
                n += 1
            m.unlink()
    return n


@contextlib.contextmanager
def staged_outputs(*finals: Path | None) -> Iterator[tuple[Path, list[Path | None]]]:
    """`with staged_outputs(out, dual) as (stage, (s_out, s_dual)):` — chạy tiến trình con với
    `write_dirs=[str(stage)]`, ghi vào s_out/s_dual, rồi gọi `publish_staged(s_out, out)`. Luôn dọn thư mục tạm."""
    first = next((f for f in finals if f is not None), None)
    if first is None:
        raise ValueError("Không có file đầu ra")
    stage = make_stage_dir(Path(first).parent)
    try:
        staged: list[Path | None] = [
            None if f is None else stage / f"out{i}{Path(f).suffix or '.bin'}" for i, f in enumerate(finals)
        ]
        yield stage, staged
    finally:
        discard_stage_dir(stage)


def _same_file(a: os.stat_result, b: os.stat_result) -> bool:
    return (a.st_dev, a.st_ino, a.st_size, a.st_mtime_ns) == (b.st_dev, b.st_ino, b.st_size, b.st_mtime_ns)


def publish_staged(staged: Path, final: Path, *, scan: bool = True, timeout: float = 300.0) -> Path:
    """Tiến trình CHÍNH kiểm file tiến trình con để lại trong thư mục tạm của job rồi mới chuyển sang đích.

    - file nằm trực tiếp trong một thư mục tạm của job đang mở;
    - mở bằng O_NOFOLLOW|O_NONBLOCK: symlink, FIFO, thiết bị... bị từ chối; phải là file thường, đúng 1 link (không
      hardlink tới file khác), đúng chủ sở hữu, không quá `MAX_OUTPUT_BYTES`;
    - quét nội dung chủ động (`render.assert_no_active_content`) trong một tiến trình con MỚI chỉ có quyền ĐỌC đúng
      file này (không tin kết quả quét của tiến trình đã ghi file);
    - chmod 0600, kiểm lại không bị thay giữa chừng, rồi `os.replace` (khác filesystem: sao chép an toàn)."""
    staged, final = Path(staged), Path(final)
    with _STAGE_LOCK:
        known = os.path.realpath(staged.parent) in _STAGE_DIRS
    if not known or staged.parent.is_symlink():
        raise SandboxError("File đầu ra không nằm trong thư mục tạm của job")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(staged, flags)
    except OSError as e:
        raise SandboxError(
            f"File đầu ra từ tiến trình con không hợp lệ ({errno.errorcode.get(e.errno or 0, e)})"
        ) from e
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise SandboxError("File đầu ra từ tiến trình con không phải file thường")
        if st.st_nlink != 1:
            raise SandboxError("File đầu ra từ tiến trình con có nhiều liên kết cứng")
        if hasattr(os, "getuid") and st.st_uid != os.getuid():
            raise SandboxError("File đầu ra từ tiến trình con sai chủ sở hữu")
        if st.st_size > MAX_OUTPUT_BYTES:
            raise SandboxError("File đầu ra quá lớn")
        if os.name == "posix":
            os.fchmod(fd, 0o600)
        st = os.fstat(fd)
    finally:
        os.close(fd)
    if scan:
        from vietpdf.core import render  # noqa: PLC0415

        run_isolated(
            render.assert_no_active_content,
            str(staged),
            timeout=timeout,
            read_files=[str(staged)],
            write_dirs=(),
            max_result_mb=1,
        )
    try:
        again = os.lstat(staged)
    except OSError as e:
        raise SandboxError("File đầu ra bị thay đổi trong lúc kiểm tra") from e
    if not stat.S_ISREG(again.st_mode) or not _same_file(st, again):
        raise SandboxError("File đầu ra bị thay đổi trong lúc kiểm tra")
    final.parent.mkdir(parents=True, exist_ok=True)
    if _WIN:
        # 2.1.3: file do tiến trình con trong AppContainer tạo mang DACL mặc định của AppContainer, nhãn Low Integrity
        # và có thể kèm luồng dữ liệu phụ (ADS `file:stream`). Không chuyển nguyên file: chép NỘI DUNG (luồng chính)
        # sang file mới do tiến trình chính tạo (ACL kế thừa bình thường, không ADS).
        _copy_private(staged, final)
        return final
    try:
        os.replace(staged, final)
    except OSError as e:
        if e.errno != errno.EXDEV:
            raise
        _copy_private(staged, final)
    return final


def run_to_outputs(
    func: Callable[..., Any],
    build_args: Callable[[list[Path | None]], tuple[Any, ...]],
    finals: list[Path | None],
    *,
    timeout: float,
    memory_mb: int = 3072,
    scan: bool = True,
    max_result_mb: int = 8,
) -> Any:
    """Chạy `func` trong tiến trình con, ghi vào thư mục tạm của job, rồi công bố từng file sang đích.

    `build_args(staged)` nhận danh sách đường dẫn tạm (cùng thứ tự `finals`, None giữ nguyên) và trả về đối số của
    `func`. Đây là đường DUY NHẤT để tiến trình con tạo file cho người dùng (R1-001)."""
    with staged_outputs(*finals) as (stage, staged):
        result = run_isolated(
            func,
            *build_args(staged),
            timeout=timeout,
            memory_mb=memory_mb,
            write_dirs=[str(stage)],
            max_result_mb=max_result_mb,
        )
        for st, fin in zip(staged, finals, strict=True):
            if fin is not None and st is not None:
                publish_staged(st, Path(fin), scan=scan)
    return result


def _copy_private(src: Path, final: Path) -> None:
    """Sao chép sang filesystem khác: file tạm mới (O_EXCL|O_NOFOLLOW, 0600) cạnh đích rồi `os.replace`."""
    from vietpdf.security.fsutil import replace_from_temp  # noqa: PLC0415

    fd_tmp, name = tempfile.mkstemp(prefix=f".{final.name}.", suffix=".tmp", dir=str(final.parent))
    tmp = Path(name)
    try:
        with open(src, "rb") as fin, os.fdopen(fd_tmp, "wb") as fout:
            shutil.copyfileobj(fin, fout, 1 << 20)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise
    replace_from_temp(tmp, final)


def _check_write_dirs(write_dirs: Iterable[str]) -> list[str]:
    """Bất biến (R1-002/R1-001): tiến trình con chỉ được ghi vào thư mục tạm của job do `make_stage_dir` tạo."""
    writes = [os.path.realpath(d) for d in write_dirs if d]
    with _STAGE_LOCK:
        bad = [d for d in writes if d not in _STAGE_DIRS]
    if bad:
        raise SandboxError(f"write_dirs không phải thư mục tạm của job: {bad}")
    return writes


# ------------------------------------------------------------------------------- tiến trình con


def _child_entry(
    conn: Connection,
    go: Connection,
    func: Callable[..., Any],
    args: tuple[Any, ...],
    memory_mb: int,
    cpu_s: int,
    parent_pid: int,
    read_files: list[str],
    write_dirs: list[str],
) -> None:
    try:
        _apply_limits(memory_mb, cpu_s)
    except Exception as e:
        status: dict[str, Any] = {"platform": sys.platform, "errors": [f"limits: {e}"]}
    else:
        status = {}
    status = {**_confine(parent_pid, read_files, write_dirs), **status}
    if "errors" in status and isinstance(status["errors"], list):
        status["errors"] = list(status["errors"])
    CONFINE_STATUS.update(status)
    # tin nhắn 1: mức cô lập, gửi TRƯỚC khi chạm vào dữ liệu; chỉ chạy func khi cha trả lời "go"
    conn.send_bytes(json.dumps({"confine": status}, ensure_ascii=False).encode("utf-8"))
    try:
        ok = go.poll(_START_TIMEOUT_S) and go.recv_bytes(16) == b"go"
    except (EOFError, OSError):
        ok = False
    go.close()
    if not ok:
        conn.close()
        os._exit(0)
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            result = func(*args)
        payload = {"ok": True, "result": result}
    except MemoryError:
        payload = {"ok": False, "type": "MemoryError", "message": "Vượt quá giới hạn bộ nhớ"}
    except Exception as e:
        payload = {
            "ok": False,
            "type": type(e).__name__,
            "message": str(e)[:2000],
            "trace": traceback.format_exc(limit=8)[-4000:],
        }
    try:
        conn.send_bytes(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    finally:
        conn.close()


class _Watchdog(threading.Thread):
    """Theo dõi RSS của tiến trình con (mọi hệ điều hành); vượt ngưỡng thì kill."""

    def __init__(self, pid: int, limit_mb: int) -> None:
        super().__init__(daemon=True)
        self.pid, self.limit = pid, limit_mb * 1024 * 1024
        self.tripped = False
        self._stop_evt = threading.Event()

    def stop(self) -> None:
        self._stop_evt.set()

    def run(self) -> None:
        try:
            import psutil  # noqa: PLC0415
        except ImportError:
            return
        try:
            p = psutil.Process(self.pid)
            while not self._stop_evt.wait(0.2):
                rss = p.memory_info().rss + sum(c.memory_info().rss for c in p.children(recursive=True))
                if rss > self.limit:
                    self.tripped = True
                    p.kill()
                    return
        except psutil.Error:
            return


def _auto_read_files(args: Iterable[Any]) -> list[str]:
    """Đối số kiểu chuỗi là đường dẫn file tồn tại => cho tiến trình con quyền ĐỌC đúng file đó."""
    out = []
    for a in args:
        if isinstance(a, str) and len(a) < 4096 and "\n" not in a:
            with contextlib.suppress(OSError, ValueError):
                if os.path.isfile(a):
                    out.append(os.path.abspath(a))
    return out


def run_isolated(
    func: Callable[..., Any],
    *args: Any,
    timeout: float,
    memory_mb: int = 3072,
    max_result_mb: int = 512,
    write_dirs: Iterable[str] = (),
    read_files: Iterable[str] | None = None,
    allow_weak: bool | None = None,
) -> Any:
    """Chạy `func(*args)` trong tiến trình con. `func` phải là hàm cấp module, kết quả phải JSON được.

    `write_dirs`: thư mục tiến trình con được ghi — CHỈ thư mục tạm của job (`make_stage_dir`/`staged_outputs`),
    mặc định không đâu cả. `allow_weak`: None = theo lựa chọn của người dùng (`set_allow_weak`)."""
    if _STOPPED.is_set():
        raise SandboxCancelledError("Ứng dụng đang dừng")
    writes = _check_write_dirs(write_dirs)
    generation = _GENERATION[0]
    reads = list(read_files) if read_files is not None else _auto_read_files(args)
    win = _WinPrep(func, reads, writes) if _WIN else None
    try:
        if win is not None and win.errors:
            _precheck_win(win, allow_weak)  # không dựng được AppContainer: từ chối TRƯỚC khi khởi động tiến trình con
        return _run_child(func, args, timeout, memory_mb, max_result_mb, reads, writes, allow_weak, generation, win)
    finally:
        if win is not None:
            win.close()


def _precheck_win(win: _WinPrep, allow_weak_override: bool | None) -> None:
    status = {"platform": "win32", "appcontainer": False, "capabilities": [], "job_object": True, "errors": win.errors}
    problems, blocking = evaluate_confine(status)
    permitted = allow_weak() if allow_weak_override is None else allow_weak_override
    if problems and blocking and not permitted:
        _record(status, problems)
        _LAST_REFUSAL[:] = problems
        raise SandboxWeakError(problems)


def _run_child(
    func: Callable[..., Any],
    args: tuple[Any, ...],
    timeout: float,
    memory_mb: int,
    max_result_mb: int,
    reads: list[str],
    writes: list[str],
    allow_weak_override: bool | None,
    generation: int,
    win: _WinPrep | None,
) -> Any:
    parent_conn, child_conn = _CTX.Pipe(duplex=False)
    go_r, go_w = _CTX.Pipe(duplex=False)
    try:
        proc = _start(
            _child_entry,
            (child_conn, go_r, func, args, memory_mb, int(timeout) + 10, os.getpid(), reads, writes),
            win,
            memory_mb,
        )
    except BaseException:
        for c in (parent_conn, child_conn, go_r, go_w):
            c.close()
        raise
    child_conn.close()
    go_r.close()
    job = _job_of(proc, memory_mb + 256)
    dog = _Watchdog(proc.pid or 0, memory_mb + 256)  # RLIMIT_AS (Linux) chặn trước; đây là lớp đa nền tảng
    dog.start()
    try:
        first = _wait_message(parent_conn, proc, dog, generation, min(timeout, _START_TIMEOUT_S), 1 << 16)
        status = first.get("confine") if isinstance(first, dict) else None
        status = _win_status(status, proc, win)
        _check_confine(status, allow_weak_override)
        go_w.send_bytes(b"go")
        payload = _wait_message(parent_conn, proc, dog, generation, timeout, max_result_mb * 1024 * 1024)
        if not isinstance(payload, dict):
            raise SandboxError("Kết quả từ tiến trình con sai định dạng")
        if payload.get("ok"):
            return payload.get("result")
        raise SandboxError(f"{payload.get('type', 'Error')}: {payload.get('message', '')}")
    finally:
        dog.stop()
        parent_conn.close()
        go_w.close()
        if proc.is_alive():
            proc.kill()
        proc.join(5)
        job.close()
        _forget(proc)


def _wait_message(conn: Connection, proc: Any, dog: _Watchdog, generation: int, timeout: float, maxlength: int) -> Any:
    waited = 0.0
    while not conn.poll(0.2):
        waited += 0.2
        if _GENERATION[0] != generation:
            raise SandboxCancelledError("Đã huỷ")
        if not proc.is_alive() and not conn.poll(0):
            # tiến trình con đã chết mà không gửi gì: KHÔNG gọi recv (nếu còn handle ghi rò ở đâu đó, recv chờ mãi)
            proc.join(5)
            if _GENERATION[0] != generation:
                raise SandboxCancelledError("Đã huỷ")
            if dog.tripped:
                raise SandboxMemoryError("Xử lý PDF vượt giới hạn bộ nhớ")
            raise SandboxCrashedError(f"Tiến trình xử lý PDF bị dừng bất thường (mã {proc.exitcode})")
        if waited >= timeout:
            if dog.tripped:
                raise SandboxMemoryError("Xử lý PDF vượt giới hạn bộ nhớ")
            raise SandboxTimeoutError(f"Xử lý PDF quá thời gian cho phép ({timeout:.0f}s)")
    try:
        raw = conn.recv_bytes(maxlength=maxlength)
    except EOFError as e:
        proc.join(5)
        if _GENERATION[0] != generation:
            raise SandboxCancelledError("Đã huỷ") from e
        if dog.tripped:
            raise SandboxMemoryError("Xử lý PDF vượt giới hạn bộ nhớ") from e
        raise SandboxCrashedError(f"Tiến trình xử lý PDF bị dừng bất thường (mã {proc.exitcode})") from e
    except OSError as e:
        raise SandboxError("Kết quả từ tiến trình con quá lớn") from e
    try:
        # json.loads nhận thẳng bytes UTF-8: không tạo thêm bản sao str của toàn bộ kết quả (R1-007)
        return safe_json_loads(raw)
    except (ValueError, UnicodeDecodeError) as e:
        raise SandboxError("Kết quả từ tiến trình con sai định dạng") from e
    finally:
        del raw


# --------------------------------------------------------------------------- tiến trình render thường trực


def _render_server(conn: Connection, memory_mb: int, parent_pid: int, read_files: list[str]) -> None:
    from vietpdf.core.render import render_page_png, text_in_rect  # noqa: PLC0415

    _apply_limits(memory_mb, None)  # mỗi request đã có timeout theo đồng hồ ở phía GUI
    status = _confine(parent_pid, read_files, [])
    CONFINE_STATUS.update(status)
    # cùng giao thức với run_isolated: báo mức cô lập, chỉ phục vụ khi cha trả "go" (R1-002)
    conn.send_bytes(json.dumps({"confine": status}, ensure_ascii=False).encode("utf-8"))
    try:
        if not conn.poll(_START_TIMEOUT_S) or conn.recv_bytes(16) != b"go":
            return
    except (EOFError, OSError):
        return
    allowed = set(read_files)
    while True:
        try:
            req = safe_json_loads(conn.recv_bytes(maxlength=1 << 16).decode("utf-8"))
        except (EOFError, OSError, ValueError):
            return
        try:
            path = os.path.abspath(str(req["path"]))
            if path not in allowed:
                raise PermissionError("File không nằm trong danh sách được mở")
            with contextlib.redirect_stdout(io.StringIO()):
                if req.get("op") == "text":
                    rect = [float(v) for v in req["rect"]][:4]
                    resp = {"ok": True, "text": text_in_rect(path, int(req["page"]), rect, req.get("password"))}
                else:
                    png = render_page_png(path, int(req["page"]), float(req["zoom"]), req.get("password"))
                    resp = {"ok": True, "png": png}
        except Exception as e:
            resp = {"ok": False, "message": f"{type(e).__name__}: {str(e)[:500]}"}
        conn.send_bytes(json.dumps(resp).encode("utf-8"))


class PageRenderer:
    """Render trang cho trình xem GUI trong một tiến trình con thường trực (không mở PDF trong tiến trình GUI).
    Tiến trình con chỉ đọc được các file đã đăng ký; gặp file mới thì khởi động lại với danh sách mới."""

    PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
    MAX_FILES = 16

    def __init__(self, memory_mb: int = 2048, timeout: float = 30.0) -> None:
        self.memory_mb, self.timeout = memory_mb, timeout
        self._lock = threading.Lock()
        self._proc: Any = None
        self._conn: Connection | None = None
        self._job: WindowsJob | None = None
        self._win: _WinPrep | None = None
        self._files: list[str] = []

    def _ensure(self, path: str) -> Connection:
        path = os.path.abspath(path)
        if path not in self._files:
            self._files = [*self._files, path][-self.MAX_FILES :]
            self.close()
        if self._proc is None or not self._proc.is_alive() or self._conn is None:
            parent, child = _CTX.Pipe(duplex=True)
            try:
                if _WIN:
                    self._win = _WinPrep(_render_server, list(self._files), [])
                    if self._win.errors:
                        _precheck_win(self._win, None)
                self._proc = _start(
                    _render_server, (child, self.memory_mb, os.getpid(), list(self._files)), self._win, self.memory_mb
                )
            except BaseException:
                parent.close()
                child.close()
                self.close()
                raise
            child.close()
            self._conn = parent
            self._job = _job_of(self._proc, self.memory_mb + 256)
            try:
                if not parent.poll(_START_TIMEOUT_S):
                    raise SandboxTimeoutError("Tiến trình render không báo mức cô lập")
                first = safe_json_loads(parent.recv_bytes(maxlength=1 << 16).decode("utf-8"))
                status = first.get("confine") if isinstance(first, dict) else None
                status = _win_status(status, self._proc, self._win)
                _check_confine(status, None)
                parent.send_bytes(b"go")
            except BaseException:
                self.close()
                raise
        return self._conn

    def render(self, path: str, page: int, zoom: float, password: str | None = None) -> bytes:
        resp = self._request({"path": path, "page": page, "zoom": zoom, "password": password})
        data = base64.b64decode(resp["png"], validate=True)
        if not data.startswith(self.PNG_MAGIC):
            raise SandboxError("Dữ liệu ảnh không hợp lệ")
        return data

    def text_in_rect(
        self, path: str, page: int, rect: tuple[float, float, float, float], password: str | None = None
    ) -> str:
        """Chữ trong vùng bôi đen (đọc PDF trong tiến trình con, kết quả chỉ là chuỗi thuần)."""
        resp = self._request({"op": "text", "path": path, "page": page, "rect": list(rect), "password": password})
        text = resp.get("text")
        if not isinstance(text, str):
            raise SandboxError("Kết quả không hợp lệ")
        return text[:2000]

    def _request(self, req: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            conn = self._ensure(str(req["path"]))
            conn.send_bytes(json.dumps({**req, "path": os.path.abspath(str(req["path"]))}).encode())
            if not conn.poll(self.timeout):
                self.close()
                raise SandboxTimeoutError("Render trang quá thời gian")
            try:
                resp = safe_json_loads(conn.recv_bytes(maxlength=256 << 20).decode("utf-8"))
            except (EOFError, OSError, ValueError) as e:
                self.close()
                raise SandboxCrashedError("Tiến trình render bị dừng") from e
        if not isinstance(resp, dict) or not resp.get("ok"):
            raise SandboxError(str(resp.get("message", "")) if isinstance(resp, dict) else "Phản hồi lạ")
        return resp

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None
        if self._proc is not None:
            if self._proc.is_alive():
                self._proc.kill()
            self._proc.join(2)
            _forget(self._proc)
            self._proc = None
        if self._job is not None:
            self._job.close()
            self._job = None
        if self._win is not None:
            self._win.close()
            self._win = None
