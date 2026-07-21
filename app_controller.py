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


def _check_binaries(progress: LoadingProgress) -> None:
    """Etap 1/3 health-checka: ffmpeg/ffprobe/deno są na miejscu i mają
    nadane uprawnienia wykonywania. PyInstaller czasem gubi exec-bit przy
    kopiowaniu `datas` (dlatego build_local.sh robi to samo po kompilacji) —
    to jest druga linia obrony w runtime, na wypadek gdyby coś to nadpisało
    (np. rozpakowanie portable ZIP-a przez narzędzie, które czyści bity)."""
    progress.progress.emit(12, "Sprawdzanie plików binarnych (ffmpeg/ffprobe/deno)…")
    from binaries import get_ffmpeg, get_ffprobe
    for getter, name in ((get_ffmpeg, "ffmpeg"), (get_ffprobe, "ffprobe")):
        try:
            p = getter()
            if os.path.isfile(p) and not os.access(p, os.X_OK):
                os.chmod(p, 0o755)
                logger.info("Health-check: nadano +x %s", p)
        except Exception as exc:
            logger.warning("Health-check binarki %s: %s", name, exc)

    try:
        from yt_dlp_worker import _bundled_runtime_dir
        binary = "deno.exe" if sys.platform == "win32" else "deno"
        for cand_dir in _bundled_runtime_dir():
            p = os.path.join(cand_dir, binary)
            if os.path.isfile(p):
                if not os.access(p, os.X_OK):
                    os.chmod(p, 0o755)
                    logger.info("Health-check: nadano +x %s", p)
                break
        else:
            logger.warning("Health-check: bundlowany deno nieodnaleziony (bin/)")
    except Exception as exc:
        logger.warning("Health-check deno: %s", exc)


def _check_ytdlp_update(progress: LoadingProgress) -> None:
    """Etap 2/3: cichy version-check + best-effort auto-update yt-dlp.

    UWAGA architektoniczna: `yt_dlp.update.update_self` NIE pasuje tutaj —
    ten mechanizm jest do samo-aktualizacji STANDALONE binarki yt-dlp.exe.
    W tej aplikacji yt-dlp jest zaimportowanym modułem Pythona wewnątrz
    WŁASNEGO PyInstaller bundla; wywołanie update_self próbowałoby podmienić
    plik wykonywalny NASZEJ aplikacji, myśląc że to yt-dlp.exe — realne
    ryzyko zepsucia instalacji. Zamiast tego używamy tej samej bezpiecznej
    metody co `perform_system_update` w server.py: pip install do overlay
    dir (get_overlay_dir), który ma priorytet nad wersją bundlowaną, bez
    dotykania samego pliku aplikacji."""
    progress.progress.emit(25, "Sprawdzanie yt-dlp…")
    try:
        import yt_dlp
        cur = getattr(getattr(yt_dlp, "version", None), "__version__", "") or ""
    except Exception as exc:
        logger.warning("Health-check yt-dlp import: %s", exc)
        return

    latest = ""
    try:
        import json
        import urllib.request
        with urllib.request.urlopen("https://pypi.org/pypi/yt-dlp/json", timeout=4) as r:
            latest = json.load(r).get("info", {}).get("version", "") or ""
    except Exception as exc:
        logger.debug("Health-check yt-dlp: PyPI check pominięty (offline?): %s", exc)
        progress.progress.emit(30, "yt-dlp: brak sieci, pomijam auto-update.")
        return

    if not latest or latest == cur:
        progress.progress.emit(30, "yt-dlp jest aktualny.")
        return

    progress.progress.emit(28, f"Aktualizowanie yt-dlp… ({cur} → {latest})")
    try:
        from server import get_overlay_dir
        overlay = get_overlay_dir()
        from binaries import subprocess_flags
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "-q", "-U", "--no-cache-dir",
             "--target", overlay, "--upgrade-strategy=eager", "yt-dlp"],
            timeout=25, capture_output=True, creationflags=subprocess_flags(),
        )
        progress.progress.emit(30, "yt-dlp zaktualizowany (efekt po następnym restarcie).")
    except Exception as exc:
        logger.warning("Health-check auto-update yt-dlp: %s", exc)


