# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all

datas = [('static', 'static')]
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
tmp_ret = collect_all('pip')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('PyQt6')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('weasyprint')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('streamlink')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('whisper')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]


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
