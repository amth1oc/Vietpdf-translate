"""Thư viện tài liệu, sổ từ vựng và ôn tập (GUI).

- ``VocabDialog``: thêm/sửa một từ (điền sẵn nghĩa từ bảng thuật ngữ, câu ví dụ trong bài; có nút hỏi AI).
- ``LibraryTab``: tài liệu đã lưu — mở lại đúng trang đang đọc, ghi chú, lịch sử hỏi đáp, bản học, xoá.
- ``ReviewTab``: ôn tập thẻ (EN→VI, VI→EN, điền từ) theo lịch SM-2; bảng từ sửa trực tiếp; xuất Anki/CSV/MD.

An toàn: mọi chuỗi (tiêu đề, từ, nghĩa, câu từ PDF/AI) chỉ hiển thị dạng VĂN BẢN THUẦN; truy cập kho học tập
chỉ ở luồng giao diện (việc gọi AI chạy nền qua ``MainWindow.run_job``, kết quả quay về luồng chính).
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices, QFont
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from vietpdf.gui import safe
from vietpdf.library.review import check_answer, make_card
from vietpdf.security.logsafe import redact
from vietpdf.textfmt import to_text

if TYPE_CHECKING:
    from vietpdf.gui.app import MainWindow
    from vietpdf.library.store import LearningStore

ID_ROLE = Qt.ItemDataRole.UserRole


def _label(text: str = "", wrap: bool = True) -> QLabel:
    lb = QLabel(text)
    lb.setTextFormat(Qt.TextFormat.PlainText)
    lb.setWordWrap(wrap)
    return lb


def _item(text: Any, editable: bool = False, key: str | None = None) -> QTableWidgetItem:
    it = QTableWidgetItem("" if text is None else str(text))
    flags = it.flags()
    it.setFlags(flags | Qt.ItemFlag.ItemIsEditable if editable else flags & ~Qt.ItemFlag.ItemIsEditable)
    if key is not None:
        it.setData(ID_ROLE, key)
    return it


def _date(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else ""


# ---------------------------------------------------------------------------------------------- thêm / sửa từ


class VocabDialog(QDialog):
    FIELDS = (
        ("term", "Từ / cụm từ (EN):"),
        ("vi", "Nghĩa tiếng Việt:"),
        ("pos", "Từ loại:"),
        ("definition", "Giải thích:"),
        ("example", "Câu ví dụ (trong bài):"),
        ("example_vi", "Câu dịch:"),
        ("note", "Ghi chú:"),
    )
    LONG = frozenset({"definition", "example", "example_vi", "note"})

    def __init__(self, win: MainWindow, data: dict[str, Any], title: str = "Thêm vào từ vựng ôn tập") -> None:
        super().__init__(win)
        self.win = win
        self.setWindowTitle(title)
        self.resize(640, 520)
        self.data = dict(data)
        self.edits: dict[str, QLineEdit | QPlainTextEdit] = {}
        form = QFormLayout()
        for key, label in self.FIELDS:
            w: QLineEdit | QPlainTextEdit
            if key in self.LONG:
                w = QPlainTextEdit(str(data.get(key) or ""))
                w.setMaximumHeight(80)
            else:
                w = QLineEdit(str(data.get(key) or ""))
            self.edits[key] = w
            form.addRow(label, w)
        self.page = QSpinBox()
        self.page.setRange(0, 100000)
        self.page.setSpecialValueText("—")
        self.page.setValue(int(data.get("page") or 0))
        form.addRow("Trang:", self.page)
        src = str(data.get("source") or "")
        self.source = _label(
            f"Nguồn nghĩa: {src}" if src else "Chưa có nghĩa trong bảng thuật ngữ/bài — hãy tự điền hoặc bấm 'Hỏi AI'."
        )
        self.ai_btn = QPushButton("Hỏi AI nghĩa theo ngữ cảnh")
        self.ai_btn.clicked.connect(self._ask_ai)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        row = QHBoxLayout()
        row.addWidget(self.ai_btn)
        row.addStretch(1)
        row.addWidget(buttons)
        lay = QVBoxLayout(self)
        lay.addLayout(form)
        lay.addWidget(self.source)
        lay.addLayout(row)

    def value(self, key: str) -> str:
        w = self.edits[key]
        return (w.toPlainText() if isinstance(w, QPlainTextEdit) else w.text()).strip()

    def set_value(self, key: str, text: str) -> None:
        w = self.edits[key]
        if isinstance(w, QPlainTextEdit):
            w.setPlainText(text)
        else:
            w.setText(text)

    def result(self) -> dict[str, Any]:  # type: ignore[override]
        out: dict[str, Any] = {k: self.value(k) for k, _ in self.FIELDS}
        out["page"] = self.page.value() or None
        out["source"] = str(self.data.get("source") or "")
        return out

    def _accept(self) -> None:
        if not self.value("term"):
            safe.info(self, "Thiếu từ", "Hãy nhập từ hoặc cụm từ tiếng Anh.")
            return
        self.accept()

    def _ask_ai(self) -> None:
        from vietpdf.library.lookup import ai_meaning  # noqa: PLC0415

        term, sentence = self.value("term"), self.value("example")
        if not term:
            return
        pid, model = self.win.current_provider()
        if not model:
            safe.info(self, "Chưa chọn mô hình", "Chọn nhà cung cấp và mô hình ở thanh trên cùng trước.")
            return
        offline = self.win.translate.checks["offline"].isChecked()
        if not self.win.confirm_privacy(pid):
            return

        async def job() -> dict[str, str]:
            from vietpdf.gui.workers import make_translator  # noqa: PLC0415

            t = make_translator(pid, model, offline)
            try:
                return await ai_meaning(t, term, sentence)
            finally:
                await t.aclose()

        self.ai_btn.setEnabled(False)
        self.ai_btn.setText("Đang hỏi AI…")
        self.win.run_job(job, self._fill_ai)

    def _fill_ai(self, res: dict[str, str]) -> None:
        try:  # hộp thoại có thể đã đóng trước khi AI trả lời
            for key, src in (
                ("vi", "vi"),
                ("pos", "pos"),
                ("definition", "definition_vi"),
                ("example_vi", "example_vi"),
                ("note", "note"),
            ):
                if res.get(src) and not self.value(key):
                    self.set_value(key, str(res[src]))
            self.data["source"] = res.get("source", "Giải thích của AI")
            self.source.setText(f"Nguồn nghĩa: {self.data['source']} — hãy kiểm tra lại.")
            self.ai_btn.setText("Hỏi AI nghĩa theo ngữ cảnh")
            self.ai_btn.setEnabled(True)
        except RuntimeError:
            return


def prefill(win: MainWindow, text: str, page: int | None) -> dict[str, Any]:
    """Điền sẵn từ: chuẩn hoá vùng bôi đen, tra bảng thuật ngữ/thuật ngữ của bài/định nghĩa trong bài,
    lấy câu chứa từ (ưu tiên trang đang xem) và câu dịch tương ứng."""
    from vietpdf.library.lookup import clean_selection, find_context, local_meaning  # noqa: PLC0415

    v = win.viewer
    term = clean_selection(text)
    found = local_meaning(term, v.ir, v.glossary)
    sentence, sentence_vi, pg = find_context(v.ir, found.get("term") or term, page)
    if not sentence and v.ir is not None and found.get("term"):  # bôi đen trên bản dịch
        sentence, sentence_vi, pg = find_context(v.ir, term, page)
    return {
        "term": found.get("term") or term,
        "vi": found.get("vi", ""),
        "definition": found.get("definition", ""),
        "example": sentence,
        "example_vi": sentence_vi,
        "page": pg or page,
        "source": found.get("source", ""),
    }


# ---------------------------------------------------------------------------------------------- thư viện


class LibraryTab(QWidget):
    COLS = ("Tiêu đề", "Trang", "Từ vựng", "Mở gần nhất")

    def __init__(self, win: MainWindow) -> None:
        super().__init__()
        self.win = win
        self.search = QLineEdit()
        self.search.setPlaceholderText("Tìm theo tiêu đề, tên file, ghi chú…")
        self.search.textChanged.connect(lambda _t: self.refresh())
        self.table = QTableWidget(0, len(self.COLS))
        self.table.setHorizontalHeaderLabels(list(self.COLS))
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.itemSelectionChanged.connect(self._selected)
        self.table.itemDoubleClicked.connect(lambda _i: self.open_selected())
        self.detail = _label("Chọn một tài liệu.")
        self.notes = QPlainTextEdit()
        self.notes.setPlaceholderText("Ghi chú của bạn về tài liệu này (được mã hoá khi lưu)…")
        self.history = QPlainTextEdit()
        self.history.setReadOnly(True)
        self.history.setPlaceholderText("Lịch sử hỏi đáp / trợ lý đọc của tài liệu này")
        btns = [
            ("Mở để đọc tiếp", self.open_selected),
            ("Mở bản dịch (ứng dụng PDF)", lambda: self._open_file("output")),
            ("Bản học song ngữ", self._study),
            ("Ôn từ của tài liệu", self._review),
            ("Lưu ghi chú", self._save_notes),
            ("Thêm file đã dịch…", self._add),
            ("Xoá khỏi thư viện", self._remove),
            ("Xoá cache bản dịch", self._clear_cache),
        ]
        bar = QHBoxLayout()
        for label, fn in btns:
            b = QPushButton(label)
            b.clicked.connect(fn)
            bar.addWidget(b)
        bar.addStretch(1)
        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.addWidget(self.detail)
        rl.addWidget(_label("Ghi chú:"))
        rl.addWidget(self.notes, 1)
        rl.addWidget(_label("Hỏi đáp đã lưu:"))
        rl.addWidget(self.history, 2)
        split = QSplitter()
        split.addWidget(self.table)
        split.addWidget(right)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 2)
        enc = self.store is not None and self.store.encrypted
        self.security = _label(
            "Thư viện, ghi chú và sổ từ được mã hoá (AES-256-GCM, khoá trong kho mật khẩu của hệ điều hành)."
            if enc
            else "Cảnh báo: không có kho mật khẩu an toàn — thư viện và sổ từ KHÔNG được mã hoá (chỉ giới hạn quyền)."
        )
        lay = QVBoxLayout(self)
        lay.addWidget(self.search)
        lay.addWidget(split, 1)
        lay.addLayout(bar)
        lay.addWidget(self.security)
        self.docs: list[dict[str, Any]] = []
        self.refresh()

    @property
    def store(self) -> LearningStore | None:
        return self.win.store

    def refresh(self) -> None:
        if self.store is None:
            return
        cur = self.current_id()
        self.docs = self.store.list_documents(self.search.text())
        self.table.setRowCount(len(self.docs))
        for r, d in enumerate(self.docs):
            self.table.setItem(r, 0, _item(d.get("title") or d.get("source_name"), key=d["id"]))
            self.table.setItem(r, 1, _item(d.get("pages")))
            self.table.setItem(r, 2, _item(d.get("vocab", 0)))
            self.table.setItem(r, 3, _item(_date(d.get("opened", 0))))
            if d["id"] == cur:
                self.table.selectRow(r)

    def current_id(self) -> str:
        r = self.table.currentRow()
        if 0 <= r < self.table.rowCount():
            it = self.table.item(r, 0)
            return str(it.data(ID_ROLE)) if it is not None else ""
        return ""

    def current(self) -> dict[str, Any] | None:
        did = self.current_id()
        return next((d for d in self.docs if d["id"] == did), None)

    def select_id(self, doc_id: str) -> None:
        for r in range(self.table.rowCount()):
            it = self.table.item(r, 0)
            if it is not None and it.data(ID_ROLE) == doc_id:
                self.table.selectRow(r)
                return

    def _selected(self) -> None:
        d = self.current()
        if d is None or self.store is None:
            return
        paths = d.get("paths", {})
        missing = [k for k in ("source", "output", "ir") if not Path(paths.get(k, "")).is_file()]
        self.detail.setText(
            f"{d.get('title')}\nFile gốc: {d.get('source_name')} · {d.get('pages')} trang · "
            f"lĩnh vực: {', '.join(d.get('domains') or []) or '—'}\n"
            f"Đang đọc tới trang {int(d.get('last_page') or 0) + 1} · {d.get('vocab', 0)} từ đã lưu"
            + (f"\nThiếu file: {', '.join(missing)} (đã bị di chuyển/xoá?)" if missing else "")
        )
        self.notes.setPlainText(str(d.get("notes") or ""))
        lines = []
        for h in self.store.list_assist(d["id"]):
            lines.append(f"[{_date(h['created'])}] {h.get('kind')}: {h.get('question') or ''}")
            lines.append(to_text(h.get("result"))[:4000])
            lines.append("")
        self.history.setPlainText("\n".join(lines))

    def open_selected(self) -> None:
        d = self.current()
        if d is None:
            return
        self.win.open_document(d)

    def _open_file(self, kind: str) -> None:
        d = self.current()
        p = Path((d or {}).get("paths", {}).get(kind, ""))
        if d is None or not p.is_file() or p.suffix.lower() != ".pdf":  # chỉ mở PDF, không mở file thực thi
            safe.info(self, "Không có file", "Tài liệu này chưa có file PDF đó.")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(p)))

    def _study(self) -> None:
        d = self.current()
        if d is None or self.store is None:
            return
        study = Path(d.get("paths", {}).get("study", ""))
        if (
            study.is_file()
            and study.suffix.lower() == ".pdf"
            and not safe.ask(self, "Bản học song ngữ", "Đã có bản học. Tạo lại (kèm sổ từ mới nhất)?")
        ):
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(study)))
            return
        ir_path = Path(d.get("paths", {}).get("ir", ""))
        if not ir_path.is_file():
            safe.info(self, "Thiếu IR", "Không tìm thấy dữ liệu bản dịch (ir.json) của tài liệu này.")
            return
        from vietpdf.core import study as study_mod  # noqa: PLC0415
        from vietpdf.core.ir import DocumentIR  # noqa: PLC0415
        from vietpdf.translate.glossary import build_glossary  # noqa: PLC0415
        from vietpdf.translate.terms import doc_terms_to_entries  # noqa: PLC0415

        ir = DocumentIR.model_validate_json(ir_path.read_text("utf-8"))
        data = study_mod.build_study_data(
            ir, build_glossary(ir.domains, doc_terms_to_entries(ir.terms)), self.store.list_words(d["id"])
        )
        out = (
            ir_path.with_name("ban-hoc.pdf")
            if d.get("copied")
            else ir_path.with_name(ir_path.name.removesuffix(".ir.json") + ".study.pdf")
        )

        async def job() -> Path:
            import asyncio  # noqa: PLC0415

            from vietpdf.core.sandbox import run_to_outputs  # noqa: PLC0415

            await asyncio.to_thread(
                run_to_outputs, study_mod.render_study, lambda st: (str(st[0]), data), [out], timeout=300
            )
            return out

        def done(p: Path) -> None:
            if self.store is not None:
                self.store.add_document(
                    {**{k: v for k, v in d.items() if k not in ("id", "added", "opened", "vocab", "paths")}},
                    {"study": p},
                    copy=bool(d.get("copied")),
                )
            self.refresh()
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(p)))

        self.win.run_job(job, done)

    def _review(self) -> None:
        d = self.current()
        if d is not None:
            self.win.review.focus_document(d["id"])

    def _save_notes(self) -> None:
        d = self.current()
        if d is None or self.store is None:
            return
        self.store.update_document(d["id"], notes=self.notes.toPlainText()[:20000])
        self.win.statusBar().showMessage("Đã lưu ghi chú.", 4000)
        self.refresh()

    def _add(self) -> None:
        if self.store is None:
            return
        p, _ = QFileDialog.getOpenFileName(self, "Chọn file PDF GỐC đã dịch", "", "PDF (*.pdf)")
        if not p:
            return
        from vietpdf.library.store import save_translation  # noqa: PLC0415

        try:
            doc_id = save_translation(self.store, Path(p))
        except Exception as e:
            safe.warn(self, "Không thêm được", redact(f"{type(e).__name__}: {e}"))
            return
        self.refresh()
        self.select_id(doc_id)

    def _remove(self) -> None:
        d = self.current()
        if d is None or self.store is None:
            return
        if not safe.ask(
            self,
            "Xoá khỏi thư viện",
            f"Xoá “{d.get('title')}” khỏi thư viện (cả bản sao trong thư "
            "viện và lịch sử hỏi đáp)? Từ vựng của tài liệu vẫn được giữ lại để ôn.",
        ):
            return
        self.store.remove_document(d["id"], delete_files=bool(d.get("copied")))
        self.refresh()

    def _clear_cache(self) -> None:
        from vietpdf.gui.workers import make_cache  # noqa: PLC0415

        c = make_cache()
        n = c.clear()
        c.close()
        safe.info(self, "Cache", f"Đã xoá {n} mục.")


# ---------------------------------------------------------------------------------------------- ôn tập


class _CardWidget(QWidget):
    def __init__(self, on_key: Any) -> None:
        super().__init__()
        self.on_key = on_key
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def keyPressEvent(self, e: Any) -> None:
        if not self.on_key(e.key()):
            super().keyPressEvent(e)


class ReviewTab(QWidget):
    COLS = ("Từ", "Nghĩa", "Giải thích", "Câu ví dụ", "Tài liệu", "Đã ôn", "Hạn ôn")
    EDITABLE: ClassVar[dict[int, str]] = {0: "term", 1: "vi", 2: "definition", 3: "example"}
    GRADES = (("Quên (1)", "again"), ("Khó (2)", "hard"), ("Nhớ (3)", "good"), ("Dễ (4)", "easy"))

    def __init__(self, win: MainWindow) -> None:
        super().__init__()
        self.win = win
        self.doc = QComboBox()
        self.doc.currentIndexChanged.connect(lambda _i: self.refresh())
        self.mode = QComboBox()
        for label, key in (("Anh → Việt", "en-vi"), ("Việt → Anh", "vi-en"), ("Điền từ vào câu", "cloze")):
            self.mode.addItem(label, key)
        self.stats = _label("", wrap=False)
        start = QPushButton("Bắt đầu ôn")
        start.clicked.connect(self.start)
        top = QHBoxLayout()
        for w in (_label("Tài liệu:", False), self.doc, _label("Kiểu thẻ:", False), self.mode, start, self.stats):
            top.addWidget(w)
        top.addStretch(1)

        self.front = _label("Bấm 'Bắt đầu ôn' để ôn các từ đến hạn.")
        f = QFont(self.front.font())
        f.setPointSize(f.pointSize() + 5)
        self.front.setFont(f)
        self.front.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.answer = QLineEdit()
        self.answer.setPlaceholderText("Gõ đáp án (không bắt buộc) rồi Enter để xem đáp án")
        self.answer.returnPressed.connect(self.reveal)
        self.back = _label("")
        self.back.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.show_btn = QPushButton("Xem đáp án (Enter)")
        self.show_btn.clicked.connect(self.reveal)
        self.grade_btns: list[QPushButton] = []
        grade_row = QHBoxLayout()
        grade_row.addWidget(self.show_btn)
        for label, key in self.GRADES:
            b = QPushButton(label)
            b.clicked.connect(lambda _c=False, k=key: self.grade(k))
            b.setEnabled(False)
            self.grade_btns.append(b)
            grade_row.addWidget(b)
        card = _CardWidget(self._key)
        cl = QVBoxLayout(card)
        cl.addWidget(self.front)
        cl.addWidget(self.answer)
        cl.addWidget(self.back)
        cl.addLayout(grade_row)
        cl.addStretch(1)
        self.card_widget = card

        self.filter = QLineEdit()
        self.filter.setPlaceholderText("Lọc từ…")
        self.filter.textChanged.connect(lambda _t: self.refresh_table())
        self.table = QTableWidget(0, len(self.COLS))
        self.table.setHorizontalHeaderLabels(list(self.COLS))
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.table.itemChanged.connect(self._edited)
        bar = QHBoxLayout()
        for label, fn in (
            ("Thêm từ…", self._add),
            ("Xoá từ đã chọn", self._delete),
            ("Xuất Anki", lambda: self._export("anki")),
            ("Xuất CSV", lambda: self._export("csv")),
            ("Xuất Markdown", lambda: self._export("md")),
            ("Nhập CSV…", self._import),
        ):
            b = QPushButton(label)
            b.clicked.connect(fn)
            bar.addWidget(b)
        bar.addStretch(1)
        words = QWidget()
        wl = QVBoxLayout(words)
        wl.setContentsMargins(0, 0, 0, 0)
        wl.addWidget(self.filter)
        wl.addWidget(self.table, 1)
        wl.addLayout(bar)
        split = QSplitter(Qt.Orientation.Vertical)
        split.addWidget(card)
        split.addWidget(words)
        lay = QVBoxLayout(self)
        lay.addLayout(top)
        lay.addWidget(split, 1)

        self.queue: list[dict[str, Any]] = []
        self.card: dict[str, str] | None = None
        self.revealed = False
        self.done_count = 0
        self._loading = False
        self.reload_docs()

    @property
    def store(self) -> LearningStore | None:
        return self.win.store

    def doc_id(self) -> str | None:
        return self.doc.currentData() or None

    def reload_docs(self) -> None:
        if self.store is None:
            return
        cur = self.doc.currentData()
        self.doc.blockSignals(True)
        self.doc.clear()
        self.doc.addItem("Tất cả tài liệu", "")
        for d in self.store.list_documents():
            self.doc.addItem(str(d.get("title") or d.get("source_name"))[:60], d["id"])
        self.doc.setCurrentIndex(max(0, self.doc.findData(cur)))
        self.doc.blockSignals(False)
        self.refresh()

    def focus_document(self, doc_id: str) -> None:
        self.reload_docs()
        self.doc.setCurrentIndex(max(0, self.doc.findData(doc_id)))
        self.win.tabs.setCurrentWidget(self)

    def refresh(self) -> None:
        if self.store is None:
            return
        s = self.store.stats(self.doc_id())
        self.stats.setText(f"{s['total']} từ · {s['due']} đến hạn · {s['new']} mới · {s['learned']} đã thuộc")
        self.refresh_table()

    def refresh_table(self) -> None:
        if self.store is None:
            return
        titles = {d["id"]: str(d.get("title") or "") for d in self.store.list_documents()}
        words = self.store.list_words(self.doc_id(), self.filter.text())
        self._loading = True
        self.table.setRowCount(len(words))
        for r, w in enumerate(words):
            for c, key in self.EDITABLE.items():
                self.table.setItem(r, c, _item(w.get(key), editable=True, key=w["id"]))
            self.table.setItem(r, 4, _item(titles.get(w["doc_id"], "")[:40]))
            self.table.setItem(r, 5, _item(w.get("reps", 0)))
            self.table.setItem(r, 6, _item(time.strftime("%Y-%m-%d", time.localtime(w["due"]))))
        self._loading = False

    def _edited(self, it: QTableWidgetItem) -> None:
        if self._loading or self.store is None or it.column() not in self.EDITABLE:
            return
        wid = it.data(ID_ROLE)
        if wid:
            self.store.update_word(str(wid), **{self.EDITABLE[it.column()]: it.text().strip()[:1500]})

    # ---- phiên ôn tập
    def start(self) -> None:
        if self.store is None:
            return
        self.queue = self.store.due_words(doc_id=self.doc_id(), limit=50)
        self.done_count = 0
        if not self.queue:
            self.front.setText("Không có từ nào đến hạn ôn. Thêm từ bằng cách bôi đen trong tab Xem & sửa.")
            self.back.setText("")
            self.card = None
            self._buttons(False, False)
            return
        self._next()

    def _next(self) -> None:
        if not self.queue:
            self.front.setText(f"Xong phiên ôn: {self.done_count} thẻ. Hẹn gặp lại ở lần ôn tới!")
            self.back.setText("")
            self.card = None
            self._buttons(False, False)
            self.refresh()
            return
        w = self.queue[0]
        self.card = {**make_card(w, str(self.mode.currentData())), "id": w["id"]}
        self.front.setText(f"[{self.done_count + 1}/{self.done_count + len(self.queue)}]\n{self.card['front']}")
        self.back.setText("")
        self.answer.clear()
        self.revealed = False
        self._buttons(True, False)
        self.answer.setFocus()

    def _buttons(self, show: bool, grades: bool) -> None:
        self.show_btn.setEnabled(show)
        self.answer.setEnabled(show)
        for b in self.grade_btns:
            b.setEnabled(grades)

    def reveal(self) -> None:
        if self.card is None or self.revealed:
            return
        self.revealed = True
        typed = self.answer.text().strip()
        verdict = ""
        if typed and self.card.get("answer"):
            verdict = "✓ Khớp đáp án.\n" if check_answer(typed, self.card["answer"]) else f"✗ Bạn gõ: {typed}\n"
        self.back.setText(verdict + self.card["back"])
        self._buttons(False, True)
        self.card_widget.setFocus()

    def grade(self, key: str) -> None:
        if self.card is None or not self.revealed or self.store is None:
            return
        w = self.queue.pop(0)
        self.store.review(w["id"], key)
        if key == "again":  # quên: đưa lại cuối hàng đợi của phiên này
            self.queue.append(w)
        else:
            self.done_count += 1
        self._next()

    def _key(self, key: int) -> bool:
        """Phím tắt khi thẻ đang có focus (không chặn việc gõ trong ô đáp án/bảng từ)."""
        if key == Qt.Key.Key_Space and not self.revealed:
            self.reveal()
            return True
        grades = {Qt.Key.Key_1: "again", Qt.Key.Key_2: "hard", Qt.Key.Key_3: "good", Qt.Key.Key_4: "easy"}
        if self.revealed and key in grades:
            self.grade(grades[Qt.Key(key)])
            return True
        return False

    # ---- quản lý từ
    def _add(self) -> None:
        if self.store is None:
            return
        dlg = VocabDialog(self.win, {"page": None}, "Thêm từ")
        if dlg.exec() == QDialog.DialogCode.Accepted:
            r = dlg.result()
            self.store.add_word(r.pop("term"), r.pop("vi"), doc_id=self.doc_id() or "", **r)
            self.refresh()

    def _delete(self) -> None:
        if self.store is None:
            return
        ids = {
            str(self.table.item(i.row(), 0).data(ID_ROLE))
            for i in self.table.selectedIndexes()
            if self.table.item(i.row(), 0) is not None
        }
        if ids and safe.ask(self, "Xoá từ", f"Xoá {len(ids)} từ khỏi sổ?"):
            for wid in ids:
                self.store.delete_word(wid)
            self.refresh()

    def _export(self, fmt: str) -> None:
        if self.store is None:
            return
        ext = {
            "anki": ("tu-vung-anki.txt", "Anki (*.txt)"),
            "csv": ("tu-vung.csv", "CSV (*.csv)"),
            "md": ("tu-vung.md", "Markdown (*.md)"),
        }[fmt]
        p, _ = QFileDialog.getSaveFileName(self, "Xuất sổ từ", ext[0], ext[1])
        if not p:
            return
        from vietpdf.security.fsutil import atomic_write_text  # noqa: PLC0415

        atomic_write_text(Path(p), self.store.export(fmt, self.doc_id()))
        hint = " Trong Anki: File › Import, chọn kiểu 'Basic', phân tách bằng Tab." if fmt == "anki" else ""
        safe.info(self, "Đã xuất", f"Đã ghi {Path(p).name}.{hint}")

    def _import(self) -> None:
        if self.store is None:
            return
        p, _ = QFileDialog.getOpenFileName(self, "Nhập từ CSV (cột term, vi, definition, example)", "", "CSV (*.csv)")
        if not p:
            return
        try:
            n = self.store.import_csv(Path(p), self.doc_id() or "")
        except Exception as e:
            safe.warn(self, "Không nhập được", redact(f"{type(e).__name__}: {e}"))
            return
        self.refresh()
        safe.info(self, "Đã nhập", f"Đã nhập {n} từ.")
