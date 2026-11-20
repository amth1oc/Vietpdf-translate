# Mô hình bảo mật (v2.1.1)

Báo cáo kiểm tra chi tiết của đợt 2.1 và bản vá 2.1.1 (theo kiểm toán độc lập lượt 1): [KIEM_TRA_BAO_MAT_2.1.md](KIEM_TRA_BAO_MAT_2.1.md).

## Tài sản cần bảo vệ

1. **API key** của người dùng (tiền và hạn mức).
2. **Nội dung tài liệu** (hợp đồng, tài liệu mật) và bản dịch trong cache; **thư viện, ghi chú, lịch sử hỏi
   đáp và sổ từ** của người học (2.1).
3. **Máy người dùng** (không bị thực thi mã lạ từ PDF, từ phản hồi LLM, hay từ chính mã nguồn/phụ thuộc).
4. **Uy tín bản phát hành** (người dùng tải đúng bản build từ mã nguồn công khai).

## Ranh giới tin cậy

```
                 không tin cậy                             tin cậy (đã kiểm tra)
 ┌──────────────────────────────────┐  JSON + pydantic  ┌────────────────────────────────┐
 │ PDF → MuPDF / OCR / ONNX         │ ────────────────▶ │ Tiến trình chính / GUI         │
 │ tiến trình con spawn:            │                   │ - API key chỉ trong bộ nhớ     │
 │  · env tối giản, seccomp+Landlock │ ◀──────────────── │ - IR đã kiểm tra               │
 │  · RLIMIT/Job + RSS + PDEATHSIG  │  IR đã kiểm tra   │ - SafeHttpClient (allowlist)   │
 └──────────────────────────────────┘                   └──────────────┬─────────────────┘
 ┌──────────────────────────────────┐  JSON Schema, xác minh            │ HTTPS
 │ Phản hồi LLM                     │ ◀──── id, ký hiệu, trích dẫn ─────┘
 └──────────────────────────────────┘
```

## Biện pháp theo ranh giới

