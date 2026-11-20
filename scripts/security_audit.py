#!/usr/bin/env python3
"""Kiểm tra tĩnh chống mã độc / cửa hậu cho mã nguồn. Chạy trong CI ở mọi pull request.

Phân tích AST (không chạy mã) và báo lỗi khi gặp:
  - Thực thi mã động: eval, exec, compile, __import__, importlib.import_module với tham số động
  - Giải tuần tự hoá nguy hiểm: pickle, marshal, shelve, dill, joblib, torch.load, yaml.load
  - Chạy lệnh hệ thống: subprocess, os.system, os.popen, os.exec*, os.spawn*, pty, ctypes
  - Truy cập mạng ngoài lớp bảo mật: socket, urllib.request, http.client, requests, httpx, aiohttp...
    (chỉ src/vietpdf/security/http.py được phép import httpx)
  - Dấu hiệu làm rối mã: chuỗi base64/hex rất dài, dòng quá dài, ký tự Unicode điều khiển hướng (Trojan Source)
  - Tắt xác minh TLS (verify=False)

Dùng: python scripts/security_audit.py [thư_mục ...]   (mặc định: src)
"""

from __future__ import annotations

import ast
import re
import sys
from dataclasses import dataclass
from pathlib import Path

NETWORK_GATEWAY = "src/vietpdf/security/http.py"
BANNED_IMPORTS = {
    "pickle",
    "cPickle",
    "marshal",
    "shelve",
    "dill",
    "joblib",
    "cloudpickle",
    "subprocess",
    "pty",
    "ctypes",
    "cffi",
    "telnetlib",
    "ftplib",
    "smtplib",
    "paramiko",
    "socket",
    "ssl",
    "urllib.request",
    "urllib3",
    "http.client",
    "requests",
    "aiohttp",
    "httpx",
    "httpcore",
    "websocket",
    "websockets",
    "importlib",
    "code",
    "codeop",
    "runpy",
    "webbrowser",
    "_socket",
    "_posixsubprocess",
    "_winapi",
    "asyncio.subprocess",
    "multiprocessing.popen_spawn_posix",
    "multiprocessing.popen_fork",
}
GATEWAY_ALLOWED = {"httpx"}
# Ngoại lệ có chủ đích, ghi rõ lý do. Thêm ngoại lệ mới phải qua CODEOWNERS.
FILE_EXCEPTIONS: dict[str, set[str]] = {
    NETWORK_GATEWAY: {"httpx"},  # cửa ngõ mạng duy nhất
    "src/vietpdf/core/sandbox.py": {"socket"},  # chỉ để VÔ HIỆU HOÁ mạng trong tiến trình con
    "src/vietpdf/core/confine.py": {"ctypes"},  # prctl/seccomp/Landlock/Job Object để CÔ LẬP tiến trình con
}
# Test được dùng thư viện mạng/tiến trình để dựng máy chủ giả và chạy CLI; vẫn bị quét các luật còn lại (eval,
# pickle, obfuscation...). Danh sách theo file, có lý do, nằm dưới CODEOWNERS.
TEST_EXCEPTIONS: dict[str, set[str]] = {
    "tests/unit/sandbox_helpers.py": {"socket", "subprocess", "ctypes", "_socket", "os"},  # PoC trong tiến trình con
    "tests/unit/test_learning.py": {"subprocess"},
    "tests/unit/test_sandbox_audit.py": {"subprocess"},
    "tests/unit/test_security_http.py": {"httpx"},
    "tests/unit/test_translate_layer.py": {"httpx"},
    "tests/unit/test_v15_v2.py": {"httpx"},
    "tests/unit/test_v1_terms_security.py": {"httpx"},
    "tests/unit/test_hardening_211.py": {"httpx", "socket", "subprocess", "ctypes"},
    # 2.1.2: chạy tiến trình nháp không có Landlock (bộ lọc seccomp cài bằng ctypes trong tiến trình con của pytest)
    "tests/unit/test_isolation_212.py": {"subprocess", "ctypes"},
    "tests/unit/test_p2_212.py": {"socket"},  # R1-008: socket UNIX datagram nghe ở tiến trình cha
    "tests/unit/test_isolation_win_213.py": {"socket"},  # 2.1.3: máy chủ TCP loopback ở tiến trình cha (PoC)
}
TEST_DYNAMIC_OK = {"tests/golden/test_hard_pdf.py", "tests/unit/test_gui_learning.py"}  # __import__ để bỏ qua test
# Mọi URL viết cứng trong src/ phải trỏ tới một trong các host này (chống chèn địa chỉ gửi dữ liệu ra ngoài).
ALLOWED_URL_HOSTS = {
    "generativelanguage.googleapis.com",
    "api.openai.com",
    "api.anthropic.com",
    "api.deepseek.com",
    "openrouter.ai",
    "api.groq.com",
    "localhost",
    "127.0.0.1",
    "api.semanticscholar.org",
    "api.openalex.org",
    "huggingface.co",
    "api.github.com",
    "github.com",
    "arxiv.org",
    "doi.org",
    "www.semanticscholar.org",
}
URL_RE = re.compile(r"https?://([A-Za-z0-9.\-]+)")
BANNED_CALLS = {"eval", "exec", "compile", "__import__", "breakpoint"}
# `from os import system` / `import os as o; o.system()`: bắt theo TÊN HÀM sau khi giải alias (L1-023)
BANNED_FUNCS = {
    ("os", "system"), ("os", "popen"), ("os", "startfile"), ("os", "fork"), ("os", "forkpty"),
    ("os", "posix_spawn"), ("os", "posix_spawnp"), ("os", "execl"), ("os", "execle"), ("os", "execlp"),
    ("os", "execlpe"), ("os", "execv"), ("os", "execve"), ("os", "execvp"), ("os", "execvpe"), ("os", "spawnl"),
    ("os", "spawnle"), ("os", "spawnlp"), ("os", "spawnlpe"), ("os", "spawnv"), ("os", "spawnve"), ("os", "spawnvp"),
    ("os", "spawnvpe"), ("asyncio", "create_subprocess_exec"), ("asyncio", "create_subprocess_shell"),
    ("multiprocessing.connection", "Client"), ("multiprocessing.connection", "Listener"),
    ("yaml", "load"), ("yaml", "unsafe_load"), ("tempfile", "mktemp"), ("builtins", "eval"), ("builtins", "exec"),
    # 2.1.2 (L1-023): gọi qua sys.modules["subprocess"] mà không import
    ("subprocess", "run"), ("subprocess", "Popen"), ("subprocess", "call"), ("subprocess", "check_call"),
    ("subprocess", "check_output"), ("subprocess", "getoutput"), ("subprocess", "getstatusoutput"),
}  # fmt: skip
BANNED_ATTR_CALLS = {
    ("os", "system"),
    ("os", "popen"),
    ("os", "execv"),
    ("os", "execve"),
    ("os", "execl"),
    ("os", "execvp"),
    ("os", "spawnl"),
    ("os", "spawnv"),
    ("os", "startfile"),
    ("os", "fork"),
    ("os", "forkpty"),
    ("yaml", "load"),
    ("yaml", "unsafe_load"),
    ("torch", "load"),
    ("numpy", "load"),
    ("np", "load"),
    ("tempfile", "mktemp"),
}
BIDI = re.compile(r"[\u202a-\u202e\u2066-\u2069\u200b-\u200f\u2060\u00ad]")  # bidi + ký tự vô hình
LONG_BLOB = re.compile(r"[A-Za-z0-9+/=]{300,}|(?:\\x[0-9a-fA-F]{2}){60,}|[0-9a-fA-F]{300,}")
MAX_LINE = 200


