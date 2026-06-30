#!/usr/bin/env bash
# Lokalny build .app + DMG dla macOS (one-liner do testów dev).
# Wymaga: python3 3.11+, pip, ffmpeg w PATH, git.
# Użycie:
#   chmod +x scripts/build_local.sh
#   ./scripts/build_local.sh
set -euo pipefail
cd "$(dirname "$0")/.."

echo "=== 1. Środowisko Python ==="
python3 -c "import PyInstaller, PIL" 2>/dev/null \
    || pip install --quiet pyinstaller pillow

echo ""
echo "=== 2. Deno binary (jeśli brak) ==="
if [ ! -f bin/deno ]; then
    mkdir -p bin
    ARCH=$(uname -m)
    case "$ARCH" in
        arm64)  DENO_ARCH="aarch64-apple-darwin" ;;
        x86_64) DENO_ARCH="x86_64-apple-darwin" ;;
        *)      echo "Unsupported arch: $ARCH"; exit 1 ;;
    esac
    DENO_VER="2.7.12"
    echo "Pobieranie deno ${DENO_VER} (${DENO_ARCH})…"
    curl -L --silent --show-error -o /tmp/deno.zip \
        "https://github.com/denoland/deno/releases/download/v${DENO_VER}/deno-${DENO_ARCH}.zip"
    unzip -o -q /tmp/deno.zip -d bin/
    chmod +x bin/deno
    rm /tmp/deno.zip
fi
bin/deno --version

echo ""
echo "=== 3. Clean dist/ + build/ ==="
rm -rf dist/ build/
git restore build/  # przywróć entitlements.plist + installer.iss (tracked)

echo ""
echo "=== 4. PyInstaller — ~3-5 min ==="
pyinstaller wp_downloader.spec --noconfirm

echo ""
echo "=== 5. Ad-hoc codesign + verify ==="
codesign --force --deep --sign - \
    --entitlements build/entitlements.plist \
    --options runtime \
    dist/WP_Downloader.app
codesign --verify --verbose dist/WP_Downloader.app

echo ""
echo "=== 6. Strip quarantine + DMG (drag&drop pattern) ==="
xattr -dr com.apple.quarantine dist/WP_Downloader.app || true
DMG_STAGING="dist/dmg-staging"
mkdir -p "$DMG_STAGING"
cp -R dist/WP_Downloader.app "$DMG_STAGING/"
ln -s /Applications "$DMG_STAGING/Applications"
chmod +x "$DMG_STAGING/WP_Downloader.app/Contents/MacOS/WP_Downloader"
hdiutil create \
    -volname "WP Downloader" \
    -srcfolder "$DMG_STAGING" \
    -ov -format UDZO \
    -fs HFS+ \
    dist/WP_Downloader_macOS.dmg
xattr -dr com.apple.quarantine dist/WP_Downloader_macOS.dmg || true

echo ""
echo "✓ DONE"
echo "  dist/WP_Downloader.app           → $(du -sh dist/WP_Downloader.app | cut -f1)"
echo "  dist/WP_Downloader_macOS.dmg     → $(du -sh dist/WP_Downloader_macOS.dmg | cut -f1)"
echo ""
echo "Aby uruchomić:"
echo "  open dist/WP_Downloader.app"
