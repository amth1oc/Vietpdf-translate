# Đánh giá kế hoạch "VietPDF Translate"

Tài liệu này đánh giá bản kế hoạch ngày 02/11/2026 trên ba tiêu chí: **bảo mật / chống mã độc**, **khả năng mở rộng**, **cấu trúc rõ ràng**. Mỗi vấn đề kèm cách đã xử lý trong mã nguồn của bản MVP này.

## 1. Tổng quan

| Tiêu chí | Đánh giá | Nhận xét ngắn |
| --- | --- | --- |
| Tầm nhìn sản phẩm | Tốt | Đúng nhu cầu thật (sinh viên, nghiên cứu viên), có tiêu chí thành công đo được |
| Kiến trúc | Tốt | 4 lớp, IR ở giữa, lõi không phụ thuộc Qt, adapter cho nhà cung cấp: đúng hướng để mở rộng |
| Bảo mật | Khá, còn lỗ hổng | Ý tưởng đúng (keyring, allowlist, không tải code lúc chạy) nhưng thiếu cơ chế ép buộc và có vài điểm sai cụ thể (mục 3) |
| Phạm vi / tiến độ | Rủi ro cao | 30 tuần cho một người, gồm cả bộ dàn chữ tự viết ngang BabelDOC, OCR, trợ lý đọc: lạc quan |
| Tính nhất quán | Cần sửa | Có 5 chỗ mâu thuẫn giữa các phần (mục 2) |

**Kết luận:** kế hoạch khả thi nếu cắt theo đúng thứ tự "lõi dựng PDF → lớp dịch → bảo mật → GUI". Mã nguồn đi kèm hiện thực MVP theo thứ tự đó và sửa các lỗ hổng nêu dưới đây ngay trong code, không để đến giai đoạn 6.

## 2. Mâu thuẫn trong kế hoạch (cần chốt)

| # | Chỗ mâu thuẫn | Đề xuất |
| --- | --- | --- |
| 1 | Bảng công nghệ ghi OCR = **Tesseract**, các phần khác ghi **RapidOCR** | Chốt RapidOCR (ONNX, không cần cài thêm phần mềm ngoài) |
| 2 | Bảng tính năng: PDF scan ở **v1.0**; Pipeline bước 1: "chuyển sang OCR ở **v2.0**" | Chốt v1.0. MVP phát hiện trang scan và **bỏ qua có báo cáo**, không dịch rác |
| 3 | Thứ tự cho vừa khung: phần Tiếng Việt "co chữ → nới khung"; phần BabelDOC "nới khung → co chữ" | Chốt **giảm dãn dòng → nới khung → co chữ → xin dịch gọn → đánh dấu** (ưu tiên giữ cỡ chữ dễ đọc) |
| 4 | "Không có kết nối mạng nào ngoài API dịch" nhưng có Semantic Scholar, OpenAlex, Hugging Face, kiểm tra cập nhật | Ghi rõ: tất cả là **tùy chọn, mặc định tắt**, chỉ thêm host vào allowlist khi bật |
| 5 | "Dưới 15 phụ thuộc trực tiếp" nhưng danh sách có scikit-learn (chỉ để dùng DBSCAN), scikit-image (chỉ để dùng SSIM), openai SDK, genanki… | Khi cần, tự viết DBSCAN 1 chiều và SSIM bằng numpy (vài chục dòng) thay vì kéo cả scikit-learn/scikit-image; bỏ openai SDK (xem 3.1). **Đã áp dụng:** MVP chỉ có 4 phụ thuộc chạy: `pymupdf`, `httpx`, `pydantic`, `keyring` |

## 3. Lỗ hổng bảo mật và cách sửa

### 3.1 Allowlist host có thể bị vượt qua
Kế hoạch dùng cả `openai` SDK và `httpx`. SDK `openai` tự tạo HTTP client riêng, nên nếu quên truyền `http_client` thì request **không đi qua bộ lọc host**. Ngoài ra kế hoạch không nói tới **redirect** (một host được phép có thể trả 302 sang host khác) và không bắt buộc HTTPS.

