# Nhật ký thay đổi

Theo [Keep a Changelog](https://keepachangelog.com/vi/1.1.0/) và [SemVer](https://semver.org/lang/vi/).
Các mốc v1.0, v1.5, v2.0 trong kế hoạch được phát hành chung trong 2.0.0.

## [2.1.3] - 2026-11-20

Bản sửa L1-001 phần **Windows** (`prompt_windows_macos_vscode.txt`), làm và tự kiểm trên máy Windows 10 thật của chủ
dự án. Trạng thái là của BÊN SỬA; kết luận thuộc về kiểm toán viên. Bằng chứng: `tu_kiem_win_mac.md`.

### Bảo mật
- **L1-001 (Windows) — Đã làm (tự kiểm: PoC thất bại)**: tiến trình con xử lý PDF (`_child_entry`, `_render_server`,
  lần quét chốt cuối của `publish_staged`) chạy trong **AppContainer** `VietPDF.Sandbox`, **không có capability mạng
  nào** (không internetClient / internetClientServer / privateNetworkClientServer). Tiến trình được tạo **SUSPENDED**
  (`CreateProcessW` + `STARTUPINFOEX`: `SECURITY_CAPABILITIES`, `HANDLE_LIST` chỉ gồm handle pipe khởi động), gán
  **Job Object** (`KILL_ON_JOB_CLOSE`, `ACTIVE_PROCESS_LIMIT=1`, giới hạn bộ nhớ) rồi mới chạy — 2.1.2 gán Job Object
  sau khi tiến trình đã chạy. multiprocessing `spawn` giữ nguyên giao thức, nhưng tiến trình con không phải mở tiến trình
  cha: handle Pipe được tiến trình cha nhân bản thẳng vào tiến trình con.
- ACL: thư viện Python/app được cấp quyền ĐỌC cho SID của AppContainer (giữ lại, chỉ là mã chương trình; không bao giờ
  cấp cho ổ gốc, HOME, %APPDATA% hay thư mục cha của chúng). File PDF đầu vào (ĐỌC) và thư mục tạm của job (GHI) được cấp
  cho một **capability SID ngẫu nhiên, mới cho từng tiến trình con**, và **gỡ ngay** khi tiến trình con dừng.
- Báo mức cô lập như Linux: tiến trình con tự đọc token + Job Object của nó và gửi
  `{"platform": "win32", "appcontainer": true, "capabilities": [], "job_object": true, "errors": []}` trước khi đụng dữ
  liệu; tiến trình cha **tự đọc token của tiến trình con** để đối chiếu. `evaluate_confine`: thiếu AppContainer, có
  capability thừa, thiếu Job Object, hay lỗi dựng ACL ⇒ vấn đề, **chặn** (fail-closed). `--allow-weak-sandbox` / hộp
  thoại GUI giữ nguyên. Lỗi khi dựng AppContainer bị từ chối TRƯỚC khi khởi động tiến trình con.
- `WEAK_PLATFORM_POLICY` thành theo nền tảng: `win32` → `"block"`; `darwin` vẫn `"warn"` (lựa chọn tạm thời (b)) vì
  sandbox macOS **Chưa làm** — chỉ được viết và kiểm trên máy Mac (quy tắc 1 của prompt).
- Windows: `publish_staged` không chuyển nguyên file do tiến trình con tạo (mang DACL mặc định của AppContainer, nhãn
  Low Integrity, có thể kèm luồng dữ liệu phụ ADS) mà chép nội dung chính sang file mới do tiến trình chính tạo.

### Sửa lỗi
- Windows: `_pid_alive` (dọn thư mục tạm của job lúc khởi động) dùng `os.kill(pid, 0)` — trên Windows đó là
  `CTRL_C_EVENT`, gửi Ctrl+C cho cả nhóm tiến trình console (kể cả chính app) và không bao giờ báo tiến trình đã chết, nên
  thư mục tạm sót lại không được dọn. Dùng `psutil.pid_exists`.
- Windows: CLI in tiếng Việt ra pipe/file (stdout/stderr dùng code page ANSI, vd. cp1252) thoát với
  `UnicodeEncodeError` (có từ 2.1.2, vd. `vietpdf verify-assets | more`). `cli.main` ghi UTF-8 khi luồng không phải
  UTF-8; console Windows không đổi.
- `_wait_message`: tiến trình con chết mà chưa gửi gì ⇒ báo `SandboxCrashedError` ngay, không gọi `recv` (trước đây có
  thể chờ mãi nếu còn handle ghi của pipe ở đâu đó).
- Test chạy được trên Windows: đọc/ghi file và stdout tiến trình con bằng UTF-8 (trước đây phụ thuộc code page, hỏng với
  cp1252); test keyring chấp nhận kho mật khẩu của OS trên Windows/macOS, vẫn kiểm backend độc hại không được nạp.

### Test mới
- `tests/unit/test_isolation_win_213.py` (chỉ chạy trên Windows 10 1607+): PoC trong tiến trình con THẬT — đọc canary
  trong HOME thật, ghi thư mục Startup thật, TCP tới listener 127.0.0.1 do test dựng, DNS, `subprocess` đều bị từ
  chối; ghi thư mục tạm của job được phép; đối chứng ở tiến trình cha (cùng thao tác thì thành công); ACL của job bị gỡ
  sau job; tiến trình dựng PDF bị chiếm quyền không chạm được HOME, `report.md` ghi "ĐẠT"; render GUI trong AppContainer;
  ADS/ACL của tiến trình con không theo file đích; lỗi dựng AppContainer ⇒ từ chối, func không chạy, CLI mã 5,
  `--allow-weak-sandbox` chạy và báo YẾU.

## [2.1.2] - 2026-11-11

Bản vá theo yêu cầu sửa của kiểm toán lượt 1R (`yeu_cau_sua_2.1.2.md`). Trạng thái dưới đây là của BÊN SỬA (tự kiểm);
kết luận "đã sửa" thuộc về kiểm toán viên ở lượt kiểm lại. **Nên cập nhật ngay** (R1-001, R1-002 mức Cao).

### Bảo mật — P0
- **R1-001** (Cao): tiến trình con chỉ GHI được vào **thư mục tạm riêng của job** (`.vietpdf-stage-*`, `mkdtemp` 0700,
  tạo cạnh đích; không tạo được thì trong thư mục cache rồi sao chép). Bất biến trong `run_isolated`: `write_dirs`
  không phải thư mục tạm của job ⇒ `SandboxError` (không tắt được). Tiến trình chính công bố file bằng
  `publish_staged`: mở `O_NOFOLLOW|O_NONBLOCK`, chỉ nhận file thường, 1 liên kết, đúng chủ, ≤ 2 GiB; quét nội dung chủ
  động trong một **tiến trình con MỚI chỉ có quyền đọc** file đó; `fchmod 0600`; kiểm lại không bị thay; `os.replace`
  (khác filesystem: sao chép an toàn). Áp cho dựng PDF, bản song ngữ, bản học, dựng lại từ IR, bản tô đọc lướt
  (`assist skim`, CLI + GUI), bản học từ thư viện (GUI) — `grep write_dirs= src/` chỉ còn trong `core/sandbox.py`.
  Khởi động CLI/GUI dọn thư mục tạm do lần chạy bị kill để lại (sổ đăng ký trong thư mục cache).
- **R1-002** (Cao): cô lập **fail-closed**. `confine_child_linux` ghi lý do mọi bước thất bại (`errors`, log
  WARNING). Tiến trình con gửi mức cô lập về cha TRƯỚC khi chạy `func` rồi chờ "go"; tiến trình cha so với chính sách
  (NO_NEW_PRIVS, seccomp, lọc tín hiệu, PDEATHSIG, Landlock ABI ≥ 1) và với khả năng kernel tự đo (`probe_kernel`);
  không đạt ⇒ không gửi "go", kill, `SandboxWeakError` (CLI mã thoát 5). Ngoại lệ chỉ do người dùng chọn: CLI
  `--allow-weak-sandbox`, GUI hộp thoại một lần mỗi phiên (không lưu vĩnh viễn) — **không** đọc từ biến môi trường.
  seccomp chặn tín hiệu ra ngoài kể cả khi Landlock ABI < 6: `kill`/`tgkill`/`rt_(tg)sigqueueinfo` chỉ cho khi đích
  là chính tiến trình con; cấm `tkill`, `pidfd_open`, `pidfd_send_signal`. Tiến trình render thường trực của GUI theo
  cùng chính sách. Mức cô lập hiện ở CLI (`-v`; cô lập yếu thì luôn cảnh báo), GUI (thanh trạng thái) và `report.md`.

### Bảo mật — P1
- **L1-010 + R1-005**: chữ ẩn phát hiện bằng **so ảnh render** (trang đầy đủ vs trang đã xoá riêng chữ), theo từng ký
  tự; ngưỡng cỡ theo chiều cao glyph trong không gian thiết bị (< 3 pt). Ba mức: nhìn thấy (dịch), mờ (dịch + cờ
  `security:low-contrast`), ẩn (không gửi đi dịch theo mặc định, **liệt kê** trong `report.md` và GUI; `--translate-hidden`
  / ô GUI để dịch cả phần này). Dòng bị che một phần ⇒ `security:partly-hidden`; chữ khác nhau chồng đúng lên nhau ⇒
  `security:overlapping-text`; lớp chữ OCR vô hình trên trang ảnh scan được dùng lại (`ocr-layer`, liệt kê theo trang).
  `injection_like` so trên văn bản đã chuẩn hoá (NFKC, casefold, đồng hình → Latin, bỏ dấu, bỏ Cf/chữ trống) + dạng
  dính liền + cửa sổ trượt 2–3 đoạn; thêm mẫu gọi thẳng mô hình/"hệ thống dịch" và động từ né. Mọi đoạn có cờ
  `security:` được dịch RIÊNG từng đoạn, không ngữ cảnh/thuật ngữ/đoạn trước, không làm ngữ cảnh cho đoạn khác, không
  vào bước tóm tắt/thuật ngữ, không vào lượt dịch lại/biên tập/dịch gọn; bản dịch được kiểm (`security:output-length`,
  `-new-link`, `-language`).
- **R1-004**: khi dựng PDF, xoá annotation ngoài allowlist (Link, Text, Highlight, Underline, StrikeOut, Squiggly,
  Square, Circle, Line, Ink, FreeText, Popup; trường biểu mẫu thành nội dung tĩnh), xoá `/A`, `/AA` của annotation
  được giữ (trừ Link), xoá khoá thừa (`/JS`...) trong hành động an toàn; `report.md` ghi số đối tượng đã xoá theo loại.
  Chốt kiểm cuối thất bại ⇒ làm sạch lại MỘT lần (chế độ nghiêm) rồi quét lại, chỉ từ chối nếu vẫn còn — không dịch
  lại, không gọi API.
- **L1-024**: `build-constraints.txt` ghim hatchling và phụ thuộc của nó **có hash**; CI/release cài dự án bằng
  `uv sync --no-install-project` + `uv pip install --require-hashes -r build-constraints.txt` + `--no-build-isolation`,
  `UV_NO_SYNC=1`; CI build wheel với `--build-constraints ... --require-hashes` (hash lệch ⇒ thất bại);
  `scripts/check_workflows.py` kiểm các điều này. CODEOWNERS thêm `core/confine.py`, `build-constraints.txt` — **người
  duyệt thật: CHƯA làm, cần chủ dự án cung cấp** (preflight vẫn chặn phát hành). Environment `release`: hướng dẫn +
  lệnh `gh api` trong `docs/PHAT_HANH.md` (chủ dự án tự làm).

### Bảo mật — P2
- **R1-003**: bỏ `--password` (lộ qua `ps`); thêm `--password-stdin` (giữ `--ask-password`, `VIETPDF_PDF_PASSWORD`).
- **R1-006**: `settings.json` sai ⇒ `ConfigError` kèm đường dẫn trường (CLI mã thoát 6, GUI hộp thoại), **không bao giờ
  ghi đè** file hỏng; lưu đồng ý chỉ sửa khoá `privacy_consents`, ghi nguyên tử, giữ `settings.json.bak`; trường bool
  dùng chế độ strict.
- **R1-007**: trần kết quả từ tiến trình con theo loại việc (báo cáo dựng PDF 4 MB, quét 1 MB, trích xuất 8 MB +
  6 MB/trang, tối đa 512 MB); `json.loads` trên bytes (bớt một bản sao).
- **R1-008** (một phần): seccomp chặn thêm `sendmsg`, `sendmmsg`, `sendto` có địa chỉ đích, `unshare`, `setns`,
  `keyctl`, `add_key`, `request_key`, `memfd_create`, `personality`. CHƯA: danh sách cho phép đầy đủ; Landlock vẫn cho
  đọc `/etc`, `/proc/self/net`. "Ghi đè đầu ra job khác cùng thư mục" được xử lý bởi R1-001.
- **R1-009**: hộp thoại đồng ý hiện host IDN kèm punycode; cảnh báo khi nhãn trộn nhiều hệ chữ.
- **R1-010**: `library migrate` liệt kê bản ghi chữ thường và cần xác nhận (`--yes`), mặc định không mã hoá lại; dòng
  cache có `created` NULL coi là hết hạn.
- **R1-011**: `scripts/fetch_models.py` tải vào file tạm và kiểm hash TRƯỚC khi thay file đích.
- **L1-012**: `report.md` liệt kê trang/đoạn bị bỏ vì dữ liệu không hợp lệ.
- **L1-015**: `sanitize_output` bỏ thêm ký tự "chữ trống" (U+3164, U+115F, U+1160, U+FFA0, U+2800, U+034F, VS1–VS256...).
- **L1-016**: tài liệu ghi rõ ai ghi được `models.lock.json` trong thư mục cấu hình thì chọn được mô hình; sau R1-001 tiến
  trình con không còn ghi được thư mục cấu hình.
- **L1-023**: `security_audit.py` bắt `sys.modules["os"].system(...)` (dạng Subscript) và lệnh `subprocess.*` gọi qua đó.
- **L1-025** (một phần): test hành vi chạy tiến trình con thật cho R1-001, R1-002, R1-004, L1-010/R1-005 và P2
  (`test_isolation_212.py`, `test_active_content_212.py`, `test_hidden_injection_212.py`, `test_p2_212.py`). CHƯA đo
  coverage trong tiến trình con (cần thêm `coverage` vào nhóm dev và quyền ghi thư mục coverage chỉ trong chế độ test).

### Windows/macOS (L1-001)
- Quyết định của chủ dự án (11/11/2026): làm lớp cô lập mạnh (AppContainer / sandbox-exec) và kiểm trên máy thật của chủ
  dự án. **Chưa làm trong 2.1.2 — phiên sửa này không có máy Windows/macOS**; không viết mã chưa chạy được. Tạm thời
  dùng lựa chọn (b): cho chạy, **cảnh báo thường trực** "cô lập YẾU" (GUI, CLI, report.md) — xem `docs/BAO_MAT.md`.

### Hiệu năng
- Thêm một tiến trình con quét chỉ-đọc mỗi file đầu ra; bù lại bằng cách gộp `doc_info` với trích xuất cho tài liệu
  ≤ 20 trang (một tiến trình con thay vì hai). Số đo: xem báo cáo tự kiểm.

### Tài liệu
- Đính chính các câu nói khác thực tế: CHANGELOG 2.1.1 ("HOME không truy cập được", chữ ẩn, từ chối đầu ra),
  `docs/BAO_MAT.md` (ranh giới PDF → app, `CONFINE_STATUS`, prompt injection, PDF đầu ra, rủi ro còn lại),
  `docs/KIEM_TRA_BAO_MAT_2.1.md` mục 5 ("Tất cả đã được sửa"), docstring đầu `core/confine.py`; `_span_hidden` được
  thay bằng `_Visibility` (so ảnh render) với chú thích mô tả đúng phạm vi.

## [2.1.1] - 2026-11-03

Bản vá bảo mật theo kiểm toán độc lập lượt 1 (26 phát hiện L1-001 … L1-026). **Nên cập nhật ngay.**

### Bảo mật
- **Cô lập tiến trình con ở mức hệ điều hành** (`core/confine.py`, L1-001/L1-003/L1-013): Linux — seccomp (không
  socket mạng, không `connect`, không exec/fork, không ptrace), Landlock (chỉ đọc file đầu vào + thư viện, chỉ ghi
  thư mục đầu ra), `PR_SET_PDEATHSIG` (chết theo app kể cả khi app bị `kill -9`);
  **[Đính chính ở 2.1.2]** câu "HOME không truy cập được" trước đây là SAI: thư mục đầu ra mặc định là thư mục của PDF
  gốc, nên khi PDF nằm trong HOME, tiến trình dựng PDF đọc/ghi/xoá được cả HOME (kiểm toán R1-001); khi kernel không
  có Landlock, cô lập file mất hẳn mà không báo (R1-002). Cả hai được sửa ở 2.1.2;
  Windows — Job Object (chết theo app, không sinh tiến trình, giới hạn bộ nhớ). Môi trường tiến trình con tối giản.
  Tiến trình chính đặt `PR_SET_DUMPABLE=0` (không đọc được `/proc/<pid>/environ|mem`).
- **Khoá API**: không bao giờ gửi khoá của nhà cung cấp tới Base URL tuỳ chỉnh — dùng khoá riêng
  `vietpdf key set custom` (L1-002). Mọi `VIETPDF_*_API_KEY` và `VIETPDF_PDF_PASSWORD` được gỡ khỏi môi trường ngay
  khi khởi động (L1-003).
- **Đồng ý trước khi gửi**: lần đầu gửi tài liệu tới mỗi nhà cung cấp/host không phải máy cục bộ, app hỏi (GUI) hoặc
  yêu cầu `--accept-privacy` (CLI); lựa chọn lưu theo `provider@host` (L1-008).
- **Mạng**: bỏ qua `HTTP(S)_PROXY`, `SSL_CERT_FILE`, `.netrc` của môi trường; proxy chỉ cấu hình tường minh bằng
  `https_proxy` trong settings.json và không bao giờ áp cho loopback/offline (L1-004).
- **onnxruntime < 1.29** (bản ≥ 1.29 trên PyPI nhúng telemetry OneDS gọi `mobile.events.data.microsoft.com`, không
  tắt được bằng API) + chặn mạng ở mức OS cho tiến trình OCR (L1-007).
- **Làm sạch PDF đầu ra** viết lại theo allowlist hành động (GoTo/Named/URI http(s)/mailto) ở MỌI vị trí, kể cả
  inline, chuỗi `/Next`, mục lục, Widget, Screen; xoá XFA; PDF đầu ra được quét lại và **từ chối ghi** nếu còn nội
  dung chủ động (L1-006). **[Đính chính ở 2.1.2: việc từ chối này làm mất cả PDF đầu ra hợp lệ có annotation
  Sound/Movie hoặc GoTo kèm `/JS` vô hại — R1-004]**.
- **Mã hoá dữ liệu cục bộ fail-closed**: kho mật khẩu lỗi tạm hoặc khoá hỏng ⇒ dừng với lỗi rõ ràng (không ghi dạng
  rõ, không tạo khoá mới đè khoá cũ); bản ghi chữ thường bị từ chối khi đã bật mã hoá — `vietpdf library migrate`
  để chuyển dữ liệu cũ (L1-005). Backend keyring khởi tạo trực tiếp theo nền tảng, không đọc `keyringrc.cfg` hay
  `PYTHON_KEYRING_BACKEND` (L1-022).
- **Prompt injection**: chữ không nhìn thấy (trong suốt, < 1pt, ngoài trang, cùng màu nền) bị bỏ, không dịch, có ghi
  trong báo cáo **[Đính chính ở 2.1.2: chỉ bắt được một số dạng — chữ bị hình/ảnh phủ, đen trên đen, 1,1–1,2 pt, độ
  mờ 2 % vẫn lọt; ngược lại chữ trắng trên nền tối sát chữ bị bỏ nhầm (L1-010, R1-005)]**; tiêu đề/mục lục/tóm tắt trông như chỉ dẫn cho AI không được đưa vào ngữ cảnh; thêm mẫu tiếng Việt
  (L1-010).
- PDF đầu ra, file xuất (CSV/Markdown/BibTeX/Anki) quyền 0600; cache tự xoá mục quá 180 ngày (L1-011).
- Một đoạn/trang có dữ liệu vô lý chỉ bị bỏ riêng, không làm hỏng cả tài liệu (L1-012); JSON lồng quá sâu từ LLM là
  lỗi thường (L1-014); `sanitize_output` bỏ mọi ký tự định dạng Unicode (Cf/Cc/Co/Cn) (L1-015); CSV chống `＝` toàn
  chiều rộng và ký tự vô hình đầu ô (L1-017); tên file được thoát trong ghi chú Markdown (L1-018); `protect()` tuyến
  tính và không va chạm với `⟦F1⟧` có sẵn (L1-019); cờ IR theo allowlist, tooltip GUI thoát HTML (L1-021).
- Toàn vẹn: mô hình ONNX chỉ tin hash trong `models.lock.json` (gói hoặc thư mục cấu hình), không tin file
  `.sha256` cạnh mô hình (L1-016); mã băm của `SHA256SUMS` ghim trong mã nguồn, file ẩn bị phát hiện (L1-020).
- Huỷ (Ctrl+C, SIGTERM, đóng cửa sổ) kill ngay mọi tiến trình con và không khởi động tiến trình mới (L1-013).

### Chuỗi cung ứng / quy trình
- `release.yml` sửa lỗi cú pháp YAML (workflow 2.1.0 không bao giờ chạy được — L1-009); job `preflight` chặn phát
  hành khi CODEOWNERS còn nhóm giữ chỗ; job `publish` dùng environment `release` (cần cấu hình người duyệt).
- CI kiểm cú pháp/chính sách workflow (`scripts/check_workflows.py`) và quét cả `tests/` bằng `security_audit.py`;
  `security_audit.py` bắt alias (`import os as o`, `from os import system`), `posix_spawn*`, `_socket`, asyncio
  subprocess (L1-023).
- `uv` `exclude-newer = "7 days"` cho MỌI lần lock; cận trên cho mọi phụ thuộc; build backend ghim
  `hatchling==1.32.0`; `.gitignore` không còn che `security/secrets.py` (L1-024).
- `fetch_models.py`/`download_to` ghi file tạm rồi đổi tên nguyên tử (L1-026).

## [2.1.0] - 2026-11-02

### Thêm — học qua bản dịch
- **Bôi đen để thêm từ vựng**: nút "Bôi đen chữ" (hoặc giữ Shift) trong trình xem, kéo chuột quanh từ/cụm trên
  trang gốc hoặc trang dịch → *Thêm vào từ vựng ôn tập / Tra nghĩa & xem ngữ cảnh / Sao chép*. Chữ được đọc trong
  tiến trình con render (`PageRenderer.text_in_rect`).
- Bảng **Đoạn đang chọn**: câu gốc tiếng Anh + bản dịch + thuật ngữ của đoạn; bôi đen ngay trong đó để thêm từ.
- **Sổ từ vựng** tự điền nghĩa (glossary của bạn → thuật ngữ của bài → glossary chuyên ngành → định nghĩa trong
  bài; nhận số nhiều), câu ví dụ trong bài, câu dịch tương ứng, số trang; nút **Hỏi AI nghĩa theo ngữ cảnh**.
- **Ôn tập lặp lại ngắt quãng (SM-2)**: thẻ Anh→Việt, Việt→Anh, điền từ vào câu gốc; gõ đáp án (bỏ dấu);
  Quên/Khó/Nhớ/Dễ; bảng từ sửa trực tiếp; xuất Anki/CSV/Markdown, nhập CSV. CLI `vocab review` trong terminal.
- **Thư viện tài liệu**: tự lưu sau khi dịch (`--no-library` để tắt); mở lại đúng trang đang đọc; ghi chú; lịch
  sử hỏi đáp của trợ lý; bản học; xoá. Tab Thư viện thay tab Lịch sử. CLI `library list/show/add/notes/history/remove`.
- **Bản học song ngữ** `*.study.pdf` (`--study` hoặc `vietpdf study`): mỗi đoạn câu gốc EN → bản dịch VI → thuật ngữ
  của đoạn; phụ lục thuật ngữ và sổ từ.
- `--gloss document|paragraph|off`: ghi tiếng Anh trong ngoặc lần đầu trong tài liệu / trong mỗi đoạn (để học).
- Kho học tập SQLite mã hoá từng dòng AES-256-GCM (`library/store.py`, `security/crypto.py`); sổ từ 2.0 tự chuyển.

### Thay đổi — chất lượng dịch cho người học
- Chỉ dẫn dịch phiên bản 4: giữ sắc thái chắc chắn (may/likely/must/should/suggest), lượng từ (few/some/most),
  điều kiện; không để sót tiếng Anh; thêm ví dụ sai sắc thái/sai lượng từ.
- Kiểm tra sau dịch mới: `qa:modality`, `qa:untranslated`; đoạn bị cờ (và đoạn thiếu thuật ngữ) được dịch lại kèm
  ghi chú lỗi cụ thể.
- Chia bản dịch của đoạn chảy qua cột/trang theo ranh giới câu.
- Đầu ra của bộ dịch giả `fake` (chỉ để kiểm thử) bị đóng dấu đỏ "BẢN DỊCH GIẢ ĐỂ KIỂM THỬ" trên mọi trang và bản
  học; IR ghi tên nhà cung cấp (`provider`); CLI và trình xem cảnh báo. Hướng dẫn chọn mô hình theo mục đích.

### Bảo mật (chi tiết: docs/KIEM_TRA_BAO_MAT_2.1.md)
- Bảng thuật ngữ/thuật ngữ/lĩnh vực không còn nằm trong chỉ dẫn hệ thống (chống prompt injection từ PDF).
- Hộp thoại GUI luôn ở chế độ chữ thuần (`gui/safe.py`); Markdown xuất ra được thoát ký tự (`md_escape`).
- Ghi file nguyên tử bằng file tạm an toàn (chống symlink); gỡ link `javascript:/file:/data:`/Launch/GoToR khỏi
  PDF đầu ra; kiểm tra DOI trước khi ghép URL; `render` không ghi đè file gốc; `--ask-password`.
- Sửa 2 lỗi ReDoS (tới 27 giây → < 0,1 giây) và gộp thuật ngữ O(n²) → O(n); giới hạn số dòng stream.
- Kiểm tra cập nhật tắt cho tới khi cấu hình kho chính thức (tránh trỏ tới kho giả mạo).
- Xuất Anki `#html:false`; CSV chống công thức; mở file bằng ứng dụng ngoài chỉ với `.pdf` của thư viện.
- Test mới: `test_security_review.py` (gồm fuzz 12 PDF đột biến), `test_learning.py`, `test_gui_learning.py`.

## [2.0.0] - 2026-11-02

### Thêm — v1.0: PDF khó
- Trích xuất mức ký tự (`rawdict`); công thức trong dòng (font toán, Unicode toán, chỉ số) giữ nguyên glyph và nét
  vẽ, dời nguyên khối bằng `show_pdf_page`; gộp tử/mẫu số bị tách dòng; gắn nét vẽ vào công thức.
- Bảng (`find_tables`) và biểu đồ (ảnh + cụm nét vẽ, kể cả nhãn trục) giữ nguyên; chỉ dịch chú thích.
- Thẻ đậm/nghiêng ⟦B⟧/⟦I⟧, thứ tự đọc XY-cut, đoạn nối qua cột/trang, đầu/chân trang.
- OCR trang scan bằng RapidOCR (extra `[ocr]`), che chữ bằng màu nền giấy; trang font hỏng ⇒ OCR.
- Mô hình bố cục DocLayout-YOLO ONNX tuỳ chọn (extra `[layout]`), bắt buộc ghim SHA-256; `scripts/fetch_models.py`.
- Dàn chữ nhiều kiểu chữ + công thức, font dự phòng cho glyph thiếu; IR v2 (tự nâng từ v1).

### Thêm — v1.0: thuật ngữ & dịch theo nghĩa
- Kho thuật ngữ 3 tầng; glossary chuyên ngành: Toán–Thống kê, Kinh tế, Sinh–Y, Vật lý–Hoá, Pháp lý (+ CNTT–AI).
- Nhận diện lĩnh vực + tóm tắt; trích thuật ngữ trên máy + đề xuất bằng LLM; duyệt thuật ngữ (CLI `terms`, GUI).
- Ép thuật ngữ nghiêm ngặt (⟦T⟧), kèm gốc lần đầu, kiểm tra sau dịch + dịch lại có nhắc, báo cáo nhất quán.
- Biên tập lượt 2 (`--quality`, `--editor-provider`), văn phong hành chính–pháp lý.
- Ghi chú thuật ngữ trong PDF, phụ lục chú giải; báo cáo `*.report.md/json`.
- `vietpdf estimate` (token/request/chi phí), `vietpdf render` (dựng lại từ IR sửa tay).

### Thêm — v1.5: trợ lý đọc hiểu
- Thẻ tổng quan 5C, tô màu đọc lướt, định nghĩa tại chỗ, giải thích công thức/bảng/đoạn, kiến thức nền, phân tích
  câu dài, hỏi đáp BM25 có trích dẫn đã xác minh, phản biện, tái hiện, so sánh nhiều bài.
- Thẻ trích dẫn (tách danh mục, ánh xạ [n], tra Semantic Scholar/OpenAlex tuỳ chọn), ghi chú Markdown + BibTeX,
  sổ từ + xuất Anki, Zotero 7 local API.

### Thêm — v2.0
- Ngôn ngữ nguồn khác (`--source-lang`); nhà cung cấp Claude, DeepSeek, OpenRouter, Groq với chế độ JSON riêng và
  tự lùi `json_schema → json_object`; tóm tắt từng chương làm ngữ cảnh; tuỳ chọn dịch cả ô bảng.

### Thêm — GUI
- 5 tab: Dịch (tuỳ chọn đầy đủ, ước tính chi phí, duyệt thuật ngữ), Xem & sửa (song song, đồng bộ, sửa tay, xuất
  lại), Trợ lý đọc, Thuật ngữ, Lịch sử; giao diện sáng/tối, Việt/Anh.

### Bảo mật
- Khoá mạng trong tiến trình con; giám sát bộ nhớ bằng psutil trên mọi HĐH; nuốt stdout tiến trình con.
- Trình xem GUI render trang trong tiến trình con thường trực (GUI không mở PDF).
- Cache mã hoá AES-256-GCM (khoá trong keyring).
- Phát hiện đoạn PDF giống prompt injection; mọi tác vụ JSON giữ dữ liệu tài liệu ngoài chỉ dẫn hệ thống.
- Audit: luật mới chặn URL tới host lạ; ngoại lệ có ghi lý do. Kiểm tra cập nhật chỉ thông báo.
- Xuất BibTeX/CSV/TSV chống chèn cú pháp.

### Sửa
- Callback từ luồng nền chạy trong luồng giao diện (lỗi có từ 0.1.0); cache SQLite dùng được đa luồng.
- Cắt từ dài (URL) trong bộ dàn chữ; link GitHub dính dấu chấm cuối câu; tách tên bài tham khảo nhầm ở chữ viết tắt
  tên tác giả.

### Phụ thuộc
- Thêm: `psutil`, `cryptography`. Extras: `ocr` (rapidocr, onnxruntime), `layout` (onnxruntime, numpy).

## [0.1.0] - 2026-11-02

- MVP: dịch PDF dạng chữ giữ vị trí khối; 3 nhà cung cấp; cache; glossary CSV; kiểm tra nghĩa; mục lục, song ngữ;
  CLI + GUI tối giản; lớp bảo mật, audit chống mã độc, CI/CodeQL/Scorecard/release có attestation.
