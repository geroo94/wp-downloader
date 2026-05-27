import PyInstaller.__main__
import os

def run_build():
    # Determine path separator for --add-data (';' on Windows, ':' on others)
    sep = os.pathsep
    
    # Base directory
    base_dir = os.path.dirname(os.path.abspath(__file__))
    
    # PyInstaller options
    opts = [
        'main.py',                  # Entry point
        '--name=WP_Downloader',     # Name of the executable
        '--noconsole',              # GUI app, no terminal window
        '--onedir',                 # Folder bundle — no extraction on each launch
        f'--add-data=static{sep}static', # Dołącz folder z interfejsem
        '--hidden-import=uvicorn.logging',
        '--hidden-import=uvicorn.loops',
        '--hidden-import=uvicorn.loops.asyncio',
        '--hidden-import=uvicorn.protocols',
        '--hidden-import=uvicorn.protocols.http',
        '--hidden-import=uvicorn.protocols.http.auto',
        '--hidden-import=uvicorn.protocols.websockets',
        '--hidden-import=uvicorn.protocols.websockets.auto',
        '--hidden-import=uvicorn.lifespan',
        '--hidden-import=uvicorn.lifespan.on',
        '--hidden-import=PyQt6.QtWebEngineCore',
        '--collect-all=fastapi',
        '--collect-all=uvicorn',
        '--collect-all=websockets',
        '--collect-all=yt_dlp',     # Silnik pobierania
        '--collect-all=pip',        # Mechanizm aktualizacji
        '--collect-all=PyQt6',      # Interfejs graficzny
        '--collect-all=weasyprint',
        '--collect-all=streamlink', # Pobieranie live streamów (Facebook)
        '--collect-all=whisper',    # Transkrypcja audio (Whisper)
        '--clean',
        '--noconfirm',  # nadpisuje istniejący dist/ bez pytania
    ]

    # Add icon if it exists
    icon_path = os.path.join(base_dir, 'static', 'wp_logo.png')
    if os.path.exists(icon_path):
        opts.append(f'--icon={icon_path}')

    PyInstaller.__main__.run(opts)

if __name__ == '__main__':
    run_build()