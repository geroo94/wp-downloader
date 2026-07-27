#!/usr/bin/env bash
# Lokalny build WP Environment Checker — osobna, lekka apka (Tkinter,
# zero zależności poza stdlibem) do folderu 'dist/'. Osobny od
# build_local.sh: to narzędzie ma zostać MAŁE (kilka-kilkanaście MB), więc
# nie przechodzi przez wp_downloader.spec (PyQt6/torch/whisper).
#
# Użycie:
#   chmod +x scripts/build_env_checker.sh
#   ./scripts/build_env_checker.sh
set -euo pipefail
cd "$(dirname "$0")/.."

echo "=== WP Environment Checker — build ==="
python3 -c "import PyInstaller" 2>/dev/null || pip install --quiet pyinstaller

ICON_ARGS=()
if [ -f "static/wp_logo.png" ]; then
    ICON_ARGS=(--icon "$(pwd)/static/wp_logo.png")
fi

# UWAGA: bez --onefile na macOS. PyInstaller >=6.20 ostrzega (i od v7.0 to
# będzie twardy błąd), że --onefile + --windowed nie ma sensu dla .app
# bundli (bundle z natury nie może być pojedynczym plikiem) i koliduje
# z Gatekeeperem. --windowed samo w sobie już daje jeden plik .app w Finderze
# z punktu widzenia usera — --onefile niczego tu nie dokłada.
pyinstaller \
    --windowed \
    --name WP_Environment_Checker \
    --distpath dist \
    --workpath build/env_checker \
    --specpath build \
    --noconfirm \
    "${ICON_ARGS[@]}" \
    tools/env_checker.py

echo ""
if [ -d "dist/WP_Environment_Checker.app" ]; then
    # macOS: ad-hoc codesign, jak reszta bundlowanych binarek — bez tego
    # Gatekeeper na macOS 15+ blokuje uruchomienie niepodpisanej apki.
    codesign --force --deep --sign - dist/WP_Environment_Checker.app 2>/dev/null || true
    xattr -dr com.apple.quarantine dist/WP_Environment_Checker.app 2>/dev/null || true
    echo "✓ dist/WP_Environment_Checker.app → $(du -sh dist/WP_Environment_Checker.app | cut -f1)"
elif [ -f "dist/WP_Environment_Checker" ]; then
    echo "✓ dist/WP_Environment_Checker → $(du -sh dist/WP_Environment_Checker | cut -f1)"
elif [ -f "dist/WP_Environment_Checker.exe" ]; then
    echo "✓ dist/WP_Environment_Checker.exe → $(du -sh dist/WP_Environment_Checker.exe | cut -f1)"
fi
