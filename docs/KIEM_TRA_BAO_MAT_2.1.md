# Báo cáo kiểm tra bảo mật — bản 2.1.0

Ngày: 02/11/2026. Phạm vi: toàn bộ mã nguồn `src/`, `scripts/`, bộ đóng gói, và các tính năng mới của 2.1
(thư viện tài liệu, sổ từ vựng, ôn tập, bôi đen để thêm từ, bản học song ngữ).

## 1. Cách kiểm tra

| Bước | Công cụ / cách làm | Kết quả |
| --- | --- | --- |
| Đọc mã theo từng ranh giới tin cậy | PDF → app, LLM → app, app → GUI, app → file xuất, app → mạng, dữ liệu cục bộ | 21 phát hiện (bảng 2) |
| Quét tĩnh | `ruff` (gồm bộ luật bảo mật `S`), `mypy --strict`, `scripts/security_audit.py` (AST: cấm eval/exec/pickle/subprocess, URL lạ, ký tự vô hình…) | 0 vấn đề còn lại |
| Thử ReDoS | chuỗi đối nghịch 20 000 ký tự vào mọi regex xử lý chữ từ PDF | 2 regex chậm 5–27 giây ⇒ đã sửa, nay < 0,1 giây |
| Fuzz | 12 PDF bị đột biến ngẫu nhiên (seed cố định) chạy qua toàn bộ pipeline | không treo, không crash tiến trình chính |
| Bí mật | gitleaks trên cả cây mã | không lộ khoá |
| Phụ thuộc | `pip-audit --strict` trên `uv.lock` (có hash) | không có lỗ hổng đã biết |
| Hồi quy | `pytest` (dịch giả, không gọi API), GUI chạy không màn hình | toàn bộ đạt |

## 2. Phát hiện và cách sửa

Mức: **Cao** = kẻ tấn công điều khiển được hành vi của app từ một file PDF; **TB** = rò rỉ/giả mạo/ghi đè có điều
kiện; **Thấp** = phòng thủ chiều sâu hoặc từ chối dịch vụ cục bộ.

