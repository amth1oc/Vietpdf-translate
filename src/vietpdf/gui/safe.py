"""Hộp thoại an toàn: luôn hiển thị PLAIN TEXT.

`QMessageBox` mặc định tự đoán định dạng (Qt::AutoText): chuỗi có thẻ HTML sẽ được render như rich text.
Thông báo lỗi có thể chứa nội dung từ máy chủ API hay từ file PDF, nên KHÔNG gọi QMessageBox.warning/... trực tiếp
trong GUI — dùng các hàm dưới đây (có test kiểm tra)."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QMessageBox, QWidget

from vietpdf.security.logsafe import redact


def _box(
    parent: QWidget | None,
    icon: QMessageBox.Icon,
    title: str,
    text: str,
    buttons: QMessageBox.StandardButton = QMessageBox.StandardButton.Ok,
) -> int:
    box = QMessageBox(parent)
    box.setIcon(icon)
    box.setWindowTitle(title)
    box.setTextFormat(Qt.TextFormat.PlainText)
    box.setText(redact(str(text))[:4000])
    box.setStandardButtons(buttons)
    return int(box.exec())


def warn(parent: QWidget | None, title: str, text: str) -> None:
    _box(parent, QMessageBox.Icon.Warning, title, text)


def info(parent: QWidget | None, title: str, text: str) -> None:
    _box(parent, QMessageBox.Icon.Information, title, text)


def ask(parent: QWidget | None, title: str, text: str) -> bool:
    yes, no = QMessageBox.StandardButton.Yes, QMessageBox.StandardButton.No
    return _box(parent, QMessageBox.Icon.Question, title, text, yes | no) == int(yes)
