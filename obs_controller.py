"""
OBSController — asyncio client for OBS Studio via obs-websocket v5.

OBS 28+ ships with obs-websocket 5.x built-in.
Enable it in OBS: Tools → obs-websocket Settings → Enable WebSocket server.
"""

import asyncio
import base64
import hashlib
import json
import logging
import uuid

logger = logging.getLogger(__name__)


class OBSController:
    def __init__(self):
        self._ws = None
        self._connected = False
        self._obs_version: str = ""
        self._ws_version: str = ""
        self._recording: bool = False
        self._record_timecode: str = ""
        self._pending: dict[str, asyncio.Future] = {}
        self._recv_task: asyncio.Task | None = None

    # ── Public state ────────────────────────────────────────────────────────

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def recording(self) -> bool:
        return self._recording

    @property
    def obs_version(self) -> str:
        return self._obs_version

    @property
    def record_timecode(self) -> str:
        return self._record_timecode

    # ── Connection ──────────────────────────────────────────────────────────

    async def connect(self, host: str = "localhost", port: int = 4455, password: str = "") -> dict:
        import websockets  # already in requirements

        uri = f"ws://{host}:{port}"
        try:
            self._ws = await asyncio.wait_for(
                websockets.connect(uri, open_timeout=5, ping_interval=20), timeout=6
            )

            # Step 1: receive Hello (op 0)
            raw = await asyncio.wait_for(self._ws.recv(), timeout=5)
            hello = json.loads(raw)
            if hello.get("op") != 0:
                raise ValueError("Expected Hello (op 0) from OBS")

            hello_d = hello["d"]
            self._ws_version = hello_d.get("obsWebSocketVersion", "?")
            self._obs_version = ""  # fetched after identify

            # Step 2: send Identify (op 1)
            identify: dict = {"rpcVersion": 1, "eventSubscriptions": 1023}
            if "authentication" in hello_d and password:
                auth_d = hello_d["authentication"]
                identify["authentication"] = self._compute_auth(
                    password, auth_d["salt"], auth_d["challenge"]
                )

            await self._ws.send(json.dumps({"op": 1, "d": identify}))

            # Step 3: receive Identified (op 2)
            raw = await asyncio.wait_for(self._ws.recv(), timeout=5)
            identified = json.loads(raw)
            if identified.get("op") != 2:
                raise ValueError("Authentication failed — wrong password?")

            self._connected = True
            self._recv_task = asyncio.create_task(self._recv_loop())

            # Fetch OBS Studio version
            try:
                ver_resp = await self._send_request("GetVersion")
                self._obs_version = ver_resp.get("responseData", {}).get("obsVersion", "?")
            except Exception:
                self._obs_version = "?"

            return {"ok": True, "obs_version": self._obs_version, "ws_version": self._ws_version}

        except Exception as exc:
            await self._cleanup()
            return {"ok": False, "error": str(exc)}

    async def disconnect(self) -> None:
        await self._cleanup()

    async def _cleanup(self) -> None:
        self._connected = False
        self._recording = False
        if self._recv_task and not self._recv_task.done():
            self._recv_task.cancel()
            try:
                await self._recv_task
            except asyncio.CancelledError:
                pass
        self._recv_task = None
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None

    # ── Auth ────────────────────────────────────────────────────────────────

    @staticmethod
    def _compute_auth(password: str, salt: str, challenge: str) -> str:
        secret = base64.b64encode(
            hashlib.sha256((password + salt).encode()).digest()
        ).decode()
        return base64.b64encode(
            hashlib.sha256((secret + challenge).encode()).digest()
        ).decode()

    # ── Request / Response loop ─────────────────────────────────────────────

    async def _send_request(self, request_type: str, data: dict | None = None) -> dict:
        if not self._connected or not self._ws:
            raise RuntimeError("Not connected to OBS")
        request_id = str(uuid.uuid4())[:8]
        msg = {
            "op": 6,
            "d": {
                "requestType": request_type,
                "requestId": request_id,
                "requestData": data or {},
            },
        }
        loop = asyncio.get_event_loop()
        fut: asyncio.Future = loop.create_future()
        self._pending[request_id] = fut
        try:
            await self._ws.send(json.dumps(msg))
            return await asyncio.wait_for(asyncio.shield(fut), timeout=10)
        except asyncio.TimeoutError:
            return {"error": "timeout"}
        finally:
            self._pending.pop(request_id, None)

    async def _recv_loop(self) -> None:
        try:
            async for raw in self._ws:
                try:
                    msg = json.loads(raw)
                    op = msg.get("op")

                    if op == 7:  # RequestResponse
                        d = msg["d"]
                        rid = d.get("requestId")
                        if rid and rid in self._pending:
                            fut = self._pending.pop(rid)
                            if not fut.done():
                                fut.set_result(d)

                    elif op == 5:  # Event
                        self._handle_event(msg["d"])

                except Exception as exc:
                    logger.debug("OBS recv parse error: %s", exc)

        except Exception as exc:
            logger.info("OBS WebSocket closed: %s", exc)
        finally:
            self._connected = False
            self._recording = False
            # Resolve any pending futures with an error
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(RuntimeError("Connection closed"))
            self._pending.clear()

    def _handle_event(self, event_data: dict) -> None:
        event_type = event_data.get("eventType", "")
        payload = event_data.get("eventData", {})

        if event_type == "RecordStateChanged":
            state = payload.get("outputState", "")
            self._recording = state in (
                "OBS_WEBSOCKET_OUTPUT_STARTED",
                "OBS_WEBSOCKET_OUTPUT_RESUMED",
            )
            if not self._recording:
                self._record_timecode = ""

        elif event_type == "RecordingTimecode":
            self._record_timecode = payload.get("outputTimecode", "")

    # ── Recording ───────────────────────────────────────────────────────────

    async def start_record(self) -> dict:
        result = await self._send_request("StartRecord")
        status = result.get("requestStatus", {})
        if status.get("result", False):
            self._recording = True
        return {"ok": status.get("result", False), "error": status.get("comment", "")}

    async def stop_record(self) -> dict:
        result = await self._send_request("StopRecord")
        status = result.get("requestStatus", {})
        if status.get("result", False):
            self._recording = False
            self._record_timecode = ""
        return {
            "ok": status.get("result", False),
            "output_path": result.get("responseData", {}).get("outputPath", ""),
            "error": status.get("comment", ""),
        }

    async def get_record_status(self) -> dict:
        result = await self._send_request("GetRecordStatus")
        rd = result.get("responseData", {})
        self._recording = rd.get("outputActive", False)
        self._record_timecode = rd.get("outputTimecode", "")
        return {
            "recording": self._recording,
            "timecode": self._record_timecode,
            "bytes": rd.get("outputBytes", 0),
        }

    # ── Scenes ──────────────────────────────────────────────────────────────

    async def get_scenes(self) -> list[dict]:
        result = await self._send_request("GetSceneList")
        rd = result.get("responseData", {})
        scenes = rd.get("scenes", [])
        current = rd.get("currentProgramSceneName", "")
        return [
            {"name": s.get("sceneName", ""), "active": s.get("sceneName", "") == current}
            for s in reversed(scenes)  # OBS returns bottom-to-top; reverse for natural order
        ]

    async def set_scene(self, scene_name: str) -> dict:
        result = await self._send_request("SetCurrentProgramScene", {"sceneName": scene_name})
        ok = result.get("requestStatus", {}).get("result", False)
        return {"ok": ok}

    # ── Stats ───────────────────────────────────────────────────────────────

    async def get_stats(self) -> dict:
        result = await self._send_request("GetStats")
        rd = result.get("responseData", {})
        return {
            "fps": round(rd.get("activeFps", 0), 1),
            "cpu": round(rd.get("cpuUsage", 0), 1),
            "memory_mb": round(rd.get("memoryUsage", 0), 0),
            "dropped_frames": rd.get("outputSkippedFrames", 0),
        }

    def status_dict(self) -> dict:
        return {
            "connected": self._connected,
            "recording": self._recording,
            "obs_version": self._obs_version,
            "ws_version": self._ws_version,
            "timecode": self._record_timecode,
        }
