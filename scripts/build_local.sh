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
echo "=== 2b. Statyczne binarki multimediów do bin/ (zero-dependency) ==="
mkdir -p bin
ARCH=$(uname -m)
case "$ARCH" in
    arm64)  FF_ARCH="darwin-arm64" ;;
    x86_64) FF_ARCH="darwin-x64" ;;
    *)      echo "Unsupported arch: $ARCH"; exit 1 ;;
esac

# ffmpeg: kopiujemy statyczną binarkę z imageio-ffmpeg (ten sam build 7.1,
# na którym przeszły wszystkie testy Fast Cuttera — pełny drawtext,
# h264_videotoolbox, libvpx-vp9, libopus).
if [ ! -f bin/ffmpeg ]; then
    IMG_FF=$(python3 -c "import imageio_ffmpeg; print(imageio_ffmpeg.get_ffmpeg_exe())" 2>/dev/null || echo "")
    if [ -n "$IMG_FF" ] && [ -f "$IMG_FF" ]; then
        echo "Kopiuję ffmpeg z imageio-ffmpeg → bin/ffmpeg"
        cp "$IMG_FF" bin/ffmpeg
    else
        echo "Pobieram statyczny ffmpeg (${FF_ARCH})…"
        curl -L --silent --show-error -o /tmp/ffmpeg.gz \
            "https://github.com/eugeneware/ffmpeg-static/releases/download/b6.0/ffmpeg-${FF_ARCH}.gz"
        gunzip -f /tmp/ffmpeg.gz && mv /tmp/ffmpeg bin/ffmpeg
    fi
    chmod +x bin/ffmpeg
fi

# ffprobe: statyczny build (imageio NIE dostarcza ffprobe).
if [ ! -f bin/ffprobe ]; then
    echo "Pobieram statyczny ffprobe (${FF_ARCH})…"
    curl -L --silent --show-error -o /tmp/ffprobe.gz \
        "https://github.com/eugeneware/ffmpeg-static/releases/download/b6.0/ffprobe-${FF_ARCH}.gz"
    gunzip -f /tmp/ffprobe.gz && mv /tmp/ffprobe bin/ffprobe
    chmod +x bin/ffprobe
fi

# yt-dlp: standalone executable (zapas — silnik używa modułu in-process).
if [ ! -f bin/yt-dlp ]; then
    echo "Pobieram standalone yt-dlp (macos)…"
    curl -L --silent --show-error -o bin/yt-dlp \
        "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp_macos"
    chmod +x bin/yt-dlp
fi

echo "bin/ zawiera:"
for b in ffmpeg ffprobe yt-dlp deno; do
    [ -x "bin/$b" ] || { echo "  $b BRAK!"; exit 1; }
    # Przechwytujemy wyjście do zmiennej (bez `| head` — pod `set -o pipefail`
    # zamknięcie potoku przez head daje SIGPIPE binarce i fałszywy błąd).
    _ver=$("bin/$b" -version 2>/dev/null || "bin/$b" --version 2>/dev/null || echo "?")
    printf "  %-8s %s\n" "$b" "$(printf '%s' "$_ver" | head -n1)"
done
# Sanity: bundlowany ffmpeg MUSI mieć drawtext (napisy źródła Fast Cutter).
# `grep -q` short-circuituje i wywołałby SIGPIPE na ffmpeg pod pipefail —
# dlatego przechwytujemy pełne wyjście do zmiennej i dopasowujemy wzorcem.
_FILTERS=$(bin/ffmpeg -hide_banner -filters 2>/dev/null || true)
case "$_FILTERS" in
    *" drawtext "*) echo "  ✓ bin/ffmpeg ma drawtext" ;;
    *) echo "FATAL: bin/ffmpeg bez drawtext"; exit 1 ;;
esac

echo ""
echo "=== 3. Clean dist/ + build/ ==="
rm -rf dist/ build/
git restore build/  # przywróć entitlements.plist + installer.iss (tracked)

