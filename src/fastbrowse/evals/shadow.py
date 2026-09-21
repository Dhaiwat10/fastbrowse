"""Counting the tripwires a run WOULD have recovered on, for the suites that judge whether to arm one."""

import logging
from collections import Counter
from collections.abc import Generator
from contextlib import contextmanager


class ShadowCounter(logging.Handler):
    """Count the tripwires that would have recovered a run, so a suite reports their rate on runs that passed.

    That rate is the only evidence for arming one: a tripwire is worth its cost when it rarely fires on a run
    that was going to succeed anyway. Reading it off a log record keeps the counting out of the agent.
    """

    def __init__(self) -> None:
        super().__init__()
        self.counts: Counter[str] = Counter()

    def emit(self, record: logging.LogRecord) -> None:
        tripwire = getattr(record, "tripwire", None)
        if isinstance(tripwire, str):
            self.counts[tripwire] += 1


@contextmanager
def shadow_counts() -> Generator[Counter[str]]:
    """One run's would-fires. Tasks run one at a time, so one handler at a time sees them.

    The level is forced because a shadow tripwire logs at INFO and the suites otherwise leave the library
    logger at its default, where the root's WARNING drops exactly the records being counted - producing a
    clean zero that reads like evidence of no false positives.
    """
    handler = ShadowCounter()
    logger = logging.getLogger("fastbrowse")
    previous = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        yield handler.counts
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous)
