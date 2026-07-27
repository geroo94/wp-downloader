"""WP Environment Checker — lekkie, niezależne narzędzie diagnostyczne.

"Krok 0" przed instalacją głównej aplikacji WP Downloader: skanuje CAŁY stos
technologiczny aplikacji (system, VC++ Redistributable, GPU/sterowniki,
FFmpeg/FFprobe, Node.js/Deno JS runtime, PyQt6+QWebEngineView, FastAPI+
Uvicorn, moduł yt-dlp, prawa zapisu, uprawnienia administratora) i pokazuje
czytelną listę kontrolną z dwoma przyciskami:
  - "Kopiuj raport dla Administratora IT" — dla środowisk korporacyjnych,
    gdzie user zgłasza brakujące biblioteki do IT zamiast zgadywać co jest
    nie tak.
  - "Instaluj brakujące komponenty" — dociąga to, co da się naprawić BEZ
    ręcznej interwencji: FFmpeg/FFprobe/Deno do lokalnego bin/ (zero
    uprawnień, działa w trybie portable) oraz VC++ Redistributable na
    Windows (wymaga elevacji — Windows sam pokaże standardowy prompt UAC,
    to NIE jest ciche/ukryte podniesienie uprawnień, tylko cichy PRZEBIEG
    samego instalatora po zaakceptowaniu UAC).

CELOWO zero-dependency poza stdlibem (Tkinter zamiast PyQt6): to narzędzie
ma działać PRZED zainstalowaniem/zaufaniem głównej aplikacji, więc nie może
zależeć od żadnego z jej modułów (server.py, binaries.py, ...) ani od
ciężkich pakietów (PyQt6, torch) — stąd też brak importu czegokolwiek z
reszty repo. Uruchamiane samodzielnie: `python tools/env_checker.py`, albo
jako spakowany `WP_Environment_Checker.exe` / `.app` (patrz build_env_checker.sh).

Uwaga architektoniczna (PyQt6/FastAPI/uvicorn/yt-dlp): te pakiety są
BUNDLOWANE WEWNĄTRZ WP Downloader.app/.exe przez PyInstaller — to nie są
oddzielne zależności systemowe, które user musi sam zainstalować. Checki
dla nich mają więc DWA konteksty:
  1. Uruchomiony jako frozen exe (normalny przypadek dla usera) — brak tych
     pakietów W SAMYM CHECKERZE jest oczekiwany (celowo ich nie bundlujemy,
     żeby zostać mały), więc raportujemy OK z wyjaśnieniem, nie fałszywy błąd.
  2. Uruchomiony z repo/venv (`python tools/env_checker.py` przez dewelopera)
     — wtedy realnie sprawdzamy `importlib.util.find_spec`, bo to ten sam
     Python co reszta projektu.
"""

from __future__ import annotations

import ctypes
import gzip
import importlib.util
import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import tkinter as tk
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from tkinter import font as tkfont
from tkinter import ttk

APP_TITLE = "WP Environment Checker"

# Poziomy wyniku pojedynczego sprawdzenia.
OK, WARN, FAIL = "ok", "warn", "fail"
_ICON = {OK: "✅", WARN: "⚠️", FAIL: "❌"}
_COLOR = {OK: "#2e9e5b", WARN: "#c98a1a", FAIL: "#d64545"}

_DENO_VERSION = "2.7.12"  # ten sam pin co scripts/build_local.sh i CI


@dataclass
class CheckResult:
    name: str
    level: str  # OK | WARN | FAIL
    detail: str
    fix_key: str | None = None  # None = brak automatycznej naprawy z tego narzędzia


def _bundled_bin_dir() -> str:
    """Katalog `bin/` obok tego skryptu/EXE — ten sam layout co główna apka,
    ale sprawdzany BEZ importu binaries.py (patrz docstring modułu)."""
    if getattr(sys, "frozen", False):
        base = os.path.dirname(os.path.abspath(sys.executable))
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, "bin")


def _exe(name: str) -> str:
    # Mirror celowy, nie przeoczenie: to samo co binaries._exe_name() w
    # głównej apce. Ten plik świadomie nie importuje NIC z reszty repo (patrz
    # docstring modułu) — narzędzie musi dać się uruchomić nawet gdy główna
    # instalacja jest niekompletna/uszkodzona, więc drobna duplikacja stałej
    # jest tańsza niż zależność od stanu drugiego pakietu.
    return name + (".exe" if sys.platform == "win32" else "")


