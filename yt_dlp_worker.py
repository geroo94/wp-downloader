"""
YtDlpWorker — yt-dlp Python API, no subprocess.
Each download runs in a thread-pool executor; progress via hooks.
Graceful stop triggers FFmpeg merge of partial intermediate files.
"""

import asyncio
import glob
import shutil
import sys
import threading
import os
import re
import logging
from pathlib import Path
from typing import Any

from yt_dlp import YoutubeDL
import yt_dlp.utils

from download_manager import DownloadManager, DownloadTask

logger = logging.getLogger(__name__)

DOWNLOAD_DIR = str(Path.home() / "Downloads" / "WP Downloader")

def _bundled_runtime_dir() -> list[str]:
    """Kandydujące lokalizacje `bin/` z dołączonym deno binary.

    Frozen .app / .exe spakowany PyInstallerem bierze deno z `_MEIPASS/bin/`
    (--onedir: rozpakowywany obok exe) lub z `bin/` w katalogu z exe (macOS
    .app Resources/bin po `--add-data=bin:bin`).
    Dev mode: project_root/bin/.
    """
    cands: list[str] = []
    if hasattr(sys, "_MEIPASS"):
        cands.append(os.path.join(sys._MEIPASS, "bin"))
        cands.append(os.path.join(os.path.dirname(sys.executable), "bin"))
        # macOS .app: exe siedzi w Contents/MacOS/, deno w Contents/Resources/bin/
        cands.append(os.path.join(os.path.dirname(sys.executable), "..", "Resources", "bin"))
    # Dev mode (repo root)
    cands.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), "bin"))
    return [os.path.normpath(c) for c in cands]


def _detect_js_runtime() -> dict:
    """yt-dlp opts dla YouTube nsig challenge.

    yt-dlp 2026+ wymaga deno (lub node/bun/qjs) — bez tego YouTube zwraca 403
    Forbidden na czystych Windowsach (brak runtime w PATH). Szukamy w kolejności:

    1. **bundled** — bin/deno[.exe] dołączone do paczki przez .spec datas.
       Najważniejsze: niezależne od stanu PATH na maszynie usera.
    2. **PATH** — fallback gdy dev/power-user ma zainstalowane.
    3. Brak — log warning, YouTube będzie zwracać 403.

    remote_components="ejs:github" mówi yt-dlp żeby pobrał+cache'ował EJS
    (Embedded JavaScript) solver przy pierwszym użyciu.
    """
    binary = "deno.exe" if sys.platform == "win32" else "deno"

    # 1) bundled — najwyższy priorytet
    for cand_dir in _bundled_runtime_dir():
        bundled = os.path.join(cand_dir, binary)
        if os.path.isfile(bundled):
            # Na Windowsie os.X_OK zwraca True dla każdego pliku, więc warunek
            # uproszczony do isfile (executable bit i tak nieprzenośny w NTFS).
            logger.info("yt-dlp JS runtime: deno (bundled) → %s", bundled)
            return {"js_runtimes": {"deno": {"path": bundled}}, "remote_components": {"ejs:github"}}

    # 2) PATH fallback
    for rt, bin_name in [("deno", "deno"), ("node", "node"), ("bun", "bun"), ("quickjs", "qjs")]:
        path = shutil.which(bin_name)
        if path:
            logger.info("yt-dlp JS runtime: %s (PATH) → %s", rt, path)
            return {"js_runtimes": {rt: {"path": path}}, "remote_components": {"ejs:github"}}

    logger.warning("yt-dlp: BRAK JS runtime — YouTube nsig nie zadziała, ryzyko HTTP 403")
    return {}



_AUDIO_EXTS = {".m4a", ".aac", ".opus", ".ogg", ".flac", ".mp3"}


def _cookies_opt(browser: str = "", file: str = "") -> dict:
    """yt-dlp opts dla źródła ciasteczek.

    Priorytet: jawny plik ``.txt`` (jeśli istnieje na dysku) → ``cookiefile``.
    Inaczej nazwa przeglądarki → ``cookiesfrombrowser`` (czyta natywną bazę
    SQLite, co czasem zawodzi gdy przeglądarka trzyma plik zalockowany —
    patrz ``_classify_cookie_error``).
    """
    f = (file or "").strip()
    if f and os.path.isfile(f):
        return {"cookiefile": f}
    b = browser.strip() or (os.environ.get("WP_DOWNLOADER_COOKIES_BROWSER") or "").strip()
    return {"cookiesfrombrowser": (b,)} if b else {}


