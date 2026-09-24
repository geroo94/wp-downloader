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
import logging
import subprocess
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
from PyQt6.QtCore import QUrl, Qt, QObject, QEvent, pyqtSignal, pyqtSlot
from PyQt6.QtGui import QIcon, QAction, QKeySequence, QShortcut

from server_thread import PORT

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from download_manager import DownloadManager

def get_resource_path(relative_path: str) -> str:
    """Zwraca poprawną ścieżkę do zasobów, działającą także po spakowaniu do EXE."""
    if hasattr(sys, '_MEIPASS'):
        # PyInstaller tworzy folder tymczasowy i tam przechowuje dane
        return os.path.join(sys._MEIPASS, relative_path)
    return os.path.join(os.path.abspath("."), relative_path)

LOGO_PATH = get_resource_path(os.path.join("static", "wp_logo.png"))


class _DropForwarder(QObject):
    """Natywny filtr zdarzeń przechwytujący drag & drop nad QWebEngineView.

    PO CO: Chromium osadzony w Qt nie przekazuje lokalnej ścieżki pliku do
    JavaScriptu. Zweryfikowane empirycznie na Qt 6.11 — w zdarzeniu `drop`
    JS dostaje obiekt File z poprawną nazwą i rozmiarem, ale:
        * `'path' in file` === false  (`file.path` to rozszerzenie Electrona,
          nie ma go w czystym Chromium),
        * `dataTransfer.getData('text/uri-list')` jest PUSTE dla plików.
    Dlatego dropzone'y w Fast Cutterze i Transkrypcji ratowały się otwarciem
    natywnego okna wyboru pliku — co użytkownik widzi jako „przeciągnięcie
    pliku otwiera eksplorator zamiast go wczytać".

    Qt widzi ścieżkę bez problemu (`QDropEvent.mimeData().urls()`), więc
    przechwytujemy SAM DROP, zanim trafi do Chromium, wyciągamy ścieżkę i
    podajemy ją do JS sygnałem `WebBridge.fileDropped`. Zdarzenia
    DragEnter/DragMove tylko akceptujemy i puszczamy dalej — Chromium musi je
    dostać, żeby dropzone zdążył się podświetlić pod kursorem.

    Filtr jest instalowany na QApplication, a nie na samym widoku: QWebEngineView
    tworzy wewnętrzny widget renderujący leniwie (i potrafi go wymienić), więc
    instalacja „na widok + dzieci" gubiłaby zdarzenia po podmianie. Filtr
    globalny widzi wszystko, a przynależność do naszego okna sprawdzamy sami.
    """

    def __init__(self, main_window: QWidget, bridge: "WebBridge") -> None:
        super().__init__(main_window)
        self._main_window = main_window
        self._bridge = bridge

    def _belongs_to_window(self, obj: QObject) -> bool:
        """Czy zdarzenie dotyczy NASZEGO okna (a nie np. okna dialogowego)?

        Rozstrzyga top-level (`QWidget.window()`), a NIE łańcuch rodziców:
        QFileDialog bywa dzieckiem MainWindow, ale jest osobnym oknem —
        chodzenie po `parentWidget()` kazałoby nam przejąć drop upuszczony
        na okno wyboru pliku i wysłać go do Fast Cuttera.
        """
        if not isinstance(obj, QWidget):
            return False
        try:
            return obj.window() is self._main_window
        except RuntimeError:  # widget zdążył zniknąć po stronie C++
            return False

    @staticmethod
    def _local_paths(event: QEvent) -> list[str]:
        md = getattr(event, "mimeData", None)
        if md is None:
            return []
        data = md()
        if data is None or not data.hasUrls():
            return []
        # normpath — ujednolica ukośniki, żeby backend dostał ścieżkę w
        # natywnej konwencji systemu (wymóg cross-platform).
        return [os.path.normpath(u.toLocalFile())
                for u in data.urls() if u.isLocalFile() and u.toLocalFile()]

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:  # noqa: N802
        t = event.type()
        if t not in (QEvent.Type.DragEnter, QEvent.Type.DragMove,
                     QEvent.Type.DragLeave, QEvent.Type.Drop):
            return False
        if not self._belongs_to_window(obj):
            return False
        if t == QEvent.Type.DragLeave:
            return False

        paths = self._local_paths(event)
        if not paths:
            return False  # np. przeciągnięcie linku — niech Chromium obsłuży

        if t in (QEvent.Type.DragEnter, QEvent.Type.DragMove):
            # Akceptujemy na poziomie Qt (żeby Drop na pewno do nas dotarł),
            # ale NIE konsumujemy zdarzenia: Chromium musi dostać
            # dragenter/dragover, inaczej dropzone nigdy się nie podświetli
            # i user nie wie, że może puścić plik.
            event.acceptProposedAction()
            return False

        # Drop: bierzemy pierwszy plik i oddajemy ścieżkę do JS.
        event.acceptProposedAction()
        try:
            self._bridge.fileDropped.emit(paths[0])
        except Exception:
            logging.getLogger(__name__).exception("fileDropped emit nie powiodło się")
        return True


