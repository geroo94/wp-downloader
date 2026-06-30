@echo off
REM Lokalny build .exe + Inno Setup installer dla Windows.
REM Wymaga: Python 3.11+ w PATH, pip, ffmpeg, git.
REM Opcjonalnie: Inno Setup 6 dla installera (https://jrsoftware.org/isdl.php).
REM Użycie: scripts\build_local.bat
setlocal enabledelayedexpansion
cd /d "%~dp0\.."

echo === 1. Srodowisko Python ===
python -c "import PyInstaller, PIL" 2>nul
if errorlevel 1 (
    pip install --quiet pyinstaller pillow
    if errorlevel 1 ( echo pip install FAILED & exit /b 1 )
)

echo.
echo === 2. Deno binary (jesli brak) ===
if not exist bin\deno.exe (
    if not exist bin mkdir bin
    set "DENO_VER=2.7.12"
    echo Pobieranie deno !DENO_VER! (Windows x64)...
    powershell -NoProfile -Command "Invoke-WebRequest -Uri 'https://github.com/denoland/deno/releases/download/v!DENO_VER!/deno-x86_64-pc-windows-msvc.zip' -OutFile '%TEMP%\deno.zip'"
    if errorlevel 1 ( echo Pobieranie deno FAILED & exit /b 1 )
    powershell -NoProfile -Command "Expand-Archive -Path '%TEMP%\deno.zip' -DestinationPath bin -Force"
    del /q "%TEMP%\deno.zip"
)
bin\deno.exe --version

echo.
echo === 3. Clean dist + build ===
if exist dist rmdir /s /q dist
if exist build rmdir /s /q build
git restore build/ 2>nul

echo.
echo === 4. Multi-res ICO ===
python -c "from PIL import Image; img = Image.open('static/wp_logo.png').convert('RGBA'); img.save('static/wp_logo.ico', sizes=[(16,16),(32,32),(48,48),(64,64),(128,128),(256,256)])"

echo.
echo === 5. PyInstaller (~3-5 min) ===
pyinstaller wp_downloader.spec --noconfirm
if errorlevel 1 ( echo PyInstaller FAILED & exit /b 1 )

echo.
echo === 6. Inno Setup installer ===
set "ISCC=C:\Program Files (x86)\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" (
    echo Inno Setup nie zainstalowany - skip. Pobierz z https://jrsoftware.org/isdl.php
) else (
    "%ISCC%" build\installer.iss
    if errorlevel 1 ( echo Inno Setup FAILED & exit /b 1 )
)

echo.
echo === 7. ZIP (portable) ===
powershell -NoProfile -Command "Compress-Archive -Path dist\WP_Downloader -DestinationPath dist\WP_Downloader_Windows.zip -Force"

echo.
echo === DONE ===
echo   dist\WP_Downloader\WP_Downloader.exe   (folder portable)
echo   dist\WP_Downloader_Windows.zip          (portable ZIP)
if exist Output\WP_Downloader_Setup.exe echo   Output\WP_Downloader_Setup.exe          (installer)
echo.
echo Aby uruchomic:
echo   dist\WP_Downloader\WP_Downloader.exe
