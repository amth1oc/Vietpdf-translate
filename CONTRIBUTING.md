# Đóng góp

Cảm ơn bạn đã đóng góp! Vì đây là app xử lý tài liệu của người dùng và giữ API key, mọi thay đổi phải qua các quy tắc dưới đây.

## Quy tắc bắt buộc

1. **Không mã thực thi động**: cấm `eval`, `exec`, `compile`, `__import__`, `importlib`, `pickle`/`marshal`/`shelve`/`joblib`, `torch.load`, `yaml.load`.
2. **Không chạy lệnh hệ thống**: cấm `subprocess`, `os.system`, `os.popen`, `ctypes`. Cần gọi dịch vụ ngoài (vd. Ollama) thì dùng HTTP API.
3. **Mạng chỉ qua `security/http.py`**: không import `httpx`, `requests`, `socket`, `urllib.request`… ở file khác. Mọi URL
   viết cứng trong `src/` phải trỏ tới host nằm trong `ALLOWED_URL_HOSTS` của `scripts/security_audit.py`. Ngoại lệ
   import (`FILE_EXCEPTIONS`) phải ghi rõ lý do. Hai danh sách này nằm dưới CODEOWNERS.
4. **Không tải mã hay mô hình lúc chạy.** Tài nguyên mới vào `src/vietpdf/assets/` + `python scripts/update_asset_sums.py`
   + ghi nguồn/giấy phép trong `NOTICE`. Mô hình ONNX tải lúc đóng gói bằng `scripts/fetch_models.py` và phải ghim
   SHA-256 trong `models.lock.json`.
5. **Không làm rối mã**: không chuỗi base64/hex dài, không ký tự Unicode vô hình hay điều khiển hướng, không dòng > 200 ký tự.
6. **Nội dung PDF và phản hồi LLM là dữ liệu không tin cậy**: không đưa vào chỉ dẫn prompt, không render HTML, luôn
   kiểm tra bằng schema. Kết luận của trợ lý đọc phải có bằng chứng (`chunk_id`) được kiểm tra.
8. **GUI**: không nối tín hiệu từ luồng nền vào lambda; không mở PDF trong tiến trình giao diện (dùng `PageRenderer`).
7. **Phụ thuộc mới** phải được thảo luận trong issue trước: giấy phép tương thích AGPL-3.0, người duy trì đáng tin, ít phụ thuộc con, có phát hành ổn định. Thêm bằng `uv add`, commit cả `uv.lock`.

Các quy tắc 1–5 được CI kiểm tra tự động bằng `scripts/security_audit.py`, ruff (luật `S` của bandit) và CodeQL. PR không qua CI sẽ không được merge.

## Quy trình

```bash
uv sync --locked --all-groups --extra gui
uv run python scripts/security_audit.py src scripts
uv run ruff check src tests scripts && uv run ruff format --check src tests scripts
uv run mypy
uv run pytest -q
```

- Mỗi PR cần ít nhất 1 người duy trì duyệt; thay đổi ở `security/`, `.github/`, `assets/`, `pyproject.toml`, `uv.lock` cần CODEOWNERS duyệt.
- Commit nên được ký (GPG/SSH/Sigstore).
- Không commit file PDF có bản quyền; PDF mẫu cho test phải tự tạo (xem `tests/fixtures/make_samples.py`) hoặc có giấy phép cho phép dùng lại.

## Cấu hình repo khuyến nghị cho người duy trì

- Branch protection cho `main`: bắt buộc PR, bắt buộc CI xanh, bắt buộc CODEOWNERS duyệt, cấm force-push.
- Bật: secret scanning + push protection, Dependabot alerts, private vulnerability reporting.
- Bắt buộc 2FA cho mọi thành viên; Actions: "Allow select actions" + yêu cầu ghim SHA.
