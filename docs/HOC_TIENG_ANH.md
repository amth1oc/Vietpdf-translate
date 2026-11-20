# Học qua bản dịch — v2.1

Mục tiêu của bản 2.1: người đọc không chỉ *hiểu nội dung* tài liệu tiếng Anh mà còn *học được tiếng Anh chuyên
ngành* từ chính tài liệu đó. Có bốn phần: bản dịch đúng ý, tiếng Anh bên cạnh, sổ từ vựng + ôn tập, thư viện.

## 1. Bản dịch để hiểu đúng, hiểu sâu

| Cơ chế | Tác dụng với người học |
| --- | --- |
| Chỉ dẫn dịch (phiên bản 4) yêu cầu dịch theo nghĩa, đủ mọi mệnh đề, giữ đúng sắc thái: phủ định, chiều so sánh, **mức độ chắc chắn** (may/might → *có thể*, likely → *nhiều khả năng*, must → *phải*, should → *nên*, suggest → *cho thấy*), **lượng từ** (few → *rất ít*, some → *một số*, most → *phần lớn*), điều kiện (unless, only if) | Không hiểu nhầm "kết quả có thể giúp" thành "kết quả sẽ giúp", "rất ít phương pháp" thành "một vài phương pháp" |
| Bảng thuật ngữ 3 tầng, ép đúng thuật ngữ | Cùng một khái niệm luôn một cách dịch trong cả tài liệu |
| Kiểm tra sau dịch: số liệu, phủ định, chiều so sánh, **sắc thái chắc chắn** (`qa:modality`), **sót tiếng Anh** (`qa:untranslated`), thiếu thuật ngữ | Đoạn nghi sai được tự dịch lại kèm ghi chú lỗi; còn sai thì viền vàng trong trình xem và có trong báo cáo |
| Cắt bản dịch theo ranh giới câu khi đoạn chảy qua cột/trang | Không còn nửa câu ở cột này, nửa câu ở cột kia |
| Biên tập lượt 2 (`--quality standard/high`) | Đoạn bị cờ được biên tập lại theo danh sách lỗi cụ thể |

### Chọn mô hình: kiểm tra tự động không thay được mô hình tốt

Độ đúng nghĩa của bản dịch **phụ thuộc chủ yếu vào mô hình AI** bạn chọn. App chỉ tự phát hiện được một số loại lỗi
cụ thể: sai/thiếu số liệu, mất phủ định, đảo chiều so sánh, sai mức độ chắc chắn, sót tiếng Anh, thiếu thuật ngữ,
độ dài bất thường. Đoạn mắc các lỗi đó được dịch lại; còn sai thì viền vàng và có trong `*.report.md`. Một câu
tiếng Việt **trôi chảy nhưng sai ý** (hiểu nhầm quan hệ nhân quả, gán sai chủ ngữ, dịch sai nghĩa chuyên ngành)
thì không kiểm tra tự động nào bắt chắc chắn được.

| Mục đích | Cấu hình nên dùng |
| --- | --- |
| Học, nghiên cứu, cần đúng nội dung | Mô hình mạnh (bản cao cấp của Gemini / OpenAI / Claude) + `--quality high` |
| Tiết kiệm nhưng vẫn cần đúng | Mô hình rẻ để dịch + mô hình mạnh để biên tập: `--quality high --editor-provider <nhà cung cấp> --editor-model <mô hình mạnh>` |
| Đọc lướt, nắm ý chung | Mô hình rẻ/nhanh, `--quality standard` |
| Không được gửi tài liệu ra ngoài | Ollama offline. Mô hình nhỏ chạy trên máy (cỡ 7–8 tỉ tham số) hay sai nghĩa với văn bản học thuật tiếng Việt: chỉ dùng để đọc lướt, đối chiếu bản gốc ở chỗ quan trọng |

