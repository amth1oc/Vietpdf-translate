"""Cô lập tiến trình con xử lý dữ liệu không tin cậy ở mức HỆ ĐIỀU HÀNH.

Bản 2.1.0 chỉ thay `socket.socket` trong Python: mã native (MuPDF, onnxruntime), `_socket`, `ctypes`, `subprocess`
đều vượt qua được. File này áp các giới hạn mà mã trong tiến trình con KHÔNG gỡ được.

Linux — mức đạt được do tiến trình con tự báo về tiến trình cha TRƯỚC khi đụng dữ liệu; tiến trình cha so với chính
sách và với khả năng kernel tự đo (`probe_kernel`), không đạt thì từ chối (`sandbox.SandboxWeakError`, 2.1.2 — R1-002):
- `PR_SET_PDEATHSIG(SIGKILL)`: tiến trình chính chết (kể cả `kill -9`) => tiến trình con chết theo.
- `PR_SET_NO_NEW_PRIVS` + seccomp-BPF (áp cho mọi luồng, TSYNC): cấm `socket()` khác AF_UNIX, `connect`,
  `execve/execveat`, `fork/vfork/clone` không phải luồng, `ptrace`, `process_vm_readv/writev`, `io_uring_setup`;
  syscall 32-bit/x32 bị chặn. Tín hiệu: `kill`/`tgkill`/`rt_(tg)sigqueueinfo` chỉ được gửi tới CHÍNH tiến trình con
  (kể cả khi Landlock ABI < 6 không giới hạn được tín hiệu); `tkill`, `pidfd_open`, `pidfd_send_signal` bị cấm.
- Landlock: chỉ ĐỌC được thư viện Python/hệ thống và đúng các file đầu vào được chỉ định; chỉ GHI được các thư mục được
  chỉ định — từ 2.1.2 đó luôn là thư mục tạm RIÊNG của job do `sandbox.make_stage_dir` tạo (R1-001), không bao giờ là
  thư mục của người dùng; không kết nối TCP (ABI ≥ 4); không gửi tín hiệu/kết nối socket abstract ra ngoài (ABI ≥ 6).
  Giới hạn đã biết: Landlock vẫn cho ĐỌC toàn bộ /etc, /usr... (xem docs/BAO_MAT.md, R1-008).
- `PR_SET_DUMPABLE(0)` cho cả tiến trình chính (`harden_current_process`): `/proc/<pid>/environ` và `/proc/<pid>/mem`
  không đọc được từ tiến trình khác cùng người dùng.
Mọi bước thất bại được ghi vào `status["errors"]` và log mức WARNING (không nuốt lỗi).

Windows (2.1.3): AppContainer không capability mạng (quyền file theo ACL: thư viện — đọc; file đầu vào — đọc; thư mục
tạm của job — ghi, cấp cho capability ngẫu nhiên của từng tiến trình con) + Job Object (`KILL_ON_JOB_CLOSE`,
`ACTIVE_PROCESS_LIMIT=1`, giới hạn bộ nhớ) gán khi tiến trình còn SUSPENDED — xem phần "Windows: AppContainer".
macOS: chưa có sandbox, được báo là "cô lập yếu" (docs/BAO_MAT.md).

Ngoại lệ có chủ đích trong scripts/security_audit.py: file này được import `ctypes` (gọi prctl/seccomp/landlock/
Win32 AppContainer/ACL/Job Object), nằm dưới CODEOWNERS.
"""

from __future__ import annotations

import contextlib
import logging
import os
import struct
import sys
from collections.abc import Iterable
from typing import Any

log = logging.getLogger(__name__)

# ------------------------------------------------------------------------------------------- môi trường tối giản

_ENV_ALLOW = {
    "PATH", "LANG", "LANGUAGE", "TZ", "TMPDIR", "TEMP", "TMP", "LD_LIBRARY_PATH",
    # Windows cần các biến này để nạp DLL/crypto
    "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "PATHEXT", "NUMBER_OF_PROCESSORS",
    "PROCESSOR_ARCHITECTURE", "OS",
    # đường dẫn mà mã của app đọc ở các luồng khác (giữ để việc thay os.environ tạm thời không gây sai thư mục)
    "HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME",
    "VIETPDF_CONFIG_DIR", "VIETPDF_CACHE_DIR", "VIETPDF_DATA_DIR", "QT_QPA_PLATFORM",
}  # fmt: skip
_ENV_ALLOW_PREFIX = ("LC_", "OMP_", "MKL_", "OPENBLAS_")


def minimal_env(environ: dict[str, str]) -> dict[str, str]:
    """Môi trường cho tiến trình con: chỉ biến cần cho trình thông dịch chạy và đường dẫn dữ liệu; không khoá, token,
    proxy, chứng chỉ, biến tuỳ ý của người dùng. HOME giữ lại chỉ để các luồng khác của tiến trình chính không thấy
    đường dẫn thay đổi; việc tiến trình con có ĐỌC được HOME hay không do Landlock quyết định (chỉ trên Linux)."""
    return {k: v for k, v in environ.items() if k.upper() in _ENV_ALLOW or k.upper().startswith(_ENV_ALLOW_PREFIX)}


# ------------------------------------------------------------------------------------------- Linux

PR_SET_PDEATHSIG = 1
PR_SET_DUMPABLE = 4
PR_SET_NO_NEW_PRIVS = 38
SECCOMP_SET_MODE_FILTER = 1
SECCOMP_FILTER_FLAG_TSYNC = 1
SECCOMP_RET_ALLOW = 0x7FFF0000
SECCOMP_RET_ERRNO = 0x00050000
EPERM, ENOSYS = 1, 38
AF_UNIX = 1
CLONE_THREAD = 0x00010000

# (audit arch, nr seccomp, nr socket, nr clone, nr clone3, các syscall cấm hẳn)
_ARCH = {
    "x86_64": (
        0xC000003E, 317, 41, 56, 435,
        {"execve": 59, "execveat": 322, "fork": 57, "vfork": 58, "ptrace": 101, "process_vm_readv": 310,
         "process_vm_writev": 311, "io_uring_setup": 425, "connect": 42,
         # tín hiệu (R1-002): không gửi được tới tiến trình khác dù Landlock ABI < 6
         "tkill": 200, "pidfd_open": 434, "pidfd_send_signal": 424,
         # R1-008: không gửi gói tới socket UNIX có sẵn qua sendmsg (seccomp không đọc được msg_name), không tạo
         # namespace, không đụng keyring kernel, không tạo file ẩn trong bộ nhớ, không đổi personality
         "sendmsg": 46, "sendmmsg": 307, "unshare": 272, "setns": 308, "keyctl": 250, "add_key": 248,
         "request_key": 249, "memfd_create": 319, "personality": 135},
    ),
    "aarch64": (
        0xC00000B7, 277, 198, 220, 435,
        {"execve": 221, "execveat": 281, "ptrace": 117, "process_vm_readv": 270, "process_vm_writev": 271,
         "io_uring_setup": 425, "connect": 203,
         "tkill": 130, "pidfd_open": 434, "pidfd_send_signal": 424,
         "sendmsg": 211, "sendmmsg": 269, "unshare": 97, "setns": 268, "keyctl": 219, "add_key": 217,
         "request_key": 218, "memfd_create": 279, "personality": 92},
    ),
}  # fmt: skip

