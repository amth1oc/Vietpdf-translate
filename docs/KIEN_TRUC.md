# Kiến trúc VietPDF Translate 2.1

## 1. Bốn lớp, ba ranh giới tin cậy

```
┌──────────────────────── Giao diện ────────────────────────┐
│  CLI (cli.py)              GUI PySide6 (gui/)              │
│                  app · viewer · learning · workers         │
└───────────────┬────────────────────────────┬───────────────┘
                │                            │
┌───────────────▼──────── Điều phối ─────────▼───────────────┐
│  core/pipeline.py  assistant/ (v1.5)  library/ (2.1)       │
└──────┬──────────────────────────┬─────────────────┬────────┘
       │ JSON qua pipe            │ IR đã kiểm tra   │
┌──────▼──── Lõi PDF (tiến trình con) ──┐  ┌────────▼──── Lớp dịch ─────────────┐
│ extract · layout_model · ocr · render │  │ engine · prompts · terms · qa      │
│ (không có API key, không có mạng)     │  │ openai_compat · providers · cache  │
└───────────────────────────────────────┘  └────────┬───────────────────────────┘
                                                    │ chỉ qua đây
                                       ┌────────────▼────────────┐
                                       │ security/http.py        │
                                       │ (cửa ngõ mạng duy nhất) │
                                       └─────────────────────────┘
```

| Ranh giới | Phía không tin cậy | Kiểm soát |
| --- | --- | --- |
| PDF → app | file PDF, parser MuPDF | tiến trình con `spawn`, khoá mạng, giới hạn RAM/CPU/thời gian, trả JSON, IR kiểm tra lại bằng pydantic |
| LLM → app | phản hồi mô hình | JSON Schema, kiểm tra id/ký hiệu/độ dài, làm sạch ký tự điều khiển, xác minh trích dẫn |
| App → mạng | — | allowlist host theo nhà cung cấp, HTTPS bắt buộc, không redirect |

Chi tiết từng biện pháp: [BAO_MAT.md](BAO_MAT.md).

## 2. Luồng xử lý một file

```
precheck (chữ ký, kích thước) ─► doc_info [con] ─► extract_pages theo khối 20 trang, song song [con]
   ─► OCR các trang scan [con] ─► đầu/chân trang ─► thứ tự đọc XY-cut ─► nối đoạn qua cột/trang
   ─► lĩnh vực + tóm tắt (1 request, cache) ─► glossary 3 tầng ─► trích + đề xuất thuật ngữ (1 request, cache)
   ─► [duyệt thuật ngữ] ─► giữ chỗ (URL, trích dẫn, ⟦T⟧ thuật ngữ khoá) ─► dịch theo lô (cache, chia đôi khi lỗi)
   ─► đặt lại giữ chỗ, chia chuỗi đoạn ─► kiểm tra nghĩa + thuật ngữ ─► dịch lại có nhắc ─► biên tập lượt 2
   ─► kèm gốc lần đầu ─► dàn chữ, xin dịch gọn đoạn tràn ─► ghi IR + báo cáo ─► render [con]
```

`[con]` = chạy trong tiến trình con cô lập (`core/sandbox.py::run_isolated`).

## 3. Mô hình trung gian (IR v2)

`core/ir.py` — pydantic, `extra="forbid"`, có JSON Schema (`vietpdf schema`). IR v1 tự nâng lên v2.

```
DocumentIR: source_sha256, page_count, title, source_lang, target_lang, domains, summary,
            chapter_summaries, terms[DocTerm], toc[TocEntry], pages[PageIR]
PageIR:     number, width, height, is_scan, ocr, regions[Region], obstacles[bbox], segments[Segment]
Segment:    id, page, bbox, role, order, source, translation, status, flags, style, formulas[Formula],
            continues, ocr_bg, confidence
Formula:    key (M<trang>x<n>), page, bbox, baseline_offset, text
Region:     kind (table|figure|formula|caption|abandon|title|text), bbox, score, source (heuristic|model)
```

**Ký hiệu trong `source`/`translation`:**

| Ký hiệu | Nghĩa | Ai tạo | Sống ở đâu |
| --- | --- | --- | --- |
| `⟦M3x1⟧` | công thức trong dòng | extract | IR + bản dịch; render vẽ lại glyph gốc |
| `⟦B⟧…⟦/B⟧`, `⟦I⟧…⟦/I⟧` | đậm / nghiêng khác kiểu chính | extract | IR + bản dịch; mất thẻ ⇒ bỏ kiểu |
| `⟦F1⟧` | URL, email, DOI, arXiv, `[12]`, code, ký hiệu toán | protect | chỉ khi gửi dịch |
| `⟦T1⟧` | thuật ngữ khoá (chế độ nghiêm ngặt) | protect | chỉ khi gửi dịch, điền bản dịch chuẩn khi đặt lại |

