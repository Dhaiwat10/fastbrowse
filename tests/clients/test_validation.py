import asyncio
import time

import httpx
import pytest

from fastbrowse.clients.validation import RequestUsage, post_with_retry, with_discarded
from fastbrowse.models import CostBasis, CostComponent, CostLine


@pytest.mark.parametrize("winner", [1, 2])
async def test_a_stalled_request_is_raced_and_the_loser_cancelled(winner: int) -> None:
    calls: list[int] = []
    cancelled = asyncio.Event()
    hedged = asyncio.Event()
    usage = RequestUsage()

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(len(calls))
        number = len(calls)
        if number == 2:
            hedged.set()
        if number != winner:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise
        await hedged.wait()
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
            usage=usage,
        )
    assert response is not None and response.status_code == 200
    assert time.monotonic() - started < 1.0
    assert len(calls) == 2 and len(reserved) == 1
    assert cancelled.is_set()
    assert usage.unaccounted_requests == 1


async def test_a_fast_request_is_not_hedged() -> None:
    calls: list[int] = []
    usage = RequestUsage()

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(200)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        response = await post_with_retry(
            http, "https://jev.test/v1", {}, {}, attempt_seconds=30.0, hedge_seconds=5.0, usage=usage
        )
    assert response is not None and len(calls) == 1
    assert usage.unaccounted_requests == 0


async def test_retry_waits_for_the_servers_retry_after(monkeypatch: pytest.MonkeyPatch) -> None:
    waits: list[float] = []

    async def record(delay: float) -> None:
        waits.append(delay)

    monkeypatch.setattr("fastbrowse.clients.validation.asyncio.sleep", record)
    responses = iter([httpx.Response(429, headers={"retry-after-ms": "250"}), httpx.Response(200)])
    usage = RequestUsage()
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: next(responses))) as http:
        response = await post_with_retry(
            http, "https://jev.test/v1", {}, {}, attempt_seconds=30.0, hedge_seconds=5.0, usage=usage
        )
    assert response is not None and response.status_code == 200
    assert waits == [0.25]
    assert usage.unaccounted_requests == 0


async def test_a_brief_outage_is_outlasted(monkeypatch: pytest.MonkeyPatch) -> None:
    waits: list[float] = []

    async def record(delay: float) -> None:
        waits.append(delay)

    monkeypatch.setattr("fastbrowse.clients.validation.asyncio.sleep", record)
    responses = iter([*(httpx.Response(503) for _ in range(4)), httpx.Response(200)])
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: next(responses))) as http:
        response = await post_with_retry(http, "https://jev.test/v1", {}, {}, attempt_seconds=30.0, hedge_seconds=5.0)
    assert response is not None and response.status_code == 200
    assert sum(waits) > 10


async def test_a_completed_hedge_loser_still_counts() -> None:
    hedged = asyncio.Event()
    calls = 0
    usage = RequestUsage()

    async def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 2:
            hedged.set()
        await hedged.wait()
        return httpx.Response(200)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        response = await post_with_retry(
            http, "https://jev.test/v1", {}, {}, attempt_seconds=30.0, hedge_seconds=0.01, usage=usage
        )
    assert response is not None and response.is_success
    assert calls == 2 and usage.unaccounted_requests == 1


def test_a_discarded_jev_request_is_charged_as_an_estimate() -> None:
    cost = CostLine(component=CostComponent.JEV, basis=CostBasis.METERED, dollars=0.002, input_tokens=900)

    charged = with_discarded(cost, RequestUsage(unaccounted_requests=1))

    assert (charged.basis, charged.dollars, charged.input_tokens) == (CostBasis.ESTIMATED, 0.004, 1800)
    assert with_discarded(cost, RequestUsage()) is cost