# syscall gửi tín hiệu có pid/tgid đích ở đối số 0: chỉ cho khi đích là CHÍNH tiến trình con (R1-002)
_SIGNAL_SELF_ONLY = {
    "x86_64": {"kill": 62, "tgkill": 234, "rt_sigqueueinfo": 129, "rt_tgsigqueueinfo": 297},
    "aarch64": {"kill": 129, "tgkill": 131, "rt_sigqueueinfo": 138, "rt_tgsigqueueinfo": 240},
}

# sendto(fd, buf, len, flags, dest_addr, addrlen): chỉ cho khi dest_addr == NULL (tức send() trên pipe/socketpair)
_NR_SENDTO = {"x86_64": 44, "aarch64": 206}
_ARG4_LO, _ARG4_HI = 48, 52

# BPF
_LD_W_ABS, _JEQ_K, _JSET_K, _JGE_K, _RET_K = 0x20, 0x15, 0x45, 0x35, 0x06
_ARG0_LO, _ARG0_HI = 16, 20  # offsetof(seccomp_data, args[0]) — little-endian


def _ins(code: int, jt: int, jf: int, k: int) -> bytes:
    return struct.pack("<HBBI", code, jt, jf, k & 0xFFFFFFFF)


def _seccomp_program(machine: str, self_pid: int) -> bytes:
    """`self_pid`: pid của tiến trình con (biết lúc dựng bộ lọc) — đích duy nhất được nhận tín hiệu."""
    arch, _nr_seccomp, nr_socket, nr_clone, nr_clone3, deny = _ARCH[machine]
    errno_eperm = SECCOMP_RET_ERRNO | EPERM
    prog: list[bytes] = [
        _ins(_LD_W_ABS, 0, 0, 4),  # A = arch
        _ins(_JEQ_K, 1, 0, arch),
        _ins(_RET_K, 0, 0, errno_eperm),  # kiến trúc khác (int 0x80 32-bit...): từ chối mọi syscall
        _ins(_LD_W_ABS, 0, 0, 0),  # A = số syscall
    ]
    if machine == "x86_64":
        prog += [_ins(_JGE_K, 0, 1, 0x40000000), _ins(_RET_K, 0, 0, errno_eperm)]  # x32 ABI
    for nr in deny.values():
        prog += [_ins(_JEQ_K, 0, 1, nr), _ins(_RET_K, 0, 0, errno_eperm)]
    prog += [_ins(_JEQ_K, 0, 1, nr_clone3), _ins(_RET_K, 0, 0, SECCOMP_RET_ERRNO | ENOSYS)]  # glibc lùi về clone
    # kill/tgkill/rt_sigqueueinfo/rt_tgsigqueueinfo(pid, ...): chỉ khi pid == self_pid. kill(0)/kill(-1)/kill(-pgid)
    # (cả nhóm tiến trình, gồm tiến trình chính) và mọi pid khác => EPERM.
    for nr in _SIGNAL_SELF_ONLY[machine].values():
        prog += [
            _ins(_JEQ_K, 0, 6, nr),
            _ins(_LD_W_ABS, 0, 0, _ARG0_HI),
            _ins(_JEQ_K, 0, 2, 0),  # 32 bit cao phải bằng 0
            _ins(_LD_W_ABS, 0, 0, _ARG0_LO),
            _ins(_JEQ_K, 1, 0, self_pid),
            _ins(_RET_K, 0, 0, errno_eperm),
            _ins(_RET_K, 0, 0, SECCOMP_RET_ALLOW),
        ]
    prog += [
        _ins(_JEQ_K, 0, 6, _NR_SENDTO[machine]),
        _ins(_LD_W_ABS, 0, 0, _ARG4_LO),
        _ins(_JEQ_K, 0, 3, 0),
        _ins(_LD_W_ABS, 0, 0, _ARG4_HI),
        _ins(_JEQ_K, 0, 1, 0),
        _ins(_RET_K, 0, 0, SECCOMP_RET_ALLOW),
        _ins(_RET_K, 0, 0, errno_eperm),
    ]
    # socket(domain, ...): chỉ AF_UNIX (multiprocessing)
    prog += [
        _ins(_JEQ_K, 0, 4, nr_socket),
        _ins(_LD_W_ABS, 0, 0, _ARG0_LO),  # args[0] (32 bit thấp, little-endian)
        _ins(_JEQ_K, 0, 1, AF_UNIX),
        _ins(_RET_K, 0, 0, SECCOMP_RET_ALLOW),
        _ins(_RET_K, 0, 0, errno_eperm),
    ]
    # clone(flags, ...): chỉ cho tạo LUỒNG
    prog += [
        _ins(_JEQ_K, 0, 4, nr_clone),
        _ins(_LD_W_ABS, 0, 0, _ARG0_LO),
        _ins(_JSET_K, 0, 1, CLONE_THREAD),
        _ins(_RET_K, 0, 0, SECCOMP_RET_ALLOW),
        _ins(_RET_K, 0, 0, errno_eperm),
    ]
    prog.append(_ins(_RET_K, 0, 0, SECCOMP_RET_ALLOW))
    return b"".join(prog)


def _machine() -> str:
    import platform  # noqa: PLC0415

    m = platform.machine().lower()
    return {"amd64": "x86_64", "arm64": "aarch64"}.get(m, m)


# Landlock
_LL_EXECUTE, _LL_WRITE_FILE, _LL_READ_FILE, _LL_READ_DIR = 1 << 0, 1 << 1, 1 << 2, 1 << 3
_LL_REMOVE_DIR, _LL_REMOVE_FILE, _LL_MAKE_REG, _LL_MAKE_DIR = 1 << 4, 1 << 5, 1 << 8, 1 << 7
_LL_ALL_ABI1 = (1 << 13) - 1
_LL_REFER, _LL_TRUNCATE, _LL_IOCTL_DEV = 1 << 13, 1 << 14, 1 << 15
_LL_FILE_ONLY = _LL_EXECUTE | _LL_WRITE_FILE | _LL_READ_FILE | _LL_TRUNCATE | _LL_IOCTL_DEV
_LL_NET_BIND, _LL_NET_CONNECT = 1 << 0, 1 << 1
_LL_SCOPE_ABSTRACT_UNIX, _LL_SCOPE_SIGNAL = 1 << 0, 1 << 1
_NR_LL_CREATE, _NR_LL_ADD, _NR_LL_RESTRICT = 444, 445, 446
_O_PATH, _O_CLOEXEC = 0o10000000, 0o2000000