def _win_creationflags() -> int:
    # CREATE_NO_WINDOW — ten sam magic number co binaries.subprocess_flags()
    # w głównej apce; zduplikowany celowo (brak importu z repo, patrz _exe()).
    return 0x08000000 if sys.platform == "win32" else 0


def _find_spec_ok(module_name: str) -> bool:
    try:
        return importlib.util.find_spec(module_name) is not None
    except (ImportError, ValueError, ModuleNotFoundError):
        return False


# ── Sprawdzenia (checks) ────────────────────────────────────────────────

def check_os_version() -> CheckResult:
    system = platform.system()
    if system == "Darwin":
        ver = platform.mac_ver()[0] or "?"
        try:
            major = int(ver.split(".")[0])
        except (ValueError, IndexError):
            major = 0
        if major >= 12:
            return CheckResult("System operacyjny", OK, f"macOS {ver}")
        return CheckResult("System operacyjny", FAIL,
                            f"macOS {ver} — wymagane macOS 12 (Monterey) lub nowszy")
    if system == "Windows":
        ver = platform.win32_ver()[0] or "?"
        is_64 = platform.machine().endswith("64")
        release_ok = ver in ("10", "11") or ver == "post2012Server"
        arch_note = "x64" if is_64 else "x86 (NIEWSPIERANE — wymagane x64)"
        if release_ok and is_64:
            return CheckResult("System operacyjny", OK, f"Windows {ver} {arch_note}")
        return CheckResult("System operacyjny", FAIL,
                            f"Windows {ver} {arch_note} — wymagane Windows 10/11 x64")
    if system == "Linux":
        return CheckResult("System operacyjny", WARN,
                            f"Linux ({platform.platform()}) — wsparcie nieoficjalne")
    return CheckResult("System operacyjny", WARN, f"Nierozpoznany system: {system}")


def check_vcredist() -> CheckResult:
    """Microsoft Visual C++ Redistributable — wymagany na Windows przez
    torch/whisper/ffmpeg (msvcp140.dll, vcruntime140.dll). Nie dotyczy
    macOS/Linux — tam OK bez sprawdzania."""
    if sys.platform != "win32":
        return CheckResult("VC++ Redistributable", OK, "Nie dotyczy (macOS/Linux)")
    try:
        import winreg  # type: ignore[import-not-found]
        for hive, key in (
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\X64"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\VisualStudio\14.0\VC\Runtimes\X86"),
        ):
            try:
                with winreg.OpenKey(hive, key) as k:
                    installed, _ = winreg.QueryValueEx(k, "Installed")
                    version, _ = winreg.QueryValueEx(k, "Version")
                    if installed:
                        return CheckResult("VC++ Redistributable", OK, f"Zainstalowany ({version})")
            except OSError:
                continue
        # Fallback: obecność samych DLL-i w System32 (starsze/nietypowe instalki).
        sys32 = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "System32")
        if os.path.isfile(os.path.join(sys32, "msvcp140.dll")):
            return CheckResult("VC++ Redistributable", OK, "Wykryto msvcp140.dll w System32")
        return CheckResult("VC++ Redistributable", FAIL,
                            "Nie znaleziono — kliknij \"Instaluj brakujące komponenty\" albo "
                            "pobierz ręcznie z aka.ms/vs/17/release/vc_redist.x64.exe",
                            fix_key="vcredist")
    except Exception as exc:
        return CheckResult("VC++ Redistributable", WARN, f"Nie udało się sprawdzić: {exc}")


def check_gpu() -> CheckResult:
    """NVIDIA CUDA (Windows/Linux) lub Apple Silicon MPS (macOS). Brak GPU
    NIE jest błędem blokującym — Whisper ma bezpieczny fallback na CPU
    (patrz whisper_device.py w głównej aplikacji), więc WARN, nie FAIL."""
    if sys.platform == "darwin":
        if platform.machine() == "arm64":
            return CheckResult("GPU / akceleracja", OK, "Apple Silicon — Metal/MPS dostępne")
        return CheckResult("GPU / akceleracja", WARN,
                            "Mac Intel — brak MPS, transkrypcja na CPU")
    nvidia_smi = shutil.which("nvidia-smi")
    if nvidia_smi:
        try:
            r = subprocess.run(
                [nvidia_smi, "--query-gpu=name,driver_version", "--format=csv,noheader"],
                capture_output=True, text=True, timeout=5,
                creationflags=_win_creationflags(),
            )
            line = (r.stdout or "").strip().splitlines()[0] if r.stdout.strip() else ""
            if r.returncode == 0 and line:
                return CheckResult("GPU / akceleracja", OK, f"NVIDIA: {line}")
        except Exception:
            pass
        return CheckResult("GPU / akceleracja", WARN,
                            "nvidia-smi obecny, ale nie zwrócił danych — sterownik może wymagać restartu")
    return CheckResult("GPU / akceleracja", WARN,
                        "Brak wykrytej karty NVIDIA — transkrypcja Whisper będzie działać na CPU")


