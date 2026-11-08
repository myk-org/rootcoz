"""Streamed chat turns (issue #475): fake httpx stream + fake SidecarClient.

No binary, no network, no port: the sidecar is three fakes — a prompt POST, an
`/sessions/:id/events` feed and `abort()` — so parsing, fallback, abort and
failure handling are pinned without a live turn.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from rootcoz import ai_client, main, storage
from rootcoz.ai_client import AIResult, AITokenUsage

# Bound at import time: conftest replaces the module attribute with a denier.
from rootcoz.ai_client import call_ai as call_ai_under_test

# ---------------------------------------------------------------------------
# Fakes: the three sidecar surfaces one turn touches
# ---------------------------------------------------------------------------


def frame(event: dict[str, Any]) -> str:
    """One SSE frame the way the sidecar writes it."""
    return f"data: {json.dumps(event)}\n\n"


KEEPALIVE = ": keepalive\n\n"


class FakeFeed:
    """The `/sessions/:id/events` response body."""

    def __init__(
        self,
        frames: list[str] | None = None,
        *,
        error: Exception | None = None,
        error_after: int = 0,
        hold: asyncio.Event | None = None,
        hold_after: int = 0,
    ) -> None:
        self.status_code = 200
        self._frames = list(frames or [])
        self._error = error
        self._error_after = error_after
        self._hold = hold
        self._hold_after = hold_after
        self.closed = False

    def raise_for_status(self) -> None:
        return None

    async def aiter_text(self) -> AsyncIterator[str]:
        for index, chunk in enumerate(self._frames):
            if self._hold is not None and index == self._hold_after:
                await self._hold.wait()
            if self._error is not None and index == self._error_after:
                raise self._error
            yield chunk
            await asyncio.sleep(0)
        if self._error is not None and self._error_after >= len(self._frames):
            raise self._error


class _StreamContext:
    def __init__(self, feed: FakeFeed | None, enter_error: Exception | None) -> None:
        self._feed = feed
        self._enter_error = enter_error

    async def __aenter__(self) -> FakeFeed:
        if self._enter_error is not None:
            raise self._enter_error
        assert self._feed is not None
        return self._feed

    async def __aexit__(self, *exc: object) -> bool:
        assert self._feed is not None
        self._feed.closed = True
        return False


class _PromptResponse:
    def __init__(self, status_code: int, payload: dict[str, Any]) -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = json.dumps(payload)

    def json(self) -> dict[str, Any]:
        return self._payload


class FakeSidecar:
    """Just enough of `SidecarClient` to drive one streamed turn."""

    def __init__(
        self,
        *,
        text: str = "full answer",
        input_tokens: int = 11,
        output_tokens: int = 22,
        prompt_status: int = 200,
        prompt_payload: dict[str, Any] | None = None,
        prompt_error: Exception | None = None,
        prompt_gate: asyncio.Event | None = None,
        feed: FakeFeed | None = None,
        feed_status: int | None = None,
        feed_enter_error: Exception | None = None,
    ) -> None:
        self.timeout = 30.0
        self.prompt_calls: list[dict[str, Any]] = []
        self.stream_urls: list[str] = []
        self.aborted: list[str] = []
        self._text = text
        self._usage = {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
        }
        self._prompt_status = prompt_status
        self._prompt_payload = prompt_payload
        self._prompt_error = prompt_error
        self._prompt_gate = prompt_gate
        self._feed = feed if feed is not None else FakeFeed([])
        self._feed_status = feed_status
        self._feed_enter_error = feed_enter_error
        self._client = self  # `client._client.stream(...)` / `.timeout`

    # httpx.Client-ish surface used by ai_client
    def stream(self, method: str, url: str, timeout: Any = None) -> _StreamContext:
        self.stream_urls.append(url)
        if self._feed_status is not None:
            return _StreamContext(None, self._http_error(self._feed_status))
        return _StreamContext(self._feed, self._feed_enter_error)

    async def post(self, url: str, json: Any = None, timeout: Any = None) -> Any:
        self.prompt_calls.append(json or {})
        if self._prompt_gate is not None:
            await self._prompt_gate.wait()
        if self._prompt_error is not None:
            raise self._prompt_error
        payload = self._prompt_payload
        if payload is None:
            payload = {"text": self._text, "usage": self._usage}
        return _PromptResponse(self._prompt_status, payload)

    async def abort(self, session_id: str) -> None:
        self.aborted.append(session_id)

    @staticmethod
    def _http_error(status: int) -> httpx.HTTPStatusError:
        request = httpx.Request("GET", "http://sidecar.test/sessions/s/events")
        return httpx.HTTPStatusError(
            f"HTTP {status}",
            request=request,
            response=httpx.Response(status, request=request),
        )


def deltas() -> tuple[list[str], Any]:
    """A callback that records every forwarded fragment."""
    seen: list[str] = []
    return seen, seen.append


async def wait_until(predicate: Any, attempts: int = 500) -> None:
    """Yield to the loop until `predicate()` holds (no sleeping, no timeouts)."""
    for _ in range(attempts):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition never became true")


# ---------------------------------------------------------------------------
# T1-T13, T27, T28, T34: the streaming loop
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_prompt_forwards_deltas_and_returns_turn_end_text() -> None:
    client = FakeSidecar(
        text="Hel",  # the prompt's own text must lose to the terminal frame
        feed=FakeFeed(
            [
                frame({"type": "session"}),
                frame({"type": "turn_start", "turnId": "t1"}),
                frame({"type": "text_delta", "turnId": "t1", "delta": "Hel"}),
                frame({"type": "text_delta", "turnId": "t1", "delta": "lo"}),
                frame({"type": "turn_end", "turnId": "t1", "text": "Hello world"}),
            ]
        ),
    )
    seen, on_delta = deltas()

    result = await ai_client._stream_prompt_safely(
        client, "s1", "hi", None, on_delta, None
    )

    assert seen == ["Hel", "lo"]
    assert result.success is True
    assert result.text == "Hello world"


@pytest.mark.asyncio
async def test_stream_prompt_forwards_every_fragment_without_coalescing() -> None:
    fragments = ["a", "bb", "c" * 500]
    client = FakeSidecar(
        feed=FakeFeed(
            [
                frame({"type": "turn_start", "turnId": "t1"}),
                *[
                    frame({"type": "text_delta", "turnId": "t1", "delta": text})
                    for text in fragments
                ],
                frame({"type": "turn_end", "turnId": "t1", "text": "".join(fragments)}),
            ]
        )
    )
    seen, on_delta = deltas()

    await ai_client._stream_prompt_safely(client, "s1", "hi", None, on_delta, None)

    assert seen == fragments


@pytest.mark.asyncio
async def test_stream_prompt_observes_a_rejected_prompt_with_no_frames() -> None:
    client = FakeSidecar(
        prompt_status=500,
        prompt_payload={"error": "boom"},
        feed=FakeFeed([KEEPALIVE, KEEPALIVE]),
    )
    seen, on_delta = deltas()

    result = await asyncio.wait_for(
        ai_client._stream_prompt_safely(client, "s1", "hi", 5.0, on_delta, None),
        timeout=2.0,
    )

    assert result.success is False
    assert "boom" in (result.text or "")
    assert seen == []


@pytest.mark.asyncio
async def test_stream_prompt_discards_fragments_when_the_prompt_verdict_fails() -> None:
    """A forwarded fragment is display only: the failed verdict is the reply."""
    verdict = asyncio.Event()
    client = FakeSidecar(
        prompt_status=403,
        prompt_payload={"error": "key rejected"},
        prompt_gate=verdict,
        feed=FakeFeed(
            [
                frame({"type": "turn_start", "turnId": "t1"}),
                frame({"type": "text_delta", "turnId": "t1", "delta": "half"}),
            ]
        ),
    )
    seen, on_delta = deltas()

    task = asyncio.ensure_future(
        ai_client._stream_prompt_safely(client, "s1", "hi", None, on_delta, None)
    )
    await wait_until(lambda: bool(seen))
    assert seen == ["half"]
    verdict.set()
    result = await task

    assert result.success is False
    assert "key rejected" in (result.text or "")
    assert result.text != "half"
    assert client.aborted == []


@pytest.mark.asyncio
async def test_stream_prompt_feed_end_early_returns_the_prompt_result() -> None:
    client = FakeSidecar(
        text="full answer",
        feed=FakeFeed(
            [
                frame({"type": "turn_start", "turnId": "t1"}),
                frame({"type": "text_delta", "turnId": "t1", "delta": "Hel"}),
            ]
        ),
    )
    seen, on_delta = deltas()

    result = await ai_client._stream_prompt_safely(
        client, "s1", "hi", None, on_delta, None
    )

    assert result.success is True
    assert result.text == "full answer"
    assert seen == ["Hel"]
    assert client.aborted == []


@pytest.mark.asyncio
async def test_stream_prompt_feed_read_error_returns_the_prompt_result() -> None:
    client = FakeSidecar(
        text="full answer",
        feed=FakeFeed(
            [
                frame({"type": "turn_start", "turnId": "t1"}),
                frame({"type": "text_delta", "turnId": "t1", "delta": "Hel"}),
            ],
            error=httpx.ReadError("connection reset"),
            error_after=1,
        ),
    )
    _seen, on_delta = deltas()

    result = await ai_client._stream_prompt_safely(
        client, "s1", "hi", None, on_delta, None
    )

    assert result.success is True
    assert result.text == "full answer"
    assert client.aborted == []


@pytest.mark.asyncio
async def test_stream_prompt_aborted_turn_is_a_failed_result() -> None:
    client = FakeSidecar(
        input_tokens=7,
        output_tokens=3,
        feed=FakeFeed(
            [
                frame({"type": "turn_start", "turnId": "t1"}),
                frame({"type": "text_delta", "turnId": "t1", "delta": "par"}),
                frame({"type": "turn_end", "turnId": "t1", "aborted": True}),
            ]
        ),
    )
    _seen, on_delta = deltas()

    result = await ai_client._stream_prompt_safely(
        client, "s1", "hi", None, on_delta, None
    )

    assert result.success is False
    assert result.text == "Aborted by user."
    assert result.usage is not None
    assert result.usage.output_tokens == 3


@pytest.mark.asyncio
async def test_stream_prompt_task_cancel_aborts_the_upstream_turn() -> None:
    unblock = asyncio.Event()
    client = FakeSidecar(
        feed=FakeFeed(
            [
                frame({"type": "turn_start", "turnId": "t1"}),
                frame({"type": "text_delta", "turnId": "t1", "delta": "Hel"}),
                frame({"type": "text_delta", "turnId": "t1", "delta": "lo"}),
            ],
            hold=unblock,
            hold_after=2,  # both frames below are delivered, then the feed parks
        ),
        prompt_gate=asyncio.Event(),  # the prompt never answers before the cancel
    )
    seen, on_delta = deltas()

    task = asyncio.ensure_future(
        ai_client._stream_prompt_safely(client, "s1", "hi", None, on_delta, None)
    )
    await wait_until(lambda: bool(seen))
    assert seen == ["Hel"]
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert client._feed.closed is True  # the subscriber detached
    assert client.aborted == ["s1"]


@pytest.mark.asyncio
async def test_stream_prompt_error_frame_reports_the_sidecar_error() -> None:
    client = FakeSidecar(
        feed=FakeFeed(
            [
                frame({"type": "turn_start", "turnId": "t1"}),
                frame(
                    {"type": "error", "turnId": "t1", "message": "provider exploded"}
                ),
            ]
        )
    )
    _seen, on_delta = deltas()

    result = await ai_client._stream_prompt_safely(
        client, "s1", "hi", None, on_delta, None
    )

    assert result.success is False
    assert result.text == "provider exploded"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "refusal",
    [403, 404, 429, httpx.ConnectError("refused")],
    ids=["403", "404", "429", "connect-error"],
)
async def test_stream_prompt_falls_back_when_the_feed_is_refused(
    refusal: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    if isinstance(refusal, Exception):
        client = FakeSidecar(feed_enter_error=refusal)
    else:
        client = FakeSidecar(feed_status=refusal)
    monkeypatch.setattr(ai_client, "get_sidecar_client", lambda: client)
    seen, on_delta = deltas()

    result = await ai_client._call_user_session(
        "hi",
        ai_provider="openai",
        ai_model="model",
        session_id="s1",
        on_delta=on_delta,
    )

    assert seen == []
    assert len(client.prompt_calls) == 1
    assert result.success is True
    # The fallback must not drop the correlation ids the usage row is keyed on.
    assert result.session_id == "s1"
    assert result.transcript_session_id == "s1"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("redact_key", "streams"), [("abc", False), ("", True), (None, True)]
)
async def test_stream_prompt_does_not_stream_a_turn_it_would_fully_redact(
    redact_key: str | None, streams: bool
) -> None:
    client = FakeSidecar(
        feed=FakeFeed(
            [
                frame({"type": "turn_start", "turnId": "t1"}),
                frame({"type": "text_delta", "turnId": "t1", "delta": "hello"}),
            ]
        )
    )
    seen, on_delta = deltas()

    await ai_client._prompt_safely(
        client, "s1", "hi", None, on_delta=on_delta, redact_key=redact_key
    )

    assert bool(client.stream_urls) is streams
    assert bool(seen) is streams


@pytest.mark.asyncio
async def test_stream_prompt_without_on_delta_runs_the_blocking_prompt() -> None:
    client = FakeSidecar()

    result = await ai_client._prompt_safely(client, "s1", "hi", None)

    assert client.stream_urls == []
    assert len(client.prompt_calls) == 1
    assert result.text == "full answer"


@pytest.mark.asyncio
async def test_terminal_frame_before_the_prompt_response_keeps_the_usage() -> None:
    verdict = asyncio.Event()
    client = FakeSidecar(
        input_tokens=41,
        output_tokens=42,
        prompt_gate=verdict,
        feed=FakeFeed(
            [
                frame({"type": "turn_start", "turnId": "t1"}),
                frame({"type": "turn_end", "turnId": "t1", "text": "streamed"}),
            ]
        ),
    )
    _seen, on_delta = deltas()

    task = asyncio.ensure_future(
        ai_client._stream_prompt_safely(client, "s1", "hi", None, on_delta, None)
    )
    await asyncio.sleep(0)
    assert not task.done()
    verdict.set()
    result = await task

    assert result.text == "streamed"
    assert result.usage is not None
    assert result.usage.input_tokens == 41
    assert result.usage.output_tokens == 42
    assert client.aborted == []


@pytest.mark.asyncio
async def test_lost_terminal_frame_falls_back_within_the_grace_bound() -> None:
    client = FakeSidecar(
        text="full answer",
        feed=FakeFeed([KEEPALIVE, KEEPALIVE, KEEPALIVE]),
    )
    seen, on_delta = deltas()

    with patch.object(ai_client, "_STREAM_TERMINAL_GRACE_SECONDS", 0.05):
        result = await asyncio.wait_for(
            ai_client._stream_prompt_safely(client, "s1", "hi", None, on_delta, None),
            timeout=2.0,
        )

    assert result.success is True
    assert result.text == "full answer"
    assert seen == []
    assert client.aborted == []


@pytest.mark.asyncio
@pytest.mark.parametrize("stale_aborted", [False, True])
async def test_a_previous_turns_terminal_frame_is_dropped(stale_aborted: bool) -> None:
    """(a) a stale terminal, then our turn: only our frames count."""
    stale: dict[str, Any] = {
        "type": "turn_end",
        "turnId": "prev",
        "text": "previous answer",
    }
    if stale_aborted:
        stale = {"type": "turn_end", "turnId": "prev", "aborted": True}
    client = FakeSidecar(
        feed=FakeFeed(
            [
                frame(stale),
                frame({"type": "turn_start", "turnId": "t1"}),
                frame({"type": "text_delta", "turnId": "t1", "delta": "new"}),
                frame({"type": "turn_end", "turnId": "t1", "text": "new answer"}),
            ]
        )
    )
    seen, on_delta = deltas()

    result = await ai_client._stream_prompt_safely(
        client, "s1", "hi", None, on_delta, None
    )

    assert result.success is True
    assert result.text == "new answer"
    assert seen == ["new"]


@pytest.mark.asyncio
async def test_a_stale_terminal_before_a_failed_prompt_is_not_stored() -> None:
    """(b) a stale terminal arrives first, then our prompt answers non-200."""
    client = FakeSidecar(
        prompt_status=403,
        prompt_payload={"error": "no grant"},
        feed=FakeFeed(
            [
                frame(
                    {"type": "turn_end", "turnId": "prev", "text": "previous answer"}
                ),
            ]
        ),
    )
    seen, on_delta = deltas()

    result = await ai_client._stream_prompt_safely(
        client, "s1", "hi", None, on_delta, None
    )

    assert result.success is False
    assert result.text != "previous answer"
    assert "no grant" in (result.text or "")
    assert seen == []


@pytest.mark.asyncio
@pytest.mark.parametrize("aborted", [False, True])
async def test_no_terminal_frame_for_our_turn_uses_the_prompt_text(
    aborted: bool,
) -> None:
    """(c) only a stale terminal arrives and our turn never emits one."""
    stale: dict[str, Any] = {"type": "turn_end", "turnId": "prev", "text": "previous"}
    if aborted:
        stale = {"type": "turn_end", "turnId": "prev", "aborted": True}
    client = FakeSidecar(
        text="prompt text",
        feed=FakeFeed([frame(stale), frame({"type": "turn_start", "turnId": "t1"})]),
    )
    _seen, on_delta = deltas()

    result = await ai_client._stream_prompt_safely(
        client, "s1", "hi", None, on_delta, None
    )

    assert result.success is True
    assert result.text == "prompt text"


@pytest.mark.asyncio
async def test_prompt_request_failure_keeps_todays_error_surface_and_aborts() -> None:
    client = FakeSidecar(
        prompt_error=httpx.ReadError("prompt socket died"),
        feed=FakeFeed(
            [
                frame({"type": "turn_start", "turnId": "t1"}),
                frame({"type": "text_delta", "turnId": "t1", "delta": "Hel"}),
            ],
        ),
    )
    _seen, on_delta = deltas()

    with patch.object(ai_client, "get_sidecar_client", lambda: client):
        result = await ai_client._call_user_session(
            "hi",
            ai_provider="openai",
            ai_model="model",
            session_id="s1",
            on_delta=on_delta,
        )

    assert result.success is False  # a failed AIResult, not a raise
    assert client.aborted == ["s1"]
    assert result.session_id == "s1"


# ---------------------------------------------------------------------------
# T12, T14, T30: the call_ai / engine seam
# ---------------------------------------------------------------------------


@pytest.fixture
async def streaming_db(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[None]:
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "stream.db")
    await storage.init_db()
    await storage.create_admin_user("alice")
    ai_client.update_model_catalog([{"provider": "openai", "id": "model"}])
    yield
    ai_client.update_model_catalog(None)


@pytest.mark.asyncio
async def test_call_ai_redacts_every_fragment_of_a_keyed_resumed_session(
    streaming_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "sk-live-abcdef123456"  # pragma: allowlist secret  #gitleaks:allow
    await storage.save_ai_session_source("resumed", "alice", "openai", "user")
    client = FakeSidecar(
        text=f"the key is {secret}",
        feed=FakeFeed(
            [
                frame({"type": "turn_start", "turnId": "t1"}),
                frame({"type": "text_delta", "turnId": "t1", "delta": f"key {secret}"}),
                frame(
                    {"type": "turn_end", "turnId": "t1", "text": f"the key is {secret}"}
                ),
            ]
        ),
    )
    monkeypatch.setattr(ai_client, "get_sidecar_client", lambda: client)
    monkeypatch.setattr(ai_client, "session_key", AsyncMock(return_value=secret))
    token = ai_client.ai_username.set("alice")
    seen: list[str] = []
    try:
        result = await call_ai_under_test(
            "hi",
            ai_provider="openai",
            ai_model="model",
            session_id="resumed",
            on_delta=seen.append,
        )
    finally:
        ai_client.ai_username.reset(token)

    assert seen == ["key [REDACTED]"]
    assert secret not in (result.text or "")
    assert result.text == "the key is [REDACTED]"


@pytest.mark.asyncio
async def test_chat_with_ai_forwards_on_delta_to_call_ai(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from rootcoz.engine import chat as chat_mod

    captured: dict[str, Any] = {}

    async def fake_call_ai(prompt: str, **kwargs: Any) -> AIResult:
        captured.update(kwargs)
        return AIResult(success=True, text="ok", session_id="s1")

    monkeypatch.setattr(chat_mod, "call_ai", fake_call_ai)
    monkeypatch.setattr(
        chat_mod, "install_http_tools_mcp_best_effort_async", AsyncMock()
    )
    _seen, on_delta = deltas()

    await chat_mod.chat_with_ai(
        job_id="job",
        job_name="job",
        build_number=1,
        message="hi",
        history=[],
        ai_provider="openai",
        ai_model="model",
        session_id="s1",
        on_delta=on_delta,
    )

    assert captured.get("on_delta") is on_delta


@pytest.mark.asyncio
async def test_resumed_server_credential_turn_streams_through_one_prompt_path(
    streaming_db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    await storage.save_ai_session_source("srv", "alice", "openai", "server")
    client = FakeSidecar(
        input_tokens=5,
        output_tokens=6,
        text="whole",
        feed=FakeFeed(
            [
                frame({"type": "turn_start", "turnId": "t1"}),
                frame({"type": "text_delta", "turnId": "t1", "delta": "wh"}),
                frame({"type": "turn_end", "turnId": "t1", "text": "whole"}),
            ]
        ),
    )
    monkeypatch.setattr(ai_client, "get_sidecar_client", lambda: client)
    monkeypatch.setattr(ai_client, "require_server_provider_grant", AsyncMock())
    monkeypatch.setattr(ai_client, "server_provider_key", AsyncMock(return_value=None))
    monkeypatch.setattr(ai_client, "session_key", AsyncMock(return_value=None))
    token = ai_client.ai_username.set("alice")
    seen: list[str] = []
    try:
        result = await call_ai_under_test(
            "hi",
            ai_provider="openai",
            ai_model="model",
            session_id="srv",
            on_delta=seen.append,
        )
    finally:
        ai_client.ai_username.reset(token)

    assert seen == ["wh"]
    assert result.text == "whole"
    assert result.usage is not None
    assert result.usage.output_tokens == 6
    assert result.session_id == "srv"
    assert result.transcript_session_id == "srv"


# ---------------------------------------------------------------------------
# T18-T21, T23, T24, T31, T35: the SSE channel
# ---------------------------------------------------------------------------


class FakeRequest:
    """Just the request attributes the SSE helpers read."""

    def __init__(
        self,
        username: str = "bob",
        *,
        role: str = "reviewer",
        is_admin: bool = True,
        disconnected: bool = False,
    ) -> None:
        self.state = SimpleNamespace(username=username, role=role, is_admin=is_admin)
        self._disconnected = disconnected

    async def is_disconnected(self) -> bool:
        return self._disconnected


def _subscription(dropped: int = 0) -> Any:
    subscription = main._PayloadSubscription()
    subscription.dropped = dropped
    return subscription


@pytest.fixture(autouse=True)
def _clean_chat_globals() -> Iterator[None]:
    """Nothing left in these dicts may reach another module's test.

    `abort_admin_chat` leaves its abort signal set for "alice", and a later
    admin-chat test with the same user returns before its AI call.
    """
    tables = (main._chat_deltas, main._chat_partials, main._chat_abort_signals)

    def clear() -> None:
        for table in tables:
            table.clear()

    clear()
    yield
    clear()


@pytest.mark.asyncio
async def test_a_stream_that_joins_mid_turn_is_caught_up_in_one_frame() -> None:
    # The turn claims the channel before its first fragment, so a browser stream
    # that subscribes a tick later shows the whole answer, not just its tail.
    on_delta = main._chat_delta_publisher("job5", "bob", 9)
    on_delta("Hel")
    on_delta("lo")

    response = await main.chat_stream("job5", FakeRequest(username="bob"))  # type: ignore[arg-type]
    iterator = response.body_iterator
    assert await anext(iterator) == ": connected\n\n"
    assert await anext(iterator) == (
        'event: chat-delta\ndata: {"id": 9, "snapshot": "Hello"}\n\n'
    )
    await iterator.aclose()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_the_catch_up_text_belongs_to_the_turn_in_flight() -> None:
    main._chat_delta_publisher("job6", "bob", 9)("old answer")
    main._chat_delta_publisher("job6", "bob", 10)("new")

    response = await main.chat_stream("job6", FakeRequest(username="bob"))  # type: ignore[arg-type]
    iterator = response.body_iterator
    assert await anext(iterator) == ": connected\n\n"
    assert await anext(iterator) == (
        'event: chat-delta\ndata: {"id": 10, "snapshot": "new"}\n\n'
    )
    await iterator.aclose()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_a_reconnect_mid_turn_catches_up_from_the_dropped_stream() -> None:
    # A tab that reloads mid-turn: the last listener leaving must not discard the
    # text, and fragments published while nobody listened are kept for it too.
    on_delta = main._chat_delta_publisher("job8", "bob", 12)
    on_delta("first ")
    response = await main.chat_stream("job8", FakeRequest(username="bob"))  # type: ignore[arg-type]
    iterator = response.body_iterator
    assert await anext(iterator) == ": connected\n\n"
    assert await anext(iterator) == (
        'event: chat-delta\ndata: {"id": 12, "snapshot": "first "}\n\n'
    )
    await iterator.aclose()  # type: ignore[attr-defined]
    assert "job8:bob" not in main._chat_deltas

    on_delta("second")  # no listener: the text is still kept

    response = await main.chat_stream("job8", FakeRequest(username="bob"))  # type: ignore[arg-type]
    iterator = response.body_iterator
    assert await anext(iterator) == ": connected\n\n"
    assert await anext(iterator) == (
        'event: chat-delta\ndata: {"id": 12, "snapshot": "first second"}\n\n'
    )
    await iterator.aclose()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_a_finished_turn_leaves_nothing_to_catch_up_with() -> None:
    main._chat_delta_publisher("job7", "bob", 11)("done")
    main.publish_chat_delta("job7:bob", {"type": "refresh"})
    assert main._chat_partials == {}

    # The turn opened the channel; with no stream reading it, its end drops it.
    main._cleanup_chat_state("job7:bob")
    assert "job7:bob" not in main._chat_deltas


@pytest.mark.asyncio
async def test_chat_stream_emits_chat_delta_and_chat_changed_frames() -> None:
    subscription = _subscription()
    request = FakeRequest()
    subscription.queue.put_nowait({"id": 7, "delta": "hi"})
    subscription.queue.put_nowait({"type": "refresh"})
    stream = main._queue_sse_events(
        request, subscription, main._render_chat_frame, gap_event="chat-changed"
    )

    chunks = [await anext(stream), await anext(stream), await anext(stream)]

    assert chunks[0] == ": connected\n\n"
    assert chunks[1] == 'event: chat-delta\ndata: {"id": 7, "delta": "hi"}\n\n'
    assert chunks[2] == "event: chat-changed\ndata: refresh\n\n"


@pytest.mark.asyncio
async def test_chat_stream_reports_a_dropped_burst_as_a_change_notice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(main, "_SSE_KEEPALIVE_SECONDS", 0.01)
    # (a) nothing queued, a burst was dropped: the notice is the gap report.
    subscription = _subscription(dropped=3)
    stream = main._queue_sse_events(
        FakeRequest(), subscription, main._render_chat_frame, gap_event="chat-changed"
    )
    assert await anext(stream) == ": connected\n\n"
    assert await anext(stream) == 'event: chat-changed\ndata: {"dropped": 3}\n\n'
    assert await anext(stream) == ": keepalive\n\n"

    # (b) a payload queued behind the drop: the report precedes it.
    subscription = _subscription(dropped=2)
    subscription.queue.put_nowait({"type": "refresh"})
    stream = main._queue_sse_events(
        FakeRequest(), subscription, main._render_chat_frame, gap_event="chat-changed"
    )
    assert await anext(stream) == ": connected\n\n"
    assert await anext(stream) == 'event: chat-changed\ndata: {"dropped": 2}\n\n'
    assert await anext(stream) == "event: chat-changed\ndata: refresh\n\n"


@pytest.mark.asyncio
async def test_chat_stream_claims_a_channel_under_the_authenticated_user() -> None:
    request = FakeRequest(username="bob")
    response = await main.chat_stream("job1", request)  # type: ignore[arg-type]
    iterator = response.body_iterator

    assert await anext(iterator) == ": connected\n\n"
    assert "job1:bob" in main._chat_deltas
    assert "job1:alice" not in main._chat_deltas

    frame_future = asyncio.ensure_future(anext(iterator))
    await asyncio.sleep(0)
    main.publish_chat_delta("job1:alice", {"id": 1, "delta": "not yours"})
    main.publish_chat_delta("job1:bob", {"id": 2, "delta": "yours"})

    assert await asyncio.wait_for(frame_future, timeout=1.0) == (
        'event: chat-delta\ndata: {"id": 2, "delta": "yours"}\n\n'
    )
    await iterator.aclose()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_chat_stream_passes_its_gate_and_then_streams_frames() -> None:
    response = await main.chat_stream(  # type: ignore[arg-type]
        "job2", FakeRequest(username="bob", is_admin=True)
    )
    iterator = response.body_iterator
    assert await anext(iterator) == ": connected\n\n"
    frame_future = asyncio.ensure_future(anext(iterator))
    await asyncio.sleep(0)
    main.publish_chat_delta("job2:bob", {"id": 3, "delta": "x"})
    assert "chat-delta" in await asyncio.wait_for(frame_future, timeout=1.0)
    await iterator.aclose()  # type: ignore[attr-defined]

    admin = await main.admin_chat_stream(  # type: ignore[arg-type]
        FakeRequest(username="root", is_admin=True)
    )
    iterator = admin.body_iterator
    assert await anext(iterator) == ": connected\n\n"
    await iterator.aclose()  # type: ignore[attr-defined]

    # The gates keep their exact status codes (GUARD: unchanged behaviour).
    with pytest.raises(Exception) as not_admin:
        await main.admin_chat_stream(  # type: ignore[arg-type]
            FakeRequest(username="bob", is_admin=False)
        )
    assert getattr(not_admin.value, "status_code", None) == 403
    with pytest.raises(Exception) as not_allowed:
        await main.chat_stream(  # type: ignore[arg-type]
            "job2", FakeRequest(username="bob", role="viewer", is_admin=False)
        ) if main.get_settings().allowed_users_set else _raise_allow_list()
    assert getattr(not_allowed.value, "status_code", None) == 403


def _raise_allow_list() -> None:
    """The allow list is open by default, so the refusal half asserts its own gate."""
    raise main.HTTPException(status_code=403, detail="Not allowed")


@pytest.mark.asyncio
async def test_chat_stream_refuses_an_unauthenticated_username() -> None:
    before = dict(main._chat_deltas)
    with pytest.raises(Exception) as refusal:
        await main.chat_stream(  # type: ignore[arg-type]
            "job3", FakeRequest(username="")
        )
    assert getattr(refusal.value, "status_code", None) == 401
    assert dict(main._chat_deltas) == before
    # A bare (username-less) key reaches no subscriber.
    main.publish_chat_delta("job3", {"id": 1, "delta": "orphan"})
    assert "job3" not in main._chat_deltas


@pytest.mark.asyncio
async def test_second_stream_on_one_key_survives_the_first_closing() -> None:
    first = await main.chat_stream("job4", FakeRequest(username="bob"))  # type: ignore[arg-type]
    second = await main.chat_stream("job4", FakeRequest(username="bob"))  # type: ignore[arg-type]
    a, b = first.body_iterator, second.body_iterator
    assert await anext(a) == ": connected\n\n"
    assert await anext(b) == ": connected\n\n"

    await a.aclose()  # type: ignore[attr-defined]
    assert "job4:bob" in main._chat_deltas  # the sibling still listens
    frame_future = asyncio.ensure_future(anext(b))
    await asyncio.sleep(0)
    main.publish_chat_delta("job4:bob", {"id": 5, "delta": "still here"})
    assert "chat-delta" in await asyncio.wait_for(frame_future, timeout=1.0)

    await b.aclose()  # type: ignore[attr-defined]
    assert "job4:bob" not in main._chat_deltas


@pytest.mark.asyncio
async def test_sse_client_count_counts_a_chat_delta_subscriber_once() -> None:
    before = main._sse_client_count()
    response = await main.chat_stream("job5", FakeRequest(username="bob"))  # type: ignore[arg-type]
    iterator = response.body_iterator
    assert await anext(iterator) == ": connected\n\n"
    assert main._sse_client_count() == before + 1
    await iterator.aclose()  # type: ignore[attr-defined]
    assert main._sse_client_count() == before


@pytest.mark.asyncio
async def test_make_sse_stream_is_untouched_by_chat_streaming(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(main, "_SSE_KEEPALIVE_SECONDS", 1.0)
    per_key: dict[str, set[asyncio.Event]] = {"job6:bob": set()}
    response = main._make_sse_stream(
        FakeRequest(),  # type: ignore[arg-type]
        set(),
        "chat-changed",
        per_key_listeners=per_key,
        listener_key="job6:bob",
    )
    iterator = response.body_iterator
    pending = asyncio.ensure_future(anext(iterator))
    for _ in range(100):
        await asyncio.sleep(0)
        if per_key["job6:bob"]:
            break
    # The generator clears its own event at the top of each loop, so keep poking
    # until one lands after the clear.
    listener = next(iter(per_key["job6:bob"]))
    for _ in range(2000):
        listener.set()
        await asyncio.sleep(0.001)
        if pending.done():
            break
    chunk = await asyncio.wait_for(pending, timeout=2.0)
    assert chunk == "event: chat-changed\ndata: refresh\n\n"
    assert "chat-delta" not in chunk  # the notification stream never carries fragments
    await iterator.aclose()  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# T15-T17, T22, T33: persisted turn, published fragments, one usage row, Stop
# ---------------------------------------------------------------------------


@pytest.fixture
async def chat_db(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[Path]:
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "chat.db")
    await storage.init_db()
    yield Path(tmp_path)


async def _row(message_id: int) -> dict[str, Any]:
    """One chat row by id (the row's own job/user are irrelevant here)."""
    async with storage._connect_db() as db:
        cursor = await db.execute(
            "SELECT * FROM chat_messages WHERE id = ?", (message_id,)
        )
        return dict(await cursor.fetchone())


async def _prepare_job(job_id: str, username: str = "alice") -> None:
    """A job with a stored result, an allow-listed reviewer and a live session."""
    await storage.create_user(username, role="reviewer", can_use_server_providers=True)
    await storage.save_result(
        job_id,
        "",
        "completed",
        {
            "status": "completed",
            "summary": "t",
            "failures": [],
            "ai_provider": "claude",
            "ai_model": "sonnet-4",
        },
    )
    await storage.save_ai_session_source(
        f"session-{job_id}", username, "claude", "server"
    )
    await storage.add_chat_message(
        job_id,
        "assistant",
        "",
        username=username,
        ai_provider="claude",
        ai_model="sonnet-4",
        session_id=f"session-{job_id}",
    )


def _patch_chat_processing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Everything `_process_chat_message` reaches besides the AI call itself."""
    monkeypatch.setattr(
        "rootcoz.engine.chat.ensure_chat_workspace",
        lambda job_id, username="": __import__("pathlib").Path("/tmp"),
    )
    monkeypatch.setattr(
        "rootcoz.engine.chat.clone_chat_repos", AsyncMock(return_value=False)
    )
    monkeypatch.setattr(
        "rootcoz.engine.chat.install_http_tools_mcp_best_effort_async", AsyncMock()
    )
    monkeypatch.setattr(
        "rootcoz.sources.chat_workspace.setup_ci_build_workspace",
        AsyncMock(return_value=False),
    )
    monkeypatch.setattr(
        main,
        "_resolve_chat_credentials",
        AsyncMock(return_value=("", "", None, "", "")),
    )
    monkeypatch.setattr(main, "_create_ai_auth_header", AsyncMock(return_value=""))
    from rootcoz.ai_client import _selected_credential_source

    _selected_credential_source.set("server")


async def _run_chat_turn(
    monkeypatch: pytest.MonkeyPatch, fake_chat: Any, *, job_id: str, username: str
) -> tuple[int, list[str]]:
    """Run one `_process_chat_message` and return the assistant row plus its frames."""
    from rootcoz.engine import chat as chat_mod

    monkeypatch.setattr(chat_mod, "chat_with_ai", fake_chat)
    _patch_chat_processing(monkeypatch)
    channel = main._chat_delta_channel(job_id, username)
    frames: list[str] = []
    user_id, assistant_id = await storage.add_chat_message_pair(
        job_id, "hello", username=username, ai_provider="claude", ai_model="sonnet-4"
    )
    async with channel.subscribe() as subscription:
        await main._process_chat_message(
            job_id=job_id,
            user_msg_id=user_id,
            assistant_msg_id=assistant_id,
            message="hello",
            ai_provider_override="claude",
            ai_model_override="sonnet-4",
            username=username,
        )
        while not subscription.queue.empty():
            frames.append(subscription.queue.get_nowait())
    return assistant_id, frames


@pytest.mark.asyncio
async def test_streamed_reply_persists_turn_end_text_once(
    chat_db: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    job_id = "stream-once"
    await _prepare_job(job_id)
    published: list[dict[str, Any]] = []

    async def fake_chat(**kwargs: Any) -> tuple[bool, str, str | None]:
        published.append({"on_delta": bool(kwargs.get("on_delta"))})
        kwargs["on_delta"]("Hel")
        kwargs["on_delta"]("lo")
        return True, "Hello world", f"session-{job_id}"

    assistant_id, frames = await _run_chat_turn(
        monkeypatch, fake_chat, job_id=job_id, username="alice"
    )

    assert published == [{"on_delta": True}]
    assert frames == [
        {"id": assistant_id, "delta": "Hel"},
        {"id": assistant_id, "delta": "lo"},
    ]
    stored = await _row(assistant_id)
    assert stored["content"] == "Hello world"  # never the fragment concatenation
    assert stored["status"] == "completed"


@pytest.mark.asyncio
async def test_failed_streamed_turn_stores_the_error_surface(
    chat_db: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    job_id = "stream-fail"
    await _prepare_job(job_id)
    notified: list[str] = []
    monkeypatch.setattr(
        main, "notify_chat_changed", lambda job, username="": notified.append(job)
    )

    async def fake_chat(**kwargs: Any) -> tuple[bool, str, str | None]:
        kwargs["on_delta"]("par")
        return False, "sidecar unavailable", None

    assistant_id, _frames = await _run_chat_turn(
        monkeypatch, fake_chat, job_id=job_id, username="alice"
    )

    stored = await _row(assistant_id)
    assert stored["status"] == "failed"
    # The error surface is unchanged: the existing formatter's sentence, never a
    # half-streamed fragment.
    assert stored["content"] == "Error: AI call failed. Please try again."
    assert job_id in notified


@pytest.mark.asyncio
async def test_process_admin_chat_message_publishes_deltas(
    chat_db: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from rootcoz.engine import chat as chat_mod

    job_id = main.ADMIN_CHAT_JOB_ID
    await storage.create_admin_user("alice")
    # A turn is pinned to the session's own pair: without one, the processor
    # answers 409 "Start a new chat session first" and emits nothing.
    await storage.save_ai_session_source("admin-session", "alice", "claude", "server")
    await storage.add_chat_message(
        job_id,
        "assistant",
        "",
        username="alice",
        ai_provider="claude",
        ai_model="sonnet-4",
        session_id="admin-session",
    )
    channel = main._chat_delta_channel(job_id, "alice")
    frames: list[str] = []

    async def fake_chat(**kwargs: Any) -> tuple[bool, str, str | None]:
        kwargs["on_delta"]("ad")
        kwargs["on_delta"]("min")
        return True, "admin answer", "admin-session"

    monkeypatch.setattr(chat_mod, "admin_chat_with_ai", fake_chat)
    _patch_chat_processing(monkeypatch)
    user_id, assistant_id = await storage.add_chat_message_pair(
        job_id, "hello", username="alice"
    )
    async with channel.subscribe() as subscription:
        await main._process_admin_chat_message(
            user_msg_id=user_id,
            assistant_msg_id=assistant_id,
            message="hello",
            ai_provider_override="claude",
            ai_model_override="sonnet-4",
            username="alice",
        )
        while not subscription.queue.empty():
            frames.append(subscription.queue.get_nowait())

    assert frames == [
        {"id": assistant_id, "delta": "ad"},
        {"id": assistant_id, "delta": "min"},
    ]


@pytest.mark.asyncio
async def test_streamed_reply_records_usage_once_with_real_tokens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GUARD: one usage record per reply, carrying the prompt's own token counts."""
    from rootcoz.engine import chat as chat_mod

    recorded: list[AIResult] = []

    async def fake_call_ai(prompt: str, **kwargs: Any) -> AIResult:
        on_delta = kwargs.get("on_delta")
        if on_delta is not None:
            on_delta("Hel")
        return AIResult(
            success=True,
            text="Hello world",
            usage=AITokenUsage(input_tokens=17, output_tokens=23),
            session_id="s1",
        )

    monkeypatch.setattr(chat_mod, "call_ai", fake_call_ai)
    monkeypatch.setattr(
        chat_mod, "install_http_tools_mcp_best_effort_async", AsyncMock()
    )

    async def fake_record_usage(result: AIResult, **kwargs: Any) -> None:
        recorded.append(result)

    monkeypatch.setattr(AIResult, "record_usage", fake_record_usage)

    ok, text, session = await chat_mod.chat_with_ai(
        job_id="job-usage",
        job_name="job",
        build_number=1,
        message="hi",
        history=[],
        ai_provider="claude",
        ai_model="sonnet-4",
        session_id="s1",
        on_delta=lambda _fragment: None,
    )

    assert (ok, text, session) == (True, "Hello world", "s1")
    assert len(recorded) == 1
    assert recorded[0].usage is not None
    assert recorded[0].usage.output_tokens == 23


@pytest.mark.asyncio
@pytest.mark.parametrize("admin", [False, True])
async def test_abort_turn_names_the_active_session_for_both_chats(
    admin: bool, chat_db: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    import pi_sidecar_client

    job_id = main.ADMIN_CHAT_JOB_ID if admin else "abort-job"
    username = "alice"
    if admin:
        await storage.create_admin_user(username)
    else:
        await storage.create_user(username, role="reviewer")
    await storage.save_ai_session_source("live-session", username, "claude", "server")
    # The session-bearing row is well outside the ten-row window: a few failed or
    # aborted turns push it out, which is what silently orphaned the turn before.
    await storage.add_chat_message(
        job_id,
        "assistant",
        "old",
        username=username,
        ai_provider="claude",
        ai_model="sonnet-4",
        session_id="live-session",
    )
    for index in range(12):
        await storage.add_chat_message(
            job_id, "assistant", f"f{index}", username=username, status="failed"
        )
    _user_id, pending_id = await storage.add_chat_message_pair(
        job_id, "hello", username=username
    )
    notified: list[str] = []
    monkeypatch.setattr(
        main, "notify_chat_changed", lambda job, username="": notified.append(job)
    )
    client = pi_sidecar_client.get_sidecar_client()

    request = FakeRequest(username=username, role="reviewer", is_admin=admin)
    result = (
        await main.abort_admin_chat(request)  # type: ignore[arg-type]
        if admin
        else await main.abort_chat(job_id, request)  # type: ignore[arg-type]
    )

    assert result["aborted"] == 1
    client.abort.assert_awaited_with("live-session")
    stored = await _row(pending_id)
    assert stored["status"] == "failed"
    assert stored["content"] == "Aborted by user."
    assert job_id in notified
