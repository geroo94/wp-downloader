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
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response, StreamingResponse
from pydantic import BaseModel

import sys
import os

from binaries import get_ffmpeg, get_ffprobe
from cutter import CutterJob, CutterManager
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
    cookies_file: str = ""     # ścieżka do wyeksportowanego cookies.txt (priorytet nad browser)
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


_WHISPER_MODEL_SIZES = ("tiny", "base", "small", "medium", "large", "large-v2", "large-v3")

# Per-rozmiar initial_prompt mocno biases Whisper na kierunek "to jest polski język"
# — redukuje halucynacje gdy nagranie zaczyna się od muzyki/szumu/ciszy.
_INITIAL_PROMPTS = {
    "pl": "Poniżej znajduje się dokładna transkrypcja nagrania w języku polskim. Zachowaj polskie znaki diakrytyczne (ą, ć, ę, ł, ń, ó, ś, ź, ż).",
}


async def _run_transcription(manager: DownloadManager, task_id: str, video_path: str,
                              language: str = "pl", model_size: str = "small") -> None:
    """Background task: transcribe a video file with Whisper and save as .txt with timecodes.

       Args:
           language: ISO 639-1 code ("pl", "en", "uk", ...) or "" for auto-detect.
                     Default "pl" — większość naszych pobierań to materiały po polsku
                     (WP/TVP/Polsat itd.), auto-detect na początku często myli się z
                     angielskim gdy nagranie zaczyna się od muzyki/jingli.
           model_size: jeden z `_WHISPER_MODEL_SIZES`. Default "small" — minimum
                     dla sensownego polskiego. "tiny"/"base" mają tragiczną jakość
                     dla pl (ucinanie wyrazów, błędne znaki). Większe modele = lepsza
                     jakość ale wolniejsze i więcej RAM.
    """
    # Walidacja rozmiaru modelu (whitelist — żeby ktoś nie wrzucił pathów na dysk)
    model_size = model_size if model_size in _WHISPER_MODEL_SIZES else "small"

    await manager.update_task(task_id, transcription="in_progress")
    txt_path = os.path.splitext(video_path)[0] + ".txt"
    loop = asyncio.get_event_loop()
    try:
        import whisper

        def _transcribe() -> str:
            # Frozen bundle (PyInstaller) czasem nie widzi systemowego magazynu
            # certyfikatów CA — whisper.load_model() ściąga wagi modelu przez
            # urllib z serwerów OpenAI i pada na
            # ssl.SSLCertVerificationError: CERTIFICATE_VERIFY_FAILED.
            # Wyłączamy weryfikację TYLKO dla tego pobrania (nie dotyka innych
            # połączeń HTTPS w aplikacji poza tym, że ssl to atrybut modułowy —
            # patch działa od pierwszego wywołania transkrypcji w tym procesie).
            import ssl
            try:
                _create_unverified_https_context = ssl._create_unverified_context
            except AttributeError:
                pass
            else:
                ssl._create_default_https_context = _create_unverified_https_context

            model = whisper.load_model(model_size)
            # Parametry zoptymalizowane pod jakość transkrypcji polskiej:
            # - task="transcribe" (nie "translate" które zamienia na angielski)
            # - language="pl" (lub user-selected) — explicit, bez auto-detect halucynacji
            # - fp16=False — wymusza float32 (CPU friendly; fp16 na CPU bywa unstable)
            # - condition_on_previous_text=False — KLUCZOWE: domyślnie Whisper
            #   "pamięta" poprzedni segment do kondycjonowania kolejnego, co bardzo
            #   często wpada w pętlę halucynacji ("powtarza dziwne frazy"). Wyłączone
            #   znacząco poprawia stabilność dla długich nagrań.
            # - temperature: 0.0 jako pierwszy próg, potem fallback do wyższych
            #   gdy compression_ratio przekroczy próg (default whisper behaviour).
            # - beam_size=5 — beam search zamiast greedy decode; lepsza jakość
            #   kosztem ~3x wolniejszego decode (warto dla pl).
            # - initial_prompt — sterowanie modelem na polskie diakrytyki.
            kwargs = {
                "task": "transcribe",
                "fp16": False,
                "condition_on_previous_text": False,
                "beam_size": 5,
                "temperature": (0.0, 0.2, 0.4, 0.6, 0.8, 1.0),
                "compression_ratio_threshold": 2.4,
                "no_speech_threshold": 0.6,
                "verbose": False,
            }
            if language:
                kwargs["language"] = language
                if language in _INITIAL_PROMPTS:
                    kwargs["initial_prompt"] = _INITIAL_PROMPTS[language]

            # Real progress: whisper.transcribe() prowadzi wewnętrzny tqdm nad
            # liczbą ramek audio (verbose=False => pasek aktywny, tylko nie
            # drukowany). Podmieniamy klasę tqdm na czas tego wywołania, żeby
            # przechwycić postęp i przekazać go do UI przez update_task —
            # whisper nie ma publicznego progress callbacku, więc to jedyny
            # nieinwazyjny sposób na realny procent zamiast statycznej "in_progress".
            #
            # UWAGA: `import whisper.transcribe as _wt` daje FUNKCJĘ, nie moduł —
            # whisper/__init__.py robi `from .transcribe import transcribe`, co
            # nadpisuje atrybut `whisper.transcribe` (submodule) nazwą funkcji
            # o tej samej nazwie. sys.modules omija to nadpisanie i daje prawdziwy
            # moduł z dostępem do jego wewnętrznego `tqdm`.
            _wt = sys.modules["whisper.transcribe"]
            last_pct = -1

            class _ProgressTqdm(_wt.tqdm.tqdm):
                def update(self_tqdm, n=1):
                    super().update(n)
                    nonlocal last_pct
                    if self_tqdm.total:
                        pct = min(99, int(self_tqdm.n / self_tqdm.total * 100))
                        if pct != last_pct:
                            last_pct = pct
                            asyncio.run_coroutine_threadsafe(
                                manager.update_task(task_id, transcription_progress=float(pct)),
                                loop,
                            )

            _orig_tqdm = _wt.tqdm.tqdm
            _wt.tqdm.tqdm = _ProgressTqdm
            try:
                result = model.transcribe(video_path, **kwargs)
            finally:
                _wt.tqdm.tqdm = _orig_tqdm
            lines = []
            for seg in result.get("segments", []):
                text = seg["text"].strip()
                if text:
                    lines.append(f"[{_fmt_ts(seg['start'])}] {text}")
            return "\n".join(lines) if lines else result.get("text", "").strip()

        text = await asyncio.wait_for(loop.run_in_executor(None, _transcribe), timeout=3600)
        # encoding="utf-8" wymusza poprawny zapis polskich diakrytyków na Windowsie
        # (gdzie default to cp1250); dodajemy newline na końcu pliku (POSIX).
        with open(txt_path, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
            if text and not text.endswith("\n"):
                f.write("\n")
        await manager.update_task(task_id, transcription=txt_path)
        logger.info("Transkrypcja gotowa (%s/%s): %s", model_size, language or "auto", txt_path)
    except ImportError:
        await manager.update_task(task_id, transcription="error")
        logger.error("openai-whisper nie jest zainstalowany")
    except Exception as e:
        await manager.update_task(task_id, transcription="error")
        logger.exception("Błąd transkrypcji (%s/%s): %s", model_size, language or "auto", e)


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
_cutter = CutterManager()

# Aktywne ffmpeg pipe transkodujące live preview dla Fast Cutter.
# Klucz = session_id z frontendu. Każde nowe żądanie tej samej sesji
# (np. przy seek) zabija poprzedni proces przed spawn nowego.
_cutter_live_procs: dict[str, asyncio.subprocess.Process] = {}


async def _kill_cutter_live_proc(session: str) -> None:
    proc = _cutter_live_procs.pop(session, None)
    if proc and proc.returncode is None:
        try:
            proc.kill()
        except Exception:
            pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=2.0)
        except asyncio.TimeoutError:
            pass


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

    # Wstrzyknij DownloadManager do CutterManager żeby postęp renderu leciał
    # przez update_task → WebSocket → karta w Historii pobierania.
    _cutter._manager = manager

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

        # Fast Cutter teardown — zabij live-transcodery preview i aktywne
        # rendery, żeby zamknięcie aplikacji nie zostawiało sierot ffmpeg.
        for session in list(_cutter_live_procs):
            await _kill_cutter_live_proc(session)
        for job in _cutter.jobs.values():
            if job.proc and job.proc.returncode is None:
                try:
                    job.proc.kill()
                except Exception:
                    pass

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
        log_path = os.environ.get("WP_DOWNLOADER_LOG_PATH", "")
        if not log_path:
            return PlainTextResponse("(ścieżka logu nieustawiona)")
        try:
            with open(log_path, encoding="utf-8", errors="replace") as f:
                all_lines = f.readlines()
            tail = "".join(all_lines[-lines:])
            return PlainTextResponse(tail)
        except FileNotFoundError:
            return PlainTextResponse(f"(plik logu nie istnieje: {log_path})")
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
           Body: {"language": "pl", "model_size": "small"}
                 language="" → auto-detect; model_size whitelist w _WHISPER_MODEL_SIZES."""
        task = manager.tasks.get(task_id)
        if not task:
            return JSONResponse({"error": "Zadanie nie istnieje"}, status_code=404)
        if task.status != "done" or not task.output_path:
            return JSONResponse({"error": "Tylko zakończone zadania można transkrybować"}, status_code=400)
        if task.transcription == "in_progress":
            return JSONResponse({"error": "Transkrypcja już w toku"}, status_code=400)
        language = "pl"  # default — większość naszych pobierań to polski
        model_size = "small"  # default — minimum dla sensownego polskiego
        if payload and isinstance(payload, dict):
            raw_lang = (payload.get("language") or "").strip().lower()
            if raw_lang == "auto":
                language = ""  # explicit auto-detect
            elif re.fullmatch(r"[a-z]{2,3}", raw_lang):
                language = raw_lang
            raw_size = (payload.get("model_size") or "").strip().lower()
            if raw_size in _WHISPER_MODEL_SIZES:
                model_size = raw_size
        asyncio.create_task(_run_transcription(manager, task_id, task.output_path, language, model_size))
        return JSONResponse({"ok": True, "language": language or "auto", "model_size": model_size})

    @app.post("/api/tasks/{task_id}/reveal")
    async def reveal_task_file(task_id: str):
        """Otwiera lokalizację pliku w Finderze (macOS) / Eksploratorze (Windows)
        z plikiem **zaznaczonym**. Jeżeli plik zniknął (np. user go usunął albo
        ścieżka po merge ffmpegu uległa zmianie), otwiera sam folder zamiast
        wysyłać `open -R` na nieistniejący plik (co na macOS jest no-op'em)."""
        task = manager.tasks.get(task_id)
        if not task:
            return JSONResponse({"error": "Zadanie nie istnieje"}, status_code=404)

        # Strip whitespace — czasem `output_path` po merge ffmpegu ma trailing
        # spację (zwłaszcza w title-fallback outtmpl).
        path = (task.output_path or "").strip()
        if not path:
            return JSONResponse({"error": "Brak ścieżki pliku"}, status_code=404)

        file_exists = os.path.exists(path)
        parent = os.path.dirname(path)
        fallback = False
        try:
            if file_exists:
                if sys.platform == "darwin":
                    cmd = ["open", "-R", path]
                elif sys.platform == "win32":
                    # explorer.exe wymaga `/select,PATH` jako JEDEN argument
                    # (bez spacji między przecinkiem a ścieżką) + ścieżki z
                    # backslashami. PyInstaller/yt-dlp trzymają forward slashes
                    # w outtmpl, więc bez normpath() explorer odmawia (otwiera
                    # tylko folder bez selekcji albo nic).
                    win_path = os.path.normpath(path)
                    cmd = ["explorer", f"/select,{win_path}"]
                else:
                    cmd = ["xdg-open", parent or path]
            else:
                fallback = True
                target_dir = parent if (parent and os.path.isdir(parent)) else None
                if not target_dir:
                    return JSONResponse(
                        {"error": "Plik nie istnieje, folder też nie."},
                        status_code=404,
                    )
                if sys.platform == "darwin":
                    cmd = ["open", target_dir]
                elif sys.platform == "win32":
                    cmd = ["explorer", os.path.normpath(target_dir)]
                else:
                    cmd = ["xdg-open", target_dir]
            subprocess.Popen(cmd)
            logger.info("reveal: cmd=%r exists=%s fallback=%s", cmd, file_exists, fallback)
            return JSONResponse({"ok": True, "fallback": "directory_only" if fallback else None})
        except Exception as e:
            logger.warning("reveal: subprocess failed: %s (path=%r)", e, path)
            return JSONResponse({"error": str(e)}, status_code=500)

    # ── Fast Cutter ────────────────────────────────────────────────────

    _CUTTER_MIME = {
        ".mp4": "video/mp4",
        ".mov": "video/quicktime",
        ".mkv": "video/x-matroska",
        ".webm": "video/webm",
    }

    @app.get("/api/cutter/info")
    async def cutter_info(path: str):
        """Metadane pliku (duration + wideo stream) przez ffprobe.

        Frontend wywołuje raz przy ładowaniu pliku żeby ustawić slider.max
        i tc-total. Alternatywa dla `<video>.duration` (który nie działa gdy
        Chromium w PyQt6-WebEngine nie ma H.264 codec).
        """
        if not os.path.isfile(path):
            return JSONResponse({"error": "Plik nie istnieje"}, status_code=404)
        cmd = [get_ffprobe(), "-v", "error",
               "-show_entries", "format=duration:stream=width,height,codec_name",
               "-select_streams", "v:0",
               "-of", "json", path]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            out, err = await proc.communicate()
            if proc.returncode != 0:
                return JSONResponse({"error": err.decode("utf-8", "replace")}, status_code=500)
            data = json.loads(out.decode("utf-8"))
            fmt = data.get("format", {})
            vs = (data.get("streams") or [{}])[0]
            return JSONResponse({
                "duration": float(fmt.get("duration", 0) or 0),
                "width": int(vs.get("width", 0) or 0),
                "height": int(vs.get("height", 0) or 0),
                "codec": vs.get("codec_name", ""),
            })
        except FileNotFoundError:
            return JSONResponse({"error": "ffprobe brak w PATH"}, status_code=500)
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)

    @app.get("/api/cutter/frame")
    async def cutter_frame(path: str, t: float = 0.0, w: int = 960):
        """Pojedyncza klatka JPG w czasie t (sekundy).

        `-ss` przed `-i` = fast seek na najbliższą keyframę. `-frames:v 1`
        gwarantuje jedną klatkę. `-vf scale=W:-2` skaluje do zadanej szerokości
        zachowując aspect. Chromium akceptuje image/jpeg natywnie — brak
        H264 codec issue z playera.
        """
        if not os.path.isfile(path):
            return JSONResponse({"error": "Plik nie istnieje"}, status_code=404)
        t = max(0.0, float(t))
        w = max(160, min(1920, int(w)))
        cmd = [get_ffmpeg(), "-hide_banner", "-loglevel", "error",
               "-ss", f"{t:.3f}", "-i", path,
               "-frames:v", "1", "-vf", f"scale={w}:-2",
               "-f", "mjpeg", "-q:v", "3", "pipe:1"]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            out, err = await proc.communicate()
            if proc.returncode != 0 or not out:
                return JSONResponse(
                    {"error": err.decode("utf-8", "replace") or "ffmpeg failed"},
                    status_code=500,
                )
            return Response(
                content=out,
                media_type="image/jpeg",
                headers={"Cache-Control": "no-store"},
            )
        except FileNotFoundError:
            return JSONResponse({"error": "ffmpeg brak w PATH"}, status_code=500)
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)

    @app.get("/api/cutter/live-stream")
    async def cutter_live_stream(path: str, start: float = 0.0, session: str = "default",
                                 logo_src: str = "", logo_custom: str = "",
                                 logo_pos: str = "tr", logo_scale: float = 0.16,
                                 logo_x: int = 20, logo_y: int = 20,
                                 vol: float = 1.0, sub: int = 0, sub_custom: str = "",
                                 src: str = "", src_size: int = 28,
                                 src_pos: str = "tl",
                                 source_x: int = 24, source_y: int = 24):
        """Live-transcode wideo do WebM/VP8+Opus przez pipe ffmpeg.

        Chromium w PyQt6-WebEngine nie ma H264 (LGPL). VP8/Opus w WebM są
        otwarte i dekodowane natywnie. Seek = restart ffmpeg z -ss PRZED -i
        (poprzedni proces sesji jest zabijany). Parametr `session`
        identyfikuje instancję UI — jeden aktywny transkode per user.

        Parametry logo_* (suwaki brandingu) wstrzykują overlay logo do
        filter_complex podglądu — user widzi na żywo dokładnie tę skalę
        i pozycję, która trafi do finalnego renderu.
        """
        if not os.path.isfile(path):
            return JSONResponse({"error": "Plik nie istnieje"}, status_code=404)
        start = max(0.0, float(start))

        await _kill_cutter_live_proc(session)

        # Rozwiązanie pliku logo jak w renderze (default z bundla / custom user)
        logo_file = ""
        if logo_src == "default":
            from cutter import _default_logo_path
            p = _default_logo_path()
            logo_file = p if os.path.isfile(p) else ""
        elif logo_src == "custom" and logo_custom and os.path.isfile(logo_custom):
            logo_file = logo_custom

        # Nakładka suba w podglądzie: animacja leci od bieżącej pozycji
        # strumienia (nie od znaczników cięcia — te żyją po stronie klienta).
        # Cel: user widzi na żywo jak animacja komponuje się z materiałem;
        # dokładne okna czasowe stosuje dopiero finalny render.
        from cutter import (_bundled_ffmpeg, _default_sub_path,
                            _drawtext_fontfile, _filter_path_escape)
        sub_file = ""
        if int(sub):
            if sub_custom and os.path.isfile(sub_custom):
                sub_file = sub_custom
            else:
                p = _default_sub_path()
                sub_file = p if os.path.isfile(p) else ""

        # Tekst źródła w podglądzie (statyczny — typewriter to efekt startu
        # klipu, w strumieniu od dowolnej pozycji pokazujemy pełny napis,
        # żeby suwaki rozmiaru/pozycji działały na żywo).
        src_txt = (src or "").strip()[:80]
        ffmpeg_bin = get_ffmpeg()
        if src_txt:
            if not await _cutter._has_filter("drawtext", ffmpeg_bin):
                alt = _bundled_ffmpeg()
                if alt and await _cutter._has_filter("drawtext", alt):
                    ffmpeg_bin = alt
                else:
                    src_txt = ""  # brak drawtext gdziekolwiek — podgląd bez tekstu

        cmd = [
            ffmpeg_bin, "-hide_banner", "-loglevel", "error",
            "-ss", f"{start:.3f}", "-i", path,
        ]
        if logo_file or sub_file or src_txt:
            # Podgląd renderuje w 480p — skalę/marginesy z suwaków (podane
            # względem pikseli źródła) przeliczamy proporcjonalnie.
            meta = await _cutter._probe_media(path)
            pw = max(2, round(480 * meta["w"] / max(1, meta["h"]) / 2) * 2)
            fc_parts = ["[0:v]scale=-2:480[v0]"]
            cur = "[v0]"
            in_idx = 1
            if logo_file:
                cmd += ["-i", logo_file]
                if logo_src == "default":
                    # Bazowe logo 1920x1080: skala 100% + zerowe marginesy
                    # = fullframe od 0:0. Suwaki w pełni aktywne — skala
                    # pomniejsza grafikę (kotwica: prawy górny róg, tam
                    # siedzi znak), X/Y odsuwają od krawędzi. Identyczne
                    # mapowanie jak w finalnym renderze (_cmd_branded).
                    s = min(1.0, max(0.05, float(logo_scale)))
                    lw = max(32, round(pw * s))
                    lh = max(18, round(480 * s))
                    dmx = max(0, round(int(logo_x) * pw / 1920))
                    dmy = max(0, round(int(logo_y) * 480 / 1080))
                    fc_parts.append(f"[{in_idx}:v]scale={lw}:{lh}:"
                                    f"force_original_aspect_ratio=decrease[lg]")
                    fc_parts.append(f"{cur}[lg]overlay=W-w-{dmx}:{dmy}:format=auto[vl]")
                else:
                    scale = min(0.40, max(0.05, float(logo_scale)))
                    lw = max(16, round(pw * scale))
                    mx = max(0, round(int(logo_x) * pw / max(1, meta["w"])))
                    my = max(0, round(int(logo_y) * 480 / max(1, meta["h"])))
                    pos = _cutter._pos_overlay(logo_pos, mx, my)
                    fc_parts.append(f"[{in_idx}:v]scale={lw}:-1[lg]")
                    fc_parts.append(f"{cur}[lg]overlay={pos}:format=auto[vl]")
                cur = "[vl]"
                in_idx += 1
            if sub_file:
                cmd += ["-i", sub_file]
                fc_parts.append(
                    f"[{in_idx}:v]scale={pw}:480:force_original_aspect_ratio=decrease,"
                    f"pad={pw}:480:(ow-iw)/2:(oh-ih)/2:color=black@0.0,"
                    f"setpts=PTS-STARTPTS[sb]")
                fc_parts.append(f"{cur}[sb]overlay=0:0:eof_action=pass:format=auto[vsb]")
                cur = "[vsb]"
                in_idx += 1
            if src_txt:
                # Sanityzacja identyczna z renderem (parser opcji drawtext).
                clean = (src_txt.replace("\\", "").replace("'", "’")
                                .replace(";", ",")
                                .replace(":", r"\:").replace("%", r"\%"))
                fsize = max(9, round(min(96, max(12, int(src_size))) * 480 / 1080))
                smx = max(0, round(max(0, int(source_x)) * pw / 1920))
                smy = max(0, round(max(0, int(source_y)) * 480 / 1080))
                sp = src_pos if src_pos in ("tl", "tr", "bl", "br") else "tl"
                sxy = {
                    "tl": f"x={smx}:y={smy}",
                    "tr": f"x=w-tw-{smx}:y={smy}",
                    "bl": f"x={smx}:y=h-th-{smy}",
                    "br": f"x=w-tw-{smx}:y=h-th-{smy}",
                }[sp]
                ff = _filter_path_escape(_drawtext_fontfile())
                fc_parts.append(
                    f"{cur}drawtext=fontfile='{ff}':text='{clean}':"
                    f"fontcolor=white:fontsize={fsize}:bordercolor=black:"
                    f"borderw=2:{sxy}[vtxt]")
                cur = "[vtxt]"
            # format=yuv420p po overlay'ach jest obowiązkowy: RGBA logo /
            # CineForm z alfą negocjują format, którego libvpx nie otwiera
            # (enkoder padał "Could not open encoder" → pusty strumień).
            fc_parts.append(f"{cur}format=yuv420p[vout]")
            cmd += ["-filter_complex", ";".join(fc_parts),
                    "-map", "[vout]", "-map", "0:a?"]
        else:
            cmd += ["-vf", "scale=-2:480"]

        # Mixer w podglądzie: -af działa na zmapowanym audio niezależnie
        # od video filter_complex.
        vol = min(2.0, max(0.0, float(vol)))
        if abs(vol - 1.0) >= 0.01:
            cmd += ["-af", f"volume={vol:.3f}"]
        cmd += [
            "-c:v", "libvpx",
            "-deadline", "realtime",
            "-cpu-used", "8",
            "-b:v", "1M",
            "-c:a", "libopus",
            "-b:a", "96k",
            "-f", "webm",
            # 2 MB / 2000 ms klastry — dłuższe fragmenty pozwalają Chromium
            "-cluster_size_limit", "2M",
            "-cluster_time_limit", "2000",
            "-fflags", "+nobuffer",
            "-flush_packets", "1",
            "pipe:1",
        ]

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except FileNotFoundError:
            return JSONResponse({"error": "ffmpeg brak w PATH"}, status_code=500)

        _cutter_live_procs[session] = proc

        async def _gen():
            assert proc.stdout
            try:
                while True:
                    chunk = await proc.stdout.read(16 * 1024)
                    if not chunk:
                        break
                    yield chunk
            finally:
                if proc.returncode is None:
                    try:
                        proc.kill()
                    except Exception:
                        pass
                _cutter_live_procs.pop(session, None)

        return StreamingResponse(
            _gen(),
            media_type="video/webm",
            headers={
                "Cache-Control": "no-store",
                "X-Accel-Buffering": "no",
            },
        )

    @app.post("/api/cutter/live-stop")
    async def cutter_live_stop(session: str = "default"):
        """Zabija ffmpeg live-transcoder dla sesji (JS wywołuje na reset / tab-switch)."""
        await _kill_cutter_live_proc(session)
        return JSONResponse({"stopped": True})

    @app.get("/api/cutter/preview")
    @app.get("/api/cutter/stream")
    async def cutter_preview(path: str, request: Request):
        """Stream lokalnego pliku wideo do <video> w QtWebEngine.

        Chromium wysyła `Range: bytes=0-` w pierwszym żądaniu metadata.
        Bez 206 Partial Content + jawnego Accept-Ranges/Content-Range video
        zostaje w stanie readyState=0 (czarny ekran, brak `loadedmetadata`).
        FileResponse Starlette nie zawsze respektuje Range w QtWebEngine —
        ręczny StreamingResponse gwarantuje poprawną sekwencję nagłówków.
        """
        if not os.path.isfile(path):
            return JSONResponse({"error": "Plik nie istnieje"}, status_code=404)

        ext = os.path.splitext(path)[1].lower()
        mime = _CUTTER_MIME.get(ext, "video/mp4")
        file_size = os.path.getsize(path)
        range_header = request.headers.get("range")

        CHUNK = 64 * 1024

        if range_header and range_header.startswith("bytes="):
            rng = range_header[6:]
            start_s, _, end_s = rng.partition("-")
            try:
                start = int(start_s) if start_s else 0
                end = int(end_s) if end_s else file_size - 1
            except ValueError:
                return JSONResponse({"error": "Bad Range"}, status_code=416)
            end = min(end, file_size - 1)
            if start > end or start >= file_size:
                return JSONResponse({"error": "Range not satisfiable"}, status_code=416)
            length = end - start + 1

            def _iter_range():
                with open(path, "rb") as f:
                    f.seek(start)
                    remaining = length
                    while remaining > 0:
                        chunk = f.read(min(CHUNK, remaining))
                        if not chunk:
                            break
                        remaining -= len(chunk)
                        yield chunk

            return StreamingResponse(
                _iter_range(),
                status_code=206,
                media_type=mime,
                headers={
                    "Accept-Ranges": "bytes",
                    "Content-Range": f"bytes {start}-{end}/{file_size}",
                    "Content-Length": str(length),
                    "Cache-Control": "no-store",
                },
            )

        def _iter_full():
            with open(path, "rb") as f:
                while True:
                    chunk = f.read(CHUNK)
                    if not chunk:
                        break
                    yield chunk

        return StreamingResponse(
            _iter_full(),
            media_type=mime,
            headers={
                "Accept-Ranges": "bytes",
                "Content-Length": str(file_size),
                "Cache-Control": "no-store",
            },
        )

    class CutterRenderRequest(BaseModel):
        input_path: str
        start_ts: float
        end_ts: float
        logo_src: str = ""
        logo_pos: str = "tr"
        logo_custom_path: str = ""
        # Suwaki brandingu: skala 0.05–0.40 (ułamek szerokości), marginesy px
        logo_scale: float = 0.16
        logo_x: int = 20
        logo_y: int = 20
        src_text: str = ""
        # Formatowanie źródła: rozmiar (px @1080p), narożnik (tl bazowy)
        # i marginesy X/Y od narożnika (suwaki Pozycja X/Y źródła)
        src_size: int = 28
        src_pos: str = "tl"
        src_x: int = 24
        src_y: int = 24
        outro_src: str = ""
        outro_custom_path: str = ""
        # Overlap outro w sekundach (0–3): overlay z alfą przed końcem filmu
        outro_overlap: float = 0.0
        # Mixer: głośność głównego materiału 0.0–2.0 (1.0 = bez zmian)
        audio_volume: float = 1.0
        # Animowany przycisk subskrypcji na starcie i przed tyłówką
        sub_overlay: bool = False
        # Własny plik animacji SUB — puste/nieistniejące → fallback do domyślnego
        sub_custom_path: str = ""
        # Jeśli podane (user wybrał SaveAs w UI), nadpisuje auto-generated
        # path w CutterManager._output_path. Pusty = zapisz obok źródła.
        output_path: str = ""

    @app.post("/api/cutter/render")
    async def cutter_render(req: CutterRenderRequest):
        if not os.path.isfile(req.input_path):
            return JSONResponse({"error": "Plik nie istnieje"}, status_code=404)
        if req.end_ts <= req.start_ts:
            return JSONResponse({"error": "Zły zakres czasu (koniec musi być po starcie)"}, status_code=400)
        request_id = uuid.uuid4().hex

        # Tytuł dla karty w Historii: nazwa pliku wynikowego lub źródła
        display_target = (req.output_path or req.input_path)
        display_title = f"{os.path.basename(display_target)} (cut)"

        # Rejestruj render-task w DownloadManager — pojawi się jako karta w UI,
        # WebSocket task_update będzie leciał z CutterManager._emit().
        dm_task_id = manager.add_render_task(
            title=display_title,
            output_path=(req.output_path or ""),
        )

        job = CutterJob(
            request_id=request_id,
            input_path=req.input_path,
            start_ts=float(req.start_ts),
            end_ts=float(req.end_ts),
            logo_src=req.logo_src,
            logo_pos=req.logo_pos,
            logo_custom_path=req.logo_custom_path,
            logo_scale=min(0.40, max(0.05, float(req.logo_scale or 0.16))),
            logo_x=min(1000, max(0, int(req.logo_x))),
            logo_y=min(1000, max(0, int(req.logo_y))),
            src_text=req.src_text,
            src_size=min(96, max(12, int(req.src_size or 28))),
            src_pos=(req.src_pos if req.src_pos in ("tl", "tr", "bl", "br") else "tl"),
            src_x=min(1000, max(0, int(req.src_x))),
            src_y=min(1000, max(0, int(req.src_y))),
            outro_src=req.outro_src,
            outro_custom_path=req.outro_custom_path,
            outro_overlap=min(3.0, max(0.0, float(req.outro_overlap or 0.0))),
            audio_volume=min(2.0, max(0.0, float(req.audio_volume
                                                 if req.audio_volume is not None else 1.0))),
            sub_overlay=bool(req.sub_overlay),
            sub_custom_path=req.sub_custom_path,
            output_path=(req.output_path or ""),
            download_task_id=dm_task_id,
        )
        mode = await _cutter.start(job)

        # Pierwszy broadcast — pojawia się karta w renderTasks bez czekania na progres
        await manager.update_task(dm_task_id, progress=0.0)

        return JSONResponse({
            "request_id": request_id,
            "download_task_id": dm_task_id,
            "mode": mode,
            "output_path": job.output_path,
        })

    @app.get("/api/cutter/status")
    async def cutter_status(id: str):
        job = _cutter.jobs.get(id)
        if not job:
            return JSONResponse({"error": "Job nie istnieje"}, status_code=404)
        return JSONResponse({
            "status": job.status,
            "progress": job.progress,
            "speed": job.speed,
            "output_path": job.output_path,
            "error": job.error,
        })

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
                cookies_file=req.cookies_file,
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
    async def get_formats(url: str, cookies_browser: str = "", cookies_file: str = ""):
        """Pobiera dostępne formaty dla podanego URL używając yt-dlp API."""
        from yt_dlp import YoutubeDL
        import yt_dlp.utils as _ydl_utils

        try:
            extra: dict = {}
            _cff = (cookies_file or "").strip()
            if _cff and os.path.isfile(_cff):
                extra["cookiefile"] = _cff
            else:
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
