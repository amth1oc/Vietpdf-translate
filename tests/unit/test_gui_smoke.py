from __future__ import annotations

import os

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def test_main_window_builds_and_shows_plain_text_only() -> None:
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication

    from vietpdf.gui.app import MainWindow

    app = QApplication.instance() or QApplication([])
    w = MainWindow()
    assert w.provider.count() >= 7  # gemini, openai, ollama, claude, deepseek, openrouter, groq
    assert w.tabs.count() == 6  # Dịch, Xem & sửa, Trợ lý, Thuật ngữ, Thư viện, Ôn từ vựng
    assert w.translate.stage.textFormat() == Qt.TextFormat.PlainText
    w.translate.append("<img src='http://evil.example.com/x.png'> sk-proj-ABCDEFGHIJKLMNOPQRSTUV")  # gitleaks:allow
    shown = w.translate.log.toPlainText()
    assert "<img" in shown  # hiển thị nguyên văn, không render HTML
    assert "sk-proj-ABCDEFGHIJ" not in shown  # bí mật bị che
    w.close()
    app.processEvents()