def _writable_dirs() -> dict[str, str]:
    home = os.path.expanduser("~")
    if sys.platform == "darwin":
        return {
            "Downloads": os.path.join(home, "Downloads"),
            "Desktop": os.path.join(home, "Desktop"),
            "Application Support": os.path.join(home, "Library", "Application Support"),
        }
    if sys.platform == "win32":
        return {
            "Downloads": os.path.join(home, "Downloads"),
            "Desktop": os.path.join(home, "Desktop"),
            "AppData": os.environ.get("APPDATA", os.path.join(home, "AppData", "Roaming")),
        }
    return {
        "Downloads": os.path.join(home, "Downloads"),
        "Desktop": os.path.join(home, "Desktop"),
        "Dane aplikacji": os.path.join(home, ".local", "share"),
    }


def check_write_permissions() -> CheckResult:
    dirs = _writable_dirs()
    failed, missing = [], []
    for label, path in dirs.items():
        if not os.path.isdir(path):
            missing.append(label)
            continue
        try:
            fd, tmp_path = tempfile.mkstemp(prefix=".wp_env_check_", dir=path)
            os.close(fd)
            os.remove(tmp_path)
        except OSError:
            failed.append(label)
    if failed:
        return CheckResult("Prawa zapisu", FAIL, f"Brak zapisu w: {', '.join(failed)}")
    if missing:
        return CheckResult("Prawa zapisu", WARN, f"Katalogi nie istnieją: {', '.join(missing)}")
    return CheckResult("Prawa zapisu", OK, f"OK: {', '.join(dirs.keys())}")


def _binary_version_line(path: str) -> str:
    """Pierwsza linia `<bin> -version`, przycięta do samego numeru wersji."""
    try:
        r = subprocess.run([path, "-version"], capture_output=True, text=True, timeout=3,
                            creationflags=_win_creationflags())
        first = (r.stdout or "").splitlines()[0] if r.stdout else ""
        return first.split(" Copyright")[0].strip() or os.path.basename(path)
    except Exception:
        return os.path.basename(path)


def check_helper_binaries() -> CheckResult:
    """ffmpeg/ffprobe/deno — sprawdzane obok checkera (bin/, jeśli spakowany
    razem z główną apką) i w PATH systemowym, żeby wynik był sensowny także
    gdy Environment Checker jest uruchomiony samodzielnie, bez reszty apki.
    Deno to nasz FAKTYCZNY bundlowany JS runtime dla YouTube nsig challenge —
    patrz check_nodejs() niżej, dlaczego Node.js jest tu traktowany osobno
    i tylko informacyjnie."""
    bin_dir = _bundled_bin_dir()
    found: list[str] = []
    missing: list[str] = []
    for name in ("ffmpeg", "ffprobe", "deno"):
        local = os.path.join(bin_dir, _exe(name))
        path = local if os.path.isfile(local) else shutil.which(name)
        if path:
            found.append(f"{name} ({_binary_version_line(path)})" if name != "deno" else name)
        else:
            missing.append(name)
    if missing:
        return CheckResult(
            "FFmpeg / FFprobe / Deno", WARN,
            f"Brak: {', '.join(missing)} — kliknij \"Instaluj brakujące komponenty\" "
            "(pobiera do lokalnego bin/, bez uprawnień administratora)",
            fix_key="helper_binaries",
        )
    return CheckResult("FFmpeg / FFprobe / Deno", OK, f"Znalezione: {', '.join(found)}")


