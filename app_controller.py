"""
AppController — główna klasa, która spina całą aplikację.
"""

import json
import os
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

        def _show_startup_error(reason: str, detail: str) -> None:
            """Replace the WebView's blank chrome ERR page with a real Polish error
               page that shows what went wrong + the log path the user can paste."""
            import html as _html
            import sys as _sys
            log_dir = os.path.dirname(os.path.abspath(
                _sys.executable if hasattr(_sys, '_MEIPASS') else __file__))
            log_path = os.path.join(log_dir, "wp_downloader_debug.log")
            page = f"""<!doctype html><html lang="pl"><head><meta charset="utf-8">
<title>WP Downloader — błąd uruchamiania</title>
<style>
  body {{font:14px/1.5 -apple-system,Segoe UI,system-ui,sans-serif;background:#FBFBFA;color:#111;
        padding:36px;max-width:760px;margin:0 auto}}
  h1 {{font-size:20px;color:#E3000F;margin:0 0 8px;letter-spacing:-.02em}}
  h2 {{font-size:13px;color:#555;margin:24px 0 6px;text-transform:uppercase;letter-spacing:.06em}}
  pre {{background:#F0F0EC;border:1px solid #EAEAEA;border-radius:8px;padding:14px 16px;
        white-space:pre-wrap;word-break:break-word;font-size:12px;line-height:1.5}}
  ul {{padding-left:18px}} li {{margin:4px 0}}
  code {{background:#F0F0EC;padding:1px 6px;border-radius:4px;font-size:12px}}
</style></head><body>
  <h1>Serwer aplikacji nie wystartował.</h1>
  <p>{_html.escape(reason)} Aplikacja zostawiła pełny ślad błędu w pliku logu — wklej go zgłaszając problem.</p>
  <h2>Szczegóły</h2>
  <pre>{_html.escape(detail or "(brak szczegółów — sprawdź log poniżej)")}</pre>
  <h2>Plik logu</h2>
  <pre>{_html.escape(log_path)}</pre>
  <h2>Co możesz spróbować</h2>
  <ul>
    <li>Zamknij i uruchom aplikację ponownie.</li>
    <li>Sprawdź czy zapora / antywirus nie blokuje portu <code>127.0.0.1:8765</code>.</li>
    <li>Jeśli to powtarzający się błąd — wyślij plik logu (ścieżka wyżej).</li>
  </ul>
</body></html>"""
            try:
                self.main_window.web_view.setHtml(page)
            except Exception:
                # Last resort if even setHtml is unavailable
                logger.error("Nie można pokazać strony błędu w WebView")

        def poll_server():
            _attempts[0] += 1
            if _server_ready():
                status_timer.stop()
                self.main_window.load_app()
                logger.info("Serwer gotowy po ~%d ms od startu", _attempts[0] * 100)
                return
            # Server thread died early? Surface the cause now instead of waiting
            # out the full 10 s timeout.
            err = getattr(self.server_thread, "startup_error", None)
            if err or not self.server_thread.is_alive():
                status_timer.stop()
                detail = err or "Wątek serwera zakończył się bez komunikatu."
                logger.error("Wątek serwera nie żyje: %s", detail)
                _show_startup_error(
                    "Komponent serwera lokalnego wywalił się podczas startu.",
                    detail,
                )
                return
            if _attempts[0] < 100:   # max ~10 s
                QTimer.singleShot(100, poll_server)
            else:
                status_timer.stop()
                logger.warning("Timeout oczekiwania na serwer (10 s) — pokazuję stronę błędu")
                _show_startup_error(
                    "Serwer nie odpowiedział w ciągu 10 sekund.",
                    "Wątek serwera nadal żyje, ale port 8765 nie zwraca odpowiedzi. "
                    "Możliwe że uvicorn utknął na imporcie modułu albo port jest zajęty.",
                )

        QTimer.singleShot(150, poll_server)

        exit_code = self.qt_app.exec()
        self.server_thread.stop()
        sys.exit(exit_code)
