"""WebSocket authentication and worker failure handling without an external TTS provider."""

import asyncio
import json

from starlette.websockets import WebSocketDisconnect

from tests.conftest import run_async


class FakeWebSocket:
    def __init__(self, messages=(), *, receive_delay=0, wait_before_disconnect=False):
        self.messages = list(messages)
        self.receive_delay = receive_delay
        self.wait_before_disconnect = wait_before_disconnect
        self.sent_json = []
        self.sent_bytes = []
        self.closed = None
        self.disconnected = asyncio.Event()

    async def accept(self):
        return None

    async def receive_text(self):
        if self.receive_delay:
            await asyncio.sleep(self.receive_delay)
        else:
            await asyncio.sleep(0)
        if self.messages:
            message = self.messages.pop(0)
            if isinstance(message, BaseException):
                raise message
            return message
        if self.wait_before_disconnect:
            await asyncio.wait_for(self.disconnected.wait(), timeout=1)
        raise WebSocketDisconnect(code=1000)

    async def send_text(self, text):
        payload = json.loads(text)
        self.sent_json.append(payload)
        if payload.get("type") == "done":
            self.disconnected.set()

    async def send_bytes(self, data):
        self.sent_bytes.append(data)

    async def close(self, *, code, reason=None):
        self.closed = {"code": code, "reason": reason}


async def _run_worker(routes, ws, jobs, cancel_event):
    queue = asyncio.Queue()
    for job in jobs:
        await queue.put(job)
    await queue.put(None)
    await routes._synth_worker(ws, queue, cancel_event)


def test_token_helper_handles_missing_token_and_auth_backend_failure(monkeypatch):
    import api.routes.tts_routes as routes
    import core.auth as auth

    assert run_async(routes._user_from_token(None)) is None

    async def failure(_token):
        raise RuntimeError("auth service unavailable")

    monkeypatch.setattr(auth, "get_active_user_from_token", failure)
    assert run_async(routes._user_from_token("token")) is None


def test_websocket_rejects_non_object_first_message_as_unauthenticated(monkeypatch):
    import api.routes.tts_routes as routes

    monkeypatch.setattr(routes, "tts_available", lambda: (True, "ready"))
    ws = FakeWebSocket(["[]"])
    run_async(routes.tts_stream(ws))
    assert ws.sent_json == [{"type": "error", "message": "未认证"}]
    assert ws.closed == {"code": 4401, "reason": "unauthorized"}


def test_auth_timeout_malformed_json_and_disconnect_close_with_auth_required(monkeypatch):
    import api.routes.tts_routes as routes

    monkeypatch.setattr(routes, "tts_available", lambda: (True, "ready"))
    monkeypatch.setattr(routes, "_AUTH_TIMEOUT_SECONDS", 0.001)
    timed_out = FakeWebSocket(receive_delay=0.02)
    run_async(routes.tts_stream(timed_out))
    assert timed_out.closed == {"code": 4401, "reason": "auth required"}

    monkeypatch.setattr(routes, "_AUTH_TIMEOUT_SECONDS", 1)
    malformed = FakeWebSocket(["not-json"])
    run_async(routes.tts_stream(malformed))
    assert malformed.closed == {"code": 4401, "reason": "auth required"}

    disconnected = FakeWebSocket([WebSocketDisconnect(code=1001)])
    run_async(routes.tts_stream(disconnected))
    assert disconnected.closed == {"code": 4401, "reason": "auth required"}


def test_unavailable_service_returns_reason_and_closes(monkeypatch):
    import api.routes.tts_routes as routes

    monkeypatch.setattr(routes, "tts_available", lambda: (False, "provider credentials missing"))
    ws = FakeWebSocket()
    run_async(routes.tts_stream(ws))
    assert ws.sent_json == [
        {"type": "error", "message": "语音合成不可用: provider credentials missing"}
    ]
    assert ws.closed == {"code": 1011, "reason": None}


def test_malformed_authenticated_message_is_contained_and_worker_is_cleaned_up(monkeypatch):
    import api.routes.tts_routes as routes

    monkeypatch.setattr(routes, "tts_available", lambda: (True, "ready"))

    async def user(_token):
        return {"username": "member"}

    monkeypatch.setattr(routes, "_user_from_token", user)
    ws = FakeWebSocket([json.dumps({"type": "auth", "token": "valid"}), "not-json"])
    run_async(routes.tts_stream(ws))
    assert ws.sent_json == [{"type": "ready"}]


