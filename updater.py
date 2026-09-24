"""updater.py — natywna aktualizacja aplikacji z GitHub Releases.

Dotychczasowe `/api/system/update` aktualizowało WYŁĄCZNIE komponenty
pythonowe (yt-dlp, streamlink) przez pip. Samej aplikacji nie podmieniało —
user musiał ręcznie pobrać nowy build ze strony wydania. Ten moduł to
domyka: sprawdza najnowsze wydanie, porównuje z `APP_VERSION`, pobiera
właściwą paczkę dla systemu i wykonuje bezpieczną podmianę.

Cross-platform (wymóg #1): każda operacja na ścieżkach idzie przez
`pathlib.Path`, a rozgałęzienia systemowe są jawne
(`sys.platform == "darwin"` / `"win32"`).

UWAGA co do tagów: to repozytorium publikuje RÓWNOLEGLE dwa tagi na ten sam
commit — `1.0` oraz `v1.0` — a `releases/latest` zwraca goły `1.0`. Dlatego
`parse_version` musi tolerować oba zapisy, inaczej aplikacja w kółko
pokazywałaby „dostępna nowa wersja" przy identycznym wydaniu.
"""

from __future__ import annotations

import json
import logging
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

# Jedno źródło prawdy o wersji aplikacji. `environment_manager.collect_system_info`
# i `/api/system-info` czytają to samo, żeby UI, updater i footer nie rozjechały się.
APP_VERSION = "1.0"

GITHUB_OWNER = "geroo94"
GITHUB_REPO = "wp-downloader"
RELEASES_LATEST_URL = (
    f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/releases/latest"
)

_UA = {"User-Agent": f"WP-Downloader/{APP_VERSION}"}

# Nazwy paczek GŁÓWNEJ aplikacji. WP_Environment_Checker.* to osobne
# narzędzie diagnostyczne — ma taki sam sufiks (.exe/.zip), więc bez jawnego
# prefiksu updater mógłby podmienić aplikację... checkerem.
_MAIN_APP_PREFIX = "WP_Downloader"


def parse_version(tag: str) -> tuple[int, int, int]:
    """`v1.2.3` / `1.2` / `v1.2.3-beta` → krotka (major, minor, patch).

    Krotka, a nie string, bo porównanie leksykalne uznałoby „1.10" < „1.9".
    Nierozpoznane człony dają 0 — wtedy `is_update_available` bezpiecznie
    stwierdzi brak aktualizacji zamiast proponować pobranie 700 MB.
    """
    if not tag:
        return (0, 0, 0)
    s = str(tag).strip().lstrip("vV")
    # Odetnij sufiks pre-release / build: 1.2.3-beta1, 1.2.3+build7
    for sep in ("-", "+", " "):
        if sep in s:
            s = s.split(sep, 1)[0]
    parts = s.split(".")
    out: list[int] = []
    for i in range(3):
        try:
            out.append(int(parts[i]))
        except (IndexError, ValueError):
            out.append(0)
    return (out[0], out[1], out[2])


def compare_versions(a: str, b: str) -> int:
    """-1 gdy a<b, 0 gdy równe, 1 gdy a>b (po znormalizowaniu tagów)."""
    pa, pb = parse_version(a), parse_version(b)
    return (pa > pb) - (pa < pb)


def is_update_available(local: str, remote: str) -> bool:
    """Czy zdalne wydanie jest NOWSZE od lokalnego.

    Świadomie tylko „nowsze": gdy zdalny tag jest starszy (np. ktoś cofnął
    wydanie), NIE proponujemy „aktualizacji" w dół — to by zdegradowało
    działającą instalację."""
    if not remote:
        return False
    return compare_versions(local, remote) < 0


def pick_asset_for_platform(
    assets: list[dict], plat: Optional[str] = None
) -> Optional[dict]:
    """Wybiera paczkę GŁÓWNEJ aplikacji pasującą do systemu.

    Priorytet celowo stawia instalator/DMG przed wersją portable — to on jest
    zalecaną ścieżką w dokumentacji wydania. Zwraca None, gdy w wydaniu nie ma
    nic pasującego (np. same paczki Environment Checkera): lepiej powiedzieć
    „brak paczki dla tego systemu" niż podmienić aplikację czymś innym.
    """
    plat = plat or sys.platform
    if plat == "darwin":
        order = ("WP_Downloader_macOS.dmg", "WP_Downloader_macOS_PORTABLE.zip")
    elif plat == "win32":
        order = ("WP_Downloader_Setup.exe", "WP_Downloader_Windows.zip")
    else:
        return None

    by_name = {
        a.get("name"): a
        for a in assets
        if str(a.get("name", "")).startswith(_MAIN_APP_PREFIX)
    }
    for wanted in order:
        if wanted in by_name:
            return by_name[wanted]
    return None


