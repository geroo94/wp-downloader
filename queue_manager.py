"""
QueueManager — logika kolejki powiązana z frontendem (usuwanie / anulowanie), jak w dokumentacji.
"""

from __future__ import annotations

from download_manager import DownloadManager


class QueueManager:
    """Cienka warstwa nad DownloadManager dla operacji na kolejce z UI."""

    def __init__(self, manager: DownloadManager) -> None:
        self._m = manager

    async def remove_queued(self, task_id: str) -> bool:
        """Usuwa zadanie oczekujące (nie rozpoczęte). Zwraca True jeśli usunięto."""
        return await self._m.remove_task_if_queued(task_id)

    async def cancel(self, task_id: str) -> bool:
        """Kolejka → usuń; pobieranie → anuluj subprocess w workerze."""
        return await self._m.cancel_task(task_id)