def _libc() -> Any:
    import ctypes  # noqa: PLC0415

    return ctypes.CDLL(None, use_errno=True)


def harden_current_process() -> None:
    """Cho tiến trình chính (giữ khoá API): không cho tiến trình khác cùng người dùng đọc /proc/<pid>/environ, mem."""
    if sys.platform.startswith("linux"):
        with contextlib.suppress(Exception):
            _libc().prctl(PR_SET_DUMPABLE, 0, 0, 0, 0)


def _read_roots() -> list[str]:
    roots = ["/usr", "/lib", "/lib64", "/lib32", "/etc", "/bin", "/sbin", "/opt/python", "/sys/devices/system/cpu"]
    roots += [sys.prefix, sys.base_prefix, sys.exec_prefix, os.path.dirname(sys.executable)]
    roots += [p for p in sys.path if p and os.path.isdir(p)]
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        roots.append(meipass)
    return roots


def _landlock(abi: int, read_files: Iterable[str], write_dirs: Iterable[str]) -> int:
    import ctypes  # noqa: PLC0415

    libc = _libc()
    fs = _LL_ALL_ABI1 | (_LL_REFER if abi >= 2 else 0) | (_LL_TRUNCATE if abi >= 3 else 0)
    fs |= _LL_IOCTL_DEV if abi >= 5 else 0
    net = (_LL_NET_BIND | _LL_NET_CONNECT) if abi >= 4 else 0
    scoped = (_LL_SCOPE_ABSTRACT_UNIX | _LL_SCOPE_SIGNAL) if abi >= 6 else 0
    attr = struct.pack("<QQQ", fs, net, scoped)
    size = 8 if abi < 4 else (16 if abi < 6 else 24)
    buf = ctypes.create_string_buffer(attr[:size], size)
    ruleset = libc.syscall(_NR_LL_CREATE, buf, ctypes.c_size_t(size), 0)
    if ruleset < 0:
        raise OSError(ctypes.get_errno(), "landlock_create_ruleset")

    read_dir = _LL_EXECUTE | _LL_READ_FILE | _LL_READ_DIR
    write_dir = read_dir | _LL_WRITE_FILE | _LL_REMOVE_FILE | _LL_MAKE_REG | (_LL_TRUNCATE if abi >= 3 else 0)

    def add(path: str, access: int) -> None:
        try:
            fd = os.open(path, _O_PATH | _O_CLOEXEC)
        except OSError:
            return
        try:
            if not os.path.isdir(path):
                access &= _LL_FILE_ONLY
            rule = ctypes.create_string_buffer(struct.pack("<Qi", access & fs, fd), 12)
            if libc.syscall(_NR_LL_ADD, ruleset, 1, rule, 0) < 0:
                log.warning("landlock_add_rule %s: errno %s", path, ctypes.get_errno())
        finally:
            os.close(fd)

    seen: set[str] = set()
    for p in _read_roots():
        rp = os.path.realpath(p)
        if rp not in seen:
            seen.add(rp)
            add(rp, read_dir)
    for f in ("/proc/self", "/proc/cpuinfo", "/proc/meminfo", "/proc/stat", "/dev/urandom", "/dev/random"):
        add(os.path.realpath(f), read_dir)
    add("/dev/null", _LL_READ_FILE | _LL_WRITE_FILE)
    for f in read_files:
        add(os.path.realpath(f), _LL_READ_FILE)
    for d in write_dirs:
        add(os.path.realpath(d), write_dir)
    try:
        if libc.syscall(_NR_LL_RESTRICT, ruleset, 0) < 0:
            raise OSError(ctypes.get_errno(), "landlock_restrict_self")
    finally:
        os.close(ruleset)
    return abi


def _fail(status: dict[str, Any], what: str) -> None:
    status["errors"].append(what)
    log.warning("Cô lập tiến trình con: %s", what)


def confine_child_linux(parent_pid: int, read_files: Iterable[str], write_dirs: Iterable[str]) -> dict[str, Any]:
    """Gọi ở đầu tiến trình con, TRƯỚC khi đụng vào dữ liệu không tin cậy. Trả về mức cô lập đạt được.

    Không bao giờ nuốt lỗi: mỗi bước thất bại được ghi vào `status["errors"]` (và log WARNING). Việc QUYẾT ĐỊNH có
    chạy tiếp hay không thuộc về tiến trình cha (`sandbox.run_isolated`), sau khi nhận status này."""
    import ctypes  # noqa: PLC0415
    import signal  # noqa: PLC0415

    libc = _libc()
    status: dict[str, Any] = {
        "platform": "linux",
        "pdeathsig": False,
        "no_new_privs": False,
        "landlock_abi": 0,
        "seccomp": False,
        "signal_filter": False,
        "errors": [],
    }
    if libc.prctl(PR_SET_PDEATHSIG, signal.SIGKILL, 0, 0, 0) == 0:
        status["pdeathsig"] = True
    else:
        _fail(status, f"pdeathsig: errno {ctypes.get_errno()}")
    if os.getppid() != parent_pid:  # cha đã chết trước khi kịp đặt PDEATHSIG
        os._exit(70)
    libc.prctl(PR_SET_DUMPABLE, 0, 0, 0, 0)
    if libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
        # không có NO_NEW_PRIVS thì kernel không cho cài seccomp/Landlock (người dùng thường): báo, không im lặng
        _fail(status, f"no_new_privs: errno {ctypes.get_errno()} — không cài được seccomp/Landlock")
        return status
    status["no_new_privs"] = True
    abi = libc.syscall(_NR_LL_CREATE, None, ctypes.c_size_t(0), 1)  # LANDLOCK_CREATE_RULESET_VERSION
    if abi > 0:
        try:
            status["landlock_abi"] = _landlock(min(abi, 7), read_files, write_dirs)
        except OSError as e:
            _fail(status, f"landlock: {e}")
    else:
        _fail(status, f"landlock: không có (errno {ctypes.get_errno()})")
    machine = _machine()
    if machine in _ARCH:
        code = _seccomp_program(machine, os.getpid())
        n = len(code) // 8
        buf = ctypes.create_string_buffer(code, len(code))
        prog = ctypes.create_string_buffer(struct.pack("<H6xQ", n, ctypes.addressof(buf)), 16)
        nr_seccomp = _ARCH[machine][1]
        if libc.syscall(nr_seccomp, SECCOMP_SET_MODE_FILTER, SECCOMP_FILTER_FLAG_TSYNC, prog) == 0:
            status["seccomp"] = True
            status["signal_filter"] = True
        else:
            _fail(status, f"seccomp: errno {ctypes.get_errno()}")
    else:
        _fail(status, f"seccomp: kiến trúc {machine!r} chưa hỗ trợ")
    return status