| Ranh giới | Kiểm soát | Mã nguồn | Test |
| --- | --- | --- | --- |
| PDF → app | chữ ký `%PDF-`, kích thước; tiến trình con `spawn` với **môi trường tối giản**; **Linux: seccomp** (không socket mạng, không `connect`, không exec/fork/ptrace, **không gửi tín hiệu tới tiến trình khác**) + **Landlock** (chỉ đọc đúng file đầu vào được chỉ định + thư viện Python/hệ thống — vẫn đọc được toàn bộ `/etc`, `/usr`, `/proc/net`, xem R1-008; chỉ ghi **thư mục tạm riêng của job** `.vietpdf-stage-*` (2.1.2, R1-001) — tiến trình chính kiểm file rồi mới chuyển sang đích, quét nội dung chủ động trong một tiến trình con mới chỉ có quyền đọc) + `PR_SET_PDEATHSIG`; **cô lập fail-closed** (2.1.2, R1-002): tiến trình con báo mức cô lập về tiến trình cha trước khi đọc dữ liệu, thiếu Landlock/seccomp/NO_NEW_PRIVS thì từ chối (trừ `--allow-weak-sandbox`/hộp thoại GUI); **Windows (2.1.3): AppContainer** không capability mạng (chặn mạng, chỉ đọc thư viện + đúng file đầu vào, chỉ ghi thư mục tạm của job — quyền file cấp cho capability ngẫu nhiên của từng tiến trình con, gỡ sau job) + **Job Object** gán khi còn SUSPENDED (chết theo app, 1 tiến trình, giới hạn bộ nhớ); thiếu ⇒ từ chối như Linux; **macOS**: chưa có sandbox — cô lập YẾU, xem mục "Quyết định Windows/macOS"; RLIMIT_AS/CPU/core + giám sát RSS; timeout + kill; huỷ tức thì (`cancel_all`); kết quả JSON (không pickle, chống lồng sâu); IR kiểm tra lại từng phần tử (phần tử lỗi bị bỏ riêng) | `core/confine.py`, `core/sandbox.py`, `core/ir.py`, `core/pipeline.py` | `test_hardening_211.py`, `test_isolation_212.py`, `test_isolation_win_213.py`, `test_sandbox_*` |
| PDF → GUI | GUI không mở PDF: trang được render trong **tiến trình con thường trực** (`PageRenderer`), trả PNG base64, kiểm tra chữ ký PNG | `core/sandbox.py::PageRenderer` | GUI test |
| App → mạng | một cửa ngõ duy nhất; allowlist đúng host nhà cung cấp; HTTPS bắt buộc (trừ loopback); không redirect (tải mô hình: redirect chỉ tới CDN liệt kê sẵn); không userinfo; giới hạn phản hồi; offline nghiêm ngặt; **bỏ qua proxy/CA/.netrc từ biến môi trường** (proxy chỉ cấu hình tường minh, không áp cho loopback); **đồng ý của người dùng trước lần gửi đầu tới mỗi `provider@host`**; khoá nhà cung cấp không bao giờ gửi tới Base URL tuỳ chỉnh | `security/http.py`, `translate/providers.py` | `test_security_http.py`, `test_hardening_211.py` |
| LLM → app | JSON Schema; đủ id, không id lạ; ký hiệu ⟦M/F/T⟧ còn nguyên; giới hạn độ dài; NFC; bỏ ký tự điều khiển/bidi; thuật ngữ LLM đề xuất chỉ nhận nếu nằm trong danh sách đã gửi; siêu tham số chỉ nhận nếu giá trị có trong bài; trích dẫn hỏi đáp phải khớp nguyên văn | `translate/engine.py`, `translate/qa.py`, `translate/terms.py`, `assistant/reader.py` | `test_*fabricated*`, `test_propose_terms_*` |
| Prompt injection | nội dung PDF chỉ nằm trong tin nhắn người dùng dạng JSON, không bao giờ trong chỉ dẫn hệ thống; mẫu câu lệnh (Anh + Việt) so trên văn bản **đã chuẩn hoá** (NFKC, casefold, bỏ dấu, đồng hình, bỏ ký tự vô hình/chữ trống, dạng dính liền) và trên **cửa sổ 2–3 đoạn**; đây chỉ là tín hiệu, không phải rào chắn; **2.1.2: chữ không nhìn thấy phát hiện bằng so ảnh render** (trang đầy đủ vs trang đã xoá chữ) + chiều cao glyph thiết bị < 3 pt — không gửi đi dịch theo mặc định nhưng **liệt kê trong báo cáo/GUI** (không lặng lẽ bỏ); chữ rất mờ / bị che một phần / chồng chữ khác nhau được dịch nhưng gắn cờ; **mọi đoạn có cờ `security:` được dịch riêng từng đoạn, không kèm ngữ cảnh, không làm ngữ cảnh cho đoạn khác, không vào bước tóm tắt/thuật ngữ, bản dịch được kiểm (độ dài, URL/email/mã mới, đúng ngôn ngữ)**; tiêu đề/mục lục/tóm tắt nghi vấn không vào ngữ cảnh | `core/extract.py`, `translate/prompts.py`, `translate/qa.py`, `core/pipeline.py` | `test_prompt_never_contains_document_text_in_system`, `test_hidden_injection_212.py` |
| App → PDF đầu ra | **allowlist annotation** (Link, Text, Highlight, Underline, StrikeOut, Squiggly, Square, Circle, Line, Ink, FreeText, Popup — loại khác bị xoá, trường biểu mẫu thành nội dung tĩnh; 2.1.2, R1-004) và allowlist hành động (GoTo, Named, URI http(s)/mailto) ở **mọi vị trí** — /A, /AA, /OpenAction, chuỗi /Next, khoá thừa như /JS kèm GoTo, inline hay đối tượng riêng; xoá XFA, file nhúng; báo cáo ghi số đối tượng đã xoá theo loại; **chốt kiểm cuối chạy ở tiến trình con mới chỉ đọc**, còn sót thì làm sạch lại một lần (không gọi API), vẫn còn thì không ghi; ghi nguyên tử, quyền 0600 | `core/render.py::_scrub`, `assert_no_active_content`, `core/sandbox.py::publish_staged` | `test_inline_and_chained_actions_are_removed`, `test_active_content_212.py` |
| App → file xuất | BibTeX bỏ `{}\@`; CSV chống công thức (`= + - @`); TSV bỏ tab/xuống dòng; **Markdown thoát HTML/link/ảnh**; **Anki `#html:false`** (2.1) | `assistant/export.py`, `textfmt.py`, `library/store.py` | `test_exports_are_sanitized`, `test_markdown_outputs_escape_untrusted_text` |
| App → GUI | chỉ văn bản thuần (**kể cả hộp thoại: `gui/safe.py`**); che bí mật; callback từ luồng nền luôn chạy ở luồng giao diện; chữ bôi đen trên trang đọc trong tiến trình con render | `gui/` | `test_gui_smoke.py`, `test_gui_learning.py` |
| Ghi file | file tạm `mkstemp` cùng thư mục + `replace` nguyên tử (không đi theo symlink cài sẵn); không ghi đè file gốc | `security/fsutil.py` | `test_atomic_write_ignores_planted_symlink` |
| Kho học tập (2.1) | SQLite; nội dung mỗi dòng mã hoá AES-256-GCM, AAD = `bảng:id`; khoá trong keyring; 0600/0700; id tài liệu chữ-số + `is_relative_to`; tên file cố định; dòng hỏng **và dòng chữ thường khi đã bật mã hoá** bị bỏ qua (`library migrate` để chuyển dữ liệu cũ) | `library/store.py`, `security/crypto.py` | `test_learning.py`, `test_plaintext_rows_rejected_when_encryption_enabled` |
| API key | backend keyring của hệ điều hành **khởi tạo trực tiếp** (không đọc keyringrc.cfg/PYTHON_KEYRING_BACKEND); mọi `VIETPDF_*_API_KEY` gỡ khỏi môi trường ngay khi khởi động; tiến trình chính không dumpable (Linux); che trong log/traceback | `security/secrets.py`, `core/confine.py`, `security/logsafe.py` | `test_capture_env_secrets_removes_all_provider_keys`, `test_keyringrc_and_env_cannot_select_backend` |
| Cache | khoá hàng là SHA-256; giá trị **mã hoá AES-256-GCM** (khoá 256-bit trong keyring, AAD = khoá hàng); quyền 0600/0700; xoá mục quá 180 ngày; kho có nhưng lỗi tạm/khoá hỏng ⇒ **dừng** (không ghi dạng rõ, không ghi đè khoá); không có kho an toàn ⇒ không mã hoá, cảnh báo | `translate/cache.py`, `security/secrets.py` | `test_cache_encryption`, `test_cache_key_fail_closed` |
| Tài nguyên | SHA-256 cho font/glossary; mã băm của `SHA256SUMS` ghim trong mã nguồn; phát hiện file lạ (kể cả file ẩn); chống path traversal | `security/integrity.py` | `test_tampered_asset_detected`, `test_assets_hidden_file_and_sums_tamper_detected` |
| Mô hình ONNX | phải có SHA-256 ghim trong `models.lock.json` (đi kèm app hoặc trong thư mục cấu hình) mới nạp — **không** tin file `.sha256` cạnh mô hình; không custom op; chạy trong tiến trình con bị cô lập; onnxruntime < 1.29 (không telemetry mạng) | `core/layout_model.py` | `test_layout_model_integrity_and_parsing` |
| Cập nhật | **tắt cho tới khi người phát hành cấu hình kho chính thức**; tuỳ chọn, chỉ gọi GitHub Releases API, **chỉ thông báo**, không tự tải/chạy; URL trả về bị ép về trang releases chính thức | `security/update.py` | `test_update_check_never_downloads` |