def check_nodejs() -> CheckResult:
    """Node.js Runtime — OPCJONALNY. yt-dlp potrzebuje JAKIEGOŚ JS runtime do
    rozwiązania YouTube nsig challenge (deno/node/bun/qjs), ale aplikacja
    bundluje własny Deno (patrz check_helper_binaries) i to jego używa w
    pierwszej kolejności niezależnie od tego, czy Node.js jest w systemie —
    patrz yt_dlp_worker.py:_detect_js_runtime(). Brak Node.js NIE blokuje
    więc niczego; check jest tu wyłącznie informacyjny (wymóg pełnego skanu
    stosu) i na Windows oferuje opcjonalną instalację dla userów, którzy z
    innych powodów chcą mieć Node.js w systemie."""
    node = shutil.which("node")
    if node:
        try:
            r = subprocess.run([node, "--version"], capture_output=True, text=True, timeout=3,
                                creationflags=_win_creationflags())
            ver = (r.stdout or "").strip() or "?"
        except Exception:
            ver = "?"
        return CheckResult("Node.js Runtime", OK,
                            f"Znaleziono w PATH ({ver}) — opcjonalny, aplikacja i tak używa "
                            "dołączonego Deno")
    return CheckResult(
        "Node.js Runtime", WARN,
        "Brak w PATH — OPCJONALNE: aplikacja używa dołączonego Deno do JS Challenge "
        "YouTube, więc to nie blokuje działania",
        fix_key="nodejs" if sys.platform == "win32" else None,
    )


def check_pyqt6_webengine() -> CheckResult:
    """PyQt6 + QWebEngineView są BUNDLOWANE WEWNĄTRZ WP Downloader.app/.exe
    (PyInstaller) — to nie jest zależność systemowa, którą user musi osobno
    zainstalować. We frozen buildzie samego Checkera (który celowo NIE
    bundluje PyQt6, żeby zostać mały — patrz docstring modułu) brak PyQt6
    jest OCZEKIWANY, nie błędem środowiska usera. Realny check ma sens tylko
    gdy uruchomiony z repo/venv dewelopera."""
    if getattr(sys, "frozen", False):
        return CheckResult("PyQt6 + QWebEngineView", OK,
                            "Wbudowane w WP Downloader.app/.exe — Environment Checker "
                            "celowo go nie ładuje (zostaje lekki)")
    has_qt = _find_spec_ok("PyQt6.QtWidgets")
    has_webengine = _find_spec_ok("PyQt6.QtWebEngineWidgets")
    if has_qt and has_webengine:
        return CheckResult("PyQt6 + QWebEngineView", OK, "Dostępne w tym środowisku Pythona (dev)")
    missing = [n for n, ok in (("PyQt6", has_qt), ("QtWebEngineWidgets", has_webengine)) if not ok]
    return CheckResult("PyQt6 + QWebEngineView", WARN,
                        f"Brak w tym venv (dev): {', '.join(missing)} — potrzebne do "
                        "uruchomienia WP Downloader z kodu źródłowego")


def check_fastapi_uvicorn() -> CheckResult:
    """Analogicznie do PyQt6 — bundlowane wewnątrz apki, nie osobna
    zależność systemowa. Dodatkowo próbujemy żywego połączenia z portem
    8765: jeśli WP Downloader akurat działa, to najlepszy możliwy dowód,
    że serwer FastAPI/Uvicorn faktycznie wystartował i odpowiada."""
    port_alive = False
    try:
        with socket.create_connection(("127.0.0.1", 8765), timeout=0.5):
            port_alive = True
    except OSError:
        pass
    if port_alive:
        return CheckResult("FastAPI + Uvicorn", OK,
                            "WP Downloader aktualnie działa — port 8765 odpowiada")
    if getattr(sys, "frozen", False):
        return CheckResult("FastAPI + Uvicorn", OK,
                            "Wbudowane w WP Downloader.app/.exe (aplikacja obecnie nie działa)")
    has_fastapi = _find_spec_ok("fastapi")
    has_uvicorn = _find_spec_ok("uvicorn")
    if has_fastapi and has_uvicorn:
        return CheckResult("FastAPI + Uvicorn", OK,
                            "Dostępne w tym środowisku Pythona (dev); aplikacja obecnie nie działa")
    missing = [n for n, ok in (("fastapi", has_fastapi), ("uvicorn", has_uvicorn)) if not ok]
    return CheckResult("FastAPI + Uvicorn", WARN,
                        f"Brak w tym venv (dev): {', '.join(missing)}; port 8765 nie odpowiada")


