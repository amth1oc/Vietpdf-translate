#!/usr/bin/env python3
"""Sinh lại src/vietpdf/assets/SHA256SUMS. Chỉ người duy trì chạy, và thay đổi phải được review (CODEOWNERS):
một thay đổi checksum không rõ lý do là dấu hiệu tài nguyên bị tráo."""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from vietpdf.security.integrity import SUMS_FILE, sha256_file  # noqa: E402

ASSETS = ROOT / "src" / "vietpdf" / "assets"


def main() -> int:
    lines = ["# SHA-256 của mọi tài nguyên đóng gói. Sinh lại: python scripts/update_asset_sums.py (cần review)"]
    for f in sorted(p for p in ASSETS.rglob("*") if p.is_file() and p.relative_to(ASSETS).as_posix() != SUMS_FILE):
        lines.append(f"{sha256_file(f)}  {f.relative_to(ASSETS).as_posix()}")
    (ASSETS / SUMS_FILE).write_text("\n".join(lines) + "\n", "utf-8")
    digest = sha256_file(ASSETS / SUMS_FILE)
    integ = ROOT / "src" / "vietpdf" / "security" / "integrity.py"
    src = integ.read_text("utf-8")
    src = re.sub(r'^SUMS_SHA256 = "[0-9a-f]{64}"', f'SUMS_SHA256 = "{digest}"', src, count=1, flags=re.M)
    integ.write_text(src, "utf-8")
    print(f"Đã ghi {len(lines) - 1} mục; SUMS_SHA256 = {digest} (integrity.py) — cần review.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