@dataclass
class Finding:
    path: str
    line: int
    rule: str
    detail: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: [{self.rule}] {self.detail}"


class Visitor(ast.NodeVisitor):
    def __init__(self, path: str) -> None:
        self.path = path
        self.findings: list[Finding] = []
        norm = path.replace("\\", "/")
        exceptions = {**FILE_EXCEPTIONS, **TEST_EXCEPTIONS}
        self.allowed = next((v for k, v in exceptions.items() if norm.endswith(k)), set())
        self.dynamic_ok = any(norm.endswith(k) for k in TEST_DYNAMIC_OK)
        self.aliases: dict[str, str] = {}  # tên cục bộ -> module/hàm đầy đủ

    def _add(self, node: ast.AST, rule: str, detail: str) -> None:
        self.findings.append(Finding(self.path, getattr(node, "lineno", 0), rule, detail))

    def _check_module(self, node: ast.AST, name: str) -> None:
        for banned in BANNED_IMPORTS:
            if name == banned or name.startswith(banned + "."):
                if banned in self.allowed:
                    return
                self._add(node, "banned-import", f"import '{name}' không được phép")

    def visit_Import(self, node: ast.Import) -> None:
        for a in node.names:
            self._check_module(node, a.name)
            self.aliases[a.asname or a.name.split(".")[0]] = a.name if a.asname else a.name.split(".")[0]
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.module and node.level == 0:
            self._check_module(node, node.module)
            for a in node.names:
                self._check_module(node, f"{node.module}.{a.name}")
                if (node.module, a.name) in BANNED_FUNCS and node.module not in self.allowed:
                    self._add(node, "dangerous-call", f"from {node.module} import {a.name} không được phép")
                self.aliases[a.asname or a.name] = f"{node.module}.{a.name}"
        self.generic_visit(node)

    def _qualname(self, f: ast.AST) -> str | None:
        parts: list[str] = []
        while isinstance(f, ast.Attribute):
            parts.append(f.attr)
            f = f.value
        # sys.modules["os"].system(...) / importlib.import_module("os").system(...) (L1-023, 2.1.2): coi như os.system
        if (
            isinstance(f, ast.Subscript)
            and isinstance(f.slice, ast.Constant)
            and isinstance(f.slice.value, str)
            and self._qualname(f.value) in ("sys.modules", "builtins.__dict__", "globals", "vars")
        ):
            return ".".join([f.slice.value, *reversed(parts)])
        if not isinstance(f, ast.Name):
            return None
        parts.append(self.aliases.get(f.id, f.id))
        return ".".join(reversed(parts))

    def visit_Call(self, node: ast.Call) -> None:
        f = node.func
        q = self._qualname(f)
        reported = False
        if q and "." in q:
            mod, _, name = q.rpartition(".")
            if (mod, name) in BANNED_FUNCS:
                reported = True  # đã xét theo tên đầy đủ (kể cả alias) — không báo lặp ở luật bên dưới
                if mod.split(".")[0] not in self.allowed:
                    self._add(node, "dangerous-call", f"gọi '{q}()' không được phép")
        if isinstance(f, ast.Name) and f.id in BANNED_CALLS and not self.dynamic_ok:
            self._add(node, "dynamic-exec", f"gọi '{f.id}()' không được phép")
        if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name):
            if (f.value.id, f.attr) in BANNED_ATTR_CALLS and not reported and f.value.id not in self.allowed:
                self._add(node, "dangerous-call", f"gọi '{f.value.id}.{f.attr}()' không được phép")
            if f.attr in ("getattr", "setattr") and f.value.id == "builtins":
                self._add(node, "dynamic-exec", "builtins.getattr/setattr")
        if (
            isinstance(f, ast.Name)
            and f.id == "getattr"
            and len(node.args) >= 2
            and not isinstance(node.args[1], ast.Constant)
        ):
            self._add(node, "dynamic-getattr", "getattr với tên thuộc tính động")
        for kw in node.keywords:
            is_const = isinstance(kw.value, ast.Constant)
            if kw.arg in ("verify", "check_hostname") and is_const and kw.value.value is False:
                self._add(node, "tls-disabled", f"{kw.arg}=False")
            if kw.arg == "shell" and isinstance(kw.value, ast.Constant) and kw.value.value is True:
                self._add(node, "shell-true", "shell=True")
        self.generic_visit(node)

    def visit_Constant(self, node: ast.Constant) -> None:
        if isinstance(node.value, (str, bytes)):
            s = node.value if isinstance(node.value, str) else node.value.decode("latin-1")
            if LONG_BLOB.search(s):
                self._add(node, "obfuscation", "chuỗi base64/hex rất dài (nghi giấu mã)")
            if self.path.startswith("src/"):
                for host in URL_RE.findall(s):
                    if host.lower() not in ALLOWED_URL_HOSTS:
                        self._add(node, "unknown-url", f"URL tới host không nằm trong danh sách: {host}")
        self.generic_visit(node)