def check_ytdlp_module() -> CheckResult:
    """yt-dlp Python API — sprawdzane REALNYM importem (yt-dlp, w
    przeciwieństwie do PyQt6/torch, jest lekkie — bez dużych rozszerzeń C),
    żeby zwrócić dokładną wersję i potwierdzić że YoutubeDL ma spodziewane
    API. Potwierdza to samo, co execution_instruction krok 4 tego zadania:
    yt_dlp_worker.py w głównej apce woła yt_dlp.YoutubeDL bezpośrednio,
    NIGDY subprocess na bin/yt-dlp (ten plik jest tylko zapasowym standalone
    exe, nieużywanym w runtime — patrz binaries.get_ytdlp(), wołane zero razy
    w całym repo poza własną definicją)."""
    try:
        import yt_dlp.version
        from yt_dlp import YoutubeDL

        ver = getattr(yt_dlp.version, "__version__", "?")
        has_api = hasattr(YoutubeDL, "extract_info") and hasattr(YoutubeDL, "download")
        if has_api:
            return CheckResult("yt-dlp Python Module", OK,
                                f"wersja {ver} — Python API (YoutubeDL) dostępne, bez subprocess")
        return CheckResult("yt-dlp Python Module", WARN,
                            f"wersja {ver}, ale brak spodziewanego API (extract_info/download)")
    except ImportError:
        if getattr(sys, "frozen", False):
            return CheckResult("yt-dlp Python Module", OK,
                                "Wbudowane w WP Downloader.app/.exe — Environment Checker "
                                "go nie bundluje")
        return CheckResult("yt-dlp Python Module", WARN,
                            "Nie zainstalowany w tym venv (dev) — potrzebny do uruchomienia "
                            "WP Downloader z kodu źródłowego")


def check_admin_rights() -> CheckResult:
    if sys.platform == "win32":
        try:
            is_admin = bool(ctypes.windll.shell32.IsUserAnAdmin())  # type: ignore[attr-defined]
        except Exception:
            return CheckResult("Uprawnienia administratora", WARN, "Nie udało się sprawdzić")
        return CheckResult(
            "Uprawnienia administratora", OK if is_admin else WARN,
            "Konto administratora" if is_admin
            else "Konto standardowe — instalacja per-user zadziała, systemowa może wymagać IT"
        )
    if sys.platform == "darwin":
        is_root = os.geteuid() == 0
        return CheckResult("Uprawnienia administratora", OK,
                            "Uruchomiono jako root" if is_root
                            else "Konto standardowe — wystarczające (.app nie wymaga admina na macOS)")
    return CheckResult("Uprawnienia administratora", OK, "Nie dotyczy")


ALL_CHECKS = (
    check_os_version,
    check_admin_rights,
    check_vcredist,
    check_nodejs,
    check_helper_binaries,
    check_pyqt6_webengine,
    check_fastapi_uvicorn,
    check_ytdlp_module,
    check_gpu,
    check_write_permissions,
)


def run_all_checks() -> list[CheckResult]:
    results = []
    for fn in ALL_CHECKS:
        try:
            results.append(fn())
        except Exception as exc:
            results.append(CheckResult(fn.__name__, WARN, f"Błąd sprawdzenia: {exc}"))
    return results


def build_report_text(results: list[CheckResult]) -> str:
    lines = [
        f"{APP_TITLE} — raport środowiska",
        f"System: {platform.platform()}",
        f"Python: {platform.python_version()}",
        "-" * 48,
    ]
    for r in results:
        lines.append(f"[{r.level.upper():4}] {r.name}: {r.detail}")
    n_fail = sum(1 for r in results if r.level == FAIL)
    n_warn = sum(1 for r in results if r.level == WARN)
    lines.append("-" * 48)
    lines.append(f"Podsumowanie: {n_fail} błąd(y) krytyczne, {n_warn} ostrzeżenie(a)")
    return "\n".join(lines)


# ── Auto-instalator brakujących komponentów ─────────────────────────────
# Trzy poziomy zaufania/uprawnień:
#   - helper_binaries: ZERO uprawnień, pliki lądują w lokalnym bin/ obok
#     checkera/apki — bezpieczne w trybie portable.
#   - vcredist / nodejs: wymagają elevacji na Windows (WinSxS/System32 albo
#     Program Files) — używamy ShellExecuteW(..., "runas", ...), więc to
#     WINDOWS pokazuje swój standardowy prompt UAC, a nie my po cichu
#     podnosimy uprawnienia. Sam PRZEBIEG instalatora jest cichy (/quiet),
#     ale zgoda na elevację jest zawsze widoczna i wymaga akcji usera.

def _download_to(url: str, dest_path: str, timeout: float = 120.0) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": "WP-Environment-Checker/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp, open(dest_path, "wb") as f:
        shutil.copyfileobj(resp, f)


def _mac_arch_suffix() -> str:
    return "arm64" if platform.machine() == "arm64" else "x64"


def _deno_asset_name() -> str:
    if sys.platform == "darwin":
        arch = "aarch64" if platform.machine() == "arm64" else "x86_64"
        return f"deno-{arch}-apple-darwin.zip"
    if sys.platform == "win32":
        return "deno-x86_64-pc-windows-msvc.zip"
    return "deno-x86_64-unknown-linux-gnu.zip"


