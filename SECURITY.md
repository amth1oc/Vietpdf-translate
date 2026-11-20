# Chính sách bảo mật

## Báo lỗ hổng

**Không** mở issue công khai cho lỗ hổng bảo mật. Hãy báo riêng qua
**GitHub → tab Security → Report a vulnerability** (GitHub Security Advisories).

Vui lòng gửi kèm: phiên bản, hệ điều hành, các bước tái hiện, file PDF mẫu (nếu có, đã xoá dữ liệu nhạy cảm), và mức ảnh hưởng.

Cam kết: phản hồi trong 7 ngày; bản sửa hoặc phương án giảm thiểu: **Nghiêm trọng ≤ 7 ngày, Cao ≤ 30 ngày, Trung bình ≤ 90
ngày**, Thấp theo đợt phát hành; ghi công người báo (nếu đồng ý).

## Phiên bản được hỗ trợ

Chỉ bản phát hành mới nhất nhận bản vá bảo mật.

## Kiểm chứng bản cài

```bash
sha256sum -c SHA256SUMS.txt
gh attestation verify VietPDF-windows-x64.zip --repo <owner>/vietpdf-translate
```

Bản cài chỉ được phát hành từ GitHub Actions (`.github/workflows/release.yml`, cần người duyệt ở environment
`release`). Lưu ý: workflow của 2.1.0 bị lỗi cú pháp nên **không có bản cài 2.1.0 nào do CI tạo** — đừng dùng bản
cài 2.1.0 lưu hành ở bất kỳ đâu. Đừng tải bản cài từ nơi khác.

## Phạm vi

Trong phạm vi: rò rỉ API key, gửi dữ liệu tới host ngoài nhà cung cấp đã chọn, thực thi mã từ PDF hay từ phản hồi LLM, vượt sandbox, tráo tài nguyên, lỗ hổng chuỗi cung ứng trong repo/CI.

Ngoài phạm vi: chính sách dữ liệu của nhà cung cấp dịch (Google, OpenAI); lỗi trong MuPDF/Qt chưa có bản vá upstream (vẫn nên báo để chúng tôi theo dõi và giảm thiểu).