def _classify_cookie_error(msg: str) -> str | None:
    """Wykryj typowy błąd kopiowania bazy ciasteczek z przeglądarki i zwróć
    przyjazny polski komunikat z instrukcją. Trafia w przypadek Windows + otwarty
    Chrome (``PermissionError: ...Cookies``) oraz yt-dlp 'Could not copy Chrome
    cookie database' bez względu na platformę.
    """
    lower = (msg or "").lower()
    if (("could not copy" in lower and "cookie" in lower)
            or ("permissionerror" in lower and "cookies" in lower)):
        return ("Nie można uzyskać dostępu do ciasteczek przeglądarki — "
                "plik jest zablokowany przez działającą instancję przeglądarki. "
                "Zamknij Chrome (lub wybraną przeglądarkę) i spróbuj ponownie, "
                "albo w Ustawieniach wskaż plik cookies.txt jako źródło ciasteczek.")
    return None


def _format_bytes(n: int) -> str:
    if n >= 1_000_000_000: return f"{n / 1_000_000_000:.1f} GB"
    if n >= 1_000_000: return f"{n / 1_000_000:.1f} MB"
    if n >= 1_000: return f"{n / 1_000:.1f} KB"
    return f"{n} B"


def _is_facebook_url(url: str) -> bool:
    u = url.lower()
    return any(x in u for x in ("facebook.com/", "fb.com/", "fb.watch/"))


def _is_audio_stream(d: dict) -> bool:
    """Use yt-dlp info_dict vcodec to distinguish audio-only streams."""
    info = d.get("info_dict") or {}
    vcodec = (info.get("vcodec") or "").strip().lower()
    return not vcodec or vcodec == "none"