Dù dùng mô hình nào: đọc `*.report.md`, xem các đoạn viền vàng, và với chỗ quan trọng thì đối chiếu câu gốc
(bảng "Đoạn đang chọn" hoặc bản học song ngữ đặt câu gốc ngay trên câu dịch).

**Bộ dịch giả `fake`** chỉ dùng để kiểm thử phần mềm: nó sinh chữ tiếng Việt ngẫu nhiên (vô nghĩa) để thử font
và dàn trang, không gọi API nào. Từ 2.1, mọi PDF do nó tạo đều có dòng chữ đỏ "BẢN DỊCH GIẢ ĐỂ KIỂM THỬ" trên đầu
mỗi trang, và giao diện chỉ hiện nó khi chạy với `--dev`.

## 2. Tiếng Anh bên cạnh bản dịch

**Ghi thuật ngữ gốc trong ngoặc** — “cơ chế chú ý (attention mechanism)”:

| Chế độ | Khi nào dùng |
| --- | --- |
| `--gloss document` (mặc định) | Đọc nhanh: chỉ lần đầu xuất hiện trong cả tài liệu |
| `--gloss paragraph` | **Học từ**: lần đầu trong *mỗi đoạn* — gặp lại thuật ngữ nhiều lần, nhớ lâu hơn |
| `--gloss off` | Bản dịch "sạch" để nộp/chia sẻ |

**Bản học song ngữ** (`--study` khi dịch, hoặc `vietpdf study bai.pdf` sau khi dịch — không gọi API):
file `bai.study.pdf` khổ A4, mỗi đoạn gồm câu tiếng Anh gốc (chữ nghiêng xám) → bản dịch tiếng Việt → các thuật
ngữ xuất hiện trong đoạn (EN → VI). Cuối file là bảng thuật ngữ của tài liệu và sổ từ bạn đã lưu kèm câu ví dụ.

Ngoài ra vẫn có bản song ngữ đặt trang gốc cạnh trang dịch (`--dual`) giữ nguyên bố cục.

## 3. Bôi đen → thêm vào từ vựng ôn tập

**Trong ứng dụng (tab Xem & sửa):**

1. Bấm nút **Bôi đen chữ** (hoặc giữ **Shift**) rồi kéo chuột quanh từ/cụm trên trang gốc hoặc trang dịch.
2. Menu hiện ra: **Thêm vào từ vựng ôn tập** · **Tra nghĩa & xem ngữ cảnh…** · **Sao chép**.
3. "Thêm vào từ vựng": nếu từ đã có nghĩa trong bảng thuật ngữ / thuật ngữ của bài, từ được thêm ngay, kèm **câu
   tiếng Anh chứa từ đó trong bài**, **câu dịch tương ứng** và số trang. Nếu chưa có nghĩa, hộp thoại mở ra để
   bạn tự điền hoặc bấm **Hỏi AI nghĩa theo ngữ cảnh** (từ loại, nghĩa đúng ngữ cảnh, giải thích, lưu ý cách dùng —
   gắn nhãn "Giải thích của AI").
4. Bấm vào một đoạn bất kỳ: bảng **Đoạn đang chọn** phía dưới hiện câu gốc + bản dịch + thuật ngữ của đoạn. Bôi
   đen ngay trong đó rồi bấm nút hoặc chuột phải → "Thêm vào từ vựng ôn tập".

Nghĩa được tìm theo thứ tự: glossary của bạn → thuật ngữ của tài liệu → glossary chuyên ngành → định nghĩa tác giả
viết trong bài ("X is defined as…") → (khi bạn bấm) AI. Số nhiều được nhận ra (mechanisms → mechanism).

**Bằng dòng lệnh:**

```bash
vietpdf vocab add "attention mechanism" --input bai.pdf          # tự lấy nghĩa + câu ví dụ trong bài
vietpdf vocab add "ablation" --input bai.pdf --ai -p gemini -m <mô-hình>   # chưa có nghĩa thì hỏi AI
vietpdf vocab add "baseline" --vi "đường cơ sở" --example "…"
vietpdf vocab list [--doc <id>] [--query chú ý]
```

