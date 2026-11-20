"""Thư viện tài liệu + sổ từ vựng + ôn tập ngắt quãng (SRS) + lịch sử hỏi đáp — một file SQLite cục bộ.

Bảo mật:
- Nội dung (tiêu đề, đường dẫn, ghi chú, từ vựng, câu ví dụ, câu hỏi/trả lời) được mã hoá AES-256-GCM theo dòng
  (security/crypto.py). Cột không mã hoá chỉ gồm id ngẫu nhiên, mốc thời gian và lịch ôn (để truy vấn "đến hạn").
- File SQLite và thư mục thư viện có quyền 0600/0700. Bản sao tài liệu nằm trong thư mục riêng theo id, tên file
  cố định (không lấy tên từ dữ liệu bên ngoài => không thể ghi ra ngoài thư mục thư viện).
- Mọi truy vấn dùng tham số (không ghép chuỗi SQL).
"""

from __future__ import annotations

import contextlib
import csv
import io
import json
import os
import secrets
import shutil
import sqlite3
import threading
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from vietpdf.security.crypto import FieldCipher
from vietpdf.security.fsutil import replace_from_temp, temp_near

DAY = 86400.0
# Tên file cố định trong thư mục của một tài liệu
FILE_NAMES = {
    "source": "goc.pdf",
    "output": "ban-dich.pdf",
    "dual": "song-ngu.pdf",
    "ir": "ir.json",
    "report": "bao-cao.md",
    "study": "ban-hoc.pdf",
    "skim": "doc-luot.pdf",
}
GRADES = {"again": 0, "hard": 3, "good": 4, "easy": 5}


def _norm(s: str) -> str:
    return unicodedata.normalize("NFC", s).strip().casefold()


