"""Trình xem song song gốc/dịch: cuộn và phóng to đồng bộ, rê chuột tô sáng đoạn tương ứng,
nhấp đúp để sửa bản dịch, đoạn cần xem lại có viền vàng/đỏ.

Học tiếng Anh: bật "Bôi đen chữ" (hoặc giữ Shift) rồi kéo chuột quanh từ/cụm trên trang → menu "Thêm vào từ vựng
ôn tập / Tra nghĩa / Sao chép". Bảng "Đoạn đang chọn" bên dưới hiện câu gốc tiếng Anh + bản dịch + thuật ngữ của
đoạn; có thể bôi đen ngay trong đó để thêm từ.

An toàn: trang PDF được render trong tiến trình con thường trực (PageRenderer), không mở PDF trong
tiến trình giao diện. Nội dung từ PDF/LLM chỉ hiển thị dạng văn bản thuần."""

from __future__ import annotations

import functools
import html
import re
from pathlib import Path
from typing import Any

from PySide6.QtCore import QPoint, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QGuiApplication, QImage, QPen, QPixmap
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGraphicsRectItem,
    QGraphicsScene,
    QGraphicsView,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from vietpdf.core.ir import DocumentIR, Segment
from vietpdf.core.sandbox import PageRenderer

PAGE_GAP = 16.0
QA_PREFIXES = ("qa:", "term:missing", "security:", "overflow")


def seg_color(seg: Segment) -> QColor | None:
    if seg.status == "failed" or "overflow" in seg.flags:
        return QColor(220, 40, 40)
    if any(f.startswith(QA_PREFIXES) for f in seg.flags):
        return QColor(230, 170, 0)
    return None


class SegmentItem(QGraphicsRectItem):
    def __init__(self, seg: Segment, rect: QRectF, view: PdfPane) -> None:
        super().__init__(rect)
        self.seg, self.view = seg, view
        self.setAcceptHoverEvents(True)
        self._base = seg_color(seg)
        self.reset()
        tip = ", ".join(seg.flags) or seg.status
        # tooltip Qt tự đoán rich text: ép văn bản thuần bằng cách thoát HTML trong một khối <span> (L1-021)
        self.setToolTip("<span>" + html.escape(f"{seg.id} — {tip}") + "</span>")

    def reset(self) -> None:
        if self._base is not None:
            self.setPen(QPen(self._base, 1.5))
        else:
            self.setPen(QPen(Qt.PenStyle.NoPen))
        self.setBrush(QBrush(Qt.BrushStyle.NoBrush))

    def highlight(self, on: bool) -> None:
        if on:
            self.setBrush(QBrush(QColor(60, 140, 255, 45)))
        else:
            self.reset()

    def hoverEnterEvent(self, e: Any) -> None:
        self.view.hovered.emit(self.seg.id, True)

    def hoverLeaveEvent(self, e: Any) -> None:
        self.view.hovered.emit(self.seg.id, False)

    def mouseDoubleClickEvent(self, e: Any) -> None:
        self.view.edit_requested.emit(self.seg.id)

    def mousePressEvent(self, e: Any) -> None:
        if self.view.select_mode or e.modifiers() & Qt.KeyboardModifier.ShiftModifier:
            e.ignore()  # để PdfPane vẽ vùng bôi đen
            return
        self.view.selected.emit(self.seg.id)


