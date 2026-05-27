"""
MainWindow — okno aplikacji PyQt6.

To jest "biuro" użytkownika:
- natywne okno Windowsa/macOS/Linux
- w środku QWebEngineView (wbudowana przeglądarka Chromium)
- ikona w zasobniku systemowym (system tray) z menu
- ładuje naszą stronę HTML z localhost

QWebEngineView to jak Chrome, tyle że wbudowany w naszą aplikację.
Użytkownik widzi normalny interfejs webowy ale bez paska adresu przeglądarki.
"""

from __future__ import annotations
import sys
import os

from pathlib import Path
from typing import TYPE_CHECKING

from PyQt6.QtWidgets import (
    QMainWindow,
    QSystemTrayIcon,
    QMenu,
    QApplication,
    QFileDialog,
    QWidget,
)
from PyQt6.QtWebEngineWidgets import QWebEngineView # type: ignore
from PyQt6.QtWebEngineCore import QWebEngineProfile, QWebEnginePage  # type: ignore
from PyQt6.QtWebChannel import QWebChannel
from PyQt6.QtCore import QUrl, Qt, QObject, pyqtSlot
from PyQt6.QtGui import QIcon, QAction

from server_thread import PORT

if TYPE_CHECKING:
    from download_manager import DownloadManager

# Shown immediately in QWebEngineView before the FastAPI server is ready.
# Uses the real WP logo via a file:// baseUrl set in show_loading() so the
# <img src="wp_logo.png"> reference resolves without embedding base64.
_LOADING_HTML = """<!DOCTYPE html>
<html><head><meta charset="UTF-8">
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#FBFBFA;display:flex;align-items:center;justify-content:center;
     height:100vh;font-family:system-ui,sans-serif;-webkit-font-smoothing:antialiased}
.w{display:flex;flex-direction:column;align-items:center;gap:10px}
.logo{width:96px;height:auto;display:block}
.nm{font-size:17px;font-weight:800;color:#111;letter-spacing:-0.04em;margin-top:4px}
.sb{font-size:12px;color:#787774;font-weight:500;transition:opacity .2s;text-align:center;min-height:18px}
.tr{width:160px;height:2px;background:#EAEAEA;border-radius:99px;overflow:hidden;margin-top:10px}
.fl{height:100%;width:45%;background:#E3000F;border-radius:99px;
    animation:s 1.1s cubic-bezier(.4,0,.2,1) infinite}
@keyframes s{0%{transform:translateX(-200%)}100%{transform:translateX(320%)}}
</style>
<script>
function updateStatus(msg){var el=document.getElementById('loading-status');if(el)el.textContent=msg;}
</script>
</head>
<body><div class="w">
<img src="wp_logo.png" class="logo" alt="WP">
<div class="nm">WP Downloader</div>
<div class="sb" id="loading-status">Uruchamianie…</div>
<div class="tr"><div class="fl"></div></div>
</div></body></html>"""


def get_resource_path(relative_path: str) -> str:
    """Zwraca poprawną ścieżkę do zasobów, działającą także po spakowaniu do EXE."""
    if hasattr(sys, '_MEIPASS'):
        # PyInstaller tworzy folder tymczasowy i tam przechowuje dane
        return os.path.join(sys._MEIPASS, relative_path)
    return os.path.join(os.path.abspath("."), relative_path)

LOGO_PATH = get_resource_path(os.path.join("static", "wp_logo.png"))


class WebBridge(QObject):
    """Mostek QWebChannel: strona HTML wywołuje natywne okno wyboru folderu."""

    def __init__(self, main_window: QWidget):
        super().__init__()
        self._main_window = main_window

    @pyqtSlot(str, str, result=str)
    def pick_save_file(self, suggested_name: str, file_filter: str) -> str:
        """Otwiera okno 'Zapisz jako' i zwraca pełną ścieżkę."""
        path, _ = QFileDialog.getSaveFileName(
            self._main_window,
            "Zapisz plik jako...",
            os.path.join(str(Path.home() / "Downloads"), suggested_name),
            file_filter
        )
        return path or ""
    
    @pyqtSlot(str)
    def open_external_link(self, url: str):
        """Otwiera podany URL w domyślnej przeglądarce systemowej."""
        from PyQt6.QtGui import QDesktopServices
        QDesktopServices.openUrl(QUrl(url))

    @pyqtSlot(str)
    def resize_window_preset(self, preset: str) -> None:
        """Zmienia rozmiar okna według presetu: compact / normal / large."""
        sizes = {"compact": (900, 640), "normal": (1080, 780), "large": (1380, 900)}
        w, h = sizes.get(preset, (1080, 780))
        self._main_window.resize(w, h)
        self._main_window._center_on_screen()

    @pyqtSlot(bool)
    def set_minimize_to_tray(self, enabled: bool) -> None:
        """Ustawia czy zamknięcie okna minimalizuje do zasobnika zamiast kończyć program."""
        self._main_window.minimize_to_tray = enabled


