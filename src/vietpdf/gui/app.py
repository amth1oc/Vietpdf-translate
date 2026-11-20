"""Giao diện desktop (PySide6): Dịch · Xem & sửa · Trợ lý đọc · Thuật ngữ · Thư viện · Ôn từ vựng.

Nguyên tắc an toàn: mọi nội dung từ PDF/LLM chỉ hiển thị dạng PLAIN TEXT (không render HTML).
Việc dịch chạy trong luồng nền (asyncio); đọc/ghi/render PDF chạy trong tiến trình con cô lập.
"""

from __future__ import annotations

import csv
import logging
import multiprocessing
import sys
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, Qt, QThread, Slot
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QMainWindow,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from vietpdf.config import ProviderSettings, config_dir, load_settings, save_settings
from vietpdf.gui import safe
from vietpdf.gui.learning import LibraryTab, ReviewTab, VocabDialog, prefill
from vietpdf.gui.viewer import SideBySideViewer
from vietpdf.gui.workers import AsyncJob, TranslateWorker, make_cache, make_translator
from vietpdf.security.logsafe import redact, setup_logging
from vietpdf.textfmt import to_text
from vietpdf.translate.providers import PROVIDERS

log = logging.getLogger("vietpdf.gui")
STAGES = {
    "check": "Kiểm tra",
    "extract": "Đọc PDF",
    "ocr": "OCR",
    "terms": "Thuật ngữ",
    "translate": "Dịch",
    "edit": "Biên tập",
    "fit": "Dàn chữ",
    "render": "Dựng PDF",
}
TR = {  # giao diện tiếng Việt / tiếng Anh
    "vi": {
        "translate": "Dịch",
        "viewer": "Xem && sửa",
        "assist": "Trợ lý đọc",
        "glossary": "Thuật ngữ",
        "library": "Thư viện",
        "review": "Ôn từ vựng",
        "provider": "Nhà cung cấp:",
        "model": "Mô hình:",
        "go": "Dịch",
        "stop": "Dừng",
    },
    "en": {
        "translate": "Translate",
        "viewer": "View && edit",
        "assist": "Reading assistant",
        "glossary": "Glossary",
        "library": "Library",
        "review": "Vocabulary review",
        "provider": "Provider:",
        "model": "Model:",
        "go": "Translate",
        "stop": "Stop",
    },
}


class _MainThreadCallback(QObject):
    """Nhận tín hiệu từ luồng nền và gọi callback trong LUỒNG GIAO DIỆN (QObject này sống ở luồng chính).
    Không nối tín hiệu luồng nền thẳng vào lambda: lambda sẽ chạy trong luồng nền và đụng vào widget."""

    def __init__(self, fn: Any, parent: QObject) -> None:
        super().__init__(parent)
        self.fn = fn

    @Slot(object)
    def call(self, value: Any) -> None:
        try:
            self.fn(value)
        finally:
            self.deleteLater()


def isolation_indicator() -> tuple[str, bool]:
    """(chữ hiển thị, có phải cô lập yếu) cho chỉ báo thường trực ở thanh trạng thái (R1-002, Windows/macOS)."""
    from vietpdf.core.sandbox import confine_summary, describe_confine, predicted_confine  # noqa: PLC0415

    seen = confine_summary()
    if seen.get("runs"):
        return describe_confine(seen), seen.get("level") != "strong"
    pred = predicted_confine()
    if pred["level"] == "strong":
        return f"Cô lập xử lý PDF: ĐẠT (seccomp, Landlock ABI {pred['landlock_abi']})", False
    return "Cô lập xử lý PDF: YẾU — " + "; ".join(pred["problems"]), True


def plain_label(text: str = "") -> QLabel:
    lb = QLabel(text)
    lb.setTextFormat(Qt.TextFormat.PlainText)
    return lb


class DropList(QListWidget):
    def __init__(self) -> None:
        super().__init__()
        self.setAcceptDrops(True)

    def dragEnterEvent(self, e: Any) -> None:
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    dragMoveEvent = dragEnterEvent

    def dropEvent(self, e: Any) -> None:
        for url in e.mimeData().urls():
            p = Path(url.toLocalFile())
            if p.suffix.lower() == ".pdf" and p.is_file():
                self.addItem(str(p))


