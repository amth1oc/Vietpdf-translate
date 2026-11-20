"""GUI 2.1: bôi đen trên trang → thêm từ vựng; bảng đoạn đang chọn; thư viện mở lại đúng trang; ôn tập thẻ."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture
def translated(samples: Path, tmp_path: Path) -> Path:
    from vietpdf.core.pipeline import PipelineOptions, run_pipeline
    from vietpdf.translate.fake import FakeTranslator

    src = tmp_path / "paper.pdf"
    src.write_bytes((samples / "paper.pdf").read_bytes())
    asyncio.run(run_pipeline(src, None, FakeTranslator(), PipelineOptions()))
    return src


@pytest.fixture
def win(monkeypatch: pytest.MonkeyPatch) -> Any:
    from PySide6.QtWidgets import QApplication

    from vietpdf.gui import safe
    from vietpdf.gui.app import MainWindow

    for name in ("warn", "info"):
        monkeypatch.setattr(safe, name, lambda *_a, **_k: None)
    monkeypatch.setattr(safe, "ask", lambda *_a, **_k: True)
    app = QApplication.instance() or QApplication([])
    w = MainWindow()
    yield w
    w.close()
    app.processEvents()


def test_select_text_on_page_and_add_to_vocab(win: Any, translated: Path) -> None:
    from vietpdf.library.store import save_translation

    save_translation(win.store, translated)
    win.library.refresh()
    win.library.table.selectRow(0)
    win.library.open_selected()
    v = win.viewer
    assert v.ir is not None and v.doc_id
    # vùng chọn quanh cả trang 1 bên trái: chữ được đọc trong tiến trình con
    text = v.renderer.text_in_rect(v.left.path, 0, (0, 0, 595, 842))
    assert "attention mechanism" in text
    hit = v.left.page_rect(__import__("PySide6.QtCore", fromlist=["QRectF"]).QRectF(10, 10, 200, 100))
    assert hit is not None and hit[0] == 0
    labels = [label for label, _fn in v.selection_actions("attention", 1)]
    assert labels[0].startswith("Thêm vào từ vựng ôn tập")
    # hành động "Thêm vào từ vựng" — từ đã có nghĩa trong thuật ngữ thì thêm ngay
    got: list[tuple[str, int]] = []
    v.vocab_requested.connect(lambda t, p: got.append((t, p)))
    v.selection_actions("baseline", 1)[0][1]()
    assert got == [("baseline", 1)]
    words = win.store.list_words(v.doc_id)
    assert [w["term"].lower() for w in words] == ["baseline"]
    assert words[0]["vi"] and "baseline" in words[0]["example"]
    assert words[0]["page"] == 1


def test_lookup_dialog_for_unknown_word_and_detail_panel(win: Any, translated: Path, monkeypatch: Any) -> None:
    from PySide6.QtWidgets import QDialog

    from vietpdf.gui import learning

    win.open_in_viewer(translated, translated.with_name("paper.vi.pdf"), translated.with_name("paper.ir.json"))
    v = win.viewer
    seg = next(s for s in v.ir.segments() if "attention" in s.source)
    v.show_detail(seg.id)
    assert "attention" in v.detail.en.toPlainText()
    assert v.detail.vi.toPlainText() and v.detail.vi.toPlainText() != "(chưa dịch)"
    assert "attention mechanism" in v.detail.terms.text()
    # bôi đen trong ô câu gốc rồi bấm nút => mở hộp thoại tra nghĩa (từ chưa có nghĩa)
    cur = v.detail.en.textCursor()
    start = v.detail.en.toPlainText().index("padding")
    cur.setPosition(start)
    cur.setPosition(start + len("padding"), cur.MoveMode.KeepAnchor)
    v.detail.en.setTextCursor(cur)
    assert v.detail.selected_text() == "padding"
    opened: list[dict[str, Any]] = []

    def fake_exec(self: Any) -> int:
        opened.append(self.result())
        self.set_value("vi", "đệm")
        return int(QDialog.DialogCode.Accepted)

    monkeypatch.setattr(learning.VocabDialog, "exec", fake_exec)
    v.detail._act("add")
    assert opened and opened[0]["term"] == "padding" and "padding" in opened[0]["example"]
    w = next(w for w in win.store.list_words(v.doc_id) if w["term"] == "padding")
    assert w["vi"] == "đệm"


def test_library_reopens_at_last_page_and_keeps_assist_history(win: Any, translated: Path) -> None:
    from vietpdf.library.store import save_translation

    doc_id = save_translation(win.store, translated)
    win.store.update_document(doc_id, last_page=1)
    win.library.refresh()
    win.library.select_id(doc_id)
    win.library.open_selected()
    assert win.viewer.doc_id == doc_id
    win.save_assist("ask", "Câu hỏi <b>thử</b>?", {"answer": "<img src=x>"})
    win.library.select_id(doc_id)
    win.library._selected()
    assert "<img src=x>" in win.library.history.toPlainText()  # hiển thị nguyên văn, không render HTML
    win.library.notes.setPlainText("ôn lại mục 2")
    win.library._save_notes()
    assert win.store.get_document(doc_id)["notes"] == "ôn lại mục 2"


def test_review_session_flow(win: Any) -> None:
    from PySide6.QtCore import Qt

    s = win.store
    a = s.add_word("baseline", "đường cơ sở", example="It beats the baseline.")
    b = s.add_word("epoch", "vòng huấn luyện")
    r = win.review
    r.reload_docs()
    assert r.table.rowCount() == 2
    r.mode.setCurrentIndex(r.mode.findData("cloze"))
    r.start()
    assert r.card is not None and "_____" in r.front.text()
    r.answer.setText("baseline")
    r.reveal()
    assert "✓" in r.back.text()
    assert r._key(Qt.Key.Key_1)  # quên => quay lại cuối hàng đợi
    r.reveal()
    r.grade("good")
    r.reveal()
    r.grade("easy")
    assert r.card is None and "Xong" in r.front.text()
    assert s.get_word(a)["reps"] == 1 and s.get_word(b)["reps"] == 1
    # sửa trực tiếp trong bảng
    r.refresh_table()
    item = r.table.item(0, 1)
    item.setText("mốc so sánh")
    assert s.get_word(str(item.data(Qt.ItemDataRole.UserRole)))["vi"] == "mốc so sánh"