| # | Mức | Phát hiện | Sửa | Test |
| --- | --- | --- | --- | --- |
| 1 | Cao | Bảng thuật ngữ, thuật ngữ của tài liệu và tên lĩnh vực (đều có thể bắt nguồn từ chữ trong PDF) được ghép vào **chỉ dẫn hệ thống** của LLM ⇒ PDF có thể cài lệnh (prompt injection) | Mọi dữ liệu từ tài liệu chỉ nằm trong trường JSON của tin nhắn người dùng (`glossary`, `domain`); `safe_term()` bỏ ký tự điều khiển, bidi và ký hiệu nội bộ ⟦ ⟧; quy tắc 6 nói rõ "chỉ là bảng tra" | `test_glossary_never_in_system_prompt`, `test_ai_meaning_keeps_document_text_out_of_system_prompt` |
| 2 | TB | Hộp thoại `QMessageBox.warning/information` tự đoán định dạng ⇒ thông báo lỗi chứa chữ từ PDF/LLM có thể được hiển thị như **HTML** (giả mạo giao diện, link) | `gui/safe.py` luôn đặt `PlainText` và che bí mật; cấm gọi `QMessageBox` trực tiếp | `test_gui_uses_plain_text_dialogs_only`, `test_safe_dialog_is_plain_text` |
| 3 | TB | Báo cáo, ghi chú, bảng so sánh, sổ từ xuất Markdown chèn nguyên văn chữ ngoài ⇒ Obsidian/Notion/GitHub có thể hiện HTML, ảnh từ xa (theo dõi), link `javascript:` | `md_escape()` cho mọi chữ không tin cậy | `test_markdown_outputs_escape_untrusted_text`, `test_export_formats_escape_untrusted_text` |
| 4 | TB | Kiểm tra cập nhật trỏ tới tên kho GitHub giữ chỗ (ai cũng có thể đăng ký) ⇒ thông báo "bản mới" giả dẫn tới trang lừa đảo | Tắt hẳn cho tới khi người phát hành cấu hình `REPO`; lệnh báo rõ | `test_update_check_disabled_without_official_repo` |
| 5 | TB | Ghi file tạm có tên đoán được / ghi đè trực tiếp ⇒ trên máy nhiều người dùng có thể cài **symlink** để app ghi đè file khác | `security/fsutil.py`: `mkstemp` cùng thư mục + `replace` nguyên tử cho cài đặt, lịch sử, IR, PDF đầu ra, sổ từ xuất ra, bản sao trong thư viện | `test_atomic_write_ignores_planted_symlink`, `test_library_copy_does_not_follow_planted_symlink` |
| 6 | TB | Link trong PDF đầu ra còn giữ `javascript:`, `file:`, `data:`, Launch, GoToR ⇒ bấm vào bản dịch có thể mở chương trình/file cục bộ | `_scrub` chỉ giữ `http(s)://`, `mailto:`, link nội bộ | `test_dangerous_uri_links_removed` |
| 7 | TB | ReDoS: regex tìm định nghĩa trong bài mất ~27 giây với 20 000 ký tự; regex trích thuật ngữ ~5,7 giây ⇒ PDF độc làm treo app | Giới hạn độ dài từng nhóm, neo ranh giới từ | `test_regex_heavy_functions_are_linear_enough` |
| 8 | Thấp | DOI đọc từ danh mục tham khảo được ghép thẳng vào đường dẫn URL tra cứu (`../`, `?`, `#`) | `safe_doi()` chỉ nhận dạng DOI hợp lệ | `test_doi_validation` |
| 9 | Thấp | `render` cho phép file đầu ra trùng file gốc ⇒ mất bản gốc | Từ chối | `test_rerender_refuses_to_overwrite_source` |
| 10 | Thấp | Mật khẩu PDF qua `--password` lộ trong danh sách tiến trình | Thêm `--ask-password` (nhập ẩn); biến môi trường bị xoá sau khi đọc | `test_pdf_password_env_is_popped` |
| 11 | Thấp | Gộp thuật ngữ con O(n²) ⇒ sách 1 500 trang rất chậm | O(n) theo tiền tố/hậu tố n-gram (10,8 s → 7,2 s) | đo trên sách giả 1 500 trang |
| 12 | Thấp | Phản hồi stream không giới hạn số dòng; kiểm tra trích dẫn không giới hạn độ dài | Giới hạn 1 000 000 dòng; cắt câu hỏi/đoạn trước khi so khớp | — |
| 13 | TB | (2.1) Kho học tập chứa ghi chú, câu hỏi, từ vựng, đường dẫn tài liệu — dữ liệu riêng tư | Mã hoá từng dòng AES-256-GCM, khoá trong kho mật khẩu hệ điều hành, AAD = `bảng:id` (chống đổi chỗ dòng); file 0600, thư mục 0700; dòng bị sửa ⇒ bỏ qua, không crash | `test_vocab_crud_dedup_and_encryption_at_rest`, `test_tampered_row_is_dropped_not_crashing` |
| 14 | TB | (2.1) Thư mục tài liệu trong thư viện dựng từ id ⇒ nguy cơ path traversal | id phải là chữ-số, kiểm tra `is_relative_to`; tên file cố định (`goc.pdf`, `ban-dich.pdf`…), không lấy tên từ dữ liệu ngoài | `test_library_ids_cannot_escape_directory` |
| 15 | TB | (2.1) Bôi đen trên trang PDF cần đọc chữ của PDF | Đọc trong **tiến trình con render** (không mạng, giới hạn bộ nhớ), chỉ trả chuỗi ≤ 2 000 ký tự | `test_select_text_on_page_and_add_to_vocab` |
| 16 | Thấp | (2.1) Mở file bằng ứng dụng mặc định của hệ điều hành | Chỉ mở file `.pdf` nằm trong bản ghi thư viện | — |
| 17 | Thấp | (2.1) Xuất Anki: trường chứa `<img>`/`<script>`; dòng bắt đầu bằng `#` bị hiểu là dòng tuỳ chọn | Tiêu đề `#html:false`; dòng dữ liệu không bao giờ bắt đầu bằng `#` | `test_export_formats_escape_untrusted_text` |
| 18 | Thấp | (2.1) Xuất CSV chứa `=HYPERLINK(...)` (CSV injection) | Thêm `'` trước `= + - @`; khi nhập lại thì bỏ `'`; nhập tối đa 10 MB / 20 000 dòng | `test_import_csv_roundtrip_and_limits` |
| 19 | Thấp | (2.1) Tra nghĩa bằng AI gửi từ + câu trong bài | Chỉ khi người dùng bấm; dữ liệu trong JSON người dùng; kết quả qua `sanitize_output`, gắn nhãn "Giải thích của AI" | `test_ai_meaning_keeps_document_text_out_of_system_prompt` |
| 20 | Thấp | (2.1) Lịch sử hỏi đáp/ghi chú hiển thị lại trong GUI | Chỉ `QPlainTextEdit`/`PlainText`; menu chuột phải thoát ký tự `&` | `test_library_reopens_at_last_page_and_keeps_assist_history` |
| 21 | Thấp | (2.1) Phím tắt ôn tập 1–4 chặn việc gõ số trong ô đáp án/bảng | Chỉ xử lý khi thẻ có focus | `test_review_session_flow` |

## 3. Rủi ro còn lại

| Rủi ro | Mức | Hướng xử lý |
| --- | --- | --- |
| Tiến trình con vẫn đọc được file của người dùng; khoá mạng ở mức Python | TB | seccomp / AppContainer / sandbox-exec |
| Không có kho mật khẩu an toàn (Linux không có Secret Service) ⇒ cache và kho học tập **không mã hoá** (vẫn 0600) | TB | `vietpdf library list` và tab Thư viện cảnh báo rõ; nên dùng ổ đĩa mã hoá |
| Bản sao tài liệu trong thư viện (`goc.pdf`, `ban-dich.pdf`) không mã hoá — giống file gốc | Thấp | Dùng `library add --no-copy` để chỉ lưu đường dẫn |
| Prompt injection tinh vi vẫn có thể làm lệch bản dịch một đoạn | Thấp | Cờ `security:injection-like`, biên tập lượt 2, báo cáo; bản dịch không bao giờ được thực thi |
| Bộ cài chưa ký số | Uy tín | SignPath / Azure Trusted Signing / Apple Developer ID |