def _secure_file(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if os.name == "posix":
        path.parent.chmod(0o700)
        if not path.exists():
            os.close(os.open(path, os.O_CREAT | os.O_WRONLY, 0o600))


@dataclass
class Schedule:
    due: float
    interval: float  # ngày
    ease: float
    reps: int
    lapses: int


def sm2(s: Schedule, grade: int, now: float) -> Schedule:
    """SM-2 (SuperMemo 2) rút gọn. grade: 0 quên · 3 khó · 4 nhớ · 5 dễ."""
    if grade < 3:
        return Schedule(due=now + 600, interval=0.0, ease=max(1.3, s.ease - 0.2), reps=0, lapses=s.lapses + 1)
    reps = s.reps + 1
    if reps == 1:
        interval = 1.0
    elif reps == 2:
        interval = 6.0 if grade >= 4 else 3.0
    else:
        interval = max(1.0, s.interval) * (1.2 if grade == 3 else s.ease)
    if grade == 5:
        interval *= 1.3
    ease = max(1.3, s.ease + 0.1 - (5 - grade) * (0.08 + (5 - grade) * 0.02))
    interval = min(interval, 3650.0)
    return Schedule(
        due=now + interval * DAY, interval=round(interval, 2), ease=round(ease, 3), reps=reps, lapses=s.lapses
    )


class LearningStore:
    def __init__(self, db_path: Path, library_dir: Path, key: bytes | None = None) -> None:
        _secure_file(db_path)
        self.library_dir = library_dir
        self.cipher = FieldCipher(key)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS documents (id TEXT PRIMARY KEY, added REAL, opened REAL, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS assist (id TEXT PRIMARY KEY, doc_id TEXT, created REAL, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS vocab (
                id TEXT PRIMARY KEY, doc_id TEXT, created REAL, due REAL, interval REAL, ease REAL,
                reps INTEGER, lapses INTEGER, data TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS vocab_due ON vocab(due);
            """
        )
        self._db.commit()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    @property
    def encrypted(self) -> bool:
        return self.cipher.enabled

    def plaintext_rows(self) -> int:
        """Số bản ghi chữ thường (tạo khi chưa có kho mật khẩu). Khi đã bật mã hoá, chúng bị bỏ qua cho tới khi
        người dùng chạy `vietpdf library migrate`."""
        with self._lock:
            return sum(
                int(self._db.execute(f"SELECT COUNT(*) FROM {t} WHERE data LIKE 'p:%'").fetchone()[0])  # noqa: S608 - tên bảng cố định
                for t in ("documents", "assist", "vocab")
            )

    def plaintext_preview(self, limit: int = 200) -> list[tuple[str, str, str]]:
        """(bảng, id, 60 ký tự đầu) của các bản ghi chữ thường — để người dùng xem trước khi `migrate` (R1-010): bản
        ghi do kẻ khác chèn vào DB sẽ được mã hoá lại thành bản ghi "hợp lệ" nếu không ai xem."""
        out: list[tuple[str, str, str]] = []
        with self._lock:
            for table in ("documents", "assist", "vocab"):
                sql = f"SELECT id, data FROM {table} WHERE data LIKE 'p:%' LIMIT ?"  # noqa: S608 - tên bảng cố định
                rows = self._db.execute(sql, (limit,)).fetchall()
                for rid, blob in rows:
                    obj = self.cipher.open_legacy_plain(blob)
                    text = json.dumps(obj, ensure_ascii=False)[:60] if obj is not None else "(không đọc được)"
                    out.append((table, str(rid)[:40], text))
        return out[:limit]

    def migrate_plaintext(self) -> int:
        """Mã hoá lại các bản ghi chữ thường cũ. Chỉ chạy khi người dùng yêu cầu (tránh nhận bản ghi bị chèn)."""
        if not self.cipher.enabled:
            raise ValueError("Chưa có kho mật khẩu an toàn: không thể mã hoá dữ liệu")
        n = 0
        with self._lock:
            for table in ("documents", "assist", "vocab"):
                rows = self._db.execute(f"SELECT id, data FROM {table} WHERE data LIKE 'p:%'").fetchall()  # noqa: S608
                for rid, blob in rows:
                    obj = self.cipher.open_legacy_plain(blob)
                    if obj is None:
                        continue
                    sealed = self.cipher.seal(f"{table}:{rid}", obj)
                    self._db.execute(f"UPDATE {table} SET data = ? WHERE id = ?", (sealed, rid))  # noqa: S608
                    n += 1
            self._db.commit()
        return n

    # ------------------------------------------------------------------ tài liệu

    def _doc_dir(self, doc_id: str) -> Path:
        if not doc_id.isalnum():
            raise ValueError("id tài liệu không hợp lệ")
        d = (self.library_dir / doc_id[:24]).resolve()
        if not d.is_relative_to(self.library_dir.resolve()):
            raise ValueError("đường dẫn thư viện không hợp lệ")
        return d

    def add_document(self, info: dict[str, Any], files: dict[str, Path | None], copy: bool = True) -> str:
        """info: title, sha256, source_name, provider, model, pages... files: source/output/dual/ir/report/study.
        Cùng một file gốc (cùng SHA-256) thì cập nhật bản ghi cũ thay vì thêm mới."""
        sha = str(info.get("sha256", ""))
        doc_id = sha[:24] if sha.isalnum() and len(sha) >= 24 else secrets.token_hex(12)
        old = self.get_document(doc_id) or {}
        paths: dict[str, str] = dict(old.get("paths", {}))
        if copy:
            folder = self._doc_dir(doc_id)
            folder.mkdir(parents=True, exist_ok=True)
            if os.name == "posix":
                self.library_dir.chmod(0o700)
                folder.chmod(0o700)
            for kind, src in files.items():
                if src is None or kind not in FILE_NAMES or not Path(src).is_file():
                    continue
                dst = folder / FILE_NAMES[kind]
                tmp = temp_near(dst, ".part")  # chép vào file tạm rồi thay thế: không đi theo symlink cài sẵn
                shutil.copyfile(src, tmp)
                replace_from_temp(tmp, dst)
                paths[kind] = str(dst)
        else:
            paths.update({k: str(v) for k, v in files.items() if v is not None and k in FILE_NAMES})
        data = {**old, **info, "paths": paths, "copied": copy or old.get("copied", False)}
        data.setdefault("notes", "")
        data.setdefault("tags", [])
        data.setdefault("last_page", 0)
        now = time.time()
        with self._lock:
            self._db.execute(
                "INSERT INTO documents (id, added, opened, data) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET data = excluded.data, opened = excluded.opened",
                (doc_id, now, now, self.cipher.seal(f"documents:{doc_id}", data)),
            )
            self._db.commit()
        return doc_id

    def get_document(self, doc_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute("SELECT id, added, opened, data FROM documents WHERE id = ?", (doc_id,)).fetchone()
        return self._doc_row(row) if row else None

    def _doc_row(self, row: tuple[Any, ...]) -> dict[str, Any] | None:
        data = self.cipher.open(f"documents:{row[0]}", row[3])
        if not isinstance(data, dict):
            return None
        return {**data, "id": row[0], "added": row[1], "opened": row[2]}

    def list_documents(self, query: str = "") -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute("SELECT id, added, opened, data FROM documents ORDER BY opened DESC").fetchall()
        docs = [d for d in (self._doc_row(r) for r in rows) if d]
        q = _norm(query)
        if q:
            docs = [
                d
                for d in docs
                if q
                in _norm(
                    f"{d.get('title', '')} {d.get('source_name', '')} "
                    f"{' '.join(d.get('tags', []))} {d.get('notes', '')}"
                )
            ]
        counts = self.vocab_counts()
        for d in docs:
            d["vocab"] = counts.get(d["id"], 0)
        return docs

    def update_document(self, doc_id: str, **changes: Any) -> None:
        allowed = {"notes", "tags", "last_page", "title"}
        doc = self.get_document(doc_id)
        if doc is None:
            raise KeyError(doc_id)
        data = {k: v for k, v in doc.items() if k not in ("id", "added", "opened", "vocab")}
        data.update({k: v for k, v in changes.items() if k in allowed})
        with self._lock:
            self._db.execute(
                "UPDATE documents SET data = ?, opened = ? WHERE id = ?",
                (self.cipher.seal(f"documents:{doc_id}", data), time.time(), doc_id),
            )
            self._db.commit()

    def touch(self, doc_id: str) -> None:
        with self._lock:
            self._db.execute("UPDATE documents SET opened = ? WHERE id = ?", (time.time(), doc_id))
            self._db.commit()

    def remove_document(self, doc_id: str, delete_files: bool = True, delete_vocab: bool = False) -> None:
        with self._lock:
            self._db.execute("DELETE FROM documents WHERE id = ?", (doc_id,))
            self._db.execute("DELETE FROM assist WHERE doc_id = ?", (doc_id,))
            if delete_vocab:
                self._db.execute("DELETE FROM vocab WHERE doc_id = ?", (doc_id,))
            self._db.commit()
        if delete_files:
            folder = self._doc_dir(doc_id)
            if folder.is_dir():
                shutil.rmtree(folder)

    # ------------------------------------------------------------------ lịch sử hỏi đáp / trợ lý

    def add_assist(self, doc_id: str, kind: str, question: str, result: Any) -> str:
        aid = secrets.token_hex(8)
        with self._lock:
            self._db.execute(
                "INSERT INTO assist VALUES (?, ?, ?, ?)",
                (
                    aid,
                    doc_id,
                    time.time(),
                    self.cipher.seal(
                        f"assist:{aid}", {"kind": kind[:40], "question": question[:2000], "result": result}
                    ),
                ),
            )
            self._db.commit()
        return aid

    def list_assist(self, doc_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT id, created, data FROM assist WHERE doc_id = ? ORDER BY created DESC", (doc_id,)
            ).fetchall()
        out = []
        for aid, created, blob in rows:
            d = self.cipher.open(f"assist:{aid}", blob)
            if isinstance(d, dict):
                out.append({**d, "id": aid, "created": created})
        return out

    # ------------------------------------------------------------------ từ vựng

    def _word_row(self, row: tuple[Any, ...]) -> dict[str, Any] | None:
        wid, doc_id, created, due, interval, ease, reps, lapses, blob = row
        d = self.cipher.open(f"vocab:{wid}", blob)
        if not isinstance(d, dict):
            return None
        return {
            **d,
            "id": wid,
            "doc_id": doc_id,
            "created": created,
            "due": due,
            "interval": interval,
            "ease": ease,
            "reps": reps,
            "lapses": lapses,
        }

    def list_words(self, doc_id: str | None = None, query: str = "") -> list[dict[str, Any]]:
        sql = "SELECT id, doc_id, created, due, interval, ease, reps, lapses, data FROM vocab"
        with self._lock:
            rows = (
                self._db.execute(sql + " WHERE doc_id = ? ORDER BY created", (doc_id,))
                if doc_id
                else self._db.execute(sql + " ORDER BY created")
            ).fetchall()
        words = [w for w in (self._word_row(r) for r in rows) if w]
        q = _norm(query)
        return [w for w in words if q in _norm(f"{w.get('term', '')} {w.get('vi', '')}")] if q else words

    def add_word(
        self,
        term: str,
        vi: str = "",
        *,
        doc_id: str = "",
        definition: str = "",
        example: str = "",
        example_vi: str = "",
        page: int | None = None,
        pos: str = "",
        source: str = "",
        note: str = "",
    ) -> str:
        """Thêm từ vào sổ. Từ đã có (cùng tài liệu, không phân biệt hoa/thường) thì cập nhật, không nhân đôi."""
        term = unicodedata.normalize("NFC", " ".join(term.split()))[:200]
        if not term:
            raise ValueError("Từ trống")
        fields = {
            "term": term,
            "vi": vi[:400],
            "definition": definition[:1500],
            "example": example[:800],
            "example_vi": example_vi[:1200],
            "page": page,
            "pos": pos[:40],
            "source": source[:80],
            "note": note[:800],
        }
        existing = next(
            (w for w in self.list_words(doc_id or None) if _norm(w["term"]) == _norm(term) and w["doc_id"] == doc_id),
            None,
        )
        if existing:
            merged = {k: (v if v not in ("", None) else existing.get(k)) for k, v in fields.items() if k != "term"}
            self.update_word(existing["id"], **merged)
            return str(existing["id"])
        wid = secrets.token_hex(8)
        now = time.time()
        with self._lock:
            self._db.execute(
                "INSERT INTO vocab VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (wid, doc_id, now, now, 0.0, 2.5, 0, 0, self.cipher.seal(f"vocab:{wid}", fields)),
            )
            self._db.commit()
        return wid

    def get_word(self, wid: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute(
                "SELECT id, doc_id, created, due, interval, ease, reps, lapses, data FROM vocab WHERE id = ?", (wid,)
            ).fetchone()
        return self._word_row(row) if row else None

    def update_word(self, wid: str, **changes: Any) -> None:
        w = self.get_word(wid)
        if w is None:
            raise KeyError(wid)
        keys = ("term", "vi", "definition", "example", "example_vi", "page", "pos", "source", "note")
        data = {k: w.get(k) for k in keys}
        data.update({k: v for k, v in changes.items() if k in keys})
        with self._lock:
            self._db.execute("UPDATE vocab SET data = ? WHERE id = ?", (self.cipher.seal(f"vocab:{wid}", data), wid))
            self._db.commit()

    def delete_word(self, wid: str) -> None:
        with self._lock:
            self._db.execute("DELETE FROM vocab WHERE id = ?", (wid,))
            self._db.commit()

    def due_words(self, now: float | None = None, doc_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        now = time.time() if now is None else now
        sql = "SELECT id, doc_id, created, due, interval, ease, reps, lapses, data FROM vocab WHERE due <= ?"
        args: tuple[Any, ...] = (now,)
        if doc_id:
            sql += " AND doc_id = ?"
            args = (now, doc_id)
        with self._lock:
            rows = self._db.execute(sql + " ORDER BY due LIMIT ?", (*args, max(1, min(limit, 1000)))).fetchall()
        return [w for w in (self._word_row(r) for r in rows) if w]

    def review(self, wid: str, grade: int | str, now: float | None = None) -> dict[str, Any]:
        g = GRADES[grade] if isinstance(grade, str) else int(grade)
        if not 0 <= g <= 5:
            raise ValueError("Điểm phải từ 0 đến 5")
        w = self.get_word(wid)
        if w is None:
            raise KeyError(wid)
        now = time.time() if now is None else now
        s = sm2(Schedule(w["due"], w["interval"], w["ease"], w["reps"], w["lapses"]), g, now)
        with self._lock:
            self._db.execute(
                "UPDATE vocab SET due = ?, interval = ?, ease = ?, reps = ?, lapses = ? WHERE id = ?",
                (s.due, s.interval, s.ease, s.reps, s.lapses, wid),
            )
            self._db.commit()
        return {"due": s.due, "interval_days": s.interval, "ease": s.ease, "reps": s.reps}

    def vocab_counts(self) -> dict[str, int]:
        with self._lock:
            return dict(self._db.execute("SELECT doc_id, COUNT(*) FROM vocab GROUP BY doc_id").fetchall())

    def stats(self, doc_id: str | None = None, now: float | None = None) -> dict[str, int]:
        now = time.time() if now is None else now
        words = self.list_words(doc_id)
        return {
            "total": len(words),
            "due": sum(1 for w in words if w["due"] <= now),
            "new": sum(1 for w in words if w["reps"] == 0 and w["lapses"] == 0),
            "learned": sum(1 for w in words if w["interval"] >= 21),
        }

    # ------------------------------------------------------------------ xuất / nhập

    def export(self, fmt: str, doc_id: str | None = None) -> str:
        from vietpdf.assistant.export import _csv_safe, anki_tsv  # noqa: PLC0415
        from vietpdf.textfmt import md_escape  # noqa: PLC0415

        words = self.list_words(doc_id)
        titles = {d["id"]: d.get("title", "") for d in self.list_documents()}
        if fmt == "anki":
            return anki_tsv(
                [{**w, "tag": (titles.get(w["doc_id"], "") or "chung")[:40].replace(" ", "_")} for w in words]
            )
        if fmt == "csv":
            buf = io.StringIO()
            wr = csv.writer(buf, lineterminator="\n")  # ghi bằng write_text: \n tự đổi theo hệ điều hành
            wr.writerow(["term", "vi", "definition", "example", "example_vi", "page", "document", "reps", "due"])
            for w in words:
                wr.writerow(
                    [_csv_safe(w.get(k, "")) for k in ("term", "vi", "definition", "example", "example_vi")]
                    + [
                        w.get("page") or "",
                        _csv_safe(titles.get(w["doc_id"], "")),
                        w["reps"],
                        time.strftime("%Y-%m-%d", time.localtime(w["due"])),
                    ]
                )
            return buf.getvalue()
        if fmt == "md":
            lines = ["# Sổ từ vựng", "", "| Từ | Nghĩa | Ví dụ | Tài liệu |", "|---|---|---|---|"]
            lines += [
                f"| {md_escape(w['term'])} | {md_escape(w.get('vi'))} | {md_escape(w.get('example'))} | "
                f"{md_escape(titles.get(w['doc_id'], ''))} |"
                for w in words
            ]
            return "\n".join(lines) + "\n"
        raise ValueError(f"Định dạng không hỗ trợ: {fmt}")

    def import_csv(self, path: Path, doc_id: str = "", max_rows: int = 20000) -> int:
        """Nhập từ CSV (cột term|english, vi|vietnamese, definition, example). Trả về số từ đã nhập."""
        if path.stat().st_size > 10 * 1024 * 1024:
            raise ValueError("File quá lớn")
        reader = csv.DictReader(io.StringIO(path.read_text("utf-8-sig")))
        n = 0
        for row in reader:
            low = {k.strip().lower(): (v or "") for k, v in row.items() if k}
            term = low.get("term") or low.get("english") or low.get("en") or ""
            term = term.lstrip("'")
            if not term.strip():
                continue
            self.add_word(
                term,
                (low.get("vi") or low.get("vietnamese") or "").lstrip("'"),
                doc_id=doc_id,
                definition=low.get("definition", "").lstrip("'"),
                example=low.get("example", "").lstrip("'"),
                source="nhập CSV",
            )
            n += 1
            if n >= max_rows:
                break
        return n


SIBLINGS = {
    "output": ".vi.pdf",
    "dual": ".dual.pdf",
    "ir": ".ir.json",
    "report": ".report.md",
    "study": ".study.pdf",
    "skim": ".skim.pdf",
}


def save_translation(
    store: LearningStore,
    src: Path,
    files: dict[str, Path | None] | None = None,
    copy: bool = True,
    extra: dict[str, Any] | None = None,
) -> str:
    """Lưu một tài liệu đã dịch vào thư viện. Thiếu file nào thì tìm file cùng tên cạnh bản gốc
    (paper.vi.pdf, paper.ir.json…). Thông tin (tiêu đề, SHA-256, số trang) đọc từ IR đã kiểm tra schema."""
    from vietpdf.core.ir import DocumentIR  # noqa: PLC0415

    found: dict[str, Path | None] = {"source": src}
    for kind, suffix in SIBLINGS.items():
        given = (files or {}).get(kind)
        sib = src.with_name(src.stem + suffix)
        found[kind] = Path(given) if given else (sib if sib.is_file() else None)
    ir_path = found.get("ir")
    if ir_path is None or not ir_path.is_file():
        raise FileNotFoundError(f"Chưa có bản dịch cho {src.name} (cần {src.stem}.ir.json).")
    ir = DocumentIR.model_validate_json(ir_path.read_text("utf-8"))
    info = {
        "title": ir.title or ir.source_name,
        "sha256": ir.source_sha256,
        "source_name": ir.source_name,
        "pages": ir.page_count,
        "domains": ir.domains,
        "original_path": str(src.resolve()),
        **(extra or {}),
    }
    return store.add_document(info, found, copy=copy)


def open_store() -> LearningStore:
    """Mở kho học tập mặc định trong thư mục dữ liệu người dùng (khoá mã hoá lấy từ kho mật khẩu hệ điều hành)."""
    from vietpdf.config import config_dir, data_dir  # noqa: PLC0415
    from vietpdf.security.secrets import get_or_create_cache_key  # noqa: PLC0415

    store = LearningStore(data_dir() / "learning.sqlite3", data_dir() / "library", key=get_or_create_cache_key())
    legacy = config_dir() / "vocab.sqlite3"  # sổ từ dạng cũ (2.0): chuyển một lần rồi đổi tên
    if legacy.is_file():
        with contextlib.suppress(sqlite3.Error):
            old = sqlite3.connect(legacy)
            for term, vi, definition, example, _doc, _added in old.execute("SELECT * FROM vocab").fetchall():
                store.add_word(term, vi or "", definition=definition or "", example=example or "", source="sổ từ cũ")
            old.close()
            legacy.rename(legacy.with_suffix(".migrated"))
    return store
