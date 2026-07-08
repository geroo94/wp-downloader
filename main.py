"""
main.py — punkt wejścia aplikacji.

To jest plik który uruchamiasz (albo który uruchamia skrót na pulpicie).
Robi jedną rzecz: tworzy AppController i wywołuje run().

Analogia: to jest klucz który przekręcasz w stacyjce.
Cały silnik (AppController) jest gdzie indziej.
"""

import multiprocessing
multiprocessing.freeze_support()  # must be called before any other code for PyInstaller + torch/whisper

import os
import sys

# PyInstaller --noconsole on Windows sets sys.stdout / sys.stderr to None.
# Any library that calls sys.stdout.isatty() during import then crashes —
# uvicorn's ColourizedFormatter does exactly that on construction, which
# kills the FastAPI worker thread before it can bind port 8765 and the
# WebView only sees ERR_CONNECTION_REFUSED. Replace None with a real file
# object so isatty() / write() / flush() all work as no-ops.
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w", encoding="utf-8")

import subprocess
import urllib.request
import zipfile
import threading
import logging
from PyQt6.QtWidgets import QMessageBox, QApplication
from PyQt6.QtGui import QPixmap
from PyQt6.QtCore import Qt

_single_instance_socket = None


def acquire_single_instance_lock(server_port: int = 8765, lock_port: int = 49327) -> str:
    """Zwraca status startu pojedynczej instancji:
         "ok"      — jesteśmy jedyną instancją, można startować
         "running" — działa już inna instancja WP Downloader
         "busy"    — port serwera okupuje OBCY proces (nie nasza aplikacja)

       Strategia: najpierw race-free lock na porcie UDP (jeden proces trzyma
       go przez cały lifetime — wygrywa wyścig przy podwójnym double-clicku).
       Potem fingerprint HTTP na porcie serwera: sam TCP-connect NIE wystarcza,
       bo dowolny inny serwer na 8765 (np. deweloperski uvicorn) wyglądał jak
       "druga instancja" i aplikacja znikała bez komunikatu — dla użytkownika
       nieodróżnialne od crash on boot."""
    import socket
    import json as _json
    import urllib.error
    import urllib.request

    # 1. Race-free claim: tylko jeden proces może trzymać ten UDP port.
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.bind(("127.0.0.1", lock_port))
    except OSError:
        s.close()
        return "running"
    global _single_instance_socket
    _single_instance_socket = s  # keep alive for the whole process lifetime

    # 2. Ktoś już serwuje na porcie aplikacji? Sprawdź, CZYM jest.
    try:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{server_port}/api/system-info", timeout=1.5) as r:
            data = _json.load(r)
        if isinstance(data, dict) and "app_version" in data:
            # Odpowiada jak WP Downloader (np. instancja innego użytkownika
            # albo stary proces sprzed wprowadzenia UDP locka).
            return "running"
        return "busy"
    except urllib.error.HTTPError:
        # Port gada HTTP, ale to nie nasze API → obcy serwer.
        return "busy"
    except Exception:
        # Brak odpowiedzi HTTP = port wolny. Jeżeli trzyma go proces nie-HTTP,
        # uvicorn zgłosi błąd bind — złapie go error-path AppController
        # (widoczna strona błędu zamiast cichego zniknięcia).
        return "ok"


def setup_overlay_site_packages():
    """Prepend a user-writable overlay to sys.path so pip-installed updates of
       yt-dlp / streamlink override the bundled copies on the next launch."""
    import platform as _platform
    home = os.path.expanduser("~")
    sysname = _platform.system()
    if sysname == "Darwin":
        base = os.path.join(home, "Library", "Application Support", "WP_Downloader")
    elif sysname == "Windows":
        base = os.path.join(os.environ.get("APPDATA", home), "WP_Downloader")
    else:
        base = os.path.join(home, ".local", "share", "wp_downloader")
    overlay = os.path.join(base, "site-packages")
    try:
        os.makedirs(overlay, exist_ok=True)
    except OSError:
        return
    if overlay not in sys.path:
        sys.path.insert(0, overlay)


