"""
AppController — główna klasa, która spina całą aplikację.

Sekwencja startu (single-window):
    1. Pokaż ``MainWindow`` od razu (1080×780, 100 % opacity). Jego central
       widget to ``QStackedLayout(StackAll)`` z ``QWebEngineView`` (pustym) i
       ``LoadingOverlay`` na wierzchu. User od pierwszej klatki widzi widok
       ładowania: duże logo, „WP Downloader", duży pasek postępu, status.
    2. Wystartuj wątek serwera FastAPI + wątek dep-checków.
    3. Workerowy ``LoadingProgress`` emituje (percent, text) na każdym
       kamieniu milowym; slot ``_on_progress`` aktualizuje overlay.
    4. Gdy serwer odpowiada → ``main_window.load_app()`` (WebView ładuje
       localhost; body w index.html ma opacity 0 — niewidoczne pod overlay'em).
    5. JS po połączeniu WS woła ``wpBridge.notify_ui_ready()`` → sygnał
       ``ui_ready`` → overlay dochodzi do 100 %, pasek zielenieje 250 ms.
    6. Po 300 ms pauzie ``overlay.start_fade_out()`` (350 ms, InCubic, przez
       QGraphicsOpacityEffect).
    7. Po ``overlay.finished`` woła ``main_window.start_main_ui_fade_in()`` —
       JS dodaje klasę `ready` do <body>, CSS transition opacity 0→1 (450 ms).
    8. Error path: ``overlay.force_hide()`` + ``setHtml(error_page)`` w WebView,
       okno cały czas widoczne na 100 %. Tak samo dla 12 s safety net.
"""

import logging
import os
import shutil
import socket
import subprocess
import sys
import threading

from PyQt6.QtCore import (
    QObject,
    Qt,
    QTimer,
    pyqtSignal,
)
from PyQt6.QtWidgets import QApplication

from download_manager import DownloadManager
from main_window import MainWindow
from server_thread import PORT, ServerThread

logger = logging.getLogger(__name__)


class LoadingProgress(QObject):
    """Cross-thread bus: workerzy emitują ``progress`` (percent, text);
    slot w ``AppController`` odbiera przez ``QueuedConnection`` (auto-wybór
    bo emitter na innym wątku) i aktualizuje overlay."""

    progress = pyqtSignal(int, str)