def fetch_latest_release(timeout: float = 15.0) -> dict[str, Any]:
    """Pobiera metadane najnowszego wydania z GitHub API.

    Bez tokenu — publiczne repo, limit 60 zapytań/h na IP w zupełności
    wystarcza do ręcznego „Sprawdź aktualizacje"."""
    req = urllib.request.Request(RELEASES_LATEST_URL, headers=_UA)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def check_for_update(local_version: str = APP_VERSION) -> dict[str, Any]:
    """Zwraca gotowy dla UI opis stanu aktualizacji.

    Kształt odpowiedzi jest stały niezależnie od wyniku, żeby frontend nie
    musiał zgadywać: {available, current, latest, notes, asset, error}.
    """
    out: dict[str, Any] = {
        "available": False,
        "current": local_version,
        "latest": "",
        "name": "",
        "notes": "",
        "published_at": "",
        "asset": None,
        "error": "",
    }
    try:
        rel = fetch_latest_release()
    except urllib.error.HTTPError as e:
        out["error"] = (
            "Limit zapytań GitHub API (spróbuj za godzinę)"
            if e.code == 403
            else f"GitHub API zwróciło HTTP {e.code}"
        )
        return out
    except Exception as e:  # sieć/DNS/timeout
        out["error"] = f"Brak połączenia z GitHub: {e}"
        return out

    tag = str(rel.get("tag_name") or "")
    out["latest"] = tag
    out["name"] = str(rel.get("name") or "")
    out["notes"] = str(rel.get("body") or "")
    out["published_at"] = str(rel.get("published_at") or "")
    out["available"] = is_update_available(local_version, tag)

    asset = pick_asset_for_platform(rel.get("assets") or [])
    if asset:
        out["asset"] = {
            "name": asset.get("name"),
            "url": asset.get("browser_download_url"),
            "size": asset.get("size", 0),
        }
    elif out["available"]:
        out["error"] = "Nowe wydanie nie zawiera paczki dla tego systemu."
    return out


