"""
server.py — FastAPI aplikacja webowa (serwer).

Wyobraź sobie go jako "recepcję hotelową":
- przyjmuje żądania od przeglądarki (gości)
- obsługuje różne "okienka" (endpoints):
  GET  /          → serwuje stronę HTML
  GET  /api/tasks → zwraca listę zadań
  POST /api/download → dodaje nowe zadanie
  GET  /api/formats  → zwraca dostępne formaty dla URL
  WS   /ws        → stałe połączenie WebSocket do live updates
"""

import asyncio
import json
import logging
import re
import subprocess
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel

import sys
import os

from download_manager import DownloadManager
from environment_manager import collect_system_info
from obs_controller import OBSController
from streamlink_proxy import StreamlinkProxy
from queue_manager import QueueManager
from yt_dlp_worker import YtDlpWorker, _detect_js_runtime

logger = logging.getLogger(__name__)

# Pydantic model — definiuje jak wygląda JSON który przyjmujemy od frontendu
# Pydantic automatycznie waliduje typy — jeśli frontend wyśle liczbę zamiast stringa,
# dostanie ładny błąd zamiast crashu
class DownloadRequest(BaseModel):
    url: str
    format_id: str = "mp4-720"
    quality: str = "Najlepsza jakość"
    output_path: str = ""
    live_record: bool = False
    cookies_browser: str = ""  # "chrome" / "firefox" / "safari" / "edge" / ""
    wait_for_video: bool = False  # --wait-for-video for scheduled live streams
    # Nowe pola inspirowane OBS
    add_watermark: bool = False
    audio_only_separate: bool = False  # Czy pobrać audio jako osobny plik (multi-track)
    low_latency_mode: bool = False     # Tryb niskiego opóźnienia dla live



def get_overlay_dir() -> str:
    """User-writable site-packages overlay so pip-updated yt-dlp/streamlink win over the bundled copy."""
    import platform
    home = os.path.expanduser("~")
    sysname = platform.system()
    if sysname == "Darwin":
        base = os.path.join(home, "Library", "Application Support", "WP_Downloader")
    elif sysname == "Windows":
        base = os.path.join(os.environ.get("APPDATA", home), "WP_Downloader")
    else:
        base = os.path.join(home, ".local", "share", "wp_downloader")
    overlay = os.path.join(base, "site-packages")
    try:
        os.makedirs(overlay, exist_ok=True)
    except OSError:
        pass
    return overlay


async def _pypi_latest(pkg: str) -> str:
    """Best-effort fetch of the latest version of `pkg` from PyPI."""
    import urllib.request, json as _json
    def _fetch() -> str:
        try:
            with urllib.request.urlopen(f"https://pypi.org/pypi/{pkg}/json", timeout=8) as r:
                return _json.load(r).get("info", {}).get("version", "") or ""
        except Exception:
            return ""
    return await asyncio.get_event_loop().run_in_executor(None, _fetch)


async def _installed_version(pkg: str) -> str:
    """Currently-imported package version (overlay first, bundled fallback) by name."""
    def _v() -> str:
        try:
            from importlib.metadata import version
            return version(pkg)
        except Exception:
            return ""
    return await asyncio.get_event_loop().run_in_executor(None, _v)


def _vtuple(v: str) -> tuple:
    parts = []
    for chunk in (v or "").split("."):
        m = re.match(r"(\d+)", chunk)
        parts.append(int(m.group(1)) if m else 0)
    return tuple(parts)