_PROBE: dict[str, Any] = {}


def probe_kernel() -> dict[str, Any]:
    """Tiến trình CHA tự đo khả năng kernel (một lần mỗi tiến trình), KHÔNG thay đổi gì ở chính nó:
    - Landlock: `landlock_create_ruleset(NULL, 0, LANDLOCK_CREATE_RULESET_VERSION)` chỉ trả về số ABI;
    - NO_NEW_PRIVS / seccomp-filter: kernel có hỗ trợ khi `/proc/self/status` có dòng `NoNewPrivs:` /
      `Seccomp_filters:` (hoặc `Seccomp:`). Không fork tiến trình nháp (scripts/security_audit.py cấm `os.fork`).
    Dùng để phát hiện tiến trình con báo mức THẤP hơn khả năng thật của kernel (R1-002)."""
    if _PROBE:
        return dict(_PROBE)
    out: dict[str, Any] = {"platform": sys.platform, "landlock_abi": 0, "no_new_privs": False, "seccomp": False}
    if sys.platform.startswith("linux"):
        import ctypes  # noqa: PLC0415

        with contextlib.suppress(Exception):
            v = _libc().syscall(_NR_LL_CREATE, None, ctypes.c_size_t(0), 1)
            out["landlock_abi"] = max(0, int(v))
        with contextlib.suppress(OSError):
            with open("/proc/self/status", encoding="ascii", errors="replace") as f:
                keys = {line.split(":", 1)[0] for line in f}
            out["no_new_privs"] = "NoNewPrivs" in keys
            out["seccomp"] = _machine() in _ARCH and ("Seccomp_filters" in keys or "Seccomp" in keys)
    _PROBE.update(out)
    return dict(out)


# ------------------------------------------------------------------------------------------- Windows


_JOB_FLAGS = 0x2000 | 0x0008 | 0x0100 | 0x0400  # KILL_ON_JOB_CLOSE|ACTIVE_PROCESS|PROCESS_MEMORY|DIE_ON_UNHANDLED
_JOB_KILL_ON_CLOSE, _JOB_ACTIVE_PROCESS = 0x2000, 0x0008


def _win() -> Any:
    """ctypes, wintypes, kernel32 (đã khai báo kiểu) và struct thông tin Job Object (chỉ gọi trên Windows)."""
    import ctypes  # noqa: PLC0415
    from ctypes import wintypes  # noqa: PLC0415

    class BASIC(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class IO(ctypes.Structure):
        _fields_ = [(n, ctypes.c_uint64) for n in ("r", "w", "o", "rb", "wb", "ob")]

    class EXT(ctypes.Structure):
        _fields_ = [
            ("Basic", BASIC),
            ("Io", IO),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    vp, h = ctypes.c_void_p, wintypes.HANDLE
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined,unused-ignore]
    k32.CreateJobObjectW.restype = h
    k32.OpenProcess.restype = h
    k32.GetCurrentProcess.restype = h
    k32.CloseHandle.argtypes = [h]
    k32.SetInformationJobObject.argtypes = [h, ctypes.c_int, vp, wintypes.DWORD]
    k32.QueryInformationJobObject.argtypes = [h, ctypes.c_int, vp, wintypes.DWORD, vp]
    k32.AssignProcessToJobObject.argtypes = [h, h]
    k32.IsProcessInJob.argtypes = [h, h, ctypes.POINTER(wintypes.BOOL)]
    k32.ResumeThread.argtypes = [h]
    k32.ResumeThread.restype = wintypes.DWORD
    k32.TerminateProcess.argtypes = [h, wintypes.UINT]
    k32.LocalFree.argtypes = [vp]
    k32.LocalFree.restype = vp
    return ctypes, wintypes, k32, EXT


def close_handle(handle: int) -> None:
    _win()[2].CloseHandle(handle)


def _last_error(name: str) -> OSError:
    import ctypes  # noqa: PLC0415

    err = ctypes.get_last_error()  # type: ignore[attr-defined,unused-ignore]
    return OSError(err, f"{name}: WinError {err}")


class WindowsJob:
    """Job Object cho một tiến trình con: chết theo tiến trình chính, không sinh tiến trình khác, giới hạn bộ nhớ.
    Không có tác dụng ngoài Windows. Lỗi khi tạo/gán được ghi log và `handle is None` được báo về tiến trình cha như
    một vấn đề cô lập (fail-closed ở `sandbox.evaluate_confine`).

    `process_handle` (2.1.3): gán tiến trình đang SUSPENDED — trước khi nó chạy lệnh nào. Không có thì mở theo `pid`
    (cách của 2.1.2: gán SAU khi tiến trình đã chạy)."""

    def __init__(self, pid: int, memory_mb: int, process_handle: int | None = None) -> None:
        self.handle: Any = None
        if sys.platform != "win32":
            return
        try:
            self._create(pid, memory_mb, process_handle)
        except Exception as e:
            log.warning("Không tạo được Job Object cho tiến trình con: %s", e)

    def _create(self, pid: int, memory_mb: int, process_handle: int | None) -> None:
        ctypes, _wt, k32, EXT = _win()
        job = k32.CreateJobObjectW(None, None)
        if not job:
            raise _last_error("CreateJobObjectW")
        info = EXT()
        info.Basic.LimitFlags = _JOB_FLAGS
        info.Basic.ActiveProcessLimit = 1
        info.ProcessMemoryLimit = memory_mb * 1024 * 1024
        if not k32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):
            err = _last_error("SetInformationJobObject")
            k32.CloseHandle(job)
            raise err
        proc = process_handle or k32.OpenProcess(0x0100 | 0x0001, False, pid)  # SET_QUOTA | TERMINATE
        if not proc:
            err = _last_error("OpenProcess")
            k32.CloseHandle(job)
            raise err
        ok = k32.AssignProcessToJobObject(job, proc)
        err = _last_error("AssignProcessToJobObject")
        if not process_handle:
            k32.CloseHandle(proc)
        if not ok:
            k32.CloseHandle(job)
            raise err
        self.handle = job

    def close(self) -> None:
        if self.handle is not None and sys.platform == "win32":
            _win()[2].CloseHandle(self.handle)
            self.handle = None


