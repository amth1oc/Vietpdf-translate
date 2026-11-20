#!/usr/bin/env python3
"""Kiểm workflow GitHub Actions trong CI (2.1.1, L1-009): YAML hợp lệ (release.yml 2.1.0 hỏng cú pháp nên không bao giờ
chạy), mọi `uses:` ghim SHA 40 ký tự, có khối `permissions`, không dùng `pull_request_target`, không chèn
`${{ github.event.* }}` vào `run:`, job phát hành dùng environment cần phê duyệt.
2.1.2 (L1-024): job nào `uv sync` thì phải `--no-install-project`, cài build backend từ `build-constraints.txt` bằng
`--require-hashes`, và đặt `UV_NO_SYNC=1` (để `uv run` không build lại dự án bằng hatchling không có hash)."""

from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
SHA = re.compile(r"^[^@\s]+@[0-9a-f]{40}$")


def check(path: Path) -> list[str]:
    errs: list[str] = []
    try:
        wf = yaml.safe_load(path.read_text("utf-8"))
    except yaml.YAMLError as e:
        return [f"{path.name}: YAML không hợp lệ: {e}".replace("\n", " ")]
    if not isinstance(wf, dict) or "jobs" not in wf:
        return [f"{path.name}: thiếu 'jobs'"]
    triggers = wf.get(True, wf.get("on"))  # PyYAML đọc khoá `on` thành True
    if isinstance(triggers, dict) and "pull_request_target" in triggers:
        errs.append(f"{path.name}: không dùng pull_request_target")
    if "permissions" not in wf:
        errs.append(f"{path.name}: thiếu permissions cấp workflow")
    for name, job in (wf.get("jobs") or {}).items():
        for step in job.get("steps", []) or []:
            uses = step.get("uses")
            if uses and not uses.startswith("./") and not SHA.match(uses.split("#")[0].strip()):
                errs.append(f"{path.name}:{name}: action chưa ghim commit SHA: {uses}")
            run = str(step.get("run", ""))
            if "${{ github.event." in run or "${{github.event." in run:
                errs.append(f"{path.name}:{name}: chèn github.event vào run (nguy cơ chèn lệnh)")
        runs = "\n".join(str(st.get("run", "")) for st in job.get("steps", []) or [])
        if "uv sync" in runs:
            if any("uv sync" in ln and "--no-install-project" not in ln for ln in runs.splitlines()):
                errs.append(f"{path.name}:{name}: uv sync phải có --no-install-project (dự án cài bằng build có hash)")
            if not re.search(r"--require-hashes\s+-r\s+build-constraints\.txt", runs):
                errs.append(f"{path.name}:{name}: thiếu cài build backend có hash (build-constraints.txt)")
            if str((job.get("env") or {}).get("UV_NO_SYNC", "")) != "1":
                errs.append(f"{path.name}:{name}: thiếu env UV_NO_SYNC=1")
        writes = "contents: write" in yaml.safe_dump(job.get("permissions", {}))
        if path.name == "release.yml" and writes and not job.get("environment"):
            errs.append(f"{path.name}:{name}: job có quyền ghi phải dùng environment cần phê duyệt")
    return errs


def check_build_constraints(path: Path) -> list[str]:
    """build-constraints.txt: hatchling đúng phiên bản trong [build-system] và MỌI gói đều có --hash."""
    if not path.is_file():
        return [f"{path.name}: không có"]
    text = path.read_text("utf-8")
    pins = re.findall(r"^([A-Za-z0-9_.\-]+)==(\S+)", text, re.M)
    errs = [
        f"{path.name}: {n} thiếu --hash"
        for n, _ in pins
        if not re.search(rf"^{re.escape(n)}==\S+\s*\\\n\s+--hash=sha256:", text, re.M)
    ]
    py = (ROOT / "pyproject.toml").read_text("utf-8")
    want = re.search(r'requires\s*=\s*\["hatchling==([^"]+)"\]', py)
    if not want or ("hatchling", want.group(1)) not in pins:
        errs.append(f"{path.name}: hatchling không khớp [build-system] của pyproject.toml")
    return errs


def main() -> int:
    errs = [e for p in sorted((ROOT / ".github" / "workflows").glob("*.yml")) for e in check(p)]
    errs += check_build_constraints(ROOT / "build-constraints.txt")
    for e in errs:
        print(e)
    print(f"check_workflows: {len(errs)} vấn đề", file=sys.stderr)
    return 1 if errs else 0


if __name__ == "__main__":
    sys.exit(main())