def get_logs_dir() -> str:
    """Returns the user-data `logs/` directory and creates it on first call.

    Lives outside the .app bundle / install directory so it survives
    auto-update (which replaces the bundle wholesale on macOS) and so the
    install directory can stay read-only.

      macOS   → ~/Library/Logs/WP_Downloader/
      Windows → %APPDATA%/WP_Downloader/logs/
      Linux   → ~/.local/share/wp_downloader/logs/

    Falls back to placing logs next to the EXE if the user-data path can't
    be created (e.g. weird permissions, sandboxing).
    """
    import platform as _platform
    home = os.path.expanduser("~")
    sysname = _platform.system()
    if sysname == "Darwin":
        candidate = os.path.join(home, "Library", "Logs", "WP_Downloader")
    elif sysname == "Windows":
        candidate = os.path.join(
            os.environ.get("APPDATA", home), "WP_Downloader", "logs")
    else:
        candidate = os.path.join(home, ".local", "share", "wp_downloader", "logs")
    try:
        os.makedirs(candidate, exist_ok=True)
        return candidate
    except OSError:
        base_dir = os.path.dirname(os.path.abspath(
            sys.executable if hasattr(sys, '_MEIPASS') else __file__))
        fallback = os.path.join(base_dir, "logs")
        os.makedirs(fallback, exist_ok=True)
        return fallback


def _rotate_old_logs(log_dir: str, keep: int = 20) -> None:
    """Keep only the N most recent log files in log_dir; delete the rest."""
    try:
        files = [os.path.join(log_dir, f) for f in os.listdir(log_dir)
                 if f.startswith("wp_downloader_") and f.endswith(".log")]
        files.sort(key=lambda p: os.path.getmtime(p), reverse=True)
        for old in files[keep:]:
            try:
                os.remove(old)
            except OSError:
                pass
    except OSError:
        pass


def setup_logging() -> str:
    """Konfiguruje logowanie do pliku tekstowego.

    Każde uruchomienie tworzy nowy plik `wp_downloader_YYYYMMDD_HHMMSS.log`
    w platformowo poprawnym folderze (poza bundle / install dir, żeby
    przeżył auto-update i nie wymagał write w Program Files). Trzymamy
    ostatnie 20 plików.
    """
    from datetime import datetime
    log_dir = get_logs_dir()
    _rotate_old_logs(log_dir)
    log_filename = f"wp_downloader_{datetime.now():%Y%m%d_%H%M%S}.log"
    log_path = os.path.join(log_dir, log_filename)

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
        handlers=[
            logging.FileHandler(log_path, encoding='utf-8', mode='w'),
            logging.StreamHandler(sys.stdout),
        ],
    )
    logging.info("=== Uruchomienie WP Downloader ===")
    logging.info("Log file: %s", log_path)
    os.environ["WP_DOWNLOADER_LOG_PATH"] = log_path
    return log_path

def add_local_bin_to_path():
    """Szuka php.exe i innych binariów w folderze aplikacji i dodaje je do PATH procesu."""
    # Folder, w którym znajduje się plik wykonywalny (lub skrypt)
    exe_dir = os.path.dirname(os.path.abspath(sys.executable))
    # Folder tymczasowy PyInstaller (jeśli spakowane)
    bundle_dir = getattr(sys, '_MEIPASS', exe_dir)

    potential_paths = [
        exe_dir,
        os.path.join(exe_dir, "bin"),
        os.path.join(exe_dir, "php"),
        bundle_dir,
        os.path.join(bundle_dir, "bin"),
        os.path.join(bundle_dir, "php"),
        "/usr/local/bin",
        "/opt/homebrew/bin"
    ]

    current_path = os.environ.get("PATH", "")
    new_entries = []
    
    for p in potential_paths:
        if os.path.isdir(p) and p not in current_path and p not in new_entries:
            new_entries.append(p)
    
    if new_entries:
        os.environ["PATH"] = os.pathsep.join(new_entries) + os.pathsep + current_path
        logging.info(f"Środowisko EXE: Dodano do PATH: {new_entries}")
    else:
        logging.info("Środowisko EXE: PATH pozostał bez zmian.")

