import asyncio

import pytest


@pytest.fixture(autouse=True)
def no_retry_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    """Retries still happen; only their backoff is skipped."""

    real_sleep = asyncio.sleep

    async def immediate(_delay: float) -> None:
        await real_sleep(0)

    monkeypatch.setattr("fastbrowse.clients.validation.asyncio.sleep", immediate)