async def perform_system_update(manager: DownloadManager, delay: int = 5,
                                packages: tuple[str, ...] = ("yt-dlp", "streamlink"),
                                only_if_outdated: bool = False):
    """Update packages by pip-installing into the user overlay (works even from a frozen EXE).
       openai-whisper drags ~2 GB of torch so it's intentionally excluded from the default set."""
    if delay > 0:
        await asyncio.sleep(delay)

    is_frozen = hasattr(sys, "_MEIPASS")
    overlay = get_overlay_dir()
    base_cmd = [sys.executable, "-m", "pip", "install", "-U", "--no-cache-dir",
                "--target", overlay, "--upgrade-strategy=eager"]

    if only_if_outdated:
        outdated: list[str] = []
        for pkg in packages:
            cur, latest = await _installed_version(pkg), await _pypi_latest(pkg)
            if cur and latest and _vtuple(latest) > _vtuple(cur):
                outdated.append(pkg)
                logger.info(f"Auto-update: {pkg} {cur} → {latest}")
        if not outdated:
            await manager.broadcast({"type": "update_log", "message": "Auto-check: wszystkie komponenty są aktualne."})
            return
        packages = tuple(outdated)
        await manager.broadcast({"type": "update_log",
                                 "message": f"Auto-update: pobieram nowsze wersje: {', '.join(packages)}…"})

    await manager.broadcast({"type": "update_log",
                             "message": f"Pip target: {overlay}" if is_frozen else "Aktualizacja przez pip…"})

    for pkg in packages:
        try:
            await manager.broadcast({"type": "update_log", "message": f"→ {pkg}"})
            cmd = base_cmd + [pkg]
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                creationflags=0x08000000 if os.name == 'nt' else 0
            )
            try:
                while True:
                    line = await asyncio.wait_for(process.stdout.readline(), timeout=30.0)
                    if not line:
                        break
                    text = line.decode(errors="replace").strip()
                    if text:
                        await manager.broadcast({"type": "update_log", "message": text})
            except asyncio.TimeoutError:
                pass
            await process.wait()
        except Exception as e:
            logger.error(f"Błąd aktualizacji komponentu {pkg}: {e}")
            await manager.broadcast({"type": "update_log", "message": f"Błąd {pkg}: {e}"})

    await manager.broadcast({"type": "system_info_refresh"})
    await manager.broadcast({"type": "update_log",
                             "message": "Gotowe — uruchom ponownie aplikację, aby załadować nowe wersje."})


