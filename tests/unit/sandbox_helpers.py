"""Hàm cấp module để chạy trong tiến trình con khi test sandbox."""

from __future__ import annotations

import os
import time
from typing import Any


def add(a: int, b: int) -> int:
    return a + b


def sleep_forever() -> None:
    time.sleep(60)


def boom() -> None:
    raise ValueError("hỏng")


def hard_exit() -> None:
    os._exit(9)


def allocate(mb: int) -> int:
    return len(bytearray(mb * 1024 * 1024))


def read_env(name: str) -> str | None:
    return os.environ.get(name)


def try_network() -> None:
    import socket

    socket.create_connection(("example.com", 80), timeout=2)


def allocate_slowly(mb: int) -> int:
    chunks = []
    for _ in range(mb // 10):
        chunks.append(bytearray(10 * 1024 * 1024))
        time.sleep(0.02)
    return len(chunks)


def noisy() -> int:
    print("NOISE NOISE")
    return 1


# ---- 2.1.1: PoC chạy bên trong tiến trình con (mô phỏng parser bị chiếm quyền) — mỗi hàm trả "ok"/"blocked: ..."


def _attempt(fn: Any) -> str:
    try:
        fn()
    except Exception as e:
        return f"blocked: {type(e).__name__}"
    return "ok"


def try_raw_socket(port: int) -> str:
    def go() -> None:
        import _socket

        s = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
        s.connect(("127.0.0.1", port))

    return _attempt(go)


def try_ctypes_socket() -> str:
    def go() -> None:
        import ctypes

        libc = ctypes.CDLL(None, use_errno=True)
        if libc.socket(2, 1, 0) < 0:
            raise OSError(ctypes.get_errno(), "socket")

    return _attempt(go)


def try_subprocess() -> str:
    import subprocess
    import sys

    return _attempt(lambda: subprocess.run([sys.executable, "-c", "print(1)"], check=True, capture_output=True))


def try_fork() -> str:
    def go() -> None:
        pid = os.fork()
        if pid == 0:
            os._exit(0)
        os.waitpid(pid, 0)

    return _attempt(go)


def try_read(path: str) -> str:
    return _attempt(lambda: open(path, "rb").read(1))


def try_write(path: str) -> str:
    def go() -> None:
        with open(path, "w") as f:
            f.write("x")

    return _attempt(go)


def env_snapshot() -> dict[str, str]:
    return dict(os.environ)


def hang(marker: str) -> None:
    with open(marker, "w") as f:  # được phép: thư mục marker nằm trong write_dirs
        f.write(str(os.getpid()))
    while True:
        time.sleep(1)


def try_unix_connect(path: str) -> str:
    """Kết nối tới socket UNIX có sẵn (vd. D-Bus/Secret Service) — phải bị seccomp chặn (`connect`)."""

    def go() -> None:
        import socket

        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.connect(path)

    return _attempt(go)


# ---- 2.1.2 (R1-001, R1-002): PoC chạy bên trong tiến trình con


def write_marker(path: str) -> str:
    """Nếu hàm này chạy được (khi lẽ ra phải bị từ chối vì cô lập yếu) thì file đánh dấu xuất hiện."""
    with open(path, "w") as f:
        f.write("func đã chạy")
    return "ran"


def _home_attempts(home: str) -> dict[str, str]:
    j = os.path.join
    res = {}

    def read_ssh() -> None:
        with open(j(home, ".ssh", "id_canary"), "rb") as f:
            f.read()

    def append_bashrc() -> None:
        with open(j(home, ".bashrc"), "a") as f:
            f.write("\n# PWNED\n")

    res["read_ssh"] = _attempt(read_ssh)
    res["append_bashrc"] = _attempt(append_bashrc)

    def autostart() -> None:
        os.makedirs(j(home, ".config", "autostart"), exist_ok=True)
        with open(j(home, ".config", "autostart", "pwn.desktop"), "w") as f:
            f.write("[Desktop Entry]\nExec=true\n")

    res["autostart"] = _attempt(autostart)
    res["delete_docx"] = _attempt(lambda: os.unlink(j(home, "Documents", "x.docx")))

    def lockfile() -> None:
        os.makedirs(j(home, ".config", "vietpdf"), exist_ok=True)
        with open(j(home, ".config", "vietpdf", "models.lock.json"), "w") as f:
            f.write("{}")

    res["models_lock"] = _attempt(lockfile)
    return res


def hijack_render(src_path: str, out_path: str, ir_json: str, options: Any = None) -> Any:
    """Thay `render.render_document` (hook chỉ có trong test): mô phỏng MuPDF bị chiếm quyền trong tiến trình dựng PDF
    — thử đọc/ghi/xoá trong HOME, rồi vẫn dựng PDF thật và GIẤU kết quả vào metadata của PDF đầu ra (đường rò dữ liệu
    duy nhất còn lại) để test đọc lại."""
    import json

    import pymupdf

    from vietpdf.core import render

    attempts = _home_attempts(os.path.expanduser("~"))
    rep = render.render_document(src_path, out_path, ir_json, options)
    d = pymupdf.open(out_path)
    d.set_metadata({"keywords": json.dumps(attempts)})
    tmp = out_path + ".2.pdf"
    d.save(tmp)
    d.close()
    os.replace(tmp, out_path)
    return rep


def try_special_files(stage: str) -> dict[str, str]:
    """Tiến trình con cố để lại symlink/hardlink/FIFO làm 'file đầu ra' trong thư mục tạm của job."""
    j = os.path.join
    return {
        "symlink": _attempt(lambda: os.symlink("/etc/passwd", j(stage, "out0.pdf"))),
        "fifo": _attempt(lambda: os.mkfifo(j(stage, "out1.pdf"))),
        "hardlink": _attempt(lambda: os.link("/etc/hostname", j(stage, "out2.pdf"))),
    }


def try_signals() -> dict[str, str]:
    """kill(getppid(), SIGTERM) và kill(0, SIGTERM): phải bị từ chối (EPERM) — kể cả khi không có Landlock."""
    import signal

    return {
        "kill_parent": _attempt(lambda: os.kill(os.getppid(), signal.SIGTERM)),
        "kill_group": _attempt(lambda: os.kill(0, signal.SIGTERM)),
        "kill_self_0": _attempt(lambda: os.kill(os.getpid(), 0)),
    }


def install_no_landlock(block_nnp: bool = False) -> None:
    """Giả lập kernel KHÔNG có Landlock cho tiến trình hiện tại và mọi tiến trình con (cách của repro_r noll.c):
    bộ lọc seccomp trả ENOSYS cho syscall 444–446 (landlock_*). `block_nnp=True`: thêm prctl(PR_SET_NO_NEW_PRIVS)
    => EPERM (giả lập NO_NEW_PRIVS thất bại). Chỉ x86_64/aarch64 (số syscall Landlock giống nhau)."""
    import ctypes
    import struct

    libc = ctypes.CDLL(None, use_errno=True)

    def ins(code: int, jt: int, jf: int, k: int) -> bytes:
        return struct.pack("<HBBI", code, jt, jf, k)

    ld, jeq, jge, jgt, ret = 0x20, 0x15, 0x35, 0x25, 0x06
    allow, enosys, eperm = 0x7FFF0000, 0x00050000 | 38, 0x00050000 | 1
    nr_prctl = 157 if os.uname().machine == "x86_64" else 167
    prog = [
        ins(ld, 0, 0, 0),
        ins(jge, 0, 2, 444),
        ins(jgt, 1, 0, 446),
        ins(ret, 0, 0, enosys),
    ]
    if block_nnp:
        prog += [
            ins(jeq, 0, 3, nr_prctl),
            ins(ld, 0, 0, 16),
            ins(jeq, 0, 1, 38),
            ins(ret, 0, 0, eperm),
        ]
    prog.append(ins(ret, 0, 0, allow))
    code = b"".join(prog)
    buf = ctypes.create_string_buffer(code, len(code))
    fprog = ctypes.create_string_buffer(struct.pack("<H6xQ", len(prog), ctypes.addressof(buf)), 16)
    if libc.prctl(38, 1, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "prctl NO_NEW_PRIVS")
    nr_seccomp = 317 if os.uname().machine == "x86_64" else 277
    if libc.syscall(nr_seccomp, 1, 0, fprog) != 0:
        raise OSError(ctypes.get_errno(), "seccomp")


def render_then_inject_js(src_path: str, out_path: str, ir_json: str, options: Any = None) -> Any:
    """R1-004: mô phỏng bước làm sạch bỏ sót — dựng PDF thật rồi cài thêm hành động JavaScript vào file đầu ra (trong
    thư mục tạm của job). Chốt kiểm cuối ở tiến trình cha phải phát hiện, làm sạch lại một lần rồi công bố."""
    import pymupdf

    from vietpdf.core import render

    rep = render.render_document(src_path, out_path, ir_json, options)
    d = pymupdf.open(out_path)
    x = d.get_new_xref()
    d.update_object(
        x, "<< /Type /Annot /Subtype /Link /Rect [10 10 50 30] /A << /S /JavaScript /JS (app.alert\\(1\\)) >> >>"
    )
    d.xref_set_key(d.page_xref(0), "Annots", f"[{x} 0 R]")
    tmp = out_path + ".js.pdf"
    d.save(tmp)
    d.close()
    os.replace(tmp, out_path)
    return rep


def allocate_string(mb: int) -> Any:
    """R1-007: kết quả JSON khoảng `mb` MB (mb > 1) hoặc số nhỏ (mb <= 1)."""
    return 1 if mb <= 1 else "x" * (mb * 1024 * 1024)


def try_dns(name: str) -> str:
    """Tra DNS bằng `_socket` (C) — vượt qua lớp chặn mạng mức Python, chỉ còn lớp hệ điều hành."""

    def go() -> None:
        import _socket

        _socket.getaddrinfo(name, 80)

    return _attempt(go)


def try_tcp_native(port: int) -> str:
    """Kết nối TCP tới 127.0.0.1:port bằng `_socket` (C)."""

    def go() -> None:
        import _socket

        s = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
        s.settimeout(5)
        s.connect(("127.0.0.1", port))
        s.sendall(b"exfil")

    return _attempt(go)


def write_with_ads(path: str, src: str) -> str:
    """Windows: ghi file đích (nội dung `src`) kèm một luồng dữ liệu phụ `path:hidden`."""

    def go() -> None:
        with open(src, "rb") as f:
            data = f.read()
        with open(path, "wb") as f:
            f.write(data)
        with open(path + ":hidden", "wb") as f:
            f.write(b"payload")

    return _attempt(go)


def try_win_attempts(canary: str, startup_file: str, stage: str, port: int) -> dict[str, str]:
    """2.1.3 (Windows): mô phỏng parser bị chiếm quyền trong tiến trình con — mỗi mục "ok"/"blocked: ..."."""
    import subprocess
    import sys

    def write_stage() -> None:
        with open(os.path.join(stage, "out0.pdf"), "wb") as f:
            f.write(b"%PDF-1.7\n")

    return {
        "read_home_canary": try_read(canary),
        "write_startup": try_write(startup_file),
        "tcp_loopback": try_tcp_native(port),
        "dns": try_dns("example.com"),
        "subprocess": _attempt(lambda: subprocess.run([sys.executable, "-c", "0"], check=True, timeout=20)),
        "write_stage": _attempt(write_stage),
    }


def try_dgram_and_ns(path: str) -> dict[str, str]:
    """R1-008: gửi tới socket UNIX datagram có đường dẫn (sendto/sendmsg), unshare, memfd_create, keyctl."""
    import ctypes
    import socket

    libc = ctypes.CDLL(None, use_errno=True)

    def sendto() -> None:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        s.sendto(b"exfil", path)

    def sendmsg() -> None:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        s.sendmsg([b"exfil"], [], 0, path)

    def check(rc: int, name: str) -> None:
        if rc < 0:
            raise OSError(ctypes.get_errno(), name)

    nr_keyctl = 250 if os.uname().machine == "x86_64" else 219
    return {
        "sendto": _attempt(sendto),
        "sendmsg": _attempt(sendmsg),
        "unshare_newuser": _attempt(lambda: check(libc.unshare(0x10000000), "unshare")),
        "memfd_create": _attempt(lambda: check(libc.memfd_create(b"x", 0), "memfd_create")),
        "keyctl": _attempt(lambda: check(libc.syscall(nr_keyctl, 0, -3, 0), "keyctl")),
    }
