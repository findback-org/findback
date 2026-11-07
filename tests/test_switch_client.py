"""Transport contract tests; no model, live Switch, or network is used."""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from findback.switch_client import (
    SwitchClient, SwitchGapError, SwitchHTTPError, SwitchProtocolError,
    SwitchUnknownOutcomeError,
)


class FakeSocket:
    def __init__(self):
        self.incoming = asyncio.Queue()
        self.sent = []
        self.closed = False

    def put(self, frame):
        self.incoming.put_nowait(None if frame is None else json.dumps(frame))

    def __aiter__(self):
        return self

    async def __anext__(self):
        value = await self.incoming.get()
        if value is None:
            raise StopAsyncIteration
        return value

    async def send(self, data):
        self.sent.append(json.loads(data))

    async def close(self):
        self.closed = True


class Response:
    def __init__(self, data, status=200):
        self.data, self.status_code = data, status

    def json(self):
        return self.data


class FakeHTTP:
    def __init__(self):
        self.calls = []
        self.failure = None
        self.status = 200

    async def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        await asyncio.sleep(0)
        if self.failure:
            raise self.failure
        if method == "GET":
            return Response({"operations": {"post_message": {"input_schema": {}}}}, self.status)
        return Response({"result": {"event_id": "posted", "received": kwargs["json"]}}, self.status)


class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.http = FakeHTTP()
        self.sockets = []
        self.urls = []

        async def connect(url, **kwargs):
            self.urls.append((url, kwargs))
            socket = FakeSocket()
            self.sockets.append(socket)
            socket.put({"event": "connection_state", "data": {
                "connection_id": self.client.connection_id, "agent_id": "service",
                "generation": len(self.sockets), "rooms": ["staff", "claim"],
                "cursor": 10 if len(self.sockets) == 1 else 99,
                "server": {"version": "0.31.0"},
            }})
            return socket

        self.client = SwitchClient("http://127.0.0.1:8000", "service", "PRIVATE_KEY",
            ["staff", "claim"], Path(self.temp.name) / "resume.json",
            reconnect_delay=.01, http_client=self.http, ws_connect=connect)
        await self.client.start()
        self.events = self.client.events()
        self.state = await anext(self.events)

    async def asyncTearDown(self):
        await self.events.aclose()
        await self.client.close()
        self.temp.cleanup()

    def message(self, ticket, body="hello"):
        return {"event": "message", "id": ticket, "data": {
            "type": "message", "sequence": ticket, "room_id": "claim",
            "payload": {"sender": "@immutable-actor:localhost", "sender_kind": None,
                        "sender_name": "Priya", "message_id": f"sw_{ticket}",
                        "body": body, "on_behalf_of": None},
        }}

    async def test_ack_only_advances_contiguous_durably_captured_prefix(self):
        self.sockets[0].put(self.message(11))
        self.sockets[0].put(self.message(12))
        first, second = await anext(self.events), await anext(self.events)
        self.assertEqual(10, self.client.cursor)
        await self.client.ack(second)
        self.assertEqual(10, self.client.cursor)
        await self.client.ack(first)
        self.assertEqual(12, self.client.cursor)
        saved = json.loads(self.client.state_path.read_text())
        self.assertEqual(12, saved["cursor"])
        self.assertNotIn("PRIVATE_KEY", self.client.state_path.read_text())
        self.assertEqual(0o600, self.client.state_path.stat().st_mode & 0o777)
        self.assertIsNone(first["data"]["payload"]["sender_kind"])
        with self.assertRaises(SwitchProtocolError):
            await self.client.ack(dict(second))

    async def test_ping_uses_durable_cursor_and_does_not_wait_for_consumer(self):
        self.sockets[0].put(self.message(11))
        self.sockets[0].put({"event": "ping", "data": {}})
        for _ in range(20):
            if self.sockets[0].sent:
                break
            await asyncio.sleep(.001)
        self.assertEqual([{"type": "pong", "cursor": 10}], self.sockets[0].sent)
        self.assertEqual(10, self.client.cursor)

    async def test_reconnect_preserves_unacked_cursor_and_fences_generation(self):
        self.sockets[0].put(self.message(11))
        frame = await anext(self.events)
        self.sockets[0].put(None)
        reconnected = await asyncio.wait_for(anext(self.events), 1)
        self.assertEqual("connection_state", reconnected["event"])
        params = parse_qs(urlsplit(self.urls[1][0]).query)
        self.assertEqual(["10"], params["start_from"])
        self.assertEqual(["1"], params["expected_generation"])
        self.assertEqual(10, self.client.cursor)  # server's sent cursor 99 is not acked
        await self.client.ack(frame)
        self.assertEqual(11, self.client.cursor)
        restored = SwitchClient(self.client.base_url, "service", "another-key", ["staff", "claim"],
            self.client.state_path, http_client=self.http, ws_connect=lambda *_: None)
        self.assertEqual(self.client.connection_id, restored.connection_id)
        self.assertEqual(11, restored.cursor)

    async def test_room_operations_are_bound_to_distinct_sessions(self):
        a, b = await asyncio.gather(
            self.client.post_message("staff", "private", thread_id="root"),
            self.client.post_message("claim", "safe"),
        )
        self.assertEqual("posted", a["event_id"])
        calls = self.http.calls
        self.assertEqual(["connect_to_room", "post_message", "connect_to_room", "post_message"],
            [url.rsplit("/", 1)[1] for _, url, _ in calls])
        self.assertEqual("staff", calls[0][2]["json"]["room_id"])
        self.assertEqual("claim", calls[2][2]["json"]["room_id"])
        self.assertEqual(calls[0][2]["headers"], calls[1][2]["headers"])
        self.assertNotEqual(calls[0][2]["headers"]["X-Switch-Session-Id"],
                            calls[2][2]["headers"]["X-Switch-Session-Id"])
        self.assertEqual("root", calls[1][2]["json"]["thread_id"])
        with self.assertRaises(SwitchProtocolError):
            await self.client.post_message("unrelated", "secret")

    async def test_failed_mutation_is_not_retried_or_credential_echoed(self):
        await self.client.read_context("claim")
        before = len(self.http.calls)
        self.http.failure = OSError("secret PRIVATE_KEY in network diagnostic")
        with self.assertRaises(SwitchUnknownOutcomeError) as caught:
            await self.client.post_message("claim", "one post")
        self.assertEqual(before + 1, len(self.http.calls))
        self.assertNotIn("PRIVATE_KEY", str(caught.exception))
        self.assertNotIn("PRIVATE_KEY", repr(self.client))

    async def test_catalog_unwrapped_and_http_refusal_kept_safe(self):
        self.assertIn("post_message", await self.client.discover_operations())
        self.http.status = 403
        with self.assertRaises(SwitchHTTPError) as caught:
            await self.client.read_context("claim")
        self.assertEqual(403, caught.exception.status)

    async def test_gap_halts_without_advancing_cursor(self):
        self.sockets[0].put({"event": "gap", "data": {"reason": "expired"}})
        with self.assertRaises(SwitchGapError):
            await asyncio.wait_for(anext(self.events), 1)
        self.assertEqual(10, self.client.cursor)


if __name__ == "__main__":
    unittest.main()