class PdfPane(QGraphicsView):
    hovered = Signal(str, bool)
    edit_requested = Signal(str)
    selected = Signal(str)
    text_selected = Signal(int, object, object)  # trang (0-based), (x0,y0,x1,y1) điểm PDF, vị trí chuột toàn cục

    def __init__(self, renderer: PageRenderer) -> None:
        super().__init__()
        self.renderer = renderer
        self.setScene(QGraphicsScene(self))
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.items_by_id: dict[str, SegmentItem] = {}
        self.path: str = ""
        self.pages: list[tuple[float, float, float]] = []  # (top, w, h) theo điểm PDF
        self.rendered: set[int] = set()
        self.zoom = 1.2
        self.select_mode = False
        self._band: QGraphicsRectItem | None = None
        self._band_origin = QPointF()
        self.verticalScrollBar().valueChanged.connect(lambda _v: self.render_visible())

    # ---- bôi đen vùng chữ (kéo chuột khi bật chế độ chọn hoặc giữ Shift)
    def _selecting(self, e: Any) -> bool:
        return bool(self.select_mode or e.modifiers() & Qt.KeyboardModifier.ShiftModifier)

    def mousePressEvent(self, e: Any) -> None:
        if e.button() == Qt.MouseButton.LeftButton and self._selecting(e) and self.pages:
            self._band_origin = self.mapToScene(e.position().toPoint())
            self._band = QGraphicsRectItem(QRectF(self._band_origin, self._band_origin))
            self._band.setPen(QPen(QColor(40, 110, 230), 1, Qt.PenStyle.DashLine))
            self._band.setBrush(QBrush(QColor(40, 110, 230, 40)))
            self._band.setZValue(5)
            self.scene().addItem(self._band)
            e.accept()
            return
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e: Any) -> None:
        if self._band is not None:
            self._band.setRect(QRectF(self._band_origin, self.mapToScene(e.position().toPoint())).normalized())
            e.accept()
            return
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e: Any) -> None:
        if self._band is None:
            super().mouseReleaseEvent(e)
            return
        rect = self._band.rect()
        self.scene().removeItem(self._band)
        self._band = None
        e.accept()
        hit = self.page_rect(rect)
        if hit is not None:
            self.text_selected.emit(hit[0], hit[1], e.globalPosition().toPoint())

    def page_rect(self, rect: QRectF) -> tuple[int, tuple[float, float, float, float]] | None:
        """Đổi vùng chọn (toạ độ cảnh) sang (trang, hình chữ nhật theo điểm PDF của trang đó)."""
        if rect.width() < 3 or rect.height() < 3:
            return None
        cy = rect.center().y()
        for i, (top, w, h) in enumerate(self.pages):
            if top <= cy <= top + h:
                x0, x1 = max(0.0, rect.left()), min(w, rect.right())
                y0, y1 = max(0.0, rect.top() - top), min(h, rect.bottom() - top)
                if x1 - x0 >= 2 and y1 - y0 >= 2:
                    return i, (x0, y0, x1, y1)
        return None

    def current_page(self) -> int:
        y = self.mapToScene(self.viewport().rect().center()).y()
        for i, (top, _w, h) in enumerate(self.pages):
            if y <= top + h + PAGE_GAP / 2:
                return i
        return max(0, len(self.pages) - 1)

    def goto_page(self, page: int) -> None:
        if 0 <= page < len(self.pages):
            top, w, _h = self.pages[page]
            half = self.viewport().height() / max(self.zoom, 0.1) / 2
            self.centerOn(w / 2, top + half - 8)
            self.render_visible()

    def load(self, path: str, sizes: list[tuple[float, float]], segments: list[Segment] | None) -> None:
        self.scene().clear()
        self.items_by_id.clear()
        self.rendered.clear()
        self.path = path
        self.pages = []
        top = 0.0
        for w, h in sizes:
            self.pages.append((top, w, h))
            self.scene().addRect(QRectF(0, top, w, h), QPen(QColor(180, 180, 180)), QBrush(QColor(255, 255, 255)))
            top += h + PAGE_GAP
        for seg in segments or []:
            if seg.page < len(self.pages):
                t = self.pages[seg.page][0]
                x0, y0, x1, y1 = seg.bbox
                item = SegmentItem(seg, QRectF(x0, t + y0, x1 - x0, y1 - y0), self)
                item.setZValue(2)
                self.scene().addItem(item)
                self.items_by_id[seg.id] = item
        self.resetTransform()
        self.scale(self.zoom, self.zoom)
        self.render_visible()

    def render_visible(self) -> None:
        if not self.path:
            return
        r = self.mapToScene(self.viewport().rect()).boundingRect()
        for i, (top, w, h) in enumerate(self.pages):
            if i in self.rendered or top > r.bottom() + 400 or top + h < r.top() - 400:
                continue
            try:
                png = self.renderer.render(self.path, i, min(2.5, self.zoom * 1.5))
            except Exception:  # noqa: S112 - trang lỗi: bỏ qua, hiển thị khung trắng
                continue
            img = QImage.fromData(png, "PNG")
            pix = self.scene().addPixmap(QPixmap.fromImage(img))
            pix.setScale(w / max(img.width(), 1))
            pix.setPos(0, top)
            pix.setZValue(1)
            self.rendered.add(i)

    def set_zoom(self, z: float) -> None:
        self.zoom = max(0.3, min(z, 4.0))
        self.resetTransform()
        self.scale(self.zoom, self.zoom)
        self.render_visible()

    def wheelEvent(self, e: Any) -> None:
        if e.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self.window().findChild(SideBySideViewer).zoom_both(1.1 if e.angleDelta().y() > 0 else 1 / 1.1)
        else:
            super().wheelEvent(e)