def audit_file(path: Path, root: Path) -> list[Finding]:
    rel = path.relative_to(root).as_posix() if path.is_relative_to(root) else str(path)
    text = path.read_text("utf-8")
    findings: list[Finding] = []
    for n, line in enumerate(text.splitlines(), 1):
        if BIDI.search(line):
            findings.append(Finding(rel, n, "trojan-source", "ký tự điều khiển hướng Unicode"))
        if len(line) > MAX_LINE:
            findings.append(Finding(rel, n, "long-line", f"dòng dài {len(line)} ký tự (nghi làm rối mã)"))
    try:
        tree = ast.parse(text, filename=rel)
    except SyntaxError as e:
        return [*findings, Finding(rel, e.lineno or 0, "syntax", str(e))]
    v = Visitor(rel)
    v.visit(tree)
    return findings + v.findings


def main(argv: list[str]) -> int:
    root = Path(__file__).resolve().parent.parent
    targets = [Path(a) for a in argv] or [root / "src"]
    findings: list[Finding] = []
    count = 0
    for t in targets:
        files = [t] if t.is_file() else sorted(t.rglob("*.py"))
        for f in files:
            count += 1
            findings.extend(audit_file(f.resolve(), root))
    for f in findings:
        print(f)
    print(f"security_audit: {count} file, {len(findings)} vấn đề", file=sys.stderr)
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