echo ""
echo "=== 4. PyInstaller — ~3-5 min ==="
pyinstaller wp_downloader.spec --noconfirm

echo ""
echo "=== 4b. Exec-bit na bundlowanych binarkach bin/ ==="
# PyInstaller kopiuje `datas` verbatim, ale bit wykonywalności bywa gubiony —
# bez +x subprocess ffmpeg/ffprobe/yt-dlp dostałby Permission denied.
BUNDLE_BIN="dist/WP_Downloader.app/Contents/Resources/bin"
if [ -d "$BUNDLE_BIN" ]; then
    chmod +x "$BUNDLE_BIN"/ffmpeg "$BUNDLE_BIN"/ffprobe "$BUNDLE_BIN"/yt-dlp "$BUNDLE_BIN"/deno 2>/dev/null || true
    echo "  bin/ w bundlu:"; ls -la "$BUNDLE_BIN" | awk 'NR>1{print "   "$1" "$NF}'
else
    echo "  UWAGA: nie znaleziono $BUNDLE_BIN"; exit 1
fi

echo ""
echo "=== 5. Ad-hoc codesign + verify ==="
# Każda binarka w bin/ codesign osobno (ad-hoc) — inaczej Gatekeeper zabija
# niepodpisany subprocess na macOS 15+.
for _b in ffmpeg ffprobe yt-dlp deno; do
    codesign --force --sign - "$BUNDLE_BIN/$_b" 2>/dev/null || true
done
codesign --force --deep --sign - \
    --entitlements build/entitlements.plist \
    --options runtime \
    dist/WP_Downloader.app
codesign --verify --verbose dist/WP_Downloader.app

echo ""
echo "=== 6. Strip quarantine + DMG (drag&drop pattern) ==="
xattr -dr com.apple.quarantine dist/WP_Downloader.app || true
DMG_STAGING="dist/dmg-staging"
rm -rf "$DMG_STAGING"
mkdir -p "$DMG_STAGING"
# `-c` = clonefile (APFS copy-on-write) — staging copy współdzieli bloki z
# oryginałem zamiast fizycznie duplikować ~1.5+ GB (.app rośnie z modelami
# Whisper zaszytymi w paczce). Bez tego build potrafił wyczerpać dysk przy
# małej ilości wolnego miejsca. Fallback na zwykły `cp -R`, gdyby wolumin
# nie był APFS (np. sieciowy dysk) i clonefile nie zadziałał.
cp -Rc dist/WP_Downloader.app "$DMG_STAGING/" 2>/dev/null || cp -R dist/WP_Downloader.app "$DMG_STAGING/"
ln -s /Applications "$DMG_STAGING/Applications"
chmod +x "$DMG_STAGING/WP_Downloader.app/Contents/MacOS/WP_Downloader"
hdiutil create \
    -volname "WP Downloader" \
    -srcfolder "$DMG_STAGING" \
    -ov -format UDZO \
    -fs HFS+ \
    dist/WP_Downloader_macOS.dmg
xattr -dr com.apple.quarantine dist/WP_Downloader_macOS.dmg || true
rm -rf "$DMG_STAGING"

echo ""
echo "=== 7. Portable ZIP (bez DMG/instalacji do /Applications) ==="
# `ditto` (nie `zip -r`) zachowuje resource forks / extended attributes
# macOS wewnątrz .app bundla — zwykły `zip` je gubi.
ditto -c -k --sequesterRsrc --keepParent \
    dist/WP_Downloader.app dist/wp-downloader_PORTABLE.zip

echo ""
echo "✓ DONE"
echo "  dist/WP_Downloader.app                    → $(du -sh dist/WP_Downloader.app | cut -f1)"
echo "  dist/WP_Downloader_macOS.dmg               → $(du -sh dist/WP_Downloader_macOS.dmg | cut -f1)"
echo "  dist/wp-downloader_PORTABLE.zip            → $(du -sh dist/wp-downloader_PORTABLE.zip | cut -f1)"
echo ""
echo "Aby uruchomić:"
echo "  open dist/WP_Downloader.app"