def fix_helper_binaries(log) -> bool:
    """Dociąga ffmpeg/ffprobe/deno do LOKALNEGO bin/ obok checkera — bez
    admina, bez instalacji systemowej, te same źródła co scripts/
    build_local.sh i .github/workflows/build.yml. Bezpieczne w trybie
    portable (zero uprawnień wymaganych)."""
    bin_dir = _bundled_bin_dir()
    os.makedirs(bin_dir, exist_ok=True)
    ok = True

    # ffmpeg + ffprobe
    ffmpeg_path = os.path.join(bin_dir, _exe("ffmpeg"))
    ffprobe_path = os.path.join(bin_dir, _exe("ffprobe"))
    if sys.platform == "darwin":
        ff_arch = f"darwin-{_mac_arch_suffix()}"
        for name, dest in (("ffmpeg", ffmpeg_path), ("ffprobe", ffprobe_path)):
            if os.path.isfile(dest):
                continue
            url = f"https://github.com/eugeneware/ffmpeg-static/releases/download/b6.0/{name}-{ff_arch}.gz"
            log(f"Pobieranie {name}…")
            try:
                gz_path = dest + ".gz"
                _download_to(url, gz_path)
                with gzip.open(gz_path, "rb") as fin, open(dest, "wb") as fout:
                    shutil.copyfileobj(fin, fout)
                os.remove(gz_path)
                os.chmod(dest, 0o755)
                log(f"{name} zainstalowany.")
            except Exception as exc:
                log(f"Błąd pobierania {name}: {exc}")
                ok = False
    elif sys.platform == "win32":
        if not (os.path.isfile(ffmpeg_path) and os.path.isfile(ffprobe_path)):
            log("Pobieranie ffmpeg/ffprobe (BtbN/FFmpeg-Builds)…")
            try:
                zip_path = os.path.join(tempfile.gettempdir(), "ffmpeg-wp.zip")
                _download_to(
                    "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/"
                    "ffmpeg-master-latest-win64-gpl.zip",
                    zip_path,
                )
                extract_dir = os.path.join(tempfile.gettempdir(), "ffmpeg-wp-extract")
                shutil.rmtree(extract_dir, ignore_errors=True)
                with zipfile.ZipFile(zip_path) as zf:
                    zf.extractall(extract_dir)
                # Zip rozpakowuje się do jednego podkatalogu ffmpeg-*/bin/*.exe
                subdirs = [d for d in os.listdir(extract_dir)
                           if os.path.isdir(os.path.join(extract_dir, d))]
                if subdirs:
                    src_bin = os.path.join(extract_dir, subdirs[0], "bin")
                    shutil.copy2(os.path.join(src_bin, "ffmpeg.exe"), ffmpeg_path)
                    shutil.copy2(os.path.join(src_bin, "ffprobe.exe"), ffprobe_path)
                    log("ffmpeg/ffprobe zainstalowane.")
                else:
                    log("Nie znaleziono katalogu bin/ w archiwum ffmpeg.")
                    ok = False
                os.remove(zip_path)
                shutil.rmtree(extract_dir, ignore_errors=True)
            except Exception as exc:
                log(f"Błąd pobierania ffmpeg/ffprobe: {exc}")
                ok = False

    # deno
    deno_path = os.path.join(bin_dir, _exe("deno"))
    if not os.path.isfile(deno_path):
        log("Pobieranie Deno…")
        try:
            zip_path = os.path.join(tempfile.gettempdir(), "deno-wp.zip")
            _download_to(
                f"https://github.com/denoland/deno/releases/download/v{_DENO_VERSION}/"
                f"{_deno_asset_name()}",
                zip_path,
            )
            with zipfile.ZipFile(zip_path) as zf:
                zf.extractall(bin_dir)
            os.remove(zip_path)
            if sys.platform != "win32":
                os.chmod(deno_path, 0o755)
            log("Deno zainstalowany.")
        except Exception as exc:
            log(f"Błąd pobierania Deno: {exc}")
            ok = False

    return ok