def get_resource_path(relative_path: str) -> str:
    """Zwraca poprawną ścieżkę do zasobów, działającą także po spakowaniu do EXE."""
    if hasattr(sys, '_MEIPASS'):
        return os.path.join(sys._MEIPASS, relative_path)
    return os.path.join(os.path.abspath("."), relative_path)

def check_and_install_ffmpeg():
    """Sprawdza FFmpeg i instaluje go, jeśli brakuje (logika z Twojego kodu)."""
    ffmpeg_bin = "ffmpeg.exe" if sys.platform == "win32" else "ffmpeg"
    ffmpeg_missing = not (os.path.exists(ffmpeg_bin) or any(
        os.access(os.path.join(path, ffmpeg_bin), os.X_OK) 
        for path in os.environ.get("PATH", "").split(os.pathsep) if path
    ))

    if not ffmpeg_missing:
        return

    if sys.platform != "win32":
        msg = "Do prawidłowego działania programu (szczególnie live streamów) wymagany jest FFmpeg.\n\n" \
              "Nie znaleziono go w systemie. Zainstaluj go komendą:\n" \
              "brew install ffmpeg\n\n" \
              "Jeśli jest już zainstalowany, upewnij się, że znajduje się w /usr/local/bin lub /opt/homebrew/bin."
        QMessageBox.warning(None, "Brak FFmpeg", msg)
        logging.warning("FFmpeg nie został znaleziony.")
        return

    msg = "Do prawidłowego działania programu brakuje FFmpeg (wymagane do Facebooka/YouTube).\n\nCzy chcesz go teraz pobrać automatycznie?"
    reply = QMessageBox.question(None, "Brakujące wymagania", msg, 
                                 QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
    
    if reply == QMessageBox.StandardButton.Yes:
        # Wyłączamy piaskownicę, bo w EXE często blokuje ona dostęp do zasobów GPU
        os.environ["QTWEBENGINE_DISABLE_SANDBOX"] = "1"
        
        # Logika pobierania (bez zmian, ale opakowana w logi w wywołaniu)
        _download_ffmpeg_logic()
    else:
        sys.exit(0)

def _download_ffmpeg_logic():
    try:
        url = "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip"
        zip_path = "ffmpeg_temp.zip"
        urllib.request.urlretrieve(url, zip_path)
        with zipfile.ZipFile(zip_path, 'r') as zip_ref:
            for file in zip_ref.namelist():
                if file.endswith("ffmpeg.exe") or file.endswith("ffprobe.exe"):
                    filename = os.path.basename(file)
                    with open(filename, "wb") as f_out:
                        f_out.write(zip_ref.read(file))
        if os.path.exists(zip_path): os.remove(zip_path)
        QMessageBox.information(None, "Sukces", "FFmpeg został zainstalowany.")
    except Exception as e:
        logging.error(f"Błąd instalacji FFmpeg: {e}")
        QMessageBox.critical(None, "Błąd", f"Nie udało się zainstalować FFmpeg: {e}")
        sys.exit(1)

if __name__ == "__main__":
    try:
        # Guard 1: QtWebEngine spawns renderer/gpu/utility helper processes using
        # the same EXE binary with --type=<kind> arguments. Intercept them before
        # any app-level setup so they don't spam the log or rebind the server port.
        if any(a.startswith("--type=") for a in sys.argv[1:]):
            from PyQt6.QtWidgets import QApplication
            sys.exit(QApplication(sys.argv).exec())

        # Guard 2: pip/yt-dlp subprocess invocations spawned by perform_system_update.
        # Must be BEFORE setup_logging() so the second process stays silent.
        if len(sys.argv) >= 3 and sys.argv[1] == "-m":
            if "yt_dlp" in sys.argv[2]:
                import yt_dlp
                sys.argv = [sys.argv[0]] + sys.argv[3:]
                sys.exit(yt_dlp.main())
            elif "pip" in sys.argv[2]:
                from pip._internal.cli.main import main as pip_main
                sys.argv = [sys.argv[0]] + sys.argv[3:]
                sys.exit(pip_main())
            sys.exit(0)

        # 0. Inicjalizacja logowania i PATH + overlay site-packages (BEFORE any yt_dlp import)
        setup_overlay_site_packages()
        setup_logging()
        add_local_bin_to_path()

        # Single-instance guard: prevents the WebView from staring at a dead port
        # when the user double-clicks the EXE during the FFmpeg download window.
        _instance_status = acquire_single_instance_lock()
        if _instance_status != "ok":
            logging.info("Single-instance guard: %s — kończę proces.", _instance_status)
            # Widoczny komunikat zamiast cichego exit(0) — ciche zniknięcie
            # procesu było zgłaszane jako "crash on boot".
            _dlg_app = QApplication(sys.argv)
            if _instance_status == "running":
                QMessageBox.information(
                    None, "WP Downloader",
                    "WP Downloader już działa.\n\n"
                    "Sprawdź Dock / zasobnik systemowy — okno mogło zostać "
                    "zminimalizowane lub ukryte.",
                )
            else:  # "busy" — obcy proces na porcie serwera
                QMessageBox.critical(
                    None, "WP Downloader — port zajęty",
                    "Port 127.0.0.1:8765 jest zajęty przez inny program, "
                    "więc lokalny serwer aplikacji nie może wystartować.\n\n"
                    "Zamknij program nasłuchujący na porcie 8765 "
                    "(w Terminalu: lsof -nP -iTCP:8765) i uruchom "
                    "WP Downloader ponownie.",
                )
            sys.exit(0)

        # Chromium flags — MUST be set before QApplication init.
        # `--autoplay-policy=no-user-gesture-required` pozwala <video>.play()
        # bez wcześniejszej interakcji użytkownika (Fast Cutter automatyczne
        # ładowanie po drop). `--proprietary-codecs` aktywuje H.264/AAC w Qt
        # buildach które kompilują codec ale wyłączają domyślnie flagą runtime;
        # jeśli PyQt6-WebEngine wheel nie ma codec w binarce — flaga jest no-op.
        _existing_flags = os.environ.get("QTWEBENGINE_CHROMIUM_FLAGS", "").strip()
        _flags = "--autoplay-policy=no-user-gesture-required --proprietary-codecs"
        os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = (
            f"{_existing_flags} {_flags}".strip()
        )

        # 2. Inicjalizacja GUI z łapaniem błędów binarnych
        logging.info("Ładowanie modułów QtWebEngine...")
        from PyQt6 import QtWebEngineWidgets
        
        logging.info("Inicjalizacja QApplication...")
        app_instance = QApplication(sys.argv)
        app_instance.setApplicationName("WP Downloader")

        logging.info("Sprawdzanie FFmpeg...")
        check_and_install_ffmpeg()

        logging.info("Uruchamianie AppController...")
        from app_controller import AppController
        app = AppController(app_instance)
        
        logging.info("Aplikacja gotowa, start pętli zdarzeń.")
        app.run()

    except Exception as e:
        # To złapie błędy, które wcześniej zabijały aplikację po cichu
        logging.exception("BŁĄD KRYTYCZNY STARTU:")
        # Jeśli mamy już QApplication, spróbujmy pokazać błąd użytkownikowi
        if 'app_instance' in locals():
            QMessageBox.critical(None, "Błąd Krytyczny", f"Aplikacja nie mogła się uruchomić:\n{e}")
        sys.exit(1)
