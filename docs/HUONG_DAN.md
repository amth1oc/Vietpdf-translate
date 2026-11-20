# Hướng dẫn sử dụng

## 1. Cài đặt

```bash
# Python 3.11–3.13 và uv (https://docs.astral.sh/uv/)
uv sync --locked --extra gui              # cơ bản + giao diện
uv sync --locked --all-extras             # thêm OCR (rapidocr) và nhận diện bố cục (onnxruntime)
uv run vietpdf verify-assets              # kiểm tra checksum font/tài nguyên
```

Bản cài sẵn (.zip/.tar.gz) lấy từ trang Releases. Kiểm tra trước khi chạy:
`sha256sum -c SHA256SUMS.txt` và `gh attestation verify <file> --repo <owner>/vietpdf-translate`.

## 2. Chọn nhà cung cấp

| Nhà cung cấp | Lấy key | Lưu key |
| --- | --- | --- |
| Gemini (có gói miễn phí) | Google AI Studio | `vietpdf key set gemini` |
| OpenAI / Claude / DeepSeek / OpenRouter / Groq | trang quản trị của từng bên | `vietpdf key set <tên>` |
| Ollama (offline, tài liệu mật) | cài Ollama, `ollama pull <mô-hình>` | không cần key |

Key được lưu trong kho mật khẩu của hệ điều hành. Trên máy chủ Linux không có Secret Service, dùng biến môi
trường `VIETPDF_<TÊN>_API_KEY` (ứng dụng đọc rồi xoá mọi biến dạng này khỏi môi trường ngay khi khởi động).

Base URL tuỳ chỉnh (`--base-url` hoặc `base_url` trong settings.json) **không bao giờ** nhận khoá của nhà cung cấp
chính thức: lưu khoá riêng bằng `vietpdf key set custom`. Mạng cần proxy: đặt `"https_proxy": "http://host:port"`
trong settings.json (biến môi trường `HTTP(S)_PROXY` bị bỏ qua).

`vietpdf models -p gemini` liệt kê mô hình hiện có (không hard-code tên mô hình).

## 3. Dịch

```bash
vietpdf estimate bai.pdf -p gemini -m <mô-hình> --price-in 0.1 --price-out 0.4   # ước tính trước
vietpdf translate bai.pdf -p gemini -m <mô-hình> --dual
vietpdf translate sach.pdf -p ollama -m <mô-hình> --offline --chapter-summaries
vietpdf translate hopdong.pdf -p openai -m <mô-hình> --style hanh_chinh --quality high
vietpdf translate scan.pdf -p gemini -m <mô-hình>          # trang scan tự OCR (cần extra [ocr])
vietpdf translate bai.zh.pdf --source-lang zh -p deepseek -m <mô-hình>
```

Tuỳ chọn hay dùng:

| Tuỳ chọn | Ý nghĩa |
| --- | --- |
| `--pages 1-10,15` | chỉ dịch các trang này |
| `--quality fast/standard/high` | 1 lượt / biên tập đoạn bị nghi / biên tập mọi đoạn |
| `--glossary my.csv` | glossary của bạn (`english,vietnamese,ghi_chu,chinh_sach`) — ưu tiên cao nhất |
| `--strict-terms` | ép thuật ngữ tuyệt đối (mặc định khi dùng Ollama) |
| `--annotate-terms` / `--glossary-appendix` | ghi chú nổi định nghĩa / phụ lục chú giải ở cuối |
| `--translate-tables` | dịch cả chữ trong ô bảng |
| `--layout-model model.onnx` | dùng DocLayout-YOLO (đã ghim SHA-256, xem `scripts/fetch_models.py`) |
| `--offline` | chặn mọi kết nối trừ localhost (biến môi trường proxy luôn bị bỏ qua) |
| `--accept-privacy` | đồng ý gửi nội dung tài liệu tới nhà cung cấp/host đang chọn (bắt buộc ở lần đầu, ghi nhớ trong settings.json) |
| `--gloss document/paragraph/off` | ghi tiếng Anh trong ngoặc lần đầu trong tài liệu / trong mỗi đoạn (học từ) / không ghi |
| `--study` | thêm bản học song ngữ `*.study.pdf` (câu gốc EN → bản dịch VI → thuật ngữ của đoạn) |
| `--no-library` | không lưu tài liệu vào thư viện |
| `--ask-password` | nhập mật khẩu PDF ẩn |
| `--password-stdin` | đọc mật khẩu PDF từ dòng đầu của stdin (cho script). Từ 2.1.2 **không còn `--password`** — mật khẩu trong dòng lệnh lộ qua `ps` với người dùng khác (R1-003) |

Đầu ra cạnh file gốc: `*.vi.pdf`, `*.dual.pdf` (nếu `--dual`), `*.study.pdf` (nếu `--study`), `*.ir.json`,
`*.report.md` (đọc file này để biết đoạn nào cần xem lại). Tài liệu được lưu vào **Thư viện** để mở lại sau.

Dừng giữa chừng bằng Ctrl+C rồi chạy lại cùng lệnh: phần đã dịch lấy từ cache (đã mã hoá nếu có kho mật khẩu).