# ------------------------------------------------------------------------------------------- Windows: AppContainer
#
# 2.1.3 (L1-001, Windows): tiến trình con chạy trong AppContainer KHÔNG có capability mạng nào (internetClient,
# internetClientServer, privateNetworkClientServer), được tạo ở trạng thái SUSPENDED, gán Job Object rồi mới chạy.
# Quyền file theo ACL:
# - thư viện Python/app: ĐỌC, cấp cho SID của AppContainer (giữ lại — chỉ là mã chương trình; `ensure_library_access`);
# - file đầu vào (ĐỌC) và thư mục tạm của job (GHI): cấp cho một capability SID MỚI, NGẪU NHIÊN của riêng tiến trình con
#   đó (`JobCapability`), gỡ ngay khi tiến trình con kết thúc. Tiến trình con khác (khác capability) không dùng được ACE
#   này; ACE sót lại khi app bị kill chỉ trỏ tới một capability không tiến trình nào còn giữ.
# Ngoài các đường dẫn trên, AppContainer chỉ đọc được những gì Windows cấp sẵn cho "ALL APPLICATION PACKAGES"
# (System32...). HOME, Startup, %APPDATA%... của người dùng: bị từ chối.

APPCONTAINER_NAME = "VietPDF.Sandbox"
ALL_APP_PACKAGES = "S-1-15-2-1"
NETWORK_CAPABILITIES = {
    "S-1-15-3-1": "internetClient",
    "S-1-15-3-2": "internetClientServer",
    "S-1-15-3-3": "privateNetworkClientServer",
}
FILE_READ_EXEC = 0x120089 | 0x1200A0  # FILE_GENERIC_READ | FILE_GENERIC_EXECUTE
FILE_MODIFY = FILE_READ_EXEC | 0x120116 | 0x10000  # + FILE_GENERIC_WRITE + DELETE
ACL_GRANT, ACL_REVOKE = 1, 4  # GRANT_ACCESS, REVOKE_ACCESS
_INHERIT_ALL = 0x3  # OBJECT_INHERIT_ACE | CONTAINER_INHERIT_ACE
_SE_FILE_OBJECT, _DACL_INFO = 1, 0x4
_TOKEN_IS_APPCONTAINER, _TOKEN_CAPABILITIES = 29, 30


def _advapi() -> Any:
    import ctypes  # noqa: PLC0415
    from ctypes import wintypes  # noqa: PLC0415

    vp = ctypes.c_void_p
    adv = ctypes.WinDLL("advapi32", use_last_error=True)  # type: ignore[attr-defined,unused-ignore]
    adv.ConvertSidToStringSidW.argtypes = [vp, ctypes.POINTER(wintypes.LPWSTR)]
    adv.ConvertStringSidToSidW.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(vp)]
    adv.GetLengthSid.argtypes = [vp]
    adv.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    adv.GetTokenInformation.argtypes = [
        wintypes.HANDLE, ctypes.c_int, vp, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
    ]  # fmt: skip
    return adv


def _sid_to_str(sid: Any) -> str:
    import ctypes  # noqa: PLC0415
    from ctypes import wintypes  # noqa: PLC0415

    s = wintypes.LPWSTR()
    if not _advapi().ConvertSidToStringSidW(sid, ctypes.byref(s)):
        raise _last_error("ConvertSidToStringSidW")
    out = s.value or ""
    _win()[2].LocalFree(ctypes.cast(s, ctypes.c_void_p))
    return out


def _sid_from_str(text: str) -> Any:
    """SID nhị phân (bộ đệm ctypes do Python quản lý) từ dạng chuỗi."""
    import ctypes  # noqa: PLC0415

    adv = _advapi()
    p = ctypes.c_void_p()
    if not adv.ConvertStringSidToSidW(text, ctypes.byref(p)):
        raise _last_error("ConvertStringSidToSidW")
    n = adv.GetLengthSid(p)
    buf = ctypes.create_string_buffer(ctypes.string_at(p, n), n)
    _win()[2].LocalFree(p)
    return buf


_AC_SID: dict[str, str] = {}


def appcontainer_sid() -> str:
    """SID (chuỗi) của AppContainer `APPCONTAINER_NAME`; tạo hồ sơ nếu chưa có. Một lần mỗi tiến trình."""
    if "s" in _AC_SID:
        return _AC_SID["s"]
    import ctypes  # noqa: PLC0415

    vp = ctypes.c_void_p
    ue = ctypes.WinDLL("userenv", use_last_error=True)  # type: ignore[attr-defined,unused-ignore]
    ue.CreateAppContainerProfile.restype = ctypes.c_long
    ue.CreateAppContainerProfile.argtypes = [
        ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_wchar_p, vp, ctypes.c_uint32, ctypes.POINTER(vp),
    ]  # fmt: skip
    ue.DeriveAppContainerSidFromAppContainerName.restype = ctypes.c_long
    ue.DeriveAppContainerSidFromAppContainerName.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(vp)]
    adv = _advapi()
    adv.FreeSid.argtypes = [vp]
    sid = vp()
    desc = "VietPDF Translate: tiến trình xử lý PDF bị cô lập"
    hr = ue.CreateAppContainerProfile(APPCONTAINER_NAME, "VietPDF Sandbox", desc, None, 0, ctypes.byref(sid))
    if hr != 0:  # 0x800700B7 = hồ sơ đã tồn tại
        hr2 = ue.DeriveAppContainerSidFromAppContainerName(APPCONTAINER_NAME, ctypes.byref(sid))
        if hr2 != 0:
            raise OSError(hr2 & 0xFFFFFFFF, f"AppContainer: create {hr & 0xFFFFFFFF:#x}, derive {hr2 & 0xFFFFFFFF:#x}")
    try:
        _AC_SID["s"] = _sid_to_str(sid)
    finally:
        adv.FreeSid(sid)
    return _AC_SID["s"]


