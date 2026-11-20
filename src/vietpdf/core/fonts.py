"""Ánh xạ font gốc -> font Việt (Noto, SIL OFL) và kiểm tra đủ ký tự tiếng Việt."""

from __future__ import annotations

import functools
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from vietpdf.core.ir import Family
from vietpdf.security.integrity import ASSETS_DIR, asset_path

_MONO = (
    "courier",
    "consolas",
    "mono",
    "menlo",
    "cmtt",
    "inconsolata",
    "lucida console",
    "sourcecode",
    "source code",
    "typewriter",
    "fixed",
)
_SANS = (
    "arial",
    "helvetica",
    "calibri",
    "verdana",
    "segoe",
    "tahoma",
    "roboto",
    "open sans",
    "opensans",
    "lato",
    "sans",
    "cmss",
    "nimbussan",
    "frutiger",
    "futura",
    "gill",
    "trebuchet",
    "myriad",
    "aptos",
    "inter",
    "montserrat",
    "candara",
    "corbel",
    "franklin",
)
_SERIF = (
    "times",
    "georgia",
    "cambria",
    "garamond",
    "minion",
    "palatino",
    "book antiqua",
    "serif",
    "cmr",
    "nimbusrom",
    "libertine",
    "charter",
    "baskerville",
    "century",
    "utopia",
    "stix",
    "constantia",
    "lmroman",
    "termes",
    "pagella",
    "bookman",
    "caslon",
)

FLAG_ITALIC = 2
FLAG_SERIF = 4
FLAG_MONO = 8
FLAG_BOLD = 16

FONT_FILES: dict[tuple[Family, bool, bool], str] = {
    ("serif", False, False): "fonts/NotoSerif-Regular.ttf",
    ("serif", True, False): "fonts/NotoSerif-Bold.ttf",
    ("serif", False, True): "fonts/NotoSerif-Italic.ttf",
    ("serif", True, True): "fonts/NotoSerif-BoldItalic.ttf",
    ("sans", False, False): "fonts/NotoSans-Regular.ttf",
    ("sans", True, False): "fonts/NotoSans-Bold.ttf",
    ("sans", False, True): "fonts/NotoSans-Italic.ttf",
    ("sans", True, True): "fonts/NotoSans-BoldItalic.ttf",
    ("mono", False, False): "fonts/NotoSansMono-Regular.ttf",
    ("mono", True, False): "fonts/NotoSansMono-Bold.ttf",
    ("mono", False, True): "fonts/NotoSansMono-Regular.ttf",  # Noto Sans Mono không có nghiêng
    ("mono", True, True): "fonts/NotoSansMono-Bold.ttf",
}


def _strip_subset_prefix(name: str) -> str:
    # Font nhúng dạng subset có tiền tố "ABCDEF+"
    if len(name) > 7 and name[6] == "+" and name[:6].isupper():
        return name[7:]
    return name


def classify_family(font_name: str, flags: int = 0) -> Family:
    n = _strip_subset_prefix(font_name).lower().replace("-", " ").replace("_", " ")
    if any(k in n for k in _MONO):
        return "mono"
    if any(k in n for k in _SANS):  # trước serif: "Microsoft Sans Serif" là sans
        return "sans"
    if any(k in n for k in _SERIF):
        return "serif"
    if flags & FLAG_MONO:
        return "mono"
    if flags & FLAG_SERIF:
        return "serif"
    return "serif"


def is_bold(font_name: str, flags: int) -> bool:
    n = font_name.lower()
    return bool(flags & FLAG_BOLD) or any(k in n for k in ("bold", "black", "heavy", "semibold", ",b"))


def is_italic(font_name: str, flags: int) -> bool:
    n = font_name.lower()
    return bool(flags & FLAG_ITALIC) or "italic" in n or "oblique" in n


def vietnamese_chars() -> list[str]:
    """134 ký tự tiếng Việt có dấu (67 thường + 67 hoa)."""
    bases = "aăâeêioôơuưy"
    tones = ["\u0301", "\u0300", "\u0309", "\u0303", "\u0323"]  # sắc, huyền, hỏi, ngã, nặng
    lower: list[str] = []
    for b in bases:
        if b in "ăâêôơư":
            lower.append(b)
        lower.extend(unicodedata.normalize("NFC", b + t) for t in tones)
    lower.append("đ")
    return lower + [c.upper() for c in lower]


@dataclass
class LoadedFont:
    key: tuple[Family, bool, bool]
    path: Path
    font: Any  # pymupdf.Font

    def text_length(self, text: str, size: float) -> float:
        return float(self.font.text_length(text, fontsize=size))

    @property
    def ascender(self) -> float:
        return float(self.font.ascender)

    @property
    def descender(self) -> float:
        return float(self.font.descender)


class FontBook:
    """Nạp font đóng gói (đã kiểm checksum) và cache theo kiểu chữ."""

    def __init__(self, assets_dir: Path = ASSETS_DIR) -> None:
        self.assets_dir = assets_dir
        self._cache: dict[str, LoadedFont] = {}

    def get(self, family: Family, bold: bool, italic: bool) -> LoadedFont:
        import pymupdf  # noqa: PLC0415

        rel = FONT_FILES[(family, bold, italic)]
        if rel not in self._cache:
            path = asset_path(rel, self.assets_dir)
            self._cache[rel] = LoadedFont((family, bold, italic), path, pymupdf.Font(fontfile=str(path)))
        return self._cache[rel]

    def missing_vietnamese(self, family: Family, bold: bool, italic: bool) -> list[str]:
        f = self.get(family, bold, italic).font
        return [c for c in vietnamese_chars() if not f.has_glyph(ord(c))]


@functools.cache
def default_fontbook() -> FontBook:
    return FontBook()