**Đã làm:** bỏ hẳn `openai` SDK. Toàn bộ mạng đi qua `security/http.py::SafeHttpClient`:
- kiểm tra host ở mọi request (event hook), **tắt follow redirect**;
- bắt buộc `https://` cho mọi host không phải loopback, luôn bật xác minh TLS;
- giới hạn kích thước phản hồi (chống trả về hàng GB làm tràn RAM);
- chế độ `offline` chỉ cho `localhost/127.0.0.1/::1`.

Script `scripts/security_audit.py` chạy trong CI **cấm import** `httpx`, `socket`, `urllib.request`, `requests`, `http.client` ở bất kỳ file nào khác ngoài `security/http.py`. Đây là cơ chế ép buộc, không chỉ là quy ước.

### 3.2 `keyring` có thể âm thầm lưu key dạng chữ thường
Trên Linux không có Secret Service (máy chủ, WSL, môi trường tối giản), `keyring` có thể rơi về backend `fail` hoặc gói `keyrings.alt` lưu **plaintext** trong file.
**Đã làm:** `security/secrets.py` từ chối các backend không an toàn (`fail`, `null`, `PlaintextKeyring`, `EncryptedKeyring` của keyrings.alt…) và báo lỗi rõ ràng; khi đó chỉ dùng biến môi trường `VIETPDF_<PROVIDER>_API_KEY` (không bao giờ ghi key vào file cấu hình).

### 3.3 "Tải mô hình" bằng cách gọi lệnh `ollama pull`
Gọi tiến trình ngoài với tên mô hình do người dùng nhập là bề mặt tấn công **command injection** và phụ thuộc PATH (có thể bị thay bằng file thực thi giả).
**Đã làm (thiết kế):** dùng HTTP API của Ollama (`POST /api/pull`) qua `SafeHttpClient`, kiểm tra tên mô hình bằng regex `^[a-zA-Z0-9._:/-]{1,128}$`. Audit script cấm `subprocess`, `os.system`, `os.popen` trong toàn bộ `src/`.

### 3.4 File PDF độc hại
Kế hoạch có ý "phân tích PDF trong tiến trình con" — đúng hướng. Bổ sung:
- **Spawn** (không fork) tiến trình con: tiến trình phân tích PDF **không thừa hưởng bộ nhớ chứa API key** của tiến trình chính. Nếu MuPDF có lỗ hổng, kẻ tấn công không đọc được key trong RAM.
- Giới hạn: kích thước file, số trang, thời gian (kill khi quá hạn), bộ nhớ (`RLIMIT_AS` trên Linux; Windows/macOS chỉ có timeout — ghi rõ trong tài liệu).
- Từ chối file mã hóa; kiểm tra chữ ký `%PDF-` ở đầu file trước khi mở.
- File xuất: xóa JavaScript, file đính kèm, form response (`doc.scrub`) nhưng giữ link.

### 3.5 Kết quả LLM là dữ liệu không tin cậy
Kế hoạch xử lý prompt injection ở đầu vào nhưng chưa nói tới **đầu ra**:
- Kết quả phải khớp schema, đủ `id`, không có `id` lạ, giữ đủ ký hiệu giữ chỗ, giới hạn độ dài (≤ 4× bản gốc + 200 ký tự), bỏ ký tự điều khiển.
- Ở GUI (sau này), nội dung từ LLM phải hiển thị dạng **plain text** — `QTextBrowser`/`QLabel` mặc định hiểu HTML và có thể tải tài nguyên ngoài. MVP GUI dùng `setPlainText`/`Qt.PlainText`.

### 3.6 Mô hình và font đóng gói
Kế hoạch có checksum SHA-256 nhưng không nói **khi nào kiểm**. **Đã làm:** `security/integrity.py` kiểm tra mọi asset với `SHA256SUMS` **trước khi dùng**, sai thì dừng (fail-closed). Không bao giờ dùng `pickle`, `torch.load`, `joblib` để nạp mô hình (audit script cấm).

### 3.7 Cache chứa nội dung tài liệu mật
Cache SQLite lưu bản dịch dạng chữ thường. **Đã làm:** file cache tạo với quyền `0600`, nằm trong thư mục dữ liệu người dùng, có lệnh `vietpdf cache clear` và cờ `--no-cache`. Khuyến nghị v1.0: tùy chọn mã hóa cache bằng khóa trong keyring.