def fix_vcredist(log) -> bool:
    """Windows-only: pobiera oficjalny bootstrapper Microsoftu (aka.ms —
    stały, oficjalny redirect) i uruchamia go z prośbą o elevację (UAC).
    Instalacja runtime'u C++ ZAWSZE wymaga uprawnień systemowych (WinSxS/
    System32) — nie da się tego obejść bez adminarights, więc robimy to
    jedynym uczciwym sposobem: ShellExecuteW(..., "runas", ...), co każe
    WINDOWSOWI pokazać jego własny, standardowy, WIDOCZNY prompt UAC. To
    nie jest ciche/ukryte podniesienie uprawnień — to jedynie /quiet flaga
    na samym instalatorze VC++, żeby nie zasypał usera własnym wizardem
    PO zaakceptowaniu UAC."""
    if sys.platform != "win32":
        log("VC++ Redistributable: nie dotyczy tej platformy.")
        return False
    url = "https://aka.ms/vs/17/release/vc_redist.x64.exe"
    tmp_path = os.path.join(tempfile.gettempdir(), "vc_redist.x64.exe")
    log("Pobieranie VC++ Redistributable…")
    try:
        _download_to(url, tmp_path)
    except Exception as exc:
        log(f"Błąd pobierania: {exc}")
        return False
    log("Uruchamianie instalatora (Windows pokaże prośbę o podniesienie uprawnień — UAC)…")
    try:
        rc = ctypes.windll.shell32.ShellExecuteW(  # type: ignore[attr-defined]
            None, "runas", tmp_path, "/install /quiet /norestart", None, 1)
        if rc <= 32:
            log(f"Nie udało się uruchomić instalatora (kod {rc}) — "
                "prawdopodobnie odrzucono UAC.")
            return False
        log("Instalator VC++ Redistributable uruchomiony w tle.")
        return True
    except Exception as exc:
        log(f"Błąd uruchamiania instalatora: {exc}")
        return False