def new_job_capability() -> str:
    """Capability SID tuỳ biến, ngẫu nhiên (`DeriveCapabilitySidsFromName`), dùng cho đúng MỘT tiến trình con."""
    import ctypes  # noqa: PLC0415
    import secrets  # noqa: PLC0415

    pp = ctypes.POINTER(ctypes.c_void_p)
    u32 = ctypes.POINTER(ctypes.c_uint32)
    kb = ctypes.WinDLL("kernelbase", use_last_error=True)  # type: ignore[attr-defined,unused-ignore]
    kb.DeriveCapabilitySidsFromName.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(pp), u32, ctypes.POINTER(pp), u32]
    groups, ng, caps, nc = pp(), ctypes.c_uint32(), pp(), ctypes.c_uint32()
    name = f"vietpdfJob{secrets.token_hex(16)}"
    by = ctypes.byref
    if not kb.DeriveCapabilitySidsFromName(name, by(groups), by(ng), by(caps), by(nc)):
        raise _last_error("DeriveCapabilitySidsFromName")
    k32 = _win()[2]
    try:
        if nc.value < 1:
            raise OSError(0, "DeriveCapabilitySidsFromName: không có capability SID")
        return _sid_to_str(caps[0])
    finally:
        for arr, n in ((groups, ng.value), (caps, nc.value)):
            for i in range(n):
                k32.LocalFree(arr[i])
            k32.LocalFree(ctypes.cast(arr, ctypes.c_void_p))


def _acl_api() -> Any:
    import ctypes  # noqa: PLC0415
    from ctypes import wintypes  # noqa: PLC0415

    class TRUSTEE(ctypes.Structure):
        _fields_ = [
            ("pMultipleTrustee", ctypes.c_void_p),
            ("MultipleTrusteeOperation", ctypes.c_int),
            ("TrusteeForm", ctypes.c_int),
            ("TrusteeType", ctypes.c_int),
            ("ptstrName", ctypes.c_void_p),
        ]

    class EXPLICIT_ACCESS(ctypes.Structure):
        _fields_ = [
            ("grfAccessPermissions", wintypes.DWORD),
            ("grfAccessMode", ctypes.c_int),
            ("grfInheritance", wintypes.DWORD),
            ("Trustee", TRUSTEE),
        ]

    vp, dw = ctypes.c_void_p, wintypes.DWORD
    adv = ctypes.WinDLL("advapi32", use_last_error=True)  # type: ignore[attr-defined,unused-ignore]
    pvp = ctypes.POINTER(vp)
    adv.GetNamedSecurityInfoW.argtypes = [ctypes.c_wchar_p, ctypes.c_int, dw, vp, vp, pvp, vp, pvp]
    adv.GetNamedSecurityInfoW.restype = dw
    adv.SetNamedSecurityInfoW.argtypes = [ctypes.c_wchar_p, ctypes.c_int, dw, vp, vp, vp, vp]
    adv.SetNamedSecurityInfoW.restype = dw
    adv.SetEntriesInAclW.argtypes = [wintypes.ULONG, ctypes.POINTER(EXPLICIT_ACCESS), vp, ctypes.POINTER(vp)]
    adv.SetEntriesInAclW.restype = dw
    adv.GetEffectiveRightsFromAclW.argtypes = [vp, ctypes.POINTER(TRUSTEE), ctypes.POINTER(dw)]
    adv.GetEffectiveRightsFromAclW.restype = dw
    return ctypes, adv, TRUSTEE, EXPLICIT_ACCESS


def _get_dacl(path: str) -> tuple[Any, Any]:
    ctypes, adv, _t, _e = _acl_api()
    dacl, sd = ctypes.c_void_p(), ctypes.c_void_p()
    by = ctypes.byref
    rc = adv.GetNamedSecurityInfoW(path, _SE_FILE_OBJECT, _DACL_INFO, None, None, by(dacl), None, by(sd))
    if rc != 0:
        raise OSError(rc, f"GetNamedSecurityInfoW({path}): WinError {rc}")
    return dacl, sd


def effective_rights(path: str, sid: str) -> int:
    """Quyền DACL của `path` trao cho đúng SID `sid`."""
    ctypes, adv, TRUSTEE, _e = _acl_api()
    from ctypes import wintypes  # noqa: PLC0415

    dacl, sd = _get_dacl(path)
    try:
        if not dacl:
            return 0x1F01FF  # DACL NULL = mọi quyền
        sid_buf = _sid_from_str(sid)
        t = TRUSTEE(None, 0, 0, 0, ctypes.cast(sid_buf, ctypes.c_void_p))  # TRUSTEE_IS_SID
        mask = wintypes.DWORD()
        rc = adv.GetEffectiveRightsFromAclW(dacl, ctypes.byref(t), ctypes.byref(mask))
        if rc != 0:
            raise OSError(rc, f"GetEffectiveRightsFromAclW: WinError {rc}")
        return int(mask.value)
    finally:
        _win()[2].LocalFree(sd)


def set_acl(path: str, sid: str, access: int, mode: int, inherit: bool) -> None:
    """Thêm (ACL_GRANT) hoặc gỡ mọi ACE (ACL_REVOKE) của `sid` trên `path`; ACE kế thừa được Windows lan xuống."""
    ctypes, adv, TRUSTEE, EXPLICIT_ACCESS = _acl_api()
    k32 = _win()[2]
    dacl, sd = _get_dacl(path)
    try:
        sid_buf = _sid_from_str(sid)
        ea = EXPLICIT_ACCESS()
        ea.grfAccessPermissions = access
        ea.grfAccessMode = mode
        ea.grfInheritance = _INHERIT_ALL if inherit else 0
        ea.Trustee = TRUSTEE(None, 0, 0, 0, ctypes.cast(sid_buf, ctypes.c_void_p))
        new = ctypes.c_void_p()
        rc = adv.SetEntriesInAclW(1, ctypes.byref(ea), dacl, ctypes.byref(new))
        if rc != 0:
            raise OSError(rc, f"SetEntriesInAclW: WinError {rc}")
        try:
            rc = adv.SetNamedSecurityInfoW(path, _SE_FILE_OBJECT, _DACL_INFO, None, None, new, None)
            if rc != 0:
                raise OSError(rc, f"SetNamedSecurityInfoW({path}): WinError {rc}")
        finally:
            k32.LocalFree(new)
    finally:
        k32.LocalFree(sd)


def _norm(path: str) -> str:
    return os.path.normcase(os.path.realpath(path)).rstrip("\\/")


def is_dangerous_root(path: str) -> bool:
    """Không bao giờ cấp quyền kế thừa cho ổ đĩa gốc, HOME, %APPDATA%... hay thư mục cha của chúng (vd. `sys.path`
    chứa '' khi app chạy từ HOME)."""
    p = _norm(path)
    if len(p) <= 3:
        return True
    homes = [os.path.expanduser("~")]
    homes += [os.environ.get(k, "") for k in ("USERPROFILE", "APPDATA", "LOCALAPPDATA", "PUBLIC", "SYSTEMROOT")]
    for h in homes:
        if h:
            hn = _norm(h)
            if p == hn or hn.startswith(p + os.sep):
                return True
    return False