### 3.8 Chuỗi cung ứng — phát hiện mới
- **PyMuPDF 2.0** (phát hành gần đây) kéo theo `onnxruntime`, `networkx`, `pyyaml`, `psutil`, `tabulate`, `pymupdf4llm`, `pymupdf-layout`. Bản **1.28.2 không có phụ thuộc nào**. Đã khóa `pymupdf>=1.28.2,<2`; nâng lên 2.x phải review riêng.
- GitHub Actions phải **ghim theo commit SHA**, không theo tag (vụ `tj-actions/changed-files` năm 2025 cho thấy tag có thể bị ghi đè để chèn mã đánh cắp secret). Workflows trong repo dùng `permissions: contents: read` mặc định, không dùng `pull_request_target`, tắt `persist-credentials`.
- Thêm: `uv lock` có hash + `uv sync --locked` trong CI, SBOM CycloneDX khi phát hành, OpenSSF Scorecard, CODEOWNERS cho `security/`, `.github/`, `pyproject.toml`, `uv.lock`.

### 3.9 File .exe bị antivirus báo nhầm
App đóng gói bằng PyInstaller **chưa ký số** thường bị Windows Defender/SmartScreen và antivirus khác gắn cờ nhầm là mã độc — đây là rủi ro uy tín lớn với dự án mới. Khuyến nghị:
- Đăng ký **SignPath Foundation** (ký số miễn phí cho dự án mã nguồn mở) hoặc Azure Trusted Signing.
- Build bootloader PyInstaller từ mã nguồn trong CI (giảm trùng chữ ký với malware dùng bootloader dựng sẵn); cân nhắc Nuitka.
- Gửi mẫu cho Microsoft Security Intelligence khi bị báo nhầm; công bố SHA-256 + attestation.

## 4. Khả năng mở rộng

| Điểm | Kế hoạch | Bổ sung trong MVP |
| --- | --- | --- |
| Nhà cung cấp mới | Adapter | Một adapter `OpenAICompatTranslator` + bảng `PROVIDERS` khai báo; thêm DeepSeek/Groq/OpenRouter = thêm 1 dòng cấu hình |
| Tài liệu dài | Lô 20 trang, checkpoint | Trích xuất theo **khối trang chạy song song** trong tiến trình con; cache theo đoạn = checkpoint tự nhiên, dừng ở đâu chạy tiếp ở đó |
| Bộ nhớ | Không giữ cả file trong RAM | Tiến trình con chết sau mỗi khối → không rò rỉ bộ nhớ MuPDF qua thời gian |
| IR | JSON có phiên bản | `IR_VERSION` + JSON Schema xuất bằng `vietpdf schema`; đọc IR khác phiên bản thì từ chối |
| Ngôn ngữ khác | v2.0 | `source_lang`/`target_lang` đã nằm trong `TranslateContext` |

## 5. Phạm vi và tiến độ

- Bộ dàn chữ tự viết + công thức trong dòng là phần khó nhất; MVP đã tự viết bộ dàn chữ đơn giản (ngắt dòng theo khoảng trắng, căn đều, chuỗi cho vừa khung) bằng `TextWriter` — đủ để thay `insert_htmlbox` và mở đường cho công thức trong dòng ở v1.0.
- Khuyến nghị tách v1.0 thành **v1.0 (PDF số + thuật ngữ + GUI)** và **v1.2 (OCR + sách dài + trợ lý đọc)** để có bản phát hành sớm, nhận phản hồi sớm.
- Một số nguồn tham khảo trong kế hoạch là trang tổng hợp ít uy tín (vd. trang blog tổng hợp giá Gemini, trang hướng dẫn Ollama của bên thứ ba). Nên thay bằng tài liệu chính thức (ai.google.dev, docs.ollama.com, platform.openai.com) trước khi công khai.

## 6. Những gì MVP này đã hiện thực