def _check_and_install_deps(progress: LoadingProgress) -> None:
    """Weryfikuje zależności w wątku tła i raportuje przez sygnał ``progress``.

    Zero-dependency: w spakowanej aplikacji yt-dlp / streamlink / ffmpeg są
    wbudowane, więc NIC nie instalujemy na maszynie usera. Pip-fallback dla
    brakujących pakietów działa wyłącznie w trybie deweloperskim (nie-frozen),
    jako wygoda przy uruchamianiu ze źródeł."""
    from binaries import get_ffmpeg

    is_frozen = hasattr(sys, "_MEIPASS")
    pip_pkgs = [("yt-dlp", "yt_dlp"), ("streamlink", "streamlink")]
    n = len(pip_pkgs)
    for i, (pkg_name, import_name) in enumerate(pip_pkgs):
        pct = 10 + int((i / n) * 40)
        progress.progress.emit(pct, f"Sprawdzanie {pkg_name}…")
        try:
            __import__(import_name)
        except ImportError:
            if is_frozen:
                # Bundlowany pakiet powinien istnieć — brak = błąd builda,
                # nie instalujemy niczego w systemie usera.
                logger.error("Bundlowany pakiet %s niedostępny w paczce!", pkg_name)
                continue
            progress.progress.emit(pct, f"Instalowanie {pkg_name}… (dev)")
            try:
                subprocess.run(
                    [sys.executable, "-m", "pip", "install", "-q", "--no-cache-dir", pkg_name],
                    timeout=120,
                    capture_output=True,
                )
            except Exception as exc:
                logger.warning("Auto-install %s failed: %s", pkg_name, exc)

    progress.progress.emit(55, "Sprawdzanie ffmpeg…")
    ff = get_ffmpeg()
    if ff == "ffmpeg" and not shutil.which("ffmpeg"):
        logger.warning("Bundlowany ffmpeg nieodnaleziony — sprawdź bin/ w paczce")
    else:
        logger.info("ffmpeg: %s", ff)


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

        # Progres workerów → log (overlay już nie istnieje — splash jest po stronie HTML).
        self._progress_bus = LoadingProgress()
        self._progress_bus.progress.connect(
            self._on_progress, type=Qt.ConnectionType.QueuedConnection
        )

        # Bridge JS → Qt: WS init oznacza „interfejs gotowy"
        self.main_window.bridge.ui_ready.connect(self._on_ui_ready)

        # Atrybuty trzymane żeby Qt nie zebrał timerów przez GC.
        self._poll_attempts = 0
        self._poll_timer: QTimer | None = None
        self._safety_net_timer: QTimer | None = None
        self._startup_error_shown = False
        self._ui_ready_handled = False

    # ── slots ────────────────────────────────────────────────────────────

    def _on_progress(self, percent: int, text: str) -> None:
        """Slot: loguje stage. Działa na głównym wątku (QueuedConnection)."""
        if text:
            logger.info("loading %d%%: %s", percent, text)

    def _on_ui_ready(self) -> None:
        """JS poinformował że interfejs się załadował (WS init przyszedł).
        Triggerujemy body.ready → MutationObserver w index.html odpala
        timeline reveal (splash fade-out + header/nav/cards drop-in)."""
        if self._ui_ready_handled or self._startup_error_shown:
            return
        self._ui_ready_handled = True
        if self._safety_net_timer is not None:
            self._safety_net_timer.stop()
        self._progress_bus.progress.emit(100, "Gotowe")
        self.main_window.start_main_ui_fade_in()

    # ── error path ───────────────────────────────────────────────────────

    def _show_startup_error(self, reason: str, detail: str) -> None:
        """Awaryjnie ukrywa overlay i pokazuje stronę błędu w WebView.
        Główne okno cały czas widoczne na 100 % opacity — wystarczy odsłonić
        WebView spod overlay'a."""
        if self._startup_error_shown:
            return
        self._startup_error_shown = True

        import html as _html
        log_path = os.environ.get("WP_DOWNLOADER_LOG_PATH", "(brak)")
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
        self.main_window.raise_()
        self.main_window.activateWindow()
        try:
            self.main_window.web_view.setHtml(page)
        except Exception:
            logger.error("Nie można pokazać strony błędu w WebView")

    # ── start ────────────────────────────────────────────────────────────

    def run(self):
        logger.info("Inicjalizacja komponentów aplikacji...")

        # 1. Pokaż główne okno od razu — WebView jest puste, ale za chwilę
        #    załaduje index.html z własnym HTML-side splashem.
        self.main_window.show()
        self.main_window.raise_()
        self.main_window.activateWindow()
        self._progress_bus.progress.emit(10, "Inicjalizacja komponentów…")

        # 2. Server thread + dep-check thread odpalają się równolegle.
        self.server_thread.start()
        threading.Thread(
            target=_check_and_install_deps,
            args=(self._progress_bus,),
            daemon=True,
        ).start()

        # 3. WebView reportuje „strona załadowana" → 90 %.
        self.main_window.web_view.loadFinished.connect(self._on_load_finished)

        # 4. Polling 100 ms × max 100 = ~10 s na start serwera.
        self._poll_timer = QTimer(self.main_window)
        self._poll_timer.setInterval(100)
        self._poll_timer.timeout.connect(self._poll_server)
        QTimer.singleShot(150, self._poll_timer.start)

        exit_code = self.qt_app.exec()
        self.server_thread.stop()
        sys.exit(exit_code)

    def _poll_server(self) -> None:
        self._poll_attempts += 1
        if _server_ready():
            self._poll_timer.stop()
            logger.info("Serwer gotowy po ~%d ms od startu", self._poll_attempts * 100)
            self._progress_bus.progress.emit(80, "Łączenie z serwerem…")
            self.main_window.load_app()
            # Safety net: jeśli `notify_ui_ready` nie nadejdzie w 12 s
            # (np. JS error w connectWS), forsujemy fade-out tak czy siak.
            self._safety_net_timer = QTimer(self.main_window)
            self._safety_net_timer.setSingleShot(True)
            self._safety_net_timer.setInterval(12_000)
            self._safety_net_timer.timeout.connect(self._on_safety_net)
            self._safety_net_timer.start()
            return

        # Server thread padł zanim port się otworzył?
        err = getattr(self.server_thread, "startup_error", None)
        if err or not self.server_thread.is_alive():
            self._poll_timer.stop()
            detail = err or "Wątek serwera zakończył się bez komunikatu."
            logger.error("Wątek serwera nie żyje: %s", detail)
            self._show_startup_error(
                "Komponent serwera lokalnego wywalił się podczas startu.",
                detail,
            )
            return

        if self._poll_attempts >= 100:   # ~10 s
            self._poll_timer.stop()
            logger.warning("Timeout oczekiwania na serwer (10 s) — pokazuję stronę błędu")
            self._show_startup_error(
                "Serwer nie odpowiedział w ciągu 10 sekund.",
                "Wątek serwera nadal żyje, ale port 8765 nie zwraca odpowiedzi. "
                "Możliwe że uvicorn utknął na imporcie modułu albo port jest zajęty.",
            )

    def _on_load_finished(self, ok: bool) -> None:
        """QWebEngineView załadował stronę (HTML, CSS, JS). Brakuje już tylko
        wiadomości WS init żeby uznać UI za gotowe — to 90 %."""
        if self._ui_ready_handled or self._startup_error_shown:
            return
        if ok:
            self._progress_bus.progress.emit(90, "Ładowanie interfejsu…")
        else:
            logger.warning("WebView loadFinished z błędem")

    def _on_safety_net(self) -> None:
        """12 s upłynęło bez ``notify_ui_ready`` — forsujemy fade-out żeby
        user nie utknął na ekranie ładowania jeśli JS się wywalił."""
        if self._ui_ready_handled or self._startup_error_shown:
            return
        logger.warning("Safety net: notify_ui_ready nie nadeszło w 12 s — forsuję fade-out")
        self._on_ui_ready()
