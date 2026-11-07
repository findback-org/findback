"""Small Switch Core 0.31 transport; application state is outside this module.

Frames are delivered unchanged. The caller MUST durably store a frame before
calling ``ack(frame)``. Receiving a frame never advances the saved resume cursor.
HTTP mutations are never retried: the application reconciles unknown outcomes.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import tempfile
import uuid
from collections import deque
from pathlib import Path
from typing import Any, AsyncIterator, Callable
from urllib.parse import quote, urlencode, urlsplit, urlunsplit


class SwitchError(RuntimeError):
    """Safe error text: never includes credentials, request body, or server prose."""


class SwitchHTTPError(SwitchError):
    def __init__(self, status: int):
        self.status = status
        super().__init__(f"Switch refused the HTTP request (status {status}).")


class SwitchUnknownOutcomeError(SwitchError):
    """A POST may have completed. Reconcile its effect before trying again."""


class SwitchProtocolError(SwitchError):
    pass


class SwitchGapError(SwitchProtocolError):
    """The server cannot provide a complete replay. Human/app reconciliation needed."""


class SwitchRefusedError(SwitchError):
    def __init__(self, status: int | None = None, code: str | None = None):
        self.status, self.code = status, code
        super().__init__("Switch refused or displaced this event connection.")


async def _default_connect(url: str, *, additional_headers: dict[str, str]):
    from websockets.asyncio.client import connect

    return await connect(
        url, additional_headers=additional_headers, open_timeout=15,
        max_size=4 * 1024 * 1024, ping_interval=20,
    )


class SwitchClient:
    """One custom agent's event connection and serialized room-scoped operations.

    ``http_client`` and ``ws_connect`` are injectable test seams. A supplied
    HTTP client remains owned by its caller. Runtime dependencies are httpx
    and websockets >= 14. The persisted state contains no API key.
    """

    def __init__(
        self, base_url: str, agent_id: str, api_key: str, room_ids: list[str],
        state_path: str | Path | None = None, *, reconnect_delay: float = 1.0,
        http_client: Any = None, ws_connect: Callable[..., Any] | None = None,
    ):
        parsed = urlsplit(base_url.rstrip("/"))
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("Switch base URL must be an HTTP(S) URL without credentials or query.")
        if not agent_id or not api_key or not room_ids or any(not r for r in room_ids):
            raise ValueError("An agent, API key, and explicit room IDs are required.")
        self.base_url = base_url.rstrip("/")
        self.agent_id = agent_id
        self.room_ids = tuple(dict.fromkeys(room_ids))
        self._api_key = api_key
        self.state_path = Path(state_path) if state_path is not None else None
        self.connection_id = str(uuid.uuid4())
        self.generation: int | None = None
        self.cursor: int | None = None
        self.server: dict[str, Any] | None = None
        self._load_state()
        self._owns_http = http_client is None
        if http_client is None:
            import httpx
            http_client = httpx.AsyncClient(timeout=20, follow_redirects=False)
        self._http = http_client
        self._connect = ws_connect or _default_connect
        self._reconnect_delay = max(0.01, reconnect_delay)
        self._ready = asyncio.Event()
        self._closed = False
        self._reader_task: asyncio.Task | None = None
        self._socket: Any = None
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=256)
        self._operation_lock = asyncio.Lock()
        self._ack_lock = asyncio.Lock()
        self._pending: deque[dict[str, Any]] = deque()
        self._receipts: dict[int, dict[str, Any]] = {}
        self._bound_sessions: set[str] = set()
        self._failure: Exception | None = None

    def __repr__(self) -> str:
        return f"SwitchClient(agent_id={self.agent_id!r}, rooms={len(self.room_ids)})"

    def _load_state(self) -> None:
        if self.state_path is None or not self.state_path.exists():
            return
        try:
            state = json.loads(self.state_path.read_text())
            if state["agent_id"] != self.agent_id or state["base_url"] != self.base_url:
                raise ValueError
            uuid.UUID(state["connection_id"])
            for key in ("generation", "cursor"):
                value = state.get(key)
                if value is not None and (type(value) is not int or value < 0):
                    raise ValueError
            self.connection_id = state["connection_id"]
            self.generation, self.cursor = state.get("generation"), state.get("cursor")
        except (ValueError, TypeError, KeyError, OSError):
            raise SwitchProtocolError("Invalid or mismatched Switch resume-state file.") from None

    def _save_state(self) -> None:
        if self.state_path is None:
            return
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"base_url": self.base_url, "agent_id": self.agent_id,
                   "connection_id": self.connection_id, "generation": self.generation,
                   "cursor": self.cursor}
        fd, temp = tempfile.mkstemp(prefix=".switch-state-", dir=self.state_path.parent)
        try:
            with os.fdopen(fd, "w") as out:
                json.dump(payload, out)
                out.flush()
                os.fsync(out.fileno())
            os.replace(temp, self.state_path)
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temp)

    def _ws_url(self) -> str:
        parsed = urlsplit(self.base_url)
        params = {"connection_id": self.connection_id, "scope": "all", "filter": "all",
                  "rooms": ",".join(self.room_ids), "protocol": 8, "protocol_accepts": 8,
                  "client": "findback", "client_version": "0.1.0",
                  "start_from": "head" if self.cursor is None else str(self.cursor)}
        if self.generation is not None:
            params["expected_generation"] = self.generation
        return urlunsplit(("wss" if parsed.scheme == "https" else "ws", parsed.netloc,
                             parsed.path + f"/agents/{quote(self.agent_id, safe='')}/connection/ws",
                             urlencode(params), ""))

    async def __aenter__(self):
        try:
            await self.start()
        except BaseException:
            # Failed entry is never registered with an AsyncExitStack, so its
            # caller cannot run __aexit__ to release the reader/owned HTTP.
            await self.close()
            raise
        return self

    async def __aexit__(self, *_):
        await self.close()

    async def start(self) -> None:
        if self._closed:
            raise SwitchError("Switch client is closed.")
        if self._reader_task is None:
            self._reader_task = asyncio.create_task(self._reader(), name="findback-switch-reader")
        await self.wait_connected()

    async def wait_connected(self, timeout: float = 30) -> None:
        try:
            await asyncio.wait_for(self._ready.wait(), timeout)
        except TimeoutError:
            raise SwitchError("Switch event connection did not become ready.") from None
        if self._failure is not None:
            raise self._failure

    async def events(self) -> AsyncIterator[dict[str, Any]]:
        await self.start()
        while not self._closed:
            value = await self._queue.get()
            if isinstance(value, Exception):
                raise value
            if value is None:
                return
            yield value

    async def ack(self, frame: dict[str, Any]) -> None:
        """Advance only over the contiguous prefix the caller durably captured.

        Pass the original frame yielded by events(), not a reconstructed copy.
        Acknowledging later frames cannot skip an earlier unacknowledged frame.
        """
        async with self._ack_lock:
            record = self._receipts.get(id(frame))
            if record is None:
                if frame.get("id") is None:
                    return
                raise SwitchProtocolError("Cannot acknowledge an unknown Switch frame.")
            record["acked"] = True
            changed = False
            while self._pending and self._pending[0]["acked"]:
                first = self._pending.popleft()
                self._receipts.pop(id(first["frame"]), None)
                self.cursor = first["cursor"]
                changed = True
            if changed:
                self._save_state()

    async def _reader(self) -> None:
        delay = self._reconnect_delay
        try:
            while not self._closed:
                self._ready.clear()
                self._bound_sessions.clear()
                try:
                    self._socket = await self._connect(self._ws_url(), additional_headers={"Authorization": f"Bearer {self._api_key}"})
                    async for raw in self._socket:
                        try:
                            frame = json.loads(raw)
                        except (ValueError, TypeError):
                            raise SwitchProtocolError("Switch sent invalid event JSON.") from None
                        if not isinstance(frame, dict) or not isinstance(frame.get("event"), str) or not isinstance(frame.get("data"), dict):
                            raise SwitchProtocolError("Switch sent an invalid event envelope.")
                        event, data = frame["event"], frame["data"]
                        if event == "ping":
                            await self._socket.send(json.dumps({"type": "pong", "cursor": self.cursor or 0}))
                            continue
                        if event == "refused":
                            raise SwitchRefusedError(data.get("status"))
                        if event == "gap":
                            raise SwitchGapError("Switch replay has a gap; reconcile before continuing.")
                        if event == "evicted":
                            if data.get("code") == "heartbeat_lapsed":
                                break
                            raise SwitchRefusedError(code=data.get("code"))
                        if event == "connection_state":
                            if data.get("agent_id") != self.agent_id or data.get("connection_id") != self.connection_id:
                                raise SwitchProtocolError("Switch connection identity did not match.")
                            rooms = data.get("rooms")
                            if not isinstance(rooms, list) or set(rooms) != set(self.room_ids):
                                raise SwitchProtocolError("Switch did not claim the configured rooms.")
                            generation = data.get("generation")
                            if type(generation) is not int or generation < 0:
                                raise SwitchProtocolError("Switch did not supply a connection generation.")
                            self.generation = generation
                            self.server = data.get("server")
                            # Initial head is an intentional starting point. On
                            # reconnect the server's sent cursor is NOT an ack.
                            if self.cursor is None and not self._pending:
                                cursor = data.get("cursor")
                                if type(cursor) is not int or cursor < 0:
                                    raise SwitchProtocolError("Switch supplied an invalid initial cursor.")
                                self.cursor = cursor
                            self._save_state()
                            self._ready.set()
                            delay = self._reconnect_delay
                        if not self._ready.is_set():
                            raise SwitchProtocolError("Switch event arrived before connection state.")
                        ticket = frame.get("id")
                        if ticket is not None:
                            if type(ticket) is not int or ticket < 0:
                                raise SwitchProtocolError("Switch supplied an invalid resume cursor.")
                            record = {"frame": frame, "cursor": ticket, "acked": False}
                            self._pending.append(record)
                            self._receipts[id(frame)] = record
                        await self._queue.put(frame)
                except (SwitchError, OSError) as exc:
                    if isinstance(exc, SwitchError):
                        raise
                    # Socket/network failures reconnect. Their exception text
                    # may contain URLs or headers and is never surfaced.
                except Exception as exc:
                    # WebSocket disconnects live in the dependency's exception
                    # hierarchy. Do not retry authentication/handshake failures.
                    name = type(exc).__name__
                    if name not in {"ConnectionClosed", "ConnectionClosedOK", "ConnectionClosedError", "TimeoutError"}:
                        raise SwitchProtocolError("Switch WebSocket connection failed.") from None
                finally:
                    self._ready.clear()
                    if self._socket is not None:
                        with contextlib.suppress(Exception):
                            await self._socket.close()
                    self._socket = None
                if not self._closed:
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 15)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._failure = exc
            self._ready.set()  # wake startup/operation waiters with the failure
            await self._queue.put(exc)

    async def _http_json(self, method: str, path: str, *, body: dict | None = None, headers: dict | None = None) -> Any:
        auth = {"Authorization": f"Bearer {self._api_key}", **(headers or {})}
        try:
            response = await self._http.request(method, self.base_url + path, json=body, headers=auth)
        except Exception:
            if method == "POST":
                raise SwitchUnknownOutcomeError("Switch POST outcome is unknown; reconcile before retrying.") from None
            raise SwitchError("Switch HTTP read failed.") from None
        if not 200 <= response.status_code < 300:
            if method == "POST" and response.status_code >= 500:
                raise SwitchUnknownOutcomeError("Switch POST failed with an uncertain outcome; reconcile before retrying.")
            raise SwitchHTTPError(response.status_code)
        try:
            return response.json()
        except (ValueError, TypeError):
            if method == "POST":
                raise SwitchUnknownOutcomeError("Switch POST returned an unreadable response; reconcile before retrying.") from None
            raise SwitchProtocolError("Switch HTTP read returned invalid JSON.") from None

    async def discover_operations(self) -> dict[str, Any]:
        data = await self._http_json("GET", f"/agents/{quote(self.agent_id, safe='')}/ops")
        if not isinstance(data, dict) or not isinstance(data.get("operations"), dict):
            raise SwitchProtocolError("Switch operation catalog is invalid.")
        return data["operations"]

    async def _call_operation(self, name: str, arguments: dict, headers: dict) -> Any:
        result = await self._http_json("POST", f"/agents/{quote(self.agent_id, safe='')}/ops/{quote(name, safe='')}", body=arguments, headers=headers)
        if not isinstance(result, dict) or "result" not in result:
            raise SwitchUnknownOutcomeError("Switch operation response is incomplete; reconcile before retrying.")
        return result["result"]

    async def operation(self, name: str, arguments: dict | None = None, *, room_id: str | None = None) -> Any:
        await self.start()
        if room_id is not None and room_id not in self.room_ids:
            raise SwitchProtocolError("Operation room is outside this client's configured scope.")
        async with self._operation_lock:
            await self.wait_connected()
            headers = {"X-Switch-Connection-Id": self.connection_id}
            if room_id is not None:
                # A session per room retains all the reader's room claims;
                # an unscoped connect_to_room would switch the connection.
                session_id = str(uuid.uuid5(uuid.UUID(self.connection_id), room_id))
                headers["X-Switch-Session-Id"] = session_id
                if room_id not in self._bound_sessions:
                    await self._call_operation("connect_to_room", {"room_id": room_id, "include_general_instructions": False}, headers)
                    self._bound_sessions.add(room_id)
            return await self._call_operation(name, arguments or {}, headers)

    async def post_message(self, room_id: str, body: str, thread_id: str | None = None) -> Any:
        arguments: dict[str, Any] = {"body": body}
        if thread_id is not None:
            arguments["thread_id"] = thread_id
        return await self.operation("post_message", arguments, room_id=room_id)

    async def read_context(self, room_id: str, limit: int = 50, *, before: str | None = None, since: str | None = None) -> Any:
        arguments: dict[str, Any] = {"room_id": room_id, "limit": limit}
        if before is not None:
            arguments["before"] = before
        if since is not None:
            arguments["since"] = since
        return await self.operation("read_context", arguments, room_id=room_id)

    async def list_participants(self, room_id: str) -> Any:
        return await self.operation("list_participants", room_id=room_id)

    async def close(self) -> None:
        self._closed = True
        if self._reader_task is not None:
            self._reader_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reader_task
        if self._owns_http:
            await self._http.aclose()
        with contextlib.suppress(asyncio.QueueFull):
            self._queue.put_nowait(None)
