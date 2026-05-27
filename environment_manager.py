"""
EnvironmentManager — wersje środowiska (Python, yt-dlp, PHP) jak w dokumentacji.

Uruchamia lekkie zapytania do procesów systemowych; wyniki można cache'ować na endpoint.
"""

from __future__ import annotations

import shutil
import subprocess 
import sys
import os
import re
from functools import lru_cache
from typing import Any


@lru_cache(maxsize=32)
def _run_version(cmd: tuple[str, ...], timeout: float = 5.0) -> str:
    creationflags = 0
    if sys.platform == "win32":
        creationflags = 0x08000000  # CREATE_NO_WINDOW

    try:
        r = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            creationflags=creationflags
        )
        stdout = (r.stdout or "").strip()
        if not stdout:
            return "?"
        # Szukamy wzorca wersji w całym wyjściu (pierwsze dopasowanie)
        # Obsługuje formaty: 2024.11.04, 6.11.0, 8.2
        match = re.search(r'(\d{4}\.\d{2}\.\d{2}|\d+\.\d+(\.\d+)?)', stdout)
        if match:
            return match.group(1)
        # Jeśli brak wzorca, spróbujmy wziąć ostatnie słowo pierwszej linii
        first_line = stdout.splitlines()[0].strip() if stdout else ""
        return first_line.split()[-1] if first_line else "?"
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return ""


def get_python_version() -> str:
    v = sys.version_info
    return f"{v.major}.{v.minor}.{v.micro}"


def get_whisper_version() -> str:
    try:
        from importlib.metadata import version
        return version("openai-whisper")
    except Exception:
        return "brak"


def get_streamlink_version() -> str:
    try:
        from importlib.metadata import version
        return version("streamlink")
    except Exception:
        return "brak"

def get_yt_dlp_version() -> str:
    # W wersji EXE yt-dlp nie jest w PATH, wywołujemy go przez silnik Pythona (sys.executable)
    v = _run_version((sys.executable, "-m", "yt_dlp", "--version"))
    if not v or v == "?":
        # Jeśli nie zadziałało, sprawdźmy tradycyjnie w PATH
        if shutil.which("yt-dlp"):
            v = _run_version(("yt-dlp", "--version"))
    return v or "brak"


def get_ffmpeg_version() -> str:
    v = _run_version(("ffmpeg", "-version"))
    return v if v and v != "?" else "brak"


def get_php_version() -> str:
    # Próbujemy bezpośrednio wywołać php. 
    # Jeśli add_local_bin_to_path w main.py zadziałało, subprocess znajdzie go w PATH.
    v = _run_version(("php", "-v"))
    if v and v != "?":
        return v
    return ""


def get_obs_local_version() -> str:
    """Detect locally installed OBS Studio version without WebSocket connection."""
    import platform
    system = platform.system()

    if system == "Darwin":
        plist_path = "/Applications/OBS.app/Contents/Info.plist"
        if os.path.exists(plist_path):
            try:
                import plistlib
                with open(plist_path, "rb") as f:
                    plist = plistlib.load(f)
                v = plist.get("CFBundleShortVersionString", "")
                if v:
                    return v
            except Exception:
                pass

    elif system == "Windows":
        for base in (
            r"C:\Program Files\obs-studio",
            r"C:\Program Files (x86)\obs-studio",
        ):
            if os.path.isdir(base):
                # Try reading version from uninstaller log or a known file
                for candidate in (
                    os.path.join(base, "data", "obs-studio", "license", "obs-studio.txt"),
                    os.path.join(base, "cmake", "OBSConfig.cmake"),
                ):
                    if os.path.exists(candidate):
                        try:
                            with open(candidate, encoding="utf-8", errors="replace") as f:
                                first = f.readline()
                            m = re.search(r'(\d+\.\d+\.\d+)', first)
                            if m:
                                return m.group(1)
                        except Exception:
                            pass
                return "installed"  # OBS found but version unreadable

    # Fallback: try binary (rarely works for GUI apps)
    v = _run_version(("obs", "--version"), timeout=2.0)
    return v if v and v != "?" else ""


def collect_system_info(app_version: str = "1.0") -> dict[str, Any]:
    php = get_php_version()
    return {
        "app_version": app_version,
        "python": get_python_version(),
        "yt_dlp": get_yt_dlp_version(),
        "streamlink": get_streamlink_version(),
        "whisper": get_whisper_version(),
        "ffmpeg": get_ffmpeg_version(),
        "php": php,
        "php_available": bool(php),
        "obs_local_version": get_obs_local_version(),
    }
