"""Cache bản dịch theo đoạn (SQLite). Dịch lại hay chạy tiếp sau khi dừng đều không tốn token.

File cache chứa nội dung tài liệu => tạo với quyền 0600 trong thư mục 0700 của người dùng, và khi có khoá
(lưu trong kho mật khẩu hệ điều hành) thì giá trị được mã hoá AES-256-GCM. Khoá hàng là SHA-256, không chứa
văn bản gốc.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any


def _secure_mkdir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    if os.name == "posix":
        path.chmod(0o700)


ENC_PREFIX = "v1:"


class _Cipher:
    def __init__(self, key: bytes) -> None:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # noqa: PLC0415

        self._aead = AESGCM(key)

    def encrypt(self, row_key: str, value: str) -> str:
        nonce = os.urandom(12)
        ct = self._aead.encrypt(nonce, value.encode("utf-8"), row_key.encode())
        return ENC_PREFIX + base64.b64encode(nonce + ct).decode("ascii")

    def decrypt(self, row_key: str, stored: str) -> str | None:
        try:
            raw = base64.b64decode(stored[len(ENC_PREFIX) :], validate=True)
            return self._aead.decrypt(raw[:12], raw[12:], row_key.encode()).decode("utf-8")
        except Exception:
            return None  # sai khoá / dữ liệu hỏng: coi như chưa có trong cache


DEFAULT_MAX_AGE_DAYS = 180  # 2.1.1: cache chứa nội dung tài liệu => có thời hạn lưu, không giữ vô thời hạn (L1-011)


class TranslationCache:
    def __init__(self, path: Path, key: bytes | None = None, max_age_days: int | None = DEFAULT_MAX_AGE_DAYS) -> None:
        self.path = path
        self._cipher = _Cipher(key) if key else None
        _secure_mkdir(path.parent)
        if os.name == "posix" and not path.exists():
            fd = os.open(path, os.O_CREAT | os.O_WRONLY, 0o600)
            os.close(fd)
        # dùng được từ nhiều luồng (GUI tạo, luồng nền dùng); mọi thao tác tuần tự qua khoá
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._lock = threading.RLock()
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS translations (key TEXT PRIMARY KEY, value TEXT NOT NULL, created INTEGER)"
        )
        if max_age_days:
            # `created` NULL (bản ghi chèn từ ngoài / lỗi) coi như đã hết hạn — trước 2.1.2 chúng không bao giờ bị
            # xoá (R1-010)
            self._db.execute(
                "DELETE FROM translations WHERE created IS NULL OR created < ?",
                (int(time.time()) - max_age_days * 86400,),
            )
        if self._cipher is not None:  # đã bật mã hoá: bản ghi chữ thường cũ không còn dùng => xoá khỏi đĩa
            self._db.execute("DELETE FROM translations WHERE value NOT LIKE ?", (ENC_PREFIX + "%",))
        self._db.commit()

    @staticmethod
    def make_key(provider: str, model: str, ctx_fp: str, text: str) -> str:
        return hashlib.sha256("\x1f".join([provider, model, ctx_fp, text]).encode()).hexdigest()

    def get_many(self, keys: list[str]) -> dict[str, str]:
        with self._lock:
            out: dict[str, str] = {}
            for i in range(0, len(keys), 500):
                chunk = keys[i : i + 500]
                q = "SELECT key, value FROM translations WHERE key IN (%s)" % ",".join("?" * len(chunk))  # noqa: S608, UP031 - chỉ chèn dấu ?, giá trị đi qua tham số
                for k, v in self._db.execute(q, chunk).fetchall():
                    val = self._decode(k, v)
                    if val is not None:
                        out[k] = val
            return out

    @property
    def encrypted(self) -> bool:
        return self._cipher is not None

    def _decode(self, k: str, v: str) -> str | None:
        if v.startswith(ENC_PREFIX):
            return self._cipher.decrypt(k, v) if self._cipher else None
        return v if self._cipher is None else None  # đã bật mã hoá: bỏ qua bản ghi chữ thường cũ

    def put_many(self, items: dict[str, str]) -> None:
        with self._lock:
            now = int(time.time())
            self._db.executemany(
                "INSERT OR REPLACE INTO translations (key, value, created) VALUES (?, ?, ?)",
                [(k, self._cipher.encrypt(k, v) if self._cipher else v, now) for k, v in items.items()],
            )
            self._db.commit()

    def get_json(self, key: str) -> Any:
        hit = self.get_many([key]).get(key)
        if hit is None:
            return None
        try:
            return json.loads(hit)
        except json.JSONDecodeError:
            return None

    def put_json(self, key: str, value: Any) -> None:
        self.put_many({key: json.dumps(value, ensure_ascii=False)})

    def clear(self) -> int:
        with self._lock:
            n = int(self._db.execute("SELECT COUNT(*) FROM translations").fetchone()[0])
            self._db.execute("DELETE FROM translations")
            self._db.commit()
            self._db.execute("VACUUM")
            return n

    def count(self) -> int:
        with self._lock:
            return int(self._db.execute("SELECT COUNT(*) FROM translations").fetchone()[0])

    def close(self) -> None:
        with self._lock:
            self._db.close()
