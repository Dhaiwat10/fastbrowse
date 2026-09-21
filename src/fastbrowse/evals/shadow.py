"""Counting the tripwires a run WOULD have recovered on, for the suites that judge whether to arm one."""

import logging
from collections import Counter
from collections.abc import Generator
from contextlib import contextmanager
from contextvars import ContextVar

_counts: ContextVar[Counter[str] | None] = ContextVar("fastbrowse_shadow_counts", default=None)
"""The run being counted, per task. A handler is process-global; a context variable is not, and an asyncio
task inherits the context it was created in, so the agent's log record finds its own run's counter."""


class ShadowCounter(logging.Handler):
    """Count the tripwires that would have recovered a run, so a suite reports their rate on runs that passed.

    That rate is the only evidence for arming one: a tripwire is worth its cost when it rarely fires on a run
    that was going to succeed anyway. Reading it off a log record keeps the counting out of the agent.
    """

    def emit(self, record: logging.LogRecord) -> None:
        tripwire = getattr(record, "tripwire", None)
        counts = _counts.get()
        if counts is not None and isinstance(tripwire, str):
            counts[tripwire] += 1


@contextmanager
def shadow_counts() -> Generator[Counter[str]]:
    """One run's would-fires, counted per task rather than per process.

    `--concurrency N` runs N tasks at once, so overlapping contexts are ordinary. Counting on the handler
    would credit every concurrent run with every run's tripwires, and the first context to exit would restore
    the logger level under the others -- corrupting the one number that decides whether a tripwire is armed.

    The level is forced because a shadow tripwire logs at INFO and the suites otherwise leave the library
    logger at its default, where the root's WARNING drops exactly the records being counted - producing a
    clean zero that reads like evidence of no false positives. It is restored by the last context out.
    """
    global _counting, _restore_level
    counts: Counter[str] = Counter()
    logger = logging.getLogger("fastbrowse")
    if _counting == 0:
        _restore_level = logger.level
        logger.addHandler(_handler)
        logger.setLevel(logging.INFO)
    _counting += 1
    token = _counts.set(counts)
    try:
        yield counts
    finally:
        _counts.reset(token)
        _counting -= 1
        if _counting == 0:
            logger.removeHandler(_handler)
            logger.setLevel(_restore_level)


_handler = ShadowCounter()
_restore_level = logging.NOTSET
_counting = 0
"""How many runs are being counted. The handler and the forced level are shared, so the last one out restores
the level the suite had; an earlier restore would silently drop the records the others still need."""