def test_cancel_discards_queued_items_and_handles_queue_race(monkeypatch):
    import api.routes.tts_routes as routes

    class ImmediateWebSocket(FakeWebSocket):
        async def receive_text(self):
            if self.messages:
                message = self.messages.pop(0)
                if isinstance(message, BaseException):
                    raise message
                return message
            raise WebSocketDisconnect(code=1000)

    class RaceQueue:
        def __init__(self):
            self.empty_checks = 0
            self.items = []

        def empty(self):
            self.empty_checks += 1
            return self.empty_checks > 1

        def get_nowait(self):
            raise asyncio.QueueEmpty

        async def put(self, item):
            self.items.append(item)

    async def user(_token):
        return {"username": "member"}

    monkeypatch.setattr(routes, "tts_available", lambda: (True, "ready"))
    monkeypatch.setattr(routes, "_user_from_token", user)

    normal_queue = asyncio.Queue()
    normal_asyncio = type(
        "AsyncioProxy",
        (),
        {
            "Queue": staticmethod(lambda: normal_queue),
            "QueueEmpty": asyncio.QueueEmpty,
            "Event": asyncio.Event,
            "create_task": staticmethod(asyncio.create_task),
            "wait_for": staticmethod(asyncio.wait_for),
            "CancelledError": asyncio.CancelledError,
        },
    )
    monkeypatch.setattr(routes, "asyncio", normal_asyncio)
    normal_ws = ImmediateWebSocket(
        [
            json.dumps({"type": "auth", "token": "valid"}),
            json.dumps({"type": "speak", "id": 1, "text": "queued text"}),
            json.dumps({"type": "cancel"}),
        ]
    )

    async def run_normal_cancel():
        await normal_queue.put((1, "queued text"))
        await routes.tts_stream(normal_ws)

    run_async(run_normal_cancel())
    assert normal_queue.get_nowait() is None
    assert normal_queue.empty()

    queue = RaceQueue()
    fake_asyncio = type(
        "AsyncioProxy",
        (),
        {
            "Queue": staticmethod(lambda: queue),
            "QueueEmpty": asyncio.QueueEmpty,
            "Event": asyncio.Event,
            "create_task": staticmethod(asyncio.create_task),
            "wait_for": staticmethod(asyncio.wait_for),
            "CancelledError": asyncio.CancelledError,
        },
    )
    monkeypatch.setattr(routes, "asyncio", fake_asyncio)
    ws = ImmediateWebSocket(
        [
            json.dumps({"type": "auth", "token": "valid"}),
            json.dumps({"type": "cancel"}),
        ]
    )
    run_async(routes.tts_stream(ws))
    assert queue.empty_checks == 1
    assert queue.items == [None]


def test_authenticated_protocol_ignores_empty_unknown_messages_and_caps_text(monkeypatch):
    import api.routes.tts_routes as routes

    monkeypatch.setattr(routes, "tts_available", lambda: (True, "ready"))

    async def user(_token):
        return {"username": "member"}

    monkeypatch.setattr(routes, "_user_from_token", user)
    synthesized = []

    async def stream(text):
        synthesized.append(text)
        yield b"audio"

    monkeypatch.setattr(routes, "synthesize_stream", stream)
    ws = FakeWebSocket(
        [
            json.dumps({"type": "auth", "token": "valid"}),
            json.dumps({"type": "speak", "id": 1, "text": "   "}),
            json.dumps({"type": "speak", "id": 2, "text": "x" * 1200}),
            json.dumps({"type": "ping"}),
            json.dumps({"type": "unknown"}),
        ],
        wait_before_disconnect=True,
    )
    run_async(routes.tts_stream(ws))
    assert ws.sent_json[0] == {"type": "ready"}
    assert {message["type"] for message in ws.sent_json} >= {"ready", "started", "done", "pong"}
    assert synthesized == ["x" * routes._MAX_TEXT_LENGTH]
    assert ws.sent_bytes == [b"audio"]


def test_synth_worker_cancellation_stops_remaining_audio_and_resets_event(monkeypatch):
    import api.routes.tts_routes as routes

    cancel_event = asyncio.Event()

    class CancellingSocket(FakeWebSocket):
        async def send_bytes(self, data):
            await super().send_bytes(data)
            if data == b"first":
                cancel_event.set()

    async def stream(_text):
        yield b"first"
        yield b"second"

    monkeypatch.setattr(routes, "synthesize_stream", stream)
    ws = CancellingSocket()
    run_async(_run_worker(routes, ws, [(4, "hello")], cancel_event))
    assert ws.sent_bytes == [b"first"]
    assert ws.sent_json == [{"type": "started", "id": 4}, {"type": "done", "id": 4}]
    assert not cancel_event.is_set()


def test_synth_worker_reports_provider_errors_and_truncates_message(monkeypatch):
    import api.routes.tts_routes as routes

    async def broken(_text):
        raise RuntimeError("E" * 250)
        yield b"unreachable"

    monkeypatch.setattr(routes, "synthesize_stream", broken)
    ws = FakeWebSocket()
    run_async(_run_worker(routes, ws, [(5, "hello")], asyncio.Event()))
    assert ws.sent_json[0] == {"type": "started", "id": 5}
    assert ws.sent_json[1]["type"] == "error"
    assert len(ws.sent_json[1]["message"]) == 200


def test_synth_worker_stops_on_disconnect_and_suppresses_error_send_failure(monkeypatch):
    import api.routes.tts_routes as routes

    class DisconnectingSocket(FakeWebSocket):
        async def send_bytes(self, _data):
            raise WebSocketDisconnect(code=1001)

    async def stream(_text):
        yield b"audio"

    monkeypatch.setattr(routes, "synthesize_stream", stream)
    disconnected = DisconnectingSocket()
    run_async(_run_worker(routes, disconnected, [(6, "hello"), (7, "not processed")], asyncio.Event()))
    assert disconnected.sent_json == [{"type": "started", "id": 6}]

    async def broken(_text):
        raise RuntimeError("provider error")
        yield b"unreachable"

    class CannotSendError(FakeWebSocket):
        async def send_text(self, text):
            payload = json.loads(text)
            if payload.get("type") == "error":
                raise RuntimeError("socket already closed")
            await super().send_text(text)

    monkeypatch.setattr(routes, "synthesize_stream", broken)
    ws = CannotSendError()
    run_async(_run_worker(routes, ws, [(8, "hello"), (9, "not processed")], asyncio.Event()))
    assert ws.sent_json == [{"type": "started", "id": 8}]