class WebBridge(QObject):
    """Mostek QWebChannel: strona HTML wywołuje natywne okno wyboru folderu.

    Sygnał ``ui_ready`` jest emitowany gdy strona zaraportuje pełne
    załadowanie interfejsu (przez wywołanie ``window.wpBridge.notify_ui_ready()``
    z handlera wiadomości WebSocketowej ``{type:'init'}`` w index.html).
    ``AppController`` łapie ten sygnał i wtedy odpala fade-out splasha
    plus fade-in głównego okna.
    """

    ui_ready = pyqtSignal()
    # Qt → JS: health-check startowy (patrz app_controller._check_and_install_deps)
    # emituje (percent, status_text) — JS słucha przez channel.objects.wpBridge
    # i aktualizuje pasek/status na splashu w czasie rzeczywistym.
    loadingProgress = pyqtSignal(int, str)
    # Qt → JS: ścieżka pliku upuszczonego na okno (drag & drop).
    # Chromium NIE przekazuje lokalnej ścieżki do JS (zweryfikowane: w
    # `drop` widać File z nazwą i rozmiarem, ale `path` nie istnieje, a
    # text/uri-list jest puste), więc ścieżkę przechwytuje natywny filtr
    # zdarzeń `_DropForwarder` i podaje ją tędy.
    fileDropped = pyqtSignal(str)

    def __init__(self, main_window: QWidget):
        super().__init__()
        self._main_window = main_window

    @pyqtSlot()
    def notify_ui_ready(self) -> None:
        """JS → Qt: interfejs gotowy do pokazania (WebSocket init przyszedł)."""
        self.ui_ready.emit()

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

    @pyqtSlot(str, str, result=str)
    def pick_open_file(self, suggested_dir: str, file_filter: str) -> str:
        """Otwiera natywne okno wyboru pliku i zwraca pełną ścieżkę.

        Używane przez UI do wskazania pliku cookies.txt (Netscape format).
        ``suggested_dir`` jest punktem startowym; jeżeli pusty, używamy
        folderu Pobierania użytkownika."""
        start = suggested_dir or str(Path.home() / "Downloads")
        path, _ = QFileDialog.getOpenFileName(
            self._main_window,
            "Wybierz plik z ciasteczkami",
            start,
            file_filter or "Plik tekstowy (*.txt);;Wszystkie pliki (*)",
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
        sizes = {"compact": (900, 640), "normal": (1200, 800), "large": (1380, 900)}
        w, h = sizes.get(preset, (1200, 800))
        self._main_window.resize(w, h)
        self._main_window._center_on_screen()

    @pyqtSlot(bool)
    def set_minimize_to_tray(self, enabled: bool) -> None:
        """Ustawia czy zamknięcie okna minimalizuje do zasobnika zamiast kończyć program."""
        self._main_window.minimize_to_tray = enabled

    @pyqtSlot(str, result=bool)
    def copy_to_clipboard(self, text: str) -> bool:
        """JS → Qt: kopiowanie przez NATYWNY schowek systemowy.

        `navigator.clipboard.writeText()` w QWebEngineView bywa zawodne:
        Promise potrafi cicho odrzucić (brak focusu/uprawnień w osadzonym
        widoku), a przy bardzo długich transkrypcjach marshalling wielkiego
        stringa przez most JS↔Chromium potrafi wywrócić proces renderujący
        (RenderProcessTerminated). Qt kopiuje w procesie głównym, więc nawet
        kilkumegabajtowa transkrypcja nie zabija UI.
        """
        try:
            from PyQt6.QtWidgets import QApplication as _QApp
            cb = _QApp.clipboard()
            if cb is None:
                return False
            cb.setText(text or "")
            return True
        except Exception:
            logging.getLogger(__name__).exception("copy_to_clipboard nie powiodło się")
            return False

    @pyqtSlot()
    def quit_app(self) -> None:
        """JS → Qt: zamknij aplikację (używane po podmianie plików przez
        updater — nowy proces startuje z odczepionego skryptu/komendy).

        Omija `minimize_to_tray`: przy aktualizacji naprawdę chcemy wyjść,
        a nie schować się do zasobnika, bo pliki pod nami właśnie się zmieniły.
        """
        from PyQt6.QtWidgets import QApplication as _QApp
        self._main_window.minimize_to_tray = False
        _QApp.quit()

    @pyqtSlot(str, result=bool)
    def run_detached(self, command_json: str) -> bool:
        """JS → Qt: odpala komendę restartu zwróconą przez /api/app-update/apply.

        Proces jest ODCZEPIANY (start_new_session/DETACHED), bo zaraz potem
        aplikacja się zamyka — inaczej skrypt podmieniający pliki zginąłby
        razem z rodzicem, zanim zdążyłby cokolwiek skopiować.
        """
        import json as _json
        import subprocess as _sp
        try:
            cmd = _json.loads(command_json)
            if not isinstance(cmd, list) or not cmd:
                return False
            kwargs: dict = {"close_fds": True}
            if sys.platform == "win32":
                # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
                kwargs["creationflags"] = 0x00000008 | 0x00000200
            else:
                kwargs["start_new_session"] = True
            _sp.Popen([str(c) for c in cmd], **kwargs)
            return True
        except Exception:
            logging.getLogger(__name__).exception("run_detached nie powiodło się")
            return False


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
        self.resize(1200, 800)
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
        # HTTP cache off — nasz index.html/JS/CSS jest podawany przez lokalny
        # uvicorn i musi być zawsze świeży po update aplikacji. Bez tego Chromium
        # trzyma stary HTML w ~/Library/Caches/WP Downloader/QtWebEngine/ nawet
        # gdy bundle został podmieniony (widoczne po dodaniu nowych zakładek).
        self._profile.setHttpCacheType(QWebEngineProfile.HttpCacheType.NoCache)
        _page = QWebEnginePage(self._profile, self.web_view)
        self.web_view.setPage(_page)

        # Public alias `bridge` — AppController łapie się sygnału `ui_ready`.
        self.bridge = WebBridge(self)
        self._web_channel = QWebChannel(self.web_view.page())
        self._web_channel.registerObject("wpBridge", self.bridge)
        self.web_view.page().setWebChannel(self._web_channel)

        # Central widget = WebView. Splash + reveal pochodzą z HTML-side
        # (#splash + initSplashReveal w index.html), Qt nie kryje już
        # WebView własnym overlay'em.
        self.setCentralWidget(self.web_view)

        # Drag & drop: Chromium nie poda JS-owi lokalnej ścieżki, więc
        # przechwytujemy zdarzenia natywnie i wysyłamy ścieżkę sygnałem
        # `wpBridge.fileDropped` (szczegóły w docstringu _DropForwarder).
        # Filtr siedzi na QApplication — wewnętrzny widget renderujący
        # Chromium powstaje leniwie, więc instalacja na samym widoku bywa
        # za wczesna.
        self.setAcceptDrops(True)
        self.web_view.setAcceptDrops(True)
        self._drop_forwarder = _DropForwarder(self, self.bridge)
        app.installEventFilter(self._drop_forwarder)

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

        # Skrót: Ctrl+D (Win/Linux) / Cmd+D (macOS) → otwiera domyślny folder
        # pobierań w systemowym menedżerze plików. Qt sam tłumaczy `Ctrl` na
        # native `Meta` (Cmd) na macOS przy QKeySequence("Ctrl+...").
        open_dl_action = QAction("Otwórz folder pobierań", self)
        open_dl_action.setShortcut(QKeySequence("Ctrl+D"))
        open_dl_action.triggered.connect(self._open_default_download_folder)
        file_menu.addAction(open_dl_action)
        # Dodatkowy globalny QShortcut na wszelki wypadek (gdy menu jest
        # ukryte / fokus nie należy do MainWindow).
        self._dl_folder_shortcut = QShortcut(QKeySequence("Ctrl+D"), self)
        self._dl_folder_shortcut.activated.connect(self._open_default_download_folder)

    def _pick_download_folder(self):
        path = QFileDialog.getExistingDirectory(
            self,
            "Wybierz folder pobierań",
            str(Path.home() / "Downloads"),
        )
        if path:
            self.download_manager.set_output_dir(path)

    def _open_default_download_folder(self) -> None:
        """Otwiera w systemowym menedżerze plików folder ustawiony w aplikacji
        jako domyślny katalog pobierań. Jeśli nie ustawiono albo nie istnieje,
        fallback do ``~/Downloads``."""
        folder = (self.download_manager.output_dir or "").strip()
        if not folder or not os.path.isdir(folder):
            folder = str(Path.home() / "Downloads")
        try:
            if sys.platform == "darwin":
                subprocess.Popen(["open", folder])
            elif sys.platform == "win32":
                os.startfile(folder)  # type: ignore[attr-defined]
            else:
                subprocess.Popen(["xdg-open", folder])
            logger.info("Otwarto folder pobierań: %s", folder)
        except Exception as exc:
            logger.warning("Nie udało się otworzyć folderu pobierań %r: %s", folder, exc)

    def _center_on_screen(self):
        """Wyśrodkowuje okno na ekranie."""
        screen = self.app.primaryScreen().geometry()
        x = (screen.width() - self.width()) // 2
        y = (screen.height() - self.height()) // 2
        self.move(x, y)

    def load_app(self) -> None:
        """Ładuje stronę aplikacji z lokalnego serwera."""
        url = QUrl(f"http://127.0.0.1:{PORT}")
        self.web_view.load(url)

    def start_main_ui_fade_in(self) -> None:
        """Daje JS sygnał do uruchomienia CSS transition `body.opacity 0→1`.
        Wywoływane przez AppController po zakończeniu fade-out overlay'a."""
        self.web_view.page().runJavaScript(
            "document.body && document.body.classList.add('ready');"
        )

    def closeEvent(self, event):
        if self.minimize_to_tray:
            self.hide()
            event.ignore()
        else:
            QApplication.quit()
            event.accept()