def download_asset(
    url: str,
    dest: Path,
    progress_cb: Optional[Callable[[int, int], None]] = None,
    timeout: float = 60.0,
) -> Path:
    """Pobiera plik do `dest`, raportując (pobrane, całość) w bajtach.

    Zapis idzie do pliku `.part` i dopiero kompletny jest przenoszony na
    docelową nazwę — przerwane pobieranie nie zostawia czegoś, co wygląda
    jak gotowa paczka."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + ".part")

    req = urllib.request.Request(url, headers=_UA)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        total = int(resp.headers.get("Content-Length") or 0)
        done = 0
        with open(part, "wb") as f:
            while True:
                chunk = resp.read(1024 * 256)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                if progress_cb:
                    progress_cb(done, total)

    got = part.stat().st_size if part.exists() else 0
    if total and got != total:
        # Rozmiar odczytujemy PRZED skasowaniem — inaczej komunikat o
        # niekompletnym pliku sam wywalal sie na FileNotFoundError.
        part.unlink(missing_ok=True)
        raise IOError(f"Pobrano {got} B zamiast {total} B — plik niekompletny")
    part.replace(dest)
    return dest


def _extract_zip_preserving_modes(zf: zipfile.ZipFile, dest: Path) -> None:
    """Rozpakowuje ZIP ZACHOWUJĄC bity uprawnień z nagłówków archiwum.

    `ZipFile.extractall()` gubi tryb POSIX — wszystko ląduje jako 0644.
    Dla paczki z aplikacją to fatalne: `Contents/MacOS/WP_Downloader`
    i bundlowane `bin/ffmpeg` tracą +x, więc zaktualizowana aplikacja
    w ogóle się nie uruchamia (wykryte testem podmiany na atrapie .app).
    """
    for info in zf.infolist():
        target = dest / info.filename
        mode = info.external_attr >> 16
        # `is_dir()` patrzy WYŁĄCZNIE na ukośnik na końcu nazwy. Nie każde
        # narzędzie pakujące go dodaje — bez sprawdzenia bitu S_IFDIR taki
        # katalog wylądowałby jako pusty PLIK i cała reszta rozpakowywania
        # wywalała się na FileExistsError (wykryte testem podmiany).
        if info.is_dir() or (mode and (mode & 0o170000) == 0o040000):
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        # Dowiązania symboliczne w .app (np. Frameworks/Current) muszą
        # zostać dowiązaniami, inaczej bundle puchnie i traci strukturę.
        if mode and (mode & 0o170000) == 0o120000:  # S_IFLNK
            link_target = zf.read(info).decode("utf-8")
            if target.exists() or target.is_symlink():
                target.unlink()
            target.symlink_to(link_target)
            continue
        with zf.open(info) as src, open(target, "wb") as out:
            shutil.copyfileobj(src, out)
        if mode:
            target.chmod(mode & 0o7777)


def staging_dir() -> Path:
    """Katalog roboczy aktualizacji (poza bundlem aplikacji)."""
    base = Path(tempfile.gettempdir()) / "wp_downloader_update"
    base.mkdir(parents=True, exist_ok=True)
    return base


def current_app_bundle() -> Optional[Path]:
    """Ścieżka do zainstalowanej aplikacji, którą trzeba podmienić.

    macOS: katalog `*.app` (exe siedzi w Contents/MacOS/).
    Windows: katalog zawierający `WP_Downloader.exe` (onedir PyInstallera).
    Dev (niezamrożony): None — nie ma czego podmieniać.
    """
    if not getattr(sys, "frozen", False):
        return None
    exe = Path(sys.executable).resolve()
    if sys.platform == "darwin":
        for parent in exe.parents:
            if parent.suffix == ".app":
                return parent
        return None
    return exe.parent


def _macos_restart_cmd(app_bundle: Path, old: Path, work: Path,
                       downloaded: Path) -> list[str]:
    """Komenda restartu odpalana PO wyjściu z aplikacji (odczepiony proces).

    Czeka na śmierć naszego procesu, dopiero potem startuje nową wersję i na
    końcu sprząta. Czekanie jest konieczne z dwóch powodów: port serwera
    (8765) jest stały, więc instancja wystartowana za wcześnie dostałaby
    „address already in use" i pokazała pusty widok; a kasowanie starego
    bundla w trakcie życia UI wywraca leniwe ładowanie zasobów Qt.
    """
    q = shlex.quote
    script = (
        f"pid={os.getpid()}\n"
        # do 60 s cierpliwości; potem i tak startujemy, żeby nie zostawić
        # użytkownika bez aplikacji przez zawieszony proces.
        "i=0\n"
        "while kill -0 $pid 2>/dev/null && [ $i -lt 120 ]; do sleep 0.5; i=$((i+1)); done\n"
        "sleep 1\n"
        f"open -n {q(str(app_bundle))}\n"
        f"rm -rf {q(str(old))} {q(str(work))}\n"
        f"rm -f {q(str(downloaded))}\n"
    )
    return ["/bin/sh", "-c", script]


def apply_update_macos(downloaded: Path, app_bundle: Path) -> dict[str, Any]:
    """Podmiana .app na macOS: rozpakuj/zamontuj → złóż obok → przełącz.

    Kolejność jest ostrożna z premedytacją:
      1. nowy bundle powstaje OBOK starego (`*.app.new`) i tam jest czyszczony
         z kwarantanny oraz podpisywany,
      2. gdy podpis się nie uda — przerywamy PRZED podmianą: bundle bez
         poprawnego podpisu dostaje na Apple Silicon SIGKILL przy starcie,
         więc user zostałby bez działającej aplikacji,
      3. sama podmiana to dwa `rename` z rollbackiem,
      4. starego bundla NIE kasujemy tutaj — robi to skrypt restartu już po
         wyjściu z procesu (patrz `_macos_restart_cmd`).
    """
    downloaded, app_bundle = Path(downloaded), Path(app_bundle)
    work = staging_dir() / "extract"
    if work.exists():
        shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)

    staged = app_bundle.with_name(app_bundle.name + ".new")
    if staged.exists():
        shutil.rmtree(staged, ignore_errors=True)

    mount_point: Optional[str] = None
    try:
        if downloaded.suffix.lower() == ".dmg":
            out = subprocess.run(
                ["hdiutil", "attach", "-nobrowse", "-readonly", str(downloaded)],
                capture_output=True, text=True, check=True,
            )
            for line in out.stdout.splitlines():
                if "/Volumes/" in line:
                    mount_point = line.split("\t")[-1].strip()
            if not mount_point:
                raise IOError("Nie udało się zamontować DMG")
            src = next(iter(Path(mount_point).glob("*.app")), None)
            if src is None:
                raise IOError("W obrazie DMG nie znaleziono pliku .app")
            # Kopiujemy PROSTO na miejsce docelowe — przystanek w katalogu
            # tymczasowym oznaczałby drugie 700 MB zajęte na dysku.
            shutil.copytree(src, staged, symlinks=True)
        else:  # .zip
            with zipfile.ZipFile(downloaded) as zf:
                _extract_zip_preserving_modes(zf, work)
            src = next(iter(work.rglob("*.app")), None)
            if src is None:
                raise IOError("W paczce nie znaleziono pliku .app")
            shutil.copytree(src, staged, symlinks=True)

        # Pas i szelki na bit wykonywalności: nawet gdy archiwum nie niosło
        # trybu POSIX, główny plik wykonywalny i bundlowane binarki MUSZĄ być
        # +x — inaczej podmieniona aplikacja nie wystartuje. To ten sam krok,
        # który robi scripts/build_local.sh po rozpakowaniu paczek.
        for d in (staged / "Contents" / "MacOS", staged / "Contents" / "Resources" / "bin"):
            if d.is_dir():
                for f in d.iterdir():
                    if f.is_file():
                        f.chmod(f.stat().st_mode | 0o111)

        # Gatekeeper: pobrane pliki mają kwarantannę, a bundle jest
        # niepodpisany developer ID -> bez tych dwóch kroków nowa wersja
        # nie wystartuje (to samo robi scripts/build_local.sh).
        subprocess.run(["xattr", "-cr", str(staged)], check=False)
        sign = subprocess.run(
            ["codesign", "--force", "--deep", "--sign", "-", str(staged)],
            capture_output=True, text=True,
        )
        if sign.returncode != 0:
            raise IOError(
                "Podpisanie nowej wersji nie powiodło się — aktualizację "
                "przerwano, aplikacja zostaje w obecnej wersji. "
                + (sign.stderr or "").strip()[:300]
            )

        old = app_bundle.with_name(app_bundle.name + ".old")
        if old.exists():
            shutil.rmtree(old, ignore_errors=True)
        app_bundle.rename(old)
        try:
            staged.rename(app_bundle)
        except Exception:
            # Rollback: lepiej stara wersja na swoim miejscu niż brak
            # aplikacji (Dock/Spotlight/`open` przestałyby cokolwiek widzieć).
            old.rename(app_bundle)
            raise

        return {"ok": True,
                "restart_cmd": _macos_restart_cmd(app_bundle, old, work, downloaded)}
    except Exception:
        shutil.rmtree(staged, ignore_errors=True)
        raise
    finally:
        if mount_point:
            subprocess.run(["hdiutil", "detach", mount_point], check=False,
                           capture_output=True)


def _windows_wait_block() -> str:
    """Fragment .bat czekający aż NASZ proces zniknie z listy zadań.

    Sam `timeout /t 3` to zgadywanka: dopóki .exe jest otwarty, robocopy nie
    podmieni plików (a cichy błąd oznaczałby restart STAREJ wersji z komunikatem
    „zaktualizowano"). Czekamy więc po PID, maksymalnie ~60 s.
    """
    return f'''set PID={os.getpid()}
set /a WAITED=0
:waitloop
tasklist /FI "PID eq %PID%" 2>nul | find "%PID%" >nul
if errorlevel 1 goto ready
set /a WAITED+=1
if %WAITED% GEQ 60 goto ready
timeout /t 1 /nobreak >nul
goto waitloop
:ready
timeout /t 2 /nobreak >nul
'''


def apply_update_windows(downloaded: Path, app_dir: Path) -> dict[str, Any]:
    """Podmiana na Windows przez skrypt .bat odpalany PO wyjściu z aplikacji.

    Nie da się nadpisać działającego .exe — dlatego generujemy skrypt, który
    czeka aż proces zniknie, podmienia pliki i uruchamia nową wersję.
    Instalator (.exe) po prostu odpalamy w trybie cichym; paczkę portable
    rozpakowujemy i kopiujemy po zamknięciu aplikacji. Każdy krok sprawdza
    kod wyjścia — po cichu nieudana podmiana to najgorszy możliwy wariant.
    """
    downloaded, app_dir = Path(downloaded), Path(app_dir)
    bat = staging_dir() / "wp_apply_update.bat"
    # normpath: w skrypcie cmd-a musza byc rodzime ukosniki (wymog #1).
    exe = os.path.normpath(str(app_dir / "WP_Downloader.exe"))
    app_dir_s = os.path.normpath(str(app_dir))
    downloaded_s = os.path.normpath(str(downloaded))
    wait = _windows_wait_block()

    if downloaded.suffix.lower() == ".exe":
        # Instalator sam zamyka/aktualizuje pliki — wystarczy go odpalić po wyjściu.
        script = f"""@echo off
{wait}
start "" /wait "{downloaded_s}" /SILENT /NORESTART
if errorlevel 1 (
  echo.
  echo Instalator zakonczyl sie bledem - aplikacja pozostaje w starej wersji.
  echo.
  pause
)
del /q "{downloaded_s}" >nul 2>&1
start "" "{exe}"
del "%~f0"
"""
    else:
        extract = staging_dir() / "extract"
        if extract.exists():
            shutil.rmtree(extract, ignore_errors=True)
        with zipfile.ZipFile(downloaded) as zf:
            _extract_zip_preserving_modes(zf, extract)
        # Paczka portable ma pojedynczy katalog na szczycie.
        roots = [p for p in extract.iterdir() if p.is_dir()]
        src = os.path.normpath(str(roots[0] if len(roots) == 1 else extract))
        extract_s = os.path.normpath(str(extract))
        # robocopy: kod < 8 to sukces (8 i wyżej = realny blad kopiowania).
        script = f"""@echo off
{wait}
set /a TRIES=0
:copyloop
robocopy "{src}" "{app_dir_s}" /E /IS /IT /R:2 /W:2 >nul
if not errorlevel 8 goto copied
set /a TRIES+=1
if %TRIES% GEQ 5 goto copyfail
timeout /t 3 /nobreak >nul
goto copyloop
:copyfail
echo.
echo Nie udalo sie podmienic plikow aplikacji - pliki sa zablokowane.
echo Zamknij WP Downloader calkowicie i uruchom aktualizacje ponownie.
echo.
pause
start "" "{exe}"
del "%~f0"
exit /b 1
:copied
rmdir /s /q "{extract_s}" >nul 2>&1
del /q "{downloaded_s}" >nul 2>&1
start "" "{exe}"
del "%~f0"
"""

    bat.write_text(script, encoding="utf-8")
    return {"ok": True, "restart_cmd": ["cmd", "/c", "start", "", str(bat)]}


def cleanup_stale_updates() -> None:
    """Sprząta pozostałości po aktualizacji: `*.app.old` / `*.app.new` obok
    aplikacji oraz katalog roboczy w temp.

    Normalnie kasuje je skrypt restartu, ale gdy ktoś go ubije (albo system
    padnie w trakcie), zostałoby ~700 MB śmieci. Wołane na starcie aplikacji,
    całość pod try/except — to sprzątanie, nie ścieżka krytyczna.
    """
    try:
        bundle = current_app_bundle()
        if bundle is not None:
            for suffix in (".old", ".new"):
                leftover = bundle.with_name(bundle.name + suffix)
                if leftover.exists():
                    logger.info("Usuwam pozostałość po aktualizacji: %s", leftover)
                    shutil.rmtree(leftover, ignore_errors=True)
        base = Path(tempfile.gettempdir()) / "wp_downloader_update"
        if base.exists():
            shutil.rmtree(base, ignore_errors=True)
    except Exception:
        logger.warning("cleanup_stale_updates: sprzątanie nie powiodło się",
                       exc_info=True)


def apply_update(downloaded: Path, app_bundle: Optional[Path] = None) -> dict[str, Any]:
    """Rozgałęzienie systemowe podmiany. Zwraca komendę restartu dla UI."""
    target = Path(app_bundle) if app_bundle else current_app_bundle()
    if target is None:
        return {"ok": False, "error":
                "Aktualizacja działa tylko w zbudowanej aplikacji (nie w trybie dev)."}
    downloaded = Path(downloaded)
    if not downloaded.is_file():
        return {"ok": False, "error": f"Brak pobranej paczki: {downloaded}"}

    try:
        if sys.platform == "darwin":
            return apply_update_macos(downloaded, target)
        if sys.platform == "win32":
            return apply_update_windows(downloaded, target)
        return {"ok": False, "error": f"Nieobsługiwany system: {sys.platform}"}
    except Exception as e:
        logger.exception("apply_update nie powiodło się")
        return {"ok": False, "error": str(e)}