def library_roots(extra: Iterable[str] = ()) -> list[str]:
    """Thư mục mã chương trình tiến trình con cần ĐỌC: cài đặt Python, venv/site-packages, thư mục chứa gói vietpdf,
    `_MEIPASS` (bản đóng gói), cùng `extra` (thư mục module của hàm chạy trong tiến trình con). Thư mục con của một thư
    mục đã có trong danh sách bị bỏ."""
    import site  # noqa: PLC0415

    import vietpdf  # noqa: PLC0415

    cands = [sys.base_prefix, sys.base_exec_prefix, sys.prefix, sys.exec_prefix]
    with contextlib.suppress(Exception):
        cands += site.getsitepackages()
    cands.append(os.path.dirname(os.path.dirname(os.path.abspath(vietpdf.__file__))))
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        cands.append(meipass)
    cands += list(extra)
    real: list[str] = []
    for c in cands:
        if c and os.path.isdir(c):
            r = _norm(c)
            if r not in real:
                real.append(r)
    real.sort(key=len)
    out: list[str] = []
    for r in real:
        if not any(r == o or r.startswith(o + os.sep) for o in out):
            out.append(r)
    return out


_LIB_OK: set[str] = set()


def ensure_library_access(roots: Iterable[str]) -> list[str]:
    """Bảo đảm AppContainer ĐỌC được các thư mục thư viện. Trả về danh sách lỗi (rỗng = đạt).

    Đã đọc được (ACE cho SID của app, hoặc cho ALL APPLICATION PACKAGES do bộ cài cấp) thì không đổi gì. Không thì thêm
    ACE đọc kế thừa cho SID của app — cần quyền WRITE_DAC (người dùng sở hữu thư mục). ACE này được giữ lại (chỉ là mã
    chương trình): lần chạy sau và các tiến trình app chạy đồng thời không phải cấp lại, và không gỡ mất của nhau."""
    sid = appcontainer_sid()
    errors = []
    for r in roots:
        if r in _LIB_OK:
            continue
        if is_dangerous_root(r):
            errors.append(f"không cấp quyền cho thư mục quá rộng: {r}")
            continue
        try:
            have = effective_rights(r, sid) | effective_rights(r, ALL_APP_PACKAGES)
            if have & FILE_READ_EXEC != FILE_READ_EXEC:
                set_acl(r, sid, FILE_READ_EXEC, ACL_GRANT, inherit=True)
            _LIB_OK.add(r)
        except OSError as e:
            errors.append(
                f"AppContainer không đọc được thư viện {r} ({e}); quản trị viên có thể cấp: "
                f'icacls "{r}" /grant *{ALL_APP_PACKAGES}:(OI)(CI)(RX)'
            )
    return errors


def token_info(process_handle: int | None = None) -> dict[str, Any]:
    """Đọc token của một tiến trình (mặc định: chính mình): có ở trong AppContainer không, có những capability nào."""
    ctypes, wintypes, k32, _ = _win()
    adv = _advapi()
    out: dict[str, Any] = {"appcontainer": False, "capabilities": None, "errors": []}
    tok = wintypes.HANDLE()
    proc = process_handle if process_handle is not None else k32.GetCurrentProcess()
    if not adv.OpenProcessToken(proc, 0x0008, ctypes.byref(tok)):  # TOKEN_QUERY
        out["errors"].append(str(_last_error("OpenProcessToken")))
        return out
    try:
        val, ret = wintypes.DWORD(), wintypes.DWORD()
        if adv.GetTokenInformation(tok, _TOKEN_IS_APPCONTAINER, ctypes.byref(val), 4, ctypes.byref(ret)):
            out["appcontainer"] = bool(val.value)
        else:
            out["errors"].append(str(_last_error("GetTokenInformation(TokenIsAppContainer)")))
        caps: list[str] = []
        adv.GetTokenInformation(tok, _TOKEN_CAPABILITIES, None, 0, ctypes.byref(ret))
        if ret.value:
            buf = ctypes.create_string_buffer(ret.value)
            if not adv.GetTokenInformation(tok, _TOKEN_CAPABILITIES, buf, ret.value, ctypes.byref(ret)):
                out["errors"].append(str(_last_error("GetTokenInformation(TokenCapabilities)")))
            else:
                ptr = ctypes.sizeof(ctypes.c_void_p)
                count = ctypes.c_uint32.from_buffer(buf).value  # TOKEN_GROUPS.GroupCount (+ đệm tới ptr)
                for i in range(count):
                    caps.append(_sid_to_str(ctypes.c_void_p.from_buffer(buf, ptr + i * 2 * ptr)))
        out["capabilities"] = caps
    finally:
        k32.CloseHandle(tok)
    return out


