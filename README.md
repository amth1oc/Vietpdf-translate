# VietPDF Translate 2.1

Dịch PDF sang tiếng Việt **giữ nguyên bố cục**: cùng số trang, cùng vị trí đoạn văn; công thức, bảng, biểu đồ giữ
nguyên; font Việt đủ dấu; OCR trang scan. Kèm **trợ lý đọc hiểu** tài liệu học thuật (thẻ tổng quan, hỏi đáp có
dẫn trang, giải thích công thức, phản biện, tái hiện). Chạy với Gemini, OpenAI, Claude, DeepSeek, OpenRouter,
Groq, hoặc **Ollama hoàn toàn offline**.

**Mới ở 2.1 — học qua bản dịch:** bôi đen từ trên trang → thêm vào sổ từ (tự điền nghĩa + câu ví dụ trong bài),
ôn tập thẻ lặp lại ngắt quãng, xuất Anki; thư viện lưu tài liệu để mở lại đúng trang; bản học song ngữ EN + VI;
ghi tiếng Anh trong ngoặc cạnh thuật ngữ.

> *English: open-source desktop app that translates PDFs into Vietnamese while preserving layout (formulas,
> tables, figures), with OCR and an evidence-checked reading assistant. AGPL-3.0.*

## Tài liệu

| Tài liệu | Nội dung |
| --- | --- |
| [docs/HUONG_DAN.md](docs/HUONG_DAN.md) | Hướng dẫn sử dụng: cài đặt, API key, lệnh, GUI, sự cố |
| [docs/KIEN_TRUC.md](docs/KIEN_TRUC.md) | Kiến trúc, luồng xử lý, IR, mô hình tiến trình, điểm mở rộng |
| [docs/v1.0.md](docs/v1.0.md) | PDF khó, thuật ngữ, dịch theo nghĩa, ứng dụng hoàn chỉnh |
| [docs/v1.5.md](docs/v1.5.md) | Trợ lý đọc hiểu và phân tích |
| [docs/v2.0.md](docs/v2.0.md) | Ngôn ngữ nguồn khác, nhà cung cấp mới, ngữ cảnh cả tài liệu |
| [docs/HOC_TIENG_ANH.md](docs/HOC_TIENG_ANH.md) | **2.1**: bản dịch cho người học, tiếng Anh bên cạnh, bôi đen → sổ từ, ôn tập, thư viện |
| [docs/KIEM_TRA_BAO_MAT_2.1.md](docs/KIEM_TRA_BAO_MAT_2.1.md) | **2.1**: báo cáo kiểm tra bảo mật, 21 phát hiện và cách sửa |
| [docs/BAO_MAT.md](docs/BAO_MAT.md) | Mô hình bảo mật, chống gài mã độc, rủi ro còn lại |
| [docs/DANH_GIA_KE_HOACH.md](docs/DANH_GIA_KE_HOACH.md) | Đánh giá kế hoạch ban đầu và trạng thái hiện thực |
| [CHANGELOG.md](CHANGELOG.md) · [SECURITY.md](SECURITY.md) · [CONTRIBUTING.md](CONTRIBUTING.md) | |

## Bắt đầu nhanh

```bash
uv sync --locked --all-extras             # cơ bản + GUI + OCR + bố cục
uv run vietpdf verify-assets
uv run vietpdf key set gemini
uv run vietpdf translate bai-bao.pdf -p gemini -m <mô-hình> --dual --study --gloss paragraph
uv run vietpdf vocab add "attention mechanism" --input bai-bao.pdf
uv run vietpdf vocab review
uv run vietpdf assist overview bai-bao.pdf -p gemini -m <mô-hình>
uv run vietpdf-gui
```

## Tính năng chính

| Nhóm | Tính năng |
| --- | --- |
| Bố cục | công thức trong dòng dời nguyên khối (glyph + nét vẽ gốc), bảng/biểu đồ giữ nguyên, đậm/nghiêng giữa câu, thứ tự đọc XY-cut, đoạn nối qua cột/trang, đầu/chân trang, mục lục, dàn chữ tự viết (nới khung → co chữ → xin dịch gọn) |
| Trang khó | OCR (RapidOCR/PP-OCR ONNX) che bằng màu giấy; font hỏng bảng Unicode ⇒ OCR; mô hình bố cục DocLayout-YOLO ONNX tuỳ chọn |
| Thuật ngữ | kho 3 tầng + 6 glossary chuyên ngành, nhận diện lĩnh vực, trích & duyệt thuật ngữ, ép thuật ngữ, kèm gốc lần đầu, kiểm tra + báo cáo nhất quán |
| Chất lượng | dịch theo nghĩa, kiểm tra phủ định/số liệu/chiều so sánh, biên tập lượt 2, báo cáo đoạn cần xem lại |
| Trợ lý đọc | tổng quan 5C, tô màu đọc lướt, định nghĩa tại chỗ, giải thích công thức/bảng, thẻ trích dẫn, hỏi đáp có trích dẫn đã xác minh, phản biện, tái hiện, so sánh nhiều bài, Markdown/BibTeX/Anki, Zotero |
| Học tiếng Anh | bôi đen → sổ từ (nghĩa từ glossary/bài/AI, câu ví dụ + câu dịch), ôn SM-2 (EN→VI, VI→EN, điền từ), Anki/CSV/MD, bản học song ngữ, ghi gốc trong ngoặc theo đoạn |
| Ứng dụng | CLI đầy đủ; GUI: hàng đợi, xem song song + sửa tay + xuất lại, trợ lý, glossary, thư viện (mở lại đúng trang, ghi chú, lịch sử hỏi đáp), ôn từ vựng, sáng/tối |

## Bảo mật (tóm tắt)

PDF được phân tích trong tiến trình con không có API key, không có mạng, có giới hạn bộ nhớ/thời gian; mọi kết
nối đi qua một cửa ngõ có allowlist host; key nằm trong kho mật khẩu hệ điều hành; cache mã hoá AES-GCM; đầu ra
LLM được kiểm tra (schema, ký hiệu, trích dẫn có thật); PDF xuất bị gỡ JavaScript và link nguy hiểm; thư viện và
sổ từ mã hoá AES-256-GCM; dữ liệu từ tài liệu không bao giờ nằm trong chỉ dẫn hệ thống của LLM. Mã nguồn được quét chống cửa
hậu (AST audit, CodeQL, gitleaks), phụ thuộc khoá hash, bản phát hành có SBOM + attestation. Chi tiết:
[docs/BAO_MAT.md](docs/BAO_MAT.md).

## Phát triển

```bash
uv sync --locked --all-groups --all-extras
uv run python scripts/security_audit.py src scripts
uv run ruff check src tests scripts && uv run mypy
QT_QPA_PLATFORM=offscreen uv run pytest -q        # dùng translator giả, không gọi API
uv run pyinstaller packaging/vietpdf.spec --noconfirm --clean
```

## Giấy phép

AGPL-3.0-or-later. Font Noto: SIL OFL 1.1. Xem [NOTICE](NOTICE).
