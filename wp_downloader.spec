# -*- mode: python ; coding: utf-8 -*-
import os as _os
from PyInstaller.utils.hooks import collect_all

# Cały frontend (index.html, JS, logo) + branding Fast Cuttera.
# ('static', 'static') kopiuje pełne drzewo, ale branding ma jawny wpis
# i twardą asercję — build ma się wywalić od razu, jeśli domyślne
# logo/outro zniknęły z repo, zamiast produkować apkę z niedziałającym
# brandingiem w Fast Cutterze.
_branding_dir = _os.path.join(_os.path.dirname(SPEC), "static", "branding")
for _asset in ("default_logo.png", "default_outro.mp4", "sub_button.mov",
               _os.path.join("fonts", "Gilroy-SemiBold.ttf")):
    assert _os.path.isfile(_os.path.join(_branding_dir, _asset)), \
        f"Brak zasobu brandingu: static/branding/{_asset}"

datas = [
    ('static', 'static'),
    (_os.path.join('static', 'branding'), _os.path.join('static', 'branding')),
]
# ── Zero-dependency: wewnętrzny katalog bin/ ────────────────────────────
# Statyczne binarki CLI (ffmpeg, ffprobe, yt-dlp, deno) pobierane/kopiowane
# przez scripts/build_local.sh. Pakowane jako `datas` (NIE `binaries`) —
# to samodzielne, statycznie linkowane executable uruchamiane jako subprocess,
# a nie biblioteki ładowane do procesu Pythona. Sekcja `binaries` przepuściłaby
# je przez dylib-dependency-rewriting PyInstallera, co mogłoby uszkodzić
# statyczny build. binaries.py rozwiązuje ścieżki do nich przez _MEIPASS /
# Contents/Resources/bin / exe_dir/bin — zero zależności od systemowego PATH.
_bin_dir = _os.path.join(_os.path.dirname(SPEC), "bin")
assert _os.path.isdir(_bin_dir), "Brak katalogu bin/ — uruchom scripts/build_local.sh (sekcja 2/2b)"
_required_bins = ["ffmpeg", "ffprobe", "yt-dlp", "deno"]
for _b in _required_bins:
    _bp = _os.path.join(_bin_dir, _b)
    assert _os.path.isfile(_bp), (
        f"Brak bundlowanej binarki bin/{_b} — zero-dependency wymaga jej "
        f"w paczce (scripts/build_local.sh sekcja 2b)")
datas += [(_bin_dir, "bin")]
binaries = []
hiddenimports = ['uvicorn.logging', 'uvicorn.loops', 'uvicorn.loops.asyncio', 'uvicorn.protocols', 'uvicorn.protocols.http', 'uvicorn.protocols.http.auto', 'uvicorn.protocols.websockets', 'uvicorn.protocols.websockets.auto', 'uvicorn.lifespan', 'uvicorn.lifespan.on', 'PyQt6.QtWebEngineCore']
tmp_ret = collect_all('fastapi')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('uvicorn')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('websockets')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('yt_dlp')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
# collect_all('pip') zostaje — perform_system_update w server.py używa pip
# subprocess do upgradów yt-dlp / streamlink przez overlay site-packages.
tmp_ret = collect_all('pip')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('PyQt6')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('streamlink')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
# ── Whisper + torch — transkrypcja jest częścią buildu produkcyjnego ──
# Twarda asercja zamiast cichego pominięcia: status "whisper: brak" w stopce
# wynikał z buildu bez pakietu. Build ma się wywalić, jeśli venv nie ma
# openai-whisper (pip install -r requirements.txt).
import importlib.util as _ilu
assert _ilu.find_spec("whisper"), (
    "openai-whisper nie jest zainstalowany w środowisku builda — "
    "uruchom: .venv/bin/pip install openai-whisper"
)
from PyInstaller.utils.hooks import collect_dynamic_libs

# collect_all('whisper') zabiera też whisper/assets (mel_filters.npz oraz
# pliki tokenizera gpt2.tiktoken / multilingual.tiktoken) jako datas.
tmp_ret = collect_all('whisper')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
# tiktoken rejestruje enkodery przez namespace-package tiktoken_ext skanowany
# w runtime — PyInstaller nie widzi tego importu statycznie. Bez
# tiktoken_ext.openai_public whisper pada na get_encoding() dopiero przy
# pierwszej transkrypcji.
tmp_ret = collect_all('tiktoken')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
hiddenimports += [
    'whisper', 'torch', 'tqdm', 'regex', 'numba', 'llvmlite',
    'tiktoken', 'tiktoken_ext', 'tiktoken_ext.openai_public',
]
# Statyczna binarka ffmpeg z imageio-ffmpeg — fallback renderu tekstu
# źródła (drawtext/sendcmd), gdy systemowy ffmpeg jest okrojony
# (np. brew bez libfreetype). cutter._bundled_ffmpeg() znajduje ją
# w spakowanym pakiecie przez imageio_ffmpeg.get_ffmpeg_exe().
assert _ilu.find_spec("imageio_ffmpeg"), (
    "imageio-ffmpeg nie jest zainstalowany w środowisku builda — "
    "uruchom: .venv/bin/pip install imageio-ffmpeg"
)
tmp_ret = collect_all('imageio_ffmpeg')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
hiddenimports.append('imageio_ffmpeg')

# Opcjonalne backendy AI (faster-whisper/tokenizers) — pakowane tylko gdy
# faktycznie zainstalowane; hiddenimport nieistniejącego pakietu generuje
# jedynie mylące warningi w logu builda.
for _opt_ai in ("faster_whisper", "tokenizers"):
    if _ilu.find_spec(_opt_ai):
        tmp_ret = collect_all(_opt_ai)
        datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
        hiddenimports.append(_opt_ai)
# Dylib-y / frameworki torcha (libtorch, libc10, libomp…) jawnie do binaries —
# oficjalny hook contrib je łapie, ale jawna mapa jest odporna na regresje.
binaries += collect_dynamic_libs('torch')


a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='WP_Downloader',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['static/wp_logo.png'],
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='WP_Downloader',
)
app = BUNDLE(
    coll,
    name='WP_Downloader.app',
    icon='static/wp_logo.png',
    bundle_identifier='com.geroo94.wpdownloader',
    # macOS 26+ na Apple Silicon waliduje PAC sygnatury metadanych bundla;
    # bundle bez tych kluczy crashuje w __CFCheckCFInfoPACSignature na
    # ścieżce QLibraryInfoPrivate::paths → CFBundleCopyBundleURL podczas
    # dlopen QtCore.abi3.so. CFBundleName / version + LSMinimumSystemVersion
    # + NSCamera/Microphone usage description są tu obowiązkowe.
    info_plist={
        'CFBundleName': 'WP Downloader',
        'CFBundleDisplayName': 'WP Downloader',
        'CFBundleShortVersionString': '1.0',
        'CFBundleVersion': '1.0',
        'LSMinimumSystemVersion': '11.0',
        'NSHighResolutionCapable': True,
        # NSCameraUsageDescription / NSMicrophoneUsageDescription są wymagane
        # dla bundli z QtWebEngine — wkomponowany Chromium może próbować
        # requestować media-permissions i bez tych kluczy macOS killuje
        # proces helper natychmiast po starcie.
        'NSCameraUsageDescription': 'WP Downloader nie korzysta z kamery.',
        'NSMicrophoneUsageDescription': 'WP Downloader nie korzysta z mikrofonu.',
        'LSUIElement': False,
    },
)
