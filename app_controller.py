"""
AppController — główna klasa, która spina całą aplikację.
"""

import json
import subprocess
import sys
import socket
import logging
import threading
from PyQt6.QtWidgets import QApplication
from PyQt6.QtCore import QTimer

from server_thread import ServerThread, PORT
from download_manager import DownloadManager
from main_window import MainWindow

logger = logging.getLogger(__name__)


def _check_and_install_deps(status_cb) -> None:
    """
    Run dependency checks in a background thread; calls status_cb(msg) for each step.
    Missing pip packages are installed automatically.
    """
    import shutil

    pip_pkgs = [("yt-dlp", "yt_dlp"), ("streamlink", "streamlink")]
    for pkg_name, import_name in pip_pkgs:
        status_cb(f"Sprawdzanie {pkg_name}…")
        try:
            __import__(import_name)
        except ImportError:
            status_cb(f"Instalowanie {pkg_name}…")
            try:
                subprocess.run(
                    [sys.executable, "-m", "pip", "install", "-q", "--no-cache-dir", pkg_name],
                    timeout=120,
                    capture_output=True,
                )
            except Exception as exc:
                logger.warning("Auto-install %s failed: %s", pkg_name, exc)

    status_cb("Sprawdzanie ffmpeg…")
    if not shutil.which("ffmpeg"):
        logger.warning("ffmpeg not found in PATH")

    status_cb("Uruchamianie serwera…")


def _server_ready() -> bool:
    """Sprawdza czy serwer FastAPI już nasłuchuje na porcie."""
    try:
        with socket.create_connection(("127.0.0.1", PORT), timeout=0.1):
            return True
    except OSError:
        return False


class AppController:
    def __init__(self, qt_app: QApplication):
        self.qt_app = qt_app
        self.qt_app.setApplicationName("WP Downloader")
        self.qt_app.setApplicationVersion("1.0")

        self.download_manager = DownloadManager()
        self.server_thread = ServerThread(self.download_manager)
        self.main_window = MainWindow(self.qt_app, self.download_manager)

        # Latest status message from the dep-check thread; read from the main thread via QTimer.
        self._loading_status: str = "Uruchamianie…"
        self._loading_lock = threading.Lock()

    def _set_loading_status(self, msg: str) -> None:
        """Thread-safe write of the loading status (called from background thread)."""
        with self._loading_lock:
            self._loading_status = msg

    def _get_loading_status(self) -> str:
        with self._loading_lock:
            return self._loading_status

    def run(self):
        logger.info("Inicjalizacja komponentów aplikacji...")

        # 1. Show the window immediately with loading screen
        self.main_window.show_loading()
        self.main_window.show()

        # 2. Start FastAPI server immediately — don't wait for dep checks
        self.server_thread.start()

        # 3. Run dep checks in pure background (status-only; never blocks server start)
        dep_thread = threading.Thread(
            target=_check_and_install_deps,
            args=(self._set_loading_status,),
            daemon=True,
        )
        dep_thread.start()

        # 4. Push loading status to HTML every 200 ms while loading screen is visible
        status_timer = QTimer()

        def sync_loading_text():
            msg = self._get_loading_status()
            self.main_window.web_view.page().runJavaScript(
                f"updateStatus({json.dumps(msg)});"
            )

        status_timer.setInterval(200)
        status_timer.timeout.connect(sync_loading_text)
        status_timer.start()

        # 5. Poll for server readiness, then load the app
        _attempts = [0]

        def poll_server():
            _attempts[0] += 1
            if _server_ready():
                status_timer.stop()
                self.main_window.load_app()
                logger.info("Serwer gotowy po ~%d ms od startu", _attempts[0] * 100)
            elif _attempts[0] < 100:   # max ~10 s
                QTimer.singleShot(100, poll_server)
            else:
                status_timer.stop()
                self.main_window.load_app()
                logger.warning("Timeout oczekiwania na serwer — ładowanie mimo to")

        QTimer.singleShot(150, poll_server)

        exit_code = self.qt_app.exec()
        self.server_thread.stop()
        sys.exit(exit_code)