**Vai trò đoạn (`role`)**: `body`, `title`, `caption`, `header`, `footer`, `footnote`, `ocr` được dịch;
`formula`, `in-table`, `in-figure`, `abandon` giữ nguyên (`in-table` dịch khi bật `--translate-tables`).

## 4. Xử lý PDF khó (v1.0)

| Bài toán | Cách làm | File |
| --- | --- | --- |
| Đọc ký tự | `get_text("rawdict")`: mã, khung, đường cơ sở, font, cỡ, cờ, màu | `extract.py` |
| Phân số / chỉ số bị tách dòng | gộp dòng chồng nhau theo chiều dọc (kể cả khác khối) | `_merge_blocks`, `_merge_stacked` |
| Công thức trong dòng | ký tự font toán (CMMI, CMSY, Symbol, STIX…), ký tự Unicode toán, chỉ số trên/dưới; nối qua `=`, `(`, số ngắn | `_mark_math`, `_build_items` |
| Nét vẽ của công thức | nét nhỏ nằm ≥50% trong khung công thức nới 1.5pt được gắn vào công thức | `_attach_strokes` |
| Dời công thức | xoá glyph + nét ở chỗ cũ (`LINE_ART_REMOVE_IF_COVERED`), vẽ lại nguyên khối bằng `show_pdf_page(clip=…)` từ bản gốc | `render._write_page` |
| Bảng | `page.find_tables()` + mô hình (tuỳ chọn) ⇒ `in-table` | `_heuristic_regions` |
| Biểu đồ | ảnh + cụm nét vẽ (`cluster_drawings`), loại hộp viền quanh chữ; nhãn trục sát hình cũng thuộc hình | `_heuristic_regions`, `_assign_role` |
| Thứ tự đọc | XY-cut đệ quy: cắt dọc (cột) trước, cắt ngang sau; đầu/chân trang đứng riêng | `segment.assign_reading_order` |
| Đoạn cắt qua cột/trang | không dấu kết câu + chữ thường đầu đoạn sau + cùng kiểu chữ ⇒ dịch chung, chia lại theo tỉ lệ | `link_continuations`, `split_translation` |
| Đầu/chân trang | cùng chữ (số → #), cùng vị trí, ≥40% số trang (tối thiểu 3) | `mark_headers_footers` |
| Trang scan | ít chữ + ảnh phủ trang, hoặc >30% ký tự lỗi mã hoá ⇒ OCR | `extract._page_ir`, `ocr.py` |
| Che chữ trên ảnh | redaction xoá điểm ảnh + tô màu nền (trung vị viền khung) | `render._write_page` |
| Dàn chữ | từ = chuỗi mảnh (chữ/đậm/nghiêng/công thức), font dự phòng cho glyph thiếu, chuỗi cho vừa khung | `fit.fit_items`, `render.segment_layout` |

## 5. Mô hình tiến trình & luồng

- **CLI**: một tiến trình chính (asyncio) + tiến trình con cho mỗi khối trang / mỗi lần render.
- **GUI**: luồng giao diện; mỗi tác vụ dài chạy trong `QThread` với `asyncio.run`. Tín hiệu từ luồng nền chỉ
  nối vào slot của QObject sống ở luồng giao diện (`_MainThreadCallback`), không nối vào lambda.
- **Trình xem**: `PageRenderer` — một tiến trình con thường trực render PNG; GUI không bao giờ mở PDF.
- **Cache SQLite**: dùng được đa luồng (`check_same_thread=False` + khoá).
- **Kho học tập** (`library/store.py`, 2.1): một file SQLite (`learning.sqlite3` trong thư mục dữ liệu) gồm bảng
  `documents`, `assist`, `vocab`; cột thường chỉ có id/mốc thời gian/lịch ôn, nội dung mã hoá từng dòng. Trong GUI
  chỉ được dùng ở luồng giao diện (worker phát tín hiệu `saved`, luồng chính lưu). `library/lookup.py` tra nghĩa
  cục bộ + câu ngữ cảnh; `library/review.py` tạo thẻ ôn; `core/study.py` dựng bản học (render trong tiến trình con).

## 6. Điểm mở rộng

| Muốn thêm | Làm gì |
| --- | --- |
| Nhà cung cấp tương thích OpenAI | 1 dòng `ProviderSpec` trong `translate/providers.py` (chọn `json_mode`) + thêm host vào `ALLOWED_URL_HOSTS` của audit |
| Glossary chuyên ngành | file CSV trong `assets/glossary/` + `DOMAIN_FILES` + `scripts/update_asset_sums.py` |
| Mô hình bố cục khác | hàm trả `Region` trong `core/layout_model.py`; ghim SHA-256 trong `models.lock.json` |
| Tác vụ trợ lý mới | schema trong `assistant/reader.py::SCHEMAS` + phương thức dùng `_ask_json` (có cache, kiểm tra bằng chứng) |
| Ngôn ngữ nguồn | `LANG_NAMES` trong `translate/base.py` (prompt, lọc đoạn đã hỗ trợ mọi bảng chữ) |
