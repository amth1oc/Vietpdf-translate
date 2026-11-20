#!/usr/bin/env python3
"""Tải mô hình DocLayout-YOLO (ONNX) cho tính năng nhận diện bố cục — DÙNG LÚC ĐÓNG GÓI, không phải lúc chạy.

Quy trình an toàn (ghim SHA-256):
  1. Lần đầu (người duy trì): python scripts/fetch_models.py --out models/   -> in SHA-256, ghi file .sha256.pending
     Kiểm tra nguồn/giấy phép trên trang mô hình, rồi điền SHA-256 vào src/vietpdf/assets/models.lock.json.
  2. Những lần sau / CI: tải lại và SO KHỚP với SHA-256 đã ghim; sai thì xoá file và báo lỗi.
Ứng dụng từ chối nạp mô hình chưa ghim SHA-256 (xem core/layout_model.py).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hmac
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from vietpdf.security.http import NetworkPolicy, SafeHttpClient  # noqa: E402
from vietpdf.security.integrity import sha256_file  # noqa: E402

LOCK = ROOT / "src" / "vietpdf" / "assets" / "models.lock.json"
DEFAULT_URL = (
    "https://huggingface.co/wybxc/DocLayout-YOLO-DocStructBench-onnx/resolve/main/"
    "doclayout_yolo_docstructbench_imgsz1024.onnx"
)
# Hugging Face chuyển hướng tải file lớn sang CDN của họ; chỉ các host này được phép.
HF_CDN = ("cdn-lfs.huggingface.co", "cdn-lfs.hf.co", "cdn-lfs-us-1.hf.co", "cas-bridge.xethub.hf.co")


async def fetch(url: str, dest: Path) -> int:
    async with SafeHttpClient(NetworkPolicy.create(["huggingface.co"])) as http:
        return await http.download_to(url, dest, max_bytes=600 * 1024 * 1024, redirect_hosts=HF_CDN)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--name", default="doclayout-yolo-docstructbench")
    ap.add_argument("--url", default=DEFAULT_URL)
    ap.add_argument("--out", default="models")
    args = ap.parse_args()
    lock = json.loads(LOCK.read_text("utf-8"))
    entry = lock["models"][args.name]
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    dest = out_dir / entry["file"]
    # R1-011 (2.1.2): tải vào file TẠM rồi kiểm hash TRƯỚC khi thay file đích — bản tải sai hash không bao giờ ghi đè
    # (rồi xoá) mô hình tốt đang có.
    fd, tmp_name = tempfile.mkstemp(prefix=f".{dest.name}.", suffix=".download", dir=str(out_dir))
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        size = asyncio.run(fetch(args.url, tmp))
        digest = sha256_file(tmp)
        pinned = entry.get("sha256", "")
        if pinned:
            if not hmac.compare_digest(digest, pinned):
                print(f"SAI SHA-256! mong đợi {pinned}, nhận {digest}. Không thay file hiện có.", file=sys.stderr)
                return 2
            os.replace(tmp, dest)
            print(f"OK {dest} ({size} byte), SHA-256 khớp bản ghim trong models.lock.json.")
            return 0
        os.replace(tmp, dest)
    finally:
        with contextlib.suppress(OSError):
            tmp.unlink()
    dest.with_suffix(dest.suffix + ".sha256.pending").write_text(digest + "\n")
    print(
        f"Đã tải {dest} ({size} byte). SHA-256 = {digest}\n"
        "CHƯA GHIM: kiểm tra nguồn & giấy phép, rồi điền SHA-256 vào models.lock.json và chạy lại."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
