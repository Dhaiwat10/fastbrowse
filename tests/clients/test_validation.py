import asyncio
import time

import httpx
import pytest

from fastbrowse.clients.validation import post_with_retry


async def test_a_stalled_request_is_raced_and_the_loser_cancelled() -> None:
    calls: list[int] = []
    cancelled = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(len(calls))
        if len(calls) == 1:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise
        return httpx.Response(200, json={"ok": True})

    reserved: list[None] = []
    started = time.monotonic()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        response = await post_with_retry(
            http,
            "https://jev.test/v1",
            {},
            {},
            attempt_seconds=30.0,
            hedge_seconds=0.05,
            before_retry=lambda: reserved.append(None),
        )
    assert response is not None and response.status_code == 200
    assert time.monotonic() - started < 1.0
    assert len(calls) == 2 and len(reserved) == 1
    assert cancelled.is_set()


async def test_a_fast_request_is_not_hedged() -> None:
    calls: list[int] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(200)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        response = await post_with_retry(http, "https://jev.test/v1", {}, {}, attempt_seconds=30.0, hedge_seconds=5.0)
    assert response is not None and len(calls) == 1


async def test_retry_waits_for_the_servers_retry_after(monkeypatch: pytest.MonkeyPatch) -> None:
    waits: list[float] = []

    async def record(delay: float) -> None:
        waits.append(delay)

    monkeypatch.setattr("fastbrowse.clients.validation.asyncio.sleep", record)
    responses = iter([httpx.Response(429, headers={"retry-after-ms": "250"}), httpx.Response(200)])
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: next(responses))) as http:
        response = await post_with_retry(http, "https://jev.test/v1", {}, {}, attempt_seconds=30.0, hedge_seconds=5.0)
    assert response is not None and response.status_code == 200
    assert waits == [0.25]