class WindowsSandboxProcess:
    """Tạo tiến trình con Windows: AppContainer (+ capability của job) ở trạng thái SUSPENDED, gán Job Object, đọc lại
    token của nó từ phía cha, rồi mới cho chạy. Chỉ kế thừa ĐÚNG các handle được liệt kê
    (`PROC_THREAD_ATTRIBUTE_HANDLE_LIST`). `appcontainer=False` chỉ dùng khi người dùng đã cho phép cô lập yếu và lớp
    AppContainer không dựng được: chỉ Job Object như 2.1.2."""

    def __init__(
        self,
        exe: str,
        cmdline: str,
        env: dict[str, str],
        cwd: str | None,
        inherit: list[int],
        memory_mb: int,
        appcontainer: bool,
        capability: str | None,
    ) -> None:
        import ctypes  # noqa: PLC0415
        from ctypes import wintypes  # noqa: PLC0415

        k32 = _win()[2]
        vp, dw, h = ctypes.c_void_p, wintypes.DWORD, wintypes.HANDLE

        class SID_AND_ATTRIBUTES(ctypes.Structure):
            _fields_ = [("Sid", vp), ("Attributes", dw)]

        class SECURITY_CAPABILITIES(ctypes.Structure):
            _fields_ = [
                ("AppContainerSid", vp),
                ("Capabilities", ctypes.POINTER(SID_AND_ATTRIBUTES)),
                ("CapabilityCount", dw),
                ("Reserved", dw),
            ]

        class STARTUPINFOW(ctypes.Structure):
            _fields_ = [
                ("cb", dw), ("lpReserved", vp), ("lpDesktop", vp), ("lpTitle", vp), ("dwX", dw), ("dwY", dw),
                ("dwXSize", dw), ("dwYSize", dw), ("dwXCountChars", dw), ("dwYCountChars", dw),
                ("dwFillAttribute", dw), ("dwFlags", dw), ("wShowWindow", wintypes.WORD),
                ("cbReserved2", wintypes.WORD), ("lpReserved2", vp), ("hStdInput", h), ("hStdOutput", h),
                ("hStdError", h),
            ]  # fmt: skip

        class STARTUPINFOEXW(ctypes.Structure):
            _fields_ = [("StartupInfo", STARTUPINFOW), ("lpAttributeList", vp)]

        class PROCESS_INFORMATION(ctypes.Structure):
            _fields_ = [("hProcess", h), ("hThread", h), ("dwProcessId", dw), ("dwThreadId", dw)]

        k32.InitializeProcThreadAttributeList.argtypes = [vp, dw, dw, ctypes.POINTER(ctypes.c_size_t)]
        k32.UpdateProcThreadAttribute.argtypes = [vp, dw, ctypes.c_size_t, vp, ctypes.c_size_t, vp, vp]
        k32.DeleteProcThreadAttributeList.argtypes = [vp]
        k32.CreateProcessW.argtypes = [
            ctypes.c_wchar_p, ctypes.c_wchar_p, vp, vp, wintypes.BOOL, dw, ctypes.c_wchar_p, ctypes.c_wchar_p, vp,
            ctypes.POINTER(PROCESS_INFORMATION),
        ]  # fmt: skip

        self.errors: list[str] = []
        keep: list[Any] = []  # giữ các bộ đệm sống tới khi CreateProcessW xong
        n_attr = 1 + (1 if appcontainer else 0)
        size = ctypes.c_size_t()
        k32.InitializeProcThreadAttributeList(None, n_attr, 0, ctypes.byref(size))
        attrs = ctypes.create_string_buffer(size.value)
        if not k32.InitializeProcThreadAttributeList(attrs, n_attr, 0, ctypes.byref(size)):
            raise _last_error("InitializeProcThreadAttributeList")
        pi = PROCESS_INFORMATION()
        try:
            handles = (h * max(1, len(inherit)))(*inherit)
            if inherit and not k32.UpdateProcThreadAttribute(
                attrs, 0, 0x20002, handles, ctypes.sizeof(h) * len(inherit), None, None
            ):  # PROC_THREAD_ATTRIBUTE_HANDLE_LIST
                raise _last_error("UpdateProcThreadAttribute(HANDLE_LIST)")
            if appcontainer:
                ac_sid = _sid_from_str(appcontainer_sid())
                caps = (SID_AND_ATTRIBUTES * 1)()
                n_caps = 0
                if capability:
                    cap_sid = _sid_from_str(capability)
                    keep.append(cap_sid)
                    caps[0] = SID_AND_ATTRIBUTES(ctypes.cast(cap_sid, vp), 0x4)  # SE_GROUP_ENABLED
                    n_caps = 1
                sc = SECURITY_CAPABILITIES(ctypes.cast(ac_sid, vp), caps, n_caps, 0)
                keep += [ac_sid, caps, sc]
                by = ctypes.byref
                if not k32.UpdateProcThreadAttribute(attrs, 0, 0x20009, by(sc), ctypes.sizeof(sc), None, None):
                    raise _last_error("UpdateProcThreadAttribute(SECURITY_CAPABILITIES)")
            si = STARTUPINFOEXW()
            si.StartupInfo.cb = ctypes.sizeof(si)
            si.lpAttributeList = ctypes.cast(attrs, vp)
            block = "".join(f"{k}={v}\0" for k, v in sorted(env.items(), key=lambda kv: kv[0].upper())) + "\0"
            # CREATE_SUSPENDED | CREATE_UNICODE_ENVIRONMENT | EXTENDED_STARTUPINFO_PRESENT | CREATE_NO_WINDOW
            flags = 0x4 | 0x400 | 0x80000 | 0x08000000
            cmd = ctypes.create_unicode_buffer(cmdline)
            by = ctypes.byref
            if not k32.CreateProcessW(exe, cmd, None, None, bool(inherit), flags, block, cwd, by(si), by(pi)):
                raise _last_error("CreateProcessW")
        finally:
            k32.DeleteProcThreadAttributeList(attrs)
        self.process_handle = int(pi.hProcess)
        self.pid = int(pi.dwProcessId)
        self.job = WindowsJob(self.pid, memory_mb, process_handle=self.process_handle)
        if self.job.handle is None:
            self.errors.append("không gán được Job Object cho tiến trình con")
        self.token = token_info(self.process_handle)
        if k32.ResumeThread(pi.hThread) == 0xFFFFFFFF:
            self.errors.append(str(_last_error("ResumeThread")))
            k32.TerminateProcess(pi.hProcess, 1)
        k32.CloseHandle(pi.hThread)


def confine_child_windows() -> dict[str, Any]:
    """Gọi ở đầu tiến trình con Windows, TRƯỚC khi đụng dữ liệu: tự đọc token và Job Object của chính mình rồi báo về
    tiến trình cha (tiến trình cha còn tự đọc token của tiến trình con để đối chiếu — `WindowsSandboxProcess.token`)."""
    ctypes, wintypes, k32, EXT = _win()
    tok = token_info()
    status: dict[str, Any] = {
        "platform": "win32",
        "appcontainer": tok["appcontainer"],
        "capabilities": tok["capabilities"],
        "job_object": False,
        "errors": list(tok["errors"]),
    }
    in_job = wintypes.BOOL()
    if not k32.IsProcessInJob(k32.GetCurrentProcess(), None, ctypes.byref(in_job)):
        _fail(status, str(_last_error("IsProcessInJob")))
    elif not in_job.value:
        _fail(status, "tiến trình con không nằm trong Job Object")
    else:
        info = EXT()
        if k32.QueryInformationJobObject(None, 9, ctypes.byref(info), ctypes.sizeof(info), None):
            fl = int(info.Basic.LimitFlags)
            ok = bool(fl & _JOB_KILL_ON_CLOSE and fl & _JOB_ACTIVE_PROCESS and info.Basic.ActiveProcessLimit == 1)
            status["job_object"] = ok
            if not ok:
                _fail(status, f"Job Object thiếu giới hạn (flags {fl:#x})")
        else:
            _fail(status, str(_last_error("QueryInformationJobObject")))
    return status


def appcontainer_supported() -> bool:
    """Windows 10 1607+: có `CreateAppContainerProfile` và `DeriveCapabilitySidsFromName`."""
    if sys.platform != "win32":
        return False
    import ctypes  # noqa: PLC0415

    try:
        kb = ctypes.WinDLL("kernelbase")  # type: ignore[attr-defined,unused-ignore]
        ue = ctypes.WinDLL("userenv")  # type: ignore[attr-defined,unused-ignore]
        return bool(kb.DeriveCapabilitySidsFromName and ue.CreateAppContainerProfile)
    except (OSError, AttributeError):
        return False
