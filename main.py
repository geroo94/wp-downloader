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
import subprocess
import urllib.request
import zipfile
import threading
import logging
from PyQt6.QtWidgets import QMessageBox, QApplication
from PyQt6.QtGui import QPixmap
from PyQt6.QtCore import Qt

_single_instance_socket = None


def acquire_single_instance_lock(server_port: int = 8765, lock_port: int = 49327) -> bool:
    """Returns True if this is the only instance (proceed with startup).
       Returns False if another instance is already running (caller should exit).

       Strategy: TCP-probe the actual server port first (catches the case where the
       first instance is fully up). Then bind a UDP lock port to win the race when
       two instances start simultaneously (the FFmpeg-download window in particular)."""
    import socket
    # Is the server already responding? Then another instance is fully up.
    try:
        with socket.create_connection(("127.0.0.1", server_port), timeout=0.5):
            return False
    except (OSError, socket.timeout):
        pass

    # Race-free claim: only one process can hold this UDP port at a time.
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.bind(("127.0.0.1", lock_port))
    except OSError:
        s.close()
        return False

    global _single_instance_socket
    _single_instance_socket = s  # keep alive for the whole process lifetime
    return True


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


def setup_logging():
    """Konfiguruje logowanie do pliku tekstowego."""
    # Określamy ścieżkę do logu (obok pliku EXE lub skryptu)
    log_dir = os.path.dirname(os.path.abspath(sys.executable if hasattr(sys, '_MEIPASS') else __file__))
    log_path = os.path.join(log_dir, "wp_downloader_debug.log")
    
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
        handlers=[
            logging.FileHandler(log_path, encoding='utf-8', mode='a'),
            logging.StreamHandler(sys.stdout)
        ]
    )
    logging.info("=== Uruchomienie WP Downloader ===")

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
        if not acquire_single_instance_lock():
            logging.info("Inna instancja WP Downloader już działa — kończę proces.")
            sys.exit(0)

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
