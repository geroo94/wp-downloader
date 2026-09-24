"""Testy podmiany aplikacji przez updater — na ATRAPIE bundla .app.

Dlaczego osobny plik od `test_updater.py`: tamten sprawdza czystą logikę
(wersje, wybór paczki), a tutaj chodzi o jedyne pytanie, które naprawdę boli
użytkownika — czy po nieudanej aktualizacji zostaje BEZ DZIAŁAJĄCEJ APLIKACJI.

Scenariusze wzięły się z realnych wpadek wykrytych w trakcie pracy:
  * `ZipFile.extractall()` gubi bity uprawnień → zaktualizowana aplikacja
    nie miała +x i w ogóle nie startowała,
  * wpisy katalogów bez ukośnika na końcu lądowały jako puste PLIKI,
  * podmiana bez rollbacku potrafiła zostawić puste miejsce po aplikacji,
  * zignorowany błąd `codesign` = bundle ubijany przy starcie (Apple Silicon).

Uruchomienie: python3 tests/test_apply_update.py   (tylko macOS)
"""

import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import updater  # noqa: E402
from updater import apply_update_macos  # noqa: E402

if sys.platform != "darwin":
    print("SKIP: testy podmiany .app mają sens tylko na macOS")
    sys.exit(0)

PLIST = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleExecutable</key><string>WP_Downloader</string>
  <key>CFBundleIdentifier</key><string>com.test.wp.%s</string>
  <key>CFBundleName</key><string>WP Downloader</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleVersion</key><string>1.0</string>
</dict></plist>
"""

failures: list[str] = []


def check(cond: bool, label: str) -> None:
    print(("  OK   " if cond else "  FAIL ") + label)
    if not cond:
        failures.append(label)


def make_app(path: Path, marker: str) -> None:
    (path / "Contents" / "MacOS").mkdir(parents=True)
    (path / "Contents" / "Info.plist").write_text(PLIST % marker)
    (path / "Contents" / "marker.txt").write_text(marker)
    exe = path / "Contents" / "MacOS" / "WP_Downloader"
    exe.write_text("#!/bin/sh\necho %s\n" % marker)
    exe.chmod(0o755)


def marker_of(app: Path) -> str:
    return (app / "Contents" / "marker.txt").read_text()


def zip_app(src: Path, dest: Path) -> None:
    """Pakuje bundle ZACHOWUJĄC tryb POSIX — tak jak robi to `ditto`.

    Katalogi celowo bez ukośnika na końcu: `ZipInfo.is_dir()` ich nie
    rozpozna, więc test pilnuje, że updater patrzy też na bit S_IFDIR.
    """
    with zipfile.ZipFile(dest, "w") as zf:
        for p in sorted(src.rglob("*")):
            info = zipfile.ZipInfo(str(Path("WP Downloader.app") / p.relative_to(src)))
            info.external_attr = (p.stat().st_mode & 0xFFFF) << 16
            zf.writestr(info, p.read_bytes() if p.is_file() else b"")


sandbox = Path(tempfile.mkdtemp(prefix="wp_update_test_"))
try:
    installed = sandbox / "WP Downloader.app"
    make_app(installed, "OLD_VERSION")
    newsrc = sandbox / "build" / "WP Downloader.app"
    make_app(newsrc, "NEW_VERSION")
    package = sandbox / "WP_Downloader_macOS_PORTABLE.zip"
    zip_app(newsrc, package)

    def leftovers() -> list[str]:
        return [p.name for p in sandbox.iterdir() if p.name.endswith((".old", ".new"))]

    print("=== podmiana z paczki portable ===")
    result = apply_update_macos(package, installed)
    exe = installed / "Contents" / "MacOS" / "WP_Downloader"
    script = result["restart_cmd"][2]
    check(result.get("ok") is True, "podmiana zgłasza sukces")
    check(marker_of(installed) == "NEW_VERSION", "na miejscu jest nowa wersja")
    check(os.access(exe, os.X_OK), "plik wykonywalny zachował bit +x")
    check((installed / "Contents" / "MacOS").is_dir(), "katalogi nie stały się plikami")
    check("kill -0 $pid" in script, "skrypt restartu czeka na wyjście z procesu")
    check("open -n" in script, "skrypt restartu uruchamia nową wersję")
    check(str(installed) + ".old" in script, "skrypt restartu sprząta starą wersję")
    check(not (sandbox / "WP Downloader.app.new").exists(), "brak resztek .new")
    shutil.rmtree(sandbox / "WP Downloader.app.old", ignore_errors=True)

    print("\n=== paczka bez .app w środku ===")
    broken = sandbox / "broken.zip"
    with zipfile.ZipFile(broken, "w") as zf:
        zf.writestr("readme.txt", "nie ma tu aplikacji")
    try:
        apply_update_macos(broken, installed)
        check(False, "zgłoszony błąd zamiast cichego sukcesu")
    except Exception:
        check(True, "zgłoszony błąd zamiast cichego sukcesu")
    check(marker_of(installed) == "NEW_VERSION", "aplikacja została nietknięta")
    check(not leftovers(), "brak resztek po nieudanej próbie")

    print("\n=== rollback: podmiana wybucha w połowie ===")
    real_rename = pathlib.Path.rename
    calls = {"n": 0}

    def flaky_rename(self, target):
        calls["n"] += 1
        if calls["n"] == 2:  # app -> .old przeszło, .new -> app wywala się
            raise OSError("symulowany błąd podmiany")
        return real_rename(self, target)

    pathlib.Path.rename = flaky_rename
    try:
        apply_update_macos(package, installed)
        check(False, "zgłoszony błąd podmiany")
    except Exception:
        check(True, "zgłoszony błąd podmiany")
    finally:
        pathlib.Path.rename = real_rename
    check(installed.exists() and marker_of(installed) == "NEW_VERSION",
          "rollback przywrócił aplikację na miejsce")
    check(not leftovers(), "rollback nie zostawił resztek")

    print("\n=== błąd podpisu przerywa PRZED podmianą ===")
    real_run = updater.subprocess.run

    def failing_codesign(cmd, *args, **kwargs):
        if cmd and cmd[0] == "codesign":
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="symulowany błąd")
        return real_run(cmd, *args, **kwargs)

    updater.subprocess.run = failing_codesign
    try:
        apply_update_macos(package, installed)
        check(False, "zgłoszony błąd podpisu")
    except Exception:
        check(True, "zgłoszony błąd podpisu")
    finally:
        updater.subprocess.run = real_run
    check(installed.exists() and marker_of(installed) == "NEW_VERSION",
          "aplikacja nietknięta mimo błędu podpisu")
    check(not leftovers(), "brak resztek po błędzie podpisu")
finally:
    shutil.rmtree(sandbox, ignore_errors=True)
    shutil.rmtree(updater.staging_dir(), ignore_errors=True)

print("\n" + ("ALL PASS" if not failures else "FAIL: " + "; ".join(failures)))
sys.exit(1 if failures else 0)