class TrayIcon(QSystemTrayIcon):
    """
    Ikona w zasobniku systemowym (ten pasek z zegarkiem/WiFi).
    
    Daje użytkownikowi menu: Pokaż / Minimalizuj / Wyjdź
    nawet gdy okno jest ukryte.
    """

    def __init__(self, window: "MainWindow", icon: QIcon):
        super().__init__(icon)

        self.window = window

        # Menu kontekstowe (pojawia się po prawym kliknięciu)
        menu = QMenu()

        show_action = QAction("Pokaż okno", menu)
        show_action.triggered.connect(self._show_window)

        hide_action = QAction("Minimalizuj do zasobnika", menu)
        hide_action.triggered.connect(window.hide)

        quit_action = QAction("Zamknij WP Downloader", menu)
        quit_action.triggered.connect(QApplication.quit)

        menu.addAction(show_action)
        menu.addAction(hide_action)
        menu.addSeparator()
        menu.addAction(quit_action)

        self.setContextMenu(menu)
        self.setToolTip("WP Downloader v1.0")

        # Podwójne kliknięcie na ikonę → pokaż okno
        self.activated.connect(self._on_activated)

        self.show()

    def _on_activated(self, reason):
        """Obsługuje kliknięcia w ikonę zasobnika."""
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self._show_window()

    def _show_window(self):
        """Pokazuje i przenosi okno na wierzch."""
        self.window.show()
        self.window.raise_()
        self.window.activateWindow()


class MainWindow(QMainWindow):
    """
    Główne okno aplikacji.
    
    QMainWindow to gotowy szablon okna Qt z paskiem menu, statusu itp.
    My używamy go prosto — tylko wbudowana przeglądarka w środku.
    """

    def __init__(self, app: QApplication, download_manager: "DownloadManager"):
        super().__init__()

        self.app = app
        self.download_manager = download_manager

        # Tytuł okna i rozmiar
        self.setWindowTitle("WP Downloader v1.0")
        self.resize(1080, 780)
        self.setMinimumSize(820, 580)

        # Wyśrodkuj okno na ekranie
        self._center_on_screen()

        # Ikona aplikacji (logo WP)
        icon = QIcon(LOGO_PATH)
        self.setWindowIcon(icon)
        app.setWindowIcon(icon)  # ikona na pasku zadań

        # QWebEngineView — wbudowana przeglądarka
        self.web_view = QWebEngineView()
        self.web_view.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)

        # Persistent profile so localStorage survives between sessions.
        # The default profile is off-the-record (incognito) and loses all
        # localStorage on exit — a named profile with an explicit storage path
        # fixes that.
        data_dir = os.path.join(os.path.expanduser("~"), ".wp_downloader")
        self._profile = QWebEngineProfile("WPDownloader", self.web_view)
        self._profile.setPersistentStoragePath(data_dir)
        self._profile.setPersistentCookiesPolicy(
            QWebEngineProfile.PersistentCookiesPolicy.AllowPersistentCookies
        )
        _page = QWebEnginePage(self._profile, self.web_view)
        self.web_view.setPage(_page)

        self._web_bridge = WebBridge(self)
        self._web_channel = QWebChannel(self.web_view.page())
        self._web_channel.registerObject("wpBridge", self._web_bridge)
        self.web_view.page().setWebChannel(self._web_channel)

        # Ustaw web_view jako centralny widget okna
        self.setCentralWidget(self.web_view)

        # Tray/close behaviour (can be toggled from Settings tab)
        self.minimize_to_tray: bool = False

        # Ikona w zasobniku
        self.tray = TrayIcon(self, icon)

        # Menu zgodne z dokumentacją: globalny folder zapisu (okno dialogowe)
        menubar = self.menuBar()
        file_menu = menubar.addMenu("Plik")
        folder_action = QAction("Ustaw folder pobierań…", self)
        folder_action.triggered.connect(self._pick_download_folder)
        file_menu.addAction(folder_action)

    def _pick_download_folder(self):
        path = QFileDialog.getExistingDirectory(
            self,
            "Wybierz folder pobierań",
            str(Path.home() / "Downloads"),
        )
        if path:
            self.download_manager.set_output_dir(path)

    def _center_on_screen(self):
        """Wyśrodkowuje okno na ekranie."""
        screen = self.app.primaryScreen().geometry()
        x = (screen.width() - self.width()) // 2
        y = (screen.height() - self.height()) // 2
        self.move(x, y)

    def show_loading(self) -> None:
        """Wyświetla natywny ekran ładowania zanim serwer będzie gotowy."""
        static_dir = get_resource_path("static")
        base_url = QUrl.fromLocalFile(static_dir + "/")
        self.web_view.setHtml(_LOADING_HTML, base_url)

    def load_app(self) -> None:
        """Ładuje stronę aplikacji z lokalnego serwera."""
        url = QUrl(f"http://127.0.0.1:{PORT}")
        self.web_view.load(url)

    def closeEvent(self, event):
        if self.minimize_to_tray:
            self.hide()
            event.ignore()
        else:
            QApplication.quit()
            event.accept()