## Chống gài mã độc (chuỗi cung ứng)

| Lớp | Biện pháp |
| --- | --- |
| Mã nguồn | `scripts/security_audit.py` (AST, quét cả `tests/`, giải alias import) chặn: `eval/exec/compile/__import__`, `importlib`, `pickle/marshal/shelve/joblib`, `torch.load`, `yaml.load`, `subprocess/os.system/ctypes`, thư viện mạng ngoài cửa ngõ, `verify=False`, `shell=True`, chuỗi base64/hex dài, dòng quá dài, ký tự bidi/vô hình (Trojan Source), **URL tới host không nằm trong danh sách cho phép**. Ngoại lệ ghi rõ lý do (`FILE_EXCEPTIONS`) và nằm dưới CODEOWNERS |
| Phụ thuộc | 6 phụ thuộc trực tiếp (27 gói kể cả bắc cầu); OCR/bố cục/GUI là extras tuỳ chọn; mọi ràng buộc có cận trên; `uv.lock` có hash; `exclude-newer = 7 days` cho mọi lần lock + Dependabot cooldown 7 ngày; build backend ghim; `pip-audit --strict` |
| CI | Actions ghim commit SHA; `permissions: contents: read`; `persist-credentials: false`; không `pull_request_target`; `scripts/check_workflows.py` kiểm cú pháp/chính sách workflow; gitleaks ghim phiên bản + SHA-256; CodeQL `security-extended`; OpenSSF Scorecard |
| Phát hành | chỉ build từ tag trên Actions; `preflight` chặn khi CODEOWNERS còn nhóm giữ chỗ; job publish dùng environment `release` (cần người duyệt); SBOM CycloneDX; SHA256SUMS; artifact attestation (Sigstore); Release dạng nháp; UPX tắt |

