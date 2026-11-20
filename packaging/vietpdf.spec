# -*- mode: python -*-
# PyInstaller spec: build GUI (cửa sổ) + CLI (console) trong cùng một thư mục phân phối.
# Chạy: uv run pyinstaller packaging/vietpdf.spec --noconfirm --clean
# Chỉ đóng gói các module Qt cần dùng; tài nguyên (font, glossary) kèm SHA256SUMS và được kiểm lúc chạy.
import sys
from pathlib import Path

ROOT = Path(SPECPATH).parent  # noqa: F821 - SPECPATH do PyInstaller cung cấp
SRC = ROOT / "src"
ASSETS = SRC / "vietpdf" / "assets"

datas = [(str(ASSETS), "vietpdf/assets")]
try:  # OCR tuỳ chọn: đóng gói kèm mô hình PP-OCR nằm trong gói rapidocr (đã khoá hash trong uv.lock)
    from PyInstaller.utils.hooks import collect_data_files

    datas += collect_data_files("rapidocr")
except Exception:  # noqa: BLE001, S110
    pass
excludes = [
    "tkinter", "unittest", "pydoc", "test",
    "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.Qt3DCore", "PySide6.QtMultimedia",
    "PySide6.QtQuick", "PySide6.QtQml", "PySide6.QtNetwork", "PySide6.QtWebSockets", "PySide6.QtBluetooth",
]
hidden = ["vietpdf.core.extract", "vietpdf.core.render", "vietpdf.core.ocr", "vietpdf.core.layout_model",
          "vietpdf.core.study", "vietpdf.library.store", "vietpdf.library.lookup", "vietpdf.library.review",
          "vietpdf.gui.learning", "cryptography.hazmat.primitives.ciphers.aead",
          "keyring.backends.Windows", "keyring.backends.macOS", "keyring.backends.SecretService",
          "keyring.backends.kwallet", "keyring.backends.libsecret"]

gui_a = Analysis([str(SRC / "vietpdf" / "gui" / "app.py")], pathex=[str(SRC)], datas=datas,  # noqa: F821
                 hiddenimports=hidden, excludes=excludes, noarchive=False)
cli_a = Analysis([str(SRC / "vietpdf" / "__main__.py")], pathex=[str(SRC)], datas=datas,  # noqa: F821
                 hiddenimports=hidden, excludes=excludes + ["PySide6"], noarchive=False)

gui_pyz = PYZ(gui_a.pure)  # noqa: F821
cli_pyz = PYZ(cli_a.pure)  # noqa: F821

gui_exe = EXE(gui_pyz, gui_a.scripts, [], exclude_binaries=True, name="VietPDF", console=False,  # noqa: F821
              upx=False, codesign_identity=None)
cli_exe = EXE(cli_pyz, cli_a.scripts, [], exclude_binaries=True, name="vietpdf", console=True,  # noqa: F821
              upx=False)  # UPX tắt: file nén UPX hay bị antivirus báo nhầm là mã độc

coll = COLLECT(gui_exe, gui_a.binaries, gui_a.datas, cli_exe, cli_a.binaries, cli_a.datas,  # noqa: F821
               upx=False, name="VietPDF")

if sys.platform == "darwin":
    app = BUNDLE(coll, name="VietPDF.app", bundle_identifier="io.github.vietpdf.translate")  # noqa: F821
