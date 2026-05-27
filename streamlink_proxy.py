"""
StreamlinkProxy — uruchamia lokalny serwer HTTP który proxy'uje stream przez streamlink.

Workflow Jana:
  1. m3u8 Sniffer daje URL → wklejasz tutaj
  2. Klikasz "Uruchom proxy" → serwer na localhost:8888
  3. W OBS: Źródła → Przechwytywanie multimediów → wklej http://localhost:8888
  4. OBS nagrywa 1:1
"""

from __future__ import annotations

import asyncio
import logging
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

logger = logging.getLogger(__name__)


class StreamlinkProxy:
    def __init__(self) -> None:
        self._server: HTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._running: bool = False
        self._port: int = 8888
        self._url: str = ""
        self._quality: str = "best"
        self._error: str = ""
        self._log_lines: list[str] = []

    # ── Public API ───────────────────────────────────────────────────────────

    async def start(self, url: str, quality: str = "best", port: int = 8888) -> dict:
        """Resolve stream via streamlink and start HTTP proxy server."""
        if self._running:
            await self.stop()

        self._url = url
        self._port = port
        self._quality = quality
        self._error = ""
        self._log_lines = []

        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, self._start_sync, url, quality, port)
        return result

    async def stop(self) -> None:
        self._running = False
        if self._server:
            self._server.shutdown()
            self._server = None
        if self._thread:
            self._thread.join(timeout=3)
            self._thread = None

    def status(self) -> dict:
        return {
            "running": self._running,
            "url": f"http://localhost:{self._port}" if self._running else "",
            "port": self._port,
            "quality": self._quality,
            "error": self._error,
            "log": list(self._log_lines[-15:]),
        }

    # ── Sync startup (runs in thread-pool) ───────────────────────────────────

    def _start_sync(self, url: str, quality: str, port: int) -> dict:
        try:
            from streamlink import Streamlink, NoPluginError  # type: ignore

            sl = Streamlink()
            self._log(f"Pobieranie listy streamów: {url}")

            try:
                streams: dict[str, Any] = sl.streams(url)
            except NoPluginError:
                return self._fail(f"Streamlink nie obsługuje tego URL: {url}")

            if not streams:
                return self._fail("Nie znaleziono żadnych streamów dla tego URL")

            # Pick quality
            stream_obj = streams.get(quality) or streams.get("best")
            if stream_obj is None:
                available = ", ".join(streams.keys())
                return self._fail(f"Brak jakości '{quality}'. Dostępne: {available}")

            chosen_quality = quality if quality in streams else "best"
            self._quality = chosen_quality
            self._log(f"Wybrany stream: {chosen_quality}")

            proxy_self = self

            class _Handler(BaseHTTPRequestHandler):
                def do_GET(self):
                    self.send_response(200)
                    self.send_header("Content-Type", "video/mp2t")
                    self.send_header("Transfer-Encoding", "chunked")
                    self.end_headers()
                    proxy_self._log("Klient OBS połączony — przesyłanie danych…")
                    try:
                        with stream_obj.open() as fd:
                            while proxy_self._running:
                                chunk = fd.read(65536)
                                if not chunk:
                                    break
                                size_header = f"{len(chunk):X}\r\n".encode()
                                self.wfile.write(size_header + chunk + b"\r\n")
                                self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError):
                        proxy_self._log("Klient OBS rozłączył się")
                    except Exception as exc:
                        proxy_self._log(f"Błąd strumienia: {exc}")

                def log_message(self, *_):
                    pass  # suppress built-in access log

            try:
                self._server = HTTPServer(("0.0.0.0", port), _Handler)
            except OSError as exc:
                return self._fail(f"Nie można uruchomić serwera na porcie {port}: {exc}")

            self._running = True
            self._log(f"Serwer proxy uruchomiony na http://localhost:{port}")

            self._thread = threading.Thread(
                target=self._server.serve_forever,
                daemon=True,
                name="StreamlinkProxyHTTP",
            )
            self._thread.start()
            return {"ok": True, "url": f"http://localhost:{port}", "quality": chosen_quality}

        except ImportError:
            return self._fail("Streamlink nie jest zainstalowany")
        except Exception as exc:
            return self._fail(str(exc))

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _log(self, msg: str) -> None:
        logger.info("[StreamProxy] %s", msg)
        self._log_lines.append(msg)
        if len(self._log_lines) > 200:
            self._log_lines = self._log_lines[-100:]

    def _fail(self, msg: str) -> dict:
        self._error = msg
        self._running = False
        logger.warning("[StreamProxy] %s", msg)
        return {"ok": False, "error": msg}
