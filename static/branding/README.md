# Branding assets Fast Cutter

- **default_logo.png** — nakładane w rogu materiału (Fast Cutter → Logo → Domyślne WP)
- **default_outro.mp4** — 3 s tyłówka doklejana na końcu (Fast Cutter → Outro → Standardowa)

Aby podmienić na produkcyjne wersje: zastąp pliki bezpośrednio, PyInstaller pakuje cały
katalog `static/` do bundle (spec: `datas = [('static', 'static')]`).

Wymagania:
- `default_logo.png`: PNG z alfa, min. 200×200 px, zalecane 300×300 (skalowanie na render)
- `default_outro.mp4`: H.264 + AAC, 1920×1080, 3–5 s, faststart

Skrypty (bash) do podglądu ffmpeg parametrów są w `../scripts/build_local.sh`.