| Hạng mục trong kế hoạch | Trạng thái |
| --- | --- |
| Dịch PDF dạng chữ, giữ vị trí khối | ✅ |
| Font Việt đủ dấu (Noto, kiểm 134 ký tự), tự co chữ | ✅ |
| 3 nhà cung cấp qua một interface, lấy danh sách mô hình từ API | ✅ |
| Lô JSON + JSON Schema, kiểm tra, chia đôi lô khi lỗi | ✅ |
| Token bucket, retry lũy thừa có jitter, `Retry-After`, dừng ngay khi 401/403 | ✅ |
| Cache SQLite theo đoạn | ✅ |
| Giữ chỗ URL/email/DOI/trích dẫn/code/ký hiệu toán | ✅ (v1.0 kéo lên sớm) |
| Glossary CSV, chỉ chèn mục có trong lô | ✅ |
| Kiểm tra sai nghĩa trên máy: số liệu, phủ định, độ dài | ✅ (v1.0 kéo lên sớm) |
| Mục lục/bookmark tiếng Việt, bản song ngữ | ✅ |
| CLI, GUI tối giản (PySide6) | ✅ |
| Lớp bảo mật đầy đủ + audit chống mã độc + CI ghim SHA | ✅ |
| DocLayout-YOLO, công thức trong dòng, OCR, trợ lý đọc | ⏳ v1.0+ (đã chừa chỗ trong kiến trúc) |

---

## 7. Cập nhật 02/11/2026 — trạng thái sau đợt phát triển 2.0.0

So với bảng tính năng trong kế hoạch gốc (✅ xong · ⚠️ một phần · ⏳ chưa):

| Mốc | Tính năng | Trạng thái |
| --- | --- | --- |
| MVP | Lõi dịch giữ bố cục, font Việt, 3 nhà cung cấp, cache, CLI | ✅ |
| v1.0 | Giữ nguyên công thức, code, URL, số liệu | ✅ |
| v1.0 | Nhận diện cột, bảng, chú thích, đầu/chân trang | ✅ heuristic · ⚠️ mô hình ONNX cần tự tải trọng số |
| v1.0 | Bảng/biểu đồ giữ nguyên, chỉ dịch chú thích | ✅ |
| v1.0 | PDF song ngữ, glossary CSV, chọn trang, dừng/tiếp, xem song song, sửa tay, hàng đợi, ước tính chi phí, Việt/Anh, sáng/tối | ✅ |
| v1.0 | PDF scan (OCR) | ✅ · ⚠️ chưa nắn phối cảnh ảnh chụp |
| v1.0 | Thuật ngữ 5 lớp, dịch theo nghĩa, 2 lượt, kiểm tra sai nghĩa, ghi chú + phụ lục | ✅ |
| v1.0 | Trợ lý đọc cơ bản (tổng quan, tô màu, định nghĩa, giải thích, thẻ trích dẫn, kiến thức nền, ghi chú) | ✅ · ⚠️ đối chiếu song ngữ ở mức đoạn |
| v1.5 | Hỏi đáp có trang, phản biện, tái hiện, so sánh nhiều bài, phân tích câu dài, sổ từ + Anki, Zotero | ✅ · ⚠️ BM25 thay embedding; Anki dạng TSV |
| v2.0 | Ngôn ngữ nguồn khác, thêm nhà cung cấp, ngữ cảnh cả tài liệu, tóm tắt chương | ✅ |
| Đo lường | Bộ 200 trang, BIoU so với BabelDOC, thử nghiệm người dùng | ⏳ |

Bảo mật: mọi khuyến nghị ở mục 3 đã được hiện thực trừ ký số bộ cài (cần chứng chỉ) và sandbox cấp hệ điều hành
cho tiến trình con. Chi tiết: [BAO_MAT.md](BAO_MAT.md).

## 8. Cập nhật 2.1.0 — học qua bản dịch

Ngoài phạm vi kế hoạch gốc, theo yêu cầu người dùng: bôi đen trên trang để thêm vào sổ từ, ôn tập lặp lại ngắt
quãng, thư viện tài liệu mở lại được, bản học song ngữ, ghi tiếng Anh cạnh thuật ngữ theo đoạn, cùng một đợt kiểm
tra bảo mật toàn bộ (21 phát hiện đã sửa). Xem [HOC_TIENG_ANH.md](HOC_TIENG_ANH.md) và
[KIEM_TRA_BAO_MAT_2.1.md](KIEM_TRA_BAO_MAT_2.1.md).