def _bundled_whisper_models_dir() -> str:
    if hasattr(sys, "_MEIPASS"):
        return os.path.join(sys._MEIPASS, "assets", "models", "whisper")
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "models", "whisper")


# SHA256 identyczne z whisper._MODELS (whisper._download waliduje tym samym
# hashem przy load_model — zaszyte pliki muszą się z nim zgadzać co do bajtu).
_WHISPER_BUNDLED_SHA256 = {
    "tiny.pt": "65147644a518d12f04e32d6f3b26facc3f8dd46e5390956a9424a650c0ce22b9",
    "base.pt": "ed3a0b6b1c0edf879ad9b11b1af5a0e6ab5db9205f891f668f8b0e6c6326e34e",
}


def _check_whisper_models(progress: LoadingProgress) -> None:
    """Etap 3/3: modele Whisper zaszyte w paczce (assets/models/whisper)
    istnieją i mają poprawny SHA256 — ten sam checksum, którego
    whisper._download() używa do walidacji cache. Brak modelu nie jest
    fatalny (transkrypcja i tak dociągnie go z sieci przy pierwszym użyciu),
    ale uszkodzony plik logujemy głośno, żeby było widać w diagnostyce."""
    progress.progress.emit(38, "Weryfikacja bazy modeli Whisper…")
    import hashlib
    models_dir = _bundled_whisper_models_dir()
    for fname, expected in _WHISPER_BUNDLED_SHA256.items():
        p = os.path.join(models_dir, fname)
        if not os.path.isfile(p):
            logger.info("Health-check: model whisper %s niedołączony do paczki "
                        "(dociągnie się z sieci przy pierwszej transkrypcji)", fname)
            continue
        try:
            h = hashlib.sha256()
            with open(p, "rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    h.update(chunk)
            if h.hexdigest() != expected:
                logger.warning("Health-check: model whisper %s USZKODZONY (SHA256 mismatch)", fname)
            else:
                logger.info("Health-check: model whisper %s OK (offline-ready)", fname)
        except OSError as exc:
            logger.warning("Health-check whisper %s: %s", fname, exc)
    progress.progress.emit(45, "Baza modeli Whisper zweryfikowana.")


def _check_and_install_deps(progress: LoadingProgress) -> None:
    """Health-check sekwencyjny przed wejściem do menu głównego: (1) binarki
    ffmpeg/ffprobe/deno + chmod, (2) cichy version-check + best-effort
    auto-update yt-dlp, (3) weryfikacja modeli Whisper zaszytych w paczce,
    (4) tylko w trybie deweloperskim (nie-frozen) — pip-install brakujących
    pakietów Python jako wygoda przy uruchamianiu ze źródeł.

    Zero-dependency w buildzie produkcyjnym: yt-dlp/streamlink/ffmpeg są
    wbudowane, więc etap (4) w praktyce nic nie robi poza zalogowaniem stanu."""
    _check_binaries(progress)
    _check_ytdlp_update(progress)
    _check_whisper_models(progress)

    is_frozen = hasattr(sys, "_MEIPASS")
    pip_pkgs = [("yt-dlp", "yt_dlp"), ("streamlink", "streamlink")]
    n = len(pip_pkgs)
    for i, (pkg_name, import_name) in enumerate(pip_pkgs):
        pct = 50 + int((i / n) * 25)
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
                from binaries import subprocess_flags
                subprocess.run(
                    [sys.executable, "-m", "pip", "install", "-q", "--no-cache-dir", pkg_name],
                    timeout=120,
                    capture_output=True,
                    creationflags=subprocess_flags(),
                )
            except Exception as exc:
                logger.warning("Auto-install %s failed: %s", pkg_name, exc)

    progress.progress.emit(75, "Komponenty gotowe.")


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
        """Slot: loguje stage i przekazuje (percent, text) na splash w JS przez
        QWebChannel. Działa na głównym wątku (QueuedConnection)."""
        if text:
            logger.info("loading %d%%: %s", percent, text)
        try:
            self.main_window.bridge.loadingProgress.emit(percent, text)
        except Exception:
            pass

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