class TermReviewDialog(QDialog):
    """Màn hình duyệt thuật ngữ trước khi dịch: chấp nhận, sửa, hoặc bỏ chọn từng mục."""

    COLS = ("Dùng", "Thuật ngữ", "Bản dịch", "Chính sách", "Số lần", "Định nghĩa", "Nguồn")

    def __init__(self, terms: list[dict[str, Any]], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Duyệt thuật ngữ trước khi dịch")
        self.resize(900, 520)
        self.terms = terms
        self.table = QTableWidget(len(terms), len(self.COLS))
        self.table.setHorizontalHeaderLabels(self.COLS)
        for r, t in enumerate(terms):
            chk = QTableWidgetItem()
            chk.setCheckState(Qt.CheckState.Checked if t.get("approved", True) else Qt.CheckState.Unchecked)
            self.table.setItem(r, 0, chk)
            for c, key in enumerate(["en", "vi", "policy", "count", "definition", "source"], 1):
                item = QTableWidgetItem(str(t.get(key, "")))
                if key in ("en", "count", "source"):
                    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self.table.setItem(r, c, item)
        self.table.resizeColumnsToContents()
        btns = QDialogButtonBox()
        btns.addButton("Áp dụng", QDialogButtonBox.ButtonRole.AcceptRole)
        btns.addButton("Bỏ qua, dùng đề xuất", QDialogButtonBox.ButtonRole.RejectRole)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.addWidget(plain_label("Bỏ chọn mục sai, sửa bản dịch nếu cần. Mục 'keep' sẽ giữ nguyên tiếng gốc."))
        lay.addWidget(self.table)
        lay.addWidget(btns)

    def result_terms(self) -> list[dict[str, Any]]:
        out = []
        for r, t in enumerate(self.terms):
            policy = self.table.item(r, 3).text().strip()
            out.append(
                {
                    **t,
                    "approved": self.table.item(r, 0).checkState() == Qt.CheckState.Checked,
                    "vi": self.table.item(r, 2).text().strip() or t["vi"],
                    "policy": policy if policy in ("translate", "keep") else t.get("policy", "translate"),
                    "definition": self.table.item(r, 5).text().strip(),
                    "source": "user",
                }
            )
        return out


class TranslateTab(QWidget):
    def __init__(self, win: MainWindow) -> None:
        super().__init__()
        self.win = win
        s = win.settings
        self.files = DropList()
        self.pages = QLineEdit()
        self.pages.setPlaceholderText("vd. 1-5,8 (để trống = tất cả)")
        self.style = QComboBox()
        for k, v in (
            ("de_doc", "Dễ đọc"),
            ("sat_nghia", "Sát nghĩa (học thuật)"),
            ("hanh_chinh", "Hành chính–pháp lý"),
        ):
            self.style.addItem(v, k)
        self.quality = QComboBox()
        for k, v in (
            ("fast", "Nhanh (1 lượt)"),
            ("standard", "Chuẩn (biên tập đoạn nghi sai)"),
            ("high", "Cao (biên tập mọi đoạn)"),
        ):
            self.quality.addItem(v, k)
        self.quality.setCurrentIndex(max(0, self.quality.findData(s.quality)))
        self.source_lang = QComboBox()
        for code in ("en", "zh", "ja", "ko", "fr", "de", "es", "ru"):
            self.source_lang.addItem(code, code)
        self.checks = {
            k: QCheckBox(t)
            for k, t in (
                ("dual", "Xuất bản song ngữ"),
                ("review_terms", "Duyệt thuật ngữ trước khi dịch"),
                ("annotate_terms", "Ghi chú thuật ngữ trong PDF"),
                ("appendix", "Phụ lục chú giải"),
                ("ocr", "OCR trang scan"),
                ("translate_tables", "Dịch cả ô bảng"),
                ("include_hidden", "Dịch cả chữ không nhìn thấy (không khuyến nghị)"),
                ("chapter_summaries", "Tóm tắt từng chương làm ngữ cảnh"),
                ("offline", "Offline nghiêm ngặt"),
                ("library", "Lưu vào thư viện"),
                ("study", "Tạo bản học song ngữ (EN+VI)"),
            )
        }
        self.checks["library"].setChecked(True)
        self.gloss = QComboBox()
        for k, v in (
            ("document", "Lần đầu trong tài liệu"),
            ("paragraph", "Lần đầu trong mỗi đoạn (học từ)"),
            ("off", "Không ghi"),
        ):
            self.gloss.addItem(v, k)
        self.gloss.setToolTip("Ghi thuật ngữ tiếng Anh trong ngoặc sau bản dịch: “cơ chế chú ý (attention)”")
        self.checks["review_terms"].setChecked(s.review_terms)
        self.checks["ocr"].setChecked(True)
        self.checks["offline"].setChecked(s.offline)
        self.progress = QProgressBar()
        self.stage = plain_label("Sẵn sàng")
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)

        add, rm, est = QPushButton("Thêm PDF…"), QPushButton("Xoá khỏi hàng đợi"), QPushButton("Ước tính chi phí")
        add.clicked.connect(self._add)
        rm.clicked.connect(lambda: [self.files.takeItem(self.files.row(i)) for i in self.files.selectedItems()])
        est.clicked.connect(self._estimate)
        self.go, self.stop = QPushButton(win.t("go")), QPushButton(win.t("stop"))
        self.go.clicked.connect(self._start)
        self.stop.clicked.connect(self._cancel)
        self.stop.setEnabled(False)

        box = QGroupBox("Tuỳ chọn dịch")
        grid = QGridLayout(box)
        grid.addWidget(plain_label("Trang:"), 0, 0)
        grid.addWidget(self.pages, 0, 1)
        grid.addWidget(plain_label("Văn phong:"), 0, 2)
        grid.addWidget(self.style, 0, 3)
        grid.addWidget(plain_label("Chất lượng:"), 1, 0)
        grid.addWidget(self.quality, 1, 1)
        grid.addWidget(plain_label("Ngôn ngữ nguồn:"), 1, 2)
        grid.addWidget(self.source_lang, 1, 3)
        grid.addWidget(plain_label("Ghi tiếng Anh bên cạnh:"), 2, 0)
        grid.addWidget(self.gloss, 2, 1)
        for i, cb in enumerate(self.checks.values()):
            grid.addWidget(cb, 3 + i // 4, i % 4)
        row = QHBoxLayout()
        for w in (add, rm, est):
            row.addWidget(w)
        row.addStretch(1)
        row.addWidget(self.go)
        row.addWidget(self.stop)
        lay = QVBoxLayout(self)
        lay.addWidget(plain_label("Kéo-thả file PDF vào danh sách:"))
        lay.addWidget(self.files, 2)
        lay.addWidget(box)
        lay.addLayout(row)
        lay.addWidget(self.stage)
        lay.addWidget(self.progress)
        lay.addWidget(self.log, 1)
        self._thread: QThread | None = None
        self._worker: TranslateWorker | None = None

    def append(self, text: str) -> None:
        self.log.appendPlainText(redact(text))

    def _add(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(self, "Chọn PDF", "", "PDF (*.pdf)")
        for p in paths:
            self.files.addItem(p)

    def options(self) -> dict[str, Any]:
        o = {k: cb.isChecked() for k, cb in self.checks.items()}
        o.update(
            pages=self.pages.text().strip(),
            style=self.style.currentData(),
            quality=self.quality.currentData(),
            source_lang=self.source_lang.currentData(),
            gloss=self.gloss.currentData(),
        )
        return o

    def _estimate(self) -> None:
        files = [Path(self.files.item(i).text()) for i in range(self.files.count())]
        if not files:
            return
        pid, model = self.win.current_provider()

        async def job() -> Any:
            from vietpdf.core.pipeline import PipelineOptions, run_pipeline  # noqa: PLC0415

            t = make_translator(pid, model or "-", self.checks["offline"].isChecked(), require_consent=False)
            try:
                out = []
                for f in files:
                    r = await run_pipeline(
                        f,
                        None,
                        t,
                        PipelineOptions(
                            dry_run=True, page_spec=self.pages.text() or None, quality=self.quality.currentData()
                        ),
                    )
                    out.append({"file": f.name, **r.estimate})
                return out
            finally:
                await t.aclose()

        self.win.run_job(job, lambda res: self.append("Ước tính: " + to_text(res)))

    def _start(self) -> None:
        files = [Path(self.files.item(i).text()) for i in range(self.files.count())]
        pid, model = self.win.current_provider()
        if not files or not model:
            safe.info(self, "Thiếu thông tin", "Hãy thêm file PDF và chọn mô hình.")
            return
        if not self.win.confirm_privacy(pid):
            return
        if not self.win.confirm_isolation():
            return
        self.win.remember(pid, model, self.options())
        self._thread = QThread()
        self._worker = TranslateWorker(files, pid, model, self.options())
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.progress.connect(self._on_progress)
        self._worker.file_done.connect(self._file_done)
        self._worker.saved.connect(self._saved)
        self._worker.failed.connect(self._failed)
        self._worker.review_terms.connect(self._review)
        self._worker.finished.connect(self._done)
        self.go.setEnabled(False)
        self.stop.setEnabled(True)
        self._thread.start()

    @Slot(str, str)
    def _file_done(self, _f: str, msg: str) -> None:
        self.append(f"✔ {msg}")

    @Slot(object)
    def _saved(self, info: dict[str, Any]) -> None:
        """Chạy ở luồng giao diện: lưu file vừa dịch vào thư viện (kho học tập chỉ dùng ở luồng chính)."""
        if not info.get("library") or self.win.store is None:
            return
        from vietpdf.library.store import save_translation  # noqa: PLC0415

        files = {k: Path(v) for k, v in info.items() if k in ("output", "dual", "ir", "report", "study") and v}
        try:
            doc_id = save_translation(self.win.store, Path(info["input"]), files)
        except Exception as e:
            self.append(redact(f"Không lưu được vào thư viện: {type(e).__name__}: {e}"))
            return
        self.append(f"  → đã lưu vào Thư viện (id {doc_id}).")
        self.win.library.refresh()
        self.win.review.reload_docs()

    @Slot(str, str)
    def _failed(self, f: str, msg: str) -> None:
        self.append(f"✘ {Path(f).name}: {msg}")

    @Slot(object)
    def _review(self, terms: list[dict[str, Any]]) -> None:
        if not self._worker:
            return
        if not terms:
            self._worker.submit_review(terms)
            return
        dlg = TermReviewDialog(terms, self)
        accepted = dlg.exec() == QDialog.DialogCode.Accepted
        self._worker.submit_review(dlg.result_terms() if accepted else terms)

    def _cancel(self) -> None:
        if self._worker:
            self._worker.cancel()

    @Slot(str, float, str)
    def _on_progress(self, stage: str, frac: float, msg: str) -> None:
        self.stage.setText(f"{STAGES.get(stage, stage)} — {msg}")
        self.progress.setValue(int(frac * 100))

    @Slot()
    def _done(self) -> None:
        if self._thread:
            self._thread.quit()
            self._thread.wait()
        self.go.setEnabled(True)
        self.stop.setEnabled(False)
        self.stage.setText("Xong")
        self.win.refresh_isolation()


class AssistTab(QWidget):
    """Trợ lý đọc hiểu: thẻ tổng quan, hỏi đáp có trang, giải thích, phản biện, tái hiện, tô màu đọc lướt."""

    def __init__(self, win: MainWindow) -> None:
        super().__init__()
        self.win = win
        self.output = QPlainTextEdit()
        self.output.setReadOnly(True)
        self.question = QLineEdit()
        self.question.setPlaceholderText("Hỏi về tài liệu (câu trả lời kèm số trang, kiểm tra trích dẫn có thật)…")
        self.question.returnPressed.connect(self._ask)
        self.kind = QComboBox()
        for k, v in (("paragraph", "Đoạn văn"), ("formula", "Công thức"), ("figure", "Bảng/hình")):
            self.kind.addItem(v, k)
        self.selected = plain_label("Chưa chọn đoạn (bấm vào một đoạn trong tab Xem & sửa).")
        self.selected_id = ""
        buttons = QHBoxLayout()
        for label, fn in (
            ("Thẻ tổng quan", "overview"),
            ("Kiến thức nền", "prerequisites"),
            ("Phản biện", "critique"),
            ("Tái hiện", "reproduce"),
            ("Tô màu đọc lướt", "skim"),
        ):
            b = QPushButton(label)
            b.clicked.connect(lambda _c=False, fn=fn: self._run(fn))
            buttons.addWidget(b)
        explain = QPushButton("Giải thích đoạn đã chọn")
        explain.clicked.connect(self._explain)
        ex_row = QHBoxLayout()
        ex_row.addWidget(self.selected, 1)
        ex_row.addWidget(self.kind)
        ex_row.addWidget(explain)
        lay = QVBoxLayout(self)
        lay.addLayout(buttons)
        lay.addWidget(self.question)
        lay.addLayout(ex_row)
        lay.addWidget(plain_label("Mọi nội dung dưới đây là 'Giải thích của AI'; phần ngoài bài được ghi rõ."))
        lay.addWidget(self.output, 1)

    def select(self, seg_id: str) -> None:
        self.selected_id = seg_id
        self.selected.setText(f"Đoạn {seg_id}: {self.win.viewer.segment_text(seg_id)[:120]}")

    def _assistant(self) -> Any:
        from vietpdf.assistant.reader import Assistant  # noqa: PLC0415

        ir = self.win.viewer.ir
        if ir is None:
            raise RuntimeError("Hãy mở một file đã dịch trong tab Xem & sửa")
        pid, model = self.win.current_provider()
        if not self.win.confirm_privacy(pid):
            raise RuntimeError("Chưa đồng ý gửi tài liệu tới nhà cung cấp này")
        return Assistant(
            ir, make_translator(pid, model, self.win.translate.checks["offline"].isChecked()), make_cache()
        )

    def _call(self, coro_fn: Any, kind: str = "", question: str = "") -> None:
        try:
            a = self._assistant()
        except Exception as e:
            safe.info(self, "Trợ lý đọc", str(e))
            return

        async def job() -> Any:
            try:
                return await coro_fn(a)
            finally:
                await a.translator.aclose()
                a.cache.close()

        def done(res: Any) -> None:
            self.output.setPlainText(to_text(res))
            self.win.save_assist(kind, question, res)

        self.output.setPlainText("Đang xử lý…")
        self.win.run_job(job, done)

    def _run(self, fn: str) -> None:
        if fn == "skim":
            self._call(self._skim, "skim")
            return
        actions = {
            "overview": lambda a: a.overview(),
            "prerequisites": lambda a: a.prerequisites(),
            "critique": lambda a: a.critique(),
            "reproduce": lambda a: a.reproduce(),
        }
        if fn in actions:
            self._call(actions[fn], fn)

    async def _skim(self, a: Any) -> Any:
        import asyncio  # noqa: PLC0415

        from vietpdf.core import render  # noqa: PLC0415
        from vietpdf.core.sandbox import run_to_outputs  # noqa: PLC0415

        items = await a.skim()
        src = self.win.viewer.src_path
        if src is None:
            return items
        out = src.with_name(src.stem + ".skim.pdf")
        n = await asyncio.to_thread(
            run_to_outputs,
            render.highlight_original,
            lambda st: (str(src), str(st[0]), items, None),
            [out],
            timeout=120,
        )
        return {"Đã tô": n, "File": str(out), "Câu": [f"[{i['kind']}] {i['sentence']}" for i in items]}

    def _ask(self) -> None:
        q = self.question.text().strip()
        if q:
            self._call(lambda a: a.ask(q), "ask", q)

    def _explain(self) -> None:
        if not self.selected_id:
            return
        text = self.win.viewer.segment_text(self.selected_id)
        kind = self.kind.currentData()
        self._call(lambda a: a.explain(text, kind=kind), "explain", text[:500])


class GlossaryTab(QWidget):
    """Glossary của người dùng (tầng 1): sửa trực tiếp, nhập/xuất CSV."""

    COLS = ("english", "vietnamese", "ghi_chu", "chinh_sach")

    def __init__(self) -> None:
        super().__init__()
        self.path = config_dir() / "glossary" / "cua_toi.csv"
        self.table = QTableWidget(0, len(self.COLS))
        self.table.setHorizontalHeaderLabels(["Thuật ngữ", "Bản dịch", "Ghi chú", "Chính sách (translate/keep)"])
        add, rm, save, imp, exp = (QPushButton(t) for t in ("Thêm dòng", "Xoá dòng", "Lưu", "Nhập CSV…", "Xuất CSV…"))
        add.clicked.connect(lambda: self.table.insertRow(self.table.rowCount()))
        rm.clicked.connect(
            lambda: [
                self.table.removeRow(r) for r in sorted({i.row() for i in self.table.selectedItems()}, reverse=True)
            ]
        )
        save.clicked.connect(self.save)
        imp.clicked.connect(self._import)
        exp.clicked.connect(self._export)
        row = QHBoxLayout()
        for b in (add, rm, save, imp, exp):
            row.addWidget(b)
        lay = QVBoxLayout(self)
        lay.addWidget(plain_label(f"Glossary của tôi (ưu tiên cao nhất): {self.path}"))
        lay.addWidget(self.table)
        lay.addLayout(row)
        self.load(self.path)

    def load(self, path: Path) -> None:
        from vietpdf.translate.glossary import load_csv  # noqa: PLC0415

        if not path.is_file():
            return
        entries = load_csv(path)
        self.table.setRowCount(0)
        for e in entries:
            r = self.table.rowCount()
            self.table.insertRow(r)
            for c, v in enumerate((e.en, e.vi, e.note, e.policy)):
                self.table.setItem(r, c, QTableWidgetItem(v))

    def rows(self) -> list[list[str]]:
        out = []
        for r in range(self.table.rowCount()):
            vals = [(self.table.item(r, c).text().strip() if self.table.item(r, c) else "") for c in range(4)]
            if vals[0] and vals[1]:
                out.append(vals)
        return out

    def save(self, path: Path | None = None) -> None:
        target = path or self.path
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("w", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            w.writerow(self.COLS)
            w.writerows(self.rows())

    def _import(self) -> None:
        p, _ = QFileDialog.getOpenFileName(self, "Nhập glossary", "", "CSV (*.csv)")
        if p:
            self.load(Path(p))

    def _export(self) -> None:
        p, _ = QFileDialog.getSaveFileName(self, "Xuất glossary", "glossary.csv", "CSV (*.csv)")
        if p:
            self.save(Path(p))


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        from vietpdf.config import ConfigError  # noqa: PLC0415

        self._config_error: str | None = None
        try:
            self.settings = load_settings()
        except ConfigError as e:  # R1-006: báo lỗi, dùng mặc định trong phiên này, KHÔNG ghi đè file
            self._config_error = str(e)
            from vietpdf.config import Settings  # noqa: PLC0415

            self.settings = Settings()
        self.setWindowTitle("VietPDF Translate")
        self.resize(1100, 760)
        self._jobs: list[tuple[QThread, AsyncJob]] = []
        self.store: Any = None
        try:
            from vietpdf.library.store import open_store  # noqa: PLC0415

            self.store = open_store()
        except Exception as e:  # kho học tập hỏng không được chặn việc dịch
            log.warning("Không mở được thư viện/sổ từ: %s", redact(f"{type(e).__name__}: {e}"))

        self.provider = QComboBox()
        for pid, spec in PROVIDERS.items():
            if pid != "fake" or "--dev" in sys.argv:
                self.provider.addItem(spec.display, pid)
        self.provider.setCurrentIndex(max(self.provider.findData(self.settings.provider), 0))
        self.model = QComboBox()
        self.model.setEditable(True)
        self.model.setMinimumWidth(240)
        models_btn, key_btn, open_btn = (
            QPushButton("Lấy danh sách mô hình"),
            QPushButton("API key…"),
            QPushButton("Mở file đã dịch…"),
        )
        models_btn.clicked.connect(self._load_models)
        key_btn.clicked.connect(self._set_key)
        open_btn.clicked.connect(self._open_translated)
        self.theme = QComboBox()
        self.theme.addItem("Sáng", "light")
        self.theme.addItem("Tối", "dark")
        self.theme.setCurrentIndex(max(0, self.theme.findData(self.settings.theme)))
        self.theme.currentIndexChanged.connect(lambda _i: self.apply_theme(self.theme.currentData()))
        self.ui_lang = QComboBox()
        self.ui_lang.addItem("Tiếng Việt", "vi")
        self.ui_lang.addItem("English", "en")
        self.ui_lang.setCurrentIndex(max(0, self.ui_lang.findData(self.settings.ui_lang)))
        self.ui_lang.currentIndexChanged.connect(self._lang_changed)
        self.provider.currentIndexChanged.connect(self._provider_changed)

        top = QHBoxLayout()
        for w in (
            plain_label(self.t("provider")),
            self.provider,
            plain_label(self.t("model")),
            self.model,
            models_btn,
            key_btn,
            open_btn,
        ):
            top.addWidget(w)
        top.addStretch(1)
        top.addWidget(self.theme)
        top.addWidget(self.ui_lang)

        self.translate = TranslateTab(self)
        self.viewer = SideBySideViewer()
        self.assist = AssistTab(self)
        self.glossary = GlossaryTab()
        self.library = LibraryTab(self)
        self.review = ReviewTab(self)
        self.viewer.segment_selected.connect(self.assist.select)
        self.viewer.vocab_requested.connect(self.add_vocab)
        self.viewer.lookup_requested.connect(self.lookup_vocab)
        self.viewer.status.connect(lambda m: self.statusBar().showMessage(m, 8000))
        self.viewer.rerender_btn.clicked.connect(self._rerender)
        self.tabs = QTabWidget()
        for w, key in (
            (self.translate, "translate"),
            (self.viewer, "viewer"),
            (self.assist, "assist"),
            (self.glossary, "glossary"),
            (self.library, "library"),
            (self.review, "review"),
        ):
            self.tabs.addTab(w, self.t(key))
        central = QWidget()
        lay = QVBoxLayout(central)
        lay.addLayout(top)
        lay.addWidget(self.tabs, 1)
        self.setCentralWidget(central)
        self.isolation_label = plain_label()
        self.statusBar().addPermanentWidget(self.isolation_label)
        self.refresh_isolation()
        if self._config_error:
            from PySide6.QtCore import QTimer  # noqa: PLC0415

            msg = self._config_error
            QTimer.singleShot(0, lambda: safe.warn(self, "Cấu hình không hợp lệ", msg))
        self._provider_changed()
        self.apply_theme(self.settings.theme)

    # ------------------------------------------------------------------ tiện ích
    def t(self, key: str) -> str:
        return TR.get(self.settings.ui_lang, TR["vi"]).get(key, key)

    def current_provider(self) -> tuple[str, str]:
        return str(self.provider.currentData()), self.model.currentText().strip()

    def run_job(self, factory: Any, on_done: Any) -> None:
        thread, job = QThread(), AsyncJob(factory)
        job.moveToThread(thread)
        thread.started.connect(job.run)
        ok_cb = _MainThreadCallback(on_done, self)
        err_cb = _MainThreadCallback(lambda m: safe.warn(self, "Lỗi", str(m)), self)
        job.done.connect(ok_cb.call)
        job.error.connect(err_cb.call)
        job.finished.connect(thread.quit)
        thread.finished.connect(self.refresh_isolation)
        thread.finished.connect(lambda: self._jobs.remove((thread, job)) if (thread, job) in self._jobs else None)
        self._jobs.append((thread, job))
        thread.start()

    def save_settings(self) -> None:
        """Lưu cấu hình; file hiện có hỏng thì KHÔNG ghi đè, chỉ báo (R1-006)."""
        from vietpdf.config import ConfigError  # noqa: PLC0415

        try:
            save_settings(self.settings)
        except ConfigError as e:
            self.statusBar().showMessage(f"Không lưu cấu hình: {e}"[:300], 10000)

    def save_consent(self, cid: str) -> None:
        from vietpdf.config import ConfigError, save_consent  # noqa: PLC0415

        try:
            save_consent(cid)
        except ConfigError as e:
            self.statusBar().showMessage(f"Không lưu đồng ý (chỉ áp dụng phiên này): {e}"[:300], 10000)

    def refresh_isolation(self) -> None:
        text, weak = isolation_indicator()
        self.isolation_label.setText(("⚠ " if weak else "") + text[:200])
        self.isolation_label.setToolTip(text)
        self.isolation_label.setStyleSheet("color: #b00020; font-weight: bold;" if weak else "")

    def confirm_isolation(self) -> bool:
        """Hệ thống không cô lập được tiến trình xử lý PDF (vd. kernel thiếu Landlock): hỏi MỘT lần mỗi phiên.
        Lựa chọn không được lưu vĩnh viễn (R1-002)."""
        from vietpdf.core.sandbox import isolation_needs_consent, set_allow_weak  # noqa: PLC0415

        problems = isolation_needs_consent()
        if not problems:
            return True
        ok = safe.ask(
            self,
            "Cô lập tiến trình xử lý PDF yếu",
            "Hệ thống không cô lập được tiến trình đọc PDF:\n- "
            + "\n- ".join(problems)
            + "\n\nNếu PDF được chế tạo để khai thác lỗi của thư viện đọc PDF, nó có thể đọc/ghi file của bạn."
            "\n\nTiếp tục trong phiên này (chỉ với PDF nguồn tin cậy)? Chọn 'No' để huỷ.",
        )
        if ok:
            set_allow_weak(True)
            self.refresh_isolation()
        return ok

    def remember(self, pid: str, model: str, options: dict[str, Any]) -> None:
        self.settings.provider = pid
        self.settings.offline = options.get("offline", False)
        self.settings.quality = options.get("quality", "standard")
        self.settings.review_terms = options.get("review_terms", True)
        ps = self.settings.providers.get(pid) or ProviderSettings()
        ps.model = model
        self.settings.providers[pid] = ps
        self.save_settings()

    def confirm_privacy(self, pid: str) -> bool:
        """Hỏi đồng ý trước lần gửi tài liệu đầu tiên tới từng nhà cung cấp/host (L1-008); lưu lựa chọn."""
        from vietpdf.translate.providers import consent_id, needs_consent, privacy_text  # noqa: PLC0415

        ps = self.settings.providers.get(pid) or ProviderSettings()
        base_url = ps.base_url or None
        if self.translate.checks["offline"].isChecked() or not needs_consent(pid, base_url):
            return True
        cid = consent_id(pid, base_url)
        if self.settings.privacy_consents.get(cid):
            return True
        if safe.ask(self, "Quyền riêng tư", privacy_text(pid, base_url) + "\n\nĐồng ý gửi?"):
            self.settings.privacy_consents[cid] = True
            self.save_consent(cid)
            return True
        return False

    def apply_theme(self, theme: str) -> None:
        app = QApplication.instance()
        if not isinstance(app, QApplication):
            return
        app.setStyle("Fusion")
        pal = QPalette()
        if theme == "dark":
            for role, color in (
                (QPalette.ColorRole.Window, "#2b2b2b"),
                (QPalette.ColorRole.WindowText, "#e6e6e6"),
                (QPalette.ColorRole.Base, "#1f1f1f"),
                (QPalette.ColorRole.Text, "#e6e6e6"),
                (QPalette.ColorRole.Button, "#3a3a3a"),
                (QPalette.ColorRole.ButtonText, "#e6e6e6"),
                (QPalette.ColorRole.Highlight, "#3d7bd9"),
            ):
                pal.setColor(role, QColor(color))
        app.setPalette(pal)
        self.settings.theme = theme
        self.save_settings()

    def _lang_changed(self) -> None:
        self.settings.ui_lang = self.ui_lang.currentData()
        self.save_settings()
        self.statusBar().showMessage("Ngôn ngữ giao diện sẽ áp dụng khi mở lại ứng dụng.", 6000)

    def _provider_changed(self) -> None:
        pid = str(self.provider.currentData())
        ps = self.settings.providers.get(pid)
        self.model.clear()
        if ps and ps.model:
            self.model.addItem(ps.model)
        if pid == "fake":
            self.model.addItem("fake-1")

    def _set_key(self) -> None:
        from vietpdf.security.secrets import SecretStoreError, set_api_key  # noqa: PLC0415

        pid = str(self.provider.currentData())
        if not PROVIDERS[pid].needs_key:
            safe.info(self, "API key", "Nhà cung cấp này không cần API key.")
            return
        value, ok = QInputDialog.getText(
            self, "API key", f"API key cho {PROVIDERS[pid].display}:", QLineEdit.EchoMode.Password
        )
        if ok and value:
            try:
                set_api_key(pid, value)
                self.statusBar().showMessage("Đã lưu API key vào kho mật khẩu của hệ điều hành.", 6000)
            except (SecretStoreError, ValueError) as e:
                safe.warn(self, "Không lưu được", str(e))

    def _load_models(self) -> None:
        pid = str(self.provider.currentData())

        async def fetch() -> list[str]:
            t = make_translator(pid, "-", self.translate.checks["offline"].isChecked(), require_consent=False)
            try:
                return await t.list_models()
            finally:
                await t.aclose()

        def done(models: list[str]) -> None:
            cur = self.model.currentText()
            self.model.clear()
            self.model.addItems(models)
            if cur in models:
                self.model.setCurrentText(cur)

        self.run_job(fetch, done)

    def open_in_viewer(self, src: Path, out: Path, ir: Path) -> None:
        self.remember_page()
        try:
            self.viewer.open(src, out, ir)
        except Exception as e:
            safe.warn(self, "Không mở được", redact(f"{type(e).__name__}: {e}"))
            return
        self.tabs.setCurrentWidget(self.viewer)
        doc = self.store.get_document(self.viewer.doc_id) if self.store is not None else None
        if doc is not None:
            self.store.touch(self.viewer.doc_id)
            self.viewer.goto_page(int(doc.get("last_page") or 0))
            self.library.refresh()

    def open_document(self, doc: dict[str, Any]) -> None:
        """Mở lại tài liệu từ thư viện, tới đúng trang đang đọc dở."""
        paths = {k: Path(v) for k, v in doc.get("paths", {}).items()}
        need = [k for k in ("source", "output", "ir") if not paths.get(k, Path()).is_file()]
        if need:
            safe.warn(
                self,
                "Thiếu file",
                "Không tìm thấy: " + ", ".join(need) + ". File có thể đã bị di chuyển; "
                "hãy dùng 'Thêm file đã dịch…' để lưu lại.",
            )
            return
        self.open_in_viewer(paths["source"], paths["output"], paths["ir"])

    def remember_page(self) -> None:
        if self.store is None or not self.viewer.doc_id:
            return
        if self.store.get_document(self.viewer.doc_id) is not None:
            self.store.update_document(self.viewer.doc_id, last_page=self.viewer.current_page())

    def save_assist(self, kind: str, question: str, result: Any) -> None:
        if self.store is None or not self.viewer.doc_id or not kind:
            return
        import json  # noqa: PLC0415

        try:
            data = json.loads(json.dumps(result, ensure_ascii=False, default=str))
            self.store.add_assist(self.viewer.doc_id, kind, question, data)
        except (TypeError, ValueError):
            return

    # ------------------------------------------------------------------ từ vựng
    def add_vocab(self, text: str, page: int) -> None:
        """Bôi đen → "Thêm vào từ vựng": có nghĩa sẵn (bảng thuật ngữ/bài) thì thêm ngay, chưa có thì mở hộp thoại."""
        if self.store is None:
            safe.warn(self, "Sổ từ", "Không mở được sổ từ vựng.")
            return
        data = prefill(self, text, page)
        if not data["term"]:
            return
        if not data["vi"]:
            self.lookup_vocab(text, page, data)
            return
        r = {k: v for k, v in data.items() if k not in ("term", "vi")}
        self.store.add_word(data["term"], data["vi"], doc_id=self.viewer.doc_id, **r)
        self.statusBar().showMessage(f"Đã thêm “{data['term']}” → {data['vi']} vào từ vựng ôn tập.", 6000)
        self.review.refresh()
        self.library.refresh()

    def lookup_vocab(self, text: str, page: int, data: dict[str, Any] | None = None) -> None:
        if self.store is None:
            return
        data = data or prefill(self, text, page)
        if not data["term"]:
            return
        dlg = VocabDialog(self, data, "Tra nghĩa & thêm vào từ vựng")
        if dlg.exec() == QDialog.DialogCode.Accepted:
            r = dlg.result()
            term, vi = r.pop("term"), r.pop("vi")
            self.store.add_word(term, vi, doc_id=self.viewer.doc_id, **r)
            self.statusBar().showMessage(f"Đã thêm “{term}” vào từ vựng ôn tập.", 6000)
            self.review.refresh()
            self.library.refresh()

    def _open_translated(self) -> None:
        p, _ = QFileDialog.getOpenFileName(self, "Chọn file PDF GỐC đã dịch", "", "PDF (*.pdf)")
        if not p:
            return
        src = Path(p)
        out, ir = src.with_name(src.stem + ".vi.pdf"), src.with_name(src.stem + ".ir.json")
        if not out.exists() or not ir.exists():
            safe.info(self, "Chưa dịch", "Không tìm thấy .vi.pdf / .ir.json cạnh file gốc.")
            return
        self.open_in_viewer(src, out, ir)

    def _rerender(self) -> None:
        v = self.viewer
        if not (v.src_path and v.out_path and v.ir_path):
            return

        async def job() -> Path:
            from vietpdf.core.pipeline import rerender  # noqa: PLC0415

            return await rerender(v.src_path, v.ir_path, v.out_path)  # type: ignore[arg-type]

        def done(_p: Path) -> None:
            v.open(v.src_path, v.out_path, v.ir_path)  # type: ignore[arg-type]
            self.statusBar().showMessage("Đã dựng lại PDF từ bản sửa tay.", 6000)

        self.run_job(job, done)

    def closeEvent(self, e: Any) -> None:
        from vietpdf.core.sandbox import cancel_all  # noqa: PLC0415

        cancel_all()  # không để tiến trình con xử lý PDF chạy tiếp sau khi đóng cửa sổ
        self.remember_page()
        self.viewer.close_renderer()
        if self.store is not None:
            self.store.close()
        super().closeEvent(e)


def main() -> int:
    multiprocessing.freeze_support()
    setup_logging()
    import contextlib  # noqa: PLC0415

    from vietpdf.cli import startup_hardening  # noqa: PLC0415
    from vietpdf.config import ConfigError  # noqa: PLC0415

    # settings.json hỏng: MainWindow hiện hộp thoại và không ghi đè file (R1-006); phần còn lại của startup đã chạy
    with contextlib.suppress(ConfigError):
        startup_hardening()  # gồm dọn thư mục tạm của job bị bỏ lại (R1-001)
    app = QApplication(sys.argv)
    w = MainWindow()
    w.show()
    code = int(app.exec())
    from vietpdf.core.sandbox import cancel_all  # noqa: PLC0415

    cancel_all(stop_new=True)  # app đã thoát vòng sự kiện: việc nền còn xếp hàng không được mở tiến trình con mới
    return code


if __name__ == "__main__":
    sys.exit(main())