## Lỗi bảo mật/độ tin cậy tìm thấy và đã sửa trong đợt v2.0

| Lỗi | Hậu quả nếu không sửa | Cách phát hiện |
| --- | --- | --- |
| Tín hiệu luồng nền nối vào lambda ⇒ callback chạy trong luồng nền, đụng widget (có từ 0.1) | GUI có thể crash/treo ngẫu nhiên | test GUI không màn hình |
| Cache SQLite tạo ở luồng giao diện, dùng ở luồng nền | trợ lý đọc trong GUI luôn lỗi | test GUI |
| Chế độ JSON riêng của nhà cung cấp không được truyền vào adapter | Claude/DeepSeek lỗi định dạng | test máy chủ giả |
| Cắt từ quá dài bọc thừa một lớp danh sách | URL dài làm hỏng dàn chữ | mypy |
| Ký tự điều khiển hướng lọt vào mã nguồn khi soạn thảo | Trojan Source | audit |

## Rủi ro còn lại

| Rủi ro | Mức | Hướng |
| --- | --- | --- |
| **macOS**: chưa có sandbox-exec — cho chạy kèm **cảnh báo thường trực** (quyết định tạm thời, xem mục dưới). **Windows** (2.1.3): AppContainer + Job Object, thiếu thì từ chối. **Linux** thiếu Landlock/seccomp/NO_NEW_PRIVS: từ 2.1.2 **từ chối** xử lý PDF (R1-002) trừ khi người dùng chủ động cho phép cho lần chạy/phiên đó; mức cô lập được tiến trình con báo về tiến trình cha và hiện ở CLI `-v`, GUI (thanh trạng thái) và `report.md` của mỗi job (trước 2.1.2 chỉ nằm trong `CONFINE_STATUS` của tiến trình con, không ai đọc) | Trung bình (macOS) | sandbox-exec (macOS) — cần làm và kiểm trên máy Mac thật |
| Khi người dùng CHO PHÉP cô lập yếu (Linux thiếu Landlock), tiến trình con đọc/ghi được HOME; seccomp vẫn chặn mạng, exec, fork, gửi tín hiệu ra ngoài | Cao nếu gặp PDF độc | chỉ cho phép với PDF nguồn tin cậy; không lưu lựa chọn này vĩnh viễn |
| Landlock vẫn cho tiến trình con ĐỌC `/etc`, `/usr`, `/proc/net/tcp`, `/etc/machine-id`; seccomp chưa là danh sách cho phép (`unshare`, `keyctl`, `memfd_create`, `sendto` socket UNIX có đường dẫn... — R1-008) | Thấp | xem CHANGELOG 2.1.2 phần R1-008 |
| Ai ghi được `models.lock.json` trong thư mục cấu hình người dùng thì chọn được mô hình ONNX được nạp (L1-016). Từ 2.1.2 tiến trình con không còn ghi được thư mục cấu hình (R1-001); file chỉ được tiến trình chính đọc | Thấp | — |
| macOS không có giới hạn bộ nhớ cứng (chỉ giám sát RSS 0.2s/lần) | Thấp | — |
| Thư viện OCR (rapidocr) có phụ thuộc `requests`; onnxruntime ≥ 1.29 có telemetry mạng | Thấp (Linux, Windows) / TB (macOS, chưa chặn mạng ở mức OS) | onnxruntime ghim < 1.29; seccomp (Linux) / AppContainer không capability mạng (Windows) chặn mạng |
| **Windows (2.1.3)**: AppContainer vẫn ĐỌC được những gì Windows cấp sẵn cho "ALL APPLICATION PACKAGES" (System32, Program Files...) và mọi thư mục thư viện Python/app; ACE đọc thư viện cho SID của AppContainer được giữ lại sau khi app thoát. ACE của job sót lại nếu app bị kill giữa chừng chỉ trỏ tới một capability ngẫu nhiên không tiến trình nào còn giữ. Ổ FAT/exFAT (USB) không có ACL nên không giới hạn được đọc/ghi trên ổ đó. Thư viện cài ở thư mục người dùng không có quyền WRITE_DAC (vd. Python cài cho mọi người dùng do quản trị viên) ⇒ không cấp được quyền đọc ⇒ từ chối (fail-closed) kèm lệnh `icacls` để quản trị viên cấp cho ALL APPLICATION PACKAGES. Mỗi tiến trình con tốn thêm ~0,5–1 s (cấp/gỡ ACL, dựng AppContainer) | Thấp | bộ cài cấp quyền đọc thư mục cài đặt cho ALL APPLICATION PACKAGES |
| Bộ cài chưa ký số | Uy tín | SignPath Foundation / Azure Trusted Signing; Apple Developer ID |
| Không có kho mật khẩu ⇒ cache và kho học tập không mã hoá (vẫn 0600); bản sao PDF trong thư viện không mã hoá | TB | cảnh báo trong tab Thư viện và `library list`; `--no-copy`; ổ đĩa mã hoá |
| Prompt injection tinh vi (diễn đạt không khớp mẫu) vẫn có thể làm lệch bản dịch một đoạn | Thấp–TB (chờ đo ở lượt 2) | chữ ẩn được phát hiện bằng so ảnh và liệt kê; đoạn bị cờ dịch riêng không ngữ cảnh và kiểm đầu ra; bản dịch không bao giờ được thực thi |
| So ảnh render không tách được chữ ẩn đặt CHỒNG ĐÚNG lên chữ nhìn thấy (chỉ gắn cờ `security:overlapping-text` khi ký tự khác nhau); chữ trong vùng clip và chữ xoay không vào bản dịch nhưng cũng không được liệt kê; trang có ảnh phủ > 60 % và chữ vô hình trên ảnh được coi là lớp OCR có sẵn (dịch, liệt kê theo trang) | Thấp–TB | đo thêm ở lượt 2 |

