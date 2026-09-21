"""The shadow counter is the only evidence for arming a tripwire, so its numbers have to be per run."""

import asyncio
import logging

from fastbrowse.evals.shadow import shadow_counts

logger = logging.getLogger("fastbrowse")


def would_fire(name: str) -> None:
    logger.info("tripwire %s would have recovered", name, extra={"tripwire": name})


async def one_run(name: str, hold: asyncio.Event) -> dict[str, int]:
    with shadow_counts() as counts:
        would_fire(name)
        await hold.wait()
        return dict(counts)


async def test_concurrent_runs_each_count_only_their_own_tripwires() -> None:
    """`--concurrency N` overlaps these contexts, and the handler is process-global.

    Counting on the handler credited every concurrent run with every run's tripwires, and the first context
    out restored the logger level under the others, dropping the rest of their records.
    """
    hold = asyncio.Event()
    runs = [asyncio.create_task(one_run(name, hold)) for name in ("no_progress", "action_repetition")]
    await asyncio.sleep(0)
    hold.set()
    first, second = await asyncio.gather(*runs)
    assert first == {"no_progress": 1}
    assert second == {"action_repetition": 1}


async def test_the_level_outlives_a_run_that_finishes_while_another_is_counting() -> None:
    previous = logger.level
    with shadow_counts() as outer:
        with shadow_counts():
            pass
        # An INFO record after the inner context closed: the level it forced must still be in place.
        would_fire("plan_stagnation")
    assert outer == {"plan_stagnation": 1}
    assert logger.level == previous