## 4. Ôn tập (lặp lại ngắt quãng)

Tab **Ôn từ vựng**: chọn tài liệu (hoặc tất cả) và kiểu thẻ, bấm **Bắt đầu ôn**.

| Kiểu thẻ | Mặt trước | Luyện |
| --- | --- | --- |
| Anh → Việt | từ + câu ví dụ trong bài | nhận diện nghĩa |
| Việt → Anh | nghĩa tiếng Việt | nhớ chủ động từ tiếng Anh |
| Điền từ vào câu | câu gốc trong bài bị che từ (có gợi ý nghĩa) | dùng từ trong ngữ cảnh thật |

Có thể gõ đáp án (so khớp bỏ dấu, không phân biệt hoa thường, chấp nhận số nhiều) rồi tự chấm **Quên / Khó / Nhớ /
Dễ** (phím 1–4 khi thẻ đang chọn). Lịch ôn theo SM-2: nhớ ⇒ 1 ngày → 6 ngày → giãn dần; quên ⇒ hỏi lại ngay trong
phiên. Bảng từ bên dưới sửa trực tiếp được (từ, nghĩa, giải thích, câu ví dụ); có xoá, thêm tay, lọc.

```bash
vietpdf vocab review --mode cloze        # en-vi | vi-en | cloze, ôn ngay trong terminal
vietpdf vocab due | vietpdf vocab stats
```

**Xuất / nhập**

| Định dạng | Lệnh | Dùng để |
| --- | --- | --- |
| Anki | `vietpdf vocab export --format anki -o tu.txt` | Anki: File › Import (đã đặt sẵn tách bằng Tab, chữ thuần) |
| CSV | `--format csv` | Excel/Google Sheets, mở được bằng ứng dụng khác |
| Markdown | `--format md` | Obsidian, Notion, ghi chú |
| Nhập CSV | `vietpdf vocab import tu.csv` | cột `term`, `vi`, `definition`, `example` |

## 5. Thư viện tài liệu

Mỗi lần dịch, tài liệu được lưu vào thư viện (tắt bằng `--no-library` hoặc bỏ chọn "Lưu vào thư viện"): bản sao
file gốc, bản dịch, IR, báo cáo, bản học — trong thư mục dữ liệu của ứng dụng.

Tab **Thư viện**: tìm theo tiêu đề/ghi chú; **Mở để đọc tiếp** (mở đúng trang lần trước đang đọc); mở bản dịch
bằng ứng dụng PDF; tạo/mở **Bản học song ngữ** (kèm sổ từ mới nhất); **Ôn từ của tài liệu**; ghi chú riêng; xem
lại **lịch sử hỏi đáp** của trợ lý đọc; thêm một file đã dịch từ trước; xoá (từ vựng vẫn được giữ để ôn).

```bash
vietpdf library list | show <id> | history <id>
vietpdf library notes <id> --text "đọc lại mục 3"
vietpdf library add bai.pdf [--no-copy]
vietpdf library remove <id> [--keep-files]
```

Lưu ý riêng tư: ghi chú, câu hỏi, từ vựng và đường dẫn được mã hoá AES-256-GCM (khoá trong kho mật khẩu của hệ
điều hành). Bản sao PDF trong thư viện không mã hoá — giống file gốc của bạn; muốn chỉ lưu đường dẫn thì dùng
`--no-copy`. Chi tiết: [KIEM_TRA_BAO_MAT_2.1.md](KIEM_TRA_BAO_MAT_2.1.md).

Thư mục dữ liệu: Linux `~/.local/share/vietpdf`, macOS `~/Library/Application Support/VietPDF/data`, Windows
`%LOCALAPPDATA%\VietPDF\data` (đổi bằng biến `VIETPDF_DATA_DIR`). Sổ từ của bản 2.0 được tự chuyển sang khi mở lần đầu.