## Quyết định Windows/macOS (2.1.2)

Câu hỏi đầu phiên sửa 2.1.2: trên Windows/macOS (cô lập mới ở mức yếu: Windows chỉ có Job Object, macOS chưa có
sandbox), (a) chặn mở PDF cho tới khi có AppContainer/sandbox-exec, hay (b) cho chạy nhưng cảnh báo thường trực?

- Chủ dự án (11/11/2026) chọn hướng **làm lớp cô lập mạnh** (AppContainer cho Windows, sandbox-exec cho macOS) và
  **kiểm trên máy Windows/Mac thật** của chủ dự án, bằng Claude Code chạy trên máy đó — không viết mã chưa chạy được.
- Cho tới khi lớp đó được làm và kiểm, 2.1.2 dùng **(b) — LỰA CHỌN TẠM THỜI**: trên Windows/macOS app vẫn xử lý PDF
  nhưng mức cô lập luôn được báo là **YẾU** ở thanh trạng thái GUI (chữ đỏ), ở CLI (cảnh báo sau mỗi lệnh có tiến
  trình con) và trong `report.md` của mỗi job, kèm lý do ("chỉ có Job Object — CHƯA chặn mạng và đọc file người
  dùng" / "CHƯA có sandbox"). Hằng `WEAK_PLATFORM_POLICY` trong `core/sandbox.py` đổi sang `"block"` để chuyển sang
  (a).
- **Windows — 2.1.3 (20/11/2026)**: lớp cô lập mạnh đã làm và tự kiểm trên máy Windows 10 thật (AppContainer không capability mạng + Job Object; PoC đọc HOME, ghi Startup, TCP loopback, DNS, sinh tiến trình đều bị từ chối — `tu_kiem_win_mac.md`). Chính sách Windows chuyển sang **(a)/fail-closed**: `WEAK_PLATFORM_POLICY["win32"] = "block"` — lớp mạnh không áp dụng được thì từ chối như Linux thiếu Landlock, trừ khi người dùng chủ động cho phép (`--allow-weak-sandbox`, hộp thoại GUI).
- **macOS**: vẫn (b) tạm thời (`WEAK_PLATFORM_POLICY["darwin"] = "warn"`) cho tới khi sandbox macOS được làm và kiểm trên máy Mac.
- Khuyến nghị cho người dùng macOS hiện tại: chỉ mở PDF từ nguồn tin cậy.
