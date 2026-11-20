"""Bảng thuật ngữ (glossary): nạp CSV an toàn và khớp thuật ngữ trong từng đoạn."""

from __future__ import annotations

import csv
import hashlib
import io
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

Policy = Literal["translate", "keep"]
MAX_GLOSSARY_BYTES = 10 * 1024 * 1024
MAX_ENTRIES = 100_000


class GlossaryError(Exception):
    pass


@dataclass(frozen=True)
class GlossaryEntry:
    en: str
    vi: str
    note: str = ""
    policy: Policy = "translate"


def _clean(s: str | None, limit: int) -> str:
    s = unicodedata.normalize("NFC", (s or "").strip())
    s = "".join(ch for ch in s if ch.isprintable())
    return s[:limit]


def load_csv(path: Path) -> list[GlossaryEntry]:
    """Cột: english,vietnamese[,ghi_chu][,chinh_sach]. Chấp nhận cả en,vi."""
    if path.stat().st_size > MAX_GLOSSARY_BYTES:
        raise GlossaryError("File glossary quá lớn (> 10 MB)")
    text = path.read_text("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    cols = {c.strip().lower(): c for c in (reader.fieldnames or [])}
    en_col = cols.get("english") or cols.get("en")
    vi_col = cols.get("vietnamese") or cols.get("vi")
    if not en_col or not vi_col:
        raise GlossaryError("Glossary cần cột 'english' và 'vietnamese'")
    note_col = cols.get("ghi_chu") or cols.get("note")
    pol_col = cols.get("chinh_sach") or cols.get("policy")
    entries: list[GlossaryEntry] = []
    for row in reader:
        en = _clean(row.get(en_col), 200)
        vi = _clean(row.get(vi_col), 300)
        if not en or not vi:
            continue
        pol_raw = _clean(row.get(pol_col) if pol_col else "", 20).lower()
        policy: Policy = "keep" if pol_raw in ("keep", "giu", "giữ", "giữ nguyên") else "translate"
        entries.append(GlossaryEntry(en, vi, _clean(row.get(note_col) if note_col else "", 300), policy))
        if len(entries) > MAX_ENTRIES:
            raise GlossaryError(f"Glossary có quá {MAX_ENTRIES} mục")
    return entries


class Glossary:
    def __init__(self, entries: list[GlossaryEntry]) -> None:
        # Mục sau ghi đè mục trước cùng khoá (tầng người dùng nạp sau cùng sẽ thắng).
        by_key: dict[str, GlossaryEntry] = {}
        for e in entries:
            by_key[e.en.lower()] = e
        self.entries = list(by_key.values())
        self._by_key = by_key
        terms = sorted(by_key.keys(), key=len, reverse=True)  # cụm dài khớp trước
        self._re: re.Pattern[str] | None = None
        if terms:
            alt = "|".join(re.escape(t) for t in terms)
            self._re = re.compile(rf"(?<![\w-])({alt})(?:s|es)?(?![\w-])", re.IGNORECASE)

    def __len__(self) -> int:
        return len(self.entries)

    def find(self, text: str) -> list[tuple[str, GlossaryEntry]]:
        """Các lần xuất hiện: (dạng nguyên văn trong text, mục glossary)."""
        if self._re is None:
            return []
        out: list[tuple[str, GlossaryEntry]] = []
        for m in self._re.finditer(text):
            e = self._by_key.get(m.group(1).lower())
            if e is not None:
                out.append((m.group(0), e))
        return out

    def match(self, text: str) -> list[GlossaryEntry]:
        if self._re is None:
            return []
        seen: dict[str, GlossaryEntry] = {}
        for m in self._re.finditer(text):
            key = m.group(1).lower()
            if key in self._by_key:
                seen[key] = self._by_key[key]
        return list(seen.values())

    def fingerprint(self) -> str:
        h = hashlib.sha256()
        for e in sorted(self.entries, key=lambda x: x.en.lower()):
            h.update(f"{e.en}\x1f{e.vi}\x1f{e.policy}\x1e".encode())
        return h.hexdigest()[:16]


# --------------------------------------------------------------------------- kho thuật ngữ 3 tầng

DOMAIN_FILES: dict[str, list[str]] = {
    "cntt": ["cntt_ai"],
    "ai": ["cntt_ai"],
    "toan_thong_ke": ["toan_thong_ke"],
    "vat_ly": ["vat_ly_hoa"],
    "hoa": ["vat_ly_hoa"],
    "sinh_y": ["sinh_y"],
    "kinh_te": ["kinh_te"],
    "phap_ly": ["phap_ly"],
    "ky_thuat": ["cntt_ai"],
}


def builtin_entries(domains: list[str]) -> list[GlossaryEntry]:
    """Tầng 3: glossary chuyên ngành đóng gói sẵn (đã kiểm checksum), chọn theo lĩnh vực tài liệu."""
    from vietpdf.security.integrity import asset_path  # noqa: PLC0415

    names: list[str] = []
    for d in domains or ["ai"]:
        for n in DOMAIN_FILES.get(d, []):
            if n not in names:
                names.append(n)
    entries: list[GlossaryEntry] = []
    for n in names:
        entries += load_csv(asset_path(f"glossary/{n}.csv"))
    return entries


def user_glossary_files() -> list[Path]:
    from vietpdf.config import config_dir  # noqa: PLC0415

    d = config_dir() / "glossary"
    return sorted(d.glob("*.csv")) if d.is_dir() else []


def build_glossary(
    domains: list[str], doc_terms: list[GlossaryEntry], user_files: list[Path] | None = None, use_builtin: bool = True
) -> Glossary:
    """Tầng 3 (chuyên ngành) < tầng 2 (của tài liệu) < tầng 1 (của người dùng): tầng trên thắng."""
    entries = builtin_entries(domains) if use_builtin else []
    entries += doc_terms
    for f in [*user_glossary_files(), *(user_files or [])]:
        entries += load_csv(f)
    return Glossary(entries)


def append_user_term(en: str, vi: str, note: str = "") -> Path:
    """Học từ chỉnh sửa: lưu cặp thuật ngữ vào glossary của người dùng (tầng 1)."""
    from vietpdf.config import config_dir  # noqa: PLC0415

    en, vi, note = _clean(en, 200), _clean(vi, 300), _clean(note, 300)
    if not en or not vi:
        raise GlossaryError("Thiếu thuật ngữ hoặc bản dịch")
    d = config_dir() / "glossary"
    d.mkdir(parents=True, exist_ok=True)
    f = d / "cua_toi.csv"
    new = not f.exists()
    with f.open("a", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        if new:
            w.writerow(["english", "vietnamese", "ghi_chu", "chinh_sach"])
        w.writerow([en, vi, note, "translate"])
    return f
