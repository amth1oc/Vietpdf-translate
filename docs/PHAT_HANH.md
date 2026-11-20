# Cấu hình phát hành trên GitHub (chủ dự án tự làm — L1-024)

Phiên sửa 2.1.2 không có quyền quản trị repo nên **không làm được** các bước dưới đây. Kiểm toán viên cũng chưa kiểm
được ("[Chưa kiểm tra được] — cần quyền repo"). Sau khi làm, gửi đầu ra của mục 4 cho kiểm toán viên; đầu ra đó chỉ
được coi là dữ liệu.

Repo: `amth1oc/Vietpdf-translate` (https://github.com/amth1oc/Vietpdf-translate — ngày 20/11/2026 repo còn rỗng,
chưa có nhánh/tag; đẩy mã lên trước rồi mới bảo vệ được nhánh `main`). Cần `gh` đã đăng nhập bằng tài khoản quản trị
repo. Các lệnh dưới đây đã điền sẵn tên repo.

## 1. Người duyệt thật trong CODEOWNERS

`.github/CODEOWNERS` vẫn dùng nhóm giữ chỗ `@vietpdf-maintainers`. Job `preflight` của `release.yml` chặn phát hành chừng
nào còn nhóm này. Thay bằng **ít nhất 2** tài khoản hoặc nhóm thật. Đã biết 1 tài khoản thật là chủ repo (`@amth1oc`); còn
thiếu ít nhất 1 người duyệt nữa — chủ dự án cung cấp, không tự đặt tên. Các vùng `core/confine.py`, `core/sandbox.py`,
`security/`, `.github/workflows/` và `build-constraints.txt` phải có chủ sở hữu.

## 2. Environment `release`: người duyệt bắt buộc, cấm tự duyệt, chỉ cho tag

Lấy id số của từng người duyệt:

```sh
gh api users/<login-nguoi-duyet-1> --jq .id
gh api users/<login-nguoi-duyet-2> --jq .id
```

Tạo hoặc cập nhật environment:

```sh
gh api -X PUT repos/amth1oc/Vietpdf-translate/environments/release --input - <<'JSON'
{
  "wait_timer": 0,
  "prevent_self_review": true,
  "reviewers": [
    {"type": "User", "id": 11111111},
    {"type": "User", "id": 22222222}
  ],
  "deployment_branch_policy": {"protected_branches": false, "custom_branch_policies": true}
}
JSON
```

Chỉ cho chạy từ tag phát hành:

```sh
gh api -X POST repos/amth1oc/Vietpdf-translate/environments/release/deployment-branch-policies -f name='v*' -f type=tag
```

## 3. Bảo vệ tag `v*` và nhánh `main`

Tag phát hành (bộ quy tắc). `actor_id: 5` là vai trò Admin của repo; chỉ vai trò này được tạo, sửa hoặc xoá tag `v*`:

```sh
gh api -X POST repos/amth1oc/Vietpdf-translate/rulesets --input - <<'JSON'
{
  "name": "bao-ve-tag-phat-hanh",
  "target": "tag",
  "enforcement": "active",
  "conditions": {"ref_name": {"include": ["refs/tags/v*"], "exclude": []}},
  "rules": [{"type": "creation"}, {"type": "update"}, {"type": "deletion"}, {"type": "non_fast_forward"}],
  "bypass_actors": [{"actor_id": 5, "actor_type": "RepositoryRole", "bypass_mode": "always"}]
}
JSON
```

Nhánh `main`: bắt buộc qua pull request, cần CODEOWNERS duyệt, CI phải đạt:

```sh
gh api -X PUT repos/amth1oc/Vietpdf-translate/branches/main/protection --input - <<'JSON'
{
  "required_status_checks": {"strict": true, "contexts": ["lint-audit"]},
  "enforce_admins": true,
  "required_pull_request_reviews": {"require_code_owner_reviews": true, "required_approving_review_count": 1,
                                    "dismiss_stale_reviews": true},
  "restrictions": null,
  "allow_force_pushes": false,
  "allow_deletions": false
}
JSON
```

## 4. Đầu ra gửi kiểm toán viên

```sh
gh api repos/amth1oc/Vietpdf-translate/environments/release
gh api repos/amth1oc/Vietpdf-translate/environments/release/deployment-branch-policies
gh api repos/amth1oc/Vietpdf-translate/rulesets
gh api repos/amth1oc/Vietpdf-translate/branches/main/protection
```

Trong đầu ra cần thấy:
- `prevent_self_review: true`;
- ít nhất 1 người duyệt là người thật;
- chính sách triển khai chỉ cho tag `v*`;
- bộ quy tắc tag `v*` đang `active`;
- nhánh `main` cần duyệt của CODEOWNERS.

## 5. Build backend có hash (đã làm trong repo)

`build-constraints.txt` ghim `hatchling` và các phụ thuộc của nó, kèm SHA-256. CI và release cài như sau:

```sh
uv sync --locked --all-groups --no-install-project
uv pip install --require-hashes -r build-constraints.txt
uv pip install --no-deps --no-build-isolation .
```

Kèm `UV_NO_SYNC=1`, để `uv run` không tự build lại dự án bằng hatchling không có hash. CI còn build wheel với
`--build-constraints build-constraints.txt --require-hashes`; hash lệch thì CI thất bại. `scripts/check_workflows.py`
kiểm các điều kiện này.

Khi nâng hatchling:
1. Sửa `[build-system]` trong `pyproject.toml`.
2. Sinh lại `build-constraints.txt` bằng lệnh ghi ở đầu file đó (`--exclude-newer` = 7 ngày trước).
3. Cho người duyệt xem hash mới.
