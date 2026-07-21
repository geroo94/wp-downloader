import asyncio
import time
import uuid
from typing import Any, Dict, Set
from dataclasses import dataclass, field


@dataclass
class DownloadTask:
    task_id: str
    url: str
    format_id: str
    quality: str
    title: str = ""
    status: str = "downloading"  # downloading / done / error / cancelled
    progress: float = 0.0
    error_msg: str = ""
    filepath: str = ""
    output_path: str = ""
    live_record: bool = False
    wait_for_video: bool = False  # --wait-for-video for scheduled YT streams
    thumbnail_url: str = ""
    downloaded_size: str = ""
    live_status: str = ""
    transcription: str = ""  # "" = not started, "in_progress", path = done, "error"
    transcription_progress: float = 0.0  # 0-100, tylko gdy transcription == "in_progress"
    cookies_browser: str = ""  # "chrome" / "firefox" / "safari" / "edge" / ""
    cookies_file: str = ""  # ścieżka do wyeksportowanego cookies.txt (priorytet nad browser)
    speed_str: str = ""
    # "download" (yt-dlp/live) | "render" (Fast Cutter ffmpeg render).
    # renderTasks() w JS wybiera layout karty na podstawie tego pola.
    job_type: str = "download"
    # Znacznik czasu utworzenia zadania (epoch s) — datownik na kafelku
    # w Procesach (JS formatuje jako "12.07.2026 00:34").
    created_at: float = field(default_factory=time.time)


class DownloadManager:
    def __init__(self):
        self.tasks: Dict[str, DownloadTask] = {}
        self.listeners = []
        self.output_dir: str = ""
        self._cancel_ids: Set[str] = set()
        self._graceful_stop_ids: Set[str] = set()
        self._worker: Any = None  # ustawiony przez YtDlpWorker

    def setup(self):
        pass  # zachowane dla kompatybilności; kolejka nie jest już używana

    def set_worker(self, worker: Any) -> None:
        self._worker = worker

    def set_output_dir(self, path: str) -> None:
        self.output_dir = (path or "").strip()

    def is_cancel_requested(self, task_id: str) -> bool:
        return task_id in self._cancel_ids

    def clear_cancel(self, task_id: str) -> None:
        self._cancel_ids.discard(task_id)

    def request_graceful_stop(self, task_id: str) -> None:
        self._graceful_stop_ids.add(task_id)

    def graceful_stop_task(self, task_id: str) -> None:
        """Set graceful flag AND immediately kill download for instant UI response."""
        self._graceful_stop_ids.add(task_id)  # fallback check in hook
        if self._worker:
            self._worker.graceful_stop(task_id)  # sets _graceful_kill_ids + stop_event

    def is_graceful_stop_requested(self, task_id: str) -> bool:
        return task_id in self._graceful_stop_ids

    def clear_graceful_stop(self, task_id: str) -> None:
        self._graceful_stop_ids.discard(task_id)

    async def remove_task(self, task_id: str) -> bool:
        task = self.tasks.get(task_id)
        if not task or task.status == "downloading":
            return False
        del self.tasks[task_id]
        self._cancel_ids.discard(task_id)
        await self.broadcast({"type": "task_removed", "task_id": task_id})
        return True

    async def cancel_task(self, task_id: str) -> bool:
        task = self.tasks.get(task_id)
        if not task:
            return False
        if task.status == "downloading":
            self._cancel_ids.add(task_id)
            # Powiadamiamy workera żeby natychmiast zabił proces
            if self._worker:
                self._worker.kill_task(task_id)
            return True
        return await self.remove_task(task_id)

    def add_task(self, url: str, format_id: str, quality: str, output_path: str = "", live_record: bool = False, cookies_browser: str = "", cookies_file: str = "", wait_for_video: bool = False) -> str:
        task_id = str(uuid.uuid4())[:8]
        task = DownloadTask(
            task_id=task_id,
            url=url,
            format_id=format_id,
            quality=quality,
            output_path=(output_path or "").strip(),
            live_record=live_record,
            cookies_browser=cookies_browser,
            cookies_file=cookies_file,
            wait_for_video=wait_for_video,
        )
        self.tasks[task_id] = task
        if self._worker:
            asyncio.create_task(self._worker.dispatch(task))
        return task_id

    def add_render_task(self, title: str, output_path: str) -> str:
        """Rejestruje task typu 'render' (Fast Cutter) w store.

        Odróżnia się od add_task: nie dispatch'uje do yt-dlp worker'a — CutterManager
        prowadzi ffmpeg sam. Manager pełni tylko rolę store'u + WebSocket broadcast."""
        task_id = str(uuid.uuid4())[:8]
        task = DownloadTask(
            task_id=task_id,
            url="",
            format_id="",
            quality="",
            title=title,
            status="rendering",
            output_path=(output_path or "").strip(),
            job_type="render",
        )
        self.tasks[task_id] = task
        return task_id

    def add_transcribe_task(self, title: str, input_path: str) -> str:
        """Rejestruje task typu 'transcribe_standalone' (zakładka Transkrypcja,
        plik z dysku — nie z Historii). Progress leci przez ten sam
        WebSocket task_update co reszta, ale renderTasks() w JS filtruje ten
        job_type z listy Historii — karta żyje tylko wewnątrz zakładki."""
        task_id = str(uuid.uuid4())[:8]
        task = DownloadTask(
            task_id=task_id,
            url="",
            format_id="",
            quality="",
            title=title,
            status="done",
            output_path=(input_path or "").strip(),
            job_type="transcribe_standalone",
        )
        self.tasks[task_id] = task
        return task_id

    def get_all_tasks(self) -> list:
        return [
            {
                "task_id": t.task_id,
                "url": t.url,
                "title": t.title,
                "format_id": t.format_id,
                "status": t.status,
                "progress": round(t.progress, 1),
                "error_msg": t.error_msg,
                "output_path": t.output_path,
                "live_record": t.live_record,
                "thumbnail_url": t.thumbnail_url,
            "downloaded_size": t.downloaded_size,
            "live_status": t.live_status,
            "transcription": t.transcription,
            "transcription_progress": round(t.transcription_progress, 1),
            "speed_str": t.speed_str,
            "job_type": t.job_type,
            "created_at": t.created_at,
            }
            for t in self.tasks.values()
        ]

    async def broadcast(self, message: dict):
        import json
        dead = []
        for ws in self.listeners:
            try:
                await ws.send_text(json.dumps(message))
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.listeners.remove(ws)

    async def update_task(self, task_id: str, **kwargs):
        task = self.tasks.get(task_id)
        if not task:
            return
        for key, value in kwargs.items():
            setattr(task, key, value)
        await self.broadcast({
            "type": "task_update",
            "task_id": task_id,
            "status": task.status,
            "progress": round(task.progress, 1),
            "title": task.title,
            "error_msg": task.error_msg,
            "output_path": task.output_path,
            "live_record": task.live_record,
            "thumbnail_url": task.thumbnail_url,
            "downloaded_size": task.downloaded_size,
            "live_status": task.live_status,
            "transcription": task.transcription,
            "transcription_progress": round(task.transcription_progress, 1),
            "speed_str": task.speed_str,
            "job_type": task.job_type,
            "created_at": task.created_at,
        })
