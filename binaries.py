"""binaries.py — centralne rozwiązywanie ścieżek do spakowanych binarek CLI.

Zero-dependency: aplikacja NIE polega na globalnym PATH ani na narzędziach
zainstalowanych w systemie (Homebrew, systemowy ffmpeg itd.). Wszystkie
narzędzia są dołączone do paczki i uruchamiane z wewnętrznego katalogu:

    ffmpeg, ffprobe, yt-dlp (standalone), deno   →  <app>/bin/

yt-dlp jest DODATKOWO bundlowany jako moduł Pythona (in-process YoutubeDL
API) — to główna ścieżka pobierania. Binarka bin/yt-dlp jest zapasowa
i spełnia wymóg „standalone executable".

Kolejność wyszukiwania każdej binarki:
  1. bundled `bin/` (obok exe / w _MEIPASS / .app Contents/Resources/bin)
  2. static ffmpeg z pakietu imageio-ffmpeg (fallback z gwarantowanym drawtext)
  3. PATH — tylko jako ostatnia deska ratunku w trybie deweloperskim
"""

from __future__ import annotations

import os
import shutil
import sys
from functools import lru_cache


def _exe_name(name: str) -> str:
    return name + (".exe" if sys.platform == "win32" else "")


def bin_dirs() -> list[str]:
    """Kandydujące lokalizacje wewnętrznego katalogu `bin/`.

    Frozen (PyInstaller):
      - `_MEIPASS/bin` (onedir rozpakowuje datas obok/rozproszone),
      - `<exe_dir>/bin` (obok pliku wykonywalnego, np. Windows onedir),
      - `<exe_dir>/../Resources/bin` (macOS .app: exe w Contents/MacOS/,
        zasoby w Contents/Resources/).
    Dev: `<repo>/bin`.
    """
    cands: list[str] = []
    if hasattr(sys, "_MEIPASS"):
        cands.append(os.path.join(sys._MEIPASS, "bin"))
        exe_dir = os.path.dirname(os.path.abspath(sys.executable))
        cands.append(os.path.join(exe_dir, "bin"))
        cands.append(os.path.join(exe_dir, "..", "Resources", "bin"))
    cands.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), "bin"))
    # Deduplikacja z zachowaniem kolejności
    seen: set[str] = set()
    out: list[str] = []
    for c in cands:
        n = os.path.normpath(c)
        if n not in seen:
            seen.add(n)
            out.append(n)
    return out


def _find_bundled(name: str) -> str:
    """Bezwzględna ścieżka do bundlowanej binarki `name` albo "" gdy brak."""
    fn = _exe_name(name)
    for d in bin_dirs():
        p = os.path.join(d, fn)
        if os.path.isfile(p):
            return p
    return ""


@lru_cache(maxsize=1)
def get_ffmpeg() -> str:
    """Ścieżka do ffmpeg: bundled `bin/ffmpeg` → imageio static → PATH.

    Bundlowany bin/ffmpeg to statyczny build z pełnym zestawem
    (h264_videotoolbox, libvpx-vp9, libopus, drawtext) — ten sam, na którym
    przeszły testy Fast Cuttera."""
    p = _find_bundled("ffmpeg")
    if p:
        return p
    try:
        import imageio_ffmpeg
        q = imageio_ffmpeg.get_ffmpeg_exe()
        if q and os.path.isfile(q):
            return q
    except Exception:
        pass
    return shutil.which("ffmpeg") or "ffmpeg"


@lru_cache(maxsize=1)
def get_ffprobe() -> str:
    """Ścieżka do ffprobe: bundled `bin/ffprobe` → PATH.

    imageio-ffmpeg NIE dostarcza ffprobe, więc bez bundlowanego bin/ffprobe
    schodzimy od razu do PATH (dev)."""
    p = _find_bundled("ffprobe")
    if p:
        return p
    return shutil.which("ffprobe") or "ffprobe"


@lru_cache(maxsize=1)
def get_ytdlp() -> str:
    """Ścieżka do standalone bin/yt-dlp (zapas). "" gdy brak — silnik i tak
    używa modułu yt_dlp in-process."""
    return _find_bundled("yt-dlp")


@lru_cache(maxsize=1)
def get_ffmpeg_location() -> str:
    """Katalog przekazywany do yt-dlp jako `ffmpeg_location` — musi zawierać
    ZARÓWNO ffmpeg jak i ffprobe (yt-dlp scala nimi strumienie).

    Zwraca pierwszy `bin/` mający obie binarki; "" gdy żaden (dev bez builda
    — yt-dlp użyje wtedy PATH)."""
    for d in bin_dirs():
        if (os.path.isfile(os.path.join(d, _exe_name("ffmpeg")))
                and os.path.isfile(os.path.join(d, _exe_name("ffprobe")))):
            return d
    return ""


def prepend_bin_to_path() -> None:
    """Dodaje bundlowany `bin/` na POCZĄTEK PATH procesu (raz, przy starcie).

    Dzięki temu każde pośrednie wyszukiwanie narzędzia (np. yt-dlp szukające
    ffmpeg do merge, albo biblioteka wołająca `ffprobe`) trafia najpierw
    w nasze spakowane binarki, a nie w systemowe. Nie usuwa istniejącego
    PATH — tylko nadaje priorytet."""
    added: list[str] = []
    for d in bin_dirs():
        if os.path.isdir(d) and d not in added:
            added.append(d)
    if not added:
        return
    cur = os.environ.get("PATH", "")
    parts = cur.split(os.pathsep) if cur else []
    new_parts = [d for d in added if d not in parts] + parts
    os.environ["PATH"] = os.pathsep.join(new_parts)