class EditDialog(QDialog):
    """Sửa tay bản dịch một đoạn. Ký hiệu ⟦M…⟧ (công thức) và thẻ ⟦B⟧/⟦I⟧ phải được giữ nguyên."""

    def __init__(self, seg: Segment, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Sửa đoạn {seg.id} — trang {seg.page + 1}")
        self.resize(720, 520)
        self.seg = seg
        src = QPlainTextEdit(seg.source)
        src.setReadOnly(True)
        self.edit = QPlainTextEdit(seg.translation or "")
        flags = QLabel(", ".join(seg.flags) or "không có cờ")
        flags.setTextFormat(Qt.TextFormat.PlainText)
        self.term_en, self.term_vi = QLineEdit(), QLineEdit()
        self.term_en.setPlaceholderText("thuật ngữ gốc (tuỳ chọn)")
        self.term_vi.setPlaceholderText("bản dịch chuẩn → lưu vào glossary của tôi")
        form = QFormLayout()
        form.addRow("Bản gốc:", src)
        form.addRow("Bản dịch:", self.edit)
        form.addRow("Cờ kiểm tra:", flags)
        row = QHBoxLayout()
        row.addWidget(self.term_en)
        row.addWidget(self.term_vi)
        form.addRow("Học từ chỉnh sửa:", row)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.addLayout(form)
        lay.addWidget(buttons)

    def result_text(self) -> str:
        return self.edit.toPlainText().strip()


def _short(text: str, n: int = 40) -> str:
    t = " ".join(text.split())
    return (t[: n - 1] + "…") if len(t) > n else t


class SegmentDetail(QWidget):
    """Bảng "Đoạn đang chọn": câu gốc (EN) + bản dịch (VI) + thuật ngữ của đoạn. Văn bản thuần, chọn được."""

    def __init__(self, viewer: SideBySideViewer) -> None:
        super().__init__()
        self.viewer = viewer
        self.title = QLabel("Bấm vào một đoạn để xem câu gốc tiếng Anh và bản dịch ở đây.")
        self.title.setTextFormat(Qt.TextFormat.PlainText)
        self.en, self.vi = QPlainTextEdit(), QPlainTextEdit()
        for box, tip in ((self.en, "Bản gốc (tiếng Anh)"), (self.vi, "Bản dịch (tiếng Việt)")):
            box.setReadOnly(True)
            box.setPlaceholderText(tip)
            box.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            box.customContextMenuRequested.connect(functools.partial(self._context_menu, box))
        self.terms = QLabel("")
        self.terms.setTextFormat(Qt.TextFormat.PlainText)
        self.terms.setWordWrap(True)
        add, look = QPushButton("Thêm phần bôi đen vào từ vựng"), QPushButton("Tra nghĩa phần bôi đen…")
        add.clicked.connect(lambda: self._act("add"))
        look.clicked.connect(lambda: self._act("lookup"))
        row = QHBoxLayout()
        row.addWidget(self.title, 1)
        row.addWidget(add)
        row.addWidget(look)
        texts = QHBoxLayout()
        texts.addWidget(self.en)
        texts.addWidget(self.vi)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 4, 0, 0)
        lay.addLayout(row)
        lay.addLayout(texts, 1)
        lay.addWidget(self.terms)
        self.page = 0

    def show_segment(self, seg: Segment, terms: list[tuple[str, str]]) -> None:
        self.page = seg.page + 1
        self.title.setText(f"Đoạn {seg.id} — trang {seg.page + 1}. Bôi đen từ/cụm rồi bấm nút hoặc chuột phải.")
        self.en.setPlainText(seg.plain_source())
        self.vi.setPlainText(seg.plain_translation() if seg.translation else "(chưa dịch)")
        self.terms.setText("Thuật ngữ trong đoạn: " + "; ".join(f"{en} → {vi}" for en, vi in terms) if terms else "")

    def selected_text(self) -> str:
        for box in (self.en, self.vi):
            t = box.textCursor().selectedText().replace("\u2029", " ")
            if t.strip():
                return t
        return ""

    def _act(self, kind: str) -> None:
        text = self.selected_text()
        if not text.strip():
            self.viewer.status.emit("Hãy bôi đen một từ hoặc cụm từ trong ô câu gốc/bản dịch trước.")
            return
        sig = self.viewer.vocab_requested if kind == "add" else self.viewer.lookup_requested
        sig.emit(text, self.page)

    def _context_menu(self, box: QPlainTextEdit, pos: QPoint) -> None:
        menu = box.createStandardContextMenu()
        text = box.textCursor().selectedText().replace("\u2029", " ")
        if text.strip():
            menu.addSeparator()
            for label, kind in (("Thêm vào từ vựng ôn tập", "add"), ("Tra nghĩa…", "lookup")):
                act = menu.addAction(f"{label}: “{_short(text).replace('&', '&&')}”")
                sig = self.viewer.vocab_requested if kind == "add" else self.viewer.lookup_requested
                act.triggered.connect(functools.partial(sig.emit, text, self.page))
        menu.exec(box.mapToGlobal(pos))
        menu.deleteLater()