class YtDlpWorker:
    def __init__(self, manager: DownloadManager):
        self.manager = manager
        self._stop_events: dict[str, threading.Event] = {}
        self._graceful_kill_ids: set[str] = set()
        manager.set_worker(self)

    # ── Public API ────────────────────────────────────────────────────────────

    async def dispatch(self, task: DownloadTask) -> None:
        asyncio.create_task(self._run_task(task))

    def kill_task(self, task_id: str) -> None:
        ev = self._stop_events.get(task_id)
        if ev:
            ev.set()

    def graceful_stop(self, task_id: str) -> None:
        """Stop download immediately but flag it for post-stop merge."""
        self._graceful_kill_ids.add(task_id)
        self.kill_task(task_id)

    def stop(self) -> None:
        for ev in self._stop_events.values():
            ev.set()
        self._stop_events.clear()
        self._graceful_kill_ids.clear()

    # ── Internal ──────────────────────────────────────────────────────────────

    async def _run_task(self, task: DownloadTask) -> None:
        try:
            if task.task_id not in self.manager.tasks:
                return
            await self._process_task(task)
        except Exception as e:
            logger.exception(f"Krytyczny błąd zadania {task.task_id}: {e}")
            if task.task_id in self.manager.tasks:
                await self.manager.update_task(task.task_id, status="error", error_msg=str(e))
        finally:
            self._stop_events.pop(task.task_id, None)
            self._graceful_kill_ids.discard(task.task_id)
            self.manager.clear_cancel(task.task_id)
            self.manager.clear_graceful_stop(task.task_id)

    async def _process_task(self, task: DownloadTask) -> None:
        if task.task_id not in self.manager.tasks:
            return

        await self.manager.update_task(task.task_id, status="downloading", progress=0)

        title, thumbnail_url = await self._get_info(task.url)
        if title and task.task_id in self.manager.tasks:
            await self.manager.update_task(task.task_id, title=title, thumbnail_url=thumbnail_url)

        if task.live_record and task.wait_for_video:
            await self.manager.update_task(
                task.task_id,
                live_status="Czekam na rozpoczęcie streamu…")

        if self.manager.is_cancel_requested(task.task_id):
            await self._finish_cancelled(task.task_id)
            return

        logger.info(f"Start pobierania: {task.url} (ID: {task.task_id})")

        loop = asyncio.get_running_loop()
        stop_event = threading.Event()
        self._stop_events[task.task_id] = stop_event
        cancelled_gracefully = threading.Event()

        # Facebook live streams → try streamlink first, fall back to yt-dlp if needed
        if task.live_record and _is_facebook_url(task.url):
            handled = await self._run_streamlink_task(task, stop_event, cancelled_gracefully, loop)
            if handled:
                return
            logger.info(f"streamlink fallback → yt-dlp dla {task.url}")

        tracked_video: list[str] = []  # video-stream intermediate paths
        tracked_audio: list[str] = []  # audio-stream intermediate paths
        live_parts: dict[str, str] = {}

        def progress_hook(d: dict) -> None:
            raw_fname = d.get("filename") or ""
            fname = raw_fname.removesuffix(".part")

            # Classify stream using vcodec from info_dict (reliable, extension-independent)
            audio = _is_audio_stream(d)
            if fname:
                target = tracked_audio if audio else tracked_video
                if fname not in target:
                    target.append(fname)

            # Graceful stop must be checked BEFORE stop_event
            # (graceful_stop() sets both _graceful_kill_ids AND stop_event simultaneously)
            is_graceful = (task.task_id in self._graceful_kill_ids or
                           (task.live_record and self.manager.is_graceful_stop_requested(task.task_id)))
            if is_graceful:
                self._graceful_kill_ids.discard(task.task_id)
                self.manager.clear_graceful_stop(task.task_id)
                cancelled_gracefully.set()
                raise yt_dlp.utils.DownloadCancelled("graceful stop")

            if stop_event.is_set() or self.manager.is_cancel_requested(task.task_id):
                raise yt_dlp.utils.DownloadCancelled("cancelled by user")

            if d["status"] != "downloading":
                return

            update_kwargs: dict[str, Any] = {}
            stream_label = "Audio" if audio else "Video"

            fi = d.get("fragment_index")
            fc = d.get("fragment_count")
            if fi is not None:
                if fc:
                    update_kwargs["progress"] = min(99.0, fi / fc * 100)
                else:
                    update_kwargs["progress"] = min(95.0, fi * 0.5)
                frag_text = f"frag {fi}/{fc if fc else '?'}"
                dl_bytes = d.get("downloaded_bytes")
                if dl_bytes:
                    frag_text += f" · {_format_bytes(dl_bytes)}"
                live_parts[stream_label] = frag_text
                v, a = live_parts.get("Video", ""), live_parts.get("Audio", "")
                update_kwargs["live_status"] = (f"V: {v} | A: {a}") if (v and a) else (v or a)
            else:
                dl = d.get("downloaded_bytes", 0)
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                if total:
                    update_kwargs["progress"] = min(99.0, dl / total * 100)
                dl_bytes = d.get("downloaded_bytes")
                if dl_bytes and task.live_record:
                    live_parts[stream_label] = _format_bytes(dl_bytes)
                    v, a = live_parts.get("Video", ""), live_parts.get("Audio", "")
                    update_kwargs["live_status"] = (f"V: {v} | A: {a}") if (v and a) else (v or a)
                # Speed / ETA for regular downloads
                spd = d.get("speed")
                eta = d.get("eta")
                if spd:
                    spd_str = _format_bytes(spd) + "/s"
                    if eta and not task.live_record:
                        m, s = divmod(int(eta), 60)
                        spd_str += f" · {m}:{s:02d}"
                    update_kwargs["speed_str"] = spd_str
                elif not task.live_record:
                    update_kwargs["speed_str"] = ""

            if update_kwargs:
                asyncio.run_coroutine_threadsafe(
                    self.manager.update_task(task.task_id, **update_kwargs),
                    loop,
                )

        ydl_opts = self._build_ydl_opts(task)
        ydl_opts["progress_hooks"] = [progress_hook]

        error_holder: list[str] = []
        _ydl_log_lines: list[str] = []

        _DIAG_KW = ('deno', 'jsc', 'ejs', 'solver', 'challenge', 'runtime', 'js runtime')

        def _ydl_log(msg: str) -> None:
            import re as _re
            clean = _re.sub(r'\x1b\[[0-9;]*[mGKH]', '', msg)
            _ydl_log_lines.append(clean)
            if any(kw in clean.lower() for kw in _DIAG_KW):
                logger.info("[yt-dlp] %s", clean)
            else:
                logger.debug("[yt-dlp] %s", clean)

        class _YdlLogger:
            def debug(self, msg: str) -> None: _ydl_log(msg)  # noqa: E704
            def info(self, msg: str) -> None: _ydl_log(msg)  # noqa: E704
            def warning(self, msg: str) -> None:
                logger.warning("[yt-dlp] %s", msg); _ydl_log(msg)
            def error(self, msg: str) -> None:
                logger.error("[yt-dlp] %s", msg); _ydl_log(msg)

        ydl_opts["logger"] = _YdlLogger()
        ydl_opts["quiet"] = False
        ydl_opts["no_warnings"] = False
        ydl_opts["verbose"] = True

        def run_download() -> None:
            try:
                with YoutubeDL(ydl_opts) as ydl:
                    ydl.download([task.url])
            except yt_dlp.utils.DownloadCancelled:
                pass
            except Exception as e:
                error_holder.append(str(e))
                logger.error("[yt-dlp] Download exception for %s: %s", task.url, e)

        await loop.run_in_executor(None, run_download)

        if task.task_id not in self.manager.tasks:
            return

        if cancelled_gracefully.is_set():
            logger.info(f"Zadanie {task.task_id} zatrzymane — scalanie do MP4…")
            merged = await self._merge_after_stop(
                tracked_video, tracked_audio, task.output_path, task.task_id
            )
            if merged:
                await self.manager.update_task(task.task_id, status="done", progress=100.0,
                                               live_status="")
            else:
                await self.manager.update_task(task.task_id, status="done", progress=100.0,
                                               live_status="",
                                               error_msg="Uwaga: scalenie do MP4 nie powiodło się.")
        elif self.manager.is_cancel_requested(task.task_id):
            await self._finish_cancelled(task.task_id)
        elif error_holder:
            raw_err = error_holder[0]
            import re as _re
            friendly = _classify_cookie_error(raw_err)
            if friendly:
                clean_err = friendly
            else:
                clean_err = _re.sub(r'\x1b\[[0-9;]*[mGKH]', '', raw_err)
                clean_err = _re.sub(r'^ERROR:\s*\[[\w:]+\]\s*[\w]+:\s*', '', clean_err).strip()
            logger.error("yt-dlp błąd dla zadania %s: %s", task.task_id, clean_err)
            diag = [l for l in _ydl_log_lines if any(kw in l.lower() for kw in _DIAG_KW)]
            if diag:
                logger.warning("yt-dlp diagnostics:\n%s", "\n".join(diag[-40:]))
            await self.manager.update_task(
                task.task_id, status="error",
                error_msg=clean_err or "Błąd pobierania — sprawdź URL.",
            )
        else:
            engine = "live" if task.live_record else "yt-dlp"
            # macOS QuickTime can't play VP9/AV1 in fMP4 — re-encode to H.264 if needed.
            # Skip audio-only and live (already handled in _merge_after_stop).
            if not task.live_record and task.output_path:
                fid_lc = (task.format_id or "").lower()
                is_audio_only = fid_lc in ("mp3", "m4a")
                if not is_audio_only:
                    await self._ensure_h264(task.output_path, task.task_id)
            logger.info(f"Zadanie {task.task_id} ukończone ({engine}).")
            await self.manager.update_task(task.task_id, status="done", progress=100.0,
                                           live_status="", speed_str="")

    async def _ensure_h264(self, output_path: str, task_id: str) -> None:
        """If output video isn't H.264, re-encode it (Instagram/Twitter often serve VP9 in mp4)."""
        if not output_path or not os.path.exists(output_path):
            return
        ext = os.path.splitext(output_path)[1].lower()
        if ext not in (".mp4", ".mkv", ".mov", ".m4v"):
            return
        ffprobe = shutil.which("ffprobe") or "ffprobe"
        try:
            proc = await asyncio.create_subprocess_exec(
                ffprobe, "-v", "error", "-select_streams", "v:0",
                "-show_entries", "stream=codec_name", "-of", "csv=p=0", output_path,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
            codec = stdout.decode(errors="replace").strip().lower()
        except Exception as e:
            logger.warning(f"ffprobe failed for {output_path}: {e}")
            return
        if not codec or codec in ("h264", "avc1"):
            return
        logger.info(f"Re-encoding {output_path} from {codec} to H.264…")
        await self.manager.update_task(task_id, status="merging",
                                       live_status=f"Konwertowanie z {codec} do H.264…")
        ffmpeg = shutil.which("ffmpeg") or "ffmpeg"
        temp_out = output_path + ".h264.tmp.mp4"
        cmd = [
            ffmpeg, "-y", "-i", output_path,
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-c:a", "aac", "-b:a", "192k",
            "-movflags", "+faststart",
            temp_out,
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
            )
            _, stderr_data = await asyncio.wait_for(proc.communicate(), timeout=1800)
            if proc.returncode == 0 and os.path.exists(temp_out):
                os.replace(temp_out, output_path)
                logger.info(f"Re-encode OK: {output_path}")
            else:
                err = stderr_data.decode(errors="replace").strip() if stderr_data else ""
                logger.warning(f"Re-encode FAILED for {output_path}: {err[-400:]}")
                if os.path.exists(temp_out):
                    try: os.remove(temp_out)
                    except OSError: pass
        except Exception as e:
            logger.error(f"Re-encode exception for {output_path}: {e}")
            if os.path.exists(temp_out):
                try: os.remove(temp_out)
                except OSError: pass

    def _build_ydl_opts(self, task: DownloadTask) -> dict:
        opts: dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "nocheckcertificate": True,
            "no_playlist": True,
        }
        url_lc = (task.url or "").lower()
        is_youtube_live = (task.live_record and
                           ("youtube.com" in url_lc or "youtu.be" in url_lc))
        # YouTube live: do NOT pass cookies. Verified empirically that with
        # --cookies-from-browser, yt-dlp falls back to the `tv_downgraded`
        # client which cannot satisfy --live-from-start and returns
        # "No video formats found!". Without cookies it uses `android_vr`,
        # which returns the DASH MPD with format 137+140 (AVC1 1080p video +
        # m4a audio) and records from the start of the broadcast — exactly
        # the CLI command from the user's tutorial.
        if not is_youtube_live:
            opts.update(_cookies_opt(
                getattr(task, "cookies_browser", ""),
                getattr(task, "cookies_file", ""),
            ))
        # Regular downloads (POBIERANIE) keep the Deno / EJS runtime that the
        # YouTube n-challenge solver needs.
        if not task.live_record:
            opts.update(_detect_js_runtime())

        if task.live_record:
            opts["live_from_start"] = True
            opts["no_part"] = True
            if task.wait_for_video:
                # Poll the URL until the scheduled stream goes live; same shape
                # as yt-dlp's "--wait-for-video 10" CLI flag (min, max) in s.
                opts["wait_for_video"] = (10, None)

        fid = (task.format_id or "").lower()
        res_match = re.search(r"(\d+)p", fid)
        res_height = res_match.group(1) if res_match else None

        # "1080-mp4", "2160-mkv", "720-mov" — explicit resolution + container picker
        m_res = re.match(r"^(\d+)-(mp4|mkv|mov)$", fid)
        if m_res:
            h = int(m_res.group(1))
            container = m_res.group(2)
            opts["format"] = (
                f"bv*[vcodec^=avc1][height<={h}]+ba[ext=m4a]/"
                f"bv*[vcodec^=avc1][height<={h}]+ba/"
                f"bestvideo[height<={h}]+bestaudio/"
                f"best[height<={h}]/best"
            )
            opts["merge_output_format"] = container
            if task.output_path:
                opts["outtmpl"] = task.output_path
            else:
                out_dir = self._resolve_dir(task)
                opts["outtmpl"] = os.path.join(out_dir, "%(title)s.%(ext)s")
            return opts

        if task.live_record and fid in ("best", "", "best-mp4"):
            # Selector verified by the user CLI against active streams:
            # yt-dlp -f "bestvideo[vcodec^=avc1]+bestaudio[ext=m4a]/best" --live-from-start
            opts["format"] = "bestvideo[vcodec^=avc1]+bestaudio[ext=m4a]/best"
            opts["merge_output_format"] = "mp4"
        elif fid == "best-mp4":
            # Always prefer H.264 — VP9/AV1 in fMP4 can't be opened by QuickTime after -c copy merge
            opts["format"] = "bv*[vcodec^=avc1]+ba[ext=m4a]/bv*[vcodec^=avc1]+ba/bestvideo+bestaudio/best"
            opts["merge_output_format"] = "mp4"
        elif fid == "best-mkv":
            opts["format"] = "bestvideo+bestaudio/best"
            opts["merge_output_format"] = "mkv"
        elif fid == "best-mov":
            opts["format"] = "bestvideo+bestaudio/best"
            opts["merge_output_format"] = "mov"
        elif fid == "mp3":
            opts["format"] = "bestaudio/best"
            opts["postprocessors"] = [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "0"}]
        elif fid == "m4a":
            opts["format"] = "bestaudio/best"
            opts["postprocessors"] = [{"key": "FFmpegExtractAudio", "preferredcodec": "m4a", "preferredquality": "0"}]
        elif fid.startswith("mp4"):
            if res_height:
                opts["format"] = f"bestvideo[height<={res_height}]+bestaudio/best[height<={res_height}]/best"
            else:
                opts["format"] = "bestvideo+bestaudio/best"
            opts["merge_output_format"] = "mp4"
        elif fid.startswith("webm"):
            if res_height:
                opts["format"] = f"bestvideo[ext=webm][height<={res_height}]+bestaudio[ext=webm]/best[ext=webm][height<={res_height}]/best"
            else:
                opts["format"] = "bestvideo[ext=webm]+bestaudio[ext=webm]/best"
        elif task.format_id and task.format_id not in ("best", ""):
            opts["format"] = task.format_id
            if task.live_record:
                opts["merge_output_format"] = "mp4"
        else:
            opts["format"] = "bestvideo+bestaudio/best"
            opts["merge_output_format"] = "mp4"

        if task.output_path:
            opts["outtmpl"] = task.output_path
        else:
            out_dir = self._resolve_dir(task)
            opts["outtmpl"] = os.path.join(out_dir, "%(title)s.%(ext)s")

        return opts

    async def _merge_after_stop(
        self,
        tracked_video: list[str],
        tracked_audio: list[str],
        output_path: str,
        task_id: str = "",
    ) -> bool:
        """Locate yt-dlp intermediate files after graceful stop and merge with FFmpeg."""
        if task_id:
            await self.manager.update_task(task_id, status="merging", live_status="Scalanie pliku…")

        def find_file(bases: list[str]) -> str | None:
            """Return the first existing non-empty file from tracked base paths."""
            for base in bases:
                for cand in [base, base + ".part"]:
                    if os.path.exists(cand) and os.path.getsize(cand) > 0:
                        return cand
            return None

        video_file = find_file(tracked_video)
        audio_file = find_file(tracked_audio)

        # Fallback: scan the output directory for intermediate files we may have missed.
        # yt-dlp names them "<base>.f<id>.<ext>" or "<base>.f<id>.<ext>.part".
        # Exclude: .ytdl state files, individual Frag temp files, zero-size files.
        if not video_file or not audio_file:
            base = os.path.splitext(output_path)[0]
            candidates = [
                f for f in glob.glob(base + ".f*.*")
                if not f.endswith(".ytdl")
                and not re.search(r"-Frag\d+\.part$", f)
                and os.path.exists(f)
                and os.path.getsize(f) > 0
            ]
            for cand in sorted(candidates, key=os.path.getsize, reverse=True):
                if cand in (video_file, audio_file, output_path):
                    continue
                clean_ext = os.path.splitext(cand.removesuffix(".part"))[1].lower()
                if not audio_file and clean_ext in _AUDIO_EXTS:
                    audio_file = cand
                    logger.info(f"Fallback: znaleziono audio: {cand}")
                elif not video_file and clean_ext not in _AUDIO_EXTS:
                    video_file = cand
                    logger.info(f"Fallback: znaleziono video: {cand}")

        logger.info(f"Scalanie — video: {video_file}, audio: {audio_file}")

        if not video_file and not audio_file:
            logger.warning("Graceful stop: nie znaleziono pliku tymczasowego")
            return False

        # Single file already at the output path (e.g. pre-muxed HLS, no_part=True)
        if (video_file or audio_file) == output_path and not (video_file and audio_file):
            logger.info("Graceful stop: plik już pod docelową ścieżką")
            return True

        ffmpeg = shutil.which("ffmpeg") or "ffmpeg"
        fflags = ["-fflags", "+genpts+discardcorrupt"]
        oflags = ["-movflags", "+faststart"]

        if video_file and audio_file:
            cmd = [ffmpeg, "-y", *fflags,
                   "-i", video_file, "-i", audio_file,
                   "-c", "copy", *oflags, output_path]
            inputs_used = [video_file, audio_file]
        elif video_file:
            if video_file == output_path:
                return True
            cmd = [ffmpeg, "-y", *fflags,
                   "-i", video_file,
                   "-c", "copy", *oflags, output_path]
            inputs_used = [video_file]
        else:
            # audio-only fallback
            if audio_file == output_path:
                return True
            cmd = [ffmpeg, "-y", *fflags,
                   "-i", audio_file,
                   "-c", "copy", *oflags, output_path]
            inputs_used = [audio_file]

        logger.info(f"FFmpeg merge: {' '.join(cmd)}")
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr_data = await asyncio.wait_for(proc.communicate(), timeout=600)
            success = proc.returncode == 0
            if not success and stderr_data:
                logger.error(f"FFmpeg stderr: {stderr_data.decode(errors='replace').strip()}")
            if success:
                # Delete only files that were FFmpeg inputs
                for f in inputs_used:
                    if f != output_path:
                        try:
                            os.remove(f)
                        except OSError:
                            pass
                # Clean up yt-dlp state files and leftover incomplete fragments
                base = os.path.splitext(output_path)[0]
                for leftover in (glob.glob(base + ".f*.ytdl") +
                                 glob.glob(base + ".f*.*-Frag*.part")):
                    try:
                        os.remove(leftover)
                    except OSError:
                        pass
            logger.info(f"FFmpeg merge {'OK' if success else 'FAILED'} dla {output_path}")
            return success
        except Exception as e:
            logger.error(f"Błąd FFmpeg merge: {e}")
            return False

    async def _resolve_url_redirect(self, url: str, loop) -> str:
        """Follow HTTP redirects to get canonical URL (helps with facebook.com/share/v/ links)."""
        if "/share/" not in url.lower():
            return url
        import urllib.request
        def _follow() -> str:
            try:
                req = urllib.request.Request(
                    url,
                    headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
                )
                with urllib.request.urlopen(req, timeout=10) as resp:
                    return resp.url
            except Exception:
                return url
        try:
            resolved = await asyncio.wait_for(loop.run_in_executor(None, _follow), timeout=15)
            return resolved or url
        except Exception:
            return url

    async def _run_streamlink_task(
        self,
        task: DownloadTask,
        stop_event: threading.Event,
        cancelled_gracefully: threading.Event,
        loop,
    ) -> bool:
        """Download a live stream using streamlink. Returns True if handled, False to fall back to yt-dlp."""
        try:
            from streamlink import Streamlink
        except ImportError:
            logger.warning("streamlink nie jest zainstalowany — fallback do yt-dlp")
            return False

        await self.manager.update_task(task.task_id, live_status="streamlink · szukanie strumieni…")

        session = Streamlink()

        # Facebook /share/v/ URLs are not directly supported by streamlink.
        # Follow HTTP redirects to get the canonical video page URL first.
        stream_url = await self._resolve_url_redirect(task.url, loop)
        if stream_url != task.url:
            logger.info(f"streamlink: URL resolved: {task.url} → {stream_url}")

        try:
            logger.info(f"streamlink: szukanie strumieni dla {stream_url}")
            streams = await asyncio.wait_for(
                loop.run_in_executor(None, lambda: session.streams(stream_url)),
                timeout=30,
            )
        except asyncio.TimeoutError:
            logger.warning(f"streamlink: timeout (30s) dla {task.url} — fallback do yt-dlp")
            await self.manager.update_task(task.task_id, live_status="")
            return False
        except Exception as e:
            logger.warning(f"streamlink: {type(e).__name__}: {e} — fallback do yt-dlp")
            await self.manager.update_task(task.task_id, live_status="")
            return False

        if not streams:
            logger.warning(f"streamlink: brak strumieni dla {stream_url} — fallback do yt-dlp")
            await self.manager.update_task(task.task_id, live_status="")
            return False

        logger.info(f"streamlink: znaleziono strumienie: {list(streams.keys())}")

        stream = None
        for quality in ("best", "720p", "480p", "360p", "worst"):
            stream = streams.get(quality)
            if stream:
                break
        if stream is None:
            stream = next(iter(streams.values()))

        # Write raw stream (typically MPEG-TS) to a temp file, then remux to MP4
        ts_path = os.path.splitext(task.output_path)[0] + ".streamlink_temp.ts"

        def _write_stream() -> None:
            bytes_written = 0
            fd = stream.open()
            try:
                with open(ts_path, "wb") as out:
                    while True:
                        is_graceful = (
                            task.task_id in self._graceful_kill_ids
                            or (task.live_record and self.manager.is_graceful_stop_requested(task.task_id))
                        )
                        if is_graceful:
                            self._graceful_kill_ids.discard(task.task_id)
                            self.manager.clear_graceful_stop(task.task_id)
                            cancelled_gracefully.set()
                            break
                        if stop_event.is_set() or self.manager.is_cancel_requested(task.task_id):
                            break
                        chunk = fd.read(8192)
                        if not chunk:
                            break
                        out.write(chunk)
                        bytes_written += len(chunk)
                        asyncio.run_coroutine_threadsafe(
                            self.manager.update_task(
                                task.task_id,
                                live_status=f"streamlink · {_format_bytes(bytes_written)}",
                            ),
                            loop,
                        )
            finally:
                fd.close()

        await loop.run_in_executor(None, _write_stream)

        if task.task_id not in self.manager.tasks:
            return True

        if self.manager.is_cancel_requested(task.task_id) and not cancelled_gracefully.is_set():
            try:
                os.remove(ts_path)
            except OSError:
                pass
            await self._finish_cancelled(task.task_id)
            return True

        # Graceful stop OR stream ended naturally → remux TS → MP4
        merged = await self._convert_ts_to_mp4(ts_path, task.output_path, task.task_id)
        try:
            os.remove(ts_path)
        except OSError:
            pass
        if merged:
            await self.manager.update_task(task.task_id, status="done", progress=100.0, live_status="")
        else:
            await self.manager.update_task(
                task.task_id, status="done", progress=100.0, live_status="",
                error_msg="Uwaga: remux TS→MP4 nie powiódł się."
            )
        return True

    async def _convert_ts_to_mp4(self, ts_path: str, mp4_path: str, task_id: str = "") -> bool:
        """Remux MPEG-TS to MP4 using FFmpeg -c copy."""
        if task_id:
            await self.manager.update_task(task_id, status="merging", live_status="Konwertowanie do MP4…")
        ffmpeg = shutil.which("ffmpeg") or "ffmpeg"
        cmd = [ffmpeg, "-y", "-i", ts_path, "-c", "copy", "-movflags", "+faststart", mp4_path]
        logger.info(f"FFmpeg TS→MP4: {' '.join(cmd)}")
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr_data = await asyncio.wait_for(proc.communicate(), timeout=600)
            success = proc.returncode == 0
            if not success and stderr_data:
                logger.error(f"FFmpeg TS→MP4 stderr: {stderr_data.decode(errors='replace').strip()}")
            logger.info(f"FFmpeg TS→MP4 {'OK' if success else 'FAILED'} → {mp4_path}")
            return success
        except Exception as e:
            logger.error(f"Błąd FFmpeg TS→MP4: {e}")
            return False

    def _resolve_dir(self, task: DownloadTask) -> str:
        candidates = []
        if task.output_path:
            candidates.append(os.path.dirname(task.output_path))
        candidates.extend([getattr(self.manager, "output_dir", ""), DOWNLOAD_DIR])
        for c in candidates:
            c = (c or "").strip()
            if c:
                os.makedirs(c, exist_ok=True)
                return c
        os.makedirs(DOWNLOAD_DIR, exist_ok=True)
        return DOWNLOAD_DIR

    async def _finish_cancelled(self, task_id: str) -> None:
        self.manager.clear_cancel(task_id)
        if task_id in self.manager.tasks:
            await self.manager.update_task(task_id, status="cancelled", error_msg="Przerwano przez użytkownika.")

    async def _get_info(self, url: str) -> tuple[str, str]:
        loop = asyncio.get_running_loop()

        def _fetch() -> tuple[str, str]:
            try:
                opts = {
                    "quiet": True, "no_warnings": True, "noprogress": True,
                    "nocheckcertificate": True, "no_playlist": True,
                    **_detect_js_runtime(), **_cookies_opt(),
                }
                with YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(url, download=False)
                    title = (info or {}).get("title", "")
                    thumb = (info or {}).get("thumbnail", "")
                    if thumb and not thumb.startswith("https://"):
                        thumb = ""
                    return title, thumb
            except Exception:
                return "", ""

        try:
            return await asyncio.wait_for(loop.run_in_executor(None, _fetch), timeout=15)
        except (asyncio.TimeoutError, Exception):
            return "", ""
