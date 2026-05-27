"""
TerminalHandler — asynchroniczna komunikacja ze strumieniem stdout procesu (jak w dokumentacji).
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
from collections.abc import AsyncIterator, Callable, Coroutine
from typing import Any


class TerminalHandler:
    """Uruchamia proces i udostępnia asynchroniczny iterator linii stdout."""

    def __init__(self) -> None:
        self.process: asyncio.subprocess.Process | None = None

    async def start_exec(
        self,
        *cmd: str,
        merge_stderr: bool = True,
    ) -> asyncio.subprocess.Process:
        stderr = asyncio.subprocess.STDOUT if merge_stderr else asyncio.subprocess.DEVNULL
        
        # Flaga ukrywająca okno konsoli na Windowsie
        creationflags = 0
        if sys.platform == "win32":
            creationflags = subprocess.CREATE_NO_WINDOW
            
        self.process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=stderr,
            creationflags=creationflags
        )
        return self.process

    async def iter_stdout_lines(self) -> AsyncIterator[str]:
        if not self.process or not self.process.stdout:
            return
        async for raw in self.process.stdout:
            yield raw.decode("utf-8", errors="ignore").strip()

    async def communicate(
        self,
        *cmd: str,
        timeout: float | None = 15.0,
    ) -> tuple[str, int]:
        """Jednorazowe uruchomienie, zwraca (stdout_text, returncode)."""
        creationflags = 0
        if sys.platform == "win32":
            creationflags = subprocess.CREATE_NO_WINDOW

        p = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            creationflags=creationflags
        )
        try:
            stdout, _ = await asyncio.wait_for(p.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            p.kill()
            return "", -1
        text = stdout.decode("utf-8", errors="ignore").strip()
        rc = p.returncode if p.returncode is not None else -1
        return text, rc

    def terminate(self) -> None:
        if self.process and self.process.returncode is None:
            self.process.terminate()

    def kill(self) -> None:
        if self.process and self.process.returncode is None:
            self.process.kill()

    async def wait(self) -> int:
        if not self.process:
            return -1
        return await self.process.wait()

    @property
    def returncode(self) -> int | None:
        return self.process.returncode if self.process else None