## 4. Tự kiểm tra lại

```bash
uv run python scripts/security_audit.py src scripts
uv run ruff check src tests scripts && uv run mypy
QT_QPA_PLATFORM=offscreen uv run pytest -q tests/unit/test_security_review.py tests/unit/test_learning.py
uv export --locked --no-emit-project --all-extras --format requirements-txt > req.txt
uv run pip-audit --strict -r req.txt --require-hashes --disable-pip
gitleaks dir .
```

## 5. Bản vá 2.1.1 — theo kiểm toán độc lập lượt 1 (02/11/2026)

Kiểm toán độc lập tìm thấy 26 vấn đề (1 Cao, 8 Trung bình, 17 Thấp) mà đợt tự kiểm tra ở trên **bỏ sót**; một số khẳng
định ở mục 2–3 (tiến trình con "không mạng, không key"; PDF đầu ra đã gỡ JavaScript/Launch; CI tạo bản phát hành) khi
đó **không đúng**. 2.1.1 đã sửa các vấn đề này và giữ các cách tấn công của kiểm toán thành test đối kháng trong
`tests/unit/test_hardening_211.py`.

**[Đính chính ở 2.1.2]** Câu trước đây "Tất cả đã được sửa trong 2.1.1" là **SAI**. Kiểm toán lượt 1R (11/11/2026) kết
luận: 21 Đã sửa, 4 Sửa một phần (L1-001 — gồm Windows/macOS —, L1-010, L1-024, L1-025), 1 Chưa xác nhận (L1-007),
kèm 3 hồi quy (R1-004, R1-005, R1-006) và 2 phát hiện mới mức Cao trong chính bản vá (R1-001: tiến trình dựng PDF
đọc/ghi/xoá được cả thư mục chứa PDF gốc, gồm HOME; R1-002: thiếu Landlock thì cô lập file mất hẳn mà không báo).
Trạng thái sau 2.1.2: xem `CHANGELOG.md` mục 2.1.2 (bên sửa chỉ ghi "đã làm, tự kiểm"; kết luận "đã sửa" thuộc về
kiểm toán viên).

Bảng gốc của 2.1.1:

| Mã | Vấn đề (2.1.0) | Sửa (2.1.1) |
| --- | --- | --- |
| L1-001 | Khoá mạng chỉ mức Python: tiến trình con mở được mạng bằng `_socket`/ctypes/subprocess, đọc `/proc/<cha>`, ghi khắp HOME, sống sau khi app chết | `core/confine.py`: seccomp + Landlock + PDEATHSIG (Linux), Job Object (Windows) |
| L1-002 | Khoá nhà cung cấp gửi tới Base URL tuỳ chỉnh bất kỳ | Khoá riêng `custom`; `build_translator` từ chối |
| L1-003 | Khoá trong biến môi trường lọt vào tiến trình con | `capture_env_secrets()` khi khởi động; env tối giản; không dumpable |
| L1-004 | `--offline` vẫn đi qua `HTTP_PROXY`; `SSL_CERT_FILE` thay kho CA | `trust_env=False`; proxy tường minh |
| L1-005 | Mã hoá fail-open; ghi đè khoá hỏng; nhận bản ghi `p:` | fail-closed; `library migrate` |
| L1-006 | `_scrub` bỏ sót hành động inline/`/Next`/mục lục/Widget/XFA | allowlist hành động + quét lại, fail-closed |
| L1-007 | onnxruntime 1.31 telemetry gọi DNS Microsoft cả khi `--offline` | onnxruntime < 1.29; seccomp |
| L1-008 | Không xin đồng ý trước khi gửi (chỉ Gemini có ghi chú) | Đồng ý theo `provider@host`, `--accept-privacy` |
| L1-009 | `release.yml` không phải YAML hợp lệ | Sửa + `scripts/check_workflows.py` trong CI |
| L1-010 … L1-026 | Chữ ẩn được dịch; PDF đầu ra 0644; một bbox lỗi hỏng cả tài liệu; huỷ để lại tiến trình con; RecursionError; bidi U+061C; mô hình tin `.sha256` cạnh file; CSV `＝`; Markdown tên file; `protect()` O(n²); SHA256SUMS tự tham chiếu; tooltip; keyringrc; security_audit bỏ sót alias; cooldown không thực thi; test chỉ trường hợp thuận lợi; tải mô hình không nguyên tử | Xem CHANGELOG 2.1.1 |

Kiểm lại: toàn bộ PoC của kiểm toán được chạy lại trên 2.1.1 (báo cáo kiểm lại đi kèm bản vá). Còn mở: cô lập mạng/file
trên Windows (AppContainer) và macOS, đo prompt injection với mô hình thật (lượt 2), các kiểm tra cần máy Windows và
quyền repo (lượt 3).