def _latest_node_lts_msi_url() -> str | None:
    """Node.js nie publikuje stałego URL-a 'najnowszy LTS' — parsujemy
    oficjalny index.json (nodejs.org) i wybieramy pierwszy wpis z lts!=false
    (lista jest posortowana od najnowszego), żeby nie przypinać na sztywno
    wersji, która wygaśnie."""
    try:
        req = urllib.request.Request(
            "https://nodejs.org/dist/index.json",
            headers={"User-Agent": "WP-Environment-Checker/1.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            releases = json.load(resp)
        for rel in releases:
            if rel.get("lts"):
                ver = rel["version"]  # np. "v20.18.1"
                return f"https://nodejs.org/dist/{ver}/node-{ver}-x64.msi"
    except (urllib.error.URLError, json.JSONDecodeError, KeyError, TimeoutError):
        pass
    return None


def fix_nodejs(log) -> bool:
    """Windows-only, OPCJONALNE: instaluje Node.js LTS przez oficjalny MSI
    (msiexec /qn — cichy przebieg samego instalatora). Node.js NIE jest
    wymagany przez aplikację (Deno już pokrywa potrzebę JS runtime — patrz
    check_nodejs()), więc to czysto opcjonalna wygoda dla userów, którzy z
    innych powodów chcą mieć Node.js w systemie. Podobnie jak VC++, MSI
    per-machine wymaga elevacji — ShellExecuteW z "runas" pokazuje
    standardowy prompt UAC."""
    if sys.platform != "win32":
        log("Node.js: instalator dostępny tylko na Windows (opcjonalny — Deno już wystarcza).")
        return False
    url = _latest_node_lts_msi_url()
    if not url:
        log("Nie udało się ustalić adresu najnowszego Node.js LTS.")
        return False
    tmp_path = os.path.join(tempfile.gettempdir(), "node-lts.msi")
    log(f"Pobieranie Node.js LTS ({url.rsplit('/', 2)[1]})…")
    try:
        _download_to(url, tmp_path)
    except Exception as exc:
        log(f"Błąd pobierania: {exc}")
        return False
    log("Uruchamianie instalatora MSI (Windows pokaże prośbę o UAC)…")
    try:
        params = f'/i "{tmp_path}" /qn /norestart'
        rc = ctypes.windll.shell32.ShellExecuteW(  # type: ignore[attr-defined]
            None, "runas", "msiexec.exe", params, None, 1)
        if rc <= 32:
            log(f"Nie udało się uruchomić instalatora (kod {rc}) — "
                "prawdopodobnie odrzucono UAC.")
            return False
        log("Instalator Node.js uruchomiony w tle.")
        return True
    except Exception as exc:
        log(f"Błąd uruchamiania instalatora: {exc}")
        return False


FIXERS = {
    "helper_binaries": fix_helper_binaries,
    "vcredist": fix_vcredist,
    "nodejs": fix_nodejs,
}


class EnvCheckerApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.results: list[CheckResult] = []
        root.title(APP_TITLE)
        root.geometry("660x560")
        root.minsize(560, 460)

        header_font = tkfont.Font(family="Helvetica", size=15, weight="bold")
        mono_font = tkfont.Font(family="Courier", size=10)

        header = ttk.Frame(root, padding=(16, 16, 16, 8))
        header.pack(fill="x")
        ttk.Label(header, text=APP_TITLE, font=header_font).pack(anchor="w")
        ttk.Label(
            header,
            text="Pełny skan stosu technologicznego WP Downloader (Krok 0)",
        ).pack(anchor="w")

        self.list_frame = ttk.Frame(root, padding=(16, 4))
        self.list_frame.pack(fill="both", expand=True)

        self.status_var = tk.StringVar(value="Sprawdzanie środowiska…")
        ttk.Label(root, textvariable=self.status_var, padding=(16, 4)).pack(anchor="w")

        btns = ttk.Frame(root, padding=16)
        btns.pack(fill="x")
        self.install_btn = ttk.Button(
            btns, text="Instaluj brakujące komponenty",
            command=self._install_missing, state="disabled")
        self.install_btn.pack(side="left")
        self.copy_btn = ttk.Button(
            btns, text="Kopiuj raport dla Administratora IT",
            command=self._copy_report, state="disabled")
        self.copy_btn.pack(side="left", padx=8)
        ttk.Button(btns, text="Sprawdź ponownie", command=self._rerun).pack(side="left", padx=8)
        ttk.Button(btns, text="Zamknij", command=root.destroy).pack(side="right")

        self._mono_font = mono_font
        self._rerun()

    def _rerun(self) -> None:
        for w in self.list_frame.winfo_children():
            w.destroy()
        self.copy_btn.configure(state="disabled")
        self.install_btn.configure(state="disabled")
        self.status_var.set("Sprawdzanie środowiska…")
        threading.Thread(target=self._run_in_background, daemon=True).start()

    def _run_in_background(self) -> None:
        results = run_all_checks()
        self.root.after(0, self._on_results, results)

    def _on_results(self, results: list[CheckResult]) -> None:
        self.results = results
        for r in results:
            row = ttk.Frame(self.list_frame)
            row.pack(fill="x", pady=3)
            icon = ttk.Label(row, text=_ICON[r.level], width=3)
            icon.pack(side="left")
            text = ttk.Label(row, text=f"{r.name}: {r.detail}", wraplength=560, justify="left")
            text.pack(side="left", fill="x", expand=True)
        n_fail = sum(1 for r in results if r.level == FAIL)
        n_warn = sum(1 for r in results if r.level == WARN)
        n_fixable = sum(1 for r in results if r.fix_key and r.level != OK)
        if n_fail:
            self.status_var.set(f"⚠ {n_fail} błąd(y) krytyczne — instalacja może się nie powieść")
        elif n_warn:
            self.status_var.set(f"{n_warn} ostrzeżenie(a) — środowisko sprawne, kilka uwag")
        else:
            self.status_var.set("Wszystko sprawne — środowisko gotowe na WP Downloader")
        self.copy_btn.configure(state="normal")
        self.install_btn.configure(state="normal" if n_fixable else "disabled")

    def _install_missing(self) -> None:
        to_fix = [r for r in self.results if r.fix_key and r.level != OK]
        if not to_fix:
            return
        self.install_btn.configure(state="disabled")
        self.copy_btn.configure(state="disabled")
        threading.Thread(target=self._install_in_background, args=(to_fix,), daemon=True).start()

    def _install_in_background(self, to_fix: list[CheckResult]) -> None:
        def log(msg: str) -> None:
            self.root.after(0, self.status_var.set, msg)

        seen_keys: set[str] = set()
        for r in to_fix:
            if r.fix_key in seen_keys or r.fix_key not in FIXERS:
                continue
            seen_keys.add(r.fix_key)
            log(f"Naprawianie: {r.name}…")
            try:
                FIXERS[r.fix_key](log)
            except Exception as exc:
                log(f"Błąd naprawy {r.name}: {exc}")
        self.root.after(0, self._rerun)

    def _copy_report(self) -> None:
        report = build_report_text(self.results)
        self.root.clipboard_clear()
        self.root.clipboard_append(report)
        # clipboard_append wymaga update, żeby zawartość przetrwała zamknięcie okna
        # zanim schowek zdąży ją realnie przejąć od procesu Tk.
        self.root.update()
        self.status_var.set("Raport skopiowany do schowka ✓")


def main() -> None:
    root = tk.Tk()
    EnvCheckerApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