## 4. Duyệt thuật ngữ trước (tài liệu dài, chuyên ngành)

```bash
vietpdf terms bai.pdf -p gemini -m <mô-hình> -o thuat-ngu.csv   # sửa file này
vietpdf translate bai.pdf -p gemini -m <mô-hình> --glossary thuat-ngu.csv
```

Trong GUI: bật "Duyệt thuật ngữ trước khi dịch" — một bảng hiện ra để chấp nhận/sửa/bỏ từng thuật ngữ.

## 5. Sửa tay và xuất lại

- GUI: tab **Xem & sửa** → nhấp đúp đoạn bên phải → sửa → Lưu → **Xuất lại PDF**. Giữ nguyên các ký hiệu `⟦M…⟧`
  (công thức). Ô "Học từ chỉnh sửa" lưu cặp thuật ngữ vào glossary của bạn.
- CLI: sửa trường `translation` trong `*.ir.json`, rồi `vietpdf render bai.pdf --ir bai.ir.json -o bai.vi.pdf`.

## 6. Trợ lý đọc (sau khi đã dịch)

```bash
vietpdf assist overview bai.pdf -p gemini -m <mô-hình>
vietpdf assist ask bai.pdf "Phương pháp này khác baseline ở điểm nào?" -p ...
vietpdf assist define bai.pdf "attention head"
vietpdf assist explain bai.pdf --segment p3-s12 --kind formula -p ...
vietpdf assist skim bai.pdf -p ...            # tạo bai.skim.pdf tô màu Mục tiêu/Phương pháp/Kết quả
vietpdf assist critique|reproduce|prereq bai.pdf -p ...
vietpdf citations bai.pdf [--lookup]          # --lookup: tra Semantic Scholar/OpenAlex (chỉ gửi DOI/ID/tên)
vietpdf notes bai.pdf --with-overview -p ...  # bai.notes.md + bai.notes.bib
vietpdf compare a.pdf b.pdf c.pdf -o so-sanh.csv -p ...
vietpdf vocab add overfitting --input bai.pdf && vietpdf vocab export --format anki -o the.txt
vietpdf zotero list --query "transformer"     # cần Zotero 7, bật local API
```

Kết quả trợ lý (thẻ tổng quan, hỏi đáp, giải thích…) được lưu vào lịch sử của tài liệu trong Thư viện
(`--no-library` để không lưu).

## 7. Học tiếng Anh: bôi đen → sổ từ → ôn tập; thư viện

Xem hướng dẫn đầy đủ: [HOC_TIENG_ANH.md](HOC_TIENG_ANH.md). Tóm tắt:

- Tab **Xem & sửa** → bật **Bôi đen chữ** (hoặc giữ Shift) → kéo chuột quanh từ → *Thêm vào từ vựng ôn tập*.
- Tab **Ôn từ vựng** → chọn kiểu thẻ → **Bắt đầu ôn** → Quên/Khó/Nhớ/Dễ (phím 1–4). Xuất Anki/CSV/Markdown.
- Tab **Thư viện** → **Mở để đọc tiếp** (đúng trang lần trước), ghi chú, lịch sử hỏi đáp, bản học song ngữ.
- CLI: `vietpdf vocab add|list|due|stats|review|export|import|delete`, `vietpdf library ...`, `vietpdf study bai.pdf`.

## 8. Sự cố thường gặp

| Hiện tượng | Cách xử lý |
| --- | --- |
| `Không có kho mật khẩu an toàn …` | bật Secret Service/KWallet, hoặc dùng biến môi trường `VIETPDF_<TÊN>_API_KEY` |
| `Không đọc được khoá mã hoá từ kho mật khẩu` | mở khoá kho mật khẩu (đăng nhập lại) rồi chạy lại; app không ghi dữ liệu dạng rõ thay thế |
| `Chưa đồng ý gửi tài liệu tới …` | chạy lại với `--accept-privacy` hoặc dùng Ollama |
| `Có N bản ghi chưa mã hoá …` | `vietpdf library migrate` (chỉ khi chắc chắn dữ liệu đó là của bạn) |
| `Host không nằm trong danh sách cho phép` | Base URL tuỳ chỉnh sai host, hoặc đang `--offline` mà chọn dịch vụ đám mây |
| HTTP 429 liên tục (Gemini miễn phí) | giảm `--rpm`, chạy lại sau — phần đã dịch nằm trong cache |
| Trang scan "chưa dịch" | cài `vietpdf-translate[ocr]` |
| Chữ quá nhỏ ở vài đoạn | xem `*.report.md` (cờ `overflow`), sửa tay bản dịch cho gọn hơn rồi Xuất lại |
| Tab Thư viện báo "KHÔNG được mã hoá" | máy không có kho mật khẩu an toàn (Linux: cài/bật Secret Service hoặc KWallet) |
| Thư viện báo "Thiếu file" | bản ghi lưu bằng `--no-copy` và file gốc đã bị di chuyển: dùng "Thêm file đã dịch…" để lưu lại |
| Windows SmartScreen cảnh báo | bộ cài chưa ký số: kiểm tra SHA256SUMS + attestation rồi chọn "Run anyway" |