def _fmt_ts(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


async def _run_transcription(manager: DownloadManager, task_id: str, video_path: str,
                              language: str = "") -> None:
    """Background task: transcribe a video file with Whisper and save as .txt with timecodes.
       language: ISO 639-1 code ("pl", "en", "uk", ...) or "" for auto-detect."""
    await manager.update_task(task_id, transcription="in_progress")
    txt_path = os.path.splitext(video_path)[0] + ".txt"
    loop = asyncio.get_event_loop()
    try:
        import whisper

        def _transcribe() -> str:
            model = whisper.load_model("base")
            kwargs = {"task": "transcribe"}
            if language:
                kwargs["language"] = language
            result = model.transcribe(video_path, **kwargs)
            lines = []
            for seg in result.get("segments", []):
                text = seg["text"].strip()
                if text:
                    lines.append(f"[{_fmt_ts(seg['start'])}] {text}")
            return "\n".join(lines) if lines else result.get("text", "").strip()

        text = await asyncio.wait_for(loop.run_in_executor(None, _transcribe), timeout=3600)
        with open(txt_path, "w", encoding="utf-8") as f:
            f.write(text)
        await manager.update_task(task_id, transcription=txt_path)
        logger.info(f"Transkrypcja gotowa: {txt_path}")
    except ImportError:
        await manager.update_task(task_id, transcription="error")
        logger.error("openai-whisper nie jest zainstalowany")
    except Exception as e:
        await manager.update_task(task_id, transcription="error")
        logger.error(f"Błąd transkrypcji: {e}")


def _fetch_m3u8_from_page(url: str) -> list[dict]:
    """Fetch a page and extract .m3u8 stream URLs via regex. Runs in a thread-pool."""
    import urllib.request
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,*/*;q=0.9",
        "Accept-Language": "pl,en;q=0.8",
    }
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=15) as resp:
            html = resp.read(5 * 1024 * 1024).decode("utf-8", errors="replace")
    except Exception as exc:
        logger.warning("m3u8 fetch error for %s: %s", url, exc)
        return []

    # Match absolute and relative .m3u8 URLs (quoted or unquoted)
    pattern = r'(?:https?://[^\s"\'<>]+\.m3u8[^\s"\'<>]*|/[^\s"\'<>]+\.m3u8[^\s"\'<>]*)'
    found: list[str] = list(dict.fromkeys(re.findall(pattern, html)))  # deduplicate, preserve order

    result = []
    for raw in found[:20]:  # cap at 20 results
        stream_url = raw if raw.startswith("http") else (
            re.match(r"(https?://[^/]+)", url).group(1) + raw
            if re.match(r"(https?://[^/]+)", url) else raw
        )
        label = stream_url.split("/")[-1].split("?")[0] or stream_url
        result.append({"url": stream_url, "label": label})
    return result


# Shared singletons — one per app process
_obs = OBSController()
_proxy = StreamlinkProxy()


class OBSConnectRequest(BaseModel):
    host: str = "localhost"
    port: int = 4455
    password: str = ""


class ProxyStartRequest(BaseModel):
    url: str
    quality: str = "best"
    port: int = 8888


def create_app(manager: DownloadManager) -> FastAPI:
    """
    Fabryka aplikacji FastAPI.
    Przyjmuje managera i zwraca gotową aplikację.
    
    Używamy wzorca "fabryki" zamiast globalnej zmiennej — łatwiej testować.
    """

    # lifespan = co robić przy starcie i zatrzymaniu serwera
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        """
        Uruchamia workera yt-dlp gdy serwer startuje.
        Zatrzymuje go gdy serwer się kończy.
        """
        manager.setup()

        # Worker rejestruje się z managerem i natychmiast startuje każde zadanie
        worker = YtDlpWorker(manager)

        # Auto-check + auto-update components on startup (only fetches if outdated)
        asyncio.create_task(perform_system_update(manager, delay=8, only_if_outdated=True))

        yield

        worker.stop()

    app = FastAPI(
        title="WP Downloader API",
        version="1.0",
        lifespan=lifespan
    )

    queue_mgr = QueueManager(manager)

    def _validate_output_dir(raw: str) -> str:
        """Akceptuje tylko bezwzględną ścieżkę; katalog może nie istnieć, jeśli istnieje rodzic."""
        p = (raw or "").strip()
        if not p:
            return ""
        expanded = str(Path(p).expanduser())
        if not os.path.isabs(expanded):
            return ""
        try:
            resolved = str(Path(expanded).resolve(strict=False))
        except OSError:
            return ""
        if os.path.isdir(resolved):
            return resolved
        parent = Path(resolved).parent
        if parent.is_dir():
            return resolved
        return ""

    # Serwuj pliki statyczne (CSS, JS, obrazki) z folderu /static
    # Każdy plik w /static będzie dostępny pod /static/nazwa_pliku
    static_dir = Path(__file__).parent / "static"
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    # ─── Endpoints ────────────────────────────────────────────────────────────

    @app.get("/")
    async def index():
        """Serwuje główną stronę HTML aplikacji."""
        return FileResponse(str(static_dir / "index.html"))

    @app.get("/api/tasks")
    async def get_tasks():
        """Zwraca listę wszystkich zadań."""
        return JSONResponse(manager.get_all_tasks())

    @app.get("/api/system-info")
    async def system_info():
        """Wersje środowiska do paska statusu (dokumentacja: Python, yt-dlp, PHP)."""
        info = collect_system_info(app_version="1.0")
        info["obs_version"] = _obs.obs_version if _obs.connected else ""
        info["obs_connected"] = _obs.connected
        return JSONResponse(info)

    @app.get("/api/logs")
    async def get_logs(lines: int = 500):
        """Zwraca ostatnie N linii pliku logu jako plain text."""
        import sys as _sys
        log_dir = os.path.dirname(os.path.abspath(_sys.executable if hasattr(_sys, '_MEIPASS') else __file__))
        log_path = os.path.join(log_dir, "wp_downloader_debug.log")
        try:
            with open(log_path, encoding="utf-8", errors="replace") as f:
                all_lines = f.readlines()
            tail = "".join(all_lines[-lines:])
            return PlainTextResponse(tail)
        except FileNotFoundError:
            return PlainTextResponse("(plik logu nie istnieje)")
        except Exception as e:
            return PlainTextResponse(f"(błąd odczytu: {e})")

    # ── m3u8 auto-detection ───────────────────────────────────────────────

    @app.post("/api/m3u8/detect")
    async def m3u8_detect(payload: dict):
        """Detect .m3u8 stream URLs from a page URL or a direct .m3u8 link."""
        url = (payload.get("url") or "").strip()
        if not url:
            return JSONResponse({"streams": [], "error": "Brak URL"}, status_code=400)
        if not url.startswith(("http://", "https://")):
            return JSONResponse({"streams": [], "error": "Nieprawidłowy URL"}, status_code=400)

        # Direct m3u8 link — no need to fetch anything
        if ".m3u8" in url.lower():
            return JSONResponse({"streams": [{"url": url, "label": "Bezpośredni link .m3u8"}]})

        loop = asyncio.get_event_loop()
        streams = await loop.run_in_executor(None, _fetch_m3u8_from_page, url)
        if streams:
            return JSONResponse({"streams": streams})
        return JSONResponse({"streams": [], "error": "Nie znaleziono strumieni .m3u8 na tej stronie"})

    # ── Stream proxy (streamlink HTTP server for OBS) ──────────────────────

    @app.post("/api/proxy/start")
    async def proxy_start(req: ProxyStartRequest):
        if not req.url.startswith(("http://", "https://")):
            return JSONResponse({"ok": False, "error": "Nieprawidłowy URL"}, status_code=400)
        result = await _proxy.start(req.url, req.quality, req.port)
        return JSONResponse(result)

    @app.post("/api/proxy/stop")
    async def proxy_stop():
        await _proxy.stop()
        return JSONResponse({"ok": True})

    @app.get("/api/proxy/status")
    async def proxy_status():
        return JSONResponse(_proxy.status())

    # ── OBS WebSocket ───────────────────────────────────────────────────────

    @app.get("/api/obs/status")
    async def obs_status():
        return JSONResponse(_obs.status_dict())

    @app.post("/api/obs/connect")
    async def obs_connect(req: OBSConnectRequest):
        result = await _obs.connect(req.host, req.port, req.password)
        return JSONResponse(result)

    @app.post("/api/obs/disconnect")
    async def obs_disconnect():
        await _obs.disconnect()
        return JSONResponse({"ok": True})

    @app.get("/api/obs/scenes")
    async def obs_scenes():
        if not _obs.connected:
            return JSONResponse({"error": "Nie połączono z OBS"}, status_code=400)
        scenes = await _obs.get_scenes()
        return JSONResponse({"scenes": scenes})

    @app.post("/api/obs/scene")
    async def obs_set_scene(payload: dict):
        if not _obs.connected:
            return JSONResponse({"error": "Nie połączono z OBS"}, status_code=400)
        result = await _obs.set_scene(payload.get("name", ""))
        return JSONResponse(result)

    @app.post("/api/obs/record/start")
    async def obs_record_start():
        if not _obs.connected:
            return JSONResponse({"error": "Nie połączono z OBS"}, status_code=400)
        result = await _obs.start_record()
        return JSONResponse(result)

    @app.post("/api/obs/record/stop")
    async def obs_record_stop():
        if not _obs.connected:
            return JSONResponse({"error": "Nie połączono z OBS"}, status_code=400)
        result = await _obs.stop_record()
        return JSONResponse(result)

    @app.get("/api/obs/record/status")
    async def obs_record_status():
        if not _obs.connected:
            return JSONResponse({"recording": False, "timecode": ""})
        result = await _obs.get_record_status()
        return JSONResponse(result)

    @app.post("/api/system/update")
    async def update_system():
        """Aktualizuje yt-dlp oraz inne biblioteki przez pip."""
        asyncio.create_task(perform_system_update(manager, delay=0))
        return JSONResponse({"ok": True, "message": "Rozpoczęto aktualizację wszystkich komponentów."})

    @app.post("/api/tasks/reorder")
    async def reorder_tasks(payload: dict):
        """Zmienia kolejność zadań w managerze (wymaga implementacji reorder_tasks w managerze)."""
        task_ids = payload.get("task_ids", [])
        await manager.reorder_tasks(task_ids)
        return JSONResponse({"ok": True})

    @app.post("/api/tasks/{task_id}/cancel")
    async def cancel_task(task_id: str):
        ok = await queue_mgr.cancel(task_id)
        if not ok:
            return JSONResponse({"error": "Nie można anulować tego zadania"}, status_code=400)
        return JSONResponse({"ok": True, "task_id": task_id})

    @app.post("/api/tasks/{task_id}/stop")
    async def stop_live_task(task_id: str):
        """Graceful stop dla live streamów — zatrzymuje natychmiast, scala do MP4."""
        task = manager.tasks.get(task_id)
        if not task:
            return JSONResponse({"error": "Zadanie nie istnieje"}, status_code=404)
        if task.status != "downloading" or not task.live_record:
            return JSONResponse({"error": "Tylko aktywny live stream można zatrzymać"}, status_code=400)
        manager.graceful_stop_task(task_id)
        return JSONResponse({"ok": True, "task_id": task_id})

    @app.delete("/api/tasks/{task_id}")
    async def delete_queued_task(task_id: str):
        ok = await manager.remove_task(task_id)
        if not ok:
            return JSONResponse(
                {"error": "Nie można usunąć aktywnego pobierania. Najpierw je anuluj."},
                status_code=400,
            )
        return JSONResponse({"ok": True, "task_id": task_id})

    @app.post("/api/tasks/{task_id}/transcribe")
    async def transcribe_task(task_id: str, payload: dict | None = None):
        """Creates a .txt transcription of the downloaded file using Whisper.
           Optional body: {"language": "pl"} — pass empty / omit for auto-detect."""
        task = manager.tasks.get(task_id)
        if not task:
            return JSONResponse({"error": "Zadanie nie istnieje"}, status_code=404)
        if task.status != "done" or not task.output_path:
            return JSONResponse({"error": "Tylko zakończone zadania można transkrybować"}, status_code=400)
        if task.transcription == "in_progress":
            return JSONResponse({"error": "Transkrypcja już w toku"}, status_code=400)
        language = ""
        if payload and isinstance(payload, dict):
            raw = (payload.get("language") or "").strip().lower()
            # only accept short ISO codes; anything else → auto-detect
            if re.fullmatch(r"[a-z]{2,3}", raw):
                language = raw
        asyncio.create_task(_run_transcription(manager, task_id, task.output_path, language))
        return JSONResponse({"ok": True, "language": language or "auto"})

    @app.post("/api/tasks/{task_id}/reveal")
    async def reveal_task_file(task_id: str):
        """Opens the file location in Finder (macOS) or Explorer (Windows)."""
        task = manager.tasks.get(task_id)
        if not task:
            return JSONResponse({"error": "Zadanie nie istnieje"}, status_code=404)
        if not task.output_path:
            return JSONResponse({"error": "Brak ścieżki pliku"}, status_code=404)
        path = task.output_path
        try:
            if sys.platform == "darwin":
                subprocess.Popen(["open", "-R", path])
            elif sys.platform == "win32":
                subprocess.Popen(["explorer", "/select,", path])
            else:
                subprocess.Popen(["xdg-open", os.path.dirname(path)])
            return JSONResponse({"ok": True})
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)

    @app.post("/api/download")
    async def start_download(req: DownloadRequest):
        """
        Dodaje nowe zadanie pobierania.
        Frontend wysyła JSON: {"url": "...", "format_id": "mp4-720"}
        My odpowiadamy: {"task_id": "abc123", "status": "queued"}
        """
        if not req.url.startswith(("http://", "https://")):
            return JSONResponse(
                {"error": "Nieprawidłowy URL. Musi zaczynać się od http:// lub https://"},
                status_code=400
            )

        try:
            task_id = manager.add_task(
                req.url,
                req.format_id,
                req.quality,
                output_path=req.output_path,
                live_record=req.live_record,
                cookies_browser=req.cookies_browser,
                wait_for_video=req.wait_for_video,
            )
        except Exception as exc:
            logger.exception("Błąd tworzenia zadania pobierania: %s", exc)
            return JSONResponse({"error": str(exc)}, status_code=500)

        logger.info("Dodano zadanie %s: %s [%s]", task_id, req.url, req.format_id)
        return JSONResponse({
            "task_id": task_id,
            "status": "queued",
            "message": "Zadanie dodane do kolejki"
        })

    @app.get("/api/formats")
    async def get_formats(url: str, cookies_browser: str = ""):
        """Pobiera dostępne formaty dla podanego URL używając yt-dlp API."""
        from yt_dlp import YoutubeDL
        import yt_dlp.utils as _ydl_utils

        try:
            extra: dict = {}
            _cfb = cookies_browser.strip() or (os.environ.get("WP_DOWNLOADER_COOKIES_BROWSER") or "").strip()
            if _cfb:
                extra["cookiesfrombrowser"] = (_cfb,)

            # Capture yt-dlp log messages for richer error context
            _ydl_messages: list[str] = []

            def _ydl_logger_cb(msg: str) -> None:
                _ydl_messages.append(msg)
                logger.debug("[yt-dlp/formats] %s", msg)

            class _FmtLogger:
                def debug(self, msg: str) -> None: _ydl_logger_cb(msg)  # noqa: E704
                def info(self, msg: str) -> None: _ydl_logger_cb(msg)  # noqa: E704
                def warning(self, msg: str) -> None: _ydl_logger_cb(msg)  # noqa: E704
                def error(self, msg: str) -> None: _ydl_logger_cb(msg)  # noqa: E704

            ydl_opts = {
                "quiet": False,
                "no_warnings": False,
                "nocheckcertificate": True,
                "no_playlist": True,
                **_detect_js_runtime(),
                "logger": _FmtLogger(),
                **extra,
            }
            # YouTube: force player clients that expose live HLS manifests.
            # Harmless for non-live YouTube videos (same clients still return formats).
            url_lc = (url or "").lower()
            if "youtube.com" in url_lc or "youtu.be" in url_lc:
                ydl_opts["extractor_args"] = {
                    "youtube": {
                        "player_client": ["web_safari", "web", "mweb", "web_embedded"],
                    }
                }

            loop = asyncio.get_event_loop()
            _fetch_error: list[str] = []

            def _fetch():
                # Pass 1: full processing (format selection included)
                try:
                    with YoutubeDL(ydl_opts) as ydl:
                        return ydl.extract_info(url, download=False)
                except (_ydl_utils.DownloadError, _ydl_utils.ExtractorError) as exc:
                    _fetch_error.append(str(exc))
                except Exception as exc:
                    _fetch_error.append(str(exc))
                    return None  # non-yt-dlp error — don't retry

                # Pass 2: process=False bypasses format selection entirely and
                # returns the raw extractor output with all available formats.
                logger.info("yt-dlp /formats pass-2 (process=False) for %s", url)
                try:
                    with YoutubeDL(ydl_opts) as ydl:
                        return ydl.extract_info(url, download=False, process=False)
                except Exception as exc2:
                    _fetch_error.append(str(exc2))
                    return None

            try:
                info = await asyncio.wait_for(loop.run_in_executor(None, _fetch), timeout=30)
            except asyncio.TimeoutError:
                return JSONResponse({"formats": [], "error": "Timeout (30s) — sprawdź połączenie lub spróbuj ponownie"}, status_code=400)

            if not info:
                detail = _fetch_error[0] if _fetch_error else "Brak danych"
                logger.warning("yt-dlp /formats failed for %s: %s", url, detail)
                return JSONResponse({"formats": [], "error": detail}, status_code=400)

            formats = []
            for f in info.get("formats", []):
                format_id = f.get("format_id")
                ext = (f.get("ext") or "").lower()
                resolution = f.get("resolution")
                height = f.get("height")
                fps = f.get("fps")
                vcodec = (f.get("vcodec") or "").lower()
                acodec = (f.get("acodec") or "").lower()
                note = f.get("format_note")
                filesize_approx = f.get("filesize_approx")

                is_audio_only = (not vcodec or vcodec == "none") and acodec and acodec != "none"
                is_video_only = vcodec and vcodec != "none" and (not acodec or acodec == "none")

                label_parts = []
                if resolution and resolution != "unknown": label_parts.append(resolution)
                elif height: label_parts.append(f"{height}p")
                if fps: label_parts.append(f"{fps}fps")
                if ext: label_parts.append(ext)
                if vcodec and vcodec != "none": label_parts.append(vcodec)
                if acodec and acodec != "none": label_parts.append(acodec)
                if note: label_parts.append(f"({note})")
                if filesize_approx: label_parts.append(f"~{filesize_approx / (1024 * 1024):.1f}MB")

                if not label_parts:
                    continue

                formats.append({
                    "id": format_id,
                    "label": " · ".join(label_parts),
                    "height": int(height) if height else None,
                    "ext": ext,
                    "fps": int(fps) if fps else None,
                    "is_audio_only": bool(is_audio_only),
                    "is_video_only": bool(is_video_only),
                })

            def sort_formats(f):
                label = f["label"].lower()
                if "mp3" in label or "m4a" in label: return (0, label)
                if "p" in label:
                    try:
                        h = int(re.search(r"(\d+)p", label).group(1))
                        return (1, -h, label)
                    except Exception:
                        pass
                return (2, label)

            formats.sort(key=sort_formats)

            if not formats:
                formats = [{"id": "mp4-720", "label": "MP4 · 720p (domyślny)"}]

            return JSONResponse({"formats": formats})

        except Exception as e:
            logger.exception(f"Błąd API /formats dla URL: {url}")
            return JSONResponse({"formats": [], "error": str(e)}, status_code=500)

    @app.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket):
        """
        Endpoint WebSocket — stałe połączenie do live updates.
        
        WebSocket to jak telefon który zostaje otwarty:
        - przeglądarka dzwoni do serwera
        - połączenie zostaje otwarte
        - serwer wysyła aktualizacje kiedy chce (bez pytania)
        - połączenie trwa dopóki ktoś nie rozłączy
        """
        # Zaakceptuj połączenie
        await websocket.accept()

        # Dodaj do listy słuchaczy
        manager.listeners.append(websocket)

        # Wyślij od razu aktualny stan wszystkich zadań
        await websocket.send_text(json.dumps({
            "type": "init",
            "tasks": manager.get_all_tasks()
        }))

        try:
            # Czekaj w nieskończoność (serwer wysyła, klient słucha)
            # Jeśli klient wyśle wiadomość (np. ping), po prostu ignorujemy
            while True:
                await websocket.receive_text()
        except WebSocketDisconnect:
            # Klient się rozłączył (zamknął okno, odświeżył stronę)
            pass
        finally:
            # Usuń z listy słuchaczy
            if websocket in manager.listeners:
                manager.listeners.remove(websocket)

    return app