class SideBySideViewer(QWidget):
    segment_selected = Signal(str)
    status = Signal(str)
    vocab_requested = Signal(str, int)  # văn bản bôi đen, trang (1-based)
    lookup_requested = Signal(str, int)
    document_opened = Signal(str)  # id tài liệu (24 ký tự đầu SHA-256)

    def __init__(self) -> None:
        super().__init__()
        self.renderer = PageRenderer()
        self.left, self.right = PdfPane(self.renderer), PdfPane(self.renderer)
        self.ir: DocumentIR | None = None
        self.ir_path: Path | None = None
        self.src_path: Path | None = None
        self.out_path: Path | None = None
        self.doc_id = ""
        self.glossary: Any = None
        self.dirty = False
        self._syncing = False
        for a, b in ((self.left, self.right), (self.right, self.left)):
            a.verticalScrollBar().valueChanged.connect(lambda v, a=a, b=b: self._sync(a, b))
            a.hovered.connect(self._hover)
            a.edit_requested.connect(self._edit)
            a.selected.connect(self.segment_selected.emit)
            a.selected.connect(self.show_detail)
            a.text_selected.connect(functools.partial(self._on_text_selected, a))
        self.info = QLabel("Mở một file đã dịch để xem song song.")
        self.info.setTextFormat(Qt.TextFormat.PlainText)
        zin, zout = QPushButton("+"), QPushButton("\u2212")
        zin.clicked.connect(lambda: self.zoom_both(1.2))
        zout.clicked.connect(lambda: self.zoom_both(1 / 1.2))
        self.select_btn = QPushButton("Bôi đen chữ")
        self.select_btn.setCheckable(True)
        self.select_btn.setToolTip("Bật rồi kéo chuột quanh từ/cụm để thêm vào từ vựng (hoặc giữ Shift khi kéo).")
        self.select_btn.toggled.connect(self.set_select_mode)
        self.rerender_btn = QPushButton("Xuất lại PDF")
        self.rerender_btn.setEnabled(False)
        top = QHBoxLayout()
        top.addWidget(self.info, 1)
        top.addWidget(self.select_btn)
        top.addWidget(zout)
        top.addWidget(zin)
        top.addWidget(self.rerender_btn)
        panes = QWidget()
        pl = QHBoxLayout(panes)
        pl.setContentsMargins(0, 0, 0, 0)
        pl.addWidget(self.left)
        pl.addWidget(self.right)
        self.detail = SegmentDetail(self)
        split = QSplitter(Qt.Orientation.Vertical)
        split.addWidget(panes)
        split.addWidget(self.detail)
        split.setStretchFactor(0, 4)
        split.setStretchFactor(1, 1)
        lay = QVBoxLayout(self)
        lay.addLayout(top)
        lay.addWidget(split, 1)

    def set_select_mode(self, on: bool) -> None:
        for p in (self.left, self.right):
            p.select_mode = on
            p.setDragMode(QGraphicsView.DragMode.NoDrag if on else QGraphicsView.DragMode.ScrollHandDrag)
            p.viewport().setCursor(Qt.CursorShape.IBeamCursor if on else Qt.CursorShape.OpenHandCursor)

    def open(self, src: Path, out: Path, ir_path: Path) -> None:
        self.ir = DocumentIR.model_validate_json(ir_path.read_text("utf-8"))
        self.src_path, self.out_path, self.ir_path = src, out, ir_path
        self.doc_id = self.ir.source_sha256[:24]
        try:
            from vietpdf.translate.glossary import build_glossary  # noqa: PLC0415
            from vietpdf.translate.terms import doc_terms_to_entries  # noqa: PLC0415

            self.glossary = build_glossary(self.ir.domains, doc_terms_to_entries(self.ir.terms))
        except Exception:  # glossary người dùng hỏng không được chặn việc mở tài liệu
            self.glossary = None
        sizes = [(p.width, p.height) for p in self.ir.pages]
        segs = [s for s in self.ir.segments() if s.status in ("translated", "failed")]
        self.left.load(str(src), self._all_sizes(src, sizes), segs)
        self.right.load(str(out), self._all_sizes(out, sizes), [s for s in segs if s.status == "translated"])
        n_flag = sum(1 for s in segs if seg_color(s) is not None)
        self.info.setText(
            f"{src.name} — {len(segs)} đoạn, {n_flag} cần xem lại (viền vàng/đỏ). Nhấp đúp đoạn bên phải để sửa; "
            "bật 'Bôi đen chữ' để thêm từ vựng."
        )
        if self.ir.provider == "fake":
            self.info.setText(
                "⚠ BẢN DỊCH GIẢ ĐỂ KIỂM THỬ (provider fake) — chữ ngẫu nhiên, không có nghĩa. " + self.info.text()
            )
        self.document_opened.emit(self.doc_id)

    def current_page(self) -> int:
        return self.left.current_page()

    def goto_page(self, page: int) -> None:
        self.left.goto_page(page)
        self._sync(self.left, self.right)

    def segment(self, seg_id: str) -> Segment | None:
        if self.ir is None:
            return None
        return next((s for s in self.ir.segments() if s.id == seg_id), None)

    def segment_terms(self, seg: Segment, limit: int = 12) -> list[tuple[str, str]]:
        if self.glossary is None:
            return []
        src = seg.plain_source()
        out: list[tuple[str, str]] = []
        for e in self.glossary.entries:
            if len(out) >= limit:
                break
            if e.en and re.search(r"(?<!\w)" + re.escape(e.en) + r"(?!\w)", src, re.IGNORECASE):
                out.append((e.en, e.en if e.policy == "keep" else e.vi))
        return out

    def show_detail(self, seg_id: str) -> None:
        seg = self.segment(seg_id)
        if seg is not None:
            self.detail.show_segment(seg, self.segment_terms(seg))

    # ---- bôi đen trên trang PDF
    def _on_text_selected(self, pane: PdfPane, page: int, rect: Any, gpos: Any) -> None:
        try:
            text = self.renderer.text_in_rect(pane.path, page, tuple(rect))
        except Exception as e:
            self.status.emit(f"Không đọc được chữ trong vùng chọn: {type(e).__name__}")
            return
        if not text.strip():
            self.status.emit("Không có chữ trong vùng chọn (trang scan hoặc hình ảnh?).")
            return
        menu = QMenu(self)
        for label, fn in self.selection_actions(text, page + 1):
            act = menu.addAction(label.replace("&", "&&"))
            act.triggered.connect(fn)
        menu.exec(gpos)
        menu.deleteLater()

    def selection_actions(self, text: str, page: int) -> list[tuple[str, Any]]:
        """Các lựa chọn cho vùng bôi đen (tách riêng để kiểm thử không cần mở menu)."""
        short = _short(text)
        return [
            (f"Thêm vào từ vựng ôn tập: “{short}”", functools.partial(self.vocab_requested.emit, text, page)),
            ("Tra nghĩa && xem ngữ cảnh…", functools.partial(self.lookup_requested.emit, text, page)),
            ("Sao chép", functools.partial(self._copy, text)),
        ]

    def _copy(self, text: str) -> None:
        cb = QGuiApplication.clipboard()
        if cb is not None:
            cb.setText(text)
        self.status.emit("Đã sao chép.")

    def _all_sizes(self, path: Path, sizes: list[tuple[float, float]]) -> list[tuple[float, float]]:
        # IR chỉ có các trang đã dịch; giả định cùng kích thước theo thứ tự (đủ cho hiển thị)
        if self.ir is None:
            return sizes
        by_no = {p.number: (p.width, p.height) for p in self.ir.pages}
        default = sizes[0] if sizes else (595.0, 842.0)
        return [by_no.get(i, default) for i in range(self.ir.page_count)]

    def zoom_both(self, factor: float) -> None:
        for p in (self.left, self.right):
            p.set_zoom(p.zoom * factor)

    def _sync(self, a: PdfPane, b: PdfPane) -> None:
        if self._syncing:
            return
        self._syncing = True
        sa, sb = a.verticalScrollBar(), b.verticalScrollBar()
        if sa.maximum() > 0:
            sb.setValue(int(sa.value() / sa.maximum() * sb.maximum()))
        self._syncing = False

    def _hover(self, seg_id: str, on: bool) -> None:
        for p in (self.left, self.right):
            item = p.items_by_id.get(seg_id)
            if item is not None:
                item.highlight(on)

    def _edit(self, seg_id: str) -> None:
        seg = self.segment(seg_id)
        if seg is None:
            return
        dlg = EditDialog(seg, self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        from vietpdf.core.protect import STRICT_TOKEN_RE  # noqa: PLC0415

        new = dlg.result_text()
        if sorted(STRICT_TOKEN_RE.findall(new)) != sorted(STRICT_TOKEN_RE.findall(seg.translation or "")):
            self.status.emit("Không lưu: bản sửa làm mất/thêm ký hiệu công thức ⟦M…⟧.")
            return
        seg.translation = new
        seg.status = "translated"
        seg.flags = sorted({f for f in seg.flags if not f.startswith(("qa:", "term:"))} | {"edited-by-user"})
        if dlg.term_en.text().strip() and dlg.term_vi.text().strip():
            from vietpdf.translate.glossary import append_user_term  # noqa: PLC0415

            append_user_term(dlg.term_en.text(), dlg.term_vi.text())
            self.status.emit(f"Đã lưu '{dlg.term_en.text()} → {dlg.term_vi.text()}' vào glossary của tôi.")
        self.save_ir()
        self.show_detail(seg_id)

    def save_ir(self) -> None:
        if self.ir is None or self.ir_path is None:
            return
        from vietpdf.security.fsutil import atomic_write_text  # noqa: PLC0415

        atomic_write_text(self.ir_path, self.ir.model_dump_json(indent=1))
        self.dirty = True
        self.rerender_btn.setEnabled(True)
        self.status.emit("Đã lưu bản sửa vào IR. Bấm 'Xuất lại PDF' để dựng lại (không gọi API).")

    def segment_text(self, seg_id: str) -> str:
        seg = self.segment(seg_id)
        return seg.plain_source() if seg else ""

    def close_renderer(self) -> None:
        self.renderer.close()
